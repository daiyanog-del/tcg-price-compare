"""サイトの公開URL（独自ドメイン移行・2026-10-21）。

設計文書: docs/design-domain-migration-2026-10-21.md

- CANONICAL_BASE_URL（例: https://greedy-lab.com）が設定されていれば、それを正のURLとする。
  未設定なら旧URL（onrender）のまま。切り替え日に Render の web と定期実行の両方へ設定する
- LEGACY_HOSTS は旧URLのホスト名（カンマ区切り・ポート込み）。ここで開かれたページは
  引き継ぎページを経由して正のURLへ移す（domain_handoff.py）

Flask に依存しないので、定期実行（x_poster.py 等）からも読む。
"""

import os
from urllib.parse import urlparse

LEGACY_BASE_URL = "https://tcg-price-compare.onrender.com"


def canonical_base_url_env() -> str:
    """環境変数で指定された正のURL（末尾スラッシュなし）。未設定なら空文字。"""
    return os.environ.get("CANONICAL_BASE_URL", "").strip().rstrip("/")


def site_url() -> str:
    """X投稿・通知などに載せるサイトのURL。正のURLが未設定なら旧URL。"""
    return canonical_base_url_env() or LEGACY_BASE_URL


def canonical_host() -> str:
    """正のURLのホスト（ポート込み）。未設定なら空文字。"""
    base = canonical_base_url_env()
    return urlparse(base).netloc if base else ""


def legacy_hosts() -> set:
    """旧URLのホスト（ポート込み）の集合。"""
    raw = os.environ.get("LEGACY_HOSTS", urlparse(LEGACY_BASE_URL).netloc)
    return {h.strip().lower() for h in raw.split(",") if h.strip()}
