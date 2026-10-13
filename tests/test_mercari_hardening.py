"""
tests/test_mercari_hardening.py — メルカリ参考相場のレビュー指摘対応の回帰テスト

カード名の区切り記号を省いた出品名でのレアリティ誤判定・形式不正応答の扱い・
負荷対策（同時実行/毎分上限/single-flight）・レアリティ抽出の曖昧さ・追加除外規則。
実通信はしない。
"""
import json
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import mercari
import rarity
import scraper
import app as app_module

KNOWN_NAMES = json.loads((REPO_ROOT / "data" / "cardnames_ja.json").read_text(encoding="utf-8"))


def _item(name, price=1000, **kw):
    it = {"id": "m1", "name": name, "price": str(price), "status": "ITEM_STATUS_ON_SALE",
          "categoryId": 1152, "auction": None, "itemType": "ITEM_TYPE_MERCARI"}
    it.update(kw)
    return it


def _reason(card, title, **kw):
    items = [_item(title, **kw)]
    return mercari.exclusion_reason(card, items[0], mercari.build_name_matcher(card, items, None))


@pytest.fixture(autouse=True)
def _reset():
    mercari._cache.clear()
    mercari._reset_load_state()
    yield
    mercari._cache.clear()
    mercari._reset_load_state()


# ── 指摘1: カード名の除去は区切り記号の有無に左右されない ──

@pytest.mark.parametrize("card,title", [
    ("ソーラー・フレア・ドラゴン", "ソーラーフレアドラゴン"),
    ("ソウル・チャージ", "ソウルチャージ"),
    ("ソウル・チャージ", "遊戯王 ソウルチャージ 美品"),
])
def test_区切りを省いたカード名の一部をレアリティと判定しない(card, title):
    assert mercari.classify_rarity(card, title) is None
    assert mercari.classify_rarity(card, title + " ウルトラ") == "ウルトラ"


def _strip_separators(name):
    t = scraper.normalize_width(name)
    return "".join(c for c in t if not c.isspace() and c not in scraper._FLEX_SEP_ALL)


def test_全カード名から区切りを抜いた表記でレアリティ誤判定しない():
    """reviewerが318件で再現した誤判定の回帰。全カード名について、区切りを抜いた表記が
    そのまま題名になっても（レアリティ表記が無いので）判定できずになること。"""
    bad = []
    for name in KNOWN_NAMES:
        title = _strip_separators(name)
        if not title:
            continue
        got = mercari.classify_rarity(name, title)
        if got is not None:
            bad.append((name, title, got))
    assert not bad, f"{len(bad)}件で誤判定: {bad[:10]}"


# ── 指摘2: items キー無しは取得失敗。0件成功としてキャッシュしない ──

def test_itemsキー無しの応答は失敗でキャッシュされない(monkeypatch):
    class R:
        status_code = 200
        def iter_content(self, n):
            yield b'{"meta": {"numFound": "0"}}'
        def close(self):
            pass
    calls = []
    monkeypatch.setattr(mercari.requests, "post", lambda *a, **k: calls.append(1) or R())
    assert mercari.get_reference("灰流うらら") == {"ok": False, "error": "fetch_failed"}
    assert mercari.get_reference("灰流うらら") == {"ok": False, "error": "fetch_failed"}
    assert len(calls) == 2 and not mercari._cache


def test_本文の受信が合計時間を超えたら失敗(monkeypatch):
    class R:
        status_code = 200
        closed = False
        def iter_content(self, n):
            yield b'{"items": ['
            yield b']}'
        def close(self):
            R.closed = True
    ticks = iter([0, 100, 100, 100, 100])
    monkeypatch.setattr(mercari.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(mercari.requests, "post", lambda *a, **k: R())
    with pytest.raises(mercari.MercariFetchError):
        mercari.fetch_raw("灰流うらら")
    assert R.closed


def test_要求のタイムアウトは接続と読み取りを分けて指定する(monkeypatch):
    seen = {}

    class R:
        status_code = 200
        def iter_content(self, n):
            yield b'{"items": []}'
        def close(self):
            pass

    def fake_post(*a, **k):
        seen.update(k)
        return R()
    monkeypatch.setattr(mercari.requests, "post", fake_post)
    mercari.fetch_raw("x")
    assert seen["timeout"] == (mercari.REQUEST_CONNECT_TIMEOUT_SEC, mercari.REQUEST_READ_TIMEOUT_SEC)
    assert seen["stream"] is True


# ── 指摘4: 負荷対策 ──

def test_同時実行上限を超えたら待たずにbusy(monkeypatch):
    started, release = threading.Event(), threading.Event()
    n = {"c": 0}

    def slow_fetch(kw):
        n["c"] += 1
        started.set()
        release.wait(5)
        return {"items": []}
    monkeypatch.setattr(mercari, "fetch_raw", slow_fetch)
    monkeypatch.setattr(mercari, "MAX_FETCHES_PER_MIN", 1000)
    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(mercari, "_fetch_slots", sem)
    t = threading.Thread(target=lambda: mercari.get_reference("カードA"))
    t.start()
    assert started.wait(5)
    try:
        assert mercari.get_reference("カードB") == {"ok": False, "error": "busy"}
        assert not mercari._cache
    finally:
        release.set()
        t.join(5)
    assert mercari.get_reference("カードB")["ok"] is True   # 空けば通る


def test_毎分上限(monkeypatch):
    monkeypatch.setattr(mercari, "fetch_raw", lambda kw: {"items": []})
    monkeypatch.setattr(mercari, "MAX_FETCHES_PER_MIN", 2)
    assert mercari.get_reference("カードA")["ok"]
    assert mercari.get_reference("カードB")["ok"]
    assert mercari.get_reference("カードC") == {"ok": False, "error": "busy"}
    # キャッシュ命中は数えない
    assert mercari.get_reference("カードA")["cached"] is True


def test_同一カードの同時取得は1回にまとめる(monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []

    def slow_fetch(kw):
        calls.append(kw)
        started.set()
        release.wait(5)
        return {"items": [_item("灰流うらら ウルトラ", 1000)]}
    monkeypatch.setattr(mercari, "fetch_raw", slow_fetch)
    results = []
    threads = [threading.Thread(target=lambda: results.append(mercari.get_reference("灰流うらら")))
               for _ in range(4)]
    threads[0].start()
    assert started.wait(5)
    for t in threads[1:]:
        t.start()
    release.set()
    for t in threads:
        t.join(10)
    assert len(calls) == 1 and len(results) == 4
    assert all(r["ok"] and r["by_rarity"]["ウルトラ"]["min"] == 1000 for r in results)


def test_合流した待ち手にも失敗が伝わり_失敗はキャッシュされない(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def failing(kw):
        started.set()
        release.wait(5)
        raise mercari.MercariFetchError("HTTP 500")
    monkeypatch.setattr(mercari, "fetch_raw", failing)
    results = []
    threads = [threading.Thread(target=lambda: results.append(mercari.get_reference("灰流うらら")))
               for _ in range(3)]
    threads[0].start()
    assert started.wait(5)
    for t in threads[1:]:
        t.start()
    release.set()
    for t in threads:
        t.join(10)
    assert results == [{"ok": False, "error": "fetch_failed"}] * 3
    assert not mercari._cache and not mercari._inflight


def test_API_busyは503で画面側が取得失敗として扱える形(monkeypatch):
    app_module.app.config.update(TESTING=True)
    app_module._reset_rate_limits()
    monkeypatch.setattr(app_module, "_supabase_client", None)
    monkeypatch.setattr(mercari, "MAX_FETCHES_PER_MIN", 0)
    r = app_module.app.test_client().get("/api/mercari", query_string={"name": "灰流うらら"})
    assert r.status_code == 503 and r.get_json() == {"ok": False, "error": "busy"}
    app_module._reset_rate_limits()


# ── 指摘5・6・7: レアリティ抽出 ──

@pytest.mark.parametrize("text,expected", [
    ("クオシク", "25thシークレット"),
    ("クォシク", "25thシークレット"),
    ("25thシークレットレア　クオシク", "25thシークレット"),   # 直後の「レア」は同じ表記の一部
    ("25thシークレットレア", "25thシークレット"),
    ("遊戯王 25th シク", "25thシークレット"),
    ("25th 絵違い シークレット", "25thシークレット"),
    ("20th シークレット 絵違い", "20thシークレット"),
    ("シクレア", "シークレット"),
    ("【シク1枚】", "シークレット"),
    ("美品 R 傷なし", "レア"),
    ("初期 レア", "レア"),
    ("(レア)", "レア"),
])
def test_レアリティ抽出_追加の正例(text, expected):
    assert rarity.extract_rarity_from_text(text) == expected


@pytest.mark.parametrize("text", [
    "スーパーセール",
    "PR プロモ",
    "PRプロモ",
    "激レア",
    "超レア",
    "レア物",
    "高レアリティ",
    "コレクターズ ウルトラ",        # 2種類のレアリティ表記 → 曖昧
    "ウルトラ スーパー",
    "ウルトラレリーフ",
    "SRP",
    "RN",
    "MN",
])
def test_レアリティ抽出_曖昧や誤爆は判定しない(text):
    assert rarity.extract_rarity_from_text(text) is None


def test_同じレアリティの複数表記は曖昧にならない():
    assert rarity.extract_rarity_from_text("ウルトラレア UR ウルトラ") == "ウルトラ"
    assert rarity.extract_rarity_from_text("シークレットレア シク") == "シークレット"


# ── 指摘10: 追加の除外規則 ──

@pytest.mark.parametrize("title,reason", [
    ("遊戯王 灰流うらら ×2", "multi"),
    ("遊戯王 灰流うらら ✖️2", "multi"),
    ("遊戯王 灰流うらら x3", "multi"),
    ("遊戯王 灰流うらら 3体", "multi"),
    ("遊戯王 灰流うらら ○○様専用", "dedicated"),
    ("さ*く様 遊戯王 灰流うらら", "dedicated"),
    ("遊戯王 灰流うらら 専用", "dedicated"),
    ("ボ*ブ様 遊戯王OCG 灰流うらら", "dedicated"),
    ("遊戯王 灰流うらら ○○様用", "dedicated"),
    ("遊戯王 灰流うらら 専用です", "dedicated"),
])
def test_追加の除外規則(title, reason):
    assert _reason("灰流うらら", title) == reason


@pytest.mark.parametrize("title", [
    "遊戯王 灰流うらら 王様",
    "神様 遊戯王 灰流うらら",
    "遊戯王 灰流うらら お客様へ",
    "皆様 遊戯王 灰流うらら",
    "遊戯王 灰流うらら 異様な人気",
    "王様 遊戯王 灰流うらら",
    "遊戯王 灰流うらら 専用ではありません",
    "遊戯王 灰流うらら 専用じゃありません",
    "遊戯王 灰流うらら スリーブ付き",
    "遊戯王 灰流うらら ローダー付き",
    "遊戯王 灰流うらら トップローダー付",
    "灰流うらら【ノーマル】(パラレル仕様)",
    "遊戯王 灰流うらら RC04-JP009 EX2",     # EX2 を ×2 と取り違えない
    "遊戯王 灰流うらら 同様",
])
def test_単品の付属表記や紛らわしい語は除外しない(title):
    assert _reason("灰流うらら", title) is None


def test_ARSは単語として現れるときだけ鑑定品():
    assert _reason("灰流うらら", "遊戯王 灰流うらら ARS10") == "graded"
    assert _reason("灰流うらら", "遊戯王 灰流うらら CARSON") is None


# ── 軽微 ──

def test_中央値は四捨五入():
    items = [_item("灰流うらら ウルトラ", p) for p in (1000, 1001, 1003, 1004)]   # 中央値 1002 → そのまま
    assert mercari.aggregate("灰流うらら", items)["by_rarity"]["ウルトラ"]["median"] == 1002
    items = [_item("灰流うらら ウルトラ", p) for p in (1000, 1001, 1002, 1003)]   # 中央値 1001.5 → 1002
    assert mercari.aggregate("灰流うらら", items)["by_rarity"]["ウルトラ"]["median"] == 1002
    items = [_item("灰流うらら ウルトラ", p) for p in (1000, 1002, 1002, 1004)]   # 中央値 1002
    assert mercari.aggregate("灰流うらら", items)["by_rarity"]["ウルトラ"]["median"] == 1002


# ── 指摘8・軽微: 母数と全件除外 ──

def test_結果に取得件数と全体件数を含む(monkeypatch):
    monkeypatch.setattr(mercari, "fetch_raw", lambda kw: {
        "items": [_item("灰流うらら ウルトラ", 1000), _item("灰流うらら 3枚セット", 900)],
        "meta": {"numFound": "1316"}})
    r = mercari.get_reference("灰流うらら")
    assert r["fetched_count"] == 2 and r["num_found"] == 1316 and r["total_considered"] == 1


def test_numFoundが無くても動く(monkeypatch):
    monkeypatch.setattr(mercari, "fetch_raw", lambda kw: {"items": []})
    r = mercari.get_reference("灰流うらら")
    assert r["ok"] and r["fetched_count"] == 0 and r["num_found"] is None


# ── 再監査の軽微指摘 ──

def test_busyの要求は毎分枠を消費しない(monkeypatch):
    monkeypatch.setattr(mercari, "fetch_raw", lambda kw: {"items": []})
    monkeypatch.setattr(mercari, "MAX_FETCHES_PER_MIN", 1)
    sem = threading.BoundedSemaphore(1)
    sem.acquire()                       # 同時実行枠を埋めておく
    monkeypatch.setattr(mercari, "_fetch_slots", sem)
    for n in "ABC":
        assert mercari.get_reference("カード" + n) == {"ok": False, "error": "busy"}
    sem.release()
    assert mercari.get_reference("カードD")["ok"] is True   # 毎分枠(1)は busy に食われていない


def test_毎分上限で断られたときは同時実行枠を返す(monkeypatch):
    monkeypatch.setattr(mercari, "fetch_raw", lambda kw: {"items": []})
    monkeypatch.setattr(mercari, "MAX_FETCHES_PER_MIN", 0)
    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(mercari, "_fetch_slots", sem)
    assert mercari.get_reference("カードA") == {"ok": False, "error": "busy"}
    assert sem.acquire(blocking=False)  # 返却されている


def test_合流待ちの上限は取得側の最悪時間より長い():
    worst = mercari.REQUEST_TOTAL_TIMEOUT_SEC + mercari.REQUEST_READ_TIMEOUT_SEC
    assert mercari.SINGLE_FLIGHT_WAIT_SEC > worst


def test_合流できる待ち手の数に上限(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow(kw):
        started.set()
        release.wait(5)
        return {"items": []}
    monkeypatch.setattr(mercari, "fetch_raw", slow)
    monkeypatch.setattr(mercari, "MAX_SINGLE_FLIGHT_WAITERS", 1)
    results = []
    leader = threading.Thread(target=lambda: results.append(mercari.get_reference("灰流うらら")))
    leader.start()
    assert started.wait(5)
    waiter = threading.Thread(target=lambda: results.append(mercari.get_reference("灰流うらら")))
    waiter.start()
    for _ in range(100):               # 待ち手が登録されるまで待つ
        if mercari._inflight["灰流うらら"].waiters == 1:
            break
        threading.Event().wait(0.02)
    assert mercari.get_reference("灰流うらら") == {"ok": False, "error": "busy"}   # 2人目は即busy
    release.set()
    leader.join(5)
    waiter.join(5)
    assert len(results) == 2 and all(r["ok"] for r in results)


def test_受信本文が上限を超えたら失敗(monkeypatch):
    class R:
        status_code = 200
        closed = False
        def iter_content(self, n):
            yield b"x" * (mercari.MAX_RESPONSE_BYTES // 2 + 1)
            yield b"x" * (mercari.MAX_RESPONSE_BYTES // 2 + 1)
            raise AssertionError("上限超過後も読み続けた")
        def close(self):
            R.closed = True
    monkeypatch.setattr(mercari.requests, "post", lambda *a, **k: R())
    with pytest.raises(mercari.MercariFetchError):
        mercari.fetch_raw("灰流うらら")
    assert R.closed
