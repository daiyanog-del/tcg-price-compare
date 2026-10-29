"""海外先行の予告カードの X 投稿（tools/tcg_announce_watch.py が1時間ごとの取り込みの後に呼ぶ）。

出す時機（2026-10-29 ユーザー合意「段階ごとに出す」）:
  1. 初報 set_first   … 弾を見つけて概要のあるカードが1枚以上できたら、すぐ（静かな時間帯は明けてから）
  2. カードごと card  … 初報の翌日から、全体で1日1枚を CARD_HOURS の間に（弾が重なっても1日1枚）。関連カード（既存カードとのつながり）が
                         分かるものを先に、あとは一覧の並び。数字の点検で警告が出たカードは出さない（ページには出る）
  3. 続報 set_update  … 判明（概要のあるカード）が前回の弾の投稿から UPDATE_MIN_NEW 枚以上増え、前回から
                         UPDATE_MIN_HOURS 時間以上たったら
  4. 全文判明 card_official … カードごとの投稿で出したカードに、英語のカードテキストが出たら
1回の実行で出すのは1件まで（一度に何件も流さない）。優先は 初報 → 全文判明 → 続報 → カードごと。

投稿は画像を添付せず、まとめページのリンクのプレビュー（OG 画像。tcg_announce_og）で見せる（未発売カードの投稿と同じ方式）。
リンクは本文に1本だけ（X は先頭の URL のプレビューを出す）。

有効化: TCG_ANNOUNCE_X_POST_ENABLED=1（既定は無効）。X_POST_DRY_RUN=1 なら送らずに本文だけ出す。
静かな時間帯は未発売カードの投稿と同じ（UNRELEASED_X_POST_QUIET_HOURS・既定 JST 1〜7時）。
"""
import logging
import os
from datetime import datetime, timedelta, timezone

import tcg_announce_og
import tcg_announce_view

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
ENABLED_ENV = "TCG_ANNOUNCE_X_POST_ENABLED"
POSTS = "tcg_announce_posts"
# カードごとの投稿を出す時間帯（JST の時。この間に1日1枚）。夜に見られやすいという一般論で置いた。TODO: calibrate from data
CARD_HOURS = (19, 23)
# 続報の条件。TODO: calibrate from data
UPDATE_MIN_NEW = 3
UPDATE_MIN_HOURS = 20
MAX_WEIGHT = 280
TWEET_LOG_PREFIX = "tcg_announce_"


def is_enabled() -> bool:
    return os.environ.get(ENABLED_ENV, "") == "1"


def site_url() -> str:
    from x_poster import SITE_URL
    return SITE_URL


def set_url(s: dict) -> str:
    return f"{site_url()}/tcg-preview/{s['slug']}"


def card_url(s: dict, card: dict) -> str:
    a = tcg_announce_view.anchor(card)
    # ?c= で OG 画像をそのカードのものに、#… で着いたときにそのカードの位置へ
    return f"{set_url(s)}?c={a}#{a}"


def postable_cards(cards: list[dict]) -> list[dict]:
    """投稿・判明枚数に数えるカード（概要ができていて、数字の点検に警告が無い）。"""
    return [c for c in cards if c.get("summary_status") == "done" and c.get("summary_short_ja")
            and not c.get("summary_warnings")]


# ──────────────────────────────────────────────
# 本文
# ──────────────────────────────────────────────

def _fit(lines: list[str], url: str) -> str:
    """URL（23字扱い）を最後に置き、長すぎたら後ろの行から落とす。"""
    from unreleased_x_post import weighted_length
    lines = [ln for ln in lines if ln]
    while lines:
        text = "\n".join(lines + [url])
        body = "\n".join(lines)
        if weighted_length(body) + 1 + 23 <= MAX_WEIGHT:
            return text
        lines = lines[:-1] if len(lines) > 1 else [lines[0][: max(len(lines[0]) - 10, 1)]]
    return url


def text_set_first(s: dict, cards: list[dict]) -> str:
    rel = f"（英語版 {tcg_announce_og.format_release(s['tcg_release_date'])} 発売予定）" if s.get("tcg_release_date") else ""
    names = [c["name_en"] for c in cards[:2]]
    return _fit([
        f"【海外先行・予告】遊戯王TCGの新弾「{s['name_en']}」{rel}",
        f"判明した{len(cards)}枚の効果の概要（非公式）を日本語でまとめました。",
        ("「" + "」「".join(names) + "」" + ("ほか" if len(cards) > 2 else "")) if names else "",
    ], set_url(s))


def text_set_update(s: dict, cards: list[dict], new_cards: list[dict]) -> str:
    names = [c["name_en"] for c in new_cards[:2]]
    return _fit([
        f"【海外先行・続報】「{s['name_en']}」の判明カードが{len(cards)}枚になりました。",
        ("新たに「" + "」「".join(names) + "」" + ("など" if len(new_cards) > 2 else "")) if names else "",
    ], set_url(s))


def _fits(lines: list[str]) -> bool:
    from unreleased_x_post import weighted_length
    return weighted_length("\n".join(ln for ln in lines if ln)) + 1 + 23 <= MAX_WEIGHT


def text_card(s: dict, card: dict, cards: list[dict]) -> str:
    """カードごとの投稿。まとめページへ誘う一文を残すため、長すぎたら 関連 → 種類の行 → 要約の末尾 の順に削る。"""
    from unreleased_x_post import truncate_to_weight
    head = f"【海外先行・{'効果判明' if card.get('lore_kind') == 'blurb' else '英語テキスト判明'}】{card['name_en']}"
    others = len(cards) - 1
    funnel = f"「{s['name_en']}」で判明した他の{others}枚もまとめています" if others > 0 else ""
    stats = tcg_announce_og.stats_line(card)
    short = card.get("summary_short_ja") or ""
    related = ("関連：" + "".join(f"《{r}》" for r in card.get("related_ja") or [])) if card.get("related_ja") else ""
    url = card_url(s, card)
    for lines in ([head, stats, short, related, funnel], [head, stats, short, funnel], [head, short, funnel]):
        if _fits(lines):
            return "\n".join([ln for ln in lines if ln] + [url])
    from unreleased_x_post import weighted_length
    room = MAX_WEIGHT - 23 - 1 - weighted_length("\n".join(x for x in (head, funnel) if x)) - 1
    return "\n".join(x for x in (head, truncate_to_weight(short, max(room, 20)), funnel, url) if x)


def text_card_official(s: dict, card: dict) -> str:
    return _fit([
        f"【海外先行・全文判明】{card['name_en']} の英語のカードテキストが出ました。",
        "日本語訳（機械訳）はこちら",
    ], card_url(s, card))


# ──────────────────────────────────────────────
# 何を出すか（DB に触らない）
# ──────────────────────────────────────────────

def _jst_date(iso: str | None):
    if not iso:
        return None
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(JST).date()


def card_order_key(card: dict):
    """カードごとの投稿の順: 関連カードが分かるもの（「あのカードの」と伝わる）を先に、あとは一覧の並び。"""
    return (not card.get("related_ja"), card.get("list_order") or 0)


def decide(sets: list[dict], cards_by_set: dict[str, list[dict]], posts: list[dict], now: datetime) -> dict | None:
    """次に出す1件を {kind, set, card?, cards, new_cards?} で返す。無ければ None。

    posts は tcg_announce_posts の行（status を問わず。unknown も出したものとして数え、出し直さない）。
    """
    now_jst = now.astimezone(JST)
    by_set: dict[str, list[dict]] = {}
    for p in posts:
        by_set.setdefault(p["set_prefix"], []).append(p)

    candidates = []
    for s in sets:
        cards = postable_cards(cards_by_set.get(s["prefix"], []))
        if not cards:
            continue
        ps = by_set.get(s["prefix"], [])
        first = next((p for p in ps if p["kind"] == "set_first"), None)
        if first is None:
            candidates.append((0, {"kind": "set_first", "set": s, "cards": cards}))
            continue
        posted_cards = {p["page_id"] for p in ps if p["kind"] == "card"}
        # ページの作り直しで番号が変わった同じカードを二度出さない
        posted_names = {p.get("name_en") for p in ps if p["kind"] == "card" and p.get("name_en")}
        official_done = {p["page_id"] for p in ps if p["kind"] == "card_official"}
        for c in cards:
            if (c["page_id"] in posted_cards and c["page_id"] not in official_done
                    and c.get("lore_kind") == "official" and c.get("official_at")):
                # 連載で出した後に英語テキストが出たカード
                candidates.append((1, {"kind": "card_official", "set": s, "card": c, "cards": cards}))
                break
        set_posts = [p for p in ps if p["kind"] in ("set_first", "set_update")]
        last = max(set_posts, key=lambda p: p.get("created_at") or "")
        last_count = max((p.get("known_count") or 0) for p in set_posts)
        last_at = datetime.fromisoformat(str(last["created_at"]).replace("Z", "+00:00"))
        if len(cards) - last_count >= UPDATE_MIN_NEW and now - last_at >= timedelta(hours=UPDATE_MIN_HOURS):
            known_before = {p["page_id"] for p in ps if p["kind"] == "card"}
            new_cards = sorted(cards, key=lambda c: c.get("first_seen_at") or "", reverse=True)[: len(cards) - last_count]
            new_cards = [c for c in new_cards if c["page_id"] not in known_before] or new_cards
            candidates.append((2, {"kind": "set_update", "set": s, "cards": cards, "new_cards": new_cards}))
        # カードごと: 初報の翌日から、時間帯の中で、その日まだ（どの弾でも）出していなければ
        today = now_jst.date()
        if (_jst_date(first.get("created_at")) < today and CARD_HOURS[0] <= now_jst.hour < CARD_HOURS[1]
                and not any(p["kind"] == "card" and _jst_date(p.get("created_at")) == today for p in posts)):
            rest = sorted((c for c in cards if c["page_id"] not in posted_cards and c["name_en"] not in posted_names),
                          key=card_order_key)
            if rest:
                candidates.append((3, {"kind": "card", "set": s, "card": rest[0], "cards": cards}))
    if not candidates:
        return None
    return min(candidates, key=lambda x: x[0])[1]


def build_text(item: dict) -> str:
    k = item["kind"]
    if k == "set_first":
        return text_set_first(item["set"], item["cards"])
    if k == "set_update":
        return text_set_update(item["set"], item["cards"], item["new_cards"])
    if k == "card":
        return text_card(item["set"], item["card"], item["cards"])
    return text_card_official(item["set"], item["card"])


# ──────────────────────────────────────────────
# 実行
# ──────────────────────────────────────────────

def _notify(msg: str) -> None:
    try:
        from discord_notify import send_discord_message
        send_discord_message(msg, no_mentions=True)
    except Exception as e:
        logger.warning(f"Discord 通知に失敗: {e}")


def load(sb) -> tuple[list[dict], dict[str, list[dict]], list[dict]]:
    sets = tcg_announce_view.fetch_all(sb.table("tcg_announce_sets").select("*").eq("hidden", False).eq("status", "active"))
    cards = tcg_announce_view.fetch_all(sb.table("tcg_announce_cards").select("*").eq("hidden", False)
                                        .eq("status", "announced").order("page_id"))
    by_set: dict[str, list[dict]] = {}
    for c in sorted(cards, key=lambda c: c.get("list_order") or 0):
        by_set.setdefault(c["set_prefix"], []).append(c)
    posts = tcg_announce_view.fetch_all(sb.table(POSTS).select("*").order("id"))
    return sets, by_set, posts


# 送信中のまま残った行を「止まった」とみなすまでの時間（GitHub の job の上限 25分より長く）
STALE_RESERVED_MIN = 30


def notify_stuck(sb, posts: list[dict], now: datetime) -> int:
    """結果不明・拒否・送信中のまま残った行を、1行につき1回だけ Discord に知らせる。"""
    n = 0
    for p in posts:
        if p.get("notified_at"):
            continue
        created = datetime.fromisoformat(str(p.get("created_at")).replace("Z", "+00:00"))
        stuck = p["status"] in ("unknown", "failed") or (
            p["status"] == "reserved" and now - created >= timedelta(minutes=STALE_RESERVED_MIN))
        if not stuck:
            continue
        _notify(f"海外先行の投稿（{p['kind']}・{p['set_prefix']}・id={p['id']}）が {p['status']} のままです"
                f"（{p.get('error') or ''}）。X で確かめ、出ていれば status='posted'、出ていなければ行を消してください")
        sb.table(POSTS).update({"notified_at": now.isoformat()}).eq("id", p["id"]).execute()
        n += 1
    return n


def run(sb, *, now: datetime | None = None, post_fn=None) -> str:
    """1件だけ判断して出す。戻り値は結果の短い説明（ログ用）。"""
    from unreleased_x_post import in_quiet_hours
    now = now or datetime.now(timezone.utc)
    if not is_enabled():
        return "無効（TCG_ANNOUNCE_X_POST_ENABLED が 1 ではない）"
    if in_quiet_hours(now):
        return "静かな時間帯のため見送り"
    sets, by_set, posts = load(sb)
    try:
        notify_stuck(sb, posts, now)
    except Exception as e:
        logger.warning(f"止まった投稿の通知に失敗: {e}")
    item = decide(sets, by_set, posts, now)
    if item is None:
        return "出すものなし"
    text = build_text(item)
    s, card = item["set"], item.get("card")
    if os.environ.get("X_POST_DRY_RUN", "") == "1":
        logger.info(f"[DRY_RUN] {item['kind']}:\n{text}")
        return f"[DRY_RUN] {item['kind']}"
    # 予約（一意の索引で二重投稿を止める）
    row = {"kind": item["kind"], "set_prefix": s["prefix"], "page_id": card["page_id"] if card else None,
           "name_en": card["name_en"] if card else None,
           "known_count": len(item["cards"]) if item["kind"] in ("set_first", "set_update") else None,
           "text": text, "status": "reserved"}
    try:
        res = sb.table(POSTS).insert(row).execute()
    except Exception as e:
        if "23505" in str(e) or "duplicate key" in str(e):
            return f"予約できず（同じ投稿が既にある）: {item['kind']}"
        raise
    pid = res.data[0]["id"]
    if post_fn is None:
        from x_poster import post_tweet_ex
        post_fn = post_tweet_ex
    tweet_id, err = post_fn(text)
    from x_poster import ERR_NOT_CONFIGURED, ERR_DRY_RUN, ERR_RATE_LIMITED, ERR_UNKNOWN
    if tweet_id:
        sb.table(POSTS).update({"status": "posted", "tweet_id": str(tweet_id),
                                "posted_at": datetime.now(timezone.utc).isoformat()}).eq("id", pid).execute()
        try:
            sb.table("tweet_log").insert({"tweet_id": str(tweet_id), "posted_at": datetime.now(JST).isoformat(),
                                          "content_type": TWEET_LOG_PREFIX + item["kind"],
                                          "parent_tweet_id": None}).execute()
        except Exception as e:
            logger.warning(f"tweet_log の記録に失敗（投稿は成功）: {e}")
        return f"投稿 {item['kind']} tweet_id={tweet_id}"
    if err == ERR_UNKNOWN:
        # 投稿されたかもしれない。行を残して出し直さない
        sb.table(POSTS).update({"status": "unknown", "error": err,
                                "notified_at": datetime.now(timezone.utc).isoformat()}).eq("id", pid).execute()
        _notify(f"海外先行の投稿の結果が不明です（{item['kind']}・{s['name_en']}）。X で確かめてください")
        return f"結果不明 {item['kind']}"
    if err in (ERR_NOT_CONFIGURED, ERR_DRY_RUN, ERR_RATE_LIMITED):
        # 送っていない（設定なし・試し・回数制限）→ 予約を消して次の回に出し直す
        sb.table(POSTS).delete().eq("id", pid).execute()
        return f"見送り {item['kind']}: {err}"
    # X に明確に拒否された（同じ本文の重複など）・送信前の失敗 → 出し直すと毎時同じ失敗で後ろが全部止まるので、
    # 行を failed で残して知らせる（直したら行を消せば次の回に出し直す）
    sb.table(POSTS).update({"status": "failed", "error": err,
                            "notified_at": datetime.now(timezone.utc).isoformat()}).eq("id", pid).execute()
    _notify(f"海外先行の投稿が X に受け付けられませんでした（{item['kind']}・{s['name_en']}・{err}）。"
            f"直したら tcg_announce_posts の id={pid} を消すと次の回に出し直します")
    return f"失敗 {item['kind']}: {err}"
