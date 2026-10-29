"""店の一時休止（SUSPENDED_SHOPS・shop_status.py）のテスト。

2026-10-21 カーナベルが PHP の脆弱性で遊戯王サイトを止めたあとも、裏側の検索サービス（Elasticsearch）が
在庫つきの結果を返し続け、ラボが閉まった店を「買える店」として出していた件への対応。
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import shop_status  # noqa: E402


def _run_with_env(code: str, suspended: str) -> dict:
    """SUSPENDED_SHOPS を設定した別プロセスで code を実行し、最後に print した JSON を返す。
    店の一覧は import 時に決まるので、同じプロセスの他のテストを汚さないように分ける。"""
    env = dict(os.environ, SUSPENDED_SHOPS=suspended, DISABLE_STARTUP_JOBS="1",
               PYTHONIOENCODING="utf-8", KANABELL_ES_ENDPOINT="https://example.invalid", KANABELL_ES_API_KEY="k")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                         capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_drop_suspended_rows(monkeypatch):
    monkeypatch.setattr(shop_status, "SUSPENDED_SHOPS", frozenset({"カーナベル"}))
    rows = [{"shop": "カーナベル", "p": 1}, {"shop": "遊々亭", "p": 2}, {"p": 3}]
    assert shop_status.drop_suspended_rows(rows) == [{"shop": "遊々亭", "p": 2}, {"p": 3}]
    assert shop_status.is_suspended("カーナベル")
    assert not shop_status.is_suspended("")
    assert not shop_status.is_suspended(None)


def test_drop_suspended_rows_noop_when_empty(monkeypatch):
    monkeypatch.setattr(shop_status, "SUSPENDED_SHOPS", frozenset())
    rows = [{"shop": "カーナベル"}]
    assert shop_status.drop_suspended_rows(rows) is rows


def test_scraper_shop_lists_exclude_suspended():
    code = (
        "import json, scraper as s\n"
        "print(json.dumps({'shops':[n for n,_ in s.SHOPS],'default':s.DEFAULT_SHOPS,"
        "'buy':[n for n,_ in s.BUYBACK_SHOPS],'buy_default':s.DEFAULT_BUYBACK_SHOPS,"
        "'img':s.kanabell_card_image_url('灰流うらら')}, ensure_ascii=False))\n"
    )
    r = _run_with_env(code, "カーナベル")
    assert "カーナベル" not in r["shops"] and "カーナベル" not in r["default"]
    assert "カーナベル" not in r["buy"] and "カーナベル" not in r["buy_default"]
    assert "遊々亭" in r["default"] and "遊々亭" in r["buy_default"]
    assert r["img"] == ""  # 休止中は画像の予備にも使わない（外へ取りに行かない）


def test_scraper_shop_lists_unchanged_without_env():
    r = _run_with_env("import json, scraper as s\nprint(json.dumps(s.DEFAULT_SHOPS, ensure_ascii=False))\n", "")
    assert r == ["遊々亭", "カードラッシュ", "トレコロCB", "カーナベル", "カードラボ", "まんぞく屋"]


def test_api_config_hides_suspended_shop():
    code = (
        "import json, app\n"
        "c = app.app.test_client().get('/api/config').get_json()\n"
        "print(json.dumps({'s':[x['name'] for x in c['shops']],'b':[x['name'] for x in c['buyback_shops']]}, ensure_ascii=False))\n"
    )
    r = _run_with_env(code, "カーナベル")
    assert "カーナベル" not in r["s"] and "カーナベル" not in r["b"]


def test_shop_cache_does_not_return_suspended_shop(monkeypatch):
    import scraper
    monkeypatch.setattr(scraper.shop_cache_kv, "load", lambda kind, key, shops: {
        "カーナベル": {"results": [{"price": 1}], "timestamp": "2026-10-22T10:00:00", "partial": False},
        "遊々亭": {"results": [{"price": 2}], "timestamp": "2026-10-22T10:00:00", "partial": False},
    })
    monkeypatch.setattr(scraper, "_filter_shop_entries", lambda kv, age: dict(kv))
    monkeypatch.setattr(scraper, "_shop_cache_load", lambda path, age: {})
    monkeypatch.setattr(scraper, "is_suspended", lambda shop: shop == "カーナベル")
    got = scraper._shop_cache_entries("sell", scraper.CACHE_DIR, "k", None, None)
    assert set(got) == {"遊々亭"}


def test_price_history_reads_drops_suspended(monkeypatch):
    import price_history_reads as phr
    monkeypatch.setattr(shop_status, "SUSPENDED_SHOPS", frozenset({"カーナベル"}))

    class _Q:
        def __init__(self, table): self.table = table
        def select(self, *_): return self
        def in_(self, *_): return self
        def eq(self, *_): return self
        def order(self, *_a, **_k): return self
        def range(self, *_): return self
        def execute(self):
            if self.table == "card_dim":
                return SimpleNamespace(data=[{"card_id": 1, "card_name": "A"}])
            return SimpleNamespace(data=[
                {"card_id": 1, "card_name": "A", "shop": "カーナベル", "min_price": 100},
                {"card_id": 1, "card_name": "A", "shop": "遊々亭", "min_price": 200},
            ])

    rows, failed = phr.fetch_daily_rows_by_card_names(
        SimpleNamespace(table=lambda name: _Q(name)), ["A"], "card_id, card_name, shop, min_price",
        ["2026-10-22"], "t")
    assert failed == set()
    assert [r["shop"] for r in rows] == ["遊々亭"]


def test_featured_matrix_skips_suspended(monkeypatch):
    import featured_matrix as fm
    monkeypatch.setattr(fm, "is_suspended", lambda shop: shop == "カーナベル")
    records = [
        {"card_name": "A", "shop": "カーナベル", "rarity": "スーパー", "min_price": 50, "recorded_at": "2026-10-21"},
        {"card_name": "A", "shop": "遊々亭", "rarity": "スーパー", "min_price": 80, "recorded_at": "2026-10-22"},
    ]
    m = fm.build_matrix(records, ["A"], "2026-10-22")
    assert "カーナベル" not in m["shops"]
    assert all("カーナベル" not in c for row in m["rows"] for c in [json.dumps(row, ensure_ascii=False)])


def test_health_check_treats_5xx_as_down(monkeypatch, capsys):
    import requests
    import collect_prices as cp
    codes = {"https://www.ka-nabell.com/": 503, "https://www.cardrush.jp/": 200,
             "https://www.torecolo.jp/": 403}
    calls = []
    monkeypatch.setattr(requests, "get", lambda url, **k: calls.append(url) or SimpleNamespace(status_code=codes[url], close=lambda: None))
    monkeypatch.setattr(cp.time, "sleep", lambda s: None)
    got = cp.check_shop_availability(["カーナベル", "カードラッシュ", "トレコロCB", "遊々亭"])
    assert got == ["カードラッシュ", "トレコロCB", "遊々亭"]  # 4xx は従来どおり通す・確認先の無い店も通す
    assert "HTTP 503" in capsys.readouterr().out
    assert calls.count("https://www.ka-nabell.com/") == 2  # 5xx は1回だけ取り直す


def test_health_check_recovers_on_retry(monkeypatch):
    import requests
    import collect_prices as cp
    seq = iter([502, 200])
    monkeypatch.setattr(requests, "get", lambda url, **k: SimpleNamespace(status_code=next(seq), close=lambda: None))
    monkeypatch.setattr(cp.time, "sleep", lambda s: None)
    assert cp.check_shop_availability(["カードラッシュ"]) == ["カードラッシュ"]


def test_price_history_and_config_with_suspended_shop():
    code = (
        "import json, app\n"
        "app._supabase_client = None\n"
        "c = app.app.test_client()\n"
        "cfg = c.get('/api/config').get_json()\n"
        "print(json.dumps({'aff': list(cfg['affiliate']), 'workers': app.SHOP_FETCH_MAX_WORKERS}, ensure_ascii=False))\n"
    )
    r = _run_with_env(code, "カーナベル")
    assert "カーナベル" not in r["aff"]
    assert r["workers"] == 6


def test_watch_suspended_shops_notifies_when_site_is_back(monkeypatch):
    import requests
    import collect_prices as cp
    sent = []
    monkeypatch.setattr(cp, "SUSPENDED_SHOPS", frozenset({"カーナベル"}))
    monkeypatch.setattr(cp, "send_discord_message", lambda msg, **k: sent.append(msg))
    monkeypatch.setattr(requests, "get", lambda url, **k: SimpleNamespace(status_code=503, close=lambda: None))
    cp.watch_suspended_shops()
    assert sent == []
    monkeypatch.setattr(requests, "get", lambda url, **k: SimpleNamespace(status_code=200, close=lambda: None))
    cp.watch_suspended_shops()
    assert len(sent) == 1 and "カーナベル" in sent[0]


def test_estimate_cache_passes_excluded_shops(monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, "_estimate_cache", {})
    monkeypatch.setattr(app_module, "_estimate_cache_time", 0)
    calls = []

    class _Fake:
        def rpc(self, name, params):
            calls.append((name, dict(params)))
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=[
                {"card_name": "A", "shop": "遊々亭", "rarity": "R", "min_price": 100, "recorded_at": "2026-10-22"}]))

    monkeypatch.setattr(app_module, "_supabase_client", _Fake())
    monkeypatch.setattr(app_module, "SUSPENDED_SHOPS", frozenset({"カーナベル"}))
    app_module._load_estimate_cache()
    assert calls[0][1]["excluded_shops"] == ["カーナベル"]

    calls.clear()
    monkeypatch.setattr(app_module, "_estimate_cache_time", 0)
    monkeypatch.setattr(app_module, "SUSPENDED_SHOPS", frozenset())
    app_module._load_estimate_cache()
    assert "excluded_shops" not in calls[0][1]  # 休止が無ければ従来どおりの引数


def test_page_marks_suspended_shops_for_notice():
    """画面の「○○はサイトを休止中のため対象外です」の元（body の data-suspended-shops）"""
    code = (
        "import json, app\n"
        "html = app.app.test_client().get('/').get_data(as_text=True)\n"
        "print(json.dumps({'marked': 'data-suspended-shops=\"\u30ab\u30fc\u30ca\u30d9\u30eb\"' in html}))\n"
    )
    assert _run_with_env(code, "カーナベル")["marked"] is True
    code_none = (
        "import json, app\n"
        "html = app.app.test_client().get('/').get_data(as_text=True)\n"
        "print(json.dumps({'marked': 'data-suspended-shops' in html}))\n"
    )
    assert _run_with_env(code_none, "")["marked"] is False
