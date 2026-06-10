"""
fetch_guard.py — ホワイトリスト強制HTTP取得モジュール

外部HTTPリクエストはすべて fetch_whitelisted() を通すこと。
ホワイトリスト外のURLには WhitelistViolation を送出し、ネットワーク接続を行わない。

担保する制約:
  - ホスト: www.yu-gi-oh.jp / yu-gi-oh.jp のみ
  - https 強制（httpは WhitelistViolation）
  - 同時接続1 + ページ間最低5秒のレート制限
  - リダイレクト先URLも同じ検証を再帰通過（最大3ホップ）

HTTPクライアントについて:
  yu-gi-oh.jp（XSERVER上）のWAFは Python requests の TLS フィンガープリントを識別して
  403 を返す（ヘッダを揃えても不可、curl は通る）。そのため curl_cffi で Chrome の
  TLS/HTTP2 署名を模倣して取得する（impersonate="chrome"）。
  ローカル環境にのみ curl_cffi が必要（Render本番は import しない）。
"""

import threading
import time
from urllib.parse import urlparse

# curl_cffi は requirements.txt に含めるが、万一の取得失敗・バージョン差でも
# アプリ起動（is_whitelisted 等の検証ロジック）が止まらないよう遅延評価する。
# 実取得（fetch_whitelisted）が呼ばれた時点で未導入なら明確なエラーを出す。
try:
    from curl_cffi import requests as _cffi_requests
except ImportError:  # pragma: no cover
    _cffi_requests = None

# ──────────────────────────────────────────────
# ホワイトリスト定義
# ──────────────────────────────────────────────

# 抽出対象は YU-GI-OH.jp のみ（ユーザー方針 2026-06-11）。
# 注意: yu-gi-oh.jp はクラウド事業者のIP（GitHub Actions / Render）を403で拒否するため、
# Watcherはローカル環境（一般回線）から定期実行する。
ALLOWED_HOSTS: frozenset = frozenset({
    "www.yu-gi-oh.jp",
    "yu-gi-oh.jp",
})

# ホストごとのパスプレフィックス制約（リストにないホストは全パス可）
ALLOWED_PATH_PREFIXES: dict[str, tuple] = {}

# curl_cffi で模倣するブラウザ署名（WAFのTLSフィンガープリント検査を通すため）
_IMPERSONATE = "chrome"

# レート制限パラメータ
_MIN_INTERVAL_SEC = 5.0   # ページ間の最低待機秒数


# ──────────────────────────────────────────────
# レート制限用グローバル状態（スレッドセーフ）
# ──────────────────────────────────────────────

_rate_lock = threading.Lock()          # 同時接続1を保証するロック
_last_request_time: float = 0.0        # 直前リクエスト完了時刻（time.monotonic）


# ──────────────────────────────────────────────
# 公開例外クラス
# ──────────────────────────────────────────────

class WhitelistViolation(Exception):
    """ホワイトリスト違反URL（未許可ホスト / http / 非許可パス）へのアクセス試行"""


# ──────────────────────────────────────────────
# 内部ヘルパー
# ──────────────────────────────────────────────

def _validate_url(url: str) -> None:
    """URLがホワイトリストを満たすか検証する。違反なら WhitelistViolation を送出。"""
    parsed = urlparse(url)

    # https 強制チェック
    if parsed.scheme != "https":
        raise WhitelistViolation(
            f"https以外のスキームは許可されていません: {parsed.scheme!r} ({url!r})"
        )

    host = parsed.netloc.lower()

    # ポート番号が含まれる場合は除去して比較
    if ":" in host:
        host = host.split(":")[0]

    # ホストのホワイトリストチェック
    if host not in ALLOWED_HOSTS:
        raise WhitelistViolation(
            f"ホワイトリスト外のホストへのアクセスは禁止されています: {host!r} ({url!r})"
        )

    # パスプレフィックスのチェック（ホストごとに追加制約がある場合）
    if host in ALLOWED_PATH_PREFIXES:
        path = parsed.path
        allowed_prefixes = ALLOWED_PATH_PREFIXES[host]
        if not any(path.startswith(prefix) for prefix in allowed_prefixes):
            raise WhitelistViolation(
                f"ホスト {host!r} では許可されていないパスです: {path!r} ({url!r})\n"
                f"  許可プレフィックス: {allowed_prefixes}"
            )


# ──────────────────────────────────────────────
# 公開関数
# ──────────────────────────────────────────────

def is_whitelisted(url: str) -> bool:
    """URLがホワイトリストを満たすかどうかを返す（例外を送出しない）。
    リンク抽出時のフィルタリングに使用する。
    """
    try:
        _validate_url(url)
        return True
    except WhitelistViolation:
        return False


def fetch_whitelisted(
    url: str,
    *,
    timeout: int = 30,
    _hop: int = 0,
):
    """ホワイトリストを検証してからHTTPリクエストを実行する。

    Args:
        url:     取得対象URL（https必須）
        timeout: リクエストタイムアウト秒数（デフォルト30秒）
        _hop:    内部用リダイレクトホップカウンタ（直接指定不要）

    Returns:
        curl_cffi.requests.Response オブジェクト
        （.status_code / .content / .text / .headers は requests 互換）

    Raises:
        WhitelistViolation: ホワイトリスト違反URL（ネットワーク接続前に発生）
    """
    global _last_request_time

    # リダイレクト上限チェック
    if _hop > 3:
        raise WhitelistViolation(
            f"リダイレクトが最大3ホップを超えました: {url!r}"
        )

    # ホワイトリスト検証（ここで違反なら即例外、ネットワークには出ない）
    _validate_url(url)

    if _cffi_requests is None:
        raise RuntimeError(
            "curl_cffi が未導入のため取得できません。`pip install curl_cffi` を実行してください。"
        )

    # レート制限つきでリクエスト実行
    with _rate_lock:
        # 前回リクエストからの経過時間を確認し、不足分だけ待機
        elapsed = time.monotonic() - _last_request_time
        if elapsed < _MIN_INTERVAL_SEC:
            time.sleep(_MIN_INTERVAL_SEC - elapsed)

        # impersonate でブラウザのTLS/HTTP2署名を模倣（WAF回避）。
        # allow_redirects=False でリダイレクト先を自前で再帰検証する。
        response = _cffi_requests.get(
            url,
            timeout=timeout,
            allow_redirects=False,
            impersonate=_IMPERSONATE,
        )
        _last_request_time = time.monotonic()

    # リダイレクトの場合は Location ヘッダを検証して再帰
    if response.status_code in (301, 302, 303, 307, 308):
        location = response.headers.get("Location", "")
        if not location:
            return response

        # 相対URLを絶対URLに変換
        if location.startswith("/"):
            parsed = urlparse(url)
            location = f"{parsed.scheme}://{parsed.netloc}{location}"

        # リダイレクト先もホワイトリスト検証を通す（違反なら WhitelistViolation）
        return fetch_whitelisted(location, timeout=timeout, _hop=_hop + 1)

    return response
