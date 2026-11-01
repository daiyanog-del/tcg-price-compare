"""
tests/test_supabase_conn.py — Web で共有する Supabase クライアント（supabase_conn.create_shared_client）

確かめること:
  - DB（postgrest）と画像の保存先（storage）が、HTTP/2 ではない同じ1つの httpx クライアントを使う
    （2026-11-01: 既定の HTTP/2 の1本を複数スレッドで共有して接続ごと切られ、購入候補の価格が全件失敗していた）
  - 待ち時間は postgrest の既定と同じ120秒（httpx の既定5秒にしない）
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from supabase_conn import create_shared_client


def test_shared_client_uses_http1_pool_for_db_and_storage():
    client = create_shared_client("https://example.supabase.co", "x" * 40)
    session = client.postgrest.session
    assert session._transport._pool._http2 is False
    assert session._transport._pool._http1 is True
    assert client.storage.session is session
    assert session.timeout.read == 120
