"""汎用カードのリスト（2026-10-21〜）

TCG PORTAL の大会結果（レシピ付き）を直近 WINDOW_DAYS 日ぶん集め、
異なるテーマ MIN_THEMES 個以上のデッキで採用されているカードを「汎用カード」とする。
X の汎用カード値動き投稿（staple_post.py）の対象リストとして使う。

- 取得元: /api/yugioh/tournament-results?hasDeckData=true（SPA の裏の JSON API）と、
  各大会結果ページのレシピ表（meta_scraper.fetch_recipe_deck を流用）
- 保存先: Supabase の staple_cards テーブル（staple_cards.sql）。週1回の
  update-staple-cards.yml が作り直す。投稿側は読むだけ
- テーマごとの代表レシピ1件だけで数えると、そのテーマにしか代表レシピが無い汎用カード
  （例: 霆王の閃光）が漏れる（2026-10-21 ユーザー指摘）。そのため全レシピを数える

python staple_cards.py --update   … 作り直して保存
python staple_cards.py --dry-run  … 作り直して表示だけ（保存しない）
"""

import os
import sys
import time
import logging
from collections import defaultdict
from datetime import datetime, timedelta

import requests

from constants import JST
from meta_scraper import HEADERS, TCG_PORTAL_BASE, fetch_recipe_deck

logger = logging.getLogger(__name__)

# レシピ付きの入賞は直近30日で21件しかない（2026-10-21 実測。97件中）。
# 8/1〜（約80日）で112件・43テーマだったので90日にする。 TODO: calibrate from data
WINDOW_DAYS = 90
# 異なるテーマ数の下限。3にすると 8/1〜の112件で霆王の閃光（3テーマ）が入る。 TODO: calibrate from data
MIN_THEMES = 3
# TCG PORTAL への負担軽減（1ページごとの間隔）
REQUEST_INTERVAL_SEC = 1.0
# 1回の作り直しで読むレシピの上限（暴走ガード。90日で約120件の見込み）
MAX_RECIPES = 400
# テーマ名の揺れで混ざるテーマ専用カードを手で外す場合の一覧（今は空）
MANUAL_EXCLUDE: set[str] = set()


def _fetch_tournament_decks(date_from: str, date_to: str) -> list[dict]:
    """レシピ付きの大会結果を全ページ取得する。失敗時は例外をそのまま上げる
    （途中までのリストで保存すると対象が欠けるため）。"""
    items: list[dict] = []
    page = 1
    while True:
        url = (f"{TCG_PORTAL_BASE}/api/yugioh/tournament-results"
               f"?hasDeckData=true&from={date_from}&to={date_to}&page={page}&limit=20")
        res = requests.get(url, headers=HEADERS, timeout=30)
        res.raise_for_status()
        data = res.json()
        items.extend(data.get("tournamentDecks") or [])
        total_pages = int((data.get("pagination") or {}).get("totalPages") or 1)
        if page >= total_pages or len(items) >= MAX_RECIPES:
            break
        page += 1
        time.sleep(REQUEST_INTERVAL_SEC)
    return items[:MAX_RECIPES]


def _theme_of(item: dict) -> str:
    guide = item.get("deckGuide") or {}
    return (guide.get("name") or item.get("deckName") or "").strip()


def select_staples(recipes: list[dict], min_themes: int = MIN_THEMES) -> list[dict]:
    """レシピ一覧から汎用カードを選ぶ純関数（ネットワーク不要）。

    recipes: [{"theme": str, "cards": [{"name", "qty", "is_ex"}, ...]}, ...]
    名前にテーマ名を含むカード（例: レイズ・ムーンの天 シエロ－ノーモアベット）は、
    テーマ名の揺れで複数テーマに数えられたテーマ専用カードなので外す。
    戻り値: [{"card_name", "themes", "decks", "is_ex"}, ...]（テーマ数・デッキ数の降順）
    """
    theme_names = {r["theme"] for r in recipes if r.get("theme")}
    themes_of = defaultdict(set)
    decks_of = defaultdict(int)
    is_ex = {}
    for r in recipes:
        seen = set()
        for c in r.get("cards") or []:
            name = (c.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            themes_of[name].add(r.get("theme") or "")
            decks_of[name] += 1
            is_ex[name] = bool(c.get("is_ex"))
    rows = []
    for name, themes in themes_of.items():
        if len(themes) < min_themes or name in MANUAL_EXCLUDE:
            continue
        if any(t and t in name for t in theme_names):
            continue
        rows.append({"card_name": name, "themes": len(themes),
                     "decks": decks_of[name], "is_ex": is_ex[name]})
    rows.sort(key=lambda x: (-x["themes"], -x["decks"], x["card_name"]))
    return rows


def build_staple_list(window_days: int = WINDOW_DAYS) -> dict:
    """TCG PORTAL から作り直す。戻り値は保存・表示用の dict。"""
    today = datetime.now(JST).date()
    date_from = (today - timedelta(days=window_days)).isoformat()
    date_to = today.isoformat()
    items = _fetch_tournament_decks(date_from, date_to)
    recipes = []
    failed = 0
    for it in items:
        cards = fetch_recipe_deck(f"{TCG_PORTAL_BASE}/yugioh/tournament-results/{it['id']}")
        if cards:
            recipes.append({"theme": _theme_of(it), "cards": cards})
        else:
            failed += 1
        time.sleep(REQUEST_INTERVAL_SEC)
    return {
        "rows": select_staples(recipes),
        "sample_decks": len(recipes),
        "failed_recipes": failed,
        "window_from": date_from,
        "window_to": date_to,
    }


def save_staple_list(sb, result: dict) -> None:
    """staple_cards を作り直した内容で置き換える（新しい行を upsert → 古い行を削除）。"""
    rows = result["rows"]
    if not rows:
        # 取得が空なら前回のリストを残す（空で上書きすると投稿対象が消える）
        raise RuntimeError("汎用カードが0件のため保存しない（前回のリストを維持）")
    now = datetime.now(JST).isoformat()
    payload = [{
        **r,
        "sample_decks": result["sample_decks"],
        "window_from": result["window_from"],
        "window_to": result["window_to"],
        "updated_at": now,
    } for r in rows]
    sb.table("staple_cards").upsert(payload, on_conflict="card_name").execute()
    sb.table("staple_cards").delete().lt("updated_at", now).execute()


def load_staple_names(sb) -> set[str]:
    """投稿側が使う汎用カード名の集合。取得失敗時は空集合（＝投稿しない）。"""
    try:
        resp = sb.table("staple_cards").select("card_name").execute()
        return {r["card_name"] for r in (resp.data or [])}
    except Exception as e:
        logger.warning(f"staple_cards 取得失敗 — 汎用カード投稿をスキップ: {e}")
        return set()


if __name__ == "__main__":
    result = build_staple_list()
    print(f"期間 {result['window_from']}〜{result['window_to']} / レシピ {result['sample_decks']}件"
          f"（読めなかった {result['failed_recipes']}件）/ 汎用カード {len(result['rows'])}枚")
    for r in result["rows"]:
        print(f"  {r['themes']:>3}テーマ {r['decks']:>4}件 {'EX ' if r['is_ex'] else '   '}{r['card_name']}")
    if "--update" in sys.argv:
        from supabase import create_client
        _sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
        save_staple_list(_sb, result)
        print("staple_cards を更新しました")
