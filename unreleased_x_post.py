"""承認された未発売カードを X に自動投稿する（2026-10-15 追加）。

承認＝公開。承認され、公開画像が揃ったカードを1枚ずつ投稿する。
  - 呼び出し元: admin_unreleased（単体/一括承認の画像取込後）と tools/post_unreleased_x.py（回収）
  - 二重投稿防止: テーブル unreleased_x_posts（card_id が主キー）に投稿前に「予約」行を入れる。
    主キー衝突＝他が処理中/済みなので何もしない。例外は「failed かつ attempts<3」と「rate_limited」の再試行だけ
    （条件付き UPDATE で1つの呼び出しだけが奪える）。
  - 投稿対象は unreleased_cards.approved_at が入っているカードだけ（承認の UPDATE と同時に入る列。
    導入前の承認済みカードは NULL のまま＝過去分は流れない）。approval_snapshot は計測用で投稿条件ではない。
    同じ名前（fuzzy_key）で投稿済みがあれば skipped_duplicate、発売済みカード名なら投稿しない。
  - 失敗の扱い: 送信前の失敗・Xの明確な拒否(4xx)は failed（attempts+1、3回まで再試行）、
    429 は rate_limited（attempts を増やさず回収で再挑戦）、結果不明（応答なし・タイムアウト・5xx）は
    unknown（自動再試行しない・Discord通知。人が X を見て判断）。
  - 件数の上限: 1回の一括承認 MAX_BULK_POST_PER_RUN 件・回収 MAX_BACKFILL_PER_RUN 件・全経路で1日(JST) DAILY_POST_LIMIT 件。
    1回の上限は「実際に送信を試みた件数」で数える（行を作らない結果では枠を消費しない）。
    上限で止めた分は予約行を作らない（回収ジョブが後で拾う）。
  - 既定は無効。環境変数 UNRELEASED_X_POST_ENABLED=1 のときだけ動く。
  - X_POST_DRY_RUN=1 のときは投稿せず本文をログに出すだけ（予約行も作らない）。
  - 非公開にしたときの投稿削除（2026-10-15 追加）: 承認済みカードを非公開にする操作（承認待ちに戻す・却下・
    非表示・削除）で、posted の行があれば X の投稿を削除して行を消す（誤字を直して再承認すれば再投稿される）。
    削除は承認操作の応答を待たせないようバックグラウンド。失敗は行を delete_failed にして残し Discord 通知。
    DRY_RUN 中は削除もしない（ログのみ）。X認証が無い process では行を残して警告する。

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

# 429（rate_limited）の再挑戦の上限。初回の予約（行の created_at）から経過時間か累計回数のどちらかを
# 超えたら failed にして諦める（Discord通知は1回）。無限に再挑戦し続けないため。
# TODO: calibrate from data
RATE_LIMIT_MAX_AGE_SEC = 24 * 3600
RATE_LIMIT_MAX_COUNT = 8

# 取り残し行（posting / unknown）の通知は、同じ行について最短でこの間隔を空ける（1日1回まで）
STUCK_RENOTIFY_SEC = 24 * 3600

# 結果不明(unknown)の通知に添える確認手順。unknown は同名の重複判定に使われるため、
# 未投稿だった場合は同名で skipped_duplicate になったカードの行も消さないと永久に投稿されない
UNKNOWN_GUIDE = ("X のアカウントで投稿されたか確認してください。投稿済みなら unreleased_x_posts を posted に（tweet_id も）、"
                 "未投稿なら該当行を削除（自動再試行はしません）。未投稿だった場合は、同名で skipped_duplicate に"
                 "なったカードが無いかも確認し、あればその行も削除してください（unknown が同名の重複判定に使われたため）")

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

# 非公開時の投稿削除に失敗した行の印（行は残す。X 上の投稿はまだ生きている可能性がある）
STATUS_DELETE_FAILED = "delete_failed"

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

    情報の出所は公式で、TCGYM は「試せる場所」という立場の文面（docs/decisions.md 2026-10-15）。
    リンク先はシミュレーター直行（/solitaire?newcards=1&focus=カード名）。
    focus は & # ? / なども含めて必ずエンコードする（quote の safe=""）。
    URL は常に完全なカード名で作る（本文側だけ詰める）。
    """
    base = site_url if site_url is not None else _site_url()
    url = f"{base}/solitaire?newcards=1&focus={urllib.parse.quote(name, safe='')}"
    date_text = _format_release_date(release_date)

    def render(n: str, p: str) -> str:
        lines = [f"公式発表の新カード「{n}」が、TCGYMのシミュレーターで使えるようになりました。"]
        info = []
        if p:
            info.append(f"収録：{p}")
        if date_text:
            info.append(f"発売日：{date_text}")
        if info:
            lines.append("／".join(info))
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

    approved_at（承認 UPDATE と同時に入る時刻）があるカードだけが対象。導入前の承認済みカードは NULL なので、
    SQL の実行・有効化のタイミングに関係なく過去分は流れない。
    """
    if card.get("status") != "approved":
        return False, "status_not_approved"
    if card.get("hidden"):
        return False, "hidden"
    if not card.get("approved_at"):
        return False, "no_approved_at"
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

_CARD_COLUMNS = "id, name, product_name, release_date, status, hidden, approved_at"


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
        # カード名など外部由来の文字列を載せるので、メンション（@everyone 等）は無効化する
        send_discord_message(message, no_mentions=True)
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


def _mark_rate_limited(sb, card_id: int, error: str, name: str = "", *, now: datetime | None = None) -> None:
    """429。投稿は作られていないので attempts は増やさず、status=rate_limited にして回収で再挑戦させる。

    予約行を消す案もあるが、行を残す方を採る: いつ・どう制限に当たったか（updated_at / last_error）が
    残り、同じ card_id の主キーによる二重投稿防止の予約も途切れない（消すと再予約までの隙間に他者が割り込める）。

    再挑戦には上限がある: 初回の予約(created_at)から RATE_LIMIT_MAX_AGE_SEC 経過、または累計
    RATE_LIMIT_MAX_COUNT 回に達したら failed（attempts=MAX_ATTEMPTS）にして諦め、通知は1回だけ出す。
    """
    now = now or datetime.now(timezone.utc)
    count = 0
    age_sec = 0.0
    try:
        resp = sb.table(TABLE).select("created_at, rate_limit_count").eq("card_id", card_id).execute()
        if resp.data:
            count = int(resp.data[0].get("rate_limit_count") or 0)
            created = _parse_ts(resp.data[0].get("created_at"))
            if created is not None:
                age_sec = (now - created).total_seconds()
    except Exception as e:
        logger.warning("[unreleased_x_post] rate_limit_count 取得失敗 card_id=%s: %s", card_id, e)
    count += 1
    if count >= RATE_LIMIT_MAX_COUNT or age_sec >= RATE_LIMIT_MAX_AGE_SEC:
        reason = (f"レート制限(429)の再挑戦が上限に達したため諦めました（累計{count}回・初回から{age_sec / 3600:.1f}時間）: "
                  f"{error}")
        try:
            sb.table(TABLE).update({
                "status": "failed", "attempts": MAX_ATTEMPTS, "rate_limit_count": count,
                "last_error": reason[:500], "updated_at": _now_iso(),
            }).eq("card_id", card_id).execute()
        except Exception as e:
            logger.error("[unreleased_x_post] rate_limited 打ち切りの記録に失敗 card_id=%s: %s", card_id, e)
        _notify(f"[未発売X投稿] レート制限(429)が続いたため再挑戦を打ち切りました card_id={card_id} {name}: "
                f"累計{count}回・初回から{age_sec / 3600:.1f}時間。X の制限状況を確認し、投稿したければ "
                f"unreleased_x_posts の該当行を削除してください")
        return
    try:
        sb.table(TABLE).update({
            "status": "rate_limited", "rate_limit_count": count, "last_error": error[:500], "updated_at": _now_iso(),
        }).eq("card_id", card_id).execute()
    except Exception as e:
        logger.error("[unreleased_x_post] rate_limited の記録に失敗 card_id=%s: %s", card_id, e)
    _notify(f"[未発売X投稿] レート制限(429) card_id={card_id} {name}: 失敗回数には数えず、次回の回収で再挑戦します"
            f"（{count}/{RATE_LIMIT_MAX_COUNT}回目）")


def _parse_ts(value) -> datetime | None:
    """ISO 文字列を timezone つき datetime に。解釈できなければ None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _mark_unknown(sb, card_id: int, error: str, name: str = "") -> None:
    """結果不明。投稿されたかもしれないので自動再試行しない。人が X を見て posted にするか行を消す。"""
    try:
        sb.table(TABLE).update({
            "status": "unknown", "last_error": error[:500], "updated_at": _now_iso(),
        }).eq("card_id", card_id).execute()
    except Exception as e:
        logger.error("[unreleased_x_post] unknown の記録に失敗 card_id=%s: %s", card_id, e)
    _notify(f"[未発売X投稿] 結果不明 card_id={card_id} {name}: {error[:200]} {UNKNOWN_GUIDE}")


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


def _posted_name_keys(sb, exclude_id: int | None = None) -> dict[str, int]:
    """投稿済み（posted / unknown / delete_failed）カードの fuzzy_key → card_id。

    unknown は実際には投稿済みかもしれないので含める。delete_failed は削除に失敗して X 上に投稿が残っているので含める。
    """
    rows = _fetch_all(lambda: sb.table(TABLE).select("card_id")
                      .in_("status", ["posted", "unknown", STATUS_DELETE_FAILED]).order("card_id"))
    ids = [r["card_id"] for r in rows if r["card_id"] != exclude_id]
    keys: dict[str, int] = {}
    for i in range(0, len(ids), _IN_CHUNK):
        chunk = ids[i:i + _IN_CHUNK]
        resp = sb.table("unreleased_cards").select("id, name").in_("id", chunk).execute()
        for r in resp.data or []:
            k = fuzzy_key(r.get("name") or "")
            if k:
                keys.setdefault(k, r["id"])
    return keys


def find_duplicate_posted(sb, card: dict) -> int | None:
    """同じカード名（fuzzy_key）で投稿済み（posted / unknown）の別カードの id。無ければ None。

    unknown は実際には投稿されているかもしれないので重複側として扱う。
    """
    key = fuzzy_key(card.get("name") or "")
    if not key:
        return None
    return _posted_name_keys(sb, exclude_id=card["id"]).get(key)


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

    text = build_tweet_text(card["name"], card.get("product_name") or "", card.get("release_date"))

    if not _is_dry_run() and not _x_configured():
        # 認証情報が無いまま予約すると失敗回数だけ増える。予約せずログのみ（警告は1プロセス1回）
        if not _warned_not_configured:
            _warned_not_configured = True
            logger.warning("[unreleased_x_post] X の認証情報(%s)が未設定のため投稿しません（予約もしません）",
                           "/".join(_X_CRED_ENVS))
        return "not_configured"

    # 同名重複の確認〜日次上限の確認〜予約〜間隔待ち〜送信までをプロセス内で直列化する。
    # 重複確認もロックの中で行う: 外だと、同名の2枚が同時に動いたとき双方が「投稿済みなし」と判定して
    # 両方投稿してしまう。複数の一括承認スレッドが同時に動いても、上限の数え漏れと連続投稿も防ぐ。
    with _post_lock:
        dup = find_duplicate_posted(sb, card)
        if dup is not None:
            logger.info("[unreleased_x_post] 同名で投稿済みのためスキップ card_id=%s (投稿済み card_id=%s)", card_id, dup)
            if not _is_dry_run():
                _record_skip(sb, card_id, "skipped_duplicate", f"同名で投稿済み card_id={dup}")
            return "skipped_duplicate"

        if _is_dry_run():
            # 予約行は作らない（posted にも posting にも残さない）
            logger.info("[unreleased_x_post] DRY_RUN card_id=%s weight=%s\n%s", card_id, weighted_length(text), text)
            return "dry_run"

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
        # 予約〜間隔待ちの間（最大 POST_INTERVAL_SEC×件数）に、管理者が非公開にしたかもしれない。
        # 送る直前にカードを読み直して投稿条件を再判定し、不可なら予約行を消して送らない
        fresh = _fetch_card(sb, card_id)
        fresh_image = _fetch_visible_image_url(sb, card_id) if fresh else ""
        ok_now, reason_now = (is_postable(fresh, bool(fresh_image), today) if fresh else (False, "missing"))
        if not ok_now:
            logger.info("[unreleased_x_post] 送信直前の再判定で投稿しない card_id=%s: %s", card_id, reason_now)
            _release_reservation(sb, card_id)
            return "not_postable"
        try:
            result = _send(sb, fresh, text, fresh_image or image_url, post_fn)
        finally:
            _mark_attempt_time()
    if result == "posted":
        _delete_if_unpublished_after_post(sb, card_id)
    return result


def _release_reservation(sb, card_id: int) -> None:
    """送らなかった予約行（posting）を消す。他の状態の行は触らない。"""
    try:
        sb.table(TABLE).delete().eq("card_id", card_id).eq("status", "posting").execute()
    except Exception as e:
        logger.error("[unreleased_x_post] 予約行の解除に失敗 card_id=%s: %s", card_id, e)


def _delete_if_unpublished_after_post(sb, card_id: int) -> None:
    """投稿が成功した直後にカードを確認し、送信中に非公開にされていたら投稿を削除する。

    非公開化の操作時点では行が posting（tweet_id なし）で削除できなかった場合の取りこぼしを塞ぐ。
    """
    try:
        card = _fetch_card(sb, card_id)
        if card and card.get("status") == "approved" and not card.get("hidden"):
            return
        logger.warning("[unreleased_x_post] 送信中に非公開にされたため、投稿済みの投稿を削除する card_id=%s", card_id)
        delete_posts(sb, collect_posted(sb, [card_id]))
    except Exception as e:
        logger.error("[unreleased_x_post] 投稿後の公開状態の確認に失敗 card_id=%s: %s", card_id, e)
        _notify(f"[未発売X投稿] 投稿後の公開状態を確認できませんでした card_id={card_id}: {e}。"
                f"非公開にしていたなら X 投稿を手動で確認してください")


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


# 「実際に送信を試みた」結果。max_cards の枠はこれだけを数える
# （missing / error / not_postable / already / skipped_duplicate などは送信せず、枠を消費しない）。
# dry_run は本番なら送信していた分なので数える。
_ATTEMPT_RESULTS = frozenset({"posted", "failed", "rate_limited", "unknown", "dry_run"})


def post_cards_sequential(sb, card_ids, *, interval: float | None = None, post_fn=None, sleep=time.sleep,
                          max_cards: int | None = None) -> dict:
    """複数枚を順に投稿する。結果コードの件数を返す。

    max_cards: 実際に送信を試みた件数の上限（_ATTEMPT_RESULTS）。到達したら残りは予約せず回収ジョブに回す。
    送信に至らなかった結果（対象外・行なし・同名スキップなど）は枠を消費しない。
    連続投稿の間隔は post_card 側（プロセス共有）で守る。上限・制限・未設定・結果不明が出たら以降は打ち切る。
    """
    if not is_enabled():
        return {}
    ids = list(card_ids)
    counts: dict[str, int] = {}
    attempted = 0
    for i, cid in enumerate(ids):
        if max_cards is not None and attempted >= max_cards:
            logger.warning("[unreleased_x_post] 送信試行が上限(%d)に達したため残り%d件は回収ジョブへ",
                           max_cards, len(ids) - i)
            break
        result = post_card(sb, cid, post_fn=post_fn, interval=interval, sleep=sleep)
        counts[result] = counts.get(result, 0) + 1
        if result in _ATTEMPT_RESULTS:
            attempted += 1
        if result in _STOP_RESULTS:
            logger.warning("[unreleased_x_post] %s のため残り%d件の投稿を打ち切り（回収ジョブで再挑戦）",
                           result, len(ids) - i - 1)
            break
    return counts


def start_post_thread(sb, card_ids) -> threading.Thread | None:
    """バックグラウンドで投稿する（承認レスポンスを待たせない）。

    無効時、またはこの process に X の認証情報（4つ）が無いとき（DRY_RUN を除く）は何もしない。
    認証の無い process（自宅PCの監視など）で承認したカードは、回収ジョブ（tools/post_unreleased_x.py・3時間ごと）が拾う。
    """
    ids = list(card_ids)
    if not ids or not is_enabled():
        return None
    if not _is_dry_run() and not _x_configured():
        logger.info("[unreleased_x_post] この process に X の認証情報が無いため即時投稿しない（回収ジョブが投稿する）")
        return None
    t = threading.Thread(target=post_cards_sequential, args=(sb, ids),
                         kwargs={"max_cards": MAX_BULK_POST_PER_RUN}, daemon=True, name="unreleased-x-post")
    t.start()
    return t


# ──────────────────────────────────────────────
# 非公開にしたときの投稿削除
# ──────────────────────────────────────────────

def collect_posted(sb, card_ids) -> list[dict]:
    """card_ids のうち X への投稿に関わる行を返す。

    [{"card_id", "tweet_id", "name", "status"}]。
    posted / delete_failed（tweet_id あり）は削除対象。posting / unknown（投稿中・結果不明。tweet_id は無いことが多い）は
    削除できないが、非公開にした時点で通知するために返す（delete_posts が通知する）。
    tweet_id の無い posted / delete_failed は削除できないので返さない。
    DELETE（行ごと消える）の前に呼ぶこと: ON DELETE CASCADE で unreleased_x_posts の行も消え、tweet_id を読めなくなる。
    """
    ids = list(card_ids)
    if not ids:
        return []
    rows = (sb.table(TABLE).select("card_id, status, tweet_id").in_("card_id", ids)
            .in_("status", ["posted", STATUS_DELETE_FAILED, "posting", "unknown"]).execute().data or [])
    rows = [r for r in rows if r.get("tweet_id") or r.get("status") in ("posting", "unknown")]
    names: dict = {}
    if rows:
        try:
            resp = sb.table("unreleased_cards").select("id, name").in_("id", [r["card_id"] for r in rows]).execute()
            names = {r["id"]: r.get("name") or "" for r in (resp.data or [])}
        except Exception as e:
            logger.warning("[unreleased_x_post] 削除通知用のカード名の取得に失敗（無視）: %s", e)
    return [{"card_id": r["card_id"], "tweet_id": str(r["tweet_id"]) if r.get("tweet_id") else None,
             "name": names.get(r["card_id"], ""), "status": r.get("status")} for r in rows]


def _mark_delete_failed(sb, row: dict, error: str) -> None:
    """削除に失敗した行を delete_failed にする（行が既に無い＝DELETE 経路のときは何もしない）。"""
    try:
        (sb.table(TABLE).update({"status": STATUS_DELETE_FAILED, "last_error": error[:500], "updated_at": _now_iso()})
         .eq("card_id", row["card_id"]).eq("tweet_id", row["tweet_id"]).execute())
    except Exception as e:
        logger.error("[unreleased_x_post] delete_failed の記録に失敗 card_id=%s: %s", row["card_id"], e)


def delete_posts(sb, rows, *, delete_fn=None) -> dict:
    """collect_posted の行の X 投稿を順に削除する。結果コードの件数を返す。例外は投げない。

    成功（削除済み=404 を含む）→ unreleased_x_posts の行を消す（再承認で再投稿できる）。
    失敗 → 行を delete_failed にして残し、Discord に通知（tweet_id つき。DELETE 経路は行が無いので通知が唯一の手がかり）。
    DRY_RUN → 削除せずログのみ。X認証が無い process → 行を残して警告（Discord にも出す）。
    delete_fn は (tweet_id) → (ok, error_kind)（x_poster.delete_tweet_ex）。テスト用に差し替えられる。
    """
    rows = list(rows)
    counts: dict[str, int] = {}

    def bump(k):
        counts[k] = counts.get(k, 0) + 1

    # 投稿中（posting）・結果不明（unknown）のまま非公開にした行は削除できない。人が X を確認できるよう通知する
    # （posting は送信直前の再判定と投稿後の確認が取りこぼしを塞ぐが、unknown は人の確認が要る）
    in_flight = [r for r in rows if r.get("status") in ("posting", "unknown") and not r.get("tweet_id")]
    if in_flight:
        _notify("[未発売X投稿] 投稿中／結果不明のまま非公開にしたカードがあります。X に投稿が出ていれば手動で削除してください: "
                + ", ".join(f"card_id={r['card_id']} {r['name']}（{r['status']}）" for r in in_flight[:10]))
        counts["in_flight"] = len(in_flight)
    rows = [r for r in rows if r.get("tweet_id")]
    if not rows:
        return counts
    if _is_dry_run():
        for r in rows:
            logger.info("[unreleased_x_post] DRY_RUN: 投稿の削除をしない card_id=%s tweet_id=%s", r["card_id"], r["tweet_id"])
            bump("dry_run")
        return counts
    if delete_fn is None and not _x_configured():
        msg = ("[未発売X投稿] X の認証情報が無い process で非公開にしたため、投稿を削除できません（行は残します）: "
               + ", ".join(f"card_id={r['card_id']} {r['name']} tweet_id={r['tweet_id']}" for r in rows[:10]))
        logger.warning(msg)
        _notify(msg)
        counts["not_configured"] = len(rows)
        return counts
    if delete_fn is None:
        from x_poster import delete_tweet_ex as delete_fn
    for r in rows:
        try:
            ok, kind = delete_fn(r["tweet_id"])
        except Exception as e:  # delete_tweet_ex は投げない設計。投げられたら結果不明として失敗扱い
            ok, kind = False, f"exception:{type(e).__name__}: {e}"
        if ok:
            try:
                (sb.table(TABLE).delete().eq("card_id", r["card_id"]).eq("tweet_id", r["tweet_id"]).execute())
            except Exception as e:
                logger.error("[unreleased_x_post] 削除済みの行の削除に失敗 card_id=%s: %s", r["card_id"], e)
            logger.info("[unreleased_x_post] 非公開に伴い X の投稿を削除 card_id=%s tweet_id=%s", r["card_id"], r["tweet_id"])
            bump("deleted")
            continue
        _mark_delete_failed(sb, r, f"X の投稿の削除に失敗: {kind}")
        _notify(f"[未発売X投稿] 非公開にしたカードの X 投稿を削除できませんでした card_id={r['card_id']} {r['name']} "
                f"tweet_id={r['tweet_id']} 理由={kind}。X で手動削除し、unreleased_x_posts の該当行を削除してください"
                f"（削除済みなのに失敗が出た場合も、行を削除すれば再承認で再投稿できます）")
        bump("failed")
    return counts


def start_delete_thread(sb, rows) -> threading.Thread | None:
    """バックグラウンドで投稿を削除する（管理画面の操作の応答を待たせない）。対象が無ければ何もしない。"""
    rows = list(rows)
    if not rows:
        return None
    t = threading.Thread(target=delete_posts, args=(sb, rows), daemon=True, name="unreleased-x-delete")
    t.start()
    return t


def collect_posted_safe(sb, card_ids) -> list[dict]:
    """collect_posted の例外を握りつぶす版（管理画面の非公開操作を、X側の状態が読めないことで止めない）。"""
    try:
        return collect_posted(sb, card_ids)
    except Exception as e:
        logger.warning("[unreleased_x_post] X投稿の状態を読めず削除対象を集められなかった（非公開操作は続行）: %s", e)
        return []


# ──────────────────────────────────────────────
# 回収（承認時に投稿できなかった分）
# ──────────────────────────────────────────────

def find_backfill_candidates(sb, today: date | None = None) -> list[int]:
    """投稿条件を満たし、未投稿（行なし）か 再試行待ち（failed で attempts<上限 / rate_limited）のカードID（古い順）。

    approved_at が無いカード（導入前の承認）は含めない。さらに、発売済みカード名・同名で投稿済み・
    その他 is_postable が偽のものも選定の段階で除いておく（post_card で必ず落ちるカードが
    max_cards の枠を食い潰さないように）。
    """
    today = today or _today_jst()
    cards = _fetch_all(lambda: sb.table("unreleased_cards")
                       .select("id, name, release_date, status, hidden, approved_at")
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

    # 画像の有無は後で見るので、ここでは has_visible_image=True として他の条件だけを判定する
    pending_cards = [c for c in cards if retryable(c["id"]) and is_postable(c, True, today)[0]
                     and not _is_released_name(c.get("name") or "")]
    if not pending_cards:
        return []
    posted_keys = _posted_name_keys(sb)
    pending = [c["id"] for c in pending_cards if fuzzy_key(c.get("name") or "") not in posted_keys]
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
    """STUCK_AFTER_SEC 以上前に更新されたきり posting / unknown の行を Discord に通知する。通知した件数を返す。

    自動で再投稿はしない（投稿済みかもしれないので、人が X を見て posted にするか行を消す）。
    同じ行の通知は STUCK_RENOTIFY_SEC（1日）に1回まで（回収は3時間ごとに動くため、放置しても通知が溢れない）。
    最終通知時刻は行の stuck_notified_at に記録する。
    """
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=STUCK_AFTER_SEC)).isoformat()
    rows = _fetch_all(lambda: sb.table(TABLE).select("card_id, status, updated_at, last_error, stuck_notified_at")
                      .in_("status", ["posting", "unknown"]).lt("updated_at", cutoff).order("card_id"))
    due = []
    for r in rows:
        last = _parse_ts(r.get("stuck_notified_at"))
        if last is None or (now - last).total_seconds() >= STUCK_RENOTIFY_SEC:
            due.append(r)
    if not due:
        return 0
    lines = [f"card_id={r['card_id']} {r['status']} (最終更新 {str(r.get('updated_at'))[:16]})" for r in due[:20]]
    more = f" ほか{len(due) - 20}件" if len(due) > 20 else ""
    _notify("[未発売X投稿] 1時間以上 posting / unknown のままの行があります（自動再投稿はしません）。"
            + UNKNOWN_GUIDE + ":\n" + "\n".join(lines) + more)
    stamp = now.isoformat()
    for r in due:
        try:
            sb.table(TABLE).update({"stuck_notified_at": stamp}).eq("card_id", r["card_id"]).execute()
        except Exception as e:
            logger.warning("[unreleased_x_post] 通知時刻の記録に失敗 card_id=%s: %s", r["card_id"], e)
    return len(due)


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
