"""
tests/test_safe_get.py — safe_get の再試行方針とサーキットブレーカの回帰テスト

背景:
  カードラボ（www.c-labo-online.jp）の夜間収集が、実行の途中から 403 Forbidden を
  返され続ける事象を2026-08-03に調査した。Renderログの実測では
  「最初の約1,000〜1,250リクエストは200 → 以降は実行終了まで100%失敗 → 翌晩リセット」
  という流量ベースの遮断で、恒久的なIPブロックではなかった（詳細は docs/decisions.md）。

  当時の safe_get は 403 でも再試行していたため、遮断後の1,451件が実質約2,900
  リクエストになり、店舗側への負荷を倍にしていた。ここでは対処の3点を固定する:
    ① 403（および429以外の4xx）は再試行しない
    ② 5xx・429・接続エラー・タイムアウトは従来どおり再試行する（トレコロのみ2026-10-14から
       タイムアウト・接続エラーを再試行しない。⑤参照）
    ③ 連続403が閾値に達したホストは一定時間スキップし、叩き続けない
       （app.py は常駐プロセスのため、恒久ブロックではなくTTLで自動復帰させる）

テスト方針:
  - ネットワークには出ない。scraper が使う Session を差し替えて検証する。
"""

import sys
import time
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scraper


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "<html><body>ok</body></html>", url: str = ""):
        self.status_code = status_code
        self.text = text
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"{self.status_code} Client Error")
            err.response = self
            raise err


class _FakeSession:
    """呼び出し回数とURLを記録するだけのSession代替"""

    def __init__(self, statuses):
        # statuses: 返すステータスコードのリスト（尽きたら最後の値を繰り返す）
        self._statuses = list(statuses)
        self.calls = []
        self.call_headers = []  # get() に渡されたヘッダ上書き
        self.final_url = None   # リダイレクト後の最終URL（None なら要求URLのまま）
        self.headers = {}

    def get(self, url, timeout=None, headers=None):
        self.calls.append(url)
        self.call_headers.append(headers)
        status = self._statuses[min(len(self.calls) - 1, len(self._statuses) - 1)]
        if status == 0:
            raise requests.ConnectionError("boom")
        if status == -1:
            raise requests.ReadTimeout("timed out")
        return _FakeResponse(status, url=self.final_url or url)


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """モジュールレベルの状態（セッション・ホストブロック）をテストごとに初期化する"""
    scraper._host_block.clear()
    scraper.reset_host_request_counts()
    scraper._session = None
    scraper._reset_fetch_errors()
    # 再試行時の sleep(2) でテストを待たせない
    monkeypatch.setattr(scraper.time, "sleep", lambda *_: None)
    yield
    scraper._host_block.clear()
    scraper._session = None


def _install(monkeypatch, statuses) -> _FakeSession:
    fake = _FakeSession(statuses)
    monkeypatch.setattr(scraper, "_get_session", lambda: fake)
    return fake


URL = "https://www.c-labo-online.jp/product-list?keyword=test"


# ── ① 403は再試行しない ──

def test_403_does_not_retry(monkeypatch):
    fake = _install(monkeypatch, [403])
    assert scraper.safe_get(URL) is None
    assert len(fake.calls) == 1, "403で再試行してはいけない（店舗への負荷が2倍になる）"


def test_404_does_not_retry(monkeypatch):
    fake = _install(monkeypatch, [404])
    assert scraper.safe_get(URL) is None
    assert len(fake.calls) == 1


# ── ② 一時的な失敗は従来どおり再試行する ──

def test_429_retries(monkeypatch):
    fake = _install(monkeypatch, [429])
    assert scraper.safe_get(URL) is None
    assert len(fake.calls) == 2, "429は一時的な過負荷なので再試行する"


def test_500_retries(monkeypatch):
    fake = _install(monkeypatch, [500])
    assert scraper.safe_get(URL) is None
    assert len(fake.calls) == 2


def test_connection_error_retries(monkeypatch):
    fake = _install(monkeypatch, [0])
    assert scraper.safe_get(URL) is None
    assert len(fake.calls) == 2


def test_timeout_retries_on_other_hosts(monkeypatch):
    fake = _install(monkeypatch, [-1])
    assert scraper.safe_get(URL) is None
    assert len(fake.calls) == 2, "トレコロ以外は従来どおりタイムアウトでも再試行する"


def test_timeout_does_not_count_toward_block_on_other_hosts(monkeypatch):
    _install(monkeypatch, [-1])
    for _ in range(scraper._HOST_BLOCK_THRESHOLD + 2):
        scraper.safe_get(URL)
    assert not scraper._is_host_blocked(scraper._host_of(URL)), "他ホストのブレーカは403のみ"


def test_torecolo_timeout_does_not_retry(monkeypatch):
    fake = _install(monkeypatch, [-1])
    assert scraper.safe_get(TORECOLO_URL) is None
    assert len(fake.calls) == 1, "トレコロはタイムアウトを再試行しない"
    assert scraper._get_fetch_errors() == 1, "取得失敗として数える"


def test_torecolo_connection_error_still_retries_and_not_counted(monkeypatch):
    """keep-alive切れ等の接続エラーは従来どおり再試行し、ブレーカの連続カウントに入れない"""
    fake = _install(monkeypatch, [0])
    assert scraper.safe_get(TORECOLO_URL) is None
    assert len(fake.calls) == 2
    for _ in range(scraper._HOST_BLOCK_THRESHOLD + 2):
        scraper.safe_get(TORECOLO_URL)
    assert not scraper._is_host_blocked("www.torecolo.jp")


def test_retry_succeeds_on_second_attempt(monkeypatch):
    fake = _install(monkeypatch, [500, 200])
    assert scraper.safe_get(URL) is not None
    assert len(fake.calls) == 2


# ── ③ サーキットブレーカ ──

def test_host_blocked_after_consecutive_403(monkeypatch):
    fake = _install(monkeypatch, [403])
    for _ in range(scraper._HOST_BLOCK_THRESHOLD):
        scraper.safe_get(URL)
    assert len(fake.calls) == scraper._HOST_BLOCK_THRESHOLD

    # 閾値到達後はネットワークに出ない
    assert scraper.safe_get(URL) is None
    assert scraper.safe_get(URL) is None
    assert len(fake.calls) == scraper._HOST_BLOCK_THRESHOLD, "打ち切り後はリクエストしない"


def test_blocked_host_still_counts_as_fetch_error(monkeypatch):
    """打ち切り中の空振りは「0件（在庫なし）」ではなく取得失敗として数える"""
    _install(monkeypatch, [403])
    for _ in range(scraper._HOST_BLOCK_THRESHOLD):
        scraper.safe_get(URL)
    scraper._reset_fetch_errors()
    scraper.safe_get(URL)
    assert scraper._get_fetch_errors() == 1


def test_other_host_is_not_blocked(monkeypatch):
    fake = _install(monkeypatch, [403])
    for _ in range(scraper._HOST_BLOCK_THRESHOLD):
        scraper.safe_get(URL)
    other = "https://www.c-labo-kaitori.jp/product-list?keyword=test"
    scraper.safe_get(other)
    assert other in fake.calls, "ブロックはホスト単位。別ホストは巻き添えにしない"


def test_success_resets_streak(monkeypatch):
    fake = _install(monkeypatch, [403, 403, 200] + [403] * 10)
    scraper.safe_get(URL)          # 403
    scraper.safe_get(URL)          # 403
    assert scraper.safe_get(URL) is not None  # 200 で連続カウントがリセット
    calls_before = len(fake.calls)
    for _ in range(scraper._HOST_BLOCK_THRESHOLD - 1):
        scraper.safe_get(URL)
    # リセットされていれば、この時点ではまだ閾値に達していない
    assert len(fake.calls) == calls_before + (scraper._HOST_BLOCK_THRESHOLD - 1)


def test_block_expires_after_ttl(monkeypatch):
    """常駐プロセス（app.py のライブ検索）が再起動なしで復帰できること"""
    fake = _install(monkeypatch, [403])
    for _ in range(scraper._HOST_BLOCK_THRESHOLD):
        scraper.safe_get(URL)
    blocked_calls = len(fake.calls)

    real_time = time.time
    monkeypatch.setattr(
        scraper.time, "time", lambda: real_time() + scraper._HOST_BLOCK_TTL_SEC + 1
    )
    scraper.safe_get(URL)
    assert len(fake.calls) == blocked_calls + 1, "TTL経過後は再試行できること"


# ── セッションの使い回し ──

def test_session_is_reused():
    scraper._session = None
    first = scraper._get_session()
    second = scraper._get_session()
    assert first is second, "毎回新しいセッションを作ると店舗側にセッションが大量生成される"
    assert first.headers.get("User-Agent") == scraper.HEADERS["User-Agent"]


# ── ④ トレコロ宛てだけUAを名乗り直す／WAF拒否ページへのリダイレクトは失敗扱い ──
# 2026-10-14: WAFが Chrome/120 等のUAを遮断し error403.html へ200で飛ばす事象への対処

TORECOLO_URL = "https://www.torecolo.jp/shop/goods/search.aspx?search=x&keyword=test"


def test_torecolo_gets_own_user_agent_only(monkeypatch):
    fake = _install(monkeypatch, [200])
    assert scraper.safe_get(TORECOLO_URL) is not None
    assert fake.call_headers[-1] == {
        "User-Agent": "TCGYM/1.0 (+https://tcg-price-compare.onrender.com)"
    }
    # 他ホストは上書きなし（共有Sessionの既存UAのまま）
    assert scraper.safe_get(URL) is not None
    assert fake.call_headers[-1] is None


# 2026-10-23: カードラッシュ販売も Chrome 140 未満の UA を 403 にし始めた。買取（cardrush.media）は対象外
def test_cardrush_sale_gets_own_user_agent(monkeypatch):
    fake = _install(monkeypatch, [200, 200])
    assert scraper.safe_get("https://www.cardrush.jp/product-list?keyword=test") is not None
    assert fake.call_headers[-1] == {
        "User-Agent": "TCGYM/1.0 (+https://tcg-price-compare.onrender.com)"
    }
    assert scraper.safe_get("https://cardrush.media/yugioh/buying_prices?name=test") is not None
    assert fake.call_headers[-1] is None


def test_waf_error403_redirect_is_failure(monkeypatch):
    fake = _install(monkeypatch, [200])
    fake.final_url = "https://www.torecolo.jp/CustomError/error403.html"
    assert scraper.safe_get(TORECOLO_URL) is None
    assert len(fake.calls) == 1, "WAF拒否は再試行しない"
    assert scraper._get_fetch_errors() == 1, "取得失敗として数える（キャッシュ・履歴に0件で載せない）"
    assert scraper._host_block["www.torecolo.jp"]["streak"] == 1, "403として記録される"


# ── ⑤ 連続タイムアウトでもホストを一時停止する／アクセス回数の記録 ──

def test_host_blocked_after_consecutive_timeouts(monkeypatch):
    fake = _install(monkeypatch, [-1])
    for _ in range(scraper._HOST_BLOCK_THRESHOLD):
        scraper.safe_get(TORECOLO_URL)
    assert len(fake.calls) == scraper._HOST_BLOCK_THRESHOLD  # 再試行なしなので1検索1リクエスト

    scraper._reset_fetch_errors()
    assert scraper.safe_get(TORECOLO_URL) is None
    assert len(fake.calls) == scraper._HOST_BLOCK_THRESHOLD, "閾値到達後はネットワークに出ない"
    assert scraper._get_fetch_errors() == 1, "スキップ中も取得失敗として数える（在庫なし扱いにしない）"


def test_timeout_streak_reset_by_success(monkeypatch):
    fake = _install(monkeypatch, [-1, -1, 200] + [-1] * 10)
    scraper.safe_get(TORECOLO_URL)
    scraper.safe_get(TORECOLO_URL)
    assert scraper.safe_get(TORECOLO_URL) is not None
    before = len(fake.calls)
    for _ in range(scraper._HOST_BLOCK_THRESHOLD - 1):
        scraper.safe_get(TORECOLO_URL)
    assert len(fake.calls) == before + scraper._HOST_BLOCK_THRESHOLD - 1


def test_500_does_not_count_toward_block(monkeypatch):
    """5xx はブレーカの連続カウントに入れない（従来どおり）"""
    fake = _install(monkeypatch, [500])
    for _ in range(scraper._HOST_BLOCK_THRESHOLD):
        scraper.safe_get(URL)
    assert not scraper._is_host_blocked(scraper._host_of(URL))


def test_host_request_counts(monkeypatch):
    """再試行は1回ずつ数え、ブレーカでのスキップは別カウント"""
    _install(monkeypatch, [500])  # 5xx は再試行あり＝1検索で2リクエスト
    scraper.safe_get(TORECOLO_URL)
    assert scraper.get_host_request_counts()["www.torecolo.jp"] == {"requests": 2, "skipped": 0}

    scraper.reset_host_request_counts()
    _install(monkeypatch, [-1])
    for _ in range(scraper._HOST_BLOCK_THRESHOLD + 2):
        scraper.safe_get(TORECOLO_URL)
    c = scraper.get_host_request_counts()["www.torecolo.jp"]
    assert c == {"requests": scraper._HOST_BLOCK_THRESHOLD, "skipped": 2}
