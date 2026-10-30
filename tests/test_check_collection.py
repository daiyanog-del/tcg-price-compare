"""価格収集の見張り（check_collection.evaluate）の判定"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_collection as cc

HIST = [5000, 5200, 4800, 0, 5100]  # 0 の日（抜けた日）は中央値に入れない


def test_ok_when_usual():
    assert cc.evaluate("買取", 4900, HIST, [{"finished_at": "x"}], "tcg-collect-buyback") is None


def test_zero_without_run_means_cron_did_not_start():
    msg = cc.evaluate("買取", 0, HIST, [], "tcg-collect-buyback")
    assert "1件もありません" in msg and "起動していない" in msg and "tcg-collect-buyback" in msg


def test_zero_with_finished_run_and_skipped_shops():
    msg = cc.evaluate("販売", 0, HIST, [{"finished_at": "x", "skipped_shops": {"カーナベル": "health_check_failed"}}], "tcg-collect-prices")
    assert "実行は終わったが0件" in msg and "カーナベル（health_check_failed）" in msg


def test_zero_with_unfinished_run():
    assert "実行中のまま" in cc.evaluate("販売", 0, HIST, [{"finished_at": None}], "tcg-collect-prices")


def test_low_compared_to_median():
    # 2026-10-11 の販売（普段の約28%）は知らせる
    msg = cc.evaluate("販売", 11059, [39000, 40000, 41000], [{"finished_at": "x"}], "tcg-collect-prices")
    assert msg and "28%" in msg


def test_usual_low_day_is_not_alerted():
    # 2026-10-11・10-13 の買取（普段の約58%）は知らせない
    assert cc.evaluate("買取", 2924, [5000, 5100, 4900], [{"finished_at": "x"}], "tcg-collect-buyback") is None


def test_no_history_is_not_alerted():
    assert cc.evaluate("買取", 10, [0, 0], [{"finished_at": "x"}], "tcg-collect-buyback") is None


# ── 店ごとの見張り（2026-10-30 追加）──

def _run(stats, skipped=None, started="2026-10-29T00:01:48.49296+00:00"):
    return {"started_at": started, "skipped_shops": skipped or {}, "shop_stats": stats}


def _st(ok, empty=0, error=0, skipped=None):
    return {"ok": ok, "empty": empty, "error": error, "skipped": skipped}


USUAL = [_run({"カードラッシュ": _st(600, 200), "トレコロCB": _st(720, 90), "_note": {"x": 1}})] * 5


def test_shops_ok_on_usual_day():
    assert cc.evaluate_shops("買取", USUAL[0], USUAL) == []


def test_skipped_shop_is_alerted_but_suspended_is_not():
    # 10月に買取のカードラッシュが9日、開始時の確認で丸ごと飛ばされていた
    today = _run({"トレコロCB": _st(720, 90)}, {"カードラッシュ": "health_check_failed", "カーナベル": "suspended"})
    msgs = cc.evaluate_shops("買取", today, USUAL)
    assert len(msgs) == 1 and "カードラッシュ" in msgs[0] and "開始時の確認で店のサイトが応答しなかった" in msgs[0]


def test_errors_and_breaker_are_alerted():
    # 2026-10-29 のトレコロCB（391/809件・386件を取りに行かず）
    today = _run({"カードラッシュ": _st(600, 200), "トレコロCB": _st(369, 49, 391, 386)})
    msgs = cc.evaluate_shops("買取", today, USUAL)
    assert any("トレコロCB のエラーが 391/809件（48%）" in m and "問い合わせを 386回止めました" in m for m in msgs)


def test_few_errors_are_not_alerted():
    today = _run({"カードラッシュ": _st(3100, 700, 20), "トレコロCB": _st(720, 90)})
    assert cc.evaluate_shops("販売", today, USUAL) == []


def test_ok_rate_drop_is_alerted():
    # 2026-10-13 のトレコロCB（WAF で全件0件）
    today = _run({"カードラッシュ": _st(600, 200), "トレコロCB": _st(0, 810)})
    msgs = cc.evaluate_shops("買取", today, USUAL)
    assert len(msgs) == 1 and "トレコロCB で取れたカードが 0%" in msgs[0]


def test_ok_rate_needs_history():
    today = _run({"カードラッシュ": _st(600, 200), "新しい店": _st(0, 800)})
    assert cc.evaluate_shops("買取", today, USUAL) == []


def test_attempted_drop_is_alerted():
    # 2026-10-27 の買取（環境デッキが空で注目カードが約4割減）
    today = _run({"カードラッシュ": _st(311, 180), "トレコロCB": _st(390, 101)})
    msgs = cc.evaluate_shops("買取", today, USUAL)
    assert len(msgs) == 1 and "491枚" in msgs[0] and "810枚" in msgs[0]


def test_no_today_run_is_left_to_evaluate():
    assert cc.evaluate_shops("買取", None, USUAL) == []


def test_daily_runs_keeps_last_run_of_jst_day():
    a = _run({}, started="2026-10-29T00:01:00+00:00")
    b = _run({}, started="2026-10-29T03:00:00.5+00:00")
    c = _run({}, started="2026-10-29T16:00:00+00:00")  # JST 10-30 1:00
    days = cc.daily_runs([b, c, a])
    from datetime import date
    assert days[date(2026, 10, 29)] is b and days[date(2026, 10, 30)] is c


def test_unknown_rarities():
    canonical = {"シークレット", "ウルトラ"}
    rows = [{"shop": "トレコロCB", "rarity": "ステンレス製"}, {"shop": "トレコロCB", "rarity": "シークレット"},
            {"shop": "カーナベル", "rarity": "MANGAシク"}, {"shop": "遊々亭", "rarity": "ステンレス製"}]
    found = cc.find_unknown_rarities(rows, canonical)
    assert found == {"ステンレス製": {"トレコロCB", "遊々亭"}}
    msg = cc.unknown_rarity_text("買取", found)
    assert "「ステンレス製」（トレコロCB・遊々亭）" in msg
    assert cc.unknown_rarity_text("買取", {}) is None


def test_known_odd_rarities_are_not_canonical():
    # 正規名になった表記は既知の一覧から外す（二重に持たない）
    import rarity
    canonical = {e["canonical"] for e in rarity.RARITIES}
    assert not (cc.KNOWN_ODD_RARITIES & canonical)
    assert "ステンレス製" not in cc.KNOWN_ODD_RARITIES


class _FakeQuery:
    def __init__(self, db, table):
        self.db, self.table, self.filters, self.want_count, self.rng = db, table, [], False, None

    def select(self, cols, count=None):
        self.want_count = count == "exact"
        return self

    def eq(self, k, v):
        self.filters.append(lambda r: r.get(k) == v)
        return self

    def gte(self, k, v):
        self.filters.append(lambda r: str(r.get(k)) >= v[:19])
        return self

    def in_(self, k, vs):
        self.filters.append(lambda r: r.get(k) in vs)
        return self

    def order(self, k):
        return self

    def range(self, a, b):
        self.rng = (a, b)
        return self

    def limit(self, n):
        return self

    def execute(self):
        from types import SimpleNamespace
        rows = [r for r in self.db.get(self.table, []) if all(f(r) for f in self.filters)]
        if self.rng:
            rows = rows[self.rng[0]:self.rng[1] + 1]
        return SimpleNamespace(data=rows, count=len(rows) if self.want_count else None)


def test_main_sends_shop_and_rarity_alerts(monkeypatch):
    import types
    from datetime import date, datetime, timedelta
    today = datetime.now(cc.JST).date()

    def day(n):
        return (today - timedelta(days=n)).isoformat()

    def started(n):
        return f"{day(n)}T00:30:00+00:00"  # JST 9:30

    db = {
        "price_history": [{"recorded_at": day(n), "shop": "遊々亭", "rarity_id": 1} for n in range(0, 15) for _ in range(3)],
        "rarity_dim": [{"rarity_id": 1, "rarity": "シークレット"}],
        "buyback_history": [{"recorded_at": day(n), "shop": "遊々亭", "rarity": "シークレット", "id": n} for n in range(0, 15)]
                           + [{"recorded_at": day(0), "shop": "トレコロCB", "rarity": "ステンレス製", "id": 99}],
        "collection_runs": (
            [{"run_type": "buyback", "started_at": started(n), "finished_at": "x", "skipped_shops": {},
              "shop_stats": {"カードラッシュ": _st(600, 200)}} for n in range(1, 6)]
            + [{"run_type": "buyback", "started_at": started(0), "finished_at": "x",
                "skipped_shops": {"カードラッシュ": "health_check_failed"}, "shop_stats": {"遊々亭": _st(600, 200)}}]
        ),
    }
    fake_supabase = types.ModuleType("supabase")
    fake_supabase.create_client = lambda url, key: types.SimpleNamespace(table=lambda t: _FakeQuery(db, t))
    sent = []
    fake_notify = types.ModuleType("discord_notify")
    fake_notify.send_discord_message = lambda msg, **k: sent.append((msg, k))
    monkeypatch.setitem(sys.modules, "supabase", fake_supabase)
    monkeypatch.setitem(sys.modules, "discord_notify", fake_notify)
    monkeypatch.setenv("SUPABASE_URL", "u")
    monkeypatch.setenv("SUPABASE_KEY", "k")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    assert cc.main() == 1
    msg, kwargs = sent[0]
    assert "カードラッシュ を今日まるごと取っていません" in msg
    assert "「ステンレス製」（トレコロCB）" in msg
    assert "販売" not in msg  # 販売は問題なし
    assert kwargs == {"no_mentions": True}


def test_aborted_run_is_alerted_without_attempted_drop():
    # DB の連続失敗で打ち切った実行（collection_run.py が shop_stats に _aborted を入れる）
    today = _run({"カードラッシュ": _st(100, 20), "_aborted": {"processed": 120, "total": 800}})
    msgs = cc.evaluate_shops("買取", today, USUAL)
    assert len(msgs) == 1 and "途中で打ち切られています（処理 120/800枚）" in msgs[0]


def test_broken_rows_do_not_raise():
    bad = {"started_at": "2026-10-29T00:00:00+00:00", "skipped_shops": ["x"], "shop_stats": None}
    assert cc.evaluate_shops("買取", bad, [bad, {"shop_stats": "x"}]) == []


def test_daily_runs_prefers_finished_run():
    from datetime import date
    done = {"started_at": "2026-10-29T00:01:00+00:00", "finished_at": "x"}
    rerun = {"started_at": "2026-10-29T05:00:00+00:00", "finished_at": None}
    assert cc.daily_runs([rerun, done])[date(2026, 10, 29)] is done
    assert cc.daily_runs([rerun])[date(2026, 10, 29)] is rerun
    assert cc.daily_runs([{"started_at": None}]) == {}


def test_run_day_reads_short_fraction_and_z():
    from datetime import date
    assert cc._run_day({"started_at": "2026-09-24T20:01:09.11532+00:00"}) == date(2026, 9, 25)
    assert cc._run_day({"started_at": "2026-09-24T14:59:59Z"}) == date(2026, 9, 24)
    assert cc._run_day({"started_at": "2026-09-24T15:00:00.5+00:00"}) == date(2026, 9, 25)


def test_rarity_message_is_capped():
    found = {f"表記{i:02d}": {"店"} for i in range(13)}
    msg = cc.unknown_rarity_text("販売", found)
    assert "「表記09」" in msg and "「表記10」" not in msg and "ほか 3件" in msg


def _fake_env(monkeypatch, db, fail_table=None):
    import types
    class Q(_FakeQuery):
        def execute(self):
            if self.table == fail_table:
                raise RuntimeError("timeout")
            return super().execute()
    fake_supabase = types.ModuleType("supabase")
    fake_supabase.create_client = lambda url, key: types.SimpleNamespace(table=lambda t: Q(db, t))
    sent = []
    fake_notify = types.ModuleType("discord_notify")
    fake_notify.send_discord_message = lambda msg, **k: sent.append(msg)
    monkeypatch.setitem(sys.modules, "supabase", fake_supabase)
    monkeypatch.setitem(sys.modules, "discord_notify", fake_notify)
    monkeypatch.setenv("SUPABASE_URL", "u")
    monkeypatch.setenv("SUPABASE_KEY", "k")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return sent


def _db_for_today(price_rarity="シークレット", buy_rows=1):
    from datetime import datetime, timedelta
    today = datetime.now(cc.JST).date()
    day = lambda n: (today - timedelta(days=n)).isoformat()
    return {
        "price_history": [{"recorded_at": day(n), "shop": "遊々亭", "rarity_id": 1 if n else 2, "rarity": "シークレット" if n else price_rarity}
                          for n in range(0, 15) for _ in range(3)],
        "rarity_dim": [{"rarity_id": 1, "rarity": "シークレット"}, {"rarity_id": 2, "rarity": price_rarity}],
        "buyback_history": [{"recorded_at": day(n), "shop": "遊々亭", "rarity": "シークレット", "id": n} for n in range(1, 15)]
                           + [{"recorded_at": day(0), "shop": "遊々亭", "rarity": "シークレット", "id": 100 + i} for i in range(buy_rows)],
        "collection_runs": [],
    }


def test_main_reads_price_rarity_by_id_and_pages_buyback(monkeypatch):
    db = _db_for_today(price_rarity="新しい表記", buy_rows=2500)
    db["buyback_history"].append({"recorded_at": db["buyback_history"][-1]["recorded_at"], "shop": "カードラボ",
                                  "rarity": "最後のページの表記", "id": 9999})
    sent = _fake_env(monkeypatch, db)
    assert cc.main() == 1
    assert "販売: 見慣れないレアリティの表記があります: 「新しい表記」（遊々亭）" in sent[0]
    assert "「最後のページの表記」（カードラボ）" in sent[0]  # 1000件ずつ読んで3ページ目まで届く


def test_main_still_sends_row_alert_when_rarity_check_fails(monkeypatch):
    db = _db_for_today()
    db["buyback_history"] = [r for r in db["buyback_history"] if r["id"] < 100]  # 今日の買取が0件
    sent = _fake_env(monkeypatch, db, fail_table="rarity_dim")
    assert cc.main() == 1
    assert "買取の価格収集が今日（JST）まだ1件もありません" in sent[0]
    assert "販売: 見慣れないレアリティの確認を読めませんでした（RuntimeError: timeout）" in sent[0]
