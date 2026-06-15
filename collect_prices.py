"""
価格履歴データ収集スクリプト
============================
GitHub Actionsで毎日実行し、監視対象カードの価格をSupabaseに記録する。

収集方式:
  注目カード（新弾・現メタ主要・規制・検索ヒット）は毎日全件収集。
  周辺カード（テーマ関連・入賞レシピ等）は last_collected_at 古い順に
  DAILY_COLD_BUDGET 枚をローテーション収集。
  → 全カードをDBに載せつつ毎日の収集時間を予算内に収める。
"""

import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone, timedelta

from supabase import create_client, Client
from scraper import compare_prices, SHOPS
from pack_scraper import (
    get_pack_list, fetch_pack_cards, _try_wiki_page_variants,
    fetch_theme_cards, fetch_card_themes,
)
from meta_scraper import fetch_tier_list, fetch_deck_cards

# ── Supabase接続 ──
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

from constants import JST, RETENTION_DAYS

WAIT_BETWEEN_CARDS = 1.0

# 毎日の周辺カード収集予算（注目カードは予算外で全件収集）
DAILY_COLD_BUDGET = int(os.environ.get("DAILY_COLD_BUDGET", "800"))
# 収集ループの時間上限（秒）。超過で周辺カードを打ち切り、翌日再開
TIME_BUDGET_SEC = int(os.environ.get("TIME_BUDGET_SEC", str(300 * 60)))

# 収集から除外する店舗。実行環境ごとに環境変数 COLLECT_SKIP_SHOPS（カンマ区切り）で上書きできる。
#   - 未設定（GitHub Actions 等の従来環境）: データセンターIPから遅い/到達不能な店舗を従来どおり除外
#   - Render など店舗に到達できる環境: 例 COLLECT_SKIP_SHOPS="駿河屋"（Cloudflare対策のみ）/ ""（除外なし）
_DEFAULT_SKIP_SHOPS = {"遊々亭", "駿河屋", "カードラボ"}
_skip_env = os.environ.get("COLLECT_SKIP_SHOPS")
if _skip_env is None:
    SKIP_SHOPS_IN_CI = set(_DEFAULT_SKIP_SHOPS)
else:
    SKIP_SHOPS_IN_CI = {s.strip() for s in _skip_env.split(",") if s.strip()}

SHOP_HEALTH_URLS = {
    "カードラッシュ": "https://www.cardrush-yugioh.jp/",
    "トレコロCB": "https://torecolo.jp/",
    "カーナベル": "https://www.ka-nabell.com/",
    "まんぞく屋": "https://shopmanzokuya.com/",
}


def check_shop_availability(shop_names: list[str]) -> list[str]:
    """各店舗に接続確認し、応答した店舗名のリストを返す（失敗店舗は当日スキップ）"""
    import requests as _req
    headers = {"User-Agent": "Mozilla/5.0 (compatible; TCGYMBot/1.0)"}
    available = []
    for name in shop_names:
        url = SHOP_HEALTH_URLS.get(name)
        if not url:
            available.append(name)
            continue
        try:
            resp = _req.get(url, timeout=5, headers=headers, stream=True)
            resp.close()
            available.append(name)
            print(f"  OK: {name}")
        except Exception as e:
            print(f"  NG: {name} — 当日スキップ（{type(e).__name__}）")
    return available


def get_supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("エラー: SUPABASE_URL と SUPABASE_KEY を環境変数に設定してください")
        sys.exit(1)
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def fetch_tracked_cards(sb: Client) -> list[str]:
    """監視対象カードの名前一覧を取得（後方互換用）"""
    resp = sb.table("tracked_cards") \
        .select("card_name") \
        .eq("active", True) \
        .execute()
    cards = [row["card_name"] for row in resp.data]
    print(f"監視対象カード: {len(cards)}件")
    return cards


def fetch_tracked_cards_with_meta(sb: Client) -> list[dict]:
    """監視対象カードを last_collected_at つきで取得。列未追加時は名前のみ返す。"""
    try:
        resp = sb.table("tracked_cards") \
            .select("card_name, last_collected_at") \
            .eq("active", True) \
            .execute()
    except Exception:
        # last_collected_at カラム未追加時のフォールバック
        resp = sb.table("tracked_cards") \
            .select("card_name") \
            .eq("active", True) \
            .execute()
    cards = [
        {"card_name": row["card_name"], "last_collected_at": row.get("last_collected_at")}
        for row in resp.data
    ]
    print(f"監視対象カード: {len(cards)}件")
    return cards


def collect_and_save(sb: Client, card_name: str, today: str, shop_names: list[str],
                     shop_stats: dict | None = None) -> int:
    """1枚のカードの価格を取得してSupabaseに保存。保存した行数を返す。

    shop_stats に dict を渡すと、店舗ごとの 取得成功/0件/取得エラー を集計する。
    「0件＝在庫なし」と「取得失敗（接続エラー・セレクタ破損等）」を区別するため。
    """
    status: dict = {}
    try:
        results = compare_prices(card_name, shop_names=shop_names, status_out=status)
    except Exception as e:
        print(f"  スクレイピング失敗 [{card_name}]: {e}")
        return 0

    had_error = False
    for shop, st in status.items():
        failed = bool(st.get("exception")) or (st.get("fetch_errors", 0) > 0 and st.get("count", 0) == 0)
        if failed:
            had_error = True
        if shop_stats is not None:
            agg = shop_stats.setdefault(shop, {"ok": 0, "empty": 0, "error": 0})
            if failed:
                agg["error"] += 1
            elif st.get("count", 0) == 0:
                agg["empty"] += 1
            else:
                agg["ok"] += 1

    if not results:
        if had_error:
            print(f"  取得失敗 [{card_name}]（店舗側エラー — 在庫なしとは区別）")
        else:
            print(f"  結果なし [{card_name}]")
        return 0

    min_prices = {}
    for item in results:
        if item.get("sold_out"):
            continue
        price = item.get("price", 0)
        if price <= 10:
            continue
        key = (item.get("shop", ""), item.get("rarity", ""))
        if key not in min_prices or price < min_prices[key]:
            min_prices[key] = price

    if not min_prices:
        print(f"  有効な価格なし [{card_name}]")
        return 0

    rows = [
        {"card_name": card_name, "shop": shop, "rarity": rarity,
         "min_price": price, "recorded_at": today}
        for (shop, rarity), price in min_prices.items()
    ]

    try:
        sb.table("price_history").insert(rows).execute()
        print(f"  保存完了 [{card_name}]: {len(rows)}件")
        return len(rows)
    except Exception as e:
        print(f"  DB保存失敗 [{card_name}]: {e}")
        return 0


def normalize_card_name(name: str) -> str:
    """Wikiから取得したカード名を正規化"""
    name = name.replace('−', '-').replace('―', '-').replace('—', '-').replace('–', '-')
    result = []
    for ch in name:
        cp = ord(ch)
        if 0xFF21 <= cp <= 0xFF3A or 0xFF41 <= cp <= 0xFF5A or 0xFF10 <= cp <= 0xFF19:
            result.append(chr(cp - 0xFEE0))
        elif cp == 0xFF0E:
            result.append('.')
        elif cp == 0xFF01:
            result.append('!')
        else:
            result.append(ch)
    return ''.join(result)


def _insert_new_cards(sb: Client, new_cards: list[dict], label: str) -> None:
    """new_cards を tracked_cards に insert してログを出す"""
    if not new_cards:
        print(f"  新規カードなし")
        return
    try:
        sb.table("tracked_cards").insert(new_cards).execute()
        print(f"  新規追加: {len(new_cards)}枚")
        for c in new_cards[:10]:
            print(f"    + {c['card_name']}")
        if len(new_cards) > 10:
            print(f"    ... 他{len(new_cards) - 10}枚")
    except Exception as e:
        print(f"  カード追加失敗 ({label}): {e}")


# ── 注目カード同期（各関数は全該当カードの set[str] を返す） ──

def sync_latest_packs(sb: Client) -> tuple[set[str], set[str]]:
    """最新弾のカードを追加。(全最新弾カード集合, 今回新規追加分) を返す。"""
    print("\n--- 最新弾カード同期 ---")

    packs = get_pack_list()
    if not packs:
        print("  パック情報を取得できませんでした")
        return set(), set()

    resp = sb.table("tracked_cards").select("card_name").execute()
    existing = {row["card_name"] for row in resp.data}

    all_pack_cards: set[str] = set()
    new_cards = []
    seen_new: set[str] = set()

    for pack in packs:
        wiki_page = _try_wiki_page_variants(pack["name"])
        result = fetch_pack_cards(pack["name"], wiki_page=wiki_page)
        print(f"  {pack['name']}: {result['count']}枚")

        for card in result.get("cards", []):
            normalized = normalize_card_name(card)
            all_pack_cards.add(normalized)
            if normalized not in existing and normalized not in seen_new:
                seen_new.add(normalized)
                new_cards.append({"card_name": normalized, "active": True})

    _insert_new_cards(sb, new_cards, "最新弾")
    newly_added = {c["card_name"] for c in new_cards}
    return all_pack_cards, newly_added


def sync_meta_decks(sb: Client) -> set[str]:
    """環境デッキの主要カード（採用率30%以上）を追加。全該当カード集合を返す。"""
    print("\n--- 環境デッキカード同期 ---")

    tiers = fetch_tier_list(force=True)
    if not tiers:
        print("  Tier表を取得できませんでした")
        return set()

    resp = sb.table("tracked_cards").select("card_name").execute()
    existing = {row["card_name"] for row in resp.data}

    hot_meta: set[str] = set()
    new_cards = []
    seen_new: set[str] = set()
    target = [t for t in tiers if t.get("tier", 99) <= 4]
    print(f"  対象テーマ: {len(target)}件（Tier1〜4）")

    for theme in target:
        deck = fetch_deck_cards(theme["name"], force=True)
        for card in deck.get("cards", []):
            name = normalize_card_name(card["name"])
            if card.get("adoption", 0) >= 30.0:
                hot_meta.add(name)
                if name not in existing and name not in seen_new:
                    seen_new.add(name)
                    new_cards.append({"card_name": name, "active": True})

    _insert_new_cards(sb, new_cards, "環境デッキ")
    return hot_meta


def fetch_regulation_cards() -> list[str]:
    """KONAMI公式サイトからリミットレギュレーション対象カードを取得"""
    import requests
    from bs4 import BeautifulSoup

    url = "https://www.yugioh-card.com/japan/event/limitregulation/"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    try:
        res = requests.get(url, headers=headers, timeout=15)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
    except Exception as e:
        print(f"  リミットレギュレーション取得失敗: {e}")
        return []

    tables = soup.select("table.limitregulation")
    if len(tables) < 4:
        print(f"  テーブル数が想定と異なります: {len(tables)}")
        return []

    cards = []
    for table in tables[1:4]:
        for td in table.select("td"):
            text = td.get_text(strip=True)
            if not text or len(text) <= 2:
                continue
            if re.match(r'^[A-Z\s\-\'".,;:!?#&\d()/<>★☆＜＞]+$', text):
                continue
            if '⇒' in text or '新規' in text:
                continue
            cards.append(text)

    return cards


def sync_regulation(sb: Client) -> set[str]:
    """リミットレギュレーション対象カードを追加。全該当カード集合を返す。"""
    print("\n--- リミットレギュレーション同期 ---")

    cards = fetch_regulation_cards()
    if not cards:
        print("  カードを取得できませんでした")
        return set()

    print(f"  規制対象カード: {len(cards)}枚")

    resp = sb.table("tracked_cards").select("card_name").execute()
    existing = {row["card_name"] for row in resp.data}

    regulation_set: set[str] = set()
    new_cards = []
    seen_new: set[str] = set()

    for card in cards:
        name = normalize_card_name(card)
        regulation_set.add(name)
        if name not in existing and name not in seen_new:
            seen_new.add(name)
            new_cards.append({"card_name": name, "active": True})

    _insert_new_cards(sb, new_cards, "規制")
    return regulation_set


def sync_searched_cards(sb: Client, recent_days: int = 7, min_count: int = 2) -> set[str]:
    """検索ログ（単体・デッキ）から頻繁に検索されたカードを追加。全該当カード集合を返す。"""
    print(f"\n--- 検索ログからカード同期（{recent_days}日/{min_count}回以上）---")

    since = (datetime.now(JST).date() - timedelta(days=recent_days)).isoformat()

    # 単体検索ログ
    try:
        resp = sb.table("search_logs") \
            .select("card_name") \
            .gte("searched_at", since) \
            .execute()
    except Exception as e:
        print(f"  検索ログ取得失敗: {e}")
        return set()

    counts = Counter(row["card_name"] for row in (resp.data or []))

    # デッキ検索ログ（テーブル未作成時はスキップ）
    try:
        deck_resp = sb.table("deck_search_logs") \
            .select("card_name") \
            .gte("searched_at", since) \
            .execute()
        counts.update(row["card_name"] for row in (deck_resp.data or []))
        print(f"  デッキ検索ログ: {len(deck_resp.data or [])}件")
    except Exception as e:
        print(f"  デッキ検索ログ取得スキップ: {e}")

    if not counts:
        print("  検索ログなし")
        return set()

    qualifying = {name for name, cnt in counts.items() if cnt >= min_count}
    print(f"  直近{recent_days}日間: {len(counts)}種, うち{min_count}回以上: {len(qualifying)}種")

    if not qualifying:
        return set()

    resp = sb.table("tracked_cards").select("card_name").execute()
    existing = {row["card_name"] for row in resp.data}

    new_cards = []
    seen_new: set[str] = set()
    for name in qualifying:
        normalized = normalize_card_name(name)
        if normalized not in existing and normalized not in seen_new:
            seen_new.add(normalized)
            new_cards.append({"card_name": normalized, "active": True})

    _insert_new_cards(sb, new_cards, "検索ログ")
    return {normalize_card_name(n) for n in qualifying}


# ── 周辺カード同期（tracked_cards に追加するが注目集合には加えない） ──

def sync_recipe_decks(sb: Client) -> None:
    """入賞デッキレシピの全カードを監視対象に追加（周辺カード枠）"""
    print("\n--- 入賞レシピ全カード同期 ---")

    # sync_meta_decks が直前で force=True 実行済みのためキャッシュを流用（HTTP追加ゼロ）
    tiers = fetch_tier_list(force=False)
    if not tiers:
        print("  Tier表なし")
        return

    resp = sb.table("tracked_cards").select("card_name").execute()
    existing = {row["card_name"] for row in resp.data}

    target = [t for t in tiers if t.get("tier", 99) <= 4]
    new_cards = []
    seen_new: set[str] = set()

    for theme in target:
        deck = fetch_deck_cards(theme["name"], force=False)
        for c in deck.get("full_deck", []):
            name = normalize_card_name(c["name"])
            if name and name not in existing and name not in seen_new:
                seen_new.add(name)
                new_cards.append({"card_name": name, "active": True})

    _insert_new_cards(sb, new_cards, "入賞レシピ")


def sync_theme_wiki_cards(sb: Client) -> None:
    """環境テーマのWikiページから全関連カードを追加（旧カード含む、周辺カード枠）"""
    print("\n--- テーマWiki全カード同期 ---")

    tiers = fetch_tier_list(force=False)
    if not tiers:
        print("  Tier表なし")
        return

    resp = sb.table("tracked_cards").select("card_name").execute()
    existing = {row["card_name"] for row in resp.data}

    target = [t for t in tiers if t.get("tier", 99) <= 4]
    new_cards = []
    seen_new: set[str] = set()

    for theme in target:
        theme_name = theme["name"]
        time.sleep(0.5)
        wiki_cards = fetch_theme_cards(theme_name)
        print(f"  【{theme_name}】: {len(wiki_cards)}枚検出")
        for card in wiki_cards:
            name = normalize_card_name(card)
            if name and name not in existing and name not in seen_new:
                seen_new.add(name)
                new_cards.append({"card_name": name, "active": True})

    _insert_new_cards(sb, new_cards, "テーマWiki")


def sync_remake_themes(sb: Client, new_pack_cards: set[str]) -> None:
    """新弾カードの所属テーマから旧カード群を追加（リメイク高騰の先回り、周辺カード枠）"""
    if not new_pack_cards:
        return

    # 新規カードが多い場合は先頭30枚に絞る（Wiki呼び出し時間の抑制）
    targets = list(new_pack_cards)[:30]
    print(f"\n--- リメイクテーマ逆引き同期（新規{len(new_pack_cards)}枚中{len(targets)}枚対象）---")

    resp = sb.table("tracked_cards").select("card_name").execute()
    existing = {row["card_name"] for row in resp.data}

    # 新弾カードからテーマを逆引き
    theme_set: set[str] = set()
    for card_name in targets:
        time.sleep(0.5)
        themes = fetch_card_themes(card_name)
        theme_set.update(themes)

    if not theme_set:
        print("  テーマ検出なし")
        return

    print(f"  検出テーマ: {len(theme_set)}件")

    new_cards = []
    seen_new: set[str] = set()
    for theme in theme_set:
        time.sleep(0.5)
        theme_cards = fetch_theme_cards(theme)
        for name_raw in theme_cards:
            name = normalize_card_name(name_raw)
            if name and name not in existing and name not in seen_new:
                seen_new.add(name)
                new_cards.append({"card_name": name, "active": True})

    _insert_new_cards(sb, new_cards, "リメイク逆引き")


def sync_trending_cards(sb: Client) -> None:
    """外部値上がりランキングから未追跡カードを追加（ベストエフォート、周辺カード枠）"""
    print("\n--- 外部値上がりランキング同期 ---")

    try:
        from trending_scraper import fetch_trending_cards
        cards = fetch_trending_cards()
    except Exception as e:
        print(f"  取得失敗（スキップ）: {e}")
        return

    if not cards:
        print("  取得カードなし")
        return

    print(f"  ランキング取得: {len(cards)}枚")

    resp = sb.table("tracked_cards").select("card_name").execute()
    existing = {row["card_name"] for row in resp.data}

    new_cards = []
    seen_new: set[str] = set()
    for card in cards:
        name = normalize_card_name(card)
        if name and name not in existing and name not in seen_new:
            seen_new.add(name)
            new_cards.append({"card_name": name, "active": True})

    _insert_new_cards(sb, new_cards, "外部ランキング")


def save_pack_list(sb: Client):
    """パック一覧をSupabaseに保存（Webアプリのフォールバック用）"""
    print("\n--- パック一覧保存 ---")
    packs = get_pack_list()
    if not packs:
        print("  パック一覧が空のためスキップ")
        return

    try:
        sb.table("pack_list").delete().neq("id", 0).execute()
        rows = [{"name": p["name"], "wiki_page": p.get("wiki_page", ""),
                 "tcg_name": p.get("tcg_name", ""), "release_date": p.get("date", "")}
                for p in packs]
        sb.table("pack_list").insert(rows).execute()
        print(f"  保存完了: {len(rows)}件")
        for p in packs:
            print(f"    {p['name']}")
    except Exception as e:
        print(f"  パック一覧保存失敗: {e}")


def send_daily_report(
    sb: Client, today: str,
    success_count: int, fail_count: int,
    cutoff_count: int = 0, elapsed_min: float = 0.0,
    shop_stats: dict | None = None,
):
    """前日の利用状況をDiscordに日次レポートとして送信"""
    import requests as _req
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL未設定 — 日次レポートをスキップ")
        return

    print("\n=== 日次レポート送信 ===")

    yesterday = (datetime.now(JST).date() - timedelta(days=1)).isoformat()
    try:
        resp = sb.table("search_logs") \
            .select("card_name") \
            .gte("searched_at", yesterday) \
            .lt("searched_at", today) \
            .execute()
        logs = resp.data or []
    except Exception as e:
        print(f"検索ログ取得失敗: {e}")
        logs = []

    total = len(logs)
    counts = Counter(r["card_name"] for r in logs)
    unique = len(counts)
    top5 = counts.most_common(5)
    top5_lines = "\n".join(
        f"  {i+1}. {name}（{cnt}回）" for i, (name, cnt) in enumerate(top5)
    ) or "  （なし）"

    cutoff_line = f"打ち切り: {cutoff_count}枚（周辺カード予算超過）\n" if cutoff_count > 0 else ""

    # 店舗別取得状況。「全カードでエラー/0件」の店舗はセレクタ破損の疑いとして警告する
    shop_block = ""
    if shop_stats:
        rows = []
        suspect = []
        for shop, s in sorted(shop_stats.items()):
            attempted = s["ok"] + s["empty"] + s["error"]
            rows.append(f"  {shop}: 取得 {s['ok']} / 0件 {s['empty']} / エラー {s['error']}")
            if attempted >= 20 and s["ok"] == 0:
                suspect.append(shop)
        shop_block = "\n**店舗別取得状況**\n" + "\n".join(rows) + "\n"
        if suspect:
            shop_block += (
                f"警告: {'、'.join(suspect)} は全カードで取得0件 — "
                f"サイト構造変更（セレクタ破損）またはブロックの可能性\n"
            )

    message = (
        f"**TCGYM 日次レポート {today}**\n"
        f"\n"
        f"**前日の検索状況**\n"
        f"検索回数: {total}回 / カード種類: {unique}種\n"
        f"\n"
        f"**検索TOP5**\n"
        f"{top5_lines}\n"
        f"\n"
        f"**収集結果**\n"
        f"成功: {success_count}件 / 失敗: {fail_count}件 / 所要: {elapsed_min:.0f}分\n"
        f"{cutoff_line}"
        f"{shop_block}"
    )

    try:
        _req.post(webhook_url, json={"content": message}, timeout=10)
        print("日次レポート送信完了")
    except Exception as e:
        print(f"日次レポート送信失敗: {e}")


def cleanup_old_data(sb: Client):
    """保持期間を超えた古いデータを削除"""
    cutoff = (datetime.now(JST).date() - timedelta(days=RETENTION_DAYS)).isoformat()
    # 価格履歴の削除
    try:
        resp = sb.table("price_history") \
            .delete() \
            .lt("recorded_at", cutoff) \
            .execute()
        deleted = len(resp.data) if resp.data else 0
        if deleted > 0:
            print(f"古い価格履歴を削除: {deleted}件（{cutoff}より前）")
    except Exception as e:
        print(f"古い価格履歴削除失敗: {e}")
    # 一人回しリプレイの削除（90日超）
    try:
        resp = sb.table("solitaire_replays") \
            .delete() \
            .lt("created_at", cutoff) \
            .execute()
        deleted = len(resp.data) if resp.data else 0
        if deleted > 0:
            print(f"古いリプレイを削除: {deleted}件（{cutoff}より前）")
    except Exception as e:
        print(f"古いリプレイ削除失敗: {e}")


def main():
    started_at = datetime.now(JST)
    print(f"=== 価格履歴収集 {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    print(f"設定: 周辺予算={DAILY_COLD_BUDGET}枚 / 時間上限={TIME_BUDGET_SEC//60}分")

    sb = get_supabase()

    # ── 注目カードの sync ──
    hot: set[str] = set()
    pack_all, pack_new = sync_latest_packs(sb)
    hot |= pack_all
    hot |= sync_meta_decks(sb)
    hot |= sync_regulation(sb)
    hot |= sync_searched_cards(sb, recent_days=7, min_count=2)

    # ── 周辺カードの sync ──
    try:
        sync_recipe_decks(sb)
    except Exception as e:
        print(f"sync_recipe_decks失敗（スキップ）: {e}")
    try:
        sync_remake_themes(sb, pack_new)
    except Exception as e:
        print(f"sync_remake_themes失敗（スキップ）: {e}")
    try:
        sync_theme_wiki_cards(sb)
    except Exception as e:
        print(f"sync_theme_wiki_cards失敗（スキップ）: {e}")
    sync_searched_cards(sb, recent_days=30, min_count=1)
    try:
        sync_trending_cards(sb)
    except Exception as e:
        print(f"sync_trending_cards失敗（スキップ）: {e}")

    save_pack_list(sb)

    # ── 収集対象の選定 ──
    all_cards = fetch_tracked_cards_with_meta(sb)
    if not all_cards:
        print("監視対象カードが登録されていません")
        return

    hot_cards = [c for c in all_cards if c["card_name"] in hot]
    cold_cards = sorted(
        [c for c in all_cards if c["card_name"] not in hot],
        key=lambda c: c["last_collected_at"] or "",  # NULL="" で最古扱い
    )
    cold_selected = cold_cards[:DAILY_COLD_BUDGET]
    selected = hot_cards + cold_selected
    cold_name_set = {c["card_name"] for c in cold_selected}

    print(
        f"\n注目: {len(hot_cards)}件 / "
        f"周辺予算: {len(cold_selected)}/{len(cold_cards)}件 / "
        f"収集対象計: {len(selected)}件"
    )

    # ── 店舗ヘルスチェック ──
    ci_shops = [name for name, _ in SHOPS if name not in SKIP_SHOPS_IN_CI]
    print("\n--- 店舗ヘルスチェック ---")
    available_shops = check_shop_availability(ci_shops)

    # カーナベルはAPIキー必須。未設定のまま収集すると全カードが0件になり、
    # 「カーナベルに在庫なし」という誤ったデータがDBに蓄積されるため当日除外する
    if "カーナベル" in available_shops and not (
        os.environ.get("KANABELL_CLOUD_ID") and os.environ.get("KANABELL_API_KEY")
    ):
        print("  NG: カーナベル — KANABELL_CLOUD_ID / KANABELL_API_KEY 未設定のため当日スキップ")
        available_shops.remove("カーナベル")

    if not available_shops:
        print("全店舗が応答しないため収集を中止します")
        return

    today = datetime.now(JST).date().isoformat()
    total_saved = 0
    success_count = 0
    fail_count = 0
    cutoff_count = 0
    shop_stats: dict = {}  # 店舗別の 取得成功/0件/エラー 集計

    for i, card_data in enumerate(selected):
        card_name = card_data["card_name"]

        # 周辺カードのみ時間予算チェック（注目カードは予算外で必ず収集）
        if card_name in cold_name_set:
            elapsed = (datetime.now(JST) - started_at).total_seconds()
            if elapsed > TIME_BUDGET_SEC:
                cutoff_count = len(selected) - i
                print(f"時間予算超過（{elapsed/60:.0f}分）。周辺カード残り{cutoff_count}枚を打ち切り")
                break

        print(f"[{i+1}/{len(selected)}] {card_name}")
        saved = collect_and_save(sb, card_name, today, available_shops, shop_stats=shop_stats)
        if saved > 0:
            total_saved += saved
            success_count += 1
            # 収集完了を記録（途中タイムアウトでも進捗が保持されローテーションが正しく進む）
            try:
                sb.table("tracked_cards").update(
                    {"last_collected_at": datetime.now(JST).isoformat()}
                ).eq("card_name", card_name).execute()
            except Exception:
                pass  # スキーマ未移行時は無視
        else:
            fail_count += 1

        if i < len(selected) - 1:
            time.sleep(WAIT_BETWEEN_CARDS)

    cleanup_old_data(sb)

    elapsed_min = (datetime.now(JST) - started_at).total_seconds() / 60
    send_daily_report(sb, today, success_count, fail_count, cutoff_count, elapsed_min, shop_stats)

    print(f"\n=== 完了（{elapsed_min:.0f}分）===")
    print(f"成功: {success_count}件 / 失敗: {fail_count}件 / 打ち切り: {cutoff_count}件 / 保存行数: {total_saved}")
    if shop_stats:
        print("店舗別取得状況:")
        for shop, s in sorted(shop_stats.items()):
            print(f"  {shop}: 取得 {s['ok']} / 0件 {s['empty']} / エラー {s['error']}")


if __name__ == "__main__":
    main()
