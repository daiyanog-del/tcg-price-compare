"""
買取価格履歴データ収集スクリプト
==================================
GitHub Actionsで毎日実行し、注目カードの買取価格をSupabaseに記録する。
collect_prices.py（販売価格）とは別スケジュール・別ワークフローで動作する。

収集方式:
  注目カード（新弾・現メタ主要・規制・検索ヒット）のみを毎日収集。
  周辺カードは対象外（買取で値動きが意味を持つのは人気カードのため）。
  店舗はカードラッシュ・カーナベルのみ（遊々亭はCIでIPブロックのためスキップ）。
"""

import os
import sys
import time
from datetime import datetime, timezone, timedelta

from supabase import create_client, Client
from scraper import compare_buyback, BUYBACK_SHOPS, get_host_request_counts
from collection_run import record_collection_run, attach_host_request_counts
from price_persist import upsert_buyback_rows
from art_variant import is_sealed
from db_write_guard import (
    ConsecutiveDbFailureGuard, DbWriteAborted, GuardedSupabase, is_db_unavailable, report_db_abort,
)
from collect_prices import (
    get_supabase,
    normalize_card_name,
    fetch_tracked_cards_with_meta,
    check_shop_availability,
    sync_latest_packs,
    sync_meta_decks,
    sync_regulation,
    sync_searched_cards,
)

# ── 定数 ──
from constants import JST, BUYBACK_RETENTION_DAYS

WAIT_BETWEEN_CARDS = 1.0  # 収集間隔（秒）。collect_prices.py に合わせる

# 買取収集から除外する店舗。実行環境ごとに環境変数 BUYBACK_SKIP_SHOPS（カンマ区切り）で上書きできる。
#   - 未設定（GitHub Actions 等の従来環境）: 遊々亭はデータセンターIPからブロックされるため従来どおり除外
#   - Render など店舗に到達できる環境: 例 BUYBACK_SKIP_SHOPS="カーナベル"
#     （カーナベル買取は buy_detail HTML が bot 検知で 403 のため Render でも取得不可。無駄打ちを避けて除外）
_DEFAULT_BUYBACK_SKIP_SHOPS = {"遊々亭"}
_buyback_skip_env = os.environ.get("BUYBACK_SKIP_SHOPS")
if _buyback_skip_env is None:
    SKIP_BUYBACK_SHOPS_IN_CI = set(_DEFAULT_BUYBACK_SKIP_SHOPS)
else:
    SKIP_BUYBACK_SHOPS_IN_CI = {s.strip() for s in _buyback_skip_env.split(",") if s.strip()}


def collect_and_save_buyback(
    sb: Client, card_name: str, today: str, shop_names: list[str],
    shop_stats: dict | None = None,
    db_guard: ConsecutiveDbFailureGuard | None = None,
) -> int:
    """1枚のカードの買取価格を取得してSupabaseに保存。保存した行数を返す。

    db_guard を渡すと、保存が例外で失敗した回数を数え、続けて上限回数失敗したら
    DbWriteAborted を送出する（collect_prices.collect_and_save と同じ。db_write_guard.py）。

    販売の collect_and_save とほぼ同じ構造だが、
    店舗×レアリティごとの「最大買取額(max_price)」を記録する点が異なる。
    shop_stats に dict を渡すと、店舗ごとの 取得成功/0件/取得エラー を集計する。
    """
    status: dict = {}
    try:
        results = compare_buyback(card_name, shop_names=shop_names, status_out=status)
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

    # 店舗×レアリティごとに最大買取額を集計（販売は最小だが買取は最大が意味を持つ）
    max_prices: dict[tuple, int] = {}
    for item in results:
        if item.get("sold_out"):
            continue
        # 未開封・付属品つきは通常品と別の値段なので最高額に入れない（art_variant.is_sealed。2026-10-28）
        if is_sealed(item.get("name", "")):
            continue
        price = item.get("price", 0)
        if price <= 10:  # 10円以下は異常値として除外
            continue
        key = (item.get("shop", ""), item.get("rarity", ""))
        if key not in max_prices or price > max_prices[key]:
            max_prices[key] = price

    if not max_prices:
        print(f"  有効な買取価格なし [{card_name}]")
        return 0

    rows = [
        {"card_name": card_name, "shop": shop, "rarity": rarity,
         "max_price": price, "recorded_at": today}
        for (shop, rarity), price in max_prices.items()
    ]

    try:
        saved = upsert_buyback_rows(sb, rows, raise_on_error=db_guard is not None)
    except Exception as e:
        print(f"  DB保存失敗 [{card_name}]")
        # 4xx など DB が応答しない系以外の失敗もここで数える（保存の仕組みが壊れたときも止めるため）。
        # DB が応答しない系は、同じ見張り役の見張り付きクライアント（GuardedSupabase）が数え済みなので重ねて数えない
        if not (is_db_unavailable(e) and getattr(sb, "guard", None) is db_guard):
            db_guard.record_failure(e)  # 上限に達したら DbWriteAborted が上がる
        return 0
    if db_guard is not None:
        db_guard.record_success()
    if saved > 0:
        print(f"  保存完了 [{card_name}]: {saved}件")
    else:
        print(f"  DB保存失敗 [{card_name}]")
    return saved


def cleanup_old_buyback_data(sb: Client):
    """保持期間を超えた古い買取履歴データを削除"""
    cutoff = (datetime.now(JST).date() - timedelta(days=BUYBACK_RETENTION_DAYS)).isoformat()
    try:
        resp = sb.table("buyback_history") \
            .delete() \
            .lt("recorded_at", cutoff) \
            .execute()
        deleted = len(resp.data) if resp.data else 0
        if deleted > 0:
            print(f"古い買取データを削除: {deleted}件（{cutoff}より前）")
    except Exception as e:
        print(f"古い買取データ削除失敗: {e}")


def main():
    """_main をくるみ、DBへの要求が続けて失敗して打ち切ったとき（カード収集のループの外で止まったとき）に
    Discord と記録に残して終了コード1で終える。ループの中で止まったときは _main 側で件数つきで知らせて終える。
    state: _main が「今どの段階か（sync/post）」と、後処理に入った時点の件数・記録の中身を書き込む。
    正常終了の行を記録し終えたあとに止まったとき（recorded）は、行を重ねないよう記録しない。
    """
    started_at = datetime.now(JST)
    state: dict = {"phase": "sync"}
    try:
        _main(state)
    except DbWriteAborted as e:
        report_db_abort(
            get_supabase(), e, label="買取価格収集", table="tracked_cards など", started_at=started_at,
            today=datetime.now(JST).date().isoformat(), phase=state["phase"],
            processed=state.get("processed"), total=state.get("total"),
            success_count=state.get("success_count", 0),
            run_type=None if state.get("recorded") else "buyback",
            record_kwargs=state.get("record_kwargs") or dict(attempted_shops=[], available_shops=[]),
        )
        sys.exit(1)


def _main(state: dict):
    started_at = datetime.now(JST)
    print(f"=== 買取価格履歴収集 {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")

    # DB が応答しない系の失敗はすべての DB 呼び出しで数えて待って再試行し、続けて失敗したら打ち切る
    # （同期処理も含む。db_write_guard.GuardedSupabase）。カード単位の保存失敗も同じ見張り役で数える
    sb = GuardedSupabase(get_supabase(), ConsecutiveDbFailureGuard())

    # ── 注目カードの同期（hot集合のみ。周辺カードは収集しない）──
    # hot同期は外部HTTPを伴うため、import時ではなくここで呼ぶ
    hot: set[str] = set()
    print("\n--- 注目カード同期 ---")
    pack_all, _ = sync_latest_packs(sb)
    hot |= pack_all
    hot |= sync_meta_decks(sb)
    hot |= sync_regulation(sb)
    hot |= sync_searched_cards(sb, recent_days=7, min_count=2)
    print(f"注目カード候補: {len(hot)}件")

    # ── 収集対象: tracked_cards の中から hot に含まれるカードのみ ──
    all_cards = fetch_tracked_cards_with_meta(sb)
    if not all_cards:
        print("監視対象カードが登録されていません")
        return

    hot_cards = [c for c in all_cards if c["card_name"] in hot]
    print(f"収集対象（注目カードのみ）: {len(hot_cards)}件 / 全体: {len(all_cards)}件")

    if not hot_cards:
        print("注目カードが0件のため終了")
        return

    # ── 店舗ヘルスチェック（CIスキップ対象を除いた買取店舗のみ）──
    buyback_shop_names = [
        name for name, _ in BUYBACK_SHOPS
        if name not in SKIP_BUYBACK_SHOPS_IN_CI
    ]
    print(f"\n買取対象店舗: {buyback_shop_names}")
    print("\n--- 店舗ヘルスチェック ---")
    available_shops = check_shop_availability(buyback_shop_names)

    # 観測店舗集合の記録用（P5）: ヘルスチェック落ち・APIキー未設定のスキップ理由を積む
    skipped_shops: dict[str, str] = {
        name: "health_check_failed" for name in buyback_shop_names if name not in available_shops
    }

    # カーナベルはAPIキー必須。未設定だと全カード0件＝誤データになるため当日除外
    if "カーナベル" in available_shops and not (
        os.environ.get("KANABELL_CLOUD_ID") and os.environ.get("KANABELL_API_KEY")
    ):
        print("  NG: カーナベル — KANABELL_CLOUD_ID / KANABELL_API_KEY 未設定のため当日スキップ")
        available_shops.remove("カーナベル")
        skipped_shops["カーナベル"] = "api_key_missing"

    if not available_shops:
        print("全買取店舗が応答しないため収集を中止します")
        record_collection_run(
            sb, "buyback", started_at, datetime.now(JST),
            attempted_shops=buyback_shop_names, available_shops=available_shops,
            skipped_shops=skipped_shops,
        )
        return
    print(f"使用店舗: {available_shops}")

    today = datetime.now(JST).date().isoformat()
    total_saved = 0
    success_count = 0
    fail_count = 0
    shop_stats: dict = {}  # 店舗別の 取得成功/0件/エラー 集計
    # DB保存の連続失敗で打ち切る（2026-10-22 DB停止。collect_prices.py と同じ見張り役）
    db_guard = sb.guard

    for i, card_data in enumerate(hot_cards):
        card_name = card_data["card_name"]
        print(f"[{i+1}/{len(hot_cards)}] {card_name}")
        try:
            saved = collect_and_save_buyback(sb, card_name, today, available_shops,
                                             shop_stats=shop_stats, db_guard=db_guard)
        except DbWriteAborted as e:
            fail_count += 1
            # 古いデータの掃除は DB への問い合わせを重ねるので行わない
            report_db_abort(
                sb, e, label="買取価格収集", table="buyback_history", started_at=started_at, today=today,
                processed=i + 1, total=len(hot_cards), success_count=success_count,
                run_type="buyback",
                record_kwargs=dict(
                    attempted_shops=buyback_shop_names, available_shops=available_shops,
                    skipped_shops=skipped_shops, shop_stats=shop_stats,
                    # cutoff_count は collect_prices.py 専用の列なので渡さない（処理件数は _aborted.processed）
                    success_count=success_count, fail_count=fail_count, saved_rows=total_saved,
                ),
            )
            sys.exit(1)
        if saved > 0:
            total_saved += saved
            success_count += 1
        else:
            fail_count += 1

        if i < len(hot_cards) - 1:
            time.sleep(WAIT_BETWEEN_CARDS)

    # ここから後処理（掃除・記録）。DB で止まったら外側の main が件数つきで知らせる
    state.update(
        phase="post", processed=len(hot_cards), total=len(hot_cards), success_count=success_count,
        record_kwargs=dict(
            attempted_shops=buyback_shop_names, available_shops=available_shops,
            skipped_shops=skipped_shops, shop_stats=shop_stats,
            success_count=success_count, fail_count=fail_count, saved_rows=total_saved,
        ),
    )
    cleanup_old_buyback_data(sb)

    finished_at = datetime.now(JST)
    # ホスト別アクセス回数（店舗サイトへの負担の記録）。shop_stats に載せて collection_runs へ保存する
    host_count_lines = attach_host_request_counts(shop_stats, get_host_request_counts())
    state["recorded"] = record_collection_run(
        sb, "buyback", started_at, finished_at,
        attempted_shops=buyback_shop_names, available_shops=available_shops,
        skipped_shops=skipped_shops, shop_stats=shop_stats,
        success_count=success_count, fail_count=fail_count,
        saved_rows=total_saved,
    )

    elapsed_min = (finished_at - started_at).total_seconds() / 60
    print(f"\n=== 完了（{elapsed_min:.0f}分）===")
    print(f"成功: {success_count}件 / 失敗: {fail_count}件 / 保存行数: {total_saved}")
    if shop_stats:
        print("店舗別取得状況:")
        for shop, s in sorted(shop_stats.items()):
            attempted = s["ok"] + s["empty"] + s["error"]
            print(f"  {shop}: 取得 {s['ok']} / 0件 {s['empty']} / エラー {s['error']}")
            if attempted >= 20 and s["ok"] == 0:
                print(f"  警告: {shop} は全カードで取得0件 — サイト構造変更またはブロックの可能性")
    if host_count_lines:
        print("ホスト別アクセス回数:")
        for line in host_count_lines:
            print(line)


if __name__ == "__main__":
    main()
