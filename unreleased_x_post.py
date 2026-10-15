"""承認された未発売カードを X に自動投稿する（2026-10-15 追加）。

承認＝公開。承認され、公開画像が揃ったカードを1枚ずつ投稿する。
  - 呼び出し元: admin_unreleased（単体/一括承認の画像取込後）と tools/post_unreleased_x.py（回収）
  - 二重投稿防止: テーブル unreleased_x_posts（card_id が主キー）に投稿前に「予約」行を入れる。
    主キー衝突＝他が処理中/済みなので何もしない。例外は「failed かつ attempts<3」の再試行だけ
    （条件付き UPDATE で1つの呼び出しだけが奪える）。
  - 既定は無効。環境変数 UNRELEASED_X_POST_ENABLED=1 のときだけ動く。
  - X_POST_DRY_RUN=1 のときは投稿せず本文をログに出すだけ（予約行も作らない）。

Flask に依存しない。Supabase クライアントは引数で受け取る。
"""

import logging
import os
import re
import tempfile
import threading
import time
import urllib.parse
from datetime import date, datetime, timezone

from constants import JST

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 定数
# ──────────────────────────────────────────────

ENABLED_ENV = "UNRELEASED_X_POST_ENABLED"
TABLE = "unreleased_x_posts"

# 失敗時に再試行する上限（この回数失敗したら failed のまま諦めて Discord 通知のみ）
# TODO: calibrate from data
MAX_ATTEMPTS = 3

# 連続投稿の間隔（秒）。一括承認・回収でタイムラインを埋め尽くさないため。
# TODO: calibrate from data
POST_INTERVAL_SEC = 20

# 回収1回あたりの投稿上限（有効化直後などの取りこぼし大量発生時の暴走防止）
# TODO: calibrate from data
MAX_BACKFILL_PER_RUN = 10

# X の加重文字数上限。日本語など多くの文字は2、URLは23で数える
TWEET_WEIGHT_LIMIT = 280
URL_WEIGHT = 23

# 文字数超過時にこれより短くは詰めない（加重）
_MIN_PRODUCT_WEIGHT = 12
_MIN_NAME_WEIGHT = 10

_URL_RE = re.compile(r"https?://\S+")
_ELLIPSIS = "…"
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_EXT_BY_MIME = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
                "image/gif": ".gif", "image/webp": ".webp"}


def is_enabled() -> bool:
    """自動投稿が有効か（既定は無効）。"""
    return os.environ.get(ENABLED_ENV, "") == "1"


def _is_dry_run() -> bool:
    return os.environ.get("X_POST_DRY_RUN", "") == "1"


# ──────────────────────────────────────────────
# 本文
# ──────────────────────────────────────────────

def _char_weight(ch: str) -> int:
    """X の加重文字数での1文字分（ラテン等は1、日本語など CJK は2）。"""
    c = ord(ch)
    if c <= 4351 or 8192 <= c <= 8205 or 8208 <= c <= 8223 or 8242 <= c <= 8247:
        return 1
    return 2


def weighted_length(text: str) -> int:
    """X の加重文字数。URL は長さに関わらず 23 で数える。"""
    total = 0
    pos = 0
    for m in _URL_RE.finditer(text):
        total += sum(_char_weight(ch) for ch in text[pos:m.start()]) + URL_WEIGHT
        pos = m.end()
    total += sum(_char_weight(ch) for ch in text[pos:])
    return total


def truncate_to_weight(s: str, max_weight: int) -> str:
    """加重文字数が max_weight 以下になるよう末尾を省略記号で詰める。"""
    if weighted_length(s) <= max_weight:
        return s
    budget = max_weight - _char_weight(_ELLIPSIS)
    out = []
    used = 0
    for ch in s:
        w = _char_weight(ch)
        if used + w > budget:
            break
        out.append(ch)
        used += w
    return "".join(out) + _ELLIPSIS


def _format_release_date(release_date) -> str | None:
    """'YYYY-MM-DD' を 'YYYY年M月D日' に。None・不正値は None。"""
    if not release_date:
        return None
    try:
        d = date.fromisoformat(str(release_date)[:10])
    except ValueError:
        return None
    return f"{d.year}年{d.month}月{d.day}日"


def _site_url() -> str:
    from x_poster import SITE_URL
    return SITE_URL


def build_tweet_text(name: str, product_name: str = "", release_date=None, *, site_url: str | None = None) -> str:
    """投稿本文を作る。280加重文字を超える場合は商品名→カード名の順に省略記号で詰める。

    URL のカード名は x_poster.format_bigmove_tweet と同じ urllib.parse.quote。
    URL は常に完全なカード名で作る（本文側だけ詰める）。
    """
    base = site_url if site_url is not None else _site_url()
    url = f"{base}/card/{urllib.parse.quote(name)}"
    date_text = _format_release_date(release_date)

    def render(n: str, p: str) -> str:
        lines = ["【新カード情報】", n]
        if p:
            lines.append(f"収録：{p}")
        if date_text:
            lines.append(f"発売日：{date_text}")
        lines += [url, "#遊戯王 #OCG"]
        return "\n".join(lines)

    product = (product_name or "").strip()
    shown_name = name
    text = render(shown_name, product)
    over = weighted_length(text) - TWEET_WEIGHT_LIMIT
    if over <= 0:
        return text

    if product:
        target = max(_MIN_PRODUCT_WEIGHT, weighted_length(product) - over)
        product = truncate_to_weight(product, target)
        text = render(shown_name, product)
        over = weighted_length(text) - TWEET_WEIGHT_LIMIT
        if over <= 0:
            return text

    target = max(_MIN_NAME_WEIGHT, weighted_length(shown_name) - over)
    while True:
        shown_name = truncate_to_weight(name, target)
        text = render(shown_name, product)
        if weighted_length(text) <= TWEET_WEIGHT_LIMIT or target <= _char_weight(_ELLIPSIS):
            return text
        target -= 2


# ──────────────────────────────────────────────
# 投稿条件
# ──────────────────────────────────────────────

def is_postable(card: dict, has_visible_image: bool, today: date) -> tuple[bool, str]:
    """投稿してよいカードか。(可否, 理由コード)。DBの予約状態は見ない。"""
    if card.get("status") != "approved":
        return False, "status_not_approved"
    if card.get("hidden"):
        return False, "hidden"
    if not has_visible_image:
        return False, "no_visible_image"
    rd = card.get("release_date")
    if rd:
        try:
            if date.fromisoformat(str(rd)[:10]) < today:
                return False, "already_released"
        except ValueError:
            return False, "release_date_invalid"
    return True, ""


def _today_jst() -> date:
    return datetime.now(JST).date()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────────────────────────────
# DB 操作
# ──────────────────────────────────────────────

_CARD_COLUMNS = "id, name, product_name, release_date, status, hidden"


def _fetch_card(sb, card_id: int) -> dict | None:
    resp = sb.table("unreleased_cards").select(_CARD_COLUMNS).eq("id", card_id).execute()
    return resp.data[0] if resp.data else None


def _fetch_visible_image_url(sb, card_id: int) -> str:
    """可視画像（hidden=false, deleted_at IS NULL）の公開URL。無ければ空文字。"""
    resp = (sb.table("official_card_images").select("public_url")
            .eq("unreleased_card_id", card_id).eq("hidden", False)
            .is_("deleted_at", "null").order("id", desc=True).limit(1).execute())
    for row in resp.data or []:
        url = (row.get("public_url") or "").strip()
        if url:
            return url
    return ""


def _is_duplicate_error(e: Exception) -> bool:
    return getattr(e, "code", None) == "23505" or "23505" in str(e) or "duplicate key" in str(e).lower()


def reserve(sb, card_id: int) -> str:
    """投稿の予約を取る。"reserved"（投稿してよい）/ "taken"（他が処理中か済み）/ "error"。"""
    now = _now_iso()
    try:
        sb.table(TABLE).insert({
            "card_id": card_id, "status": "posting", "attempts": 0, "updated_at": now,
        }).execute()
        return "reserved"
    except Exception as e:
        if not _is_duplicate_error(e):
            logger.error("[unreleased_x_post] 予約の挿入に失敗 card_id=%s: %s", card_id, e)
            return "error"
    # 主キー衝突: failed かつ attempts<上限 のときだけ再試行権を奪う（条件付き UPDATE なので1者だけ成功）
    try:
        resp = (sb.table(TABLE).update({"status": "posting", "updated_at": now})
                .eq("card_id", card_id).eq("status", "failed").lt("attempts", MAX_ATTEMPTS).execute())
        return "reserved" if resp.data else "taken"
    except Exception as e:
        logger.error("[unreleased_x_post] 再試行の予約に失敗 card_id=%s: %s", card_id, e)
        return "error"


def _notify(message: str) -> None:
    try:
        from discord_notify import send_discord_message
        send_discord_message(message)
    except Exception as e:
        logger.warning("[unreleased_x_post] Discord通知失敗: %s", e)


def _mark_posted(sb, card_id: int, tweet_id) -> None:
    try:
        sb.table(TABLE).update({
            "status": "posted", "tweet_id": str(tweet_id), "last_error": None, "updated_at": _now_iso(),
        }).eq("card_id", card_id).execute()
    except Exception as e:
        # 投稿は成功済み。posting のまま残るので二重投稿は起きない（手動で posted に直す）
        logger.error("[unreleased_x_post] 投稿済みの記録に失敗 card_id=%s tweet_id=%s: %s", card_id, tweet_id, e)
        _notify(f"[未発売X投稿] 投稿成功したがDB記録に失敗: card_id={card_id} tweet_id={tweet_id} ({e})。"
                f" unreleased_x_posts を手動で posted に直してください")


def _mark_failed(sb, card_id: int, error: str, name: str = "") -> None:
    attempts = 0
    try:
        resp = sb.table(TABLE).select("attempts").eq("card_id", card_id).execute()
        if resp.data:
            attempts = int(resp.data[0].get("attempts") or 0)
    except Exception as e:
        logger.warning("[unreleased_x_post] attempts 取得失敗 card_id=%s: %s", card_id, e)
    attempts += 1
    try:
        sb.table(TABLE).update({
            "status": "failed", "attempts": attempts, "last_error": error[:500], "updated_at": _now_iso(),
        }).eq("card_id", card_id).execute()
    except Exception as e:
        logger.error("[unreleased_x_post] 失敗の記録に失敗 card_id=%s: %s", card_id, e)
    tail = "（上限に達したため再試行しません）" if attempts >= MAX_ATTEMPTS else "（次回の回収で再試行します）"
    _notify(f"[未発売X投稿] 失敗 card_id={card_id} {name} attempts={attempts}/{MAX_ATTEMPTS}: {error[:200]} {tail}")


# ──────────────────────────────────────────────
# 画像ダウンロード
# ──────────────────────────────────────────────

def _download_image(url: str) -> str:
    """画像を一時ファイルに保存してパスを返す。呼び出し側が削除する。"""
    import requests
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    mime = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if not mime.startswith("image/"):
        raise ValueError(f"Content-Type が image/* でない: {mime!r}")
    if len(resp.content) > _MAX_IMAGE_BYTES:
        raise ValueError(f"画像サイズ超過: {len(resp.content):,}バイト")
    fd, path = tempfile.mkstemp(suffix=_EXT_BY_MIME.get(mime, ".jpg"))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(resp.content)
    except Exception:
        os.remove(path)
        raise
    return path


# ──────────────────────────────────────────────
# 投稿
# ──────────────────────────────────────────────

def post_card(sb, card_id: int, *, post_fn=None, today: date | None = None) -> str:
    """1枚を投稿する。結果コード:
      disabled / missing / not_postable / dry_run / already（他が処理中か済み）/ posted / failed / error
    例外は投げない（承認処理や回収を止めないため）。
    """
    if not is_enabled():
        return "disabled"
    try:
        return _post_card(sb, card_id, post_fn=post_fn, today=today or _today_jst())
    except Exception as e:
        logger.exception("[unreleased_x_post] 想定外のエラー card_id=%s: %s", card_id, e)
        return "error"


def _post_card(sb, card_id: int, *, post_fn, today: date) -> str:
    card = _fetch_card(sb, card_id)
    if not card:
        return "missing"
    image_url = _fetch_visible_image_url(sb, card_id)
    ok, reason = is_postable(card, bool(image_url), today)
    if not ok:
        logger.info("[unreleased_x_post] 投稿しない card_id=%s: %s", card_id, reason)
        return "not_postable"

    text = build_tweet_text(card["name"], card.get("product_name") or "", card.get("release_date"))

    if _is_dry_run():
        # 予約行は作らない（posted にも posting にも残さない）
        logger.info("[unreleased_x_post] DRY_RUN card_id=%s weight=%s\n%s", card_id, weighted_length(text), text)
        return "dry_run"

    reserved = reserve(sb, card_id)
    if reserved == "taken":
        return "already"
    if reserved == "error":
        return "error"
    return _send(sb, card, text, image_url, post_fn)


def _send(sb, card: dict, text: str, image_url: str, post_fn) -> str:
    card_id = card["id"]
    if post_fn is None:
        from x_poster import post_tweet as post_fn
    path = None
    try:
        path = _download_image(image_url)
        # 画像なし投稿を防ぐため画像必須で呼ぶ（添付に全部失敗したら None が返る）
        tweet_id = post_fn(text, [path], require_media=True)
    except Exception as e:
        _mark_failed(sb, card_id, f"{type(e).__name__}: {e}", card.get("name", ""))
        return "failed"
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
    if not tweet_id:
        # post_tweet は認証未設定・API失敗・DRY_RUN で None を返す。成功扱いにしない
        _mark_failed(sb, card_id, "post_tweet が None を返した（認証情報未設定・API失敗・画像添付失敗のいずれか。画像必須のため本文のみの投稿はしない）", card.get("name", ""))
        return "failed"
    _mark_posted(sb, card_id, tweet_id)
    logger.info("[unreleased_x_post] 投稿成功 card_id=%s tweet_id=%s", card_id, tweet_id)
    return "posted"


def post_cards_sequential(sb, card_ids, *, interval: float | None = None, post_fn=None, sleep=time.sleep) -> dict:
    """複数枚を順に投稿する。実際に投稿を試みた（posted/failed）後だけ間隔を空ける。結果コードの件数を返す。"""
    if not is_enabled():
        return {}
    wait = POST_INTERVAL_SEC if interval is None else interval
    counts: dict[str, int] = {}
    first = True
    for cid in card_ids:
        if not first and counts.get("_last") in ("posted", "failed"):
            sleep(wait)
        first = False
        result = post_card(sb, cid, post_fn=post_fn)
        counts[result] = counts.get(result, 0) + 1
        counts["_last"] = result
    counts.pop("_last", None)
    return counts


def start_post_thread(sb, card_ids) -> threading.Thread | None:
    """バックグラウンドで投稿する（承認レスポンスを待たせない）。無効時は何もしない。"""
    ids = list(card_ids)
    if not ids or not is_enabled():
        return None
    t = threading.Thread(target=post_cards_sequential, args=(sb, ids), daemon=True, name="unreleased-x-post")
    t.start()
    return t


# ──────────────────────────────────────────────
# 回収（承認時に投稿できなかった分）
# ──────────────────────────────────────────────

_PAGE = 1000
_IN_CHUNK = 100


def _fetch_all(query_builder_fn) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        batch = query_builder_fn().range(offset, offset + _PAGE - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < _PAGE:
            return rows
        offset += _PAGE


def find_backfill_candidates(sb, today: date | None = None) -> list[int]:
    """投稿条件を満たし、未投稿（行なし）か failed で attempts<上限 のカードID（古い順）。"""
    today = today or _today_jst()
    cards = _fetch_all(lambda: sb.table("unreleased_cards").select("id, release_date")
                       .eq("status", "approved").eq("hidden", False)
                       .or_(f"release_date.is.null,release_date.gte.{today.isoformat()}")
                       .order("id"))
    if not cards:
        return []
    posts = {r["card_id"]: r for r in _fetch_all(
        lambda: sb.table(TABLE).select("card_id, status, attempts").order("card_id"))}
    pending = [c["id"] for c in cards
               if c["id"] not in posts
               or (posts[c["id"]].get("status") == "failed" and int(posts[c["id"]].get("attempts") or 0) < MAX_ATTEMPTS)]
    if not pending:
        return []
    with_image: set[int] = set()
    for i in range(0, len(pending), _IN_CHUNK):
        chunk = pending[i:i + _IN_CHUNK]
        resp = (sb.table("official_card_images").select("unreleased_card_id")
                .in_("unreleased_card_id", chunk).eq("hidden", False).is_("deleted_at", "null").execute())
        with_image.update(r["unreleased_card_id"] for r in (resp.data or []))
    return [cid for cid in pending if cid in with_image]


def run_backfill(sb, *, post_fn=None, sleep=time.sleep) -> dict:
    """未投稿分を回収して投稿する（MAX_BACKFILL_PER_RUN 件まで）。結果コードの件数を返す。"""
    if not is_enabled():
        logger.info("[unreleased_x_post] %s が1ではないため回収しません", ENABLED_ENV)
        return {}
    candidates = find_backfill_candidates(sb)
    logger.info("[unreleased_x_post] 回収候補: %d件", len(candidates))
    if len(candidates) > MAX_BACKFILL_PER_RUN:
        logger.warning("[unreleased_x_post] 候補が上限(%d)を超えたため先頭のみ投稿: %d件",
                       MAX_BACKFILL_PER_RUN, len(candidates))
    return post_cards_sequential(sb, candidates[:MAX_BACKFILL_PER_RUN], post_fn=post_fn, sleep=sleep)
