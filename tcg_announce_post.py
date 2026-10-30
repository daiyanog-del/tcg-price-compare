"""海外先行の予告カードの X 投稿（tools/tcg_announce_watch.py が1時間ごとの取り込みの後に呼ぶ）。

出す時機（2026-10-29 ユーザー合意「段階ごとに出す」・10-30「公式を基本に、販売店の速報は例外の分岐」）:
弾の段は2つ。公式の段＝KONAMI 公式の商品ページを出どころにしたカードか、公式の絵の入ったカードが1枚でもある /
速報の段＝投稿できるカードはあるが、出どころが販売店だけ（販売店の商品案内が公式より先に出た弾。MZMC は約17時間先。
過去に時刻を比べられた5弾では、販売店が先だったのは MZMC だけ）。
  1. 初報 set_first   … 弾を見つけて概要のあるカードが1枚以上できたら、すぐ（静かな時間帯は明けてから）。
                         速報の段では、弾を見つけてから LEAK_MAX_HOURS 以内に出せるときだけ「速報」として出す
                         （遅れたら速報の価値が薄いので出さずに公式を待つ）。出した段を phase に残す
  2. 公式の続報 set_official … 速報で初報を出した弾が公式の段に入ったら1回だけ（イラストが判明したことを知らせる）
  3. カードごと card  … 初報の翌日から、全体で1日1枚を CARD_HOURS の間に（弾が重なっても1日1枚）。公式の絵のある
                         カード → 表紙のカード → 関連カード（既存カードとのつながり）が分かるもの → 一覧の並び。
                         速報の段の弾は、絵の無いうちに出すと X のプレビューが「?」の札のまま残りうるので公式を待つ。
                         ただし初報から OFFICIAL_WAIT_DAYS 日たっても公式が出なければ、絵なしで出し始める。
                         数字の点検で警告が出たカードは出さない（ページには出る）
  4. 続報 set_update  … 判明（概要のあるカード）が前回の弾の投稿から UPDATE_MIN_NEW 枚以上増え、前回から
                         UPDATE_MIN_HOURS 時間以上たったら
  5. 全文判明 card_official … カードごとの投稿で出したカードに、英語のカードテキストが出たら
1回の実行で出すのは1件まで（一度に何件も流さない）。優先は 初報 → 公式の続報 → 全文判明 → 続報 → カードごと。

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
# 速報（販売店の商品案内）で初報を出すのは、弾を見つけてからこの時間以内に出せるときだけ。
# MZMC では公式が速報の約17時間後に出た。12時間を過ぎると、速報で先に出せる時間は残り数時間になる。TODO: calibrate from data
LEAK_MAX_HOURS = 12
# 速報の段の弾で、カードごとの投稿を公式の絵が出るまで待つ日数（初報から）。TODO: calibrate from data
OFFICIAL_WAIT_DAYS = 3
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


def _related_names(cards: list[dict], limit: int = 2) -> list[str]:
    names: list[str] = []
    for c in cards:
        for r in c.get("related_ja") or []:
            if r not in names:
                names.append(r)
    return names[:limit]


def text_set_first(s: dict, cards: list[dict]) -> str:
    rel = f"{tcg_announce_og.format_release_ja(s['tcg_release_date'])}発売予定。" if s.get("tcg_release_date") else ""
    related = _related_names(cards)
    return _fit([
        f"【海外先行】遊戯王TCG（英語版）の新パック「{s['name_en']}」",
        f"{rel}判明した{len(cards)}枚の効果を、日本語で紹介しています。",
        ("".join(f"《{r}》" for r in related) + "ゆかりの新カードも登場します。") if related else "",
    ], set_url(s))


def text_set_first_leak(s: dict, cards: list[dict]) -> str:
    """速報（販売店の商品案内で判明した段）の初報。イラストはまだ無いことも書く（公式の続報への前振り）。"""
    rel = f"{tcg_announce_og.format_release_ja(s['tcg_release_date'])}発売予定。" if s.get("tcg_release_date") else ""
    related = _related_names(cards)
    return _fit([
        f"【海外先行・速報】遊戯王TCG（英語版）の新パック「{s['name_en']}」",
        f"{rel}販売店の商品案内で、{len(cards)}枚の効果が判明しました。",
        ("".join(f"《{r}》" for r in related) + "ゆかりの新カードも登場します。") if related else "",
        "イラストは KONAMI 公式の発表待ちです。",
    ], set_url(s))


def text_set_official(s: dict, cards: list[dict]) -> str:
    """公式の続報（速報で初報を出した弾に、公式の商品ページが出た）。先頭に挙げるのは連載で最初に出すカード。"""
    head = sorted(cards, key=lambda c: card_order_key(c, s.get("cover_cards") or []))[0]["name_en"]
    return _fit([
        f"【海外先行・続報】「{s['name_en']}」の公式サイトが公開。",
        "収録カードのイラストが判明しました。",
        f"{head} など{len(cards)}枚を、イラストつきで紹介しています。",
    ], set_url(s))


def text_set_update(s: dict, cards: list[dict], new_cards: list[dict]) -> str:
    names = [c["name_en"] for c in new_cards[:2]]
    return _fit([
        f"【海外先行・続報】「{s['name_en']}」で判明したカードが{len(cards)}枚になりました。",
        ("新しく判明：" + "、".join(names) + (" ほか" if len(new_cards) > 2 else "")) if names else "",
    ], set_url(s))


def _fits(lines: list[str]) -> bool:
    from unreleased_x_post import weighted_length
    return weighted_length("\n".join(ln for ln in lines if ln)) + 1 + 23 <= MAX_WEIGHT


def text_card(s: dict, card: dict, cards: list[dict]) -> str:
    """カードごとの投稿。見出し（ゆかり・テーマ）とまとめページへ誘う一文はいちばんの引きなので残し、
    長すぎたら 種類の行 → 要約の末尾 の順に削る（2026-10-29 本番のデータで見出しから落ちていたのを直した）。"""
    # 画像の札と同じ言い方（公式の絵があれば「イラストと効果」）
    found = "の英語のテキストが判明" if card.get("lore_kind") != "blurb" else         ("のイラストと効果が判明" if card.get("art_url") else "の効果が判明")
    head = f"【海外先行】{card['name_en']} {found}"
    others = len(cards) - 1
    funnel = f"同じパックで判明した残り{others}枚も、まとめて紹介しています。" if others > 0 else ""
    stats = tcg_announce_og.stats_line(card)
    short = card.get("summary_short_ja") or ""
    # 見出し（ゆかりのカード・テーマ）。どれにも当たらないカードは出さない（弾の名前は下の行にある）
    related = tcg_announce_og.headline(card, s["name_en"]) if (card.get("related_ja") or card.get("theme_kind")) else ""
    url = card_url(s, card)
    for lines in ([head, related, stats, short, funnel], [head, related, short, funnel]):
        if _fits(lines):
            return "\n".join([ln for ln in lines if ln] + [url])
    from unreleased_x_post import weighted_length
    fixed = [x for x in (head, related, funnel) if x]
    room = MAX_WEIGHT - 23 - 1 - weighted_length("\n".join(fixed)) - 1
    return "\n".join(x for x in (head, related, _cut_short(short, max(room, 20)), funnel, url) if x)


def _cut_short(short: str, room: int) -> str:
    """要約を入る長さにする。文の切れ目（。）で収まるなら、そこまでで切って「…」を付けない。
    要約の指示は1文・45字以内なので、ここに来るのは長いカード名・見出しのときか、指示を守らなかった要約だけ。"""
    from unreleased_x_post import truncate_to_weight, weighted_length
    if weighted_length(short) <= room:
        return short
    head = ""
    for sentence in short.split("。"):
        if not sentence:
            break
        if weighted_length(head + sentence + "。") > room:
            break
        head += sentence + "。"
    return head or truncate_to_weight(short, room)


def text_card_official(s: dict, card: dict) -> str:
    return _fit([
        f"【海外先行】{card['name_en']} の英語のテキストが公開されました。",
        "日本語訳（機械訳）を載せています。",
    ], card_url(s, card))


# ──────────────────────────────────────────────
# 何を出すか（DB に触らない）
# ──────────────────────────────────────────────

def _jst_date(iso: str | None):
    if not iso:
        return None
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(JST).date()


def card_order_key(card: dict, covers=()):
    """カードごとの投稿の順: 公式の絵のあるカード → 表紙のカード（弾の目玉）→ 画像に既存カードの絵が出るカード
    （「あのカードの」と伝わる）→ それ以外。同じ中では一覧の並び。
    絵を先にするのは、X がリンクのプレビュー画像を最初に読んだもので持ち続けることがあり、絵の無いうちに出すと
    あとで絵が入っても「?」の札のまま残りうるため（2026-10-30）。"""
    covers = list(covers)
    cover_rank = covers.index(card.get("name_en")) if card.get("name_en") in covers else len(covers)
    return (not card.get("art_url"), cover_rank, not tcg_announce_og.picture_card(card), card.get("list_order") or 0)


def is_official_phase(cards: list[dict]) -> bool:
    """弾が公式の段か（公式の商品ページを出どころにしたカード・公式の絵の入ったカード・英語の正式なテキストが
    出たカードが1枚でもある）。"""
    return any(tcg_announce_og.from_official_page(c) or c.get("art_url") or c.get("lore_kind") == "official"
               for c in cards)


def _ts(iso) -> datetime | None:
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00")) if iso else None


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
        official = is_official_phase(cards)
        first = next((p for p in ps if p["kind"] == "set_first"), None)
        if first is None:
            if official:
                candidates.append((0, {"kind": "set_first", "phase": "official", "set": s, "cards": cards}))
            else:
                # 速報の古さは、効果の分かるカードを最初に取り込んだ時刻から数える（弾の名前だけ何週間も前から
                # 出ていることがあるので、弾を見つけた時刻からは数えない）
                found = min((t for t in (_ts(c.get("first_seen_at")) for c in cards) if t), default=None)
                if found is None or now - found <= timedelta(hours=LEAK_MAX_HOURS):
                    candidates.append((0, {"kind": "set_first", "phase": "leak", "set": s, "cards": cards}))
                # 速報が遅れた弾は初報を出さずに公式を待つ（公式の段に入れば上の分岐で出る）
            continue
        if (official and first.get("phase") == "leak"
                and not any(p["kind"] == "set_official" for p in ps)):
            candidates.append((1, {"kind": "set_official", "set": s, "cards": cards}))
        posted_cards = {p["page_id"] for p in ps if p["kind"] == "card"}
        # ページの作り直しで番号が変わった同じカードを二度出さない
        posted_names = {p.get("name_en") for p in ps if p["kind"] == "card" and p.get("name_en")}
        official_done = {p["page_id"] for p in ps if p["kind"] == "card_official"}
        for c in cards:
            if (c["page_id"] in posted_cards and c["page_id"] not in official_done
                    and c.get("lore_kind") == "official" and c.get("official_at")):
                # 連載で出した後に英語テキストが出たカード
                candidates.append((2, {"kind": "card_official", "set": s, "card": c, "cards": cards}))
                break
        set_posts = [p for p in ps if p["kind"] in ("set_first", "set_update", "set_official")]
        last = max(set_posts, key=lambda p: p.get("created_at") or "")
        last_count = max((p.get("known_count") or 0) for p in set_posts)
        last_at = datetime.fromisoformat(str(last["created_at"]).replace("Z", "+00:00"))
        if len(cards) - last_count >= UPDATE_MIN_NEW and now - last_at >= timedelta(hours=UPDATE_MIN_HOURS):
            known_before = {p["page_id"] for p in ps if p["kind"] == "card"}
            new_cards = sorted(cards, key=lambda c: c.get("first_seen_at") or "", reverse=True)[: len(cards) - last_count]
            new_cards = [c for c in new_cards if c["page_id"] not in known_before] or new_cards
            candidates.append((3, {"kind": "set_update", "set": s, "cards": cards, "new_cards": new_cards}))
        # カードごと: 初報の翌日から、時間帯の中で、その日まだ（どの弾でも）出していなければ。
        # 速報の段の弾は、公式の絵が出るまで待つ（初報から OFFICIAL_WAIT_DAYS 日たっても出なければ絵なしで出す）
        today = now_jst.date()
        waiting = not official and now - _ts(first.get("created_at")) < timedelta(days=OFFICIAL_WAIT_DAYS)
        if (not waiting and _jst_date(first.get("created_at")) < today and CARD_HOURS[0] <= now_jst.hour < CARD_HOURS[1]
                and not any(p["kind"] == "card" and _jst_date(p.get("created_at")) == today for p in posts)):
            rest = sorted((c for c in cards if c["page_id"] not in posted_cards and c["name_en"] not in posted_names),
                          key=lambda c: card_order_key(c, s.get("cover_cards") or []))
            if rest:
                candidates.append((4, {"kind": "card", "set": s, "card": rest[0], "cards": cards}))
    if not candidates:
        return None
    return min(candidates, key=lambda x: x[0])[1]


def build_text(item: dict) -> str:
    k = item["kind"]
    if k == "set_first":
        if item.get("phase") == "leak":
            return text_set_first_leak(item["set"], item["cards"])
        return text_set_first(item["set"], item["cards"])
    if k == "set_official":
        return text_set_official(item["set"], item["cards"])
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
           "known_count": len(item["cards"]) if item["kind"] in ("set_first", "set_update", "set_official") else None,
           "phase": item.get("phase", ""), "text": text, "status": "reserved"}
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
