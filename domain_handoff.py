"""旧URL → 独自ドメインへの引き継ぎ（2026-10-21）。

設計文書: docs/design-domain-migration-2026-10-21.md

旧URLのホストで画面（HTML）が開かれたら、本来のページの代わりに引き継ぎページを返す。
引き継ぎページはブラウザに保存されたデータ（localStorage は origin ごとに分かれる）を
URL の # 以降に入れて正のURLの /handoff へ移る。/handoff が書き戻してから元のページへ進む。

API・静的ファイル・/sync・robots・sitemap は旧URLでもそのまま動かす
（更新前の拡張機能・旧URLに登録された service worker・途中の同期リンクのため）。
CANONICAL_BASE_URL が未設定のあいだは何もしない（切り替え前の本番の動きは変わらない）。
"""

from urllib.parse import quote

from flask import abort, g, render_template, request

import site_urls

# 引き継ぎの対象にする画面のパス（完全一致・前方一致）。ここに無いものは旧URLでもそのまま返す
_PAGE_EXACT = {"/", "/featured", "/new-cards", "/tcg-preview", "/solitaire", "/solitaire/replays", "/admin", "/share-target"}
_PAGE_PREFIXES = ("/card/", "/buy/")


def is_page_path(path: str) -> bool:
    return path in _PAGE_EXACT or path.startswith(_PAGE_PREFIXES)


def handoff_active() -> bool:
    return bool(site_urls.canonical_base_url_env())


def is_legacy_host(host: str) -> bool:
    host = (host or "").lower()
    return bool(host) and host != site_urls.canonical_host() and host in site_urls.legacy_hosts()


def _target_path() -> str:
    """移った先で開くパス（クエリ込み）。"""
    qs = request.query_string.decode("utf-8", "replace")
    return request.path + (f"?{qs}" if qs else "")


def _og_title(path: str) -> str:
    """旧URLの共有リンクのプレビュー用の見出し（X・LINE 等は旧URLの OGP を読みに来る）。"""
    if path.startswith("/card/") and len(path) > 6:
        return f"{path[6:]} の価格比較 | 強欲なラボ"
    if path.startswith("/buy/") and len(path) > 5:
        return f"{path[5:]} の買取価格比較 | 強欲なラボ"
    return "強欲なラボ — 遊戯王OCG プレイヤーサポート"


def maybe_handoff():
    """before_request から呼ぶ。引き継ぎページを返すときだけ応答を返す。"""
    if not handoff_active() or request.method not in ("GET", "HEAD"):
        return None
    if not is_legacy_host(request.host) or not is_page_path(request.path):
        return None
    g.no_store = True
    canonical = site_urls.canonical_base_url_env()
    return render_template(
        "handoff.html",
        mode="send",
        canonical_base=canonical,
        target_path=_target_path(),
        canonical_url=canonical + quote(request.path, safe="/"),
        og_title=_og_title(request.path),
    )


def receive_page():
    """正のURLの /handoff。旧URLから運ばれたデータを書き戻す。

    正のURLのホストで、切り替え後のときだけ返す。旧URLでも返すと、旧URLの中で
    /handoff へ移る細工リンク（referrer が旧URLになる）で、他人の同期の状態を
    書き込ませる経路ができる（2026-10-21 reviewer 指摘）。
    """
    if not handoff_active() or (request.host or "").lower() != site_urls.canonical_host():
        abort(404)
    g.no_store = True
    legacy_origins = sorted(
        f"{'http' if h.split(':')[0] in ('localhost', '127.0.0.1') else 'https'}://{h}"
        for h in site_urls.legacy_hosts()
    )
    return render_template("handoff.html", mode="receive", legacy_origins=legacy_origins)


def register(app):
    app.before_request(maybe_handoff)
    app.add_url_rule("/handoff", "domain_handoff_receive", receive_page)
