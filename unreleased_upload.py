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
  - 公開中の行: 自動では差し替えない（2026-10-30。自動クロップが外れて、公開中の画像が崩れたため）。
    公式版の画像を「候補」として表示に使わない場所に保存し、管理画面の札と Discord で知らせる。
    管理画面で今の画像と見比べ、切り抜き枠を確かめてから差し替える（apply_official_candidate）。
    差し替えるときは人が確認済みの内容は変えず、公式版との差分を記録して「要確認」の札で知らせる。
  - 差し替える前に、見えているキャプチャと切り抜く前の画像を退避し、「キャプチャに戻す」で戻せる（revert_to_capture）。
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
from unreleased_card_number import is_from_official_url

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
# 承認待ちの状態（公式版が届いたら内容ごと置き換える）。これ以外（公開中）は候補として保存するだけ
REVIEW_STATUSES = ("pending", "needs_review")
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
                          only_keys=None, product_name=None) -> dict:
    """1枚の画像から未発売カードを抽出して承認待ちに登録し、カードごとの画像を保存する。

    - 発売済み（ygoresources 既知）のカードは登録しない（X監視・公式サイト巡回と同じ扱い）。
    - 同名のカードが既にあれば新しく登録しない。ただしそれが画像取込の行で画像が無い（前回の保存失敗）なら、
      今回の画像で画像だけ付け直す。
    - 自動承認は呼ばない（判定記録には upload_source が付く）。
    - only_keys（fuzzy_key の集合）を渡すと、その名前（または近い名前。ocgtimes_reveal と同じ近さ）のカードだけを
      登録する（紹介場面の取込で、一覧にだけあったカードを一覧画面から補うとき）。
    - product_name を渡すと、収録商品をその値にして発売日を照合し直す（紹介場面の取込の --product に揃える）。
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
        from ocgtimes_reveal import near_name
        keep = [r for r in rows
                if any(near_name(fuzzy_key(r.get("name", "")), k) for k in only_keys)]
        skipped += [{"name": r.get("name", ""), "reason": "紹介場面から登録済み（一覧からは補わない）"}
                    for r in rows if r not in keep]
        rows = keep
    if product_name:
        from unreleased_quality import attach_release_date
        dates: dict = {}
        for r in rows:
            r["product_name"] = product_name
            # 抽出時に別の商品名で照合した日付を「記事の日付」として持ち越さない（候補は抽出時のものに戻す）
            ev = (r.get("extraction_raw") or {}).get("release_date_evidence")
            r["release_date"] = ev.get("candidate_date") if isinstance(ev, dict) else None
            attach_release_date(r, dates)
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
    # 却下した行: 同じ出どころ（同じ動画・ページ）で却下した名前は登録しない。別の出どころなら承認待ちに戻す
    from unreleased_quality import lacks_card_body
    from unreleased_rejected import bust_image_url, load_rejected, revive_rejected
    rejected = load_rejected(sb)
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
        source_url = row.get("source_url") or ""
        if rejected.blocks(name, source_url):
            skipped.append({"name": name, "reason": "同じ出どころで却下済み"})
            continue
        revive_target = rejected.revivable(row, source_url, active_keys=set(existing))
        if revive_target is not None and lacks_card_body(row):
            # 一度人が却下した名前を、カード本体の読めない画像（イラストのみ等）で戻さない
            skipped.append({"name": name, "reason": "却下済みの同名があり、カード本体が読めないため見送り"})
            continue
        annotate_row(row, active_names, text_context)
        if revive_target is not None:
            # 画像アップロードの行は自動承認しない（呼び出し側も maybe_auto_approve を呼ばない）ので、ここで戻すだけ
            revived = revive_rejected(sb, revive_target, row)
            rejected.take(name)
            if not revived:
                skipped.append({"name": name, "reason": "却下済みの同名を承認待ちに戻せませんでした"})
                continue
            new_rows = [revived]
        else:
            resp = sb.table("unreleased_cards").upsert(
                row, on_conflict="name,product_name", ignore_duplicates=True).execute()
            if not resp.data:
                skipped.append({"name": name, "reason": "同名のカードが登録済み"})
                continue
            new_rows = resp.data
        for card in new_rows:
            inserted.append(card)
            existing[fk] = card
            if active_names is not None:
                active_names.append(name)
            if card.get("id") and store(card["id"], row):
                bust_image_url(sb, card)  # 承認待ちに戻した行は同じ保存先に上書きするため
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
    戻り値: {"inserted", "skipped", "images", "extracted", "list_only", "list_fallback"（missing＝補えなかった名前）,
            "no_card_frames", "failed_frames"}
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
                                      store_image=store_image, known_keys=known_keys, only_keys=keys,
                                      product_name=product_name or None)
            reg["inserted"] += r["inserted"]
            reg["skipped"] += r["skipped"]
            reg["images"].update(r["images"])
            # 一覧の読み直しで見つからなかった名前（2回の読みで名前が大きく揺れた等）は黙って落とさず返す
            from ocgtimes_reveal import near_name
            handled = [fuzzy_key(c.get("name") or "") for c in r["inserted"]]
            handled += [fuzzy_key(x.get("name") or "") for x in r["skipped"]
                        if not str(x.get("reason", "")).startswith("紹介場面から登録済み")]
            missing = sorted(k for k in keys if not any(near_name(k, h) for h in handled))
            fallback.append({"t": f["t"], "names": sorted(keys), "inserted": [c.get("name") for c in r["inserted"]],
                             **({"missing": missing} if missing else {})})
        except Exception as e:  # noqa: BLE001 — 補いの失敗で紹介場面の登録結果を捨てない
            logger.warning(f"[upload] 一覧画面からの補いに失敗: t={f['t']}: {e}")
            fallback.append({"t": f["t"], "names": sorted(keys), "error": repr(e)[:300]})
    logger.info(f"[upload] 紹介場面の取込: 場面={len(reveal)} 一覧={len(lists)} 行={len(rows)} "
                f"登録={len(reg['inserted'])} 一覧のみ={[c['name'] for c in result['list_only']]}")
    return {**reg, "extracted": len(rows), "list_only": [c["name"] for c in result["list_only"]],
            "list_fallback": fallback, "no_card_frames": result.get("no_card_frames", []),
            "failed_frames": result.get("failed_frames", [])}


def _data_url(data: bytes) -> str:
    with Image.open(io.BytesIO(data)) as im:
        fmt = (im.format or "png").lower()
    mime = {"jpeg": "image/jpeg", "webp": "image/webp"}.get(fmt, "image/png")
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def _load_existing_by_key(sb) -> dict[str, dict]:
    """却下以外の行（fuzzy_key → 行）。却下した行は unreleased_rejected で別に扱う。"""
    resp = (sb.table("unreleased_cards").select("id, name, status, extraction_raw")
            .neq("status", "rejected").execute())
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

_CARD_COLUMNS = ("id, name, status, product_name, source_url, extraction_raw, card_number, release_date, "
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
                      *, store=None, notify=None, fetch=None) -> list[dict]:
    """抽出した行のうち差し替え待ちと同名のものは差し替えに回し、残り（新規登録してよい行）を返す。

    - 承認待ちの行は差し替える。公開中の行は公式版を候補として保存するだけ（save_official_candidate）。
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
        # 実行の最初に読んだ状態は古いことがある（抽出の間に承認されうる）。判断の直前に読み直す
        try:
            fresh_card = _load_card(sb, card["id"]) if card.get("id") is not None else None
        except Exception as e:  # noqa: BLE001 — 監視の本体を止めない。このカードだけ次回に回す
            logger.warning(f"[upload] 差し替え待ちのカードを読み直せないため見送り: card_id={card.get('id')}: {e}")
            continue
        if isinstance(fresh_card, dict) and fresh_card.get("id") == card.get("id"):
            card = fresh_card
            if not is_replaceable(card):
                # 実行中に却下・差し替え済みになった
                logger.info(f"[upload] もう差し替え待ちでないため見送り: card_id={card.get('id')} ({page_url})")
                queue.cards.pop(key, None)
                queue.done.add(key)
                continue
        if card.get("status") in REVIEW_STATUSES:
            ok, reason = replace_with_official(sb, card, row, page_url=page_url, store=store, notify=notify)
            logger.info(f"[upload] 公式版で差し替え: card_id={card.get('id')} ok={ok} {reason} ({page_url})")
        else:
            ok, reason = save_official_candidate(sb, card, row, page_url=page_url, fetch=fetch, notify=notify)
            logger.info(f"[upload] 公式版を候補として保存: card_id={card.get('id')} ok={ok} {reason} ({page_url})")
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
                          crop=None, backup=None, require_backup: bool = False) -> tuple[bool, str]:
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
    backup = backup or _default_backup_capture
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
        # 差し替える前に、見えているキャプチャと切り抜く前の画像を退避する（「キャプチャに戻す」で使う）
        backup_failed = False
        if not raw.get("capture_backup"):
            saved = backup(sb, cid, old, raw)
            if saved:
                raw["capture_backup"] = saved
            elif saved == {}:
                pass   # 見える画像が無い（失うものが無い）ので、退避なしで進める
            elif require_backup:
                # 人が操作して差し替えるときは、退避できなければ何も変えずに止める（上書きでキャプチャを失うため）
                return False, "今の画像を退避できないため、差し替えを止めました（もう一度試してください）"
            else:
                backup_failed = True
        # X の自動クロップは extraction_raw.crop_suggestion を見るので、保存より先に公式版の候補を入れる。
        # キャプチャ側の元画像・クロップ状態・表示用URLは引き継がない（保存処理が公式版の値を書く）
        raw_for_store = {k: v for k, v in raw.items() if k not in ("original_image", "crop_state", "card_image_url")}
        for k in ("crop_suggestion", "card_bbox", "source_image_url", "card_image_url", "card_image_urls"):
            if k in new_raw:
                raw_for_store[k] = new_raw[k]
        sb.table("unreleased_cards").update({"extraction_raw": raw_for_store}).eq("id", cid).execute()

        ok, reason = store(sb, cid, row, page_url)
        if not ok:
            _put_back_capture(sb, cid, old, raw.get("capture_backup"))
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
                  "previous_source_url": card.get("source_url"),
                  # キャプチャに戻したとき、公式版を候補に戻すのに使う
                  "official_row": compact_row(row),
                  "official_original": stored_raw.get("original_image")}
        updates = {"source_url": page_url, "source_domain": (urlparse(page_url).hostname or "").lower()}
        if card.get("status") in REVIEW_STATUSES:
            # 承認待ち: 内容も公式版にする。元の値は記録に残す
            _annotate_official(sb, row, name, raw.get("upload"))
            record["mode"] = "content_replaced"
            record["previous"] = {f: card.get(f) for f in REPLACE_FIELDS}
            merged = dict(row.get("extraction_raw") or {})
            for k in ("original_image", "crop_state", "card_image_url", "capture_backup"):
                if k in stored_raw:
                    merged[k] = stored_raw[k]
                else:
                    merged.pop(k, None)
            merged["upload"] = raw.get("upload")
            # 却下から戻した行の記録（以前却下した出どころ）は、もう一度却下したときの判定に要るので引き継ぐ
            from unreleased_rejected import REVIVED_KEY
            if REVIVED_KEY in raw:
                merged[REVIVED_KEY] = raw[REVIVED_KEY]
            merged["replaced_by_official"] = record
            updates.update({f: row.get(f) for f in REPLACE_FIELDS if f in row})
            if not card.get("release_date") and row.get("release_date"):
                # 発売日は変えない方針だが、未確定なら公式版で確かめた値を採る（根拠の記録と揃う）
                updates["release_date"] = row["release_date"]
            if row.get("card_number"):
                # 型番も公式版の値にする（キャプチャの型番は AI の読みで、空や読み違いがある）。元の値は記録に残す
                record["previous"]["card_number"] = card.get("card_number")
                updates["card_number"] = row["card_number"]
            updates["extraction_raw"] = merged
            message = f"[未発売] 画像取込のカードを公式版で置き換えました（承認待ち・内容も更新）: {name}"
        else:
            # 公開中: 人が確認した内容は変えない。違いだけ記録して知らせる
            diff = field_diff(card, row)
            if is_from_official_url(row) and row["card_number"] != card.get("card_number"):
                if card.get("card_number"):
                    # 人が入れた・確かめた値かもしれないので変えない。違いとして知らせる
                    diff["card_number"] = {"current": card.get("card_number"), "official": row["card_number"]}
                else:
                    # 空の型番は公式画像のURLの確かな値で埋める（内容の確認とは関係しない並び順の情報）
                    updates["card_number"] = row["card_number"]
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
        if backup_failed:
            message += "（⚠ 今の画像を退避できなかったため、キャプチャには戻せません）"
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


# ──────────────────────────────────────────────
# 公開中の行: 公式版を候補として保存し、人が見比べてから差し替える（2026-10-30）
# ──────────────────────────────────────────────

# 候補・差し替え記録に残す公式版の抽出結果の記録の欄（切り抜き枠・画像URL）
# card_number_source は型番の出どころ（公式画像のURL由来なら公開中の空の型番を埋める。unreleased_card_number.is_from_official_url）
_ROW_RAW_KEYS = ("source", "crop_suggestion", "card_bbox", "source_image_url", "card_image_url", "card_image_urls",
                 "card_number_source")


def compact_row(row: dict) -> dict:
    """公式版の抽出結果のうち、後で差し替えに使う分だけを抜き出す（記録に保存できる大きさにする）。"""
    out = {f: row.get(f) for f in REPLACE_FIELDS if f in row}
    for k in ("name", "release_date", "card_number"):
        if row.get(k) is not None:
            out[k] = row[k]
    raw = row.get("extraction_raw") or {}
    out["extraction_raw"] = {k: raw[k] for k in _ROW_RAW_KEYS if k in raw}
    return out


def _load_card(sb, card_id: int) -> dict | None:
    rows = (sb.table("unreleased_cards").select(_CARD_COLUMNS).eq("id", card_id).execute().data or [])
    return rows[0] if rows else None


def _read_raw(sb, card_id: int) -> dict:
    rows = (sb.table("unreleased_cards").select("extraction_raw").eq("id", card_id).execute().data or [{}])
    return dict(rows[0].get("extraction_raw") or {})


def _write_raw(sb, card_id: int, raw: dict, extra: dict | None = None) -> None:
    sb.table("unreleased_cards").update(dict(extra or {}, extraction_raw=raw)).eq("id", card_id).execute()


def _default_backup_capture(sb, card_id: int, old_rows: list[dict], raw: dict) -> dict | None:
    """見えているキャプチャと切り抜く前の画像を captures/ に写す。

    Returns: 退避の記録（dict）／退避するものが無い（{}。見える画像が無い）／読めない・写せない（None）。
    切り抜く前の画像が記録にあるのに読めないときも None（片方だけ残すと、戻したとき二重に切り抜かれる）。
    """
    from unreleased_image_store import CAPTURE_DIR, ext_for, get_file, mime_for_path, put_file
    shown = next((r.get("storage_path") for r in old_rows if r.get("storage_path")), "")
    if not shown:
        return {}
    data = get_file(sb, shown)
    if data is None:
        logger.warning(f"[upload] キャプチャを退避できません（キャプチャに戻せなくなる）: card_id={card_id} {shown!r}")
        return None
    try:
        mime = mime_for_path(shown)
        image_path = f"{CAPTURE_DIR}/{card_id}.{ext_for(mime)}"
        put_file(sb, image_path, data, mime)
        saved = {"at": _now_iso(), "image": {"storage_path": image_path, "mime": mime},
                 "crop_state": raw.get("crop_state"), "original": None}
        orig = raw.get("original_image")
        orig_path = orig.get("storage_path") if isinstance(orig, dict) else None
        orig_data = get_file(sb, orig_path)
        if orig_path and orig_data is None:
            logger.warning(f"[upload] 切り抜く前の画像を退避できません（キャプチャに戻せなくなる）: card_id={card_id} {orig_path!r}")
            return None
        if orig_data is not None:
            orig_mime = orig.get("mime") or mime_for_path(orig_path)
            path = f"{CAPTURE_DIR}/originals/{card_id}.{ext_for(orig_mime)}"
            put_file(sb, path, orig_data, orig_mime)
            saved["original"] = {"storage_path": path, "mime": orig_mime}
        return saved
    except Exception as e:  # noqa: BLE001 — 退避できなくても差し替えは止めない
        logger.warning(f"[upload] キャプチャの退避に失敗（キャプチャに戻せなくなる）: card_id={card_id}: {e}")
        return None


def _put_candidate(sb, card_id: int, data: bytes, mime: str, *, page_url: str, image_url: str,
                   row: dict, diff: dict, official_site: bool) -> dict:
    """公式版の画像を candidates/ に置き、候補の記録を返す。例外はそのまま投げる。"""
    from unreleased_image_store import CANDIDATE_DIR, ext_for, put_file
    path = f"{CANDIDATE_DIR}/{card_id}.{ext_for(mime)}"
    url = put_file(sb, path, data, mime)
    return {"at": _now_iso(), "page_url": page_url, "image_url": image_url, "official_site": official_site,
            "image": {"storage_path": path, "mime": mime, "public_url": url}, "row": row, "diff": diff}


def _candidate_path(cand) -> str:
    img = cand.get("image") if isinstance(cand, dict) else None
    return (img.get("storage_path") or "") if isinstance(img, dict) else ""


def save_official_candidate(sb, card: dict, row: dict, *, page_url: str, fetch=None,
                            notify=None) -> tuple[bool, str]:
    """公開中の行に届いた公式版を候補として保存する。表示中の画像・内容は変えない。例外は投げない。

    同じカードに公式版がまた届いたら（公式X → 公式サイトなど）、候補を新しい方に置き換える。
    """
    notify = notify or _default_notify
    if fetch is None:
        from unreleased_image_store import fetch_replacement_image as fetch
    cid = card.get("id")
    name = card.get("name", "?")
    image_url = _official_image_url(row)
    official_site = (row.get("extraction_raw") or {}).get("source") == "official_html"
    cur = card.get("extraction_raw") or {}
    # 同じ公式版（記事の読み直しなど）が届き直したときは、候補・見送りの記録を変えず知らせもしない
    for prev in (cur.get("official_candidate"), cur.get("official_candidate_dismissed")):
        if isinstance(prev, dict) and image_url and prev.get("image_url") == image_url:
            return True, "候補・見送り済みと同じ公式版"
    diff = {}
    try:
        if not image_url:
            data, mime, err = None, "", "公式版の画像URLがありません"
        else:
            data, mime, err = fetch(image_url, official_site=official_site)
        if data is None:
            raw = _read_raw(sb, cid)
            raw["replace_error"] = {"at": _now_iso(), "page_url": page_url, "image_url": image_url, "reason": err}
            _write_raw(sb, cid, raw)
            notify(f"[未発売] 画像取込のカードに公式版が届きましたが、画像を取得できませんでした（表示は今のまま）: "
                   f"{name}（{err}） 公式版: {page_url}")
            return False, f"画像取得失敗: {err}"
        diff = field_diff(card, row)
        cand = _put_candidate(sb, cid, data, mime, page_url=page_url, image_url=image_url,
                              row=compact_row(row), diff=diff, official_site=official_site)
        # 書く直前に最新の記録を読み直して、候補の欄だけを差し替える
        raw = _read_raw(sb, cid)
        old_path = _candidate_path(raw.get("official_candidate"))
        raw.pop("replace_error", None)
        raw.pop("official_candidate_dismissed", None)
        raw["official_candidate"] = cand
        _write_raw(sb, cid, raw)
        if old_path and old_path != cand["image"]["storage_path"]:
            from unreleased_image_store import remove_files
            remove_files(sb, [old_path])
    except Exception as e:  # noqa: BLE001 — 監視の本体を止めない
        logger.warning(f"[upload] 公式版の候補を保存できません: card_id={cid}: {e}")
        notify(f"[未発売] 画像取込のカードに公式版が届きましたが、候補として保存できませんでした（表示は今のまま）: {name}（{e}）")
        return False, f"エラー: {e}"
    extra = f"。内容にも違いがあります（{'・'.join(diff)}）" if diff else ""
    notify(f"[未発売] 画像取込のカードに公式版が届きました。表示は今の画像のままです。"
           f"管理画面の公開中一覧で見比べて差し替えてください: {name}{extra}")
    return True, "candidate_saved"


class CandidateChanged(Exception):
    """見比べていた候補が、別の公式版に置き換わっていた。"""


def apply_official_candidate(sb, card_id: int, box: dict | None = None, *, expected_at: str | None = None,
                             notify=None) -> tuple[bool, str]:
    """候補の公式版で画像を差し替える。box（画像全体を 1.0 とした割合）があればその枠で切り抜く。

    expected_at は見比べた候補の at。今の候補と違えば CandidateChanged を投げる（別の画像に枠を当てない）。
    Returns: (成功フラグ, 失敗理由 or 補足)。差し替えは済んで切り抜きだけ失敗したときは (True, 補足)。
    """
    from unreleased_image_store import crop_and_save_image, get_file, remove_files, store_replacement_bytes
    card = _load_card(sb, card_id)
    if not card:
        return False, "カードが見つかりません"
    cand = (card.get("extraction_raw") or {}).get("official_candidate")
    if not isinstance(cand, dict):
        return False, "公式版の候補がありません"
    if expected_at is not None and cand.get("at") != expected_at:
        raise CandidateChanged("公式版の候補が新しいものに変わりました。見比べ直してください")
    img = cand.get("image") or {}
    data = get_file(sb, img.get("storage_path"))
    if data is None:
        return False, "公式版の候補の画像を読めません"
    mime = img.get("mime") or "image/jpeg"
    image_url = cand.get("image_url") or ""
    row = dict(cand.get("row") or {})
    row["extraction_raw"] = dict(row.get("extraction_raw") or {})

    def store(sb_, cid, _row, page_url):
        return store_replacement_bytes(sb_, cid, data, mime, image_url, page_url)

    # 人が操作して差し替えるので Discord には流さない（ログだけ）
    ok, reason = replace_with_official(sb, card, row, page_url=cand.get("page_url") or "", store=store,
                                       crop=lambda *a: None, notify=notify or logger.info, require_backup=True)
    if not ok:
        return False, reason
    raw = _read_raw(sb, card_id)
    raw.pop("official_candidate", None)
    _write_raw(sb, card_id, raw)
    remove_files(sb, [img.get("storage_path")])
    if box:
        c_ok, c_res = crop_and_save_image(sb, card_id, box["left"], box["top"], box["right"], box["bottom"],
                                          mode="manual")
        if not c_ok:
            return True, f"差し替えましたが、切り抜きに失敗しました（{c_res}）。クロップ画面で切り直してください"
    return True, ""


def dismiss_official_candidate(sb, card_id: int) -> tuple[bool, str]:
    """候補を見送る（画像を消し、見送った記録を残す）。同じカードに公式版がまた届けば、また候補になる。"""
    from unreleased_image_store import remove_files
    card = _load_card(sb, card_id)
    if not card:
        return False, "カードが見つかりません"
    raw = dict(card.get("extraction_raw") or {})
    cand = raw.pop("official_candidate", None)
    if not isinstance(cand, dict):
        return False, "公式版の候補がありません"
    raw["official_candidate_dismissed"] = {"at": _now_iso(), "page_url": cand.get("page_url"),
                                           "image_url": cand.get("image_url")}
    _write_raw(sb, card_id, raw)
    remove_files(sb, [_candidate_path(cand)])
    return True, ""


def _put_back_capture(sb, card_id: int, old_rows: list[dict], saved) -> None:
    """保存が途中で失敗したとき、同じ保存先に上書きされたかもしれない表示中の画像をキャプチャに書き戻す。"""
    if not isinstance(saved, dict):
        return
    from unreleased_image_store import get_file, put_file, store_thumbnail
    img = saved.get("image") or {}
    data = get_file(sb, img.get("storage_path"))
    shown = next((r.get("storage_path") for r in old_rows if r.get("storage_path")), "")
    if data is None or not shown:
        logger.warning(f"[upload] 表示中の画像を書き戻せません（公式版が見えている場合あり）: card_id={card_id}")
        return
    try:
        put_file(sb, shown, data, img.get("mime") or "image/jpeg")
        store_thumbnail(sb, card_id, data)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[upload] 表示中の画像の書き戻しに失敗（公式版が見えている場合あり）: card_id={card_id}: {e}")


def revert_to_capture(sb, card_id: int) -> tuple[bool, str]:
    """公式版に差し替えたカード（公開中・画像だけ差し替えたもの）を、退避してあるキャプチャに戻す。

    順番: ①要るファイルを全部読む（1つでも読めなければ何も変えない）
          ②公式版を候補として置き、記録に確定させる（途中で失敗して押し直しても、公式版がキャプチャにすり替わらない）
          ③キャプチャを保存し、古い画像行を消し、記録を戻す ④使われなくなったファイルを消す（最後）
    """
    from unreleased_image_store import (ext_for, get_file, mime_for_path, put_file, remove_files,
                                        store_replacement_bytes)
    card = _load_card(sb, card_id)
    if not card:
        return False, "カードが見つかりません"
    raw = dict(card.get("extraction_raw") or {})
    rep, bk = raw.get("replaced_by_official"), raw.get("capture_backup")
    if not isinstance(rep, dict) or not isinstance(bk, dict):
        return False, "戻せるキャプチャがありません"
    if rep.get("mode") != "image_only":
        return False, "内容ごと公式版にしたカードは、画像だけキャプチャに戻せません"

    # ① 読む
    img = bk.get("image") or {}
    data = get_file(sb, img.get("storage_path"))
    if data is None:
        return False, "退避したキャプチャを読めません（何も変えていません）"
    mime = img.get("mime") or mime_for_path(img.get("storage_path"))
    b_orig = bk.get("original") if isinstance(bk.get("original"), dict) else None
    b_orig_data = None
    if b_orig:
        b_orig_data = get_file(sb, b_orig.get("storage_path"))
        if b_orig_data is None:
            return False, "退避した切り抜く前のキャプチャを読めません（何も変えていません）"
    cand = raw.get("official_candidate")
    # 前回の途中で公式版を候補に置き済みなら、置き直さない（その後 originals はキャプチャに書き換わっている）
    pending_cand = cand if isinstance(cand, dict) and cand.get("from_replaced_at") == rep.get("at") else None
    official_row = rep.get("official_row") if isinstance(rep.get("official_row"), dict) else None
    official_bytes, o_mime, official_path = None, "", None
    if pending_cand is None and official_row is not None:
        o_rec = rep.get("official_original")
        if not isinstance(o_rec, dict):
            o_rec = raw.get("original_image") if isinstance(raw.get("original_image"), dict) else {}
        official_path = o_rec.get("storage_path")
        official_bytes = get_file(sb, official_path)
        if official_bytes is None:
            return False, "公式版の切り抜く前の画像を読めません（何も変えていません）"
        o_mime = o_rec.get("mime") or mime_for_path(official_path)

    old = _visible_images(sb, card_id)
    try:
        # ② 公式版を候補に置いて確定させる
        if official_bytes is not None:
            pending_cand = _put_candidate(
                sb, card_id, official_bytes, o_mime, page_url=rep.get("page_url") or "",
                image_url=rep.get("image_url") or "", row=official_row, diff=field_diff(card, official_row),
                official_site=(official_row.get("extraction_raw") or {}).get("source") == "official_html")
            pending_cand["from_replaced_at"] = rep.get("at")
            cur = _read_raw(sb, card_id)
            cur["official_candidate"] = pending_cand
            _write_raw(sb, card_id, cur)

        # ③ キャプチャを保存して記録を戻す
        previous_url = rep.get("previous_source_url") or card.get("source_url") or ""
        ok, reason = store_replacement_bytes(sb, card_id, data, mime, previous_url, previous_url)
        if not ok:
            return False, f"キャプチャを保存できません: {reason}"
        new_rows = _visible_images(sb, card_id)
        old_ids = {r["id"] for r in old}
        if old_ids:
            sb.table("official_card_images").update({"deleted_at": _now_iso()}).in_("id", sorted(old_ids)).execute()
        new_paths = {r.get("storage_path") for r in new_rows if r["id"] not in old_ids}

        new_raw = _read_raw(sb, card_id) or raw
        if b_orig_data is not None:
            o_path = f"unreleased/originals/{card_id}.{ext_for(b_orig.get('mime') or 'image/jpeg')}"
            put_file(sb, o_path, b_orig_data, b_orig.get("mime") or "image/jpeg")
            new_raw["original_image"] = {"storage_path": o_path, "mime": b_orig.get("mime") or "image/jpeg"}
        if bk.get("crop_state") is not None:
            new_raw["crop_state"] = bk["crop_state"]
        else:
            new_raw.pop("crop_state", None)
        for k in ("replaced_by_official", "capture_backup", "replace_error"):
            new_raw.pop(k, None)
        new_raw["reverted_to_capture"] = {"at": _now_iso(),
                                          "official": {k: v for k, v in rep.items() if k != "official_row"}}
        if pending_cand:
            pending_cand = dict(pending_cand)
            pending_cand.pop("from_replaced_at", None)
            new_raw["official_candidate"] = pending_cand
        updates = {}
        if rep.get("previous_source_url"):
            updates = {"source_url": previous_url, "source_domain": (urlparse(previous_url).hostname or "").lower()}
        _write_raw(sb, card_id, new_raw, updates)
        _bust_new_image_url(sb, card_id)
    except Exception as e:  # noqa: BLE001 — 退避は消していないので、押し直せば続きから戻せる
        logger.warning(f"[upload] キャプチャに戻す途中でエラー: card_id={card_id}: {e}")
        return False, f"エラー: {e}（もう一度押すと続きから戻します）"

    # ④ 使われなくなったファイル（拡張子が違う公式版の画像・切り抜く前の公式版・退避したキャプチャ）を消す
    now_orig = (new_raw.get("original_image") or {}).get("storage_path")
    keep = {now_orig, _candidate_path(new_raw.get("official_candidate"))} | new_paths
    stale = {r.get("storage_path") for r in old} | {official_path, img.get("storage_path"),
                                                     (b_orig or {}).get("storage_path")}
    remove_files(sb, stale - keep)
    return True, ""


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
                "page_url": rep.get("page_url"), "diff_fields": sorted(diff),
                "can_revert": (isinstance(raw.get("capture_backup"), dict) and rep.get("mode") == "image_only"
                               and isinstance(rep.get("official_original"), dict))}
    cand = raw.get("official_candidate")
    if isinstance(cand, dict):
        img = cand.get("image") or {}
        return {"state": "candidate", "at": cand.get("at"), "page_url": cand.get("page_url"),
                "image_url": img.get("public_url") or "", "crop_box": _candidate_box(cand),
                "diff_fields": sorted(cand.get("diff") or {})}
    err = raw.get("replace_error")
    return {"state": "waiting", "error": err.get("reason") if isinstance(err, dict) else None,
            "official_page": err.get("page_url") if isinstance(err, dict) else None}


def _candidate_box(cand: dict) -> dict | None:
    """候補の切り抜き案（抽出時の枠）。画像全体・不正な値なら None（切り抜かない）。"""
    sug = ((cand.get("row") or {}).get("extraction_raw") or {}).get("crop_suggestion")
    if not isinstance(sug, dict) or sug.get("source_image_url") != cand.get("image_url"):
        return None
    try:
        box = {k: float(sug[k]) for k in ("left", "top", "right", "bottom")}
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= box["left"] < box["right"] <= 1 and 0 <= box["top"] < box["bottom"] <= 1):
        return None
    if box == {"left": 0.0, "top": 0.0, "right": 1.0, "bottom": 1.0}:
        return None
    return box
