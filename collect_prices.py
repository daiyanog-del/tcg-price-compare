"""
価格履歴データ収集スクリプト
============================
GitHub Actionsで毎日実行し、監視対象カードの価格をSupabaseに記録する。
"""

import os
import re
import sys
import time
from datetime import datetime, date

from supabase import create_client, Client
from scraper import compare_prices, SHOPS
from pack_scraper import get_pack_list, fetch_pack_cards, _try_wiki_page_variants
from meta_scraper import fetch_tier_list, fetch_deck_cards

# ── Supabase接続 ──
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# カード間の待機秒数（店舗への負荷軽減）
WAIT_BETWEEN_CARDS = 2.0

# 古いデータの保持日数
RETENTION_DAYS = 90

# GitHub Actionsからアクセスできない店舗をスキップ
SKIP_SHOPS_IN_CI = {"遊々亭"}


def get_supabase() -> Client:
    """Supabaseクライアントを取得"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("エラー: SUPABASE_URL と SUPABASE_KEY を環境変数に設定してください")
        sys.exit(1)
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def fetch_tracked_cards(sb: Client) -> list[str]:
    """監視対象カードの一覧を取得"""
    resp = sb.table("tracked_cards") \
        .select("card_name") \
        .eq("active", True) \
        .execute()
    cards = [row["card_name"] for row in resp.data]
    print(f"監視対象カード: {len(cards)}件")
    return cards


def collect_and_save(sb: Client, card_name: str, today: str) -> int:
    """1枚のカードの価格を取得してSupabaseに保存。保存した行数を返す"""
    # GitHub Actions環境ではブロックされる店舗をスキップ
    available_shops = [name for name, _ in SHOPS if name not in SKIP_SHOPS_IN_CI]

    try:
        results = compare_prices(card_name, shop_names=available_shops)
    except Exception as e:
        print(f"  スクレイピング失敗 [{card_name}]: {e}")
        return 0

    if not results:
        print(f"  結果なし [{card_name}]")
        return 0

    # 店舗×レアリティごとの最安値を抽出
    min_prices = {}
    for item in results:
        if item.get("sold_out"):
            continue
        price = item.get("price", 0)
        if price <= 0:
            continue

        key = (item.get("shop", ""), item.get("rarity", ""))
        if key not in min_prices or price < min_prices[key]:
            min_prices[key] = price

    if not min_prices:
        print(f"  有効な価格なし [{card_name}]")
        return 0

    # Supabaseに保存
    rows = []
    for (shop, rarity), price in min_prices.items():
        rows.append({
            "card_name": card_name,
            "shop": shop,
            "rarity": rarity,
            "min_price": price,
            "recorded_at": today,
        })

    try:
        sb.table("price_history").insert(rows).execute()
        print(f"  保存完了 [{card_name}]: {len(rows)}件")
        return len(rows)
    except Exception as e:
        print(f"  DB保存失敗 [{card_name}]: {e}")
        return 0


def normalize_card_name(name: str) -> str:
    """Wikiから取得したカード名を正規化"""
    # 特殊ダッシュを通常ハイフンに統一
    name = name.replace('\u2212', '-').replace('\u2015', '-').replace('\u2014', '-').replace('\u2013', '-')
    # 全角英数字を半角に変換
    result = []
    for ch in name:
        cp = ord(ch)
        # 全角英数字（Ａ-Ｚ, ａ-ｚ, ０-９）→ 半角
        if 0xFF21 <= cp <= 0xFF3A or 0xFF41 <= cp <= 0xFF5A or 0xFF10 <= cp <= 0xFF19:
            result.append(chr(cp - 0xFEE0))
        # 全角記号の一部（．→., ！→!）
        elif cp == 0xFF0E:
            result.append('.')
        elif cp == 0xFF01:
            result.append('!')
        else:
            result.append(ch)
    return ''.join(result)


def sync_latest_packs(sb: Client):
    """最新弾のカードリストを取得し、tracked_cardsに未登録のカードを自動追加"""
    print("\n--- 最新弾カード同期 ---")

    packs = get_pack_list()
    if not packs:
        print("  パック情報を取得できませんでした")
        return

    # 現在の監視対象カードを取得
    resp = sb.table("tracked_cards").select("card_name").execute()
    existing = {row["card_name"] for row in resp.data}

    new_cards = []
    for pack in packs:
        wiki_page = _try_wiki_page_variants(pack["name"])
        result = fetch_pack_cards(pack["name"], wiki_page=wiki_page)
        print(f"  {pack['name']}: {result['count']}枚")

        for card in result.get("cards", []):
            normalized = normalize_card_name(card)
            if normalized not in existing and normalized not in [c["card_name"] for c in new_cards]:
                new_cards.append({"card_name": normalized, "active": True})

    if new_cards:
        try:
            sb.table("tracked_cards").insert(new_cards).execute()
            print(f"  新規追加: {len(new_cards)}枚")
            for c in new_cards[:10]:
                print(f"    + {c['card_name']}")
            if len(new_cards) > 10:
                print(f"    ... 他{len(new_cards) - 10}枚")
        except Exception as e:
            print(f"  カード追加失敗: {e}")
    else:
        print("  新規カードなし")


def sync_meta_decks(sb: Client):
    """環境デッキの主要カードを監視対象に自動追加"""
    print("\n--- 環境デッキカード同期 ---")

    tiers = fetch_tier_list(force=True)
    if not tiers:
        print("  Tier表を取得できませんでした")
        return

    # 現在の監視対象カードを取得
    resp = sb.table("tracked_cards").select("card_name").execute()
    existing = {row["card_name"] for row in resp.data}

    new_cards = []
    # Tier1〜3のデッキを対象
    target_themes = [t for t in tiers if t.get("tier", 99) <= 3]
    print(f"  対象テーマ: {len(target_themes)}件（Tier1〜3）")

    for theme in target_themes:
        deck = fetch_deck_cards(theme["name"], force=True)
        cards = deck.get("cards", [])
        # 採用率50%以上のカードを対象
        for card in cards:
            name = normalize_card_name(card["name"])
            if card.get("adoption", 0) >= 50.0 and name not in existing and name not in [c["card_name"] for c in new_cards]:
                new_cards.append({"card_name": name, "active": True})

    if new_cards:
        try:
            sb.table("tracked_cards").insert(new_cards).execute()
            print(f"  新規追加: {len(new_cards)}枚")
            for c in new_cards[:10]:
                print(f"    + {c['card_name']}")
            if len(new_cards) > 10:
                print(f"    ... 他{len(new_cards) - 10}枚")
        except Exception as e:
            print(f"  カード追加失敗: {e}")
    else:
        print("  新規カードなし")


def fetch_regulation_cards() -> list[str]:
    """KONAMI公式サイトからリミットレギュレーション対象カード（制限・準制限・解除）を取得"""
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
    # テーブル2=制限, テーブル3=準制限, テーブル4=解除（価格変動しやすい）
    for table in tables[1:4]:
        for td in table.select("td"):
            text = td.get_text(strip=True)
            # 英語名・ステータス変更テキスト・短すぎるものを除外
            if not text or len(text) <= 2:
                continue
            if re.match(r'^[A-Z\s\-\'".,;:!?#&\d()/<>★☆＜＞]+$', text):
                continue
            if '⇒' in text or '新規' in text:
                continue
            cards.append(text)

    return cards


def sync_regulation(sb: Client):
    """リミットレギュレーション対象カードを監視対象に自動追加"""
    print("\n--- リミットレギュレーション同期 ---")

    cards = fetch_regulation_cards()
    if not cards:
        print("  カードを取得できませんでした")
        return

    print(f"  規制対象カード: {len(cards)}枚")

    # 現在の監視対象カードを取得
    resp = sb.table("tracked_cards").select("card_name").execute()
    existing = {row["card_name"] for row in resp.data}

    new_cards = []
    for card in cards:
        name = normalize_card_name(card)
        if name not in existing and name not in [c["card_name"] for c in new_cards]:
            new_cards.append({"card_name": name, "active": True})

    if new_cards:
        try:
            sb.table("tracked_cards").insert(new_cards).execute()
            print(f"  新規追加: {len(new_cards)}枚")
            for c in new_cards[:10]:
                print(f"    + {c['card_name']}")
            if len(new_cards) > 10:
                print(f"    ... 他{len(new_cards) - 10}枚")
        except Exception as e:
            print(f"  カード追加失敗: {e}")
    else:
        print("  新規カードなし")


def cleanup_old_data(sb: Client):
    """保持期間を超えた古いデータを削除"""
    from datetime import timedelta
    cutoff = (date.today() - timedelta(days=RETENTION_DAYS)).isoformat()
    try:
        resp = sb.table("price_history") \
            .delete() \
            .lt("recorded_at", cutoff) \
            .execute()
        deleted = len(resp.data) if resp.data else 0
        if deleted > 0:
            print(f"古いデータを削除: {deleted}件（{cutoff}より前）")
    except Exception as e:
        print(f"古いデータ削除失敗: {e}")


def main():
    print(f"=== 価格履歴収集 {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")

    sb = get_supabase()

    # 監視対象カードを自動更新
    sync_latest_packs(sb)
    sync_meta_decks(sb)
    sync_regulation(sb)

    cards = fetch_tracked_cards(sb)

    if not cards:
        print("監視対象カードが登録されていません。Supabaseの tracked_cards テーブルにカードを追加してください。")
        return

    today = date.today().isoformat()
    total_saved = 0
    success_count = 0
    fail_count = 0

    for i, card_name in enumerate(cards):
        print(f"[{i+1}/{len(cards)}] {card_name}")
        saved = collect_and_save(sb, card_name, today)
        if saved > 0:
            total_saved += saved
            success_count += 1
        else:
            fail_count += 1

        # 最後のカード以外は待機
        if i < len(cards) - 1:
            time.sleep(WAIT_BETWEEN_CARDS)

    # 古いデータの削除
    cleanup_old_data(sb)

    print(f"\n=== 完了 ===")
    print(f"成功: {success_count}件 / 失敗: {fail_count}件 / 保存行数: {total_saved}")


if __name__ == "__main__":
    main()
