"""
tests/test_deck_estimate_fixes.py — デッキ相場見積もりの2バグの回帰テスト

背景（2026-08-03 判明）:
  バグA: 入口の _normalize_query が全角ダッシュを半角へ寄せるのに、
    /api/deck-estimate だけ correct_names=False で正式名称へ戻さなかった。
    DBキー（正式名称=全角ダッシュ）と一致せず、該当カードは永遠に best:null。
    （correct_names 引数自体を廃止し、_parse_deck_entries は常に補正する）
  バグB: _load_estimate_cache が RPC get_card_best_prices を .limit(5000) で
    読んでいたが、PostgREST の返却上限は1000行。直近7日のカード2,688種のうち
    名前順1000種以降（漢字始まりの大半）が見積もりから消えていた。

テスト方針: ネットワーク不使用。fake Supabase クライアントと Flask test_client で検証。
  カード名DBは原則 monkeypatch の合成データを使う（cardnames_ja.json は毎週月曜に
  自動更新されるため、実ファイル依存はコールドスタートのテスト1本に限定する）。
"""

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module


# ── バグB: RPC結果の分割取得 ──

class _FakeRpcQuery:
    """rpc().order().range().execute() のチェーンを模倣し、引数を記録して
    total_rows 件のデータをページ返しする"""

    def __init__(self, total_rows: int, calls: list, orders: list):
        self.total_rows = total_rows
        self.calls = calls
        self.orders = orders
        self._start = 0
        self._end = 0

    def order(self, column):
        self.orders.append(column)
        return self

    def range(self, start, end):
        self.calls.append((start, end))
        self._start, self._end = start, end
        return self

    def execute(self):
        n = min(self._end + 1, self.total_rows)
        data = [{"card_name": f"カード{i:04d}", "shop": "店", "rarity": "レア",
                 "min_price": 100, "recorded_at": "2026-08-03"}
                for i in range(self._start, n)]
        return SimpleNamespace(data=data)


class _FakeSupabaseRpc:
    def __init__(self, total_rows: int, ignore_offset: bool = False):
        self.calls: list = []
        self.orders: list = []
        self.total_rows = total_rows
        self.ignore_offset = ignore_offset

    def rpc(self, name, params):
        assert name == "get_card_best_prices"
        q = _FakeRpcQuery(self.total_rows, self.calls, self.orders)
        if self.ignore_offset:
            # offsetを無視する異常サーバの模倣: 常に先頭ページを返す
            q.range = lambda start, end: (self.calls.append((start, end)),
                                          setattr(q, "_start", 0),
                                          setattr(q, "_end", end - start))[0] or q
        return q


@pytest.fixture()
def _estimate_isolated(monkeypatch):
    """見積もりキャッシュのグローバルをテスト間で汚さない"""
    monkeypatch.setattr(app_module, "_estimate_cache", {})
    monkeypatch.setattr(app_module, "_estimate_cache_time", 0)
    yield


def test_estimate_cache_paginates_beyond_1000(monkeypatch, _estimate_isolated):
    """1000行上限を超えるカード数でも全件ロードされる（バグBの回帰テスト）"""
    fake = _FakeSupabaseRpc(total_rows=2500)
    monkeypatch.setattr(app_module, "_supabase_client", fake)

    app_module._load_estimate_cache()

    # 1000行ずつ3ページ（1000+1000+500）で取得され、全2500カードが載る
    assert fake.calls == [(0, 999), (1000, 1999), (2000, 2999)]
    # ページ間の並び順は order で明示的に安定化する（RPC本体の実装に依存しない）
    assert fake.orders == ["card_name"] * 3
    assert len(app_module._estimate_cache) == 2500
    assert "カード2499" in app_module._estimate_cache


def test_estimate_cache_single_page(monkeypatch, _estimate_isolated):
    """1000行未満なら1リクエストで完了する"""
    fake = _FakeSupabaseRpc(total_rows=30)
    monkeypatch.setattr(app_module, "_supabase_client", fake)

    app_module._load_estimate_cache()

    assert fake.calls == [(0, 999)]
    assert len(app_module._estimate_cache) == 30


def test_estimate_cache_runaway_guard(monkeypatch, _estimate_isolated):
    """offsetが効かない異常サーバでも最大ページ数で打ち切られ、無限ループしない"""
    fake = _FakeSupabaseRpc(total_rows=1000, ignore_offset=True)
    monkeypatch.setattr(app_module, "_supabase_client", fake)

    app_module._load_estimate_cache()  # 停止すること自体が検証

    assert len(fake.calls) == 20  # max_pages で打ち切り
    assert len(app_module._estimate_cache) == 1000


# ── 相場RPCのjsonb一括集約経路（2026-09-02: PostgREST 1000行上限による
#    range分割ページング=RPC最大4回実行を1回に減らす対策） ──

class _FakeSupabaseJsonRpc:
    """get_card_best_prices_json（1回のRPCでjsonb配列を返す）を模倣"""

    def __init__(self, rows):
        self.rows = rows
        self.calls: list = []

    def rpc(self, name, params):
        self.calls.append(name)
        if name != "get_card_best_prices_json":
            raise AssertionError(f"想定外のRPC呼び出し: {name}")
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=self.rows))


def test_estimate_cache_uses_json_route_when_available(monkeypatch, _estimate_isolated):
    """get_card_best_prices_json が使える場合は1回のRPC呼び出しだけで完了し、
    従来のページング経路（get_card_best_prices の range 分割）は呼ばれない"""
    rows = [{"card_name": "カードA", "shop": "店", "rarity": "レア",
             "min_price": 100, "recorded_at": "2026-09-02"}]
    fake = _FakeSupabaseJsonRpc(rows)
    monkeypatch.setattr(app_module, "_supabase_client", fake)

    app_module._load_estimate_cache()

    assert fake.calls == ["get_card_best_prices_json"]
    assert app_module._estimate_cache["カードA"]["price"] == 100


class _FakeSupabaseJsonFailsFallback(_FakeSupabaseRpc):
    """get_card_best_prices_json が本番未適用（PGRST202相当）で例外になるケース。
    従来のページング経路（get_card_best_prices）へフォールバックすることを検証する"""

    def rpc(self, name, params):
        if name == "get_card_best_prices_json":
            raise Exception("PGRST202: Could not find the function public.get_card_best_prices_json")
        return super().rpc(name, params)


def test_estimate_cache_falls_back_to_paging_when_json_route_missing(monkeypatch, _estimate_isolated):
    """json経路が未定義で例外になっても、警告を出すだけで従来のページング経路に
    フォールバックして完走する（本番に未適用のRPCがあってもキャッシュロードは壊れない）"""
    # 想定上はこのテストで実際にリトライ(time.sleep)には入らないが、将来の回帰で
    # リトライ経路（wait=5*attempt秒）に落ちてもテストが遅くならないよう無効化しておく
    monkeypatch.setattr(app_module.time, "sleep", lambda *_a, **_k: None)
    fake = _FakeSupabaseJsonFailsFallback(total_rows=30)
    monkeypatch.setattr(app_module, "_supabase_client", fake)

    app_module._load_estimate_cache()

    assert fake.calls == [(0, 999)]  # ページング経路（get_card_best_prices）で完了
    assert len(app_module._estimate_cache) == 30


class _FakeSupabaseJsonNonList(_FakeSupabaseRpc):
    """get_card_best_prices_json が list 以外（dict等）の想定外レスポンスを返すケース。
    isinstance チェックで ValueError になり、ページング経路にフォールバックすることを検証する"""

    def rpc(self, name, params):
        if name == "get_card_best_prices_json":
            return SimpleNamespace(execute=lambda: SimpleNamespace(data={"unexpected": "dict"}))
        return super().rpc(name, params)


def test_estimate_cache_falls_back_when_json_route_returns_non_list(monkeypatch, _estimate_isolated):
    """get_card_best_prices_json が dict 等 list 以外を返した場合もページング経路に
    フォールバックする（例外だけでなく想定外レスポンス型も同じ扱いにする回帰テスト）"""
    monkeypatch.setattr(app_module.time, "sleep", lambda *_a, **_k: None)
    fake = _FakeSupabaseJsonNonList(total_rows=30)
    monkeypatch.setattr(app_module, "_supabase_client", fake)

    app_module._load_estimate_cache()

    assert fake.calls == [(0, 999)]
    assert len(app_module._estimate_cache) == 30


def test_estimate_cache_preserves_existing_cache_when_zero_rows(monkeypatch, _estimate_isolated):
    """0行はRPC異常の疑いとして例外化し、リトライしても解消しなければ既存キャッシュを
    保持する（2,700枚規模の運用で0行は常に異常。空キャッシュで上書きしない）"""
    monkeypatch.setattr(app_module.time, "sleep", lambda *_a, **_k: None)
    existing = {"既存カード": {"shop": "店", "price": 1, "rarity": "", "recorded_at": ""}}
    monkeypatch.setattr(app_module, "_estimate_cache", dict(existing))
    fake = _FakeSupabaseJsonRpc([])  # json経路が常に0行を返す異常系
    monkeypatch.setattr(app_module, "_supabase_client", fake)

    app_module._load_estimate_cache()

    # 0行応答では上書きされず、既存の中身がそのまま残る
    assert app_module._estimate_cache == existing


# ── バグA: 名前補正 ──

_CANON = "召喚魔術－「剣」"  # 全角ダッシュ持ちの正式名称（合成カード名DBに登録して使う）


def _patch_synthetic_cardnames(monkeypatch):
    """実ファイルに依存しない合成カード名DB。_cardnames_loaded=True にすることで
    ルート側の _load_cardnames() は早期returnし、合成データが生き残る"""
    monkeypatch.setattr(app_module, "_cardnames_loaded", True)
    monkeypatch.setattr(app_module, "_cardnames", [_CANON])
    monkeypatch.setattr(app_module, "_cardnames_set", {_CANON})
    monkeypatch.setattr(app_module, "_cardnames_fuzzy",
                        {app_module._fuzzy_key(_CANON): [_CANON]})
    monkeypatch.setattr(app_module, "_cardnames_reading", {})
    monkeypatch.setattr(app_module, "_cardnames_reading_fuzzy", {})


def _patch_estimate_hit(monkeypatch):
    monkeypatch.setattr(app_module, "_estimate_cache",
                        {_CANON: {"shop": "店", "price": 500, "rarity": "レア",
                                  "recorded_at": "2026-08-03"}})
    # 鮮度チェックでバックグラウンド再ロードが走らないよう現在時刻にする
    monkeypatch.setattr(app_module, "_estimate_cache_time", time.time())


def test_deck_estimate_corrects_dash_variant(monkeypatch):
    """半角ダッシュ入力でも正式名称（全角ダッシュ）のキャッシュに命中する
    （バグAの回帰テスト）"""
    _patch_synthetic_cardnames(monkeypatch)
    _patch_estimate_hit(monkeypatch)

    app_module.app.config.update(TESTING=True)
    client = app_module.app.test_client()

    # 半角ハイフン版（_normalize_query が生成する形）で問い合わせ
    resp = client.post("/api/deck-estimate", json={"cards": "召喚魔術-「剣」"})
    assert resp.status_code == 200
    result = resp.get_json()["results"][0]
    assert result["name"] == _CANON, "正式名称へ補正されること"
    assert result["best"] is not None, "補正後の名前でキャッシュに命中すること"
    assert result["best"]["price"] == 500
    assert resp.get_json()["total"] == 500


def test_deck_estimate_loads_cardnames_on_cold_start(monkeypatch):
    """カード名DB未ロードのプロセスで最初に /api/deck-estimate が叩かれても
    補正が効く（ルート内の _load_cardnames() を守る回帰テスト。2026-08-03
    reviewer指摘 中-5: この1行が無いと補正が空振りしバグAが再現する）"""
    # コールドスタート状態を再現（実ファイルからの再ロードをルートに行わせる）
    monkeypatch.setattr(app_module, "_cardnames_loaded", False)
    monkeypatch.setattr(app_module, "_cardnames_set", set())
    monkeypatch.setattr(app_module, "_cardnames_fuzzy", {})
    monkeypatch.setattr(app_module, "_cardnames_reading_fuzzy", {})
    _patch_estimate_hit(monkeypatch)

    app_module.app.config.update(TESTING=True)
    client = app_module.app.test_client()
    resp = client.post("/api/deck-estimate", json={"cards": "召喚魔術-「剣」"})
    assert resp.status_code == 200

    if _CANON not in app_module._cardnames_set:
        pytest.skip("実カード名DBに対象カードが無い（週次更新で消えた場合のみ）")
    result = resp.get_json()["results"][0]
    assert result["name"] == _CANON, "コールドスタートでもルートがカード名DBをロードして補正すること"
    assert result["best"] is not None
