"""トップの汎用カード列（staple_ranking.py）の並べ方と API。"""
import app as app_module
import staple_ranking


def _row(name, themes, now, old, new, stable=True, rarity="ウルトラ"):
    return {"card_name": name, "themes": themes, "rarity": rarity, "price_now": now,
            "price_old": old, "price_new": new, "stable": stable,
            "date_new": "2026-10-21", "date_old": "2026-10-14"}


def test_drop_sorted_by_yen_and_excludes_unstable_and_incomparable():
    rows = [
        _row("A", 10, 5780, 5980, 5780),          # -200
        _row("B", 3, 20, 50, 20),                  # -30（割合は大きいが金額は小さい）
        _row("C", 15, 3280, 3980, 3280, stable=False),  # 定着しない＝在庫入替などの一時的な動き
        _row("D", 40, 480, None, None),            # 比べられない（共通店舗なし）
        _row("E", 5, 700, 600, 700),               # 値上がりは値下がり順に入らない
    ]
    got = staple_ranking.build_lists(rows)
    assert [i["name"] for i in got["drop"]] == ["A", "B"]
    assert got["drop"][0]["diff"] == -200 and got["drop"][0]["price"] == 5780


def test_themes_order_and_display_price_rules():
    rows = [
        _row("増殖するG", 42, 80, 80, 80),
        _row("グリフォー", 5, 120, 266, 180),          # 全店の最安が共通店舗の外＝値段は全店の最安・変化は出さない
        _row("天救竜", 10, 5780, 5980, 5780),
        _row("新しいカード", 5, 900, None, None),       # 比べられなければ全店の最安値・変化なし
        _row("揺れたカード", 7, 300, 400, 300, stable=False),
    ]
    themes = staple_ranking.build_lists(rows)["themes"]
    assert [i["name"] for i in themes] == ["増殖するG", "天救竜", "揺れたカード", "グリフォー", "新しいカード"]
    by = {i["name"]: i for i in themes}
    assert by["増殖するG"]["diff"] == 0
    assert by["グリフォー"]["price"] == 120 and by["グリフォー"]["diff"] is None
    assert by["天救竜"]["price"] == 5780 and by["天救竜"]["diff"] == -200
    assert by["新しいカード"]["price"] == 900 and by["新しいカード"]["diff"] is None
    assert by["揺れたカード"]["price"] == 300 and by["揺れたカード"]["diff"] is None


def test_api_validates_sort(monkeypatch):
    client = app_module.app.test_client()
    assert client.get("/api/staple-movers?sort=bad").status_code == 400
    monkeypatch.setattr(staple_ranking, "get_cached", lambda sb, force=False: {
        "drop": [{"name": "A", "rarity": "", "themes": 3, "price": 100, "diff": -50, "pct": -33.3}] * 12,
        "themes": [], "date_new": "2026-10-21", "date_old": "2026-10-14", "total": 12, "error": None})
    data = client.get("/api/staple-movers").get_json()
    assert len(data["items"]) == 10 and data["error"] is None
