"""
tests/test_mercari.py — メルカリ参考相場（mercari.py / rarity.extract_rarity_from_text / /api/mercari）

実通信はしない。tests/fixtures/mercari/*.json は 2026-10-13 に実APIから取得した生レスポンス
（sellerId/buyerId・画像URL等は削除、集計に使う項目だけ残してある）。
"""
import base64
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import mercari
import rarity
import app as app_module

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "mercari"
KNOWN_NAMES = json.loads((REPO_ROOT / "data" / "cardnames_ja.json").read_text(encoding="utf-8"))


def _load(name):
    d = json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return d["keyword"], d["response"]["items"]


def _item(name, price=1000, **kw):
    it = {"id": "m1", "name": name, "price": str(price), "status": "ITEM_STATUS_ON_SALE",
          "categoryId": 1152, "auction": None, "itemType": "ITEM_TYPE_MERCARI"}
    it.update(kw)
    return it


def _reason(card, title, known=None, **kw):
    items = [_item(title, **kw)]
    matcher = mercari.build_name_matcher(card, items, known)
    return mercari.exclusion_reason(card, items[0], matcher)


# ── 除外規則 ──

@pytest.mark.parametrize("title", [
    "遊戯王 灰流うらら ウルトラ",
    "【即発送】灰流うらら ホログラフィックレア 美品",
    "灰流うらら　RC04-JP009　1枚",
    "遊戯王、灰流うらら　　シークレット　1枚 、匿名発送",
])
def test_対象の出品は除外されない(title):
    assert _reason("灰流うらら", title) is None


@pytest.mark.parametrize("title,reason", [
    ("遊戯王 灰流うらら 【PSA10】", "graded"),
    ("遊戯王 灰流うらら BGS9.5", "graded"),
    ("遊戯王 灰流うらら 鑑定品", "graded"),
    ("遊戯王 灰流うらら 3枚セット", "multi"),
    ("遊戯王 灰流うらら ウルトラ 2枚", "multi"),
    ("遊戯王 灰流うらら まとめ売り", "multi"),
    ("遊戯王 灰流うらら 計8枚", "multi"),
    ("灰流うらら アジア版 シークレット", "overseas"),
    ("灰流うらら 英語版", "overseas"),
    ("灰流うらら 韓国版", "overseas"),
    ("遊戯王 灰流うらら スリーブ 100枚", "multi"),
    ("遊戯王 灰流うらら デュエリストカードプロテクター", "supply"),
    ("遊戯王 灰流うらら プレイマット", "supply"),
    ("灰流うらら バンダイ版", "non_ocg"),
    ("遊戯王 無限泡影", "name_mismatch"),
])
def test_除外規則(title, reason):
    assert _reason("灰流うらら", title) == reason


def test_オークションと価格不正は除外():
    assert _reason("灰流うらら", "灰流うらら", auction={"id": "x"}) == "auction"
    assert _reason("灰流うらら", "灰流うらら", price="abc") == "bad_price"
    assert _reason("灰流うらら", "灰流うらら", price="0") == "bad_price"


def test_売り切れは除外():
    assert _reason("灰流うらら", "灰流うらら", status="ITEM_STATUS_SOLD_OUT") == "not_on_sale"


def test_全角半角の差は無視してカード名を照合する():
    assert _reason("増殖するG", "遊戯王 増殖するＧ ウルトラ") is None
    assert _reason("増殖するG", "遊戯王 増殖するg ウルトラ") is None


def test_より長い別カード名の一部は対象外():
    # ブラック・マジシャン → ブラック・マジシャン・ガール は別カード
    assert _reason("ブラック・マジシャン", "遊戯王 ブラック・マジシャン・ガール ウルトラ", KNOWN_NAMES) == "name_mismatch"
    assert _reason("ブラック・マジシャン", "遊戯王 ブラックマジシャン ウルトラ", KNOWN_NAMES) is None
    assert _reason("青眼の白龍", "遊戯王 白き幻獣－青眼の白龍", KNOWN_NAMES) == "name_mismatch"


def test_別カード名が並ぶ詰め合わせは除外():
    assert _reason("灰流うらら", "遊戯王 灰流うらら 無限泡影 ウルトラ", KNOWN_NAMES) == "name_mismatch"
    # known_names が無いときは判定できないので含有のみ（ここでは通る）
    assert _reason("灰流うらら", "遊戯王 灰流うらら 無限泡影 ウルトラ") is None


def test_除外語がカード名の一部でも誤爆しない():
    # カード名自体に「セット」を含む場合、カード名部分は除外語の判定から外す
    assert _reason("ラッシュセット", "遊戯王 ラッシュセット ウルトラ") is None


# ── レアリティ抽出 ──

@pytest.mark.parametrize("text,expected", [
    ("ウルトラ", "ウルトラ"),
    ("ウルトラレア", "ウルトラ"),
    ("ウルトラ パラレル", "ウルトラパラレル"),          # 長い別名が優先
    ("ゴールドシークレット", "ゴールドシークレット"),      # 「シークレット」より先
    ("ゴルシク", "ゴールドシークレット"),
    ("プリズマティックシークレットレア", "プリシク"),
    ("クォーターセンチュリーシークレット", "25thシークレット"),
    ("25th シークレット", "25thシークレット"),
    ("20thシークレット", "20thシークレット"),
    ("SR", "スーパー"),
    ("【SR】", "スーパー"),
    ("(SR)", "スーパー"),
    ("SR-P", "スーパーパラレル"),
    ("レリーフ", "アルティメット"),
    ("初期 レア", "レア"),
    ("N", "ノーマル"),
    ("ノーマル", "ノーマル"),
    ("ホロ", "ホログラフィック"),
])
def test_レアリティ抽出_正例(text, expected):
    assert rarity.extract_rarity_from_text(text) == expected


@pytest.mark.parametrize("text", [
    "",
    "遊戯王 灰流うらら",
    "RC04-JP009",                        # 型番の R / JP を略号と取り違えない
    "遊戯王 EX-49",                       # EX は除外別名
    "SM-51",
    "高レアリティ多数",                    # 「レア」の誤爆
    "レア度高め",
    "遊戯王アートワークコレクション",        # 「コレ」の誤爆（実データ）
    "オルフェゴール ガラテア ディヴェル カノーネ",   # 「ノー」の誤爆（実データ）
    "美品 即発送 匿名配送",
    "ABCSRDEF",                          # 英字に埋もれた略号
])
def test_レアリティ抽出_誤爆しない(text):
    assert rarity.extract_rarity_from_text(text) is None


def test_既存のnormalize_rarityは変わらない():
    assert rarity.normalize_rarity("UR") == "ウルトラ"
    assert rarity.normalize_rarity("ウルトラレア") == "ウルトラ"
    assert rarity.normalize_rarity("ホロ") == "ホログラフィック"


def test_classify_カード名と型番は先に除いてから判定する():
    assert mercari.classify_rarity("増殖するG", "遊戯王 増殖するG RC04-JP005") is None
    assert mercari.classify_rarity("増殖するG", "遊戯王 増殖するG RC04-JP005 ウルトラレア") == "ウルトラ"


@pytest.fixture(autouse=True)
def _clear_caches():
    mercari._cache.clear()
    yield
    mercari._cache.clear()


# ── 集計 ──

def test_中央値は件数が閾値以上のときだけ出す():
    n = mercari.MERCARI_MEDIAN_MIN_COUNT
    items = [_item("灰流うらら ウルトラ", 1000 + i * 100) for i in range(n)]
    items += [_item("灰流うらら スーパー", 500 + i * 100) for i in range(n - 1)]
    agg = mercari.aggregate("灰流うらら", items)
    assert agg["by_rarity"]["ウルトラ"]["median"] == 1100
    assert agg["by_rarity"]["ウルトラ"]["count"] == n
    assert agg["by_rarity"]["スーパー"]["median"] is None
    assert agg["by_rarity"]["スーパー"]["min"] == 500


def test_最低出品価格300円はmin_is_floor():
    items = [_item("増殖するG ウルトラ", 300), _item("増殖するG ウルトラ", 350),
             _item("増殖するG スーパー", 301)]
    agg = mercari.aggregate("増殖するG", items)
    assert agg["by_rarity"]["ウルトラ"]["min_is_floor"] is True
    assert agg["by_rarity"]["スーパー"]["min_is_floor"] is False


def test_判定できなかった件数と母数():
    items = [_item("灰流うらら ウルトラ", 1000), _item("灰流うらら", 500),
             _item("灰流うらら まとめ売り", 100), _item("無限泡影 ウルトラ", 100)]
    agg = mercari.aggregate("灰流うらら", items)
    assert agg["unclassified_count"] == 1
    assert agg["total_considered"] == 2
    assert list(agg["by_rarity"]) == ["ウルトラ"]


def test_by_rarityは既存のレアリティ順():
    items = [_item("灰流うらら ノーマル", 100), _item("灰流うらら ウルトラ", 100),
             _item("灰流うらら シークレット", 100)]
    assert list(mercari.aggregate("灰流うらら", items)["by_rarity"]) == ["シークレット", "ウルトラ", "ノーマル"]


# ── 実データ fixture（2026-10-13 取得） ──

@pytest.mark.parametrize("fx", ["hairu", "G", "blue", "senkoku"])
def test_fixtureの集計が妥当(fx):
    kw, items = _load(fx)
    agg = mercari.aggregate(kw, items, KNOWN_NAMES)
    assert agg["total_considered"] > 0
    assert (sum(v["count"] + (v["set"]["count"] if v["set"] else 0) for v in agg["by_rarity"].values())
            + agg["unclassified_count"] == agg["total_considered"])
    assert agg["unclassified_count"] == agg["unclassified_single"] + agg["unclassified_set"]
    matcher = mercari.build_name_matcher(kw, items, KNOWN_NAMES)
    for it in items:
        reason, qty = mercari.evaluate_listing(kw, it, matcher)
        if reason is None:
            n = it["name"].upper()
            # 鑑定品・海外版・サプライが混ざっていない
            for bad in ("PSA", "BGS", "鑑定", "まとめ", "アジア", "スリーブ", "プロテクター", "英語"):
                assert bad not in n, it["name"]
            if qty == 1:   # 単品に複数枚・セットが混ざっていない
                assert "セット" not in n and not mercari.MULTI_PATTERN.search(it["name"]), it["name"]
            else:
                assert qty in mercari.SET_QTY_CHOICES
    for r, v in agg["by_rarity"].items():
        assert v["min"] is None or v["min"] >= 300
        assert v["median"] is None or v["median"] >= v["min"]
        assert v["median"] is None or v["count"] >= mercari.MERCARI_MEDIAN_MIN_COUNT


def test_fixture_灰流うらら_既知の除外():
    kw, items = _load("hairu")
    matcher = mercari.build_name_matcher(kw, items, KNOWN_NAMES)
    by_reason = {}
    for it in items:
        by_reason.setdefault(mercari.exclusion_reason(kw, it, matcher), []).append(it["name"])
    assert any("PSA10" in n for n in by_reason["graded"])
    assert any("3枚セット" in n for n in by_reason["multi"])
    assert any("アジア版" in n for n in by_reason["overseas"])
    assert len(by_reason[None]) > 0


# ── 検索リンク ──

def test_検索URL():
    url = mercari.build_search_url("灰流うらら")
    assert url.startswith("https://jp.mercari.com/search?keyword=")
    assert "%E7%81%B0%E6%B5%81" in url and "status=on_sale" in url and "category_id=1152" in url
    # カード名中の & が別パラメータにならない
    assert "A%26B" in mercari.build_search_url("A&B")


def test_検索URL_afidあり(monkeypatch):
    monkeypatch.setenv("MERCARI_AFID", "8033172568")
    url = mercari.build_search_url("増殖するG")
    assert "afid=8033172568" in url and "status=on_sale" in url and "category_id=1152" in url
    assert mercari.get_afid() == "8033172568"


def test_検索URL_afidなし_空_不正値(monkeypatch, caplog):
    monkeypatch.delenv("MERCARI_AFID", raising=False)
    assert "afid" not in mercari.build_search_url("x")
    monkeypatch.setenv("MERCARI_AFID", "  ")
    assert "afid" not in mercari.build_search_url("x")
    # 不正値: 付けずにWARNは1回だけ
    monkeypatch.setattr(mercari, "_AFID_WARNED", False)
    monkeypatch.setenv("MERCARI_AFID", "12a&x=1")
    with caplog.at_level("WARNING"):
        assert "afid" not in mercari.build_search_url("x")
        assert "afid" not in mercari.build_search_url("y")
    assert sum("MERCARI_AFID" in r.getMessage() for r in caplog.records) == 1


# ── DPoP ──

def test_dpop署名はES256で検証できる():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    def unb64(s):
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    token = mercari._dpop_token("POST", mercari.SEARCH_API_URL)
    h, p, sig = token.split(".")
    header, payload = json.loads(unb64(h)), json.loads(unb64(p))
    assert header["typ"] == "dpop+jwt" and header["alg"] == "ES256"
    assert header["jwk"]["crv"] == "P-256" and header["jwk"]["kty"] == "EC"
    assert payload["htm"] == "POST" and payload["htu"] == mercari.SEARCH_API_URL
    assert payload["jti"] and payload["uuid"] and isinstance(payload["iat"], int)
    raw = unb64(sig)
    assert len(raw) == 64
    pub = ec.EllipticCurvePublicNumbers(
        int.from_bytes(unb64(header["jwk"]["x"]), "big"),
        int.from_bytes(unb64(header["jwk"]["y"]), "big"), ec.SECP256R1()).public_key()
    pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
               f"{h}.{p}".encode(), ec.ECDSA(hashes.SHA256()))


def test_リクエストボディは出品中のみ_遊戯王シングル_1ページ():
    body = mercari._build_request_body("灰流うらら")
    cond = body["searchCondition"]
    assert cond["status"] == ["STATUS_ON_SALE"] and cond["categoryId"] == [1152]
    assert cond["keyword"] == "灰流うらら" and body["pageSize"] == 120 and body["pageToken"] == ""


# ── 通信エラーの区別 ──

class _Resp:
    def __init__(self, status=200, data=None, bad_json=False, chunks=None):
        self.status_code, self._data, self._bad, self._chunks = status, data, bad_json, chunks
        self.closed = False

    def iter_content(self, size):
        if self._chunks is not None:
            yield from self._chunks
            return
        yield b"<html>" if self._bad else json.dumps(self._data).encode("utf-8")

    def close(self):
        self.closed = True


@pytest.mark.parametrize("resp", [_Resp(401), _Resp(403), _Resp(429), _Resp(500), _Resp(bad_json=True),
                                  _Resp(data=[1, 2]), _Resp(data={"items": "x"}),
                                  _Resp(data={}), _Resp(data={"meta": {"numFound": "0"}})])  # items キー無しは失敗
def test_fetch_rawの失敗はMercariFetchError(monkeypatch, resp):
    monkeypatch.setattr(mercari.requests, "post", lambda *a, **k: resp)
    with pytest.raises(mercari.MercariFetchError):
        mercari.fetch_raw("灰流うらら")


def test_fetch_rawのタイムアウトはMercariFetchError(monkeypatch):
    def boom(*a, **k):
        raise mercari.requests.Timeout()
    monkeypatch.setattr(mercari.requests, "post", boom)
    with pytest.raises(mercari.MercariFetchError):
        mercari.fetch_raw("灰流うらら")


def test_fetch_rawの0件は失敗ではない(monkeypatch):
    monkeypatch.setattr(mercari.requests, "post", lambda *a, **k: _Resp(data={"items": []}))
    assert mercari.fetch_raw("灰流うらら")["items"] == []


# ── キャッシュ ──

def test_get_reference_成功はキャッシュ_失敗はキャッシュしない(monkeypatch):
    calls = []

    def fake_fetch(kw):
        calls.append(kw)
        if len(calls) == 1:
            raise mercari.MercariFetchError("HTTP 500")
        return {"items": [_item("灰流うらら ウルトラ", 1000)]}
    monkeypatch.setattr(mercari, "fetch_raw", fake_fetch)
    assert mercari.get_reference("灰流うらら") == {"ok": False, "error": "fetch_failed"}
    r1 = mercari.get_reference("灰流うらら")
    assert r1["ok"] and r1["cached"] is False and r1["by_rarity"]["ウルトラ"]["min"] == 1000
    r2 = mercari.get_reference("灰流うらら")
    assert r2["cached"] is True and len(calls) == 2


def test_get_reference_キャッシュは期限切れで再取得(monkeypatch):
    calls = []
    monkeypatch.setattr(mercari, "fetch_raw", lambda kw: calls.append(1) or {"items": []})
    mercari.get_reference("灰流うらら")
    key = next(iter(mercari._cache))
    t, v = mercari._cache[key]
    mercari._cache[key] = (t - mercari.CACHE_TTL_SEC - 1, v)
    mercari.get_reference("灰流うらら")
    assert len(calls) == 2


def test_型番補完はせずSupabaseに一切問い合わせない(monkeypatch):
    """型番補完は不採用（docs/decisions.md 2026-10-13）。リクエスト経路でDBを呼ばないことを固定する。"""
    calls = []

    class Spy:
        def __getattr__(self, name):
            calls.append(name)
            raise AssertionError("Supabase を呼んではいけない")
    monkeypatch.setattr(app_module, "_supabase_client", Spy())
    monkeypatch.setattr(mercari, "fetch_raw", lambda kw: {"items": [_item("灰流うらら RC04-JP009", 500)]})
    app_module.app.config.update(TESTING=True)
    app_module._reset_rate_limits()
    d = app_module.app.test_client().get("/api/mercari", query_string={"name": "灰流うらら"}).get_json()
    app_module._reset_rate_limits()
    assert d["ok"] is True
    # 題名にレアリティが無ければ、型番があっても判定できずのまま
    assert d["unclassified_count"] == 1 and d["by_rarity"] == {}
    assert calls == []
    assert not hasattr(mercari, "load_code_rarity_map") and not hasattr(mercari, "extract_code")


# ── API ──

@pytest.fixture()
def client(monkeypatch):
    app_module.app.config.update(TESTING=True)
    app_module._reset_rate_limits()
    monkeypatch.setattr(app_module, "_supabase_client", None)
    yield app_module.app.test_client()
    app_module._reset_rate_limits()


def test_api_成功(client, monkeypatch):
    monkeypatch.setattr(mercari, "fetch_raw", lambda kw: {"items": [
        _item("灰流うらら ウルトラ", 1000), _item("灰流うらら ウルトラ", 1200), _item("灰流うらら ウルトラ", 1400),
        _item("灰流うらら", 300)]})
    r = client.get("/api/mercari", query_string={"name": "灰流うらら"})
    d = r.get_json()
    assert r.status_code == 200 and d["ok"] is True and d["cached"] is False
    assert d["by_rarity"]["ウルトラ"] == {"count": 3, "min": 1000, "median": 1200, "min_is_floor": False, "set": None}
    assert d["unclassified_count"] == 1 and d["total_considered"] == 4
    assert d["search_url"].startswith("https://jp.mercari.com/search?keyword=")
    assert d["affiliate"] is False


def test_api_affiliateフラグはキャッシュ越しでも現在の環境変数に従う(client, monkeypatch):
    monkeypatch.setattr(mercari, "fetch_raw", lambda kw: {"items": [_item("灰流うらら ウルトラ", 1000)]})
    monkeypatch.delenv("MERCARI_AFID", raising=False)
    d1 = client.get("/api/mercari", query_string={"name": "灰流うらら"}).get_json()
    assert d1["affiliate"] is False and "afid" not in d1["search_url"]
    monkeypatch.setenv("MERCARI_AFID", "8033172568")
    app_module._reset_rate_limits()   # 同一IPの連続要求をレート制限で弾かせない
    d2 = client.get("/api/mercari", query_string={"name": "灰流うらら"}).get_json()
    assert d2["cached"] is True and d2["affiliate"] is True and "afid=8033172568" in d2["search_url"]


def test_api_取得失敗(client, monkeypatch):
    def boom(kw):
        raise mercari.MercariFetchError("HTTP 403")
    monkeypatch.setattr(mercari, "fetch_raw", boom)
    r = client.get("/api/mercari", query_string={"name": "灰流うらら"})
    assert r.get_json() == {"ok": False, "error": "fetch_failed"}


def test_api_0件は成功として返す(client, monkeypatch):
    monkeypatch.setattr(mercari, "fetch_raw", lambda kw: {"items": []})
    d = client.get("/api/mercari", query_string={"name": "灰流うらら"}).get_json()
    assert d["ok"] is True and d["by_rarity"] == {} and d["total_considered"] == 0


def test_api_レート制限(client, monkeypatch):
    monkeypatch.setattr(mercari, "fetch_raw", lambda kw: {"items": []})
    assert client.get("/api/mercari", query_string={"name": "灰流うらら"}).status_code == 200
    r = client.get("/api/mercari", query_string={"name": "灰流うらら"})
    assert r.status_code == 429 and r.get_json() == {"ok": False, "error": "rate_limited"}


def test_api_レート制限は他APIのバケットと独立(client, monkeypatch):
    monkeypatch.setattr(mercari, "fetch_raw", lambda kw: {"items": []})
    with app_module.app.test_request_context("/"):
        app_module._consume_rate_limit()   # 価格検索バケットを消費
    assert client.get("/api/mercari", query_string={"name": "灰流うらら"}).status_code == 200


def test_api_入力検証(client):
    assert client.get("/api/mercari").status_code == 400
    assert client.get("/api/mercari", query_string={"name": "  "}).status_code == 400
    app_module._reset_rate_limits()
    r = client.get("/api/mercari", query_string={"name": "あ" * (app_module.MAX_CARD_NAME_LEN + 1)})
    assert r.status_code == 400 and r.get_json()["ok"] is False


def test_api_未知のカード名は外部に問い合わせない(client, monkeypatch):
    def must_not_call(kw):
        raise AssertionError("外部通信してはいけない")
    monkeypatch.setattr(mercari, "fetch_raw", must_not_call)
    r = client.get("/api/mercari", query_string={"name": "存在しないカード名ZZZ"})
    assert r.status_code == 404 and r.get_json()["error"] == "unknown_card"


def test_メルカリは店舗一覧に入っていない():
    assert all("メルカリ" not in name for name, _ in app_module.SHOPS)
    assert all("メルカリ" not in name for name in app_module.DEFAULT_SHOPS)
