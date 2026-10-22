"""
tests/test_shop_cache_kv.py — 店舗検索キャッシュの Key Value 置き場（shop_cache_kv）の検証

確かめること:
  - Key Value に置いたものを読める（ファイルには書かない）・partial の印・販売と買取の分離
  - 店舗ごとの通常の期限（SHOP_CACHE_TTL_MINUTES）が効く。期限切れでも「前回の結果」としては読める
  - キーの期限（TTL）が切れたら消える
  - Key Value に届かない・上限超え・使用量を確かめられないとき、検索を止めずにファイルへ
  - 不調中にファイルへ書いた新しい値が、復旧後に Key Value の古い値より優先される
  - 接続の空き待ちの時間切れ・命令の拒否は停止扱いにしない。接続の失敗だけ停止扱い
  - 停止期間が明けたら1スレッドだけが試す
  - カーナベルの他TCG除外が Key Value から読んだ分にも効く・CACHE_ENABLED=False で触れない

Key Value の経路のテストは、メモリ上の偽物（_FakeRedis。CI でも動く）と、本物の Redis 互換サーバー
（環境変数 SHOP_CACHE_TEST_REDIS_URL があるときだけ。例 redis://localhost:6390/15）の両方で流す。
"""

import os
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import redis

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scraper
import shop_cache_kv

REAL_URL = os.environ.get("SHOP_CACHE_TEST_REDIS_URL", "")

# 何も待ち受けていないポート（届かない Key Value の代わり）
UNREACHABLE_URL = "redis://127.0.0.1:1/0"
_REAL_NEW_CLIENT = shop_cache_kv._new_client


def _row(shop, price=100, name="テストカード"):
    return {"shop": shop, "name": name, "rarity": "レア", "code": "",
            "condition": "-", "price": price, "stock": 1, "sold_out": False,
            "url": "", "image": ""}


class _FakeRedis:
    """テストに要る命令だけを真似る（ハッシュ・期限・INFO・MULTI）。"""

    def __init__(self):
        self.h: dict[str, dict] = {}
        self.exp: dict[str, float] = {}
        self.used_memory = 1_000_000

    def _alive(self, k):
        if k in self.exp and time.monotonic() >= self.exp[k]:
            self.h.pop(k, None)
            self.exp.pop(k, None)
        return k in self.h

    def hmget(self, k, fields):
        d = self.h.get(k, {}) if self._alive(k) else {}
        return [d.get(f) for f in fields]

    def hgetall(self, k):
        return {f.encode(): v for f, v in self.h.get(k, {}).items()} if self._alive(k) else {}

    def hget(self, k, f):
        return self.h.get(k, {}).get(f) if self._alive(k) else None

    def hset(self, k, f=None, v=None, mapping=None):
        self._alive(k)
        d = self.h.setdefault(k, {})
        if mapping:
            d.update(mapping)
        if f is not None:
            d[f] = v

    def expire(self, k, sec):
        if self._alive(k):
            self.exp[k] = time.monotonic() + sec

    def ttl(self, k):
        if not self._alive(k):
            return -2
        return int(self.exp[k] - time.monotonic()) if k in self.exp else -1

    def exists(self, k):
        return 1 if self._alive(k) else 0

    def delete(self, k):
        self.h.pop(k, None)
        self.exp.pop(k, None)

    def scan_iter(self, pattern):
        prefix = pattern.rstrip("*")
        return [k for k in list(self.h) if k.startswith(prefix)]

    def info(self, _section):
        return {"used_memory": self.used_memory}

    def pipeline(self, transaction=True):
        fake = self

        class _Pipe:
            def __init__(self):
                self.ops = []

            def hset(self, *a, **k):
                self.ops.append(lambda: fake.hset(*a, **k))

            def expire(self, *a):
                self.ops.append(lambda: fake.expire(*a))

            def execute(self):
                for op in self.ops:
                    op()
        return _Pipe()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(scraper, "CACHE_ENABLED", True)
    monkeypatch.setattr(scraper, "CACHE_DIR", tmp_path / "sell")
    monkeypatch.setattr(scraper, "BUYBACK_CACHE_DIR", tmp_path / "buy")
    monkeypatch.setattr(scraper, "SHOP_CACHE_TTL_MINUTES", {})
    # テストごとに別の名前空間（本物のサーバーに残ったキーと混ざらない）
    monkeypatch.setattr(shop_cache_kv, "KEY_PREFIX", f"test:shopcache:{time.time_ns()}")


@pytest.fixture(params=["fake", "real"])
def kv(request, monkeypatch):
    """Key Value を有効にし、直接覗くためのクライアントを返す"""
    if request.param == "fake":
        fake = _FakeRedis()
        monkeypatch.setenv("SHOP_CACHE_REDIS_URL", "redis://fake/0")
        monkeypatch.setattr(shop_cache_kv, "_new_client", lambda url: fake)
        shop_cache_kv._reset_for_tests()
        yield fake
        return
    if not REAL_URL:
        pytest.skip("SHOP_CACHE_TEST_REDIS_URL 未設定")
    monkeypatch.setenv("SHOP_CACHE_REDIS_URL", REAL_URL)
    shop_cache_kv._reset_for_tests()
    r = shop_cache_kv._get_client()
    yield r
    for k in r.scan_iter(f"{shop_cache_kv.KEY_PREFIX}:*"):
        r.delete(k)


def _files(tmp_path):
    return list(tmp_path.rglob("*.json"))


def _age_kv_entry(r, kind, key, shop, minutes):
    """Key Value に置いたエントリの timestamp を minutes 分前にずらす"""
    k = shop_cache_kv._key(kind, key)
    ent = shop_cache_kv._decode(r.hget(k, shop))
    ent["timestamp"] = (datetime.now() - timedelta(minutes=minutes)).isoformat()
    r.hset(k, shop, shop_cache_kv._encode(ent))


def _go_unreachable(monkeypatch):
    monkeypatch.setenv("SHOP_CACHE_REDIS_URL", UNREACHABLE_URL)
    monkeypatch.setattr(shop_cache_kv, "_new_client", _REAL_NEW_CLIENT)
    shop_cache_kv._reset_for_tests()


# ── Key Value の経路（偽物と本物の両方）──

def test_store_and_hit_in_kv_not_file(kv, tmp_path):
    scraper.cache_store_shops("カードA", {"遊々亭": [_row("遊々亭")], "カーナベル": []})
    hit, missing = scraper.cache_get_shops("カードA", ["遊々亭", "カーナベル", "トレコロCB"])
    assert hit == {"遊々亭": [_row("遊々亭")], "カーナベル": []}
    assert missing == ["トレコロCB"]
    assert _files(tmp_path) == []
    ttl = kv.ttl(shop_cache_kv._key("sell", scraper._cache_key("カードA")))
    assert 0 < ttl <= scraper.STALE_CACHE_MAX_MINUTES * 60


def test_partial_mark_kept(kv):
    scraper.cache_store_shops("カードA", {"トレコロCB": [_row("トレコロCB")]},
                              partial_shops={"トレコロCB"})
    assert scraper.cache_get_shops("カードA", ["トレコロCB"])[1] == ["トレコロCB"]
    hit, missing = scraper.cache_get_shops("カードA", ["トレコロCB"], include_partial=True)
    assert "トレコロCB" in hit and missing == []


def test_sell_and_buy_are_separate(kv):
    scraper.cache_store_shops("カードA", {"遊々亭": [_row("遊々亭", 100)]})
    scraper.buyback_cache_store_shops("カードA", {"遊々亭": [_row("遊々亭", 50)]})
    assert scraper.cache_get_shops("カードA", ["遊々亭"])[0]["遊々亭"][0]["price"] == 100
    assert scraper.buyback_cache_get_shops("カードA", ["遊々亭"])[0]["遊々亭"][0]["price"] == 50
    assert len(scraper.cache_get("カードA")) == 1
    assert len(scraper.buyback_cache_get("カードA")) == 1


def test_per_shop_ttl_and_stale(kv, monkeypatch):
    scraper.cache_store_shops("カードA", {"遊々亭": [_row("遊々亭")], "カーナベル": [_row("カーナベル")]})
    scraper.buyback_cache_store_shops("カードA", {"遊々亭": [_row("遊々亭", 50)]})
    key = scraper._cache_key("カードA")
    _age_kv_entry(kv, "sell", key, "遊々亭", 20)
    _age_kv_entry(kv, "sell", key, "カーナベル", 20)
    _age_kv_entry(kv, "buy", scraper._buyback_cache_key("カードA"), "遊々亭", 20)
    # 既定15分: どちらも期限切れ
    assert scraper.cache_get_shops("カードA", ["遊々亭", "カーナベル"])[1] == ["遊々亭", "カーナベル"]
    # カーナベルだけ期限を30分に
    monkeypatch.setattr(scraper, "SHOP_CACHE_TTL_MINUTES", {"カーナベル": 30})
    hit, missing = scraper.cache_get_shops("カードA", ["遊々亭", "カーナベル"])
    assert list(hit) == ["カーナベル"] and missing == ["遊々亭"]
    # 期限切れでも前回の結果としては読める（販売・買取とも）
    assert scraper.cache_get_shops_stale("カードA", ["遊々亭"])["遊々亭"]["results"] == [_row("遊々亭")]
    assert scraper.buyback_cache_get_shops_stale("カードA", ["遊々亭"])["遊々亭"]["results"][0]["price"] == 50
    # 前回の期間（24時間）を過ぎたら読まない
    _age_kv_entry(kv, "sell", key, "遊々亭", scraper.STALE_CACHE_MAX_MINUTES + 5)
    assert scraper.cache_get_shops_stale("カードA", ["遊々亭"]) == {}


def test_kv_ttl_expires_key(kv, monkeypatch):
    """Key Value の期限（キーの TTL）が切れたらキーごと消える"""
    monkeypatch.setattr(scraper, "STALE_CACHE_MAX_MINUTES", 1 / 60)  # 1秒
    scraper.cache_store_shops("カードA", {"遊々亭": [_row("遊々亭")]})
    k = shop_cache_kv._key("sell", scraper._cache_key("カードA"))
    assert kv.exists(k)
    time.sleep(2.2)
    assert not kv.exists(k)
    assert scraper.cache_get_shops("カードA", ["遊々亭"])[1] == ["遊々亭"]


def test_kanabell_exclusion_applies_to_kv(kv):
    """他TCGの品はキャッシュから読むときにも除く（保存時の判定が古くても）"""
    ocg = _row("カーナベル", name="ブラック・マジシャン")
    rush = _row("カーナベル", name="ブラック・マジシャン（ラッシュデュエル）")
    assert not scraper._is_kanabell_excluded(ocg) and scraper._is_kanabell_excluded(rush)
    scraper.cache_store_shops("カードA", {"カーナベル": [ocg, rush]})
    hit, _ = scraper.cache_get_shops("カードA", ["カーナベル"])
    assert hit["カーナベル"] == [ocg]


def test_cache_disabled_does_not_touch_kv(kv, monkeypatch):
    monkeypatch.setattr(scraper, "CACHE_ENABLED", False)
    scraper.cache_store_shops("カードA", {"遊々亭": [_row("遊々亭")]})
    assert not kv.exists(shop_cache_kv._key("sell", scraper._cache_key("カードA")))
    assert scraper.cache_get_shops("カードA", ["遊々亭"]) == ({}, ["遊々亭"])
    assert scraper.cache_get("カードA") is None


def test_over_limit_falls_back_to_file(kv, tmp_path, monkeypatch):
    monkeypatch.setenv("SHOP_CACHE_KV_MAX_BYTES", "1")
    scraper.cache_store_shops("カードA", {"遊々亭": [_row("遊々亭")]})
    assert len(_files(tmp_path)) == 1
    assert not kv.exists(shop_cache_kv._key("sell", scraper._cache_key("カードA")))
    assert "遊々亭" in scraper.cache_get_shops("カードA", ["遊々亭"])[0]
    assert shop_cache_kv._down_until == 0.0  # 上限超えは停止ではない


def test_newer_file_value_wins_over_older_kv_value(kv, tmp_path, monkeypatch):
    """Key Value に古い値・ファイルに新しい値（不調中・上限超えの間に書いた分）なら、新しい方を出す"""
    scraper.cache_store_shops("カードA", {"遊々亭": [_row("遊々亭", 100)]})
    _age_kv_entry(kv, "sell", scraper._cache_key("カードA"), "遊々亭", 60)
    monkeypatch.setenv("SHOP_CACHE_KV_MAX_BYTES", "1")  # 上限超え → ファイルへ
    shop_cache_kv._mem_checked_at = 0.0
    scraper.cache_store_shops("カードA", {"遊々亭": [_row("遊々亭", 300)]})
    assert len(_files(tmp_path)) == 1
    assert scraper.cache_get_shops_stale("カードA", ["遊々亭"])["遊々亭"]["results"][0]["price"] == 300
    assert scraper.cache_get_shops("カードA", ["遊々亭"])[0]["遊々亭"][0]["price"] == 300
    assert scraper.cache_get("カードA")[0]["price"] == 300


def test_file_written_during_outage_is_read_after_recovery(kv, tmp_path, monkeypatch):
    """Key Value の不調中にファイルへ書いた分も、復旧後に読める"""
    url = os.environ["SHOP_CACHE_REDIS_URL"]
    new_client = shop_cache_kv._new_client
    _go_unreachable(monkeypatch)
    scraper.cache_store_shops("カードA", {"遊々亭": [_row("遊々亭")]})
    assert len(_files(tmp_path)) == 1
    monkeypatch.setenv("SHOP_CACHE_REDIS_URL", url)
    monkeypatch.setattr(shop_cache_kv, "_new_client", new_client)
    shop_cache_kv._reset_for_tests()
    scraper.cache_store_shops("カードA", {"カーナベル": [_row("カーナベル")]})
    hit, missing = scraper.cache_get_shops("カードA", ["遊々亭", "カーナベル"])
    assert set(hit) == {"遊々亭", "カーナベル"} and missing == []


def test_concurrent_threads_use_kv_without_marking_down(kv, tmp_path):
    """16スレッドが同時に読み書きしても、停止扱いにならず全件読める（接続は上限まで）"""
    errors = []

    def work(i):
        try:
            for j in range(10):
                scraper.cache_store_shops(f"カード{i}-{j}", {"遊々亭": [_row("遊々亭", i * 100 + j)]})
                hit, _ = scraper.cache_get_shops(f"カード{i}-{j}", ["遊々亭"])
                assert hit["遊々亭"][0]["price"] == i * 100 + j
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=work, args=(i,)) for i in range(16)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
    assert shop_cache_kv._down_until == 0.0


# ── 失敗の種類と停止期間（サーバー不要）──

def test_unset_url_uses_file(tmp_path):
    assert not shop_cache_kv.enabled()
    scraper.cache_store_shops("カードA", {"遊々亭": [_row("遊々亭")]})
    assert len(_files(tmp_path)) == 1
    assert "遊々亭" in scraper.cache_get_shops("カードA", ["遊々亭"])[0]


def test_unreachable_falls_back_to_file_and_cools_down(tmp_path, monkeypatch, caplog):
    _go_unreachable(monkeypatch)
    t0 = time.monotonic()
    scraper.cache_store_shops("カードA", {"遊々亭": [_row("遊々亭")]})
    first = time.monotonic() - t0
    assert len(_files(tmp_path)) == 1
    assert any("ファイルに戻します" in r.message for r in caplog.records)
    assert first < shop_cache_kv.SOCKET_CONNECT_TIMEOUT_SEC + shop_cache_kv.SOCKET_TIMEOUT_SEC + 1
    # 停止中は接続を試さないので、読み書きがすぐ返る
    calls = []
    monkeypatch.setattr(shop_cache_kv, "_mark_down", lambda *a: calls.append(a))
    t0 = time.monotonic()
    for _ in range(20):
        hit, missing = scraper.cache_get_shops("カードA", ["遊々亭", "カーナベル"])
        scraper.cache_store_shops("カードA", {"カーナベル": []})
    assert time.monotonic() - t0 < 1.0
    assert calls == []
    assert set(hit) == {"遊々亭", "カーナベル"}


def test_only_one_thread_probes_after_cooldown(monkeypatch):
    _go_unreachable(monkeypatch)
    assert shop_cache_kv.load("sell", "k", ["店"]) is None
    assert shop_cache_kv._get_client() is None  # 停止中
    monkeypatch.setattr(shop_cache_kv, "_down_until", time.monotonic() - 1)
    assert shop_cache_kv._get_client() is not None  # 期間が明けたら1スレッドだけ試す
    assert shop_cache_kv._get_client() is None      # 試し役の結果が出るまでほかは停止中のまま
    shop_cache_kv._mark_up()
    assert shop_cache_kv._get_client() is not None


class _Raising:
    def __init__(self, err):
        self.err = err

    def hmget(self, *a):
        raise self.err

    def hgetall(self, *a):
        raise self.err

    def info(self, *a):
        raise self.err

    def pipeline(self, transaction=True):
        raise self.err


@pytest.mark.parametrize("err, marks_down", [
    (redis.ConnectionError("No connection available."), False),  # 接続の空き待ち＝混雑
    (redis.ResponseError("WRONGTYPE Operation against a key"), False),
    (redis.ConnectionError("Error 111 connecting"), True),
    (redis.TimeoutError("Timeout reading from socket"), True),
])
def test_error_kinds(monkeypatch, err, marks_down):
    monkeypatch.setenv("SHOP_CACHE_REDIS_URL", "redis://fake/0")
    monkeypatch.setattr(shop_cache_kv, "_new_client", lambda url: _Raising(err))
    shop_cache_kv._reset_for_tests()
    assert shop_cache_kv.load("sell", "k", ["店"]) is None
    assert (shop_cache_kv._down_until > 0) is marks_down


def test_info_failure_means_no_write_but_reads_continue(monkeypatch):
    """INFO が使えないときは使用量を確かめられないので書かない。停止扱いにはしない"""
    class _NoInfo(_FakeRedis):
        def info(self, _):
            raise redis.ResponseError("unknown command 'INFO'")
    fake = _NoInfo()
    monkeypatch.setenv("SHOP_CACHE_REDIS_URL", "redis://fake/0")
    monkeypatch.setattr(shop_cache_kv, "_new_client", lambda url: fake)
    shop_cache_kv._reset_for_tests()
    assert shop_cache_kv.store("sell", "k", {"店": {"timestamp": "x"}}, 60) is False
    assert shop_cache_kv._down_until == 0.0
    assert shop_cache_kv.load("sell", "k", ["店"]) == {}


def test_broken_value_is_skipped(monkeypatch):
    class _R:
        def hmget(self, k, shops):
            return [b"not-zlib", shop_cache_kv._encode({"timestamp": datetime.now().isoformat(),
                                                        "partial": False, "results": []})]
    monkeypatch.setattr(shop_cache_kv, "_get_client", lambda: _R())
    assert list(shop_cache_kv.load("sell", "k", ["壊れ", "正常"])) == ["正常"]


def test_oom_error_falls_back_without_cooldown(monkeypatch):
    """満杯（noeviction の OOM）は停止扱いにせず、読みは Key Value を使い続ける"""
    class _Pipe:
        def hset(self, *a, **k): pass
        def expire(self, *a): pass
        def execute(self):
            raise redis.ResponseError("OOM command not allowed when used memory > 'maxmemory'.")

    class _R(_FakeRedis):
        def pipeline(self, transaction=True):
            return _Pipe()
    monkeypatch.setattr(shop_cache_kv, "_get_client", lambda: _R())
    assert shop_cache_kv.store("sell", "k", {"店": {"timestamp": "x"}}, 60) is False
    assert shop_cache_kv._down_until == 0.0
    assert shop_cache_kv._mem_over is True


def test_stats_log_shows_key_counts_per_db(kv, caplog):
    """使用量と DB ごとのキーの数を、最初の確かめでログに出す（中身は出さない）"""
    import logging
    if isinstance(kv, _FakeRedis):
        kv.info = lambda section: ({"used_memory": 1_000_000} if section == "memory"
                                   else {"db0": {"keys": 7, "expires": 7}})
    with caplog.at_level(logging.INFO, logger="shop_cache_kv"):
        scraper.cache_store_shops("カードA", {"遊々亭": [_row("遊々亭")]})
    lines = [r.message for r in caplog.records if "キーの数" in r.message]
    assert len(lines) == 1 and "使用量" in lines[0]
    if isinstance(kv, _FakeRedis):
        assert "db0=7" in lines[0]
