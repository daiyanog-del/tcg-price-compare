"""collect_tournament_posts の検索式・日付・続きの読み方のテスト（X には繋がない）。"""

from datetime import date, datetime, timezone

import collect_tournament_posts as ctp
from tests.fake_supabase import FakeSB


def _id_at(dt: datetime) -> str:
    """その時刻に投稿された X の ID（snowflake）を作る。"""
    ms = int(dt.timestamp() * 1000) - 1288834974657
    return str(ms << 22)


NOW = datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)


def test_build_queries_fit_length_and_cover_all():
    names = [f"shop_account_{i:03d}" for i in range(60)]
    qs = ctp.build_queries(names, ctp._SHOP_KW, max_len=480)
    assert all(len(q) <= 480 for q in qs)
    joined = " ".join(qs)
    assert all(f"from:{n}" in joined for n in names)
    assert len(qs) > 1


def test_bundled_accounts_build_valid_queries():
    accounts = ctp.load_accounts()
    assert len(accounts["shop"]) >= 100 and accounts["cs"]
    assert all(a == a.lower() for a in accounts["shop"] + accounts["cs"])
    for qs in ctp.queries_by_kind(accounts).values():
        assert all(len(q) <= 512 for q in qs)


def test_parse_event_date():
    posted = date(2026, 10, 28)
    assert ctp.parse_event_date("10/25開催第16回テストCS結果", posted) == date(2026, 10, 25)
    assert ctp.parse_event_date("1⃣0⃣月2⃣7⃣日開催の #遊戯王", posted) == date(2026, 10, 27)
    # 次回の告知（先の日付）は大会の日付にしない
    assert ctp.parse_event_date("本日の優勝は…\n次回は10/29(木)14:00〜の開催", posted) == posted
    # 年をまたぐ
    assert ctp.parse_event_date("12/30開催 テストCS", date(2027, 1, 2)) == date(2026, 12, 30)
    assert ctp.parse_event_date("本日開催", posted) == posted


def test_snowflake_time_roundtrip():
    dt = datetime(2026, 10, 28, 15, 11, 37, tzinfo=timezone.utc)
    assert abs((ctp.snowflake_time(_id_at(dt)) - dt).total_seconds()) < 1


def _tweet(tid, text="本日の #遊戯王OCG ランキングデュエル\n優勝者は…\n使用デッキ：三幻魔", user="shop_a"):
    return {"id": tid, "text": text, "created_at": "2026-10-29T12:00:00.000Z", "username": user}


def _accounts():
    return {"shop": ["shop_a"], "cs": ["cs_a"]}


def test_collect_advances_since_id_and_kind_precedence(monkeypatch):
    calls = []
    recent = _id_at(datetime(2026, 10, 29, tzinfo=timezone.utc))
    newer = str(int(recent) + 10)

    def fake_search(token, query, budget, *, since_id, start_time):
        calls.append((query, since_id, start_time))
        budget.used += 1
        if query == ctp._CS_SEARCH_QUERY:
            return [_tweet(newer, "テストCS 優勝は【三幻魔】！ 遊戯王", "cs_a")], True
        if "from:cs_a" in query:
            return [_tweet(newer, "テストCS 優勝は【三幻魔】！", "cs_a")], True
        return [_tweet(recent)], True

    monkeypatch.setattr(ctp, "search_recent", fake_search)
    rows, new_since, budget, incomplete = ctp.collect(
        "tok", _accounts(), {"shop": str(int(recent) - 5)}, now=NOW)

    # 同じポストは主催者の一覧の種類が勝つ
    assert rows[newer]["source_kind"] == "cs"
    assert rows[newer]["placements"][0]["theme"] == "三幻魔"
    assert rows[recent]["source_kind"] == "shop"
    assert new_since == {"cs_search": newer, "shop": recent, "cs": newer}
    assert incomplete == []
    # shop は保存済みの since_id から、他は初回なので時刻から
    by_kind = {("shop" if "from:shop_a" in q else "cs" if "from:cs_a" in q else "cs_search"): (s, st)
               for q, s, st in calls}
    assert by_kind["shop"] == (str(int(recent) - 5), None)
    assert by_kind["cs"][0] is None and by_kind["cs"][1] == "2026-10-28T12:00:00Z"


def test_collect_keeps_since_id_when_budget_runs_out(monkeypatch):
    old = _id_at(datetime(2026, 10, 29, tzinfo=timezone.utc))

    def fake_search(token, query, budget, *, since_id, start_time):
        if "from:shop_a" in query:
            budget.used = budget.limit
            return [_tweet(str(int(old) + 1))], False
        return [], True

    monkeypatch.setattr(ctp, "search_recent", fake_search)
    rows, new_since, budget, incomplete = ctp.collect("tok", _accounts(), {"shop": old}, max_reads=50, now=NOW)
    assert "shop" in incomplete
    assert "shop" not in new_since
    # 読めた分は保存する（upsert なので次回に読み直しても重複しない）
    assert str(int(old) + 1) in rows


def test_collect_falls_back_to_time_when_since_id_too_old(monkeypatch):
    stale = _id_at(datetime(2026, 10, 22, tzinfo=timezone.utc))
    seen = {}

    def fake_search(token, query, budget, *, since_id, start_time):
        if "from:shop_a" in query:
            seen["shop"] = (since_id, start_time)
        return [], True

    monkeypatch.setattr(ctp, "search_recent", fake_search)
    ctp.collect("tok", _accounts(), {"shop": stale}, now=NOW)
    assert seen["shop"][0] is None
    assert seen["shop"][1] == "2026-10-23T08:00:00Z"


def test_build_row_shape():
    row = ctp.build_row(_tweet("123", "10/27開催 #遊戯王 ランキングデュエル\n参加者8名\n優勝：烙印"), "shop")
    assert row["event_date"] == "2026-10-27"
    assert row["participants"] == 8
    assert row["placements"] == [{"rank": 1, "theme_raw": "烙印", "theme": "烙印"}]
    assert row["is_ocg"] is True and row["parser_version"] == ctp.PARSER_VERSION
    assert set(row) == {"tweet_id", "author", "source_kind", "posted_at", "event_date", "text", "is_ocg",
                        "participants", "placements", "distribution", "parser_version"}


def test_load_accounts_drops_excluded(tmp_path):
    p = tmp_path / "a.json"
    p.write_text('{"shop": ["Shop_A", "Matome"], "cs": ["cs_a"], "exclude": {"matome": "まとめ直し"}}',
                 encoding="utf-8")
    assert ctp.load_accounts(p) == {"shop": ["shop_a"], "cs": ["cs_a"]}



def test_cs_account_candidates_excludes_known():
    from datetime import datetime as _dt
    from constants import JST
    today = _dt.now(JST).date().isoformat()
    rows = [
        {"author": "new_cs", "source_kind": "cs_search", "is_ocg": True, "event_date": today},
        {"author": "new_cs", "source_kind": "cs_search", "is_ocg": True, "event_date": today},
        {"author": "cs_a", "source_kind": "cs_search", "is_ocg": True, "event_date": today},
        {"author": "other", "source_kind": "cs_search", "is_ocg": False, "event_date": today},
        {"author": "old_cs", "source_kind": "cs_search", "is_ocg": True, "event_date": "2020-01-01"},
    ]
    assert ctp.cs_account_candidates(FakeSB({"tournament_posts": rows}), _accounts()) == [("new_cs", 2)]


def test_reparse_updates_old_rows_only(monkeypatch):
    old = {"tweet_id": "1", "author": "shop_a", "source_kind": "shop", "posted_at": "2026-10-29T12:00:00+00:00",
           "text": "#遊戯王 ランキングデュエル\n優勝：烙印", "parser_version": 0, "placements": []}
    cur = {**old, "tweet_id": "2", "parser_version": ctp.PARSER_VERSION, "placements": "そのまま"}
    sb = FakeSB({"tournament_posts": [old, cur]})

    def save_one_by_one(sb_, rows, chunk=200):
        for r in rows:
            sb_.table("tournament_posts").upsert(r, on_conflict="tweet_id").execute()

    monkeypatch.setattr(ctp, "save_rows", save_one_by_one)
    assert ctp.reparse_all(sb) == 1
    by_id = {r["tweet_id"]: r for r in sb.tables["tournament_posts"]}
    assert by_id["1"]["placements"][0]["theme"] == "烙印"
    assert by_id["1"]["parser_version"] == ctp.PARSER_VERSION
    assert by_id["2"]["placements"] == "そのまま"


def test_run_alerts_only_on_trouble():
    assert ctp.run_alerts(80, [], 600) == []
    assert "shop" in ctp.run_alerts(600, ["shop"], 600)[0]
    assert len(ctp.run_alerts(0, [], 600)) == 1


def _run_main(monkeypatch, collect_result=None, error=None, argv=()):
    sent = []
    monkeypatch.setattr(ctp, "_notify", lambda lines: sent.append(lines))
    monkeypatch.setattr(ctp, "get_bearer_token", lambda: "tok")
    monkeypatch.setattr(ctp, "load_since_ids", lambda sb: {})
    monkeypatch.setattr(ctp, "save_rows", lambda sb, rows: None)
    monkeypatch.setattr(ctp, "save_since_ids", lambda sb, s: None)
    import supabase
    monkeypatch.setattr(supabase, "create_client", lambda url, key: object())
    monkeypatch.setenv("SUPABASE_URL", "u")
    monkeypatch.setenv("SUPABASE_KEY", "k")

    def fake_collect(*a, **k):
        if error:
            raise error
        return collect_result
    monkeypatch.setattr(ctp, "collect", fake_collect)
    return sent


def test_main_notifies_when_budget_runs_out(monkeypatch):
    budget = ctp.ReadBudget(600)
    budget.used = 600
    sent = _run_main(monkeypatch, ({}, {}, budget, ["shop"]))
    assert ctp.main([]) == 0
    assert len(sent) == 1 and "shop" in sent[0][0]


def test_main_quiet_on_normal_day(monkeypatch):
    budget = ctp.ReadBudget(600)
    budget.used = 80
    sent = _run_main(monkeypatch, ({}, {"shop": "1"}, budget, []))
    assert ctp.main([]) == 0
    assert sent == []


def test_main_notifies_and_fails_on_error(monkeypatch):
    import pytest
    sent = _run_main(monkeypatch, error=RuntimeError("X API 503"))
    with pytest.raises(RuntimeError):
        ctp.main([])
    assert "X API 503" in sent[0][0]
