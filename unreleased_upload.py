"""
unreleased_upload.py — 画像アップロードからの未発売カード取込と、公式版が届いたときの差し替え

背景（2026-10-17）:
  公式YouTube「遊戯王OCGタイムズ」で初公開されたカードは、公式X（◤名◢ 書式）や公式サイトに
  すぐには載らないことがある。配信の「新規カード一覧」画面のキャプチャを管理画面から取り込めるようにする。

取込（import_uploaded_image）:
  - 1リクエスト1画像（5枚並びの画面で 17〜53 秒。gunicorn の timeout 120 秒に収める）。
  - 抽出は unreleased_extractor.extract_cards_from_uploaded_image（X画像と同じ設定）。
  - 行には extraction_raw.source = "image_upload" を付け、自動承認の対象から外す
    （unreleased_auto_approve.evaluate が upload_source を理由に不適格にする）。必ず人が承認する。
  - 画像はカードごとに自動クロップして保存する（unreleased_image_store.ingest_uploaded_card_image）。

差し替え（split_replaceable）:
  動画のキャプチャは公式画像より小さく、効果文も読み直し頼みになる。そこで、公式X監視・公式サイト巡回・
  管理画面のX取込で同名（fuzzy_key 一致）のカードが届いたら、新しい行を作らずに既存行の画像を公式版に差し替える。
  - 承認待ちの行: カードの内容（効果文・数値など）も公式版の抽出結果で置き換える（元の値は記録に残す）。
  - 公開中の行: 人が確認済みの内容は変えず、公式版との差分を記録して「要確認」の札と Discord で知らせる。
  - 公式版の画像を保存してから古い画像を論理削除する（途中で失敗しても画像が消えない）。
  - 画像の保存に失敗したら記録を戻し、差し替え済みの印を付けない。同名の公式版がまた届けば再試行されるが、
    公式Xは同じカードを普通1回しか投稿しないので、多くは画像取込のまま残る（管理画面の札と Discord で知らせる）。

Flask に依存しないため、Cron スクリプト（watch_x_unreleased / watch_unreleased）からも import できる。
"""

import base64
import binascii
import hashlib
import io
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

from PIL import Image

from name_normalize import fuzzy_key

logger = logging.getLogger(__name__)

UPLOAD_SOURCE = "image_upload"

# アップロードを受け付ける形式と上限（抽出・保存側の 5MB 上限に合わせる）
ALLOWED_MIME = {"image/png", "image/jpeg", "image/webp"}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
NOTE_MAX_LEN = 500
SOURCE_URL_MAX_LEN = 2000

# 公式版で置き換える内容の欄（名前・収録商品・発売日は照合と公開の軸なので変えない）
REPLACE_FIELDS = (
    "reading", "card_type", "attribute", "race", "level", "rank", "link_val",
    "atk", "def", "effect_text", "pendulum_scale", "pendulum_effect", "confidence",
)
# 公開中の行で差分を知らせる欄（confidence は内容ではないので比べない）
DIFF_FIELDS = tuple(f for f in REPLACE_FIELDS if f not in ("reading", "confidence"))


class UploadError(ValueError):
    """アップロード内容の不備（管理画面にそのまま見せてよい文言）。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────────────────────────────
# 取込
# ──────────────────────────────────────────────

def decode_upload(data_url: str) -> tuple[bytes, str]:
    """data URL（data:image/png;base64,...）を検証してバイト列と MIME を返す。不備は UploadError。"""
    if not isinstance(data_url, str) or not data_url.startswith("data:") or "," not in data_url:
        raise UploadError("画像データの形式が不正です")
    header, b64 = data_url.split(",", 1)
    mime = header[5:].split(";")[0].strip().lower()
    if ";base64" not in header:
        raise UploadError("画像データの形式が不正です（base64 ではありません）")
    if mime not in ALLOWED_MIME:
        raise UploadError(f"対応していない画像形式です: {mime or '不明'}（PNG・JPEG・WebP のみ）")
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise UploadError("画像データを読み取れません") from None
    if not data:
        raise UploadError("画像データが空です")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(f"画像が大きすぎます: {len(data):,}バイト（上限5MB）")
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = (im.format or "").lower()
            im.verify()
    except Exception:
        raise UploadError("画像として開けません") from None
    actual = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}.get(fmt)
    if actual != mime:
        raise UploadError("画像の中身と形式の表示が一致しません")
    return data, mime


def validate_source_url(source_url: str) -> str:
    """出典URL（必須・http/https）。公開時の情報元にもなるので、どこから取ったかを必ず残す。"""
    u = (source_url or "").strip()
    if not u:
        raise UploadError("出典URLは必須です（例: 配信のURL。時刻つきが望ましい）")
    if len(u) > SOURCE_URL_MAX_LEN:
        raise UploadError("出典URLが長すぎます")
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname or "." not in p.hostname:
        raise UploadError("出典URLは http(s) のURLにしてください")
    return u


def image_key_for(data: bytes) -> str:
    """抽出・クロップで画像を区別する識別子（同じ画像なら同じ値）。"""
    return f"upload:{hashlib.sha256(data).hexdigest()[:20]}"


def mark_upload_rows(rows: list[dict], source_url: str, note: str, filename: str) -> None:
    """抽出結果の行に、画像アップロード由来であることと出典を記録する。"""
    domain = (urlparse(source_url).hostname or "").lower()
    for row in rows:
        row["source_url"] = source_url
        row["source_domain"] = domain
        raw = row.setdefault("extraction_raw", {})
        raw["source"] = UPLOAD_SOURCE
        raw["upload"] = {"filename": (filename or "")[:200], "note": note, "at": _now_iso()}


def import_uploaded_image(sb, *, data_url: str, source_url: str, note: str = "",
                          filename: str = "", extract=None, store_image=None, known_keys=None,
                          only_keys=None) -> dict:
    """1枚の画像から未発売カードを抽出して承認待ちに登録し、カードごとの画像を保存する。

    - 発売済み（ygoresources 既知）のカードは登録しない（X監視・公式サイト巡回と同じ扱い）。
    - 同名のカードが既にあれば新しく登録しない。ただしそれが画像取込の行で画像が無い（前回の保存失敗）なら、
      今回の画像で画像だけ付け直す。
    - 自動承認は呼ばない（判定記録には upload_source が付く）。
    - only_keys（fuzzy_key の集合）を渡すと、その名前のカードだけを登録する（紹介場面の取込で、一覧にだけ
      あったカードを一覧画面から補うとき）。
    extract / store_image / known_keys はテスト用の差し替え口。
    戻り値: {"inserted": [行...], "skipped": [{"name","reason"}...], "images": {id: 結果}, "extracted": 件数}
    """
    data, _mime = decode_upload(data_url)
    source_url = validate_source_url(source_url)
    note = (note or "").strip()[:NOTE_MAX_LEN]
    key = image_key_for(data)

    from unreleased_extractor import normalize_uploaded_image
    try:
        image = normalize_uploaded_image(data, key)
    except Exception as e:
        raise UploadError(f"画像を処理できません: {e}") from None

    if extract is None:
        from unreleased_extractor import extract_cards_from_uploaded_image as extract

    rows = extract(image, source_url, note)
    mark_upload_rows(rows, source_url, note, filename)
    extracted = len(rows)
    skipped = []
    if only_keys is not None:
        keep = [r for r in rows if fuzzy_key(r.get("name", "")) in only_keys]
        skipped += [{"name": r.get("name", ""), "reason": "紹介場面から登録済み（一覧からは補わない）"}
                    for r in rows if r not in keep]
        rows = keep
    result = register_rows(sb, rows, {key: image["png"]}, store_image=store_image, known_keys=known_keys,
                           default_key=key)
    result["skipped"] = skipped + result["skipped"]
    result["extracted"] = extracted
    logger.info(f"[upload] 取込: 抽出={extracted}件 登録={len(result['inserted'])}件 "
                f"スキップ={result['skipped']} ({source_url})")
    return result


def register_rows(sb, rows: list[dict], pngs: dict[str, bytes], *, store_image=None, known_keys=None,
                  default_key=None) -> dict:
    """抽出済みの行（画像取込の印つき）を承認待ちに登録し、カードごとの画像を保存する。

    pngs は画像の識別子（行の extraction_raw.source_image_url）→ PNG バイト列。保存する画像は行ごとに選ぶ
    （紹介場面の取込では、カードごとに別の場面の画像を保存する）。行の識別子が pngs に無ければ default_key の画像。
    戻り値: {"inserted": [行...], "skipped": [{"name","reason"}...], "images": {id: 結果}}
    """
    if store_image is None:
        from unreleased_image_store import ingest_uploaded_card_image as store_image

    from unreleased_auto_approve import annotate_row, load_active_names, load_text_context
    active_names = load_active_names(sb)
    text_context = load_text_context(sb)
    existing = _load_existing_by_key(sb)
    known = _known_released_keys() if known_keys is None else known_keys

    inserted, skipped, images = [], [], {}

    def store(cid, row) -> bool:
        key = row["extraction_raw"].get("source_image_url")
        if key not in pngs:
            key = default_key
        png = pngs.get(key)
        if png is None:
            images[cid] = "失敗: 保存する画像がありません"
            return False
        source_url = row.get("source_url") or ""
        domain = (urlparse(source_url).hostname or "").lower()
        ok, reason = store_image(sb, cid, png, "image/png", key, source_url, domain)
        images[cid] = reason if ok else f"失敗: {reason}"
        logger.info(f"[upload] 画像保存: card_id={cid}, ok={ok}, reason={reason!r}")
        return ok

    for row in rows:
        name = row.get("name", "")
        fk = fuzzy_key(name)
        if fk and fk in known:
            skipped.append({"name": name, "reason": "発売済みのカード"})
            continue
        prev = existing.get(fk)
        if prev is not None:
            if _needs_image_again(sb, prev):
                # 前回の取込で画像の保存だけ失敗した行。今回の画像の枠候補を入れてから保存し直す
                _merge_raw(sb, prev["id"], {k: row["extraction_raw"][k] for k in
                                            ("crop_suggestion", "card_bbox", "source_image_url", "card_image_url")
                                            if k in row["extraction_raw"]})
                ok = store(prev["id"], row)
                skipped.append({"name": name, "reason": "登録済みのため画像だけ付け直し" if ok
                                else f"登録済み・画像の付け直しに失敗（{images[prev['id']]}）"})
            else:
                # 同名を二重に登録しない（公式版が先に入っていれば、それを正とする）
                skipped.append({"name": name, "reason": "同名のカードが登録済み"})
            continue
        annotate_row(row, active_names, text_context)
        resp = sb.table("unreleased_cards").upsert(
            row, on_conflict="name,product_name", ignore_duplicates=True).execute()
        if not resp.data:
            skipped.append({"name": name, "reason": "同名のカードが登録済み"})
            continue
        for card in resp.data:
            inserted.append(card)
            existing[fk] = card
            if active_names is not None:
                active_names.append(name)
            if card.get("id"):
                store(card["id"], row)
    return {"inserted": inserted, "skipped": skipped, "images": images}


def _with_t(url: str, t) -> str:
    """動画URLに再生位置（秒）を付ける。t が None ならそのまま。"""
    if t is None:
        return url
    return f"{url}{'&' if '?' in url else '?'}t={int(t)}s"


def import_reveal_frames(sb, *, reveal_images, list_images=(), video_url: str, note: str = "",
                         product_name: str = "", filename_prefix: str = "",
                         extract=None, store_image=None, known_keys=None, list_extract=None) -> dict:
    """OCGタイムズの紹介場面（と一覧画面）の画像から未発売カードを登録する（ocgtimes_reveal 参照）。

    reveal_images / list_images: [(画像のバイト列, 秒 or None), ...]。PNG・JPEG・WebP。
    - 紹介場面のカードを名前でまとめて1行ずつ登録する。画像はカードごとに選んだ場面を保存し、カードの枠で自動クロップする。
    - 一覧画面にあって紹介場面に無いカードは、その一覧画面を従来の読み方（extract_cards_from_uploaded_image）で読み、
      そのカードだけを登録する（list_fallback）。
    - 自動承認は呼ばない。突き合わせの結果は extraction_raw.reveal に記録する（auto_approve_candidate は記録だけ）。
    extract は ocgtimes_reveal.extract_reveal、list_extract は一覧画面の読み方の差し替え口（テスト用）。
    戻り値: {"inserted", "skipped", "images", "extracted", "list_only", "list_fallback", "no_card_frames"}
    """
    from unreleased_extractor import normalize_uploaded_image
    video_url = validate_source_url(video_url)
    note = (note or "").strip()[:NOTE_MAX_LEN]

    def prepare(items):
        out = []
        for data, t in items:
            if not data:
                raise UploadError(f"画像データが空です（{t}秒）")
            if len(data) > MAX_UPLOAD_BYTES:
                raise UploadError(f"画像が大きすぎます（{t}秒）: {len(data):,}バイト（上限5MB）")
            try:
                out.append({"image": normalize_uploaded_image(data, image_key_for(data)), "t": t, "data": data})
            except Exception as e:
                raise UploadError(f"画像を処理できません（{t}秒）: {e}") from None
        return out

    reveal = prepare(reveal_images)
    lists = prepare(list_images)
    if not reveal:
        raise UploadError("紹介場面の画像がありません")
    if extract is None:
        from ocgtimes_reveal import extract_reveal as extract
    result = extract(reveal, lists, video_url=video_url, product_name=product_name)
    rows = result["rows"]
    for row in rows:
        t = (row["extraction_raw"].get("reveal") or {}).get("saved_frame_t")
        mark_upload_rows([row], row["source_url"], note,
                         f"{filename_prefix}reveal" + (f"_t{t}" if t is not None else ""))
    pngs = {f["image"]["url"]: f["image"]["png"] for f in reveal}
    reg = register_rows(sb, rows, pngs, store_image=store_image, known_keys=known_keys)

    # 一覧にだけあったカードを一覧画面から補う（紹介場面が撮れていない・紹介されなかったカード）
    fallback = []
    missing_by_t: dict = {}
    for c in result["list_only"]:
        missing_by_t.setdefault(c.get("list_t"), set()).add(fuzzy_key(c["name"]))
    for f in lists:
        keys = missing_by_t.get(f["t"])
        if not keys:
            continue
        try:
            r = import_uploaded_image(sb, data_url=_data_url(f["data"]), source_url=_with_t(video_url, f["t"]),
                                      note=note, extract=list_extract,
                                      filename=f"{filename_prefix}list" + (f"_t{f['t']}" if f["t"] is not None else ""),
                                      store_image=store_image, known_keys=known_keys, only_keys=keys)
            reg["inserted"] += r["inserted"]
            reg["skipped"] += r["skipped"]
            reg["images"].update(r["images"])
            fallback.append({"t": f["t"], "names": sorted(keys), "inserted": [c.get("name") for c in r["inserted"]]})
        except Exception as e:  # noqa: BLE001 — 補いの失敗で紹介場面の登録結果を捨てない
            logger.warning(f"[upload] 一覧画面からの補いに失敗: t={f['t']}: {e}")
            fallback.append({"t": f["t"], "names": sorted(keys), "error": repr(e)[:300]})
    logger.info(f"[upload] 紹介場面の取込: 場面={len(reveal)} 一覧={len(lists)} 行={len(rows)} "
                f"登録={len(reg['inserted'])} 一覧のみ={[c['name'] for c in result['list_only']]}")
    return {**reg, "extracted": len(rows), "list_only": [c["name"] for c in result["list_only"]],
            "list_fallback": fallback, "no_card_frames": result.get("no_card_frames", [])}


def _data_url(data: bytes) -> str:
    with Image.open(io.BytesIO(data)) as im:
        fmt = (im.format or "png").lower()
    mime = {"jpeg": "image/jpeg", "webp": "image/webp"}.get(fmt, "image/png")
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def _load_existing_by_key(sb) -> dict[str, dict]:
    resp = sb.table("unreleased_cards").select("id, name, status, extraction_raw").execute()
    return {fuzzy_key(r["name"]): r for r in (resp.data or []) if r.get("name")}


def _known_released_keys() -> set[str]:
    """ygoresources の既知（発売済み）カード名。読めなければ空（除外しない。人が承認時に見る）。"""
    try:
        from ygores_repository import repository
        return {fuzzy_key(n) for n in (repository.get_name_index() or {})}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[upload] 既知カードの索引を読めません（除外しない）: {e}")
        return set()


def _needs_image_again(sb, card: dict) -> bool:
    """画像取込の行（却下済みを除く）で、見える画像が1枚も無いか。"""
    raw = card.get("extraction_raw") or {}
    if raw.get("source") != UPLOAD_SOURCE or card.get("status") == "rejected":
        return False
    rows = (sb.table("official_card_images").select("id")
            .eq("unreleased_card_id", card["id"]).is_("deleted_at", "null").execute().data or [])
    return not rows


def _merge_raw(sb, card_id: int, updates: dict) -> None:
    cur = (sb.table("unreleased_cards").select("extraction_raw").eq("id", card_id).execute().data or [{}])[0]
    raw = dict(cur.get("extraction_raw") or {})
    raw.update(updates)
    sb.table("unreleased_cards").update({"extraction_raw": raw}).eq("id", card_id).execute()


# ──────────────────────────────────────────────
# 公式版が届いたときの差し替え
# ──────────────────────────────────────────────

_CARD_COLUMNS = ("id, name, status, product_name, source_url, extraction_raw, "
                 + ", ".join(REPLACE_FIELDS))


def is_replaceable(card: dict) -> bool:
    """画像アップロード由来で、まだ公式版に差し替えていない行か（却下済みは対象外）。"""
    raw = card.get("extraction_raw") or {}
    return (raw.get("source") == UPLOAD_SOURCE and not raw.get("replaced_by_official")
            and card.get("status") != "rejected")


class ReplaceQueue:
    """差し替え待ちの行（fuzzy_key → 行）と、同じ実行内で差し替え済みの名前。"""

    def __init__(self, cards: dict[str, dict] | None = None):
        self.cards = dict(cards or {})
        self.done: set[str] = set()

    def keys(self) -> set[str]:
        return set(self.cards)

    def __bool__(self) -> bool:
        return bool(self.cards)

    def handles(self, name: str) -> bool:
        """この名前は差し替え待ち、または今回差し替え済みか（新しく登録してはいけない名前か）。"""
        key = fuzzy_key(name or "")
        return bool(key) and (key in self.cards or key in self.done)


def load_replaceable(sb) -> ReplaceQueue:
    """差し替え待ちの行を読む。読めなければ空（従来どおり同名はスキップされる）。"""
    try:
        resp = (sb.table("unreleased_cards").select(_CARD_COLUMNS)
                .eq("extraction_raw->>source", UPLOAD_SOURCE).execute())
    except Exception as e:  # noqa: BLE001 — 取込本体を止めない
        logger.warning(f"[upload] 差し替え待ちの行を読めません（今回は差し替えしない）: {e}")
        return ReplaceQueue()
    cards = {}
    for card in resp.data or []:
        if is_replaceable(card) and card.get("name"):
            cards[fuzzy_key(card["name"])] = card
    if cards:
        logger.info(f"[upload] 公式版への差し替え待ち: {len(cards)}件")
    return ReplaceQueue(cards)


def split_replaceable(sb, rows: list[dict], queue: ReplaceQueue, page_url: str,
                      *, store=None, notify=None) -> list[dict]:
    """抽出した行のうち差し替え待ちと同名のものは差し替えに回し、残り（新規登録してよい行）を返す。

    - カード本体（効果・数値）が読めていない行（イラストのみの投稿など）では差し替えない。新規登録もしない。
    - 同じ実行内で同名が2回来ても差し替えは1回だけ。差し替え済みの名前の2回目以降は捨てる
      （新しい行を作ると同名が2行になるため）。差し替えに失敗した行も新規登録はしない。
    """
    from unreleased_quality import lacks_card_body
    remaining = []
    for row in rows:
        name = row.get("name", "")
        if not queue.handles(name):
            remaining.append(row)
            continue
        key = fuzzy_key(name)
        card = queue.cards.get(key)
        if card is None:
            logger.info(f"[upload] 同名は差し替え済みのため捨てる: {name!r} ({page_url})")
            continue
        if lacks_card_body(row):
            logger.info(f"[upload] 画像取込カードの公式版だがカード本体が読めないため見送り: {name!r} ({page_url})")
            continue
        ok, reason = replace_with_official(sb, card, row, page_url=page_url, store=store, notify=notify)
        logger.info(f"[upload] 公式版で差し替え: card_id={card.get('id')} ok={ok} {reason} ({page_url})")
        if ok:
            queue.cards.pop(key, None)
            queue.done.add(key)
    return remaining


def _official_image_url(row: dict) -> str:
    raw = row.get("extraction_raw") or {}
    url = (raw.get("card_image_url") or "").strip()
    if not url:
        url = ((raw.get("card_image_urls") or [""])[0] or "").strip()
    return url


def _default_store(sb, card_id: int, row: dict, page_url: str) -> tuple[bool, str]:
    """公式版の画像を保存する（既存の画像行があっても保存する）。X は自動クロップつき、公式サイトは切り抜かない。"""
    url = _official_image_url(row)
    if not url:
        return False, "公式版の画像URLがありません"
    from unreleased_image_store import store_replacement_image
    official_site = (row.get("extraction_raw") or {}).get("source") == "official_html"
    return store_replacement_image(sb, card_id, url, page_url, official_site=official_site)


def _default_crop(sb, card_id: int, image_url: str) -> None:
    from unreleased_image_store import apply_auto_crop
    apply_auto_crop(sb, card_id, image_url)


def _default_notify(message: str) -> None:
    try:
        from discord_notify import send_discord_message
        send_discord_message(message, no_mentions=True)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[upload] Discord 通知失敗: {e}")


def _norm(v):
    if v is None:
        return ""
    return str(v).strip()


def field_diff(card: dict, row: dict) -> dict:
    """公開中の内容と公式版の抽出結果の違い（欄 → {current, official}）。"""
    diff = {}
    for f in DIFF_FIELDS:
        if _norm(card.get(f)) != _norm(row.get(f)):
            diff[f] = {"current": card.get(f), "official": row.get(f)}
    return diff


def _visible_images(sb, card_id: int) -> list[dict]:
    return (sb.table("official_card_images").select("id, storage_path")
            .eq("unreleased_card_id", card_id).is_("deleted_at", "null").execute().data or [])


def _annotate_official(sb, row: dict, own_name: str, upload) -> None:
    """公式版の行にも判定記録（抽出時スナップショット・効果文チェック）を付ける。自分自身は同名判定から外す。"""
    try:
        from unreleased_auto_approve import annotate_row, load_active_names, load_text_context
        # 画像取込から来た行であることを判定にも伝える（集計で自動承認の候補に混ざらないように）
        row.setdefault("extraction_raw", {})["upload"] = upload
        names = load_active_names(sb)
        if names is not None:
            own = fuzzy_key(own_name)
            names = [n for n in names if fuzzy_key(n) != own]
        annotate_row(row, names, load_text_context(sb))
    except Exception as e:  # noqa: BLE001 — 記録が欠けても差し替えは進める
        logger.warning(f"[upload] 公式版の判定記録を付けられません: {own_name!r}: {e}")


def replace_with_official(sb, card: dict, row: dict, *, page_url: str, store=None, notify=None,
                          crop=None) -> tuple[bool, str]:
    """差し替え待ちの行 card を、公式版の抽出結果 row で差し替える。例外は投げない。

    順番: ①公式版の枠候補を記録に入れる ②公式版の画像を保存（新しい画像行が増える）
          ③成功したら古い画像行だけ論理削除 ④内容と記録を更新。
    ③の後で X の画像だけ自動クロップする（見えている行が新しい1行になってから。古い行が見えている間に切り抜くと
    古い行のファイルを相手にしてしまう）。公式サイトの画像は承認時と同じく切り抜かない。
    ①〜②で失敗しても古い画像は見えたまま（記録は元に戻す）。③④で失敗しても新しい画像は見えている。
    """
    store = store or _default_store
    crop = crop or _default_crop
    notify = notify or _default_notify
    cid = card.get("id")
    name = card.get("name", "?")
    now = _now_iso()
    official_image = _official_image_url(row)
    raw = dict(card.get("extraction_raw") or {})
    new_raw = row.get("extraction_raw") or {}
    stored = False
    old = []
    try:
        old = _visible_images(sb, cid)
        # X の自動クロップは extraction_raw.crop_suggestion を見るので、保存より先に公式版の候補を入れる。
        # キャプチャ側の元画像・クロップ状態・表示用URLは引き継がない（保存処理が公式版の値を書く）
        raw_for_store = {k: v for k, v in raw.items() if k not in ("original_image", "crop_state", "card_image_url")}
        for k in ("crop_suggestion", "card_bbox", "source_image_url", "card_image_url", "card_image_urls"):
            if k in new_raw:
                raw_for_store[k] = new_raw[k]
        sb.table("unreleased_cards").update({"extraction_raw": raw_for_store}).eq("id", cid).execute()

        ok, reason = store(sb, cid, row, page_url)
        if not ok:
            raw["replace_error"] = {"at": now, "page_url": page_url, "image_url": official_image, "reason": reason}
            sb.table("unreleased_cards").update({"extraction_raw": raw}).eq("id", cid).execute()
            notify(f"[未発売] 画像取込のカードを公式版に差し替えられませんでした（画像取込のまま）: {name}（{reason}）"
                   f" 公式版: {page_url}")
            return False, f"画像保存失敗: {reason}"
        stored = True

        new_rows = _visible_images(sb, cid)
        old_ids = {r["id"] for r in old}
        if old_ids:
            sb.table("official_card_images").update({"deleted_at": now}).in_("id", sorted(old_ids)).execute()
        if new_raw.get("source") != "official_html" and official_image:
            crop(sb, cid, official_image)
        _bust_new_image_url(sb, cid)
        new_paths = {r.get("storage_path") for r in new_rows if r["id"] not in old_ids}

        # 保存処理が書いた記録（元画像の退避・クロップ状態・表示用URL）を読み直して上に重ねる
        fresh = (sb.table("unreleased_cards").select("extraction_raw").eq("id", cid).execute().data or [{}])[0]
        stored_raw = dict(fresh.get("extraction_raw") or raw_for_store)
        record = {"at": now, "page_url": page_url, "image_url": official_image,
                  "previous_source_url": card.get("source_url")}
        updates = {"source_url": page_url, "source_domain": (urlparse(page_url).hostname or "").lower()}
        if card.get("status") in ("pending", "needs_review"):
            # 承認待ち: 内容も公式版にする。元の値は記録に残す
            _annotate_official(sb, row, name, raw.get("upload"))
            record["mode"] = "content_replaced"
            record["previous"] = {f: card.get(f) for f in REPLACE_FIELDS}
            merged = dict(row.get("extraction_raw") or {})
            for k in ("original_image", "crop_state", "card_image_url"):
                if k in stored_raw:
                    merged[k] = stored_raw[k]
                else:
                    merged.pop(k, None)
            merged["upload"] = raw.get("upload")
            merged["replaced_by_official"] = record
            updates.update({f: row.get(f) for f in REPLACE_FIELDS if f in row})
            if not card.get("release_date") and row.get("release_date"):
                # 発売日は変えない方針だが、未確定なら公式版で確かめた値を採る（根拠の記録と揃う）
                updates["release_date"] = row["release_date"]
            updates["extraction_raw"] = merged
            message = f"[未発売] 画像取込のカードを公式版で置き換えました（承認待ち・内容も更新）: {name}"
        else:
            # 公開中: 人が確認した内容は変えない。違いだけ記録して知らせる
            diff = field_diff(card, row)
            record["mode"] = "image_only"
            record["diff"] = diff
            stored_raw.pop("replace_error", None)
            stored_raw["replaced_by_official"] = record
            updates["extraction_raw"] = stored_raw
            if diff:
                message = (f"[未発売] 画像取込のカードの画像を公式版に差し替えました。内容に違いがあります（要確認）: "
                           f"{name}（{'・'.join(diff)}）")
            else:
                message = f"[未発売] 画像取込のカードの画像を公式版に差し替えました（内容の違いなし）: {name}"
        sb.table("unreleased_cards").update(updates).eq("id", cid).execute()
        _remove_old_files(sb, [r.get("storage_path") for r in old], new_paths, raw, stored_raw)
        notify(message)
        return True, record["mode"]
    except Exception as e:  # noqa: BLE001 — 監視の本体を止めない
        logger.warning(f"[upload] 差し替え中にエラー: card_id={cid} 画像保存={'済' if stored else '前'}: {e}")
        if not stored:
            # 画像はまだ古いまま見えている。記録だけ元に戻す（失敗しても古い画像は見える）
            try:
                sb.table("unreleased_cards").update({"extraction_raw": raw}).eq("id", cid).execute()
            except Exception:  # noqa: BLE001
                pass
        notify(f"[未発売] 画像取込のカードを公式版に差し替える途中でエラー: {name}（{e}）"
               + ("。公式版の画像は保存済み、記録の更新が未完了" if stored else "。画像取込のまま"))
        return False, f"エラー: {e}"


def _remove_old_files(sb, old_paths, new_paths, old_raw: dict, new_raw: dict) -> None:
    """拡張子が変わって使われなくなったキャプチャの画像・元画像を Storage から消す（失敗しても続行）。"""
    paths = {p for p in old_paths if p and p not in new_paths}
    old_orig = old_raw.get("original_image")
    new_orig = new_raw.get("original_image")
    old_path = old_orig.get("storage_path") if isinstance(old_orig, dict) else None
    new_path = new_orig.get("storage_path") if isinstance(new_orig, dict) else None
    if old_path and old_path != new_path:
        paths.add(old_path)
    if not paths:
        return
    try:
        from unreleased_image_store import _STORAGE_BUCKET
        sb.storage.from_(_STORAGE_BUCKET).remove(sorted(paths))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[upload] 使われなくなった画像を消せません（残っても表示には影響なし）: {sorted(paths)}: {e}")


def _bust_new_image_url(sb, card_id: int) -> None:
    """同じ保存先に上書きすると公開URLが変わらず古い画像が見え続けるので、新しい行のURLに時刻を付ける。"""
    try:
        from unreleased_image_store import _bust_url
        rows = (sb.table("official_card_images").select("id, storage_path, public_url")
                .eq("unreleased_card_id", card_id).is_("deleted_at", "null").execute().data or [])
        for r in rows:
            path, url = r.get("storage_path") or "", r.get("public_url") or ""
            if path and path not in url.split("?")[0]:
                url = ""   # 保存先と食い違う公開URLは使わず、保存先から作り直す
            busted = _bust_url(sb, path, url)
            if busted:
                sb.table("official_card_images").update({"public_url": busted}).eq("id", r["id"]).execute()
    except Exception as e:  # noqa: BLE001 — 表示が古いままになるだけなので続行
        logger.warning(f"[upload] 差し替え画像のURL更新に失敗（古い画像が残って見える場合あり）: {e}")


def admin_view(raw: dict, card: dict | None = None) -> dict | None:
    """管理画面の札用。画像取込でなければ None。

    承認待ちで内容ごと公式版にした行は source が公式側に変わるので、upload の記録の有無でも判定する。
    公開中の差分は、card（現在の値）を渡すと「今も公式版と違う欄」だけに絞る（人が直せば札が消える）。
    """
    if not isinstance(raw, dict):
        return None
    rep = raw.get("replaced_by_official")
    if raw.get("source") != UPLOAD_SOURCE and not (isinstance(rep, dict) and raw.get("upload")):
        return None
    if isinstance(rep, dict):
        diff = rep.get("diff") or {}
        if card is not None:
            diff = {f: v for f, v in diff.items()
                    if isinstance(v, dict) and _norm(card.get(f)) != _norm(v.get("official"))}
        return {"state": "replaced", "mode": rep.get("mode"), "at": rep.get("at"),
                "page_url": rep.get("page_url"), "diff_fields": sorted(diff)}
    err = raw.get("replace_error")
    return {"state": "waiting", "error": err.get("reason") if isinstance(err, dict) else None,
            "official_page": err.get("page_url") if isinstance(err, dict) else None}
