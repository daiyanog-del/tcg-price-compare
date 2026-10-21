"""
unreleased_image_store.py — X投稿画像の Storage 保存・クロップ・元画像への復元

取り込み時は元画像を unreleased/originals/ に退避したうえで、クロップが必要な候補
（画像全体でない・URL一致・座標が正しい）は常に自動適用する。ずれていたら管理画面から
手動クロップで直すか、元画像に戻せる。
admin_unreleased.py と watch_x_unreleased.py の両方から使用する。
Flask に依存しないため、Cron スクリプトからも安全にインポートできる。
"""

import hashlib
import io
import logging
import math
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from PIL import Image, ImageOps
from supabase import Client

logger = logging.getLogger(__name__)

_STORAGE_BUCKET = "official-card-images"


_EXT_MAP = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
            "image/gif": "gif", "image/webp": "webp"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _merge_extraction_raw(sb: Client, card_id: int, updates: dict) -> bool:
    """
    extraction_raw を最新の値で読み直し、updates のキーだけを上書きして書き戻す。
    複数回の読み→書きで先行する書き込み（card_image_url・original_image・crop_state）を
    消さないよう、書き込みごとに必ず最新を読む。失敗時は警告ログを出して False。
    """
    try:
        card_resp = sb.table("unreleased_cards").select("extraction_raw").eq("id", card_id).execute()
        if not card_resp.data:
            logger.warning(f"[image_store] extraction_raw 更新対象の行が見つかりません: card_id={card_id}")
            return False
        raw = dict(card_resp.data[0].get("extraction_raw") or {})
        raw.update(updates)
        sb.table("unreleased_cards").update({"extraction_raw": raw}).eq("id", card_id).execute()
        return True
    except Exception as e:
        logger.warning(f"[image_store] extraction_raw 更新失敗（無視）: {e}")
        return False


def _read_extraction_raw(sb: Client, card_id: int) -> dict:
    try:
        card_resp = sb.table("unreleased_cards").select("extraction_raw").eq("id", card_id).execute()
        if card_resp.data:
            return dict(card_resp.data[0].get("extraction_raw") or {})
    except Exception as e:
        logger.warning(f"[image_store] extraction_raw 取得失敗（無視）: {e}")
    return {}


# 新着欄など小さく並べる表示用の縮小版（2026-10-21）。スマホの新着欄は1枚 幅約113px で、高精細画面の2〜3倍に足りる幅にする。
# 元画像は幅290〜410px の PNG/JPEG が多く、重さの大半は形式（PNG 1枚200〜350KB）。WebP にすると1枚20〜35KB。
_THUMB_DIR = "unreleased/thumbs"
_THUMB_MAX_WIDTH = 300
_THUMB_QUALITY = 75
# Cache-Control は原寸と同じ既定（1時間）のまま。長くすると、削除した画像が CDN・ブラウザに長く残る


def thumb_path(card_id: int) -> str:
    return f"{_THUMB_DIR}/{card_id}.webp"


def make_thumbnail(image_bytes: bytes) -> bytes:
    """画像バイトから縮小版（幅 _THUMB_MAX_WIDTH 以下の WebP）を作る。向きは EXIF を反映してから縮める（ブラウザの表示と同じ向き）。"""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes)))
    # 色の基準（ICC）は引き継ぐ（広色域の画像がくすまないように）。CMYK は変換で基準が合わなくなるので付けない
    icc = img.info.get("icc_profile") if img.mode != "CMYK" else None
    has_alpha = "A" in img.getbands() or "transparency" in img.info
    img = img.convert("RGBA" if has_alpha else "RGB")
    if img.width > _THUMB_MAX_WIDTH:
        h = max(1, round(img.height * _THUMB_MAX_WIDTH / img.width))
        img = img.resize((_THUMB_MAX_WIDTH, h), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=_THUMB_QUALITY, method=6, **({"icc_profile": icc} if icc else {}))
    return buf.getvalue()


def store_thumbnail(sb: Client, card_id: int, image_bytes: bytes) -> str | None:
    """
    表示中の画像（切り抜き後なら切り抜き後のバイト）から縮小版を作って Storage に保存し、中身のハッシュ付きの公開URLを返す。
    ?v= は中身から作る（時刻の秒では、取り込み直後の自動切り抜きで切り抜き前と同じ URL になりうるため）。
    失敗したら警告ログを出して None を返す（呼び出し側は thumb_url を None にし、表示は原寸の画像に戻る）。
    """
    path = thumb_path(card_id)
    try:
        data = make_thumbnail(image_bytes)
        sb.storage.from_(_STORAGE_BUCKET).upload(
            path=path,
            file=data,
            file_options={"content-type": "image/webp", "upsert": "true"},
        )
        r = sb.storage.from_(_STORAGE_BUCKET).get_public_url(path)
        base = (r if isinstance(r, str) else r.get("publicUrl", "")).split("?")[0]
    except Exception as e:
        logger.warning(f"[image_store] 縮小版の保存失敗（原寸の画像で表示を続ける）: card_id={card_id}: {e}")
        return None
    if not base:
        logger.warning(f"[image_store] 縮小版の公開URLが不明（原寸の画像で表示を続ける）: card_id={card_id}")
        return None
    return f"{base}?v={hashlib.sha1(data).hexdigest()[:12]}"


def _backup_original(sb: Client, card_id: int, image_bytes: bytes, mime: str) -> dict | None:
    """
    元画像を unreleased/originals/{card_id}.{ext} に退避し、記録用の dict を返す。
    失敗しても例外は出さず None を返す（警告ログのみ）。呼び出し側で extraction_raw に記録する。
    """
    ext = _EXT_MAP.get(mime, "jpg")
    path = f"unreleased/originals/{card_id}.{ext}"
    try:
        sb.storage.from_(_STORAGE_BUCKET).upload(
            path=path,
            file=image_bytes,
            file_options={"content-type": mime, "upsert": "true"},
        )
    except Exception as e:
        logger.warning(f"[image_store] 元画像の退避失敗（無視）: card_id={card_id}: {e}")
        return None
    return {"storage_path": path, "mime": mime}


_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 取り込みと同じ上限


def _fetch_validated_image(url: str) -> tuple[bytes | None, str, str]:
    """
    URL から画像を取得して検証する。Content-Type が image/* であること、5MB 以下であること、
    PIL で開けて verify() が通ることを確認する。検証前のバイトは呼び出し側に渡さない。
    Returns: (bytes or None, mime, エラー文言)
    """
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        return None, "image/jpeg", f"画像取得失敗: {e}"
    mime = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if not mime.startswith("image/"):
        return None, "image/jpeg", f"Content-Type が image/* でない: {mime!r}"
    data = resp.content
    if len(data) > _MAX_IMAGE_BYTES:
        return None, "image/jpeg", f"画像サイズ超過: {len(data):,}バイト（上限5MB）"
    try:
        Image.open(io.BytesIO(data)).verify()
    except Exception as e:
        return None, "image/jpeg", f"画像として不正: {e}"
    return data, mime, ""


def _load_original_bytes(
    sb: Client, card_id: int, raw: dict, source_url: str, public_url: str, allow_public: bool,
) -> tuple[bytes | None, str, str, str]:
    """
    元画像バイトを優先順で取得する。
    ①退避済み(Storage) → ②source_image_url → ③public_url（allow_public のときのみ）
    ③の public_url はクロップ済みの可能性があり、二重クロップになるため、
    呼び出し側が「Storage がまだ未クロップ」と判断できるときだけ allow_public=True にする。
    allow_public=False のとき、source_url の取得失敗では ③ に落とさずエラーを返す。
    Returns: (bytes or None, mime, 取得元 "originals"|"source"|"public", エラー文言)
    """
    orig = raw.get("original_image") or {}
    path = orig.get("storage_path") if isinstance(orig, dict) else None
    if path:
        try:
            data = sb.storage.from_(_STORAGE_BUCKET).download(path)
            if data:
                mime = (orig.get("mime") or "image/jpeg")
                return bytes(data), mime, "originals", ""
        except Exception as e:
            logger.warning(f"[image_store] 退避元画像の取得失敗（source_image_url へ）: card_id={card_id}: {e}")

    urls = [("source", source_url)]
    if allow_public:
        urls.append(("public", public_url))
    last_err = "画像URLが登録されていません"
    for kind, url in urls:
        if not url:
            continue
        data, mime, err = _fetch_validated_image(url)
        if data is None:
            last_err = err
            continue
        return data, mime, kind, ""
    return None, "image/jpeg", "", last_err


def _bust_url(sb: Client, storage_path: str, public_url: str) -> str:
    """キャッシュバスター付き公開URLを作る。公開URLが分からなければ空文字（壊れたURLは作らない）。"""
    base = (public_url or "").split("?")[0]
    if not base and storage_path:
        try:
            r = sb.storage.from_(_STORAGE_BUCKET).get_public_url(storage_path)
            base = (r if isinstance(r, str) else r.get("publicUrl", "")).split("?")[0]
        except Exception as e:
            logger.warning(f"[image_store] public URL 取得失敗: {e}")
    if not base:
        logger.warning(f"[image_store] public URL が不明のためキャッシュバスターURLを作れません: path={storage_path!r}")
        return ""
    return f"{base}?t={datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"


def collect_original_paths(sb: Client, card_ids: list[int]) -> list[str]:
    """
    カード削除時に Storage から消すべき退避元画像と縮小版のパスを集める。
    extraction_raw.original_image.storage_path があればそれ、無ければ
    unreleased/originals/{card_id}.{jpg,png,gif,webp} を候補にする（存在しなくても remove は無害）。
    行を消す（CASCADE を含む）前に呼ぶこと。
    """
    paths: list[str] = []
    for cid in card_ids:
        raw = _read_extraction_raw(sb, cid)
        orig = raw.get("original_image")
        path = orig.get("storage_path") if isinstance(orig, dict) else None
        if path:
            paths.append(path)
        else:
            paths.extend(f"unreleased/originals/{cid}.{ext}" for ext in ("jpg", "png", "gif", "webp"))
        paths.append(thumb_path(cid))  # 縮小版も公開バケットに残さない
    return paths


def _is_auto_crop_candidate(raw: dict, image_url: str) -> tuple[dict | None, str]:
    """crop_suggestion が自動適用の条件を満たすとき (箱 dict, "") を、満たさなければ (None, 理由) を返す。"""
    sug = raw.get("crop_suggestion")
    if not isinstance(sug, dict):
        return None, "crop_suggestion なし"
    if sug.get("source_image_url") != image_url:
        return None, "候補の source_image_url が今回の画像と不一致"
    box = {}
    for k in ("left", "top", "right", "bottom"):
        v = sug.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return None, f"候補の {k} が有限数でない"
        box[k] = float(v)
    if not (0 <= box["left"] < box["right"] <= 1 and 0 <= box["top"] < box["bottom"] <= 1):
        return None, "候補の座標が範囲外"
    # 画像全体（カード単体の画像）はクロップ不要
    if box["left"] == 0 and box["top"] == 0 and box["right"] == 1 and box["bottom"] == 1:
        return None, "候補が画像全体（クロップ不要）"
    return box, ""


def ingest_x_card_image(
    sb: Client,
    card_id: int,
    image_url: str,
    tweet_url: str,
) -> tuple[bool, str]:
    """
    X カード画像を取得して Storage に保存し、official_card_images に INSERT する。
    元画像は unreleased/originals/ にも退避する（失敗しても取り込みは続行）。
    抽出時のクロップ候補が条件を満たすときだけ自動クロップを適用する（失敗しても元画像のまま残す）。

    成功時は unreleased_cards.extraction_raw.card_image_url を
    Storage の public URL に更新する（管理画面プレビュー用）。

    Returns: (成功フラグ, 理由文字列)
    """
    # 既存の画像レコードがあればスキップ（二重取込防止）
    try:
        existing = (
            sb.table("official_card_images")
            .select("id")
            .eq("unreleased_card_id", card_id)
            .is_("deleted_at", "null")
            .execute()
        )
        if existing.data:
            logger.info(f"[image_store] 画像取込済みのためスキップ: card_id={card_id}")
            return True, "取込済み"
    except Exception as e:
        logger.warning(f"[image_store] 既存確認失敗（処理継続）: {e}")

    # 画像ダウンロード
    try:
        resp = requests.get(image_url, timeout=30)
        resp.raise_for_status()
        image_bytes = resp.content
    except Exception as e:
        return False, f"画像取得失敗: {e}"

    content_type = resp.headers.get("Content-Type", "image/jpeg")
    mime = content_type.split(";")[0].strip().lower()
    if not mime.startswith("image/"):
        return False, f"Content-Type が image/* でない: {mime!r}"

    MAX_SIZE = 5 * 1024 * 1024
    if len(image_bytes) > MAX_SIZE:
        return False, f"画像サイズ超過: {len(image_bytes):,}バイト（上限5MB）"

    return _store_card_image(sb, card_id, image_bytes, mime, image_url, tweet_url,
                             urlparse(image_url).netloc)


def ingest_uploaded_card_image(
    sb: Client,
    card_id: int,
    image_bytes: bytes,
    mime: str,
    image_key: str,
    source_page_url: str,
    source_domain: str,
) -> tuple[bool, str]:
    """
    管理画面からアップロードした画像（配信のキャプチャ等）を保存する。保存・退避・自動クロップは X と同じ。
    image_key は抽出時に画像を区別した識別子（crop_suggestion.source_image_url と一致させる）。
    呼び出し側で画像の検証（形式・サイズ）を済ませてから渡すこと。
    """
    try:
        existing = (
            sb.table("official_card_images")
            .select("id")
            .eq("unreleased_card_id", card_id)
            .is_("deleted_at", "null")
            .execute()
        )
        if existing.data:
            logger.info(f"[image_store] 画像取込済みのためスキップ: card_id={card_id}")
            return True, "取込済み"
    except Exception as e:
        logger.warning(f"[image_store] 既存確認失敗（処理継続）: {e}")
    return _store_card_image(sb, card_id, image_bytes, mime, image_key, source_page_url, source_domain)


def store_replacement_image(
    sb: Client,
    card_id: int,
    image_url: str,
    page_url: str,
    *,
    official_site: bool,
) -> tuple[bool, str]:
    """
    画像取込のカードを公式版に差し替えるときの保存。既存の画像行があっても保存する（古い行の論理削除は呼び出し側）。
    official_site=True（公式サイトの画像）は承認時と同じく fetch_guard の許可リストで取得する。
    ここでは切り抜かない。X の画像は、呼び出し側が古い行を消した後に apply_auto_crop で切り抜く。
    """
    if official_site:
        import fetch_guard
        if not fetch_guard.is_whitelisted(image_url):
            return False, f"ホワイトリスト外のURL: {image_url!r}"
        try:
            resp = fetch_guard.fetch_whitelisted(image_url)
        except Exception as e:
            return False, f"画像取得失敗: {e}"
    else:
        try:
            resp = requests.get(image_url, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            return False, f"画像取得失敗: {e}"
    if getattr(resp, "status_code", 200) != 200:
        return False, f"画像取得失敗: HTTP {resp.status_code}"
    mime = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if not mime.startswith("image/"):
        return False, f"Content-Type が image/* でない: {mime!r}"
    data = resp.content
    if len(data) > _MAX_IMAGE_BYTES:
        return False, f"画像サイズ超過: {len(data):,}バイト（上限5MB）"
    # 切り抜きはここではしない。古い画像行が見えている間に切り抜くと、古い行のファイルを相手にしてしまう
    # （呼び出し側が古い行を論理削除してから apply_auto_crop を呼ぶ）
    return _store_card_image(sb, card_id, data, mime, image_url, page_url, urlparse(image_url).netloc,
                             auto_crop=False)


def apply_auto_crop(sb: Client, card_id: int, image_url: str) -> None:
    """クロップが必要な候補（画像全体でない・URL一致・座標が正しい）なら自動で切り抜く。失敗しても元画像のまま続行。

    crop_and_save_image は見えている画像行を相手にするので、見えている行が1行のときに呼ぶこと
    （画像取込の差し替えでは、古い行を論理削除してから呼ぶ）。
    """
    # 元画像は退避済みで管理画面から戻せる。最新の extraction_raw を読み直す
    raw_now = _read_extraction_raw(sb, card_id)
    box, skip_reason = _is_auto_crop_candidate(raw_now, image_url)
    if not box:
        logger.info(f"[image_store] 自動クロップ見送り: card_id={card_id}: {skip_reason}")
        return
    sug = raw_now.get("crop_suggestion") or {}
    try:
        # この時点の Storage は未クロップなので、元画像取得失敗時に public_url を使ってよい
        ok, result = crop_and_save_image(
            sb, card_id, box["left"], box["top"], box["right"], box["bottom"], mode="auto",
            state_extra={"method": sug.get("method"), "refinement": sug.get("refinement")},
            allow_public_fallback=True,
        )
        if not ok:
            logger.warning(f"[image_store] 自動クロップ失敗（元画像のまま継続）: card_id={card_id}: {result}")
    except Exception as e:
        logger.warning(f"[image_store] 自動クロップ例外（元画像のまま継続）: card_id={card_id}: {e}")


def _store_card_image(
    sb: Client,
    card_id: int,
    image_bytes: bytes,
    mime: str,
    image_url: str,
    tweet_url: str,
    source_domain: str,
    *,
    auto_crop: bool = True,
) -> tuple[bool, str]:
    """取得済みの画像を Storage に保存し、来歴を登録して、元画像の退避と自動クロップを行う。

    auto_crop=False は差し替え用（古い画像行を消した後に呼び出し側が apply_auto_crop を呼ぶ）。
    """
    ext = _EXT_MAP.get(mime, "jpg")
    storage_path = f"unreleased/{card_id}.{ext}"

    # Storage アップロード
    try:
        sb.storage.from_(_STORAGE_BUCKET).upload(
            path=storage_path,
            file=image_bytes,
            file_options={"content-type": mime, "upsert": "true"},
        )
    except Exception as e:
        return False, f"Storage アップロード失敗: {e}"

    # public URL 取得
    try:
        url_resp = sb.storage.from_(_STORAGE_BUCKET).get_public_url(storage_path)
        public_url = url_resp if isinstance(url_resp, str) else url_resp.get("publicUrl", "")
    except Exception as e:
        logger.warning(f"[image_store] public URL 取得失敗: {e}")
        public_url = ""

    thumb_url = store_thumbnail(sb, card_id, image_bytes)

    # official_card_images INSERT
    try:
        sb.table("official_card_images").insert({
            "unreleased_card_id": card_id,
            "storage_path": storage_path,
            "public_url": public_url,
            "thumb_url": thumb_url,
            "source_image_url": image_url,
            "source_page_url": tweet_url,
            "source_domain": source_domain,
        }).execute()
    except Exception as e:
        return False, f"DB 登録失敗: {e}"

    # 元画像の退避（失敗しても取り込みは続行）
    updates: dict = {}
    original = _backup_original(sb, card_id, image_bytes, mime)
    if original:
        updates["original_image"] = original
    # extraction_raw.card_image_url を Storage URL に更新（管理画面プレビュー用）
    if public_url:
        updates["card_image_url"] = public_url
    if updates:
        _merge_extraction_raw(sb, card_id, updates)

    if auto_crop:
        apply_auto_crop(sb, card_id, image_url)
    else:
        logger.info(f"[image_store] 自動クロップ見送り: card_id={card_id}: 保存時には切り抜かない")

    logger.info(f"[image_store] 画像保存完了: card_id={card_id}, path={storage_path!r}")
    return True, f"取込成功: {storage_path}"


def crop_and_save_image(
    sb: Client,
    card_id: int,
    left: float,
    top: float,
    right: float,
    bottom: float,
    mode: str = "manual",
    state_extra: dict | None = None,
    allow_public_fallback: bool = False,
) -> tuple[bool, str]:
    """
    オリジナル画像を指定割合でクロップして Storage を上書きする。
    元画像の取得は ①退避済み(originals) → ②source_image_url → ③public_url の順。
    常にオリジナルから切り出すため、何度でもやり直せる。
    mode は crop_state.mode に記録する（"auto" | "manual"）。state_extra は crop_state に追記する情報（method 等）。
    public_url からの取得は二重クロップになり得るため、Storage が未クロップと分かる呼び出し
    （取り込み直後の自動クロップ）か、source_url が無く未クロップ（crop_state なし/original）の場合に限る。
    left/top/right/bottom は画像全体を 1.0 とした割合（0.0〜1.0）。
    """
    # オリジナル画像URLと storage_path を取得
    try:
        rec = (
            sb.table("official_card_images")
            .select("id, storage_path, public_url, source_image_url")
            .eq("unreleased_card_id", card_id)
            .is_("deleted_at", "null")
            .execute()
        )
        if not rec.data:
            return False, "画像レコードが見つかりません"
        row = rec.data[0]
        storage_path = row.get("storage_path", "")
        public_url = row.get("public_url", "")
        source_url = row.get("source_image_url", "")
    except Exception as e:
        return False, f"DB参照失敗: {e}"

    # オリジナル画像を取得（Storage の上書き済み画像ではなく元画像から切り出す）
    raw_state = _read_extraction_raw(sb, card_id)
    cs = raw_state.get("crop_state")
    prev_mode = cs.get("mode") if isinstance(cs, dict) else None
    allow_public = allow_public_fallback or (not source_url and prev_mode in (None, "original"))
    raw_bytes, mime, origin, err = _load_original_bytes(
        sb, card_id, raw_state, source_url, public_url, allow_public=allow_public,
    )
    if raw_bytes is None:
        return False, err

    # クロップ
    try:
        # ブラウザと認識入力に合わせ、EXIF回転後の座標で保存する。
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw_bytes)))
        w, h = img.size
        # 比率への変換・JSON往復による誤差で455pxが454pxへ切り捨てられないようにする。
        x1 = round(w * left)
        y1 = round(h * top)
        x2 = round(w * right)
        y2 = round(h * bottom)
        cropped = img.crop((x1, y1, x2, y2))
        ext = mime.split("/")[-1].upper()
        # 出力形式と content-type を一致させる（GIF は PNG バイトで保存し content-type も image/png）
        pil_fmt, out_mime = {
            "JPEG": ("JPEG", "image/jpeg"), "JPG": ("JPEG", "image/jpeg"),
            "PNG": ("PNG", "image/png"), "WEBP": ("WEBP", "image/webp"),
            "GIF": ("PNG", "image/png"),
        }.get(ext, ("JPEG", "image/jpeg"))
        if pil_fmt == "JPEG" and cropped.mode not in ("RGB", "L"):
            cropped = cropped.convert("RGB")
        buf = io.BytesIO()
        cropped.save(buf, format=pil_fmt)
        image_bytes = buf.getvalue()
    except Exception as e:
        return False, f"クロップ失敗: {e}"

    # Storage に上書き保存
    try:
        sb.storage.from_(_STORAGE_BUCKET).upload(
            path=storage_path,
            file=image_bytes,
            file_options={"content-type": out_mime, "upsert": "true"},
        )
    except Exception as e:
        return False, f"Storage保存失敗: {e}"

    # キャッシュバスター付き URL を生成（Supabase CDN が古い画像をキャッシュしているため）
    cache_busted_url = _bust_url(sb, storage_path, public_url)
    # 縮小版も切り抜き後の画像で作り直す。失敗したら None にして、切り抜き前の縮小版を出さない
    thumb_url = store_thumbnail(sb, card_id, image_bytes)

    # official_card_images.public_url・thumb_url を更新（公開中カードリストが参照するURL）
    row_updates: dict = {"thumb_url": thumb_url}
    if cache_busted_url:
        row_updates["public_url"] = cache_busted_url
    try:
        sb.table("official_card_images").update(row_updates).eq("unreleased_card_id", card_id).is_("deleted_at", "null").execute()
    except Exception as e:
        logger.warning(f"[image_store] official_card_images の URL 更新失敗（無視）: {e}")

    # extraction_raw を更新（管理画面プレビュー用URL・クロップ状態・後からの退避）
    crop_state = {
        "mode": mode,
        "box": {"left": left, "top": top, "right": right, "bottom": bottom},
        "at": _now_iso(),
    }
    if state_extra:
        crop_state.update({k: v for k, v in state_extra.items() if v is not None})
    updates = {"crop_state": crop_state}
    if cache_busted_url:
        updates["card_image_url"] = cache_busted_url
    if origin == "source":
        # 退避が無い既存カード: 取得できた元画像をその場で退避する
        original = _backup_original(sb, card_id, raw_bytes, mime)
        if original:
            updates["original_image"] = original
    _merge_extraction_raw(sb, card_id, updates)

    logger.info(f"[image_store] クロップ保存完了: card_id={card_id}, path={storage_path!r}")
    return True, cache_busted_url


def restore_original_image(sb: Client, card_id: int) -> tuple[bool, str]:
    """
    クロップ済み画像を元画像に戻す。元画像バイトは再エンコードせずそのまま storage_path に上書きする。
    退避元（originals）が無ければ source_image_url から取得し、その場で退避する。
    Returns: (成功フラグ, 新URL or 失敗理由)
    """
    try:
        rec = (
            sb.table("official_card_images")
            .select("id, storage_path, public_url, source_image_url")
            .eq("unreleased_card_id", card_id)
            .is_("deleted_at", "null")
            .execute()
        )
        if not rec.data:
            return False, "画像レコードが見つかりません"
        row = rec.data[0]
        storage_path = row.get("storage_path", "")
        public_url = row.get("public_url", "")
        source_url = row.get("source_image_url", "")
    except Exception as e:
        return False, f"DB参照失敗: {e}"

    raw_state = _read_extraction_raw(sb, card_id)
    # 復元元にクロップ済みの public_url は使わない（元画像ではないため）
    image_bytes, mime, origin, err = _load_original_bytes(
        sb, card_id, raw_state, source_url, public_url, allow_public=False,
    )
    if image_bytes is None:
        return False, err

    try:
        sb.storage.from_(_STORAGE_BUCKET).upload(
            path=storage_path,
            file=image_bytes,
            file_options={"content-type": mime, "upsert": "true"},
        )
    except Exception as e:
        return False, f"Storage保存失敗: {e}"

    new_url = _bust_url(sb, storage_path, public_url)
    thumb_url = store_thumbnail(sb, card_id, image_bytes)
    row_updates: dict = {"thumb_url": thumb_url}
    if new_url:
        row_updates["public_url"] = new_url
    try:
        sb.table("official_card_images").update(row_updates).eq("unreleased_card_id", card_id).is_("deleted_at", "null").execute()
    except Exception as e:
        logger.warning(f"[image_store] official_card_images の URL 更新失敗（無視）: {e}")

    updates = {"crop_state": {"mode": "original", "box": None, "at": _now_iso()}}
    if new_url:
        updates["card_image_url"] = new_url
    if origin == "source":
        original = _backup_original(sb, card_id, image_bytes, mime)
        if original:
            updates["original_image"] = original
    _merge_extraction_raw(sb, card_id, updates)

    logger.info(f"[image_store] 元画像へ復元完了: card_id={card_id}, path={storage_path!r}")
    return True, new_url
