"""
aggregations.py — 価格履歴集計の共通関数

app.py / notify.py の両方から利用する。
重複実装を1箇所に集約する。
"""

from __future__ import annotations
from collections import defaultdict

from rarity import UNKNOWN_RARITY_LABEL


def daily_min_by_lowest_rarity(rows: list) -> dict:
    """price_history 行リストから、最安レアリティ系列を代表として
    {card_name: {date_str: min_price}} を返す。

    異なるレアリティ間の価格を比較しないよう、以下のアルゴリズムで代表レアリティを選ぶ:
      1. (card_name, rarity, date) → min_price を集計
      2. "(不明)"（rarity抽出失敗の隔離ラベル、フェーズ3 P3）は他に候補がある限り
         代表選定の候補から除外する。正体不明の疑似系列がグラフの顔になることを防ぐ
      3. 各 card_name の「最新日の価格が最安のレアリティ」を代表に選択
         ※ 最新日に欠損のあるレアリティは候補除外
         ※ 全レアリティで最新日欠損の場合は期間最小値が最安のレアリティにフォールバック
      4. 代表レアリティの系列のみ返す

    10円以下の異常値は除外する。
    """
    # ステップ1: (name, rarity, date) → min_price
    rarity_dates: dict = defaultdict(lambda: defaultdict(dict))
    for r in rows:
        name   = r.get("card_name", "")
        rarity = r.get("rarity", "") or ""
        date   = r.get("recorded_at", "")[:10]
        price  = r.get("min_price", 0)
        if not name or not date or price <= 10:
            continue
        if date not in rarity_dates[name][rarity] or price < rarity_dates[name][rarity][date]:
            rarity_dates[name][rarity][date] = price

    # ステップ2 & 3: 代表レアリティを選んで card_name → {date: price} を返す
    result: dict = {}
    for name, by_rarity in rarity_dates.items():
        if not by_rarity:
            continue
        # "(不明)" は他に候補がある限り除外する（フェーズ3 P3）。
        # 全レアリティが "(不明)" しかない場合のみフォールバックとして使用する。
        # 移行前のレガシー空文字 rarity も同様に除外（保持90日で自然消滅するまでの穴塞ぎ）
        candidates = {r: d for r, d in by_rarity.items() if r not in ("", UNKNOWN_RARITY_LABEL)} or by_rarity

        all_dates = sorted({d for dates in candidates.values() for d in dates})
        if not all_dates:
            continue
        latest_date = all_dates[-1]

        best_rarity = None
        best_price  = None
        # 最新日に存在するレアリティから最安を選ぶ
        for rarity, dates in candidates.items():
            if latest_date not in dates:
                continue
            p = dates[latest_date]
            if best_price is None or p < best_price:
                best_price  = p
                best_rarity = rarity

        # フォールバック: 全レアリティで最新日欠損 → 期間最小値が最安のレアリティ
        if best_rarity is None:
            for rarity, dates in candidates.items():
                min_p = min(dates.values())
                if best_price is None or min_p < best_price:
                    best_price  = min_p
                    best_rarity = rarity

        if best_rarity is not None:
            result[name] = dict(candidates[best_rarity])

    return result


def common_shop_price_change(rows: list) -> dict:
    """price_history 行リスト（複数カード可、shop列必須）から、カードごとに
    共通店舗比較による値動き（base_7d/diff/pct）を計算する。

    代表レアリティの選定は daily_min_by_lowest_rarity と同一ロジック（店舗横断で
    (rarity, date) -> 最安値を求め、最新日に存在するレアリティのうち最安を選択。
    全レアリティで最新日欠損なら期間最小値が最安のレアリティにフォールバック）。
    店舗粒度の比較にはレアリティ名自体が必要なため、選定ロジックをここに複製する
    （notify._representative_rarity が同じ事情で複製しているのと同じ理由）。

    date_new は窓内最新日で固定。date_old は「date_new との共通店舗（両日に
    記録がある店舗）が2店以上になる最古の日」を窓内で古い順に探索して選ぶ
    （notify.compute_drop と同じ探索方式）。ただし通知用の閾値判定・
    ガード②（値下がり方向に2店以上確認）はここでは適用しない。この関数は
    表示用の±%再計算だけを目的とするため（裁定 中7, 2026-08-07: ガード②は
    Web Push通知の誤送信対策専用であり、UI表示の±%計算には適用しない）。

    diff/pct は見つかった共通店舗集合内の min どうしで計算する。条件を満たす
    date_old が窓内に見つからない場合、その系列は base_7d/diff/pct とも
    None を返す（比較不能な古値を使って偽の変動シグナルを出さないため）。

    戻り値: {card_name: {"base_7d": int|None, "diff": int|None, "pct": float|None}}
    """
    by_card: dict = defaultdict(list)
    for r in rows:
        name = r.get("card_name", "")
        if name:
            by_card[name].append(r)

    result: dict = {}
    for name, card_rows in by_card.items():
        result[name] = _common_shop_change_for_card(card_rows)
    return result


def _common_shop_change_for_card(rows: list) -> dict:
    """common_shop_price_change の内部処理。1カード分の行から
    base_7d/diff/pct を計算する。"""
    none_result = {"base_7d": None, "diff": None, "pct": None}

    # 代表レアリティの選定（daily_min_by_lowest_rarity と同一ロジックの複製）
    rarity_dates: dict = defaultdict(dict)  # rarity -> {date: min_price}
    for r in rows:
        rarity = r.get("rarity", "") or ""
        date   = r.get("recorded_at", "")[:10]
        price  = r.get("min_price", 0)
        if not date or price <= 10:
            continue
        if date not in rarity_dates[rarity] or price < rarity_dates[rarity][date]:
            rarity_dates[rarity][date] = price
    if not rarity_dates:
        return none_result
    candidates = {r: d for r, d in rarity_dates.items() if r not in ("", UNKNOWN_RARITY_LABEL)} or rarity_dates

    all_dates = sorted({d for dates in candidates.values() for d in dates})
    if not all_dates:
        return none_result
    latest_date = all_dates[-1]

    best_rarity, best_price = None, None
    for rarity, dates in candidates.items():
        if latest_date not in dates:
            continue
        p = dates[latest_date]
        if best_price is None or p < best_price:
            best_price, best_rarity = p, rarity
    if best_rarity is None:
        # フォールバック: 全レアリティで最新日欠損 → 期間最小値が最安のレアリティ
        for rarity, dates in candidates.items():
            min_p = min(dates.values())
            if best_price is None or min_p < best_price:
                best_price, best_rarity = min_p, rarity
    if best_rarity is None:
        return none_result

    # 代表レアリティの行だけに絞り、店舗別の日次最安値を求める
    shop_dates: dict = defaultdict(dict)  # shop -> {date: min_price}
    for r in rows:
        if (r.get("rarity", "") or "") != best_rarity:
            continue
        shop  = r.get("shop", "")
        date  = r.get("recorded_at", "")[:10]
        price = r.get("min_price", 0)
        if not shop or not date or price <= 10:
            continue
        if date not in shop_dates[shop] or price < shop_dates[shop][date]:
            shop_dates[shop][date] = price

    shop_all_dates = sorted({d for dates in shop_dates.values() for d in dates})
    if len(shop_all_dates) < 2:
        return none_result
    date_new = shop_all_dates[-1]

    # date_new との共通店舗が2店以上になる最古の日を、窓内を古い順に探索して選ぶ。
    # 閾値=2店は notify.compute_drop と同じ値を維持する（裁定H2, 2026-08-07:
    # 3店以上に上げる案は本番実測で対象カードの6.6%が変動表示を喪失する計算に
    # なり悪化のため不採用。2店のまま据え置く）
    date_old = None
    common_shops: list = []
    for candidate in shop_all_dates[:-1]:
        shops = [s for s, dates in shop_dates.items() if candidate in dates and date_new in dates]
        if len(shops) >= 2:
            date_old = candidate
            common_shops = shops
            break
    if date_old is None:
        return none_result

    base_price   = min(shop_dates[s][date_old] for s in common_shops)
    latest_price = min(shop_dates[s][date_new] for s in common_shops)
    if base_price <= 0:
        return none_result

    diff = latest_price - base_price
    pct  = round((diff / base_price) * 100, 1)
    return {"base_7d": base_price, "diff": diff, "pct": pct}
