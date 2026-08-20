"""
クライアントの実IPアドレス解決ヘルパー。

Render(Cloudflare経由)前提。Renderのエッジ(request.remote_addr)は
Cloudflareの折り返しIPを見てしまい、実クライアントIPと異なるため
レートリミット等のIP単位制御をすり抜けられる（本番実測で確認済み）。

優先順位:
1. CF-Connecting-IP — 公開URL（Render経由＝Cloudflare配下）からのアクセスでは
   Cloudflareがエッジで上書き付与する想定のヘッダ。ただしオリジンへの直叩き経路が
   本当に存在しない（Cloudflare以外からRenderに到達できない）かは未検証のため、
   「偽装不可」と断定はしない。公開URL経由の実測で継続検証すること。
2. True-Client-IP — 同上の位置づけ（一部CDN/プロキシが付与）。
3. request.remote_addr — 上記が無い場合の最終フォールバック。

X-Forwarded-For は採用しない: クライアントがリクエストごとに自由な値を送り込め、
IP単位のレート制限バケットを丸ごと無効化できることを実測で確認したため
（1リクエストごとに異なる偽装IPを名乗ればバケットが分散し、実質無制限になる）。

ヘッダ値は ipaddress.ip_address() で形式検証し、不正な値（IPアドレスとして
パースできない文字列）は次の候補にフォールバックする。最終的な戻り値は
念のため45文字（IPv6の最大表記長）で切り詰める。
"""
import ipaddress

from flask import request

_MAX_IP_LEN = 45  # IPv6の最大文字数（例: 0000:0000:0000:0000:0000:ffff:255.255.255.255）


def _valid_ip_or_none(raw: str) -> str | None:
    """文字列がIPアドレスとしてパース可能なら正規化前の文字列をそのまま返す。不正ならNone。"""
    candidate = raw.strip()
    if not candidate:
        return None
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate[:_MAX_IP_LEN]


def _client_ip() -> str:
    """実クライアントIPを解決する（優先順位はモジュールdocstring参照）。"""
    cf_ip = _valid_ip_or_none(request.headers.get("CF-Connecting-IP", ""))
    if cf_ip:
        return cf_ip

    true_client_ip = _valid_ip_or_none(request.headers.get("True-Client-IP", ""))
    if true_client_ip:
        return true_client_ip

    remote = _valid_ip_or_none(request.remote_addr or "")
    if remote:
        return remote

    return "unknown"
