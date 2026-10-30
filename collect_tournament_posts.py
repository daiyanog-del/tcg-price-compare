"""collect_tournament_posts.py — 店舗・CS の大会結果ポストを X から集めて tournament_posts に保存する

採用急増の型（入賞が増えているのに値段がまだ動いていないテーマ）と、使用率分布の速報の材料。
まだ投稿には使わない（集めるだけ）。経緯と計測の数字は docs/decisions.md 2026-10-30。

集め方（X API v2 の tweets/search/recent。直近7日まで・読んだポスト数で課金）:
  - shop: tournament_accounts.json の店のアカウント（TCG PORTAL の出典）から、
          優勝などの語と「遊戯王/OCG/ランキングデュエル」を含むもの
  - cs:   CS 主催者のアカウントから、優勝・結果・分布などの語を含むもの（主催者は「遊戯王」と書かないことが多い）
  - cs_search: 主催者の一覧に無い CS を見つけるための語の検索（見つけた投稿者を一覧に足す候補にする）
  続きは種類ごとの since_id（app_settings の TOURNAMENT_POSTS_SINCE_ID）から読む。

料金の目安（2026-10-30 の計測）: shop 約90件/日・cs と cs_search で約20件/日 → 約 $0.55/日。
1回で読む上限を MAX_READS_PER_RUN で抑え、上限に達した種類は since_id を進めない（次回に読み直す）。

使い方:
  python collect_tournament_posts.py            # 集めて保存
  python collect_tournament_posts.py --dry-run  # 集めて表示だけ（X の料金はかかる）
  python collect_tournament_posts.py --reparse  # 保存済みの本文を今の抜き出し方で読み直す（X は読まない）
  python collect_tournament_posts.py --candidates  # CS 主催者の一覧に足す候補を出す（X は読まない）
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from adoption_parse import PARSER_VERSION, parse_post
from constants import JST

logger = logging.getLogger(__name__)

X_API_BASE = "https://api.twitter.com/2"
ACCOUNTS_PATH = Path(__file__).with_name("tournament_accounts.json")
_SINCE_ID_KEY = "TOURNAMENT_POSTS_SINCE_ID"

# 1回の実行で読むポストの上限（$0.005/件 → 600件で $3）。普段は約110件/日
MAX_READS_PER_RUN = 600  # TODO: calibrate from data
# since_id が無い初回に遡る時間
INITIAL_LOOKBACK_HOURS = 36
# X の検索式の長さの上限（512字）より少し短く区切る
QUERY_MAX_LEN = 480
REQUEST_INTERVAL_SEC = 1.0
# search/recent が遡れる時間（7日）より少し短く
SEARCH_WINDOW_HOURS = 160

_SHOP_KW = "(優勝 OR 入賞 OR 準優勝 OR ベスト4) (遊戯王 OR OCG OR ランキングデュエル) -RD -ラッシュ -is:retweet"
_CS_KW = "(優勝 OR 入賞 OR 準優勝 OR 結果 OR 分布) -is:retweet"
# 主催者の一覧に無い CS を見つけるための検索。まとめの転載（スターライト速報の転載2つ）は除く
_CS_SEARCH_QUERY = ("(遊戯王 OR OCG) CS (優勝 OR 分布 OR 結果) -is:retweet "
                    "-from:maxut5 -from:yugioh_antenna -from:tuitekoreruka")

# 取り込む順。同じポストが複数の検索に当たったら後の種類が勝つ（アカウントの一覧を語の検索より優先）
KINDS = ("cs_search", "shop", "cs")


# ──────────────────────────────────────────────
# 検索式
# ──────────────────────────────────────────────

def load_accounts(path: Path = ACCOUNTS_PATH) -> dict[str, list[str]]:
    """exclude の アカウント（まとめ直しの投稿で二重に数えるもの）は一覧に入っていても読まない。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    exclude = {a.lower() for a in data.get("exclude", {})}
    return {k: [a.lower() for a in data.get(k, []) if a.lower() not in exclude] for k in ("shop", "cs")}


def build_queries(accounts: list[str], keywords: str, max_len: int = QUERY_MAX_LEN) -> list[str]:
    """アカウントを「(from:a OR from:b ...) 語」に詰め、max_len 字以内で区切る。"""
    queries: list[str] = []
    cur: list[str] = []

    def q(names: list[str]) -> str:
        return "(" + " OR ".join("from:" + n for n in names) + ") " + keywords

    for name in accounts:
        if cur and len(q(cur + [name])) > max_len:
            queries.append(q(cur))
            cur = []
        cur.append(name)
    if cur:
        queries.append(q(cur))
    return queries


def queries_by_kind(accounts: dict[str, list[str]]) -> dict[str, list[str]]:
    return {
        "cs_search": [_CS_SEARCH_QUERY],
        "shop": build_queries(accounts["shop"], _SHOP_KW),
        "cs": build_queries(accounts["cs"], _CS_KW),
    }


# ──────────────────────────────────────────────
# 本文から大会の日付
# ──────────────────────────────────────────────

_EVENT_DATE = re.compile(r"(\d{1,2})\s*[/月]\s*(\d{1,2})\s*日?\s*(?:\([^)]{1,3}\))?\s*(?:開催|に開催|の)?")


def parse_event_date(text: str, posted_jst: date) -> date:
    """「10/25開催」「10月25日開催の」を大会の日付とする。無い・先の日付なら投稿日。"""
    t = unicodedata.normalize("NFKC", text or "")
    t = re.sub(r"[️⃣]", "", t)  # 「1⃣0⃣月2⃣9⃣日」
    t = re.sub(r"https?://\S+", " ", t)
    for m in _EVENT_DATE.finditer(t[:200]):
        if "開催" not in t[m.start():m.end() + 8] and "日" not in m.group(0):
            continue
        month, day = int(m.group(1)), int(m.group(2))
        for year in (posted_jst.year, posted_jst.year - 1):
            try:
                d = date(year, month, day)
            except ValueError:
                break
            if d <= posted_jst and (posted_jst - d).days <= 31:
                return d
    return posted_jst


# ──────────────────────────────────────────────
# X API
# ──────────────────────────────────────────────

def get_bearer_token() -> str:
    """X_BEARER_TOKEN が無ければ、X_API_KEY/SECRET から app-only の token を得る。"""
    token = os.environ.get("X_BEARER_TOKEN", "")
    if token:
        return token
    resp = requests.post(
        "https://api.twitter.com/oauth2/token",
        auth=(os.environ["X_API_KEY"], os.environ["X_API_SECRET"]),
        data={"grant_type": "client_credentials"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def snowflake_time(tweet_id: str) -> datetime:
    """X の ID に入っている投稿時刻。"""
    return datetime.fromtimestamp(((int(tweet_id) >> 22) + 1288834974657) / 1000, tz=timezone.utc)


class ReadBudget:
    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0

    @property
    def left(self) -> int:
        return self.limit - self.used


def search_recent(token: str, query: str, budget: ReadBudget, *,
                  since_id: str | None, start_time: str | None) -> tuple[list[dict], bool]:
    """1つの検索式を全ページ読む。戻り値: (ポスト, 最後まで読めたか)。

    ポストは {"id","text","created_at","username"}。上限に達したら途中で止める。
    """
    out: list[dict] = []
    next_token = None
    while True:
        if budget.left < 10:
            return out, False
        params = {
            "query": query,
            "max_results": max(10, min(100, budget.left)),
            "tweet.fields": "created_at,author_id",
            "expansions": "author_id",
            "user.fields": "username",
        }
        if since_id:
            params["since_id"] = since_id
        elif start_time:
            params["start_time"] = start_time
        if next_token:
            params["next_token"] = next_token
        resp = requests.get(f"{X_API_BASE}/tweets/search/recent",
                            headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        users = {u["id"]: u["username"] for u in (data.get("includes") or {}).get("users", [])}
        tweets = data.get("data") or []
        budget.used += len(tweets)
        for t in tweets:
            out.append({
                "id": t["id"],
                "text": t.get("text") or "",
                "created_at": t["created_at"],
                "username": users.get(t.get("author_id"), "").lower(),
            })
        next_token = (data.get("meta") or {}).get("next_token")
        time.sleep(REQUEST_INTERVAL_SEC)
        if not next_token:
            return out, True


# ──────────────────────────────────────────────
# 行の組み立て
# ──────────────────────────────────────────────

def build_row(tweet: dict, kind: str) -> dict:
    posted = datetime.fromisoformat(tweet["created_at"].replace("Z", "+00:00"))
    posted_jst = posted.astimezone(JST).date()
    parsed = parse_post(tweet["text"], assume_ocg=(kind == "cs"))
    return {
        "tweet_id": tweet["id"],
        "author": tweet["username"] or "unknown",
        "source_kind": kind,
        "posted_at": posted.isoformat(),
        "event_date": parse_event_date(tweet["text"], posted_jst).isoformat(),
        "text": tweet["text"],
        **parsed,
    }


def collect(token: str, accounts: dict[str, list[str]], since_ids: dict[str, str],
            *, max_reads: int = MAX_READS_PER_RUN, lookback_hours: int | None = None,
            now: datetime | None = None) -> tuple[dict[str, dict], dict[str, str], ReadBudget, list[str]]:
    """全種類を読む。戻り値: (tweet_id→行, 進めてよい since_id, 読んだ数, 読み切れなかった種類)。"""
    now = now or datetime.now(timezone.utc)
    budget = ReadBudget(max_reads)
    rows: dict[str, dict] = {}
    new_since: dict[str, str] = {}
    incomplete: list[str] = []
    for kind, queries in queries_by_kind(accounts).items():
        since_id = None if lookback_hours else since_ids.get(kind)
        # 検索は直近7日まで。止まっていた間に since_id が古くなったら時刻で読む（間の分は取れない）
        if since_id and now - snowflake_time(since_id) > timedelta(days=6, hours=12):
            logger.warning(f"{kind}: since_id が7日近く前なので直近 {SEARCH_WINDOW_HOURS} 時間を読む")
            since_id, lookback = None, SEARCH_WINDOW_HOURS
        else:
            lookback = lookback_hours
        start_time = None
        if not since_id:
            hours = lookback or INITIAL_LOOKBACK_HOURS
            start_time = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        max_id = since_ids.get(kind)
        complete = True
        for q in queries:
            tweets, done = search_recent(token, q, budget, since_id=since_id, start_time=start_time)
            complete = complete and done
            for t in tweets:
                rows[t["id"]] = build_row(t, kind)
                if max_id is None or int(t["id"]) > int(max_id):
                    max_id = t["id"]
            if not done:
                break
        if complete:
            if max_id:
                new_since[kind] = max_id
        else:
            incomplete.append(kind)
            logger.warning(f"{kind}: 読む上限 {max_reads} 件に達したので since_id を進めない（次回に読み直す）")
    return rows, new_since, budget, incomplete


# ──────────────────────────────────────────────
# DB
# ──────────────────────────────────────────────

def load_since_ids(sb) -> dict[str, str]:
    resp = sb.table("app_settings").select("value").eq("key", _SINCE_ID_KEY).execute()
    if resp.data:
        return dict(resp.data[0].get("value") or {})
    return {}


def save_since_ids(sb, since_ids: dict[str, str]) -> None:
    sb.table("app_settings").upsert({"key": _SINCE_ID_KEY, "value": since_ids}, on_conflict="key").execute()


def save_rows(sb, rows: list[dict], chunk: int = 200) -> None:
    for i in range(0, len(rows), chunk):
        sb.table("tournament_posts").upsert(rows[i:i + chunk], on_conflict="tweet_id").execute()


def reparse_all(sb, page: int = 500) -> int:
    """保存済みの行を今の抜き出し方で読み直す（parser_version が古いものだけ）。"""
    done = 0
    while True:
        resp = (sb.table("tournament_posts")
                .select("tweet_id,author,source_kind,posted_at,text")
                .lt("parser_version", PARSER_VERSION).limit(page).execute())
        batch = resp.data or []
        if not batch:
            return done
        rows = [build_row({"id": r["tweet_id"], "text": r["text"], "created_at": r["posted_at"],
                           "username": r["author"]}, r["source_kind"]) for r in batch]
        save_rows(sb, rows)
        done += len(rows)


def cs_account_candidates(sb, accounts: dict[str, list[str]], days: int = 30) -> list[tuple[str, int]]:
    """語の検索（cs_search）で見つけた OCG の投稿の投稿者のうち、一覧に無いもの（件数の多い順）。
    tournament_accounts.json の cs に足す候補。X は読まない。"""
    since = (datetime.now(JST) - timedelta(days=days)).date().isoformat()
    resp = (sb.table("tournament_posts").select("author")
            .eq("source_kind", "cs_search").eq("is_ocg", True).gte("event_date", since).execute())
    known = set(accounts["shop"]) | set(accounts["cs"])
    counts: dict[str, int] = {}
    for r in resp.data or []:
        if r["author"] not in known:
            counts[r["author"]] = counts.get(r["author"], 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])


def summarize(rows: list[dict]) -> str:
    ocg = [r for r in rows if r["is_ocg"]]
    with_pl = [r for r in ocg if r["placements"]]
    by_kind: dict[str, int] = {}
    for r in ocg:
        by_kind[r["source_kind"]] = by_kind.get(r["source_kind"], 0) + 1
    return (f"読んだ {len(rows)}件 / OCG {len(ocg)}件 {by_kind} / テーマが取れた {len(with_pl)}件"
            f" / 分布あり {sum(1 for r in ocg if r['distribution'])}件")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="保存せず表示だけ")
    ap.add_argument("--reparse", action="store_true", help="保存済みの本文を読み直す（X は読まない）")
    ap.add_argument("--candidates", action="store_true",
                    help="CS 主催者の一覧に足す候補（語の検索で見つけた投稿者）を出す（X は読まない）")
    ap.add_argument("--max-reads", type=int, default=MAX_READS_PER_RUN)
    ap.add_argument("--dump", default=None, help="--dry-run のとき、読んだ行を JSON に書き出す")
    ap.add_argument("--lookback-hours", type=int, default=None,
                    help="since_id を使わずこの時間だけ遡る（最大7日=168）")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    sb = None
    if not args.dry_run or args.reparse or args.candidates:
        from supabase import create_client
        sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    if args.reparse:
        print(f"読み直した {reparse_all(sb)}件")
        return 0
    if args.candidates:
        for name, n in cs_account_candidates(sb, load_accounts()):
            print(f"  {n:>3}件 @{name}")
        return 0

    since_ids = load_since_ids(sb) if sb else {}
    rows, new_since, budget, incomplete = collect(
        get_bearer_token(), load_accounts(), since_ids,
        max_reads=args.max_reads, lookback_hours=args.lookback_hours)
    row_list = sorted(rows.values(), key=lambda r: r["posted_at"])
    print(summarize(row_list) + f" / 課金の対象 {budget.used}件（約 ${budget.used * 0.005:.2f}）")
    if args.dry_run:
        if args.dump:
            Path(args.dump).write_text(json.dumps(row_list, ensure_ascii=False, indent=1), encoding="utf-8")
        for r in row_list:
            if r["is_ocg"]:
                pl = " ".join(f"{p['rank']}:{p['theme']}" for p in r["placements"])
                print(f"  [{r['source_kind']}] {r['event_date']} @{r['author']} 人数={r['participants']} {pl}")
        return 0
    save_rows(sb, row_list)
    if new_since:
        save_since_ids(sb, {**since_ids, **new_since})
    print(f"保存 {len(row_list)}件 / since_id を進めた種類 {sorted(new_since)}"
          + (f" / 読み切れなかった種類 {incomplete}" if incomplete else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
