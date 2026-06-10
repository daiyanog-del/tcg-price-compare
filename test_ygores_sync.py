"""
ygores_repository / sync_ygores のロジックテスト（外部通信なし）
================================================================
実行: python test_ygores_sync.py

検証項目（受け入れ基準）:
- リビジョン不変 → 同期は何もしない
- 変更あり → ローカル保持パスのみ再取得（保持外の変更は無視）
- API完全遮断でもキャッシュ済みデータの読み出しが動く
- 全リクエストに固有User-Agentとレートリミット（1秒間隔）が適用される
- サーキットブレーカー（連続失敗でAPIアクセス停止）
"""

import time

import ygores_repository
from ygores_repository import (
    YgoResourcesClient, CardDataRepository, USER_AGENT, NAME_INDEX_PATH,
)
import sync_ygores
from sync_ygores import run_sync, flatten_manifest, META_KEY_REVISION


# ---------------------------------------------------------------- テスト用フェイク

CARD_JSON = {"cardData": {"ja": {
    "id": 4007, "name": "青眼の白龍", "cardType": "monster",
    "atk": 3000, "def": 2500, "level": 8, "properties": [],
    "effectText": "テキスト", "prints": [{"code": "LOB-001", "date": "2002-08-08"}],
}}}


class FakeClient:
    """YgoResourcesClient 互換。pathごとの応答を辞書で指定し、呼び出しを記録する"""

    def __init__(self, responses=None, revision=None):
        self.responses = responses or {}
        self.last_revision = revision
        self.calls = []

    def get_json(self, path, timeout=15):
        self.calls.append(path)
        return self.responses.get(path)


class FakeRepo(CardDataRepository):
    """Supabaseをインメモリ辞書に置き換えたrepository"""

    def __init__(self, client):
        super().__init__(client=client)
        self.store = {"ygores_cards": {}, "ygores_qa": {},
                      "ygores_blobs": {}, "ygores_sync_meta": {}}
        self._keycols = {"ygores_cards": "konami_id", "ygores_qa": "qa_id",
                         "ygores_blobs": "path", "ygores_sync_meta": "key"}

    def _supabase(self):
        return self.store  # None でなければ接続済み扱い

    def _cache_select(self, table, key_col, key, value_col="raw"):
        row = self.store[table].get(key)
        return row.get(value_col) if row else None

    def _cache_upsert(self, table, row):
        self.store[table][row[self._keycols[table]]] = row
        return True

    def _select_all_keys(self, table, column):
        return sorted(self.store[table].keys())


def make_repo(responses=None, revision=None):
    return FakeRepo(FakeClient(responses, revision))


# ---------------------------------------------------------------- repository のテスト

def test_cache_first_and_write_through():
    """初回はAPI→write-through、2回目はキャッシュのみ（API呼び出しなし）"""
    repo = make_repo({"data/card/4007": CARD_JSON})
    raw = repo.get_card_raw(4007)
    assert raw == CARD_JSON
    assert repo.client.calls == ["data/card/4007"]
    assert repo.store["ygores_cards"][4007]["name"] == "青眼の白龍"  # write-through 確認
    raw2 = repo.get_card_raw(4007)
    assert raw2 == CARD_JSON
    assert repo.client.calls == ["data/card/4007"]  # 2回目はAPIを呼ばない


def test_api_down_serves_from_cache():
    """API完全遮断（全てNone）でもキャッシュ済みデータで応答する"""
    repo = make_repo(responses={})  # 全パスNone = API全断
    repo.save_card(4007, CARD_JSON)
    repo.save_blob(NAME_INDEX_PATH, {"青眼の白龍": [4007]})

    summary = repo.get_card_summary(4007)
    assert summary["name"] == "青眼の白龍"
    assert summary["card_type"] == "monster"
    assert summary["atk"] == 3000
    assert repo.get_name_index() == {"青眼の白龍": [4007]}
    assert "data/card/4007" not in repo.client.calls  # キャッシュ優先でAPI不要


def test_api_down_and_no_cache():
    """API断かつキャッシュなし → None/{} を返す（例外にしない）"""
    repo = make_repo(responses={})
    assert repo.get_card_summary(99999) is None
    assert repo.get_name_index() == {}


# ---------------------------------------------------------------- sync のテスト

def test_sync_revision_unchanged():
    """リビジョン不変 → manifestを見ず、何も再取得しない"""
    repo = make_repo({"data/card/4007": CARD_JSON}, revision=100)
    repo.save_card(4007, CARD_JSON)
    repo.set_sync_meta(META_KEY_REVISION, 100)
    assert run_sync(repo) == 0
    assert repo.client.calls == ["data/card/4007"]  # revision確認の1回のみ
    assert repo.get_sync_meta(META_KEY_REVISION) == "100"


def test_sync_refetches_only_held_paths():
    """変更あり → 保持しているパスだけ再取得し、保持外の変更は無視する"""
    manifest = {"data": {
        "card": {"4007": 1, "99999": 1},          # 99999 は保持していない → 無視
        "idx": {"card": {"name": {"ja": 1}}},     # 保持している blob → 再取得
        "qa": {"123": 1},                          # 保持していない → 無視
    }}
    repo = make_repo({
        "data/card/4007": CARD_JSON,
        "manifest/100": manifest,
        f"data/{NAME_INDEX_PATH}": {"青眼の白龍": [4007]},
    }, revision=105)
    repo.save_card(4007, {"cardData": {"ja": {"name": "古いデータ"}}})
    repo.save_blob(NAME_INDEX_PATH, {"古い": [1]})
    repo.set_sync_meta(META_KEY_REVISION, 100)

    assert run_sync(repo) == 0
    assert "manifest/100" in repo.client.calls
    assert "data/card/4007" in repo.client.calls            # 保持分は再取得
    assert "data/card/99999" not in repo.client.calls       # 保持外は無視
    assert "data/qa/123" not in repo.client.calls
    assert repo.store["ygores_cards"][4007]["name"] == "青眼の白龍"  # 更新された
    assert repo.get_name_index() == {"青眼の白龍": [4007]}
    assert repo.get_sync_meta(META_KEY_REVISION) == "105"   # リビジョン更新


def test_sync_first_run_stores_revision_only():
    """初回（保存済みリビジョンなし）→ リビジョン保存のみで再取得しない"""
    repo = make_repo({"data/card/4007": CARD_JSON}, revision=100)
    assert run_sync(repo) == 0
    assert repo.get_sync_meta(META_KEY_REVISION) == "100"
    assert repo.client.calls == ["data/card/4007"]


def test_sync_manifest_failure_falls_back_to_full_refetch():
    """manifest取得失敗（リビジョン飛び）→ 保持パス全件を再取得"""
    repo = make_repo({
        "data/card/4007": CARD_JSON,
        # manifest/50 は応答なし（None）
    }, revision=105)
    repo.save_card(4007, {"cardData": {"ja": {"name": "古いデータ"}}})
    repo.set_sync_meta(META_KEY_REVISION, 50)
    assert run_sync(repo) == 0
    assert repo.client.calls.count("data/card/4007") == 2  # revision確認 + 全件再取得
    assert repo.get_sync_meta(META_KEY_REVISION) == "105"


# ---------------------------------------------------------------- ミラー形式の正規化テスト
# 実データ（github.com/db-ygoresources-com/yugioh-card-history）から取得したサンプル

MIRROR_XYZ = {"id": 11932, "type": "monster", "name": "SNo.0 ホープ・ゼアル",
              "englishAttribute": "light", "effectText": "...", "rank": 0,
              "atk": -1, "def": -1, "properties": ["戦士族", "エクシーズ", "効果"]}
MIRROR_LINK = {"id": 13036, "type": "monster", "name": "デコード・トーカー",
               "englishAttribute": "dark", "effectText": "...", "linkRating": 3,
               "linkArrows": ["8", "1", "3"], "atk": 2300,
               "properties": ["サイバース族", "リンク", "効果"]}
MIRROR_FUSION = {"id": 11878, "type": "monster", "name": "旧神ヌトス",
                 "englishAttribute": "light", "level": 4, "atk": 2500, "def": 1200,
                 "properties": ["天使族", "融合", "効果"]}
MIRROR_NORMAL_MON = {"id": 4925, "type": "monster", "name": "ボアソルジャー",
                     "englishAttribute": "earth", "level": 4, "atk": 2000, "def": 500,
                     "properties": ["獣戦士族", "効果"]}
MIRROR_QUICKPLAY = {"id": 12175, "type": "spell", "name": "ツインツイスター",
                    "englishAttribute": "spell", "englishProperty": "quickplay",
                    "localizedProperty": "速攻", "effectText": "..."}


def _summ(raw):
    return CardDataRepository._summarize(raw["id"], raw)


def test_mirror_ex_detection():
    """ミラー形式: エクシーズ/リンク/融合は is_ex=True、通常モンスターは False"""
    assert _summ(MIRROR_XYZ)["is_ex"] is True
    assert _summ(MIRROR_LINK)["is_ex"] is True
    assert _summ(MIRROR_FUSION)["is_ex"] is True
    assert _summ(MIRROR_NORMAL_MON)["is_ex"] is False
    assert _summ(MIRROR_QUICKPLAY)["is_ex"] is False  # 魔法はEX対象外


def test_mirror_field_mapping():
    """ミラー形式: ランク/リンク/種族/属性/サブ種別が正しく取れる"""
    xyz = _summ(MIRROR_XYZ)
    assert xyz["rank"] == 0 and xyz["level"] is None
    assert xyz["race_ja"] == "戦士族"
    assert xyz["attribute"] == "light"

    link = _summ(MIRROR_LINK)
    assert link["link_rating"] == 3
    assert link["race_ja"] == "サイバース族"

    spell = _summ(MIRROR_QUICKPLAY)
    assert spell["card_type"] == "spell"
    assert spell["property"] == "quickplay"   # app側で _PROP_JA により「速攻」に変換される
    assert spell["race_ja"] is None
    assert spell["prints"] == []              # ミラーは収録情報を持たない


def test_api_format_still_works():
    """API形式（cardData.ja）も従来通り正規化できる（整数propertiesでEX判定）"""
    api_xyz = {"cardData": {"ja": {"name": "X", "cardType": "monster",
                                   "properties": [18], "atk": 100}}}
    s = CardDataRepository._summarize(1, api_xyz)
    assert s["is_ex"] is True and s["card_type"] == "monster"
    api_normal = {"cardData": {"ja": {"name": "Y", "cardType": "monster",
                                      "properties": [], "atk": 100}}}
    assert CardDataRepository._summarize(2, api_normal)["is_ex"] is False


def test_card_row_handles_both_formats():
    """_card_row はミラー形式・API形式どちらからも name/card_type を取り出す"""
    r1 = CardDataRepository._card_row(11932, MIRROR_XYZ, "now")
    assert r1["name"] == "SNo.0 ホープ・ゼアル" and r1["card_type"] == "monster"
    api = {"cardData": {"ja": {"name": "青眼", "cardType": "monster"}}}
    r2 = CardDataRepository._card_row(4007, api, "now")
    assert r2["name"] == "青眼" and r2["card_type"] == "monster"


def test_flatten_manifest():
    paths = flatten_manifest({"data": {
        "card": {"100": 1}, "qa": {"5": 1},
        "idx": {"card": {"name": {"ja": 1}}},
        "meta": {"recent": {"ja": {"qa": 1}}},
    }})
    assert paths == {"card/100", "qa/5", "idx/card/name/ja", "meta/recent/ja/qa"}


# ---------------------------------------------------------------- クライアントのテスト

class FakeSession:
    """requests.Session 互換。応答を記録・偽装する"""

    class Resp:
        def __init__(self, status, headers, body):
            self.status_code = status
            self.headers = headers
            self._body = body

        def json(self):
            return self._body

    def __init__(self, status=200, revision="100", fail_times=0):
        self.requests = []   # (time, url, headers)
        self.status = status
        self.revision = revision
        self.fail_times = fail_times

    def get(self, url, timeout=None, headers=None):
        self.requests.append((time.monotonic(), url, headers or {}))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("simulated network error")
        return self.Resp(self.status, {"X-Cache-Revision": self.revision}, {"ok": True})


def test_user_agent_and_rate_limit():
    """全リクエストに固有UAが付き、リクエスト間隔が1秒以上あく"""
    session = FakeSession()
    client = YgoResourcesClient(session=session)
    assert client.get_json("data/card/1") == {"ok": True}
    assert client.get_json("data/card/2") == {"ok": True}
    assert client.last_revision == 100
    for _, url, headers in session.requests:
        assert headers["User-Agent"] == USER_AGENT
        assert url.startswith("https://db.ygoresources.com/")
    interval = session.requests[1][0] - session.requests[0][0]
    assert interval >= ygores_repository.MIN_REQUEST_INTERVAL_SEC * 0.95, \
        f"リクエスト間隔が短すぎます: {interval:.3f}s"


def test_circuit_breaker_opens_after_failures():
    """連続失敗でサーキットブレーカーが開き、以降のAPI呼び出しを止める"""
    # テスト高速化のため待ち時間をゼロにする（ロジックは不変）
    orig_backoff = ygores_repository.BACKOFF_BASE_SEC
    orig_interval = ygores_repository.MIN_REQUEST_INTERVAL_SEC
    ygores_repository.BACKOFF_BASE_SEC = 0.0
    ygores_repository.MIN_REQUEST_INTERVAL_SEC = 0.0
    try:
        session = FakeSession(fail_times=10 ** 6)
        client = YgoResourcesClient(session=session)
        for _ in range(ygores_repository.CIRCUIT_FAILURE_THRESHOLD):
            assert client.get_json("data/card/1") is None
        assert not client.api_available()         # ブレーカーが開いた
        n = len(session.requests)
        assert client.get_json("data/card/1") is None
        assert len(session.requests) == n         # 開いている間はHTTPに到達しない
    finally:
        ygores_repository.BACKOFF_BASE_SEC = orig_backoff
        ygores_repository.MIN_REQUEST_INTERVAL_SEC = orig_interval


def test_api_disabled_env(monkey_env="YGORES_API_DISABLED"):
    """YGORES_API_DISABLED=1 でAPIに一切到達しない（完全遮断スイッチ）"""
    import os
    session = FakeSession()
    client = YgoResourcesClient(session=session)
    os.environ[monkey_env] = "1"
    try:
        assert client.get_json("data/card/1") is None
        assert session.requests == []
    finally:
        del os.environ[monkey_env]


# ---------------------------------------------------------------- 実行

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  OK   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 件成功")
    raise SystemExit(1 if failed else 0)
