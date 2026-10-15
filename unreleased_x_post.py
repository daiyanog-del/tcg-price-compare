"""承認された未発売カードを X に自動投稿する（2026-10-15 追加）。

承認＝公開。承認され、公開画像が揃ったカードを1枚ずつ投稿する。
  - 呼び出し元: admin_unreleased（単体/一括承認の画像取込後）と tools/post_unreleased_x.py（回収）
  - 二重投稿防止: テーブル unreleased_x_posts（card_id が主キー）に投稿前に「予約」行を入れる。
    主キー衝突＝他が処理中/済みなので何もしない。例外は「failed かつ attempts<3」と「rate_limited」の再試行だけ
    （条件付き UPDATE で1つの呼び出しだけが奪える）。
  - 投稿対象は extraction_raw.approval_snapshot があるカードだけ（このデプロイ以降に承認されたものの印）。
    同じ名前（fuzzy_key）で投稿済みがあれば skipped_duplicate、発売済みカード名なら投稿しない。
  - 失敗の扱い: 送信前の失敗・Xの明確な拒否(4xx)は failed（attempts+1、3回まで再試行）、
    429 は rate_limited（attempts を増やさず回収で再挑戦）、結果不明（応答なし・タイムアウト・5xx）は
    unknown（自動再試行しない・Discord通知。人が X を見て判断）。
  - 件数の上限: 1回の一括承認 MAX_BULK_POST_PER_RUN 件・回収 MAX_BACKFILL_PER_RUN 件・全経路で1日(JST) DAILY_POST_LIMIT 件。
    上限で止めた分は予約行を作らない（回収ジョブが後で拾う）。
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
from datetime import date, datetime, timedelta, timezone

from constants import JST
from name_normalize import fuzzy_key

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

# 一括承認1回あたりの投稿上限。超えた分は予約せず、回収ジョブ（3時間ごと）が拾う
# TODO: calibrate from data
MAX_BULK_POST_PER_RUN = 10

# 全経路共通の1日(JST)あたりの投稿上限。数えるのは unreleased_x_posts の posted と unknown（unknown は
# 実際には投稿されているかもしれないので安全側で数える）。X側の上限（pay-per-use: 100件/15分/ユーザー、
# 10,000件/日/アプリ）よりずっと手前の、暴走防止とタイムライン汚染防止のための自前の上限。
# TODO: calibrate from data
DAILY_POST_LIMIT = 30

# posting / unknown のまま放置されている行を通知に出すまでの経過時間（秒）
STUCK_AFTER_SEC = 3600

# x_poster.post_tweet_ex が返す失敗の種類（x_poster の ERR_* と同じ値。x_poster はここでは import しない）
KIND_NOT_CONFIGURED = "not_configured"
KIND_PRE_SEND = "pre_send"
KIND_DRY_RUN = "dry_run"
KIND_REJECTED = "rejected"
KIND_RATE_LIMITED = "rate_limited"
KIND_UNKNOWN = "unknown"

# X の認証情報の環境変数（x_poster.post_tweet_ex が読むものと同じ）
_X_CRED_ENVS = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")

# 連続投稿の間隔を複数スレッド（複数の一括承認・単体承認）で共有するためのロックと最終投稿時刻。
# 同じプロセス内だけの制御（Render と GitHub Actions など別プロセス間は守れない）
_post_lock = threading.Lock()
_last_attempt_at: float | None = None
_clock = time.monotonic

# 認証情報未設定の警告を1プロセスで1回だけ出すための印
_warned_not_configured = False

# 順番投稿を途中で打ち切る結果（続けても同じ理由で失敗する、または人の確認が要る）
_STOP_RESULTS = frozenset({"rate_limited", "daily_limit", "not_configured", "unknown"})

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
    """投稿してよいカードか。(可否, 理由コード)。DBの予約状態は見ない。

    approval_snapshot（承認時の記録）があるカードだけが対象。これは「このデプロイ以降に承認された」印で、
    SQL の実行と有効化の間に承認されて skipped_legacy 行が無いカードでも、過去分が流れないようにする。
    """
    if card.get("status") != "approved":
        return False, "status_not_approved"
    if card.get("hidden"):
        return False, "hidden"
    raw = card.get("extraction_raw")
    if not (isinstance(raw, dict) and isinstance(raw.get("approval_snapshot"), dict)):
        return False, "no_approval_snapshot"
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


# 発売済みカード名のインデックス（data/cardnames_ja.json。app.py の _cardnames_set / _cardnames_fuzzy と同じ元データ）
_released_lock = threading.Lock()
_released_index: tuple[set, set] | None = None


def _load_released_index() -> tuple[set, set]:
    """(正式名の集合, fuzzy_key の集合)。name_normalize.load_cardnames_index を使う（app.py に依存しない）。"""
    global _released_index
    with _released_lock:
        if _released_index is None:
            from name_normalize import load_cardnames_index
            _names, name_set, fuzzy = load_cardnames_index()
            if not name_set:
                logger.warning("[unreleased_x_post] data/cardnames_ja.json が空か無い（発売済み名の除外が効かない）")
            _released_index = (name_set, set(fuzzy))
        return _released_index


def _is_released_name(name: str) -> bool:
    """発売済みカード名の索引に含まれるか（完全一致または記号・全半角を除いた比較）。"""
    name_set, fuzzy_set = _load_released_index()
    if name in name_set:
        return True
    key = fuzzy_key(name or "")
    return bool(key) and key in fuzzy_set


# ──────────────────────────────────────────────
# DB 操作
# ──────────────────────────────────────────────

_CARD_COLUMNS = "id, name, product_name, release_date, status, hidden, extraction_raw"


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
    # 主キー衝突: 再試行してよい状態のときだけ再試行権を奪う（条件付き UPDATE なので1者だけ成功）。
    #   failed かつ attempts<上限 / rate_limited（429 は失敗回数に数えないので上限なし）。
    #   unknown（結果不明）は自動再試行しない＝奪わない。
    try:
        resp = (sb.table(TABLE).update({"status": "posting", "updated_at": now})
                .eq("card_id", card_id).eq("status", "failed").lt("attempts", MAX_ATTEMPTS).execute())
        if resp.data:
            return "reserved"
        resp = (sb.table(TABLE).update({"status": "posting", "updated_at": now})
                .eq("card_id", card_id).eq("status", "rate_limited").execute())
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


def _mark_rate_limited(sb, card_id: int, error: str, name: str = "") -> None:
    """429。投稿は作られていないので attempts は増やさず、status=rate_limited にして回収で再挑戦させる。

    予約行を消す案もあるが、行を残す方を採る: いつ・どう制限に当たったか（updated_at / last_error）が
    残り、同じ card_id の主キーによる二重投稿防止の予約も途切れない（消すと再予約までの隙間に他者が割り込める）。
    """
    try:
        sb.table(TABLE).update({
            "status": "rate_limited", "last_error": error[:500], "updated_at": _now_iso(),
        }).eq("card_id", card_id).execute()
    except Exception as e:
        logger.error("[unreleased_x_post] rate_limited の記録に失敗 card_id=%s: %s", card_id, e)
    _notify(f"[未発売X投稿] レート制限(429) card_id={card_id} {name}: 失敗回数には数えず、次回の回収で再挑戦します")


def _mark_unknown(sb, card_id: int, error: str, name: str = "") -> None:
    """結果不明。投稿されたかもしれないので自動再試行しない。人が X を見て posted にするか行を消す。"""
    try:
        sb.table(TABLE).update({
            "status": "unknown", "last_error": error[:500], "updated_at": _now_iso(),
        }).eq("card_id", card_id).execute()
    except Exception as e:
        logger.error("[unreleased_x_post] unknown の記録に失敗 card_id=%s: %s", card_id, e)
    _notify(f"[未発売X投稿] 結果不明 card_id={card_id} {name}: {error[:200]} "
            f"X のアカウントで投稿されたか確認してください。投稿済みなら unreleased_x_posts を posted に、"
            f"未投稿なら該当行を削除（自動再試行はしません）")


def _record_skip(sb, card_id: int, status: str, detail: str) -> None:
    """投稿しない理由を行として残す（skipped_duplicate）。既に行があれば、再試行待ちの行だけ置き換える。"""
    now = _now_iso()
    try:
        sb.table(TABLE).insert({"card_id": card_id, "status": status, "attempts": 0,
                                "last_error": detail[:500], "updated_at": now}).execute()
    except Exception as e:
        if not _is_duplicate_error(e):
            logger.error("[unreleased_x_post] スキップの記録に失敗 card_id=%s: %s", card_id, e)
            return
        try:
            (sb.table(TABLE).update({"status": status, "last_error": detail[:500], "updated_at": now})
             .eq("card_id", card_id).in_("status", ["failed", "rate_limited"]).execute())
        except Exception as e2:
            logger.error("[unreleased_x_post] スキップの記録(更新)に失敗 card_id=%s: %s", card_id, e2)


# ──────────────────────────────────────────────
# 重複・日次上限
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


def find_duplicate_posted(sb, card: dict) -> int | None:
    """同じカード名（fuzzy_key）で投稿済み（posted / unknown）の別カードの id。無ければ None。

    unknown は実際には投稿されているかもしれないので重複側として扱う。
    """
    key = fuzzy_key(card.get("name") or "")
    if not key:
        return None
    rows = _fetch_all(lambda: sb.table(TABLE).select("card_id").in_("status", ["posted", "unknown"]).order("card_id"))
    ids = [r["card_id"] for r in rows if r["card_id"] != card["id"]]
    for i in range(0, len(ids), _IN_CHUNK):
        chunk = ids[i:i + _IN_CHUNK]
        resp = sb.table("unreleased_cards").select("id, name").in_("id", chunk).execute()
        for r in resp.data or []:
            if fuzzy_key(r.get("name") or "") == key:
                return r["id"]
    return None


def count_posted_today(sb) -> int:
    """JST の当日に投稿した件数（posted と unknown。updated_at が当日0時(JST)以降の行）。"""
    start_jst = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_jst.astimezone(timezone.utc).isoformat()
    rows = _fetch_all(lambda: sb.table(TABLE).select("card_id").in_("status", ["posted", "unknown"])
                      .gte("updated_at", start_utc).order("card_id"))
    return len(rows)


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

def _x_configured() -> bool:
    """X の認証情報（4つの環境変数）が揃っているか。"""
    return all(os.environ.get(k) for k in _X_CRED_ENVS)


def post_card(sb, card_id: int, *, post_fn=None, today: date | None = None,
              interval: float | None = None, sleep=time.sleep) -> str:
    """1枚を投稿する。結果コード:
      disabled / missing / not_postable / skipped_duplicate / dry_run / not_configured / daily_limit /
      already（他が処理中か済み）/ posted / failed / rate_limited / unknown / error
    例外は投げない（承認処理や回収を止めないため）。
    """
    if not is_enabled():
        return "disabled"
    try:
        return _post_card(sb, card_id, post_fn=post_fn, today=today or _today_jst(),
                          interval=POST_INTERVAL_SEC if interval is None else interval, sleep=sleep)
    except Exception as e:
        logger.exception("[unreleased_x_post] 想定外のエラー card_id=%s: %s", card_id, e)
        return "error"


def _post_card(sb, card_id: int, *, post_fn, today: date, interval: float, sleep) -> str:
    global _warned_not_configured
    card = _fetch_card(sb, card_id)
    if not card:
        return "missing"
    image_url = _fetch_visible_image_url(sb, card_id)
    ok, reason = is_postable(card, bool(image_url), today)
    if not ok:
        logger.info("[unreleased_x_post] 投稿しない card_id=%s: %s", card_id, reason)
        return "not_postable"
    if _is_released_name(card.get("name") or ""):
        # 発売済みカード名の索引に載っている＝既に発売されている（未発売扱いのまま残っている）
        logger.info("[unreleased_x_post] 投稿しない card_id=%s: 発売済みカード名 %r", card_id, card.get("name"))
        return "not_postable"

    dup = find_duplicate_posted(sb, card)
    if dup is not None:
        logger.info("[unreleased_x_post] 同名で投稿済みのためスキップ card_id=%s (投稿済み card_id=%s)", card_id, dup)
        if not _is_dry_run():
            _record_skip(sb, card_id, "skipped_duplicate", f"同名で投稿済み card_id={dup}")
        return "skipped_duplicate"

    text = build_tweet_text(card["name"], card.get("product_name") or "", card.get("release_date"))

    if _is_dry_run():
        # 予約行は作らない（posted にも posting にも残さない）
        logger.info("[unreleased_x_post] DRY_RUN card_id=%s weight=%s\n%s", card_id, weighted_length(text), text)
        return "dry_run"

    if not _x_configured():
        # 認証情報が無いまま予約すると失敗回数だけ増える。予約せずログのみ（警告は1プロセス1回）
        if not _warned_not_configured:
            _warned_not_configured = True
            logger.warning("[unreleased_x_post] X の認証情報(%s)が未設定のため投稿しません（予約もしません）",
                           "/".join(_X_CRED_ENVS))
        return "not_configured"

    # 日次上限の確認〜予約〜間隔待ち〜送信までをプロセス内で直列化する。
    # 複数の一括承認スレッドが同時に動いても、上限の数え漏れと連続投稿を防ぐ。
    with _post_lock:
        posted_today = count_posted_today(sb)
        if posted_today >= DAILY_POST_LIMIT:
            logger.warning("[unreleased_x_post] 本日(JST)の投稿上限 %d 件に達したため投稿しません card_id=%s"
                           "（予約しない・翌日以降の回収で投稿）", DAILY_POST_LIMIT, card_id)
            return "daily_limit"
        reserved = reserve(sb, card_id)
        if reserved == "taken":
            return "already"
        if reserved == "error":
            return "error"
        _wait_interval(interval, sleep)
        try:
            return _send(sb, card, text, image_url, post_fn)
        finally:
            _mark_attempt_time()


def _wait_interval(interval: float, sleep) -> None:
    """直前の投稿試行から interval 秒空くまで待つ（プロセス内の全スレッドで共有）。_post_lock の中で呼ぶ。"""
    if _last_attempt_at is None:
        return
    remaining = interval - (_clock() - _last_attempt_at)
    if remaining > 0:
        sleep(remaining)


def _mark_attempt_time() -> None:
    global _last_attempt_at
    _last_attempt_at = _clock()


_LEGACY_NONE_MESSAGE = ("post_tweet が None を返した（認証情報未設定・API失敗・画像添付失敗のいずれか。"
                        "画像必須のため本文のみの投稿はしない）")


def _send(sb, card: dict, text: str, image_url: str, post_fn) -> str:
    """送信して結果を記録する。戻り値: posted / failed / rate_limited / unknown。

    post_fn は (tweet_id, error_kind) を返す関数（x_poster.post_tweet_ex）。
    tweet_id だけを返す旧形式の関数も受け付け、None は送信前の失敗として扱う。
    """
    card_id = card["id"]
    name = card.get("name", "")
    path = None
    try:
        # ここまでは送信前。失敗しても X には何も送っていない
        try:
            if post_fn is None:
                from x_poster import post_tweet_ex as post_fn
            path = _download_image(image_url)
        except Exception as e:
            _mark_failed(sb, card_id, f"{type(e).__name__}: {e}", name)
            return "failed"
        try:
            # 画像なし投稿を防ぐため画像必須で呼ぶ（添付に全部失敗したら失敗が返る）
            result = post_fn(text, [path], require_media=True)
        except Exception as e:
            # post_tweet_ex は例外を投げない設計。投げられたら送信済みかどうか分からない
            _mark_unknown(sb, card_id, f"投稿呼び出しで例外 {type(e).__name__}: {e}", name)
            return "unknown"
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
    tweet_id, kind = result if isinstance(result, tuple) else (result, None)
    if tweet_id:
        _mark_posted(sb, card_id, tweet_id)
        logger.info("[unreleased_x_post] 投稿成功 card_id=%s tweet_id=%s", card_id, tweet_id)
        return "posted"
    if kind == KIND_RATE_LIMITED:
        _mark_rate_limited(sb, card_id, "X のレート制限(429)", name)
        return "rate_limited"
    if kind == KIND_UNKNOWN:
        _mark_unknown(sb, card_id, "X への送信結果が不明（応答なし・タイムアウト・5xx のいずれか）", name)
        return "unknown"
    if kind == KIND_REJECTED:
        message = "X が投稿を明確に拒否した（4xx。詳細はジョブのログ）"
    elif kind == KIND_NOT_CONFIGURED:
        message = "X の認証情報が未設定"
    else:
        # pre_send / dry_run / 旧形式の None
        message = _LEGACY_NONE_MESSAGE
    _mark_failed(sb, card_id, message, name)
    return "failed"


def post_cards_sequential(sb, card_ids, *, interval: float | None = None, post_fn=None, sleep=time.sleep,
                          max_cards: int | None = None) -> dict:
    """複数枚を順に投稿する。結果コードの件数を返す。

    max_cards: 先頭から何枚まで試すか。超えた分は予約しない（回収ジョブが後で拾う）。
    連続投稿の間隔は post_card 側（プロセス共有）で守る。上限・制限・未設定・結果不明が出たら以降は打ち切る。
    """
    if not is_enabled():
        return {}
    ids = list(card_ids)
    if max_cards is not None and len(ids) > max_cards:
        logger.warning("[unreleased_x_post] 対象が上限(%d)を超えたため先頭のみ投稿: %d件中。残りは回収ジョブへ",
                       max_cards, len(ids))
        ids = ids[:max_cards]
    counts: dict[str, int] = {}
    for i, cid in enumerate(ids):
        result = post_card(sb, cid, post_fn=post_fn, interval=interval, sleep=sleep)
        counts[result] = counts.get(result, 0) + 1
        if result in _STOP_RESULTS:
            logger.warning("[unreleased_x_post] %s のため残り%d件の投稿を打ち切り（回収ジョブで再挑戦）",
                           result, len(ids) - i - 1)
            break
    return counts


def start_post_thread(sb, card_ids) -> threading.Thread | None:
    """バックグラウンドで投稿する（承認レスポンスを待たせない）。無効時は何もしない。"""
    ids = list(card_ids)
    if not ids or not is_enabled():
        return None
    t = threading.Thread(target=post_cards_sequential, args=(sb, ids),
                         kwargs={"max_cards": MAX_BULK_POST_PER_RUN}, daemon=True, name="unreleased-x-post")
    t.start()
    return t


# ──────────────────────────────────────────────
# 回収（承認時に投稿できなかった分）
# ──────────────────────────────────────────────

def find_backfill_candidates(sb, today: date | None = None) -> list[int]:
    """投稿条件を満たし、未投稿（行なし）か 再試行待ち（failed で attempts<上限 / rate_limited）のカードID（古い順）。

    approval_snapshot が無いカード（このデプロイ以前の承認）は含めない。
    """
    today = today or _today_jst()
    cards = _fetch_all(lambda: sb.table("unreleased_cards").select("id, release_date")
                       .eq("status", "approved").eq("hidden", False)
                       .or_(f"release_date.is.null,release_date.gte.{today.isoformat()}")
                       .order("id"))
    if not cards:
        return []
    posts = {r["card_id"]: r for r in _fetch_all(
        lambda: sb.table(TABLE).select("card_id, status, attempts").order("card_id"))}

    def retryable(cid: int) -> bool:
        row = posts.get(cid)
        if row is None:
            return True
        if row.get("status") == "rate_limited":
            return True
        return row.get("status") == "failed" and int(row.get("attempts") or 0) < MAX_ATTEMPTS

    pending = [c["id"] for c in cards if retryable(c["id"])]
    if not pending:
        return []
    # 承認記録のあるカードだけ（このデプロイ以降の承認）。候補になりうる分だけ extraction_raw を読む
    with_snapshot: set[int] = set()
    for i in range(0, len(pending), _IN_CHUNK):
        chunk = pending[i:i + _IN_CHUNK]
        resp = sb.table("unreleased_cards").select("id, extraction_raw").in_("id", chunk).execute()
        for r in resp.data or []:
            raw = r.get("extraction_raw")
            if isinstance(raw, dict) and isinstance(raw.get("approval_snapshot"), dict):
                with_snapshot.add(r["id"])
    pending = [cid for cid in pending if cid in with_snapshot]
    if not pending:
        return []
    with_image: set[int] = set()
    for i in range(0, len(pending), _IN_CHUNK):
        chunk = pending[i:i + _IN_CHUNK]
        resp = (sb.table("official_card_images").select("unreleased_card_id")
                .in_("unreleased_card_id", chunk).eq("hidden", False).is_("deleted_at", "null").execute())
        with_image.update(r["unreleased_card_id"] for r in (resp.data or []))
    return [cid for cid in pending if cid in with_image]


def notify_stuck_rows(sb, *, now: datetime | None = None) -> int:
    """STUCK_AFTER_SEC 以上前に更新されたきり posting / unknown の行を Discord に通知する。件数を返す。

    自動で再投稿はしない（投稿済みかもしれないので、人が X を見て posted にするか行を消す）。
    """
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(seconds=STUCK_AFTER_SEC)).isoformat()
    rows = _fetch_all(lambda: sb.table(TABLE).select("card_id, status, updated_at, last_error")
                      .in_("status", ["posting", "unknown"]).lt("updated_at", cutoff).order("card_id"))
    if not rows:
        return 0
    lines = [f"card_id={r['card_id']} {r['status']} (最終更新 {str(r.get('updated_at'))[:16]})" for r in rows[:20]]
    more = f" ほか{len(rows) - 20}件" if len(rows) > 20 else ""
    _notify("[未発売X投稿] 1時間以上 posting / unknown のままの行があります（自動再投稿はしません）。"
            "Xで投稿の有無を確認し、投稿済みなら posted に、未投稿なら行を削除してください:\n"
            + "\n".join(lines) + more)
    return len(rows)


def run_backfill(sb, *, post_fn=None, sleep=time.sleep) -> dict:
    """未投稿分を回収して投稿する（MAX_BACKFILL_PER_RUN 件まで）。結果コードの件数を返す。"""
    if not is_enabled():
        logger.info("[unreleased_x_post] %s が1ではないため回収しません", ENABLED_ENV)
        return {}
    try:
        stuck = notify_stuck_rows(sb)
        if stuck:
            logger.warning("[unreleased_x_post] posting/unknown のまま残っている行: %d件（Discord通知済み）", stuck)
    except Exception as e:
        logger.warning("[unreleased_x_post] 取り残し行の確認に失敗: %s", e)
    candidates = find_backfill_candidates(sb)
    logger.info("[unreleased_x_post] 回収候補: %d件", len(candidates))
    return post_cards_sequential(sb, candidates, post_fn=post_fn, sleep=sleep, max_cards=MAX_BACKFILL_PER_RUN)
