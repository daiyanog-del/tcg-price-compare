# tests/test_top_page.py — top_page.py（最初の画面向けランキング集計）の単体テスト
#
# 対象: summarize_deck / rank_decks（N-1 デッキランキング）、
#       aggregate_common_shop_movers（N-2 価格推移ランキング・共通店舗ガードのみ）
# 方針: Supabase・ネットワーク不要な純関数のみをテストする（x_poster等の既存流儀に倣う）

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from top_page import (
    summarize_deck, rank_decks, aggregate_common_shop_movers,
    MOVERS_MIN_PRICE, MOVERS_STABILITY_DAYS,
)


# ── summarize_deck ──

class TestSummarizeDeck:
    def test_all_priced(self):
        entries = [{"name": "A", "qty": 1}, {"name": "B", "qty": 3}]
        price_lookup = {"A": {"price": 1000}, "B": {"price": 500}}
        result = summarize_deck(entries, price_lookup)
        assert result == {"total": 1000 + 500 * 3, "priced_count": 2, "missing_count": 0}

    def test_one_missing(self):
        entries = [{"name": "A", "qty": 2}, {"name": "見つからないカード", "qty": 1}]
        price_lookup = {"A": {"price": 300}}
        result = summarize_deck(entries, price_lookup)
        assert result == {"total": 600, "priced_count": 1, "missing_count": 1}

    def test_all_missing(self):
        entries = [{"name": "X", "qty": 1}, {"name": "Y", "qty": 1}]
        result = summarize_deck(entries, {})
        assert result == {"total": 0, "priced_count": 0, "missing_count": 2}

    def test_zero_price_treated_as_missing(self):
        # priceが0/Noneの場合は「取得できた」扱いにしない（誤って合計に0円を含めて
        # 「価格が付いている」ように見せない）
        entries = [{"name": "A", "qty": 1}]
        price_lookup = {"A": {"price": 0}}
        result = summarize_deck(entries, price_lookup)
        assert result == {"total": 0, "priced_count": 0, "missing_count": 1}

    def test_empty_deck(self):
        assert summarize_deck([], {}) == {"total": 0, "priced_count": 0, "missing_count": 0}


# ── rank_decks ──

class TestRankDecks:
    def make_decks(self):
        return [
            {"name": "エルフェンノーツ", "tier": 2, "share": 11.76, "total": 12499},
            {"name": "キラーチューン", "tier": 3, "share": 8.82, "total": 8666},
            {"name": "巳剣", "tier": 3, "share": 8.82, "total": 25679},
            {"name": "光と闇の儀式", "tier": 1, "share": 14.71, "total": 46689},
        ]

    def test_sort_price_ascending(self):
        result = rank_decks(self.make_decks(), sort="price")
        assert [d["name"] for d in result] == ["キラーチューン", "エルフェンノーツ", "巳剣", "光と闇の儀式"]

    def test_sort_tier_strongest_first(self):
        result = rank_decks(self.make_decks(), sort="tier")
        # Tier1が最強想定で先頭。Tier3が2件あるためshare降順のタイブレークを確認
        assert [d["name"] for d in result] == ["光と闇の儀式", "エルフェンノーツ", "キラーチューン", "巳剣"]

    def test_tier_zero_sorts_last(self):
        decks = [
            {"name": "Tier不明", "tier": 0, "share": 50.0, "total": 100},
            {"name": "Tier1", "tier": 1, "share": 1.0, "total": 100},
        ]
        result = rank_decks(decks, sort="tier")
        assert [d["name"] for d in result] == ["Tier1", "Tier不明"]

    def test_default_sort_is_price(self):
        decks = self.make_decks()
        assert rank_decks(decks) == rank_decks(decks, sort="price")

    # ── Q-6: 価格取得に失敗したデッキが「安い順」で1位に化けない ──

    def test_q6_zero_total_always_last(self):
        decks = [
            {"name": "価格取得済みデッキ", "tier": 1, "share": 10, "total": 5000,
             "priced_count": 3, "missing_count": 0},
            {"name": "全滅デッキ", "tier": 1, "share": 5, "total": 0,
             "priced_count": 0, "missing_count": 3},
        ]
        result = rank_decks(decks, sort="price")
        assert [d["name"] for d in result] == ["価格取得済みデッキ", "全滅デッキ"], \
            "total=0（価格情報なし）は最安に見えても1位に来てはいけない"

    def test_q6_partial_deck_ranked_after_fully_priced_even_if_cheaper(self):
        # 一部未取得のデッキは、合計が安く見えても全カード取得済みデッキより後ろへ
        decks = [
            {"name": "全カード取得済み(高め)", "tier": 1, "share": 10, "total": 20000,
             "priced_count": 5, "missing_count": 0},
            {"name": "一部未取得(安く見える)", "tier": 1, "share": 5, "total": 3000,
             "priced_count": 1, "missing_count": 4},
        ]
        result = rank_decks(decks, sort="price")
        assert [d["name"] for d in result] == ["全カード取得済み(高め)", "一部未取得(安く見える)"]

    def test_q6_ordering_within_each_reliability_group(self):
        decks = [
            {"name": "全滅A", "tier": 1, "share": 1, "total": 0, "priced_count": 0, "missing_count": 5},
            {"name": "一部未取得・高", "tier": 1, "share": 1, "total": 9000, "priced_count": 1, "missing_count": 2},
            {"name": "全取得・安", "tier": 1, "share": 1, "total": 1000, "priced_count": 3, "missing_count": 0},
            {"name": "一部未取得・安", "tier": 1, "share": 1, "total": 2000, "priced_count": 1, "missing_count": 2},
            {"name": "全取得・高", "tier": 1, "share": 1, "total": 8000, "priced_count": 3, "missing_count": 0},
        ]
        result = rank_decks(decks, sort="price")
        assert [d["name"] for d in result] == [
            "全取得・安", "全取得・高", "一部未取得・安", "一部未取得・高", "全滅A",
        ]


# ── aggregate_common_shop_movers ──

def _row(name, shop, price, rarity=""):
    return {"card_name": name, "shop": shop, "min_price": price, "rarity": rarity}


class TestAggregateCommonShopMovers:
    def test_up_and_down_split(self):
        rows_new = [_row("値上がりカード", "A店", 2000), _row("値下がりカード", "A店", 1000)]
        rows_old = [_row("値上がりカード", "A店", 1000), _row("値下がりカード", "A店", 2000)]
        result = aggregate_common_shop_movers(rows_new, rows_old, min_price=1000)
        assert len(result["up"]) == 1 and result["up"][0]["name"] == "値上がりカード"
        assert result["up"][0] == {"name": "値上がりカード", "rarity": "", "today": 2000, "yesterday": 1000, "diff": 1000, "pct": 100.0}
        assert len(result["down"]) == 1 and result["down"][0]["name"] == "値下がりカード"
        assert result["down"][0] == {"name": "値下がりカード", "rarity": "", "today": 1000, "yesterday": 2000, "diff": -1000, "pct": -50.0}
        assert result["stability_checked"] is False  # rows_prev未指定なので定着チェックは行わない

    def test_below_min_price_excluded(self):
        # 最安(当日)が閾値未満のカードは対象外
        rows_new = [_row("安いカード", "A店", 999)]
        rows_old = [_row("安いカード", "A店", 500)]
        result = aggregate_common_shop_movers(rows_new, rows_old, min_price=1000)
        assert result["up"] == [] and result["down"] == []

    def test_at_min_price_boundary_included(self):
        # 当日・7日前とも閾値ちょうどなら含める（両端とも境界OK）
        rows_new = [_row("境界カード", "A店", 1000)]
        rows_old = [_row("境界カード", "A店", 900)]
        # 900は閾値未満なのでP-1修正後は除外される（下のP-1テストで確認）。
        # 境界確認は両端とも1000ちょうどにする
        rows_old_at_boundary = [_row("境界カード", "A店", 1000)]
        result = aggregate_common_shop_movers(rows_new, rows_old_at_boundary, min_price=1000)
        assert len(result["up"]) == 0  # today==yesterday=1000なので「変化なし」で除外
        rows_new2 = [_row("境界カード2", "A店", 1001)]
        rows_old2 = [_row("境界カード2", "A店", 1000)]
        result2 = aggregate_common_shop_movers(rows_new2, rows_old2, min_price=1000)
        assert len(result2["up"]) == 1

    def test_unchanged_price_excluded(self):
        # 変化が無いカードはランキングに混ざらない（「変化があったものだけ」の要件）
        rows_new = [_row("変化なしカード", "A店", 1500)]
        rows_old = [_row("変化なしカード", "A店", 1500)]
        result = aggregate_common_shop_movers(rows_new, rows_old, min_price=1000)
        assert result["up"] == [] and result["down"] == []

    def test_non_common_shop_excluded_guard1(self):
        # ガード①: 当日にしか無い店舗（7日前のデータが無い店舗）は比較対象から除外
        rows_new = [_row("片方の日にしか無い店のカード", "新規店", 5000)]
        rows_old = []  # 7日前は別カードのみ
        result = aggregate_common_shop_movers(rows_new, rows_old, min_price=1000)
        assert result["up"] == [] and result["down"] == []

    def test_common_shop_uses_min_across_shops(self):
        # カード単位の最安は「共通店舗内の最安値」。当日・7日前で最安を出す店舗が
        # 異なっていても、それぞれの共通店舗内最安を独立に採用する
        rows_new = [_row("複数店カード", "A店", 3000), _row("複数店カード", "B店", 2000)]
        rows_old = [_row("複数店カード", "A店", 1000), _row("複数店カード", "B店", 4000)]
        result = aggregate_common_shop_movers(rows_new, rows_old, min_price=1000)
        # today = min(3000, 2000) = 2000, yesterday = min(1000, 4000) = 1000
        assert result["up"][0]["today"] == 2000
        assert result["up"][0]["yesterday"] == 1000

    def test_zero_or_missing_old_price_excluded(self):
        rows_new = [_row("旧値ゼロカード", "A店", 2000)]
        rows_old = [_row("旧値ゼロカード", "A店", 0)]  # 10円以下は異常値として除外される
        result = aggregate_common_shop_movers(rows_new, rows_old, min_price=1000)
        assert result["up"] == [] and result["down"] == []

    def test_sorted_by_abs_pct_desc(self):
        rows_new = [
            _row("小さい変動", "A店", 1100), _row("大きい変動", "A店", 3000),
        ]
        rows_old = [
            _row("小さい変動", "A店", 1000), _row("大きい変動", "A店", 1000),
        ]
        result = aggregate_common_shop_movers(rows_new, rows_old, min_price=1000)
        assert [e["name"] for e in result["up"]] == ["大きい変動", "小さい変動"]

    def test_empty_input(self):
        result = aggregate_common_shop_movers([], [])
        assert result["up"] == [] and result["down"] == [] and result["stability_checked"] is False

    def test_default_min_price_constant(self):
        assert MOVERS_MIN_PRICE == 1000

    def test_default_stability_days_constant(self):
        assert MOVERS_STABILITY_DAYS == 1

    # ── P-1: 閾値は当日・7日前の両方に掛ける ──

    def test_p1_old_price_below_threshold_excluded_even_if_new_above(self):
        # 本番で実際に1位に出ていた「次元融合」の再現: 7日前が閾値未満・当日は倍増
        # 従来（当日にしか閾値を掛けていない）実装ではこれが1位に出ていた
        rows_new = [_row("次元融合", "A店", 1080)]
        rows_old = [_row("次元融合", "A店", 540)]
        result = aggregate_common_shop_movers(rows_new, rows_old, min_price=1000)
        assert result["up"] == [] and result["down"] == []

    def test_p1_both_ends_above_threshold_included(self):
        rows_new = [_row("滅びの黒魔術師", "A店", 3980)]
        rows_old = [_row("滅びの黒魔術師", "A店", 2980)]
        result = aggregate_common_shop_movers(rows_new, rows_old, min_price=1000)
        assert len(result["up"]) == 1
        assert result["up"][0]["name"] == "滅びの黒魔術師"

    def test_p1_new_price_below_threshold_excluded(self):
        # 従来から効いていたガード（当日側）が引き続き効くことの確認
        rows_new = [_row("安くなったカード", "A店", 900)]
        rows_old = [_row("安くなったカード", "A店", 2000)]
        result = aggregate_common_shop_movers(rows_new, rows_old, min_price=1000)
        assert result["up"] == [] and result["down"] == []

    # ── P-2: 定着チェック（在庫入替の一時的な変動を除外） ──

    def test_p2_stable_price_included(self):
        # 当日=前日（共通店舗ベース）なら定着済みとして含める
        rows_new = [_row("定着カード", "A店", 2000)]
        rows_old = [_row("定着カード", "A店", 1000)]
        rows_prev = [_row("定着カード", "A店", 2000)]  # 前日も当日と同じ価格
        result = aggregate_common_shop_movers(rows_new, rows_old, rows_prev, min_price=1000)
        assert result["stability_checked"] is True
        assert len(result["up"]) == 1
        assert result["up"][0]["name"] == "定着カード"

    def test_p2_unstable_price_excluded(self):
        # 前日と当日が一致しない（在庫入替の疑い）は除外
        rows_new = [_row("在庫入替カード", "A店", 2000)]
        rows_old = [_row("在庫入替カード", "A店", 1000)]
        rows_prev = [_row("在庫入替カード", "A店", 1200)]  # 前日は当日と違う価格
        result = aggregate_common_shop_movers(rows_new, rows_old, rows_prev, min_price=1000)
        assert result["stability_checked"] is True
        assert result["up"] == [] and result["down"] == []

    def test_p2_missing_prev_data_globally_skips_stability_check(self):
        # rows_prev が空/Noneなら「前日データが取れない」として定着チェックをスキップして通す
        rows_new = [_row("前日データ無しカード", "A店", 2000)]
        rows_old = [_row("前日データ無しカード", "A店", 1000)]
        result_none = aggregate_common_shop_movers(rows_new, rows_old, None, min_price=1000)
        assert result_none["stability_checked"] is False
        assert len(result_none["up"]) == 1

        result_empty = aggregate_common_shop_movers(rows_new, rows_old, [], min_price=1000)
        assert result_empty["stability_checked"] is False
        assert len(result_empty["up"]) == 1

    def test_p2_card_missing_from_prev_common_shops_excluded(self):
        # rows_prev自体は届いている（グローバルスキップではない）が、このカードの
        # 共通店舗（ガード①対象店舗）には前日データが無い → 検証不能として除外
        rows_new = [_row("前日データ無しカード", "A店", 2000)]
        rows_old = [_row("前日データ無しカード", "A店", 1000)]
        rows_prev = [_row("別のカード", "A店", 500)]  # 別カードの前日データのみ存在
        result = aggregate_common_shop_movers(rows_new, rows_old, rows_prev, min_price=1000)
        assert result["stability_checked"] is True
        assert result["up"] == [] and result["down"] == []

    def test_p2_uses_only_common_shops_with_old_for_stability(self):
        # 定着チェックに使う店舗は「7日前との共通店舗」に限定される。
        # 前日データが別店舗にしか無い場合は検証不能として除外される
        rows_new = [_row("複数店定着カード", "A店", 2000), _row("複数店定着カード", "B店", 2500)]
        rows_old = [_row("複数店定着カード", "A店", 1000)]  # B店は7日前データなし=共通店舗はA店のみ
        rows_prev = [_row("複数店定着カード", "B店", 2000)]  # 前日データはB店にしかない
        result = aggregate_common_shop_movers(rows_new, rows_old, rows_prev, min_price=1000)
        assert result["stability_checked"] is True
        assert result["up"] == [] and result["down"] == []

    # ── Q-7: 定着チェックの店舗集合を当日・前日で対称に揃える ──

    def test_q7_false_positive_prevented_when_only_one_shop_has_prev_data(self):
        # reviewer実証の偽陽性再現: 当日 A¥2,000/B¥2,500、前日はBの¥2,000のみ。
        # 修正前は「前日データがある店舗(B)のminが当日の全体minと一致するか」で
        # 判定していたため、B店が2000→2500と動いているのに一致判定されて通過していた。
        # 正しくは「前日データがある共通店舗」に当日側も限定して比較すること
        rows_new = [_row("偽陽性カード", "A店", 2000), _row("偽陽性カード", "B店", 2500)]
        rows_old = [_row("偽陽性カード", "A店", 1000), _row("偽陽性カード", "B店", 1500)]
        rows_prev = [_row("偽陽性カード", "B店", 2000)]  # 前日データはB店のみ
        result = aggregate_common_shop_movers(rows_new, rows_old, rows_prev, min_price=1000)
        assert result["stability_checked"] is True
        assert result["up"] == [] and result["down"] == [], \
            "B店が前日比で動いている(2000→2500)のに定着済みと誤判定してはいけない"

    def test_q7_true_stability_not_falsely_excluded(self):
        # 真に安定している（前日データがある店舗内では当日=前日）ケースは通ること
        rows_new = [_row("真の定着カード", "A店", 2000), _row("真の定着カード", "B店", 2000)]
        rows_old = [_row("真の定着カード", "A店", 1000), _row("真の定着カード", "B店", 1000)]
        rows_prev = [_row("真の定着カード", "B店", 2000)]  # A店は前日欠測だがB店は当日と一致
        result = aggregate_common_shop_movers(rows_new, rows_old, rows_prev, min_price=1000)
        assert result["stability_checked"] is True
        assert len(result["up"]) == 1
        assert result["up"][0]["name"] == "真の定着カード"


class TestAggregateCommonShopMoversRarity:
    """Q-1: レアリティを跨いで最安を取らない（代表レアリティに固定する）"""

    def test_cheap_rarity_going_out_of_stock_does_not_count_as_price_up(self):
        # 7日前は安いレアリティ(ノーマル)しか無く、当日は高いレアリティ(レア)しか無い
        # ケース（安いレアリティの在庫切れ）。レアリティを跨いで最安を取ると
        # 2980→3980の値上がりに見えるが、代表レアリティ固定なら「当日のレア」と
        # 「7日前のレア」を比較しようとして7日前データが無く、対象外になるべき
        rows_new = [_row("滅びの黒魔術師", "A店", 3980, rarity="レア")]
        rows_old = [_row("滅びの黒魔術師", "A店", 2980, rarity="ノーマル")]
        result = aggregate_common_shop_movers(rows_new, rows_old, min_price=1000)
        names = [e["name"] for e in result["up"] + result["down"]]
        assert "滅びの黒魔術師" not in names, "レアリティ跨ぎの見かけの値上がりを含めてはいけない"

    def test_same_rarity_both_days_is_compared_correctly(self):
        # 同一レアリティで7日前・当日とも存在すれば通常どおり比較される
        rows_new = [_row("契約を結びし竜の戦士", "A店", 2980, rarity="ノーマル")]
        rows_old = [_row("契約を結びし竜の戦士", "A店", 3480, rarity="ノーマル")]
        result = aggregate_common_shop_movers(rows_new, rows_old, min_price=1000)
        assert len(result["down"]) == 1
        entry = result["down"][0]
        assert entry["rarity"] == "ノーマル"
        assert entry["today"] == 2980 and entry["yesterday"] == 3480
        assert entry["pct"] == -14.4

    def test_representative_rarity_is_cheapest_available_today(self):
        # 当日に複数レアリティがあれば「当日最安」のレアリティを代表に選ぶ
        rows_new = [
            _row("複数レアカード", "A店", 5000, rarity="レア"),
            _row("複数レアカード", "A店", 3000, rarity="ノーマル"),
        ]
        rows_old = [
            _row("複数レアカード", "A店", 2000, rarity="ノーマル"),
            _row("複数レアカード", "A店", 6000, rarity="レア"),
        ]
        result = aggregate_common_shop_movers(rows_new, rows_old, min_price=1000)
        assert len(result["up"]) == 1
        entry = result["up"][0]
        assert entry["rarity"] == "ノーマル"
        assert entry["today"] == 3000 and entry["yesterday"] == 2000

    def test_unknown_rarity_excluded_from_representative_pick(self):
        # "(不明)"は他に候補がある限り代表レアリティの候補から除外する（ガード③相当）
        rows_new = [
            _row("不明レア混在カード", "A店", 100, rarity="(不明)"),
            _row("不明レア混在カード", "A店", 5000, rarity="ノーマル"),
        ]
        rows_old = [
            _row("不明レア混在カード", "A店", 4000, rarity="ノーマル"),
        ]
        result = aggregate_common_shop_movers(rows_new, rows_old, min_price=1000)
        assert len(result["up"]) == 1
        assert result["up"][0]["rarity"] == "ノーマル"
        assert result["up"][0]["today"] == 5000

    def test_production_expected_ranking_after_rarity_fix(self):
        # 司令塔が本番で確認した「レアリティ統一版」の期待リストをそのまま固定する。
        # 全カード同一レアリティ(ノーマル)・単一共通店舗(A店)の合成データ。
        # ノイズとして「滅びの黒魔術師」のレアリティ跨ぎ偽物も混ぜ、消えることを確認する
        expected = [
            ("契約を結びし竜の戦士", 3480, 2980, -14.4),
            ("命王の螺旋", 1480, 1280, -13.5),
            ("墓場のゴースト王－パンプキング－", 1580, 1780, 12.7),
            ("闘者を導く光", 4980, 4480, -10.0),
            ("ヘリオス・トリス・メギストス", 2080, 1880, -9.6),
            ("銀河眼の時源竜", 1180, 1080, -8.5),
            ("白き竜の落胤", 2380, 2180, -8.4),
            ("黒き竜のエクレシア", 2580, 2780, 7.8),
        ]
        rows_new = [_row(name, "A店", new, rarity="ノーマル") for name, old, new, pct in expected]
        rows_old = [_row(name, "A店", old, rarity="ノーマル") for name, old, new, pct in expected]
        # レアリティ跨ぎのノイズ（Q-1修正前は1位に出ていた偽物）
        rows_new.append(_row("滅びの黒魔術師", "A店", 3980, rarity="レア"))
        rows_old.append(_row("滅びの黒魔術師", "A店", 2980, rarity="ノーマル"))

        result = aggregate_common_shop_movers(rows_new, rows_old, min_price=1000)
        combined = sorted(result["up"] + result["down"], key=lambda e: abs(e["pct"]), reverse=True)

        assert [e["name"] for e in combined] == [name for name, *_ in expected]
        for entry, (name, old, new, pct) in zip(combined, expected):
            assert entry["today"] == new
            assert entry["yesterday"] == old
            assert entry["pct"] == pct
        assert "滅びの黒魔術師" not in [e["name"] for e in combined]
