"""
tests/test_collect_featured.py — collect_featured.py の純関数テスト

対象:
  - _is_shop_failed: (card, shop) 1回の取得試行の失敗判定（4分岐）
  - classify_blocklike_shops: 店舗ごとの失敗率によるブロック様判定
  - _group_pairs_by_card: (card, shop) ペアのカード単位グルーピング

ネットワーク・実Supabaseは一切使わない。
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from collect_featured import _is_shop_failed, classify_blocklike_shops, _group_pairs_by_card


# ──────────────────────────────────────────────
# _is_shop_failed（4分岐）
# ──────────────────────────────────────────────

class TestIsShopFailed:
    def test_status_none_is_failed(self):
        # compare_prices が結果を書き込まなかった（想定外）
        assert _is_shop_failed(None) is True

    def test_exception_present_is_failed(self):
        assert _is_shop_failed({"count": 0, "fetch_errors": 0, "exception": "boom"}) is True

    def test_fetch_errors_with_zero_count_is_failed(self):
        assert _is_shop_failed({"count": 0, "fetch_errors": 2}) is True

    def test_count_positive_with_fetch_errors_is_not_failed(self):
        # 一部エラーがあっても結果が得られていれば「取得できた」扱い
        assert _is_shop_failed({"count": 3, "fetch_errors": 1}) is False

    def test_zero_errors_zero_count_is_not_failed(self):
        # 0件かつエラーなし＝在庫なし。取得失敗ではない
        assert _is_shop_failed({"count": 0, "fetch_errors": 0}) is False


# ──────────────────────────────────────────────
# classify_blocklike_shops
# ──────────────────────────────────────────────

class TestClassifyBlocklikeShops:
    def test_shop_at_threshold_is_blocklike(self):
        failed = [("カードA", "カードラボ"), ("カードB", "カードラボ"), ("カードC", "カードラボ")]
        attempted = {"カードラボ": 10}  # 3/10=30% ちょうど閾値
        result = classify_blocklike_shops(failed, attempted, threshold=0.30)
        assert result == {"カードラボ"}

    def test_shop_under_threshold_is_not_blocklike(self):
        failed = [("カードA", "遊々亭")]
        attempted = {"遊々亭": 10}  # 1/10=10%
        result = classify_blocklike_shops(failed, attempted, threshold=0.30)
        assert result == set()

    def test_mixed_shops_classified_independently(self):
        failed = [
            ("カードA", "カードラボ"), ("カードB", "カードラボ"), ("カードC", "カードラボ"),
            ("カードA", "遊々亭"),
        ]
        attempted = {"カードラボ": 10, "遊々亭": 10}
        result = classify_blocklike_shops(failed, attempted, threshold=0.30)
        assert result == {"カードラボ"}

    def test_zero_attempted_shop_is_not_blocklike(self):
        # attempted=0（試行数不明）はゼロ除算を避け、ブロック様と判定しない
        failed = [("カードA", "遊々亭")]
        result = classify_blocklike_shops(failed, {}, threshold=0.30)
        assert result == set()


# ──────────────────────────────────────────────
# _group_pairs_by_card
# ──────────────────────────────────────────────

class TestGroupPairsByCard:
    def test_groups_shops_under_card_preserving_order(self):
        pairs = [("カードA", "遊々亭"), ("カードA", "カードラボ"), ("カードB", "遊々亭")]
        result = _group_pairs_by_card(pairs)
        assert result == {"カードA": ["遊々亭", "カードラボ"], "カードB": ["遊々亭"]}


# ──────────────────────────────────────────────
# トレコロCBの当日取得済みカードを第1パスから外す（2026-10-14）
# ──────────────────────────────────────────────

import collect_featured
from collect_featured import fetch_cards_collected_today


class _FakeQuery:
    """_fetch_all_rows が使う select().eq().eq().order().range().execute() のチェーンを模す"""

    def __init__(self, rows, log):
        self._rows = rows
        self._log = log
        self._range = (0, 999)

    def select(self, *_):
        return self

    def eq(self, col, val):
        self._log.append((col, val))
        return self

    def order(self, *_):
        return self

    def range(self, a, b):
        self._range = (a, b)
        return self

    def execute(self):
        a, b = self._range
        return type("R", (), {"data": self._rows[a:b + 1]})()


class _FakeSb:
    def __init__(self, rows):
        self.rows = rows
        self.log = []

    def table(self, name):
        assert name == "price_history"
        return _FakeQuery(self.rows, self.log)


class TestFetchCardsCollectedToday:
    def test_returns_only_target_cards_with_rows(self):
        sb = _FakeSb([{"card_name": "A"}, {"card_name": "B"}, {"card_name": "Z"}])
        got = fetch_cards_collected_today(sb, ["A", "C", "B"], "2026-10-14", "トレコロCB")
        assert got == {"A", "B"}
        # 店舗と日付で絞っている
        assert ("shop", "トレコロCB") in sb.log and ("recorded_at", "2026-10-14") in sb.log

    def test_paginates_beyond_1000_rows(self):
        rows = [{"card_name": f"C{i}"} for i in range(2500)]
        got = fetch_cards_collected_today(_FakeSb(rows), ["C0", "C1500", "C2499"], "2026-10-14", "トレコロCB")
        assert got == {"C0", "C1500", "C2499"}

    def test_query_failure_falls_back_to_empty_set(self):
        class Boom:
            def table(self, *_):
                raise RuntimeError("db down")
        assert fetch_cards_collected_today(Boom(), ["A"], "2026-10-14", "トレコロCB") == set()


class TestMainSkipsCollectedTorecolo:
    def test_first_pass_excludes_torecolo_only_for_collected_cards(self, monkeypatch):
        calls = []  # (card, shops)

        def fake_collect(sb, card, today, shops, shop_stats=None, db_guard=None, pack_code="", art_contexts=None):
            calls.append((card, list(shops)))
            return 0, []

        monkeypatch.setattr(sys, "argv", ["collect_featured.py"])
        monkeypatch.setattr(collect_featured, "get_supabase", lambda: object())
        monkeypatch.setattr(collect_featured, "_resolve_pack", lambda sb, a: {"pack_name": "P"})
        monkeypatch.setattr(collect_featured, "get_featured_cards", lambda sb, p: ["A", "B", "C"])
        monkeypatch.setattr(collect_featured, "check_shop_availability", lambda shops: ["カードラッシュ", "トレコロCB"])
        monkeypatch.setattr(collect_featured, "fetch_cards_collected_today",
                            lambda sb, names, today, shop: {"A", "B"})
        monkeypatch.setattr(collect_featured, "collect_and_save_for_shops", fake_collect)
        monkeypatch.setattr(collect_featured, "resolve_pack_head", lambda pack: "IMPH")
        monkeypatch.setattr(collect_featured.time, "sleep", lambda *_: None)

        collect_featured.main()

        by_card = dict(calls)
        assert by_card["A"] == ["カードラッシュ"], "取得済みカードはトレコロを外す・他店は変えない"
        assert by_card["B"] == ["カードラッシュ"]
        assert by_card["C"] == ["カードラッシュ", "トレコロCB"], "行が無いカードは従来どおり取る"


class TestBlocklikeMinAttempted:
    def test_small_denominator_is_not_blocklike(self):
        # 1/1 失敗（100%）でも試行数が下限未満ならブロック様にしない
        assert classify_blocklike_shops([("A", "トレコロCB")], {"トレコロCB": 1}) == set()
        assert classify_blocklike_shops([("A", "S")] * 4, {"S": 4}) == set()

    def test_at_min_attempted_is_judged(self):
        from collect_featured import BLOCKLIKE_MIN_ATTEMPTED
        pairs = [(f"C{i}", "S") for i in range(BLOCKLIKE_MIN_ATTEMPTED)]
        assert classify_blocklike_shops(pairs, {"S": BLOCKLIKE_MIN_ATTEMPTED}) == {"S"}


class TestAttemptedDenominatorExcludesSkipped:
    def test_failures_ratio_uses_actually_attempted_cards(self, monkeypatch):
        """トレコロを外した分は分母に入れない: 10枚中6枚取得済み→試行4枚（下限未満）なので
        4枚全失敗でもブロック様（長時間待ち）にならず、即時再試行へ回る"""
        cards = [f"C{i}" for i in range(10)]
        done = set(cards[:6])
        sleeps = []
        retried = []

        def fake_collect(sb, card, today, shops, shop_stats=None, db_guard=None, pack_code="", art_contexts=None):
            if "トレコロCB" in shops and card not in done:
                retried.append((card, list(shops)))
                return 0, ["トレコロCB"]
            return 0, []

        monkeypatch.setattr(sys, "argv", ["collect_featured.py"])
        monkeypatch.setattr(collect_featured, "get_supabase", lambda: object())
        monkeypatch.setattr(collect_featured, "_resolve_pack", lambda sb, a: {"pack_name": "P"})
        monkeypatch.setattr(collect_featured, "get_featured_cards", lambda sb, p: cards)
        monkeypatch.setattr(collect_featured, "check_shop_availability", lambda shops: ["トレコロCB"])
        monkeypatch.setattr(collect_featured, "fetch_cards_collected_today", lambda *a: done)
        monkeypatch.setattr(collect_featured, "collect_and_save_for_shops", fake_collect)
        monkeypatch.setattr(collect_featured, "resolve_pack_head", lambda pack: "IMPH")
        monkeypatch.setattr(collect_featured.time, "sleep", lambda s: sleeps.append(s))

        collect_featured.main()

        assert all(s < collect_featured.RETRY_WAIT_SEC - 100 for s in sleeps), "長時間待ちが発生した"
        # 第1パス4件 + 即時再試行ラウンド(最大2回)分
        assert len(retried) >= 8
        assert all(c not in done for c, _ in retried), "取得済みカードは試行されない"


# ──────────────────────────────────────────────
# 絵ごとの内訳の入れ直し（2026-11-01: 本表の上書きで消えた price_history_art を新弾の収集が書いていなかった）
# ──────────────────────────────────────────────

def _art_item(shop, name, price, rarity="シークレット", code="IMPH-JP001"):
    return {"shop": shop, "name": name, "code": code, "rarity": rarity, "price": price,
            "sold_out": False, "stock": 1, "condition": "-", "url": "", "image": ""}


class TestArtRowsRewritten:
    def _patch(self, monkeypatch, items, calls):
        import collect_featured

        def fake_compare(name, shop_names=None, status_out=None):
            status_out.update({s: {"count": 1, "fetch_errors": 0} for s in shop_names})
            return [it for it in items if it["shop"] in shop_names]

        monkeypatch.setattr(collect_featured, "compare_prices", fake_compare)
        monkeypatch.setattr(collect_featured, "upsert_price_rows",
                            lambda sb, rows, raise_on_error=False: calls.append(("main", rows)) or len(rows))
        monkeypatch.setattr(collect_featured, "upsert_price_art_rows",
                            lambda sb, rows: calls.append(("art", rows)) or len(rows))
        return collect_featured

    def test_split_rarity_is_written_after_main(self, monkeypatch):
        """絵が分かれるレアリティは、本表を書いた後に店×絵ごとの行を書く（本表の上書きで消えた分を入れ直す）"""
        import art_variant as av
        calls = []
        cf = self._patch(monkeypatch, [_art_item("遊々亭", "篝火", 500),
                                       _art_item("遊々亭", "篝火(イラスト違い版)", 9980)], calls)
        saved, failed = cf.collect_and_save_for_shops(None, "篝火", "2026-11-01", ["遊々亭"])
        assert saved == 1 and failed == []
        assert [kind for kind, _ in calls] == ["main", "art"]
        art = {(r["shop"], r["rarity"], r["art"]): r["min_price"] for r in calls[1][1]}
        assert art == {("遊々亭", "シークレット", av.ART_BASE): 500, ("遊々亭", "シークレット", av.ART_ALT): 9980}

    def test_retry_of_one_shop_uses_first_pass_context(self, monkeypatch):
        """再試行で1店だけ取り直すときも、第1パスでほかの店の行から分かった「絵が分かれるレアリティ」にそろえる
        （この店の行だけでは分かれないので、文脈が無いと消えたまま入れ直されない）"""
        calls = []
        items = [_art_item("遊々亭", "篝火", 500), _art_item("遊々亭", "篝火(イラスト違い版)", 9980),
                 _art_item("カードラッシュ", "篝火", 450)]
        cf = self._patch(monkeypatch, items, calls)
        contexts: dict = {}
        cf.collect_and_save_for_shops(None, "篝火", "2026-11-01", ["遊々亭"], art_contexts=contexts)
        assert contexts["篝火"]["rarities"] == {"シークレット"}

        calls.clear()
        cf.collect_and_save_for_shops(None, "篝火", "2026-11-01", ["カードラッシュ"], art_contexts=contexts)
        import art_variant as av
        # 同じ型番に元と違う絵が並ぶので、文脈の違う絵の型番に入れない（入れるとカードラッシュの元の絵が違う絵になる）
        assert "IMPH-JP001" not in contexts["篝火"]["alt_codes"]
        assert [(r["shop"], r["art"], r["min_price"]) for r in calls[1][1]] == [("カードラッシュ", av.ART_BASE, 450)]
        # 文脈が無ければ書かない（1店の元の絵だけ＝本表と同じ値）
        calls.clear()
        cf.collect_and_save_for_shops(None, "篝火", "2026-11-01", ["カードラッシュ"])
        assert calls[1] == ("art", [])
        # 取り直した店の結果で文脈が狭まらない
        assert contexts["篝火"]["rarities"] == {"シークレット"}

    def test_art_is_not_written_when_main_fails(self, monkeypatch):
        """本表の保存が失敗したら（本表の行も別表の行も変わっていない）別表は書かない"""
        import collect_featured as cf
        from db_write_guard import ConsecutiveDbFailureGuard
        calls = []
        self._patch(monkeypatch, [_art_item("遊々亭", "篝火", 500),
                                  _art_item("遊々亭", "篝火(イラスト違い版)", 9980)], calls)

        def boom(sb, rows, raise_on_error=False):
            raise RuntimeError("400 bad request")

        monkeypatch.setattr(cf, "upsert_price_rows", boom)
        saved, _ = cf.collect_and_save_for_shops(None, "篝火", "2026-11-01", ["遊々亭"],
                                                 db_guard=ConsecutiveDbFailureGuard())
        assert saved == 0 and calls == []


class _ArtQuery:
    def __init__(self, table, data):
        self.table, self.data, self.filters = table, data, {}

    def select(self, *_a):
        return self

    def in_(self, col, vals):
        self.filters[col] = set(vals)
        return self

    def eq(self, col, val):
        self.filters[col] = {val}
        return self

    def order(self, *_a, **_k):
        return self

    def range(self, *_a):
        return self

    def execute(self):
        rows = [r for r in self.data.get(self.table, [])
                if all(r.get(c) in v for c, v in self.filters.items())]
        return type("R", (), {"data": rows})()


class _ArtSb:
    def __init__(self, data):
        self.data = data

    def table(self, name):
        return _ArtQuery(name, self.data)


def test_fetch_art_contexts_today_builds_rarities_only():
    """当日の絵ごとの内訳から、カードごとの絵が分かれるレアリティを作る"""
    from price_history_reads import fetch_art_contexts_today
    sb = _ArtSb({
        "card_dim": [{"card_id": 1, "card_name": "篝火"}, {"card_id": 2, "card_name": "ラーの翼神竜"}],
        "rarity_dim": [{"rarity_id": 10, "rarity": "シークレット"}, {"rarity_id": 11, "rarity": "ウルトラ"}],
        "art_dim": [{"art_id": 1, "art": "元のイラスト"}, {"art_id": 3, "art": "イラスト違い"}],
        "price_history_art": [
            {"card_id": 1, "rarity_id": 10, "art_id": 1, "code": "IMPH-JP001", "recorded_at": "2026-11-01"},
            {"card_id": 1, "rarity_id": 10, "art_id": 3, "code": "IMPH-JP101", "recorded_at": "2026-11-01"},
            {"card_id": 1, "rarity_id": 11, "art_id": 1, "code": "IMPH-JP001", "recorded_at": "2026-11-01"},
            {"card_id": 1, "rarity_id": 11, "art_id": 3, "code": "IMPH-JP001", "recorded_at": "2026-11-01"},
            {"card_id": 2, "rarity_id": 11, "art_id": 3, "code": "X-JP001", "recorded_at": "2026-10-31"},
        ],
    })
    got = fetch_art_contexts_today(sb, ["篝火", "ラーの翼神竜"], "2026-11-01", "t")
    # 違う絵の型番は渡さない（当日の内訳からは1つの型番に元と違う絵が並ぶかを見分けられない）。前日の行は使わない
    assert got == {"篝火": {"rarities": {"シークレット", "ウルトラ"}, "alt_codes": set()}}


def test_fetch_art_contexts_today_returns_empty_when_unreadable():
    from price_history_reads import fetch_art_contexts_today
    assert fetch_art_contexts_today(object(), ["篝火"], "2026-11-01", "t") == {}


def test_main_passes_same_art_contexts_to_first_pass_and_retries(monkeypatch):
    """当日の内訳から作った文脈を第1パスに渡し、再試行にも同じ dict を渡す（再試行で第1パスの分かったことを使う）"""
    import collect_featured
    import price_history_reads
    seen = []  # (card, shops, id(art_contexts), 文脈のコピー)

    def fake_collect(sb, card, today, shops, shop_stats=None, db_guard=None, pack_code="", art_contexts=None):
        seen.append((card, list(shops), id(art_contexts), dict(art_contexts)))
        if len(seen) == 1:
            art_contexts["B"] = {"rarities": {"ウルトラ"}, "alt_codes": set()}
            return 0, ["カードラッシュ"]
        return 1, []

    monkeypatch.setattr(sys, "argv", ["collect_featured.py"])
    monkeypatch.setattr(collect_featured, "get_supabase", lambda: object())
    monkeypatch.setattr(collect_featured, "_resolve_pack", lambda sb, a: {"pack_name": "P"})
    monkeypatch.setattr(collect_featured, "get_featured_cards", lambda sb, p: ["B"])
    monkeypatch.setattr(collect_featured, "check_shop_availability", lambda shops: ["カードラッシュ"])
    monkeypatch.setattr(collect_featured, "fetch_cards_collected_today", lambda *a: set())
    monkeypatch.setattr(collect_featured, "collect_and_save_for_shops", fake_collect)
    monkeypatch.setattr(collect_featured, "resolve_pack_head", lambda pack: "IMPH")
    monkeypatch.setattr(collect_featured.time, "sleep", lambda *_: None)
    monkeypatch.setattr(price_history_reads, "fetch_art_contexts_today",
                        lambda sb, names, today, tag: {"B": {"rarities": {"シークレット"}, "alt_codes": set()}})

    collect_featured.main()

    assert seen[0][3] == {"B": {"rarities": {"シークレット"}, "alt_codes": set()}}, "当日の内訳の文脈を第1パスに渡す"
    assert len(seen) >= 2 and seen[1][1] == ["カードラッシュ"]
    assert seen[1][2] == seen[0][2], "再試行にも同じ dict を渡す"
    assert seen[1][3]["B"]["rarities"] == {"ウルトラ"}, "第1パスで書いた文脈を再試行で読める"
