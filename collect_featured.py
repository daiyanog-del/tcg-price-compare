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
from collection_run import attach_host_request_counts
from scraper import compare_prices, SHOPS, get_host_request_counts
from price_persist import build_min_price_rows, upsert_price_rows
from db_write_guard import (
    ConsecutiveDbFailureGuard, DbWriteAborted, GuardedSupabase, is_db_unavailable, report_db_abort,
)
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

# 店舗の試行数（分母）がこれ未満ならブロック様と判定しない下限。
# 当日取得済みのトレコロを外すと分母が小さくなり、1枚の失敗で比率が閾値を超えて
# 約32分待ちになるのを避ける。
# TODO: calibrate from data（暫定値）
BLOCKLIKE_MIN_ATTEMPTED = 5

# 朝5時の collect_prices.py で当日分を取得済みなら第1パスで取り直さない店舗。
# トレコロCBは2026-10-11に負荷で応答しなくなった経緯があり（夜間収集が1日4,300回超）、
# 同日の二重取得を避けて負担を減らす。他店は従来どおり last-write-wins で取り直す
SKIP_IF_COLLECTED_TODAY_SHOPS = ("トレコロCB",)


def fetch_cards_collected_today(sb, card_names: list[str], today: str, shop: str) -> set[str]:
    """today（JST日付）の price_history に shop の行が既にある card_names の部分集合を返す。

    カードごとに問い合わせず、(shop, recorded_at) の行をまとめて分割取得し
    対象カード集合と突き合わせる。PostgREST の1000行上限対策で range 分割し、
    ページ境界で行を落とさないよう card_name+rarity で order する
    （tests/test_price_history_read_discipline.py の規律）。
    取得に失敗した場合は空集合を返す＝従来どおり全カードを取る側（安全側）に倒す。
    """
    page = 1000
    try:
        rows: list[dict] = []
        offset = 0
        while True:
            resp = (sb.table("price_history").select("card_name,rarity")
                    .eq("shop", shop).eq("recorded_at", today)
                    .order("card_name").order("rarity")
                    .range(offset, offset + page - 1).execute())
            batch = resp.data or []
            rows.extend(batch)
            if len(batch) < page:
                break
            offset += page
    except Exception as e:
        print(f"  警告: {shop} の当日取得済み確認に失敗: {e} — 全カードを取得します")
        return set()
    targets = set(card_names)
    return {r["card_name"] for r in rows if r.get("card_name") in targets}


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
                              threshold: float = BLOCKLIKE_FAIL_RATIO,
                              min_attempted: int = BLOCKLIKE_MIN_ATTEMPTED) -> set[str]:
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
        if attempted >= max(1, min_attempted) and (fails / attempted) >= threshold:
            blocklike.add(shop)
    return blocklike


def _group_pairs_by_card(pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    """(card, shop) ペアのリストを {card: [shop, ...]} へグルーピングする。"""
    by_card: dict[str, list[str]] = {}
    for card_name, shop in pairs:
        by_card.setdefault(card_name, []).append(shop)
    return by_card


def collect_and_save_for_shops(sb, card_name: str, today: str, shop_names: list[str],
                                shop_stats: dict | None = None,
                                db_guard: ConsecutiveDbFailureGuard | None = None) -> tuple[int, list[str]]:
    """1枚のカードについて、指定店舗のみ検索してSupabaseに保存する。

    collect_prices.collect_and_save と異なり、失敗した店舗名のリストも返す
    （本スクリプトは (card, shop) 単位の再試行が必要なため）。
    再試行呼び出し時は shop_stats に同じ店舗の集計が積み増しされる（再試行分を含む
    累積値になる。ラウンドごとの内訳が要る場合はログの「再試行 N/M」行を参照）。
    戻り値: (保存した行数, 失敗した店舗名のリスト)

    db_guard を渡すと、保存が例外で失敗した回数を数え、続けて上限回数失敗したら
    DbWriteAborted を送出する（collect_prices.collect_and_save と同じ。db_write_guard.py）。
    DB保存の失敗は店舗の取得失敗ではないので、失敗店舗リスト（再試行の対象）には入れない。
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

    try:
        # デフォルト ignore_duplicates=False（last-write-wins）
        saved = upsert_price_rows(sb, rows, raise_on_error=db_guard is not None)
    except Exception as e:
        print(f"  DB保存失敗 [{card_name}]")
        # 4xx など DB が応答しない系以外の失敗もここで数える（保存の仕組みが壊れたときも止めるため）。
        # DB が応答しない系は、同じ見張り役の見張り付きクライアント（GuardedSupabase）が数え済みなので重ねて数えない
        if not (is_db_unavailable(e) and getattr(sb, "guard", None) is db_guard):
            db_guard.record_failure(e)  # 上限に達したら DbWriteAborted が上がる
        return 0, failed_shops
    if db_guard is not None:
        db_guard.record_success()
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
            get_supabase(), e, label="新弾フィーチャー価格収集", table="featured_pack など", started_at=started_at,
            today=datetime.now(JST).date().isoformat(), phase=state["phase"],
            processed=state.get("processed"), total=state.get("total"),
            success_count=state.get("success_count", 0),
            run_type=None,
        )
        sys.exit(1)


def _main(state: dict):
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

    # DB が応答しない系の失敗はすべての DB 呼び出しで数えて待って再試行し、続けて失敗したら打ち切る
    # （同期処理も含む。db_write_guard.GuardedSupabase）。カード単位の保存失敗も同じ見張り役で数える
    sb = GuardedSupabase(get_supabase(), ConsecutiveDbFailureGuard())

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

    # 朝5時の収集で当日行が既にある (カード, 店舗) は第1パスの対象から外す。
    # 行が無いカード（朝に失敗・未収集）は従来どおり取る。失敗ペアの再試行は
    # 第1パスの失敗集合から作られるため、外したペアは再試行にも現れない
    # 既知の制約: 当日行は /api/deck（トレコロは1ページのみ）や /api/search の途中結果からも
    # 書かれるため、朝の収集で取れていないカードを取得済みと誤認しうる。
    # 翌日の通常収集で回復するので許容する
    already_collected: dict[str, set[str]] = {}
    for skip_shop in SKIP_IF_COLLECTED_TODAY_SHOPS:
        if skip_shop in available_shops:
            already_collected[skip_shop] = fetch_cards_collected_today(
                sb, card_names, today, skip_shop
            )
            print(f"{skip_shop}: 当日取得済み {len(already_collected[skip_shop])}/{len(card_names)}枚は取り直しません")
    # ブロック様判定の分母は「実際に試行したカード数」にする
    attempted_per_shop = {
        shop: sum(1 for c in card_names if c not in already_collected.get(shop, ()))
        for shop in available_shops
    }

    # DB保存の連続失敗で打ち切る（2026-10-22 DB停止。collect_prices.py と同じ見張り役）。
    # 第1パスと再試行ラウンドで同じ見張り役を使う（どこで続けて失敗しても止める）
    db_guard = sb.guard
    processed = 0     # 第1パスで処理したカード数（打ち切りの通知用）
    saved_cards = 0   # 第1パスで保存できたカード数（同上）
    try:
        for i, card_name in enumerate(card_names):
            print(f"[{i + 1}/{len(card_names)}] {card_name}")
            processed = i + 1
            card_shops = [s for s in available_shops if card_name not in already_collected.get(s, ())]
            if not card_shops:
                print("  全店舗取得済みのためスキップ")
                continue
            saved, failed_shops = collect_and_save_for_shops(
                sb, card_name, today, card_shops, shop_stats=shop_stats, db_guard=db_guard
            )
            total_saved += saved
            if saved:
                saved_cards += 1
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
                        sb, card_name, today, shops, shop_stats=shop_stats, db_guard=db_guard
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
                        sb, card_name, today, shops, shop_stats=shop_stats, db_guard=db_guard
                    )
                    total_saved += saved
                    next_failed_pairs.extend((card_name, s) for s in still_failed)
                    time.sleep(WAIT_BETWEEN_CARDS)

            failed_pairs = next_failed_pairs
            # 次ラウンドのブロック様判定は、直前ラウンドで実際に再試行したペア数を基準にする
            attempted_per_shop = {}
            for _c, s in (non_blocklike_pairs + blocklike_pairs):
                attempted_per_shop[s] = attempted_per_shop.get(s, 0) + 1
    except DbWriteAborted as e:
        # collection_runs には記録しない（run_type の CHECK 制約に新弾収集が無い。モジュール冒頭の注記）
        report_db_abort(
            sb, e, label="新弾フィーチャー価格収集", table="price_history", started_at=started_at,
            today=today, processed=processed, total=len(card_names), success_count=saved_cards,
            run_type=None,
        )
        sys.exit(1)

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
    host_count_lines = attach_host_request_counts(shop_stats, get_host_request_counts())
    if host_count_lines:
        print("ホスト別アクセス回数:")
        for line in host_count_lines:
            print(line)


if __name__ == "__main__":
    main()
