"""買取の値動きランキングで、発売から7日以内の新弾の収録カードに「発売直後」の印（new_release）を付ける。"""
import featured_pack


def _rows():
    def row(name, rarity, direction, new, old):
        return {"card_name": name, "rarity": rarity, "today_price": new, "prev_price": old,
                "diff": new - old, "pct": round((new - old) / old * 100, 1),
                "date_new": "2026-11-01", "date_old": "2026-10-31", "direction": direction}
    return [
        row("ハーピィ・レディ-魅惑の舞い-", "プリシク", "up", 2000, 300),   # 収録一覧は全角「－」
        row("ハーピィ・レディ三姉妹・TX", "プリシク", "up", 1000, 300),     # 収録一覧は全角「ＴＸ」
        row("ブラック・マジシャン", "スーパー", "up", 200, 30),              # 収録されていない
        row("ダスト・シュート", "ノーマル", "down", 500, 700),
    ]


class _Rpc:
    def execute(self):
        return type("R", (), {"data": _rows()})()


class _Sb:
    def rpc(self, name, params):
        return _Rpc()


def _fetch(monkeypatch, pack, keys):
    import app as app_module
    monkeypatch.setattr(app_module, "_supabase_client", _Sb())
    monkeypatch.setattr(app_module, "get_featured_pack", lambda sb: pack)
    monkeypatch.setattr(app_module, "get_featured_card_keys_cached", lambda sb, p: keys)
    return app_module._fetch_buyback_movers_from_db()


def _pack(start):
    return {"pack_name": "IMMORTAL PHOENIX", "wiki_page": "", "tcg_name": "",
            "start_date": start, "window_days": 7}


def test_marks_only_cards_in_new_pack(monkeypatch):
    from datetime import date
    keys = {featured_card_key(n) for n in ("ハーピィ・レディ－魅惑の舞い－", "ハーピィ・レディ三姉妹・ＴＸ")}
    data = _fetch(monkeypatch, _pack(date.today().isoformat()), keys)
    marks = {e["name"]: e.get("new_release", False) for e in data["up"] + data["down"]}
    assert marks == {
        "ハーピィ・レディ-魅惑の舞い-": True,
        "ハーピィ・レディ三姉妹・TX": True,
        "ブラック・マジシャン": False,
        "ダスト・シュート": False,
    }


def test_no_mark_outside_window(monkeypatch):
    # 発売から8日目以降は印を付けない（featured_pack の運用期間と同じ7日）
    from datetime import date, timedelta
    keys = {featured_card_key("ハーピィ・レディ－魅惑の舞い－")}
    data = _fetch(monkeypatch, _pack((date.today() - timedelta(days=8)).isoformat()), keys)
    assert not any(e.get("new_release") for e in data["up"] + data["down"])


def test_no_new_pack_still_returns_ranking(monkeypatch):
    data = _fetch(monkeypatch, None, set())
    assert len(data["up"]) == 3 and len(data["down"]) == 1
    assert not any(e.get("new_release") for e in data["up"] + data["down"])


def test_new_pack_lookup_failure_still_returns_ranking(monkeypatch):
    import app as app_module

    def boom(sb):
        raise RuntimeError("db down")
    monkeypatch.setattr(app_module, "_supabase_client", _Sb())
    monkeypatch.setattr(app_module, "get_featured_pack", boom)
    data = app_module._fetch_buyback_movers_from_db()
    assert len(data["up"]) == 3
    assert not any(e.get("new_release") for e in data["up"])


def test_cached_keys_read_saved_list_without_scraping(monkeypatch):
    class _Q:
        def __init__(self, data):
            self._data = data
        def select(self, *a):
            return self
        def eq(self, col, val):
            assert (col, val) == ("pack_key", "pack_IMMORTAL_PHOENIX")
            return self
        def execute(self):
            return type("R", (), {"data": self._data})()

    class _TableSb:
        def table(self, name):
            assert name == "pack_cards_cache"
            return _Q([{"cards": ["太陽神の処刑人－マキュラ", "ハーピィ・レディ三姉妹・ＴＸ", ""]}])

    import pack_scraper
    monkeypatch.setattr(pack_scraper, "fetch_pack_cards",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Wiki を読みに行った")))
    keys = featured_pack.get_featured_card_keys_cached(_TableSb(), _pack("2026-10-31"))
    assert keys == {featured_card_key("太陽神の処刑人-マキュラ"), featured_card_key("ハーピィ・レディ三姉妹・TX")}


def featured_card_key(name):
    return featured_pack.featured_card_key(name)
