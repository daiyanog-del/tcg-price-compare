"""価格履歴 (price_history) への書き込みを集約するモジュール。

collect_prices.py（定期収集）と app.py（検索・見積もりの即時反映）の両方から
呼ばれる共通ヘルパ。書き込み経路を 1 か所に集約することで、スキーマ変更や
冪等性ロジックの修正を 1 か所で完結させる。

前提:
- price_history テーブルには UNIQUE(card_name, shop, rarity, recorded_at) 制約あり
- ON CONFLICT DO UPDATE による upsert で last-write-wins
- フェーズ3 P4/P2（docs/design-phase3-generation-side.md）: min_price_any 列（状態を
  問わない本当の最安値）・code 列（min_price を決めた出品の型番）を追加済み
  （price_history_phase3_columns.sql）
"""
from __future__ import annotations
import logging

from rarity import UNKNOWN_RARITY_LABEL

logger = logging.getLogger(__name__)


# フェーズ3 P4（状態2値記録）: 店舗別の「通常品」判定テーブル。
# 通常品 = キズあり・状態難などの割引条件がついていない出品で、変動検出・グラフ用の
# min_price 系列を構成する。判定基準は docs/design-phase3-generation-side.md
# P4-B の推奨に従う:
#   - condition が "-"（無条件）の出品は常に通常品
#   - カーナベル「状態:SA」= 傷なし相当のため通常品として扱う（B/C/D は除外）
#   - 遊々亭「セール」= 実在の販売価格であり本物の値下げのため通常品に含める
#   - トレコロCBの「中古キズあり」（正価×0.8 SKU）は通常品から除外
#   - カードラッシュの〔状態…〕表記は状態難の出品として通常品から除外（デフォルトの
#     除外扱いに従う。個別の例外登録はしない）
_NORMAL_CONDITION_EXCEPTIONS = {
    ("カーナベル", "状態:SA"),
    ("遊々亭", "セール"),
}


def _is_normal_condition(shop: str, condition: str) -> bool:
    """(shop, condition) が「通常品」（状態を揃えた変動検出用系列の対象）かどうかを判定する。"""
    cond = condition or "-"
    if cond == "-":
        return True
    return (shop, cond) in _NORMAL_CONDITION_EXCEPTIONS


def build_min_price_rows(card_name: str, scrape_results: list, today: str) -> list[dict]:
    """スクレイピング結果から price_history への書き込み行を組み立てる。

    (shop, rarity) ごとに2種類の最安値を並行集計する（フェーズ3 P4）:
      - min_price     : 状態を揃えた「通常品」の最安値（変動検出・グラフ用）
      - min_price_any : 状態を問わない本当の最安値（表示・簡易見積り用）
    通常品の出品が1件もない (shop, rarity) では、min_price は min_price_any に
    フォールバックする（行自体を欠測させない。表示すべき最安値がある限り欠測は
    偽の空白日を生むため、より安全側に倒す実装判断）。

    code（フェーズ3 P2）は min_price を決めた出品の型番を属性として添える
    （通常品が無く min_price_any にフォールバックした場合はその出品の型番）。
    店舗横断のキーとしては使えない（店舗ごとに表記形式が異なる）ため、あくまで
    「その日その店のそのレアリティの最安は、どの収録だったか」の監査情報。

    url は code と役割分担が非対称: 常に min_price_any（本当の最安値）を決めた
    出品のURLを添える（code は min_price 側のまま変えない）。通常品とany品が
    別出品の場合、code と url は別の出品を指すことになる（意図どおり）。
    出品に有効なURLが無ければ None（NULL維持）。

    rarity 抽出失敗行（空文字）は "(不明)" ラベルへ明示し、正規レアリティ系列に
    混ぜない（フェーズ3 P3。除外は読み出し側の代表レアリティ選定で行う）。

    sold_out や price <= 10 はノイズとして除外する。
    （畳みキー・除外条件は collect_prices.py:156-175 から切り出したロジックを踏襲）
    """
    # key(shop, rarity) -> {"any": (price, code, url), "normal": (price, code) | None}
    best: dict[tuple[str, str], dict] = {}
    for item in scrape_results:
        if item.get("sold_out"):
            continue
        price = item.get("price", 0)
        if price <= 10:
            continue
        shop = item.get("shop", "")
        if not shop:
            continue
        rarity = item.get("rarity", "") or ""
        if not rarity:
            rarity = UNKNOWN_RARITY_LABEL
        code = item.get("code", "") or ""
        condition = item.get("condition", "") or ""
        url = item.get("url", "") or ""

        key = (shop, rarity)
        entry = best.setdefault(key, {"any": None, "normal": None})

        if entry["any"] is None or price < entry["any"][0]:
            entry["any"] = (price, code, url)

        if _is_normal_condition(shop, condition):
            if entry["normal"] is None or price < entry["normal"][0]:
                entry["normal"] = (price, code)

    rows = []
    for (shop, rarity), entry in best.items():
        any_price, any_code, any_url = entry["any"]
        if entry["normal"] is not None:
            min_price, min_code = entry["normal"]
        else:
            min_price, min_code = any_price, any_code
        rows.append({
            "card_name": card_name, "shop": shop, "rarity": rarity,
            "min_price": min_price, "min_price_any": any_price,
            "code": min_code, "url": any_url or None, "recorded_at": today,
        })
    return rows


def upsert_price_rows(sb, rows: list[dict], ignore_duplicates: bool = False) -> int:
    """price_history に upsert する。

    UNIQUE(card_name, shop, rarity, recorded_at) 制約に基づき、既存があれば
    全列上書き（直近の scraper の結果を最新として採用 = last-write-wins）。

    ignore_duplicates=True の場合は既存行があれば書き込まない（フェーズ3 P6b。
    網羅性の低い経路 /api/deck の即時upsertが、夜間収集の網羅的な最安値を
    同日上書きしないようにするための切替。/api/search 等は既定の False のまま）。

    成功した行数を返す。エラー時は 0 を返し、警告ログのみ出す（呼び元の処理は止めない）。
    """
    if not rows:
        return 0
    try:
        sb.table("price_history").upsert(
            rows,
            on_conflict="card_name,shop,rarity,recorded_at",
            ignore_duplicates=ignore_duplicates,
        ).execute()
        return len(rows)
    except Exception as e:
        # ERRORレベル: ここが無言で失敗し続けると価格履歴が丸ごと止まる
        # （例: 列追加DDL未適用のままコードだけデプロイした場合）
        logger.error(f"[price_persist] upsert 失敗 ({len(rows)}行): {e}")
        return 0


def upsert_buyback_rows(sb, rows: list[dict]) -> int:
    """buyback_history に upsert する。

    UNIQUE(card_name, shop, rarity, recorded_at) 前提。同日の再実行は上書き
    （その日の最高買取額の最新値を採用）。

    成功した行数を返す。エラー時は 0 を返し、警告ログのみ出す（呼び元の処理は止めない）。
    """
    if not rows:
        return 0
    try:
        sb.table("buyback_history").upsert(
            rows,
            on_conflict="card_name,shop,rarity,recorded_at",
        ).execute()
        return len(rows)
    except Exception as e:
        logger.error(f"[price_persist] buyback upsert 失敗 ({len(rows)}行): {e}")
        return 0
