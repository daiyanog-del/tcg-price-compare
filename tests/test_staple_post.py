"""汎用カードのリストと値動き投稿（staple_cards.py / staple_post.py）の純ロジック。"""
import os

import pytest

import staple_cards
import staple_post


def _row(name, rarity, prev, today):
    return {"card_name": name, "rarity": rarity, "prev_price": prev, "today_price": today,
            "date_old": "2026-09-25", "date_new": "2026-09-26"}


def test_select_staples_counts_distinct_themes_and_drops_theme_cards():
    recipes = [
        {"theme": "DDD", "cards": [{"name": "霆王の閃光", "is_ex": False}, {"name": "DDD怒涛壊薙王カエサル・ラグナロク", "is_ex": True}]},
        {"theme": "巳剣", "cards": [{"name": "霆王の閃光", "is_ex": False}, {"name": "霆王の閃光", "is_ex": False}]},
        {"theme": "閃刀姫", "cards": [{"name": "霆王の閃光", "is_ex": False}, {"name": "閃刀姫－レイ", "is_ex": False}]},
        {"theme": "閃刀姫", "cards": [{"name": "閃刀姫－レイ", "is_ex": False}]},
        # テーマ名の揺れで別テーマ扱いのレシピに入ったテーマ専用カード
        {"theme": "異解△", "cards": [{"name": "閃刀姫－レイ", "is_ex": False}]},
        {"theme": "レイズ・ムーン", "cards": [{"name": "閃刀姫－レイ", "is_ex": False}]},
    ]
    rows = staple_cards.select_staples(recipes, min_themes=3)
    assert [r["card_name"] for r in rows] == ["霆王の閃光"]
    assert rows[0]["themes"] == 3 and rows[0]["decks"] == 3  # 同じレシピ内の重複は1件


def test_select_staple_movers_thresholds_are_asymmetric():
    staples = {"聖王の粉砕", "ドロール＆ロックバード", "灰流うらら", "増殖するG"}
    rows = [
        _row("聖王の粉砕", "シークレット", 660, 280),        # -380 / -57.6% → 値下がり
        _row("灰流うらら", "ノーマル", 220, 320),            # +100 / +45% → 値上がり基準(+200)に届かない
        _row("ドロール＆ロックバード", "シークレット", 480, 680),  # +200 / +41.7% → 値上がり
        _row("増殖するG", "シークレット", 680, 590),          # -90 → 値下がり基準(-100)に届かない
        _row("天下独歩の大義賊", "シークレット", 9000, 5000),  # 汎用リスト外
    ]
    got = staple_post.select_staple_movers(rows, staples)
    assert [(m["name"], m["direction"]) for m in got] == [
        ("聖王の粉砕", "down"), ("ドロール＆ロックバード", "up")]


def test_select_staple_movers_boundaries_and_fuzzy_name():
    staples = {"ドロール&ロックバード"}  # 半角＆でも照合できる
    rows = [_row("ドロール＆ロックバード", "ウルトラ", 1000, 1200),   # +200 / +20.0% ちょうど
            _row("ドロール＆ロックバード", "スーパー", 1000, 850)]    # -150 / -15.0% ちょうど
    got = {(m["rarity"], m["direction"]) for m in staple_post.select_staple_movers(rows, staples)}
    assert got == {("ウルトラ", "up"), ("スーパー", "down")}


def test_format_post_text():
    m = {"name": "聖王の粉砕", "rarity": "シークレット", "today": 280, "yesterday": 660,
         "diff": -380, "pct": -57.6, "direction": "down", "date_old": "2026-09-25", "date_new": "2026-09-26"}
    lines = staple_post.format_post_text(m).split("\n")
    assert lines[0] == "【値下がり】聖王の粉砕（シークレット）"
    assert lines[1] == "¥280（9/25 ¥660から -380円）"
    assert "/card/%E8%81%96%E7%8E%8B%E3%81%AE%E7%B2%89%E7%A0%95?" in lines[2]
    assert lines[2].endswith("utm_source=x&utm_medium=social&utm_campaign=staple&utm_content=down")
    assert staple_post.content_type(m) == "staple_down|" + staple_post.fuzzy_key("聖王の粉砕") + "|シークレット"


def test_format_post_text_floor():
    """底値の型は「過去N日でいちばん安い」を2行目に出し、前日比は3行目に回す。記録と utm も floor。"""
    m = {"name": "聖王の粉砕", "rarity": "シークレット", "today": 280, "yesterday": 660, "floor_days": 90,
         "diff": -380, "pct": -57.6, "direction": "down", "date_old": "2026-09-25", "date_new": "2026-09-26"}
    lines = staple_post.format_post_text(m).split("\n")
    assert lines[0] == "【値下がり】聖王の粉砕（シークレット）"
    assert lines[1] == "¥280　過去90日でいちばん安い値段です"
    assert lines[2] == "（9/25 ¥660から -380円）"
    assert lines[3].endswith("utm_content=floor")
    assert staple_post.content_type(m).startswith("staple_floor|")
    # 値上がりは floor_days があっても底値の型にしない
    assert staple_post.post_kind({**m, "direction": "up"}) == "up"


def test_floor_control_day_uses_usual_text_but_own_kind():
    """底値に当たっても比べる相手の日（奇数日）は今までの文面で出し、型は floorctl として記録する。"""
    m = {"name": "聖王の粉砕", "rarity": "シークレット", "today": 280, "yesterday": 660, "floor_days": 90,
         "floor_show": False, "diff": -380, "pct": -57.6, "direction": "down",
         "date_old": "2026-09-25", "date_new": "2026-09-26"}
    lines = staple_post.format_post_text(m).split("\n")
    assert lines[1] == "¥280（9/25 ¥660から -380円）"
    assert lines[2].endswith("utm_content=floorctl")
    assert staple_post.content_type(m).startswith("staple_floorctl|")
    # 偶数日と奇数日が1日おきに入れ替わる
    days = [staple_post.is_floor_day(f"2026-10-{d:02d}") for d in range(25, 31)]
    assert days == [days[0], not days[0]] * 3


def _hist(start, days, price):
    from datetime import date, timedelta
    d0 = date.fromisoformat(start)
    return [((d0 + timedelta(days=i)).isoformat(), price) for i in range(days)]


def test_floor_days_picks_longest_window_and_needs_records_from_its_start():
    m = {"today": 480, "date_new": "2026-10-29"}
    # 7/31〜10/28 ずっと ¥600 … 90日でいちばん安い
    assert staple_post.floor_days(_hist("2026-07-31", 90, 600), m) == 90
    # 同じ値段が前にあっても「いちばん安い」と言える
    assert staple_post.floor_days(_hist("2026-07-31", 90, 480), m) == 90
    # 8/20 に ¥450 があった … 90日では負け、9/29〜の30日なら最安
    h = _hist("2026-07-31", 90, 600)
    h[20] = ("2026-08-20", 450)
    assert staple_post.floor_days(h, m) == 30
    # 30日の中に安い日がある … どちらでもない
    h[80] = ("2026-10-19", 470)
    assert staple_post.floor_days(h, m) is None
    # 記録が9/15から … 90日は名乗らず30日
    assert staple_post.floor_days(_hist("2026-09-15", 44, 600), m) == 30
    # 記録が10/10から … どちらも名乗らない
    assert staple_post.floor_days(_hist("2026-10-10", 19, 600), m) is None
    # 判定日とその後の値段は比べる相手に入れない
    assert staple_post.floor_days(_hist("2026-07-31", 90, 600) + [("2026-10-29", 300)], m) == 90
    assert staple_post.floor_days([], m) is None


def test_align_history_uses_judged_prices_for_last_two_days():
    m = {"today": 280, "yesterday": 660, "date_old": "2026-09-25", "date_new": "2026-09-26"}
    hist = [("2026-09-24", 680), ("2026-09-25", 640), ("2026-09-26", 250)]
    assert staple_post.align_history(hist, m) == [
        ("2026-09-24", 680), ("2026-09-25", 660), ("2026-09-26", 280)]


@pytest.mark.skipif(not os.environ.get("STAPLE_FONT_DIR"), reason="STAPLE_FONT_DIR 未設定（フォントが無い）")
def test_render_post_image(tmp_path):
    from PIL import Image
    card = tmp_path / "card.png"
    Image.new("RGB", (256, 372), (120, 40, 90)).save(card)
    m = {"name": "カオス・アンヘル－混沌の双翼－", "rarity": "レア", "today": 780, "yesterday": 1000,
         "diff": -220, "pct": -22.0, "direction": "down", "date_old": "2026-10-19", "date_new": "2026-10-20"}
    hist = [("2026-07-22", 1500), ("2026-08-30", 1000), ("2026-10-19", 1000), ("2026-10-20", 780)]
    out = staple_post.render_post_image(str(card), m, hist, str(tmp_path / "out.png"))
    assert Image.open(out).size == (1200, 675)


@pytest.mark.skipif(not os.environ.get("STAPLE_FONT_DIR"), reason="STAPLE_FONT_DIR 未設定（フォントが無い）")
def test_name_font_falls_back_when_glyph_missing():
    pytest.importorskip("fontTools")
    jp, _num, fallback = staple_post._font_paths()
    assert staple_post.name_font_path("カオス・アンヘル－混沌の双翼－", jp, fallback) == jp
    # M PLUS 2 に無い字だけを予備へ回す（どちらにも無い字はそのまま M PLUS 2）
    missing = [c for c in "𠮟鷗髙﨑" if staple_post._missing_chars(jp, c)]
    for c in missing:
        if fallback and not staple_post._missing_chars(fallback, c):
            assert staple_post.name_font_path("名" + c, jp, fallback) == fallback


# ── 投稿直前の取り直し（2026-10-24） ──

def _down(today=280, yesterday=660):
    return {"name": "聖王の粉砕", "rarity": "シークレット", "today": today, "yesterday": yesterday,
            "diff": today - yesterday, "pct": round((today - yesterday) / yesterday * 100, 1),
            "direction": "down", "date_old": "2026-09-25", "date_new": "2026-09-26"}


def _item(shop, price, rarity="シークレット", **kw):
    return {"shop": shop, "name": "聖王の粉砕", "rarity": rarity, "price": price,
            "condition": kw.get("condition", "-"), "sold_out": kw.get("sold_out", False),
            "code": "", "url": ""}


def _fake_fetch(per_shop, errors=()):
    """shop -> 出品リスト。errors に入れた店は取得失敗（0件＋fetch_errors）を返す。"""
    calls = []

    def fetch(name, shop_names=None, status_out=None):
        shop = shop_names[0]
        calls.append(shop)
        if shop in errors:
            status_out[shop] = {"count": 0, "fetch_errors": 1}
            return []
        items = per_shop.get(shop, [])
        status_out[shop] = {"count": len(items), "fetch_errors": 0}
        return items
    fetch.calls = calls
    return fetch


def test_recheck_price_uses_normal_condition_and_skips_sold_out_and_other_rarity():
    fetch = _fake_fetch({"カードラッシュ": [
        _item("カードラッシュ", 200, condition="〔状態B〕"),   # 状態難は通常品から外す（収集と同じ）
        _item("カードラッシュ", 150, sold_out=True),          # 売り切れ
        _item("カードラッシュ", 100, rarity="ノーマル"),      # 別レアリティ
        _item("カードラッシュ", 280),
    ]})
    price, shop, _ = staple_post.recheck_price(_down(), ["カードラッシュ"], fetch=fetch)
    assert (price, shop) == (280, "カードラッシュ")


def test_recheck_price_fetch_error_and_no_stock_are_none():
    fetch = _fake_fetch({"遊々亭": []}, errors={"カードラッシュ"})
    price, shop, why = staple_post.recheck_price(_down(), ["カードラッシュ", "遊々亭"], fetch=fetch)
    assert (price, shop) == (None, None)
    assert fetch.calls == ["カードラッシュ", "遊々亭"]
    assert "カードラッシュ:取得失敗" in why and "遊々亭:そのレアリティの通常品の在庫なし" in why


def test_recheck_price_ignores_damaged_only_stock():
    """通常品が売り切れて状態難だけ残ったら「今その値段で買える」とは言わない（収集の代用とは違う）"""
    fetch = _fake_fetch({"カードラッシュ": [_item("カードラッシュ", 200, condition="〔状態B〕")]})
    assert staple_post.recheck_price(_down(), ["カードラッシュ"], fetch=fetch)[:2] == (None, None)


def test_recheck_price_shop_exception_and_skipped_shop():
    def boom(name, shop_names=None, status_out=None):
        raise RuntimeError("timeout")
    assert "取得失敗(timeout)" in staple_post.recheck_price(_down(), ["遊々亭"], fetch=boom)[2]

    def silent(name, shop_names=None, status_out=None):   # 休止中の店は status_out に載らない
        return []
    assert staple_post.recheck_price(_down(), ["カーナベル"], fetch=silent) == (None, None, "カーナベル:取得対象外")


def test_recheck_price_takes_cheaper_of_tied_shops():
    fetch = _fake_fetch({"カードラッシュ": [_item("カードラッシュ", 300)],
                         "遊々亭": [_item("遊々亭", 260)]})
    assert staple_post.recheck_price(_down(), ["カードラッシュ", "遊々亭"], fetch=fetch)[:2] == (260, "遊々亭")


def test_apply_recheck_rebuilds_or_drops_candidate():
    m = _down(today=280, yesterday=660)
    cheaper = staple_post.apply_recheck(m, 250)
    assert (cheaper["today"], cheaper["diff"], cheaper["pct"]) == (250, -410, -62.1)
    assert cheaper["yesterday"] == 660 and cheaper["date_old"] == "2026-09-25"
    assert staple_post.apply_recheck(m, 280)["today"] == 280      # 同じ値段は合格
    # 判定時より高い: 前日比の基準は満たしても（¥500 は -160円/-24%）、ほかの店のほうが安いかもしれない
    assert staple_post.apply_recheck(m, 500) is None
    assert staple_post.apply_recheck(m, None) is None


def test_apply_recheck_drops_when_threshold_no_longer_met():
    """判定時以下でも基準を割ったら落とす（前日 ¥660・判定時 ¥560 → 取り直し ¥565 は高いので別理由、
    ¥560 未満で -100円に届かない値は無いので、前日 ¥700・判定時 ¥600 の候補で確かめる）"""
    m = _down(today=600, yesterday=700)      # -100円 / -14.3% … そもそも % が足りない形を作る
    assert staple_post.apply_recheck(m, 600) is None
    m = _down(today=580, yesterday=700)      # -120円 / -17.1% … 合格
    assert staple_post.apply_recheck(m, 580) is not None


class _FakeQuery:
    def __init__(self, data, fail=False):
        self.data, self.fail = data, fail

    def select(self, *a):
        return self

    def eq(self, *a):
        return self

    def order(self, *a):
        return self

    def gte(self, *a):
        return self

    def lte(self, *a):
        return self

    def range(self, *a):
        return self

    def execute(self):
        if self.fail:
            raise RuntimeError("boom")
        return self


class _FakeSb:
    def __init__(self, rows, fail=False):
        self.rows, self.fail = rows, fail

    def table(self, name):
        return _FakeQuery(self.rows, self.fail)


def test_cheapest_shops_returns_shops_at_judged_price():
    rows = [{"shop": "カードラッシュ", "min_price": 280}, {"shop": "遊々亭", "min_price": 300},
            {"shop": "トレコロCB", "min_price": 280}]
    assert staple_post.cheapest_shops(_FakeSb(rows), _down()) == ["カードラッシュ", "トレコロCB"]
    assert staple_post.cheapest_shops(_FakeSb([], fail=True), _down()) == []


def test_post_staple_movers_skips_down_when_recheck_fails(monkeypatch):
    """取り直しで確かめられない値下がりは投稿せず次点へ。RPC には min_down_shops=1 を渡す。"""
    rpc_args = {}

    class _Rpc:
        def execute(self):
            return type("R", (), {"data": [
                _row("聖王の粉砕", "シークレット", 660, 280),
                _row("増殖するG", "シークレット", 900, 700)]})()

    class _Sb:
        def rpc(self, name, args):
            rpc_args.update(args)
            return _Rpc()

    posted = []
    monkeypatch.setenv("X_POST_DRY_RUN", "1")
    monkeypatch.setattr(staple_post.staple_cards, "load_staple_names", lambda sb: {"聖王の粉砕", "増殖するG"})
    monkeypatch.setattr(staple_post, "_recently_posted", lambda sb, m: False)
    monkeypatch.setattr(staple_post, "cheapest_shops", lambda sb, m: ["カードラッシュ"])
    monkeypatch.setattr(staple_post, "week_low", lambda sb, m: None)
    monkeypatch.setattr(staple_post, "recheck_price",
                        lambda m, shops: (None, None, "x") if m["name"] == "聖王の粉砕" else (690, "カードラッシュ", "x"))
    monkeypatch.setattr(staple_post, "fetch_history", lambda sb, name, rarity, d: [])
    monkeypatch.setattr(staple_post.x_poster, "get_card_image_path", lambda name: posted.append(name) or None)
    staple_post.post_staple_movers(_Sb())
    assert rpc_args["min_down_shops"] == 1
    # 1件目（聖王の粉砕）は取り直し不合格で次点へ。2件目は ¥690（-210円/-23.3%）で合格し画像取得へ進む
    assert posted == ["増殖するG"]


def test_post_staple_movers_does_not_recheck_up_and_stops_after_one(monkeypatch, tmp_path):
    """値上がりは取り直さない。合格した値下がりを1件投稿しに行ったら次点へ進まない。"""
    class _Rpc:
        def execute(self):
            return type("R", (), {"data": [
                _row("聖王の粉砕", "シークレット", 660, 280),
                _row("増殖するG", "シークレット", 900, 700),
                _row("ドロール＆ロックバード", "シークレット", 480, 680)]})()

    class _Sb:
        def rpc(self, name, args):
            return _Rpc()

    rechecked, tweeted = [], []

    def card_path(name):
        p = tmp_path / f"{len(tweeted)}_{len(rechecked)}.png"
        p.write_bytes(b"x")
        return str(p)

    monkeypatch.setenv("X_POST_DRY_RUN", "1")
    monkeypatch.chdir(tmp_path)   # 試し実行の画像置き場（PREVIEW_DIR）を一時フォルダへ
    monkeypatch.setattr(staple_post.staple_cards, "load_staple_names",
                        lambda sb: {"聖王の粉砕", "増殖するG", "ドロール＆ロックバード"})
    monkeypatch.setattr(staple_post, "_recently_posted", lambda sb, m: False)
    monkeypatch.setattr(staple_post, "cheapest_shops", lambda sb, m: ["カードラッシュ"])
    monkeypatch.setattr(staple_post, "week_low", lambda sb, m: None)
    monkeypatch.setattr(staple_post, "recheck_price",
                        lambda m, shops: rechecked.append(m["name"]) or (m["today"], "カードラッシュ", "x"))
    monkeypatch.setattr(staple_post.x_poster, "get_card_image_path", card_path)
    monkeypatch.setattr(staple_post, "fetch_history", lambda sb, name, rarity, d: [])
    monkeypatch.setattr(staple_post, "render_post_image", lambda c, m, h, out: None)
    monkeypatch.setattr(staple_post.x_poster, "post_tweet",
                        lambda text, image_paths=None, require_media=False: tweeted.append(text.splitlines()[0]))
    staple_post.post_staple_movers(_Sb())
    assert rechecked == ["聖王の粉砕"]
    assert tweeted == ["【値下がり】聖王の粉砕（シークレット）", "【値上がり】ドロール＆ロックバード（シークレット）"]


def test_week_low_ignores_prices_below_10_yen():
    # 10円未満は異常値として見ない。¥10 ちょうどは本物の値段として数える（2026-10-29〜）
    rows = [{"min_price": 400, "recorded_at": "2026-09-20", "shop": "遊々亭"},
            {"min_price": 300, "recorded_at": "2026-09-22", "shop": "カードラッシュ"},
            {"min_price": 9, "recorded_at": "2026-09-23", "shop": "トレコロCB"}]
    assert staple_post.week_low(_FakeSb(rows), _down()) == 300
    rows.append({"min_price": 10, "recorded_at": "2026-09-24", "shop": "カーナベル"})
    assert staple_post.week_low(_FakeSb(rows), _down()) == 10
    assert staple_post.week_low(_FakeSb([]), _down()) is None
    with pytest.raises(RuntimeError):
        staple_post.week_low(_FakeSb([], fail=True), _down())


def test_post_staple_movers_skips_return_to_usual_price(monkeypatch):
    """直近7日に今日以下の値段があった値下がりは採らない。照会失敗も見送り。取り直しまで進まない。"""
    class _Rpc:
        def execute(self):
            return type("R", (), {"data": [
                _row("聖王の粉砕", "シークレット", 660, 280),       # 直近に ¥280 があった → 戻っただけ
                _row("増殖するG", "シークレット", 900, 700),         # 照会失敗 → 見送り
                _row("灰流うらら", "シークレット", 1000, 800)]})()   # 直近の最安 ¥850 を下回る → 取り直しへ

    class _Sb:
        def rpc(self, name, args):
            return _Rpc()

    def low(sb, m):
        if m["name"] == "増殖するG":
            raise RuntimeError("boom")
        return {"聖王の粉砕": 280, "灰流うらら": 850}[m["name"]]

    rechecked = []
    monkeypatch.setenv("X_POST_DRY_RUN", "1")
    monkeypatch.setattr(staple_post.staple_cards, "load_staple_names", lambda sb: {"聖王の粉砕", "増殖するG", "灰流うらら"})
    monkeypatch.setattr(staple_post, "_recently_posted", lambda sb, m: False)
    monkeypatch.setattr(staple_post, "week_low", low)
    monkeypatch.setattr(staple_post, "cheapest_shops", lambda sb, m: ["カードラッシュ"])
    monkeypatch.setattr(staple_post, "recheck_price",
                        lambda m, shops: rechecked.append(m["name"]) or (None, None, "x"))
    staple_post.post_staple_movers(_Sb())
    assert rechecked == ["灰流うらら"]


def test_recently_posted_counts_other_kind_of_same_direction():
    """底値（floor）と今までの形（down）は同じ向きなので、どちらを出していても7日は同じカードを出さない。"""
    seen = {}

    class _Q:
        def select(self, *a):
            return self

        def in_(self, col, vals):
            seen["types"] = vals
            return self

        def gte(self, *a):
            return self

        def limit(self, *a):
            return self

        def execute(self):
            return type("R", (), {"data": []})()

    class _Sb:
        def table(self, name):
            return _Q()

    m = {"name": "聖王の粉砕", "rarity": "シークレット", "direction": "down", "floor_days": 90}
    assert staple_post._recently_posted(_Sb(), m) is False
    key = staple_post.fuzzy_key("聖王の粉砕") + "|シークレット"
    assert seen["types"] == ["staple_floor|" + key, "staple_floorctl|" + key, "staple_down|" + key]
    staple_post._recently_posted(_Sb(), {**m, "direction": "up"})
    assert seen["types"] == ["staple_up|" + key]
