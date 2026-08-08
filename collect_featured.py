"""
新弾フィーチャー価格収集スクリプト
====================================
Renderの Cron（JST 10:15、新弾発売日の朝に各店舗の新カード価格が出そろうタイミング）
から実行し、運用対象の新弾（featured_pack）に収録されたカードだけを収集する。

collect_prices.py（毎日5時の全件収集）とは別経路。新弾週以外は featured 対象が
無いため即座に終了する（cronは毎日走る前提）。

流儀は collect_prices.py を踏襲する:
  - Supabase接続 / 店舗ヘルスチェック（check_shop_availability, COLLECT_SKIP_SHOPS）
  - カード間 WAIT_BETWEEN_CARDS=1.0 秒待機
  - price_persist.build_min_price_rows + upsert_price_rows（デフォルトの
    last-write-wins。ignore_duplicates は使わない＝10時の値で朝5時の行を上書きする）

collect_prices.collect_and_save は「保存行数」のみを返し (card, shop) 単位の
失敗を返さないため、再試行に (card, shop) ペアの失敗集合が要る本スクリプトでは
collect_and_save_for_shops として最小限のロジックを複製している
（店舗フィルタして compare_prices を呼ぶ部分。出典: collect_prices.py の
collect_and_save、2026-08-08時点）。

collection_runs への実行記録について:
  collection_runs.run_type は CHECK制約で 'prices' / 'buyback' / 'instant' に
  限定されている（collection_runs.sql）。新弾収集はどれとも意味が異なり
  （'instant' はユーザー検索由来の即時反映用途）、本タスクはスキーマ変更をしない
  範囲のため、collection_runs への記録は行わない。実行区別は本スクリプトの
  ログ（"新弾フィーチャー価格収集" で検索可能）で行う。

失敗ペアの再試行について（2026-08-08 reviewer監査を受けて条件化）:
  第1パス完了後、店舗ごとの失敗率が BLOCKLIKE_FAIL_RATIO 以上の店舗を
  「ブロック様」（403連続などでホスト単位ブロックされている疑い）と判定する。
  ブロック様店舗の失敗ペアだけ RETRY_WAIT_SEC 秒（403サーキットブレーカTTL超）
  待って再試行し、それ以外の散発的な失敗ペアは待たずに即時再試行する。
  詳細は classify_blocklike_shops() / main() 内コメント参照。
"""

import argparse
import os
import sys
import time
from datetime import datetime

from constants import JST
from collect_prices import (
    get_supabase, check_shop_availability, SKIP_SHOPS_IN_CI, WAIT_BETWEEN_CARDS,
)
from scraper import compare_prices, SHOPS
from price_persist import build_min_price_rows, upsert_price_rows
from featured_pack import get_featured_pack, get_featured_cards

# 403サーキットブレーカのホスト単位TTL（scraper.py _HOST_BLOCK_TTL_SEC=1800秒）を
# 超えて待たないと、再試行時もブロックされたままになる。
RETRY_WAIT_SEC = 1900
MAX_RETRIES = 2

# 「対象カード数の何%が失敗したら店舗単位でブロックされている（ブロック様）とみなすか」
# の閾値。実データからの校正は未実施の暫定値。
# TODO: calibrate from data（collection_runs.shop_stats の実績が溜まり次第、
#       誤判定率と待機コストのトレードオフを見て再校正すること）
BLOCKLIKE_FAIL_RATIO = 0.30


def _is_shop_failed(status_entry: dict | None) -> bool:
    """(card, shop) の1回の取得試行が「取得失敗」かどうかを判定する純関数。

    compare_prices の status_out 形式 {"count": .., "fetch_errors": .., "exception": ..(任意)}
    を受け取り、「0件＝在庫なし」と「取得失敗（接続エラー・セレクタ破損等）」を区別する
    （collect_prices.collect_and_save の失敗判定ロジックと同じ基準）。

    4分岐:
      - status_entry が None（compare_prices が結果を書き込まなかった） → 失敗
      - exception あり → 失敗
      - fetch_errors > 0 かつ count == 0 → 失敗（取得エラーがあり結果も無い）
      - count > 0（fetch_errors の有無に関わらず） → 失敗ではない（一部エラーでも
        結果が得られていれば「取得できた」扱い。shop_stats 上は "ok"）
    """
    if status_entry is None:
        return True
    if status_entry.get("exception"):
        return True
    return status_entry.get("fetch_errors", 0) > 0 and status_entry.get("count", 0) == 0


def classify_blocklike_shops(failed_pairs: list[tuple[str, str]], attempted_per_shop: dict[str, int],
                              threshold: float = BLOCKLIKE_FAIL_RATIO) -> set[str]:
    """failed_pairs を店舗ごとに集計し、(失敗数 / その店舗の試行数) が threshold 以上の
    店舗を「ブロック様」（403連続などでホスト単位ブロックされている疑い）と判定する。

    ブロック様店舗の失敗ペアだけを長時間（403サーキットブレーカTTL超）待ってから
    再試行し、それ以外の散発的な失敗は待たずに即時再試行する（呼び出し元 main() 参照）。
    """
    fail_counts: dict[str, int] = {}
    for _card, shop in failed_pairs:
        fail_counts[shop] = fail_counts.get(shop, 0) + 1

    blocklike: set[str] = set()
    for shop, fails in fail_counts.items():
        attempted = attempted_per_shop.get(shop, 0)
        if attempted > 0 and (fails / attempted) >= threshold:
            blocklike.add(shop)
    return blocklike


def _group_pairs_by_card(pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    """(card, shop) ペアのリストを {card: [shop, ...]} へグルーピングする。"""
    by_card: dict[str, list[str]] = {}
    for card_name, shop in pairs:
        by_card.setdefault(card_name, []).append(shop)
    return by_card


def collect_and_save_for_shops(sb, card_name: str, today: str, shop_names: list[str],
                                shop_stats: dict | None = None) -> tuple[int, list[str]]:
    """1枚のカードについて、指定店舗のみ検索してSupabaseに保存する。

    collect_prices.collect_and_save と異なり、失敗した店舗名のリストも返す
    （本スクリプトは (card, shop) 単位の再試行が必要なため）。
    再試行呼び出し時は shop_stats に同じ店舗の集計が積み増しされる（再試行分を含む
    累積値になる。ラウンドごとの内訳が要る場合はログの「再試行 N/M」行を参照）。
    戻り値: (保存した行数, 失敗した店舗名のリスト)
    """
    status: dict = {}
    try:
        results = compare_prices(card_name, shop_names=shop_names, status_out=status)
    except Exception as e:
        print(f"  スクレイピング失敗 [{card_name}]: {e}")
        return 0, list(shop_names)

    failed_shops = []
    for shop in shop_names:
        st = status.get(shop)
        failed = _is_shop_failed(st)
        if failed:
            failed_shops.append(shop)
        if shop_stats is not None:
            agg = shop_stats.setdefault(shop, {"ok": 0, "empty": 0, "error": 0})
            if failed:
                agg["error"] += 1
            elif (st or {}).get("count", 0) == 0:
                agg["empty"] += 1
            else:
                agg["ok"] += 1

    if not results:
        if failed_shops:
            print(f"  取得失敗 [{card_name}]（{'、'.join(failed_shops)} — 在庫なしとは区別）")
        else:
            print(f"  結果なし [{card_name}]")
        return 0, failed_shops

    rows = build_min_price_rows(card_name, results, today)
    if not rows:
        print(f"  有効な価格なし [{card_name}]")
        return 0, failed_shops

    saved = upsert_price_rows(sb, rows)  # デフォルト ignore_duplicates=False（last-write-wins）
    if saved:
        print(f"  保存完了 [{card_name}]: {saved}件")
    else:
        print(f"  DB保存失敗 [{card_name}]")
    return saved, failed_shops


def _resolve_pack(sb, args) -> dict | None:
    """運用対象の新弾情報を返す。--pack/--wiki 指定時は featured 自動選択を上書きする
    （手動テスト用）。指定が無ければ get_featured_pack(sb) の自動判定に従う。
    """
    if args.pack:
        pack = {
            "pack_name": args.pack,
            "wiki_page": args.wiki or "",
            "tcg_name": "",
            "start_date": datetime.now(JST).date().isoformat(),
            "window_days": 7,
        }
        print(f"[featured] 手動指定パック: {pack['pack_name']} (wiki={pack['wiki_page']!r})")
        return pack
    return get_featured_pack(sb)


def main():
    parser = argparse.ArgumentParser(description="新弾フィーチャー価格収集（新弾収録カード限定）")
    parser.add_argument("--dry-run", action="store_true",
                         help="書き込みを行わず、収集対象カード数とサンプルのみ表示する")
    parser.add_argument("--limit", type=int, default=None,
                         help="先頭N枚のみ収集する")
    parser.add_argument("--pack", type=str, default=None,
                         help="featured自動選択を上書きする弾名（手動テスト用）")
    parser.add_argument("--wiki", type=str, default=None,
                         help="--pack 指定時のWikiページ名")
    args = parser.parse_args()

    started_at = datetime.now(JST)
    print(f"=== 新弾フィーチャー価格収集 {started_at.strftime('%Y-%m-%d %H:%M')} ===")

    sb = get_supabase()

    pack = _resolve_pack(sb, args)
    if not pack:
        print("[featured] 運用対象の新弾なし。終了します。")
        sys.exit(0)

    print(f"対象弾: {pack['pack_name']}")

    raw_cards = get_featured_cards(sb, pack)
    # 重複除去・順序維持
    seen: set[str] = set()
    card_names: list[str] = []
    for name in raw_cards:
        if name and name not in seen:
            seen.add(name)
            card_names.append(name)

    if not card_names:
        print("[featured] 収録カードを取得できませんでした。終了します。")
        sys.exit(0)

    if args.limit is not None:
        card_names = card_names[:args.limit]

    print(f"対象カード数: {len(card_names)}")

    # ── 店舗選定・ヘルスチェック（collect_prices.py と同じ仕組みを再利用） ──
    ci_shops = [name for name, _ in SHOPS if name not in SKIP_SHOPS_IN_CI]
    print("\n--- 店舗ヘルスチェック ---")
    available_shops = check_shop_availability(ci_shops)

    # カーナベルはAPIキー必須（collect_prices.py と同じガード）
    if "カーナベル" in available_shops and not (
        os.environ.get("KANABELL_CLOUD_ID") and os.environ.get("KANABELL_API_KEY")
    ):
        print("  NG: カーナベル — KANABELL_CLOUD_ID / KANABELL_API_KEY 未設定のため当日スキップ")
        available_shops.remove("カーナベル")

    if not available_shops:
        print("全店舗が応答しないため収集を中止します")
        sys.exit(1)

    print(f"収集対象店舗: {', '.join(available_shops)}")

    if args.dry_run:
        print(f"\n[dry-run] 書き込みは行いません。対象カード数={len(card_names)} / 対象店舗={available_shops}")
        print("[dry-run] 対象カード（先頭10件）:")
        for name in card_names[:10]:
            print(f"  - {name}")
        if len(card_names) > 10:
            print(f"  ... 他{len(card_names) - 10}枚")
        return

    today = datetime.now(JST).date().isoformat()
    total_saved = 0
    shop_stats: dict = {}
    failed_pairs: list[tuple[str, str]] = []  # (card_name, shop)

    for i, card_name in enumerate(card_names):
        print(f"[{i + 1}/{len(card_names)}] {card_name}")
        saved, failed_shops = collect_and_save_for_shops(
            sb, card_name, today, available_shops, shop_stats=shop_stats
        )
        total_saved += saved
        for shop in failed_shops:
            failed_pairs.append((card_name, shop))

        if i < len(card_names) - 1:
            time.sleep(WAIT_BETWEEN_CARDS)

    # ── 失敗ペアの再試行 ──
    # 店舗ごとに「対象カード数の BLOCKLIKE_FAIL_RATIO 以上が失敗」した店舗を
    # 「ブロック様」（403連続などでホスト単位ブロックされている疑い）と判定し、
    #   (b) ブロック様でない散発的な失敗ペアは待機なしで即時再試行
    #   (c) ブロック様店舗の失敗ペアだけ、403サーキットブレーカTTL(1800秒)を超える
    #       RETRY_WAIT_SEC秒（第1パス終了からの経過分を差し引いた残り）待って再試行
    # を最大 MAX_RETRIES ラウンド行う（司令塔裁定: 全店舗一律1900秒待機は過剰なため）。
    pass1_end = datetime.now(JST)
    attempted_per_shop = {shop: len(card_names) for shop in available_shops}

    retry_round = 0
    while failed_pairs and retry_round < MAX_RETRIES:
        retry_round += 1
        blocklike_shops = classify_blocklike_shops(failed_pairs, attempted_per_shop)
        non_blocklike_pairs = [(c, s) for c, s in failed_pairs if s not in blocklike_shops]
        blocklike_pairs = [(c, s) for c, s in failed_pairs if s in blocklike_shops]

        print(f"\n--- 再試行 {retry_round}/{MAX_RETRIES}（失敗ペア{len(failed_pairs)}件）---")
        if blocklike_shops:
            print(
                f"  ブロック様店舗: {'、'.join(sorted(blocklike_shops))}"
                f"（対象カードの{BLOCKLIKE_FAIL_RATIO:.0%}以上が失敗）"
            )

        next_failed_pairs: list[tuple[str, str]] = []

        # (b) 散発的な失敗（ブロック様でない）は待機なしで即時再試行
        if non_blocklike_pairs:
            print(f"  即時再試行（待機なし）: {len(non_blocklike_pairs)}ペア")
            by_card = _group_pairs_by_card(non_blocklike_pairs)
            for j, (card_name, shops) in enumerate(by_card.items()):
                print(f"    [{j + 1}/{len(by_card)}] {card_name} ({'、'.join(shops)})")
                saved, still_failed = collect_and_save_for_shops(
                    sb, card_name, today, shops, shop_stats=shop_stats
                )
                total_saved += saved
                next_failed_pairs.extend((card_name, s) for s in still_failed)
                time.sleep(WAIT_BETWEEN_CARDS)

        # (c) ブロック様店舗があるときだけ、サーキットブレーカTTLを超える時間まで待つ。
        # 第1パス終了からの経過秒を差し引く（上の即時再試行に費やした時間も含まれるため、
        # ラウンド2以降ではほぼ待たずに再試行できることが多い）
        if blocklike_pairs:
            elapsed = (datetime.now(JST) - pass1_end).total_seconds()
            wait_sec = max(0, RETRY_WAIT_SEC - elapsed)
            print(f"  ブロック様店舗の再試行: {wait_sec:.0f}秒待機後に{len(blocklike_pairs)}ペア")
            if wait_sec > 0:
                time.sleep(wait_sec)
            by_card = _group_pairs_by_card(blocklike_pairs)
            for j, (card_name, shops) in enumerate(by_card.items()):
                print(f"    [{j + 1}/{len(by_card)}] {card_name} ({'、'.join(shops)})")
                saved, still_failed = collect_and_save_for_shops(
                    sb, card_name, today, shops, shop_stats=shop_stats
                )
                total_saved += saved
                next_failed_pairs.extend((card_name, s) for s in still_failed)
                time.sleep(WAIT_BETWEEN_CARDS)

        failed_pairs = next_failed_pairs
        # 次ラウンドのブロック様判定は、直前ラウンドで実際に再試行したペア数を基準にする
        attempted_per_shop = {}
        for _c, s in (non_blocklike_pairs + blocklike_pairs):
            attempted_per_shop[s] = attempted_per_shop.get(s, 0) + 1

    if failed_pairs:
        print(f"\n取得失敗（再試行後も失敗、在庫なしとは区別）: {len(failed_pairs)}ペア")
        for card_name, shop in failed_pairs[:20]:
            print(f"  - {card_name} / {shop}")
        if len(failed_pairs) > 20:
            print(f"  ... 他{len(failed_pairs) - 20}ペア")

    finished_at = datetime.now(JST)
    elapsed_min = (finished_at - started_at).total_seconds() / 60
    print(f"\n=== 完了（{elapsed_min:.1f}分）===")
    print(
        f"対象カード数: {len(card_names)} / 保存行数: {total_saved} / "
        f"再試行ラウンド: {retry_round} / 最終失敗ペア: {len(failed_pairs)}"
    )
    if shop_stats:
        print("店舗別取得状況:")
        for shop, s in sorted(shop_stats.items()):
            print(f"  {shop}: 取得 {s['ok']} / 0件 {s['empty']} / エラー {s['error']}")


if __name__ == "__main__":
    main()
