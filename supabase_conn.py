"""
supabase_conn.py — Web の処理で複数のスレッドから共有する Supabase クライアントを作る。

supabase-py（postgrest）の既定の接続は HTTP/2 の1本で、1つのクライアントを gunicorn のスレッドと
共有の実行枠（featured_matrix._executor）から同時に使うと、接続ごと切られる
（RemoteProtocolError: ConnectionTerminated PROTOCOL_ERROR／Server disconnected・
RuntimeError: deque mutated during iteration）。切られると同じ接続で読んでいた要求がまとめて失敗する。
2026-11-01 本番 DB への同時読み込み試験（58枚×31日を4スレッド・5回×2）で、HTTP/2 は 620件中14件失敗、
HTTP/1.1 の接続プールは 620件中0件・1件の時間は 0.07→0.05秒。本番では購入候補の価格が30秒の締め切りに
かかって全カード「価格を読み込めませんでした」になっていた（docs/decisions.md 2026-11-01）。

そこで HTTP/1.1 の接続プール（要求ごとに空いている接続を使う）を渡す。httpx_client は postgrest・storage・
auth・functions が共用する。どれも要求ごとに完全な URL とヘッダーを付けて送るので、base_url・headers は持たせない。
"""

import httpx
from supabase import Client, create_client
from supabase.lib.client_options import SyncClientOptions

# 待ち時間は postgrest の既定（DEFAULT_POSTGREST_CLIENT_TIMEOUT = 120秒）と同じ。
# 渡したクライアントの設定がそのまま使われ、httpx の既定（5秒）では重い読み込みが途中で切れるため
_TIMEOUT_SEC = 120


def create_shared_client(url: str, key: str) -> Client:
    """複数スレッドから同時に使ってよい Supabase クライアント（HTTP/1.1 の接続プール）"""
    http = httpx.Client(http2=False, timeout=_TIMEOUT_SEC, follow_redirects=True)
    return create_client(url, key, options=SyncClientOptions(httpx_client=http))
