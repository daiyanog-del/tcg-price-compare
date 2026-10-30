"""
遊戯王カードWiki へのリンク — カード名から wiki のカードのページのURLを解決する
=============================================================================

カードのページ（/card/<名前>）の「Wiki」リンクは /card-wiki?name= を経由し、
ここで解決したURLへ302で送る。

wiki のカードのページ名は《カード名》だが、当サイトのカード名（公式DB由来）とは
英数字の全角・半角が違う（wiki は全角で書く: ＥＭスカイ・マジシャン・Ｄ・ゲイザー）。
2026-10-30 の実測（cardnames_ja.json から無作為に100件）では、英数字を全角にした
ページ名で90件が直接開け、外れはラッシュデュエルのカードや読みと表記の違い
（ブンボーグゼロゼロファイブ → 《ブンボーグ００５》）だった。
存在しないページは「指定されたページは存在しません」だけの画面になるため、
ページが無ければ wiki のタイトル検索の結果へ送る（外れた10件はタイトル検索で候補に出た）。

wiki は 2026 年に UTF-8 へ移行済み（EUC-JP でURLを作ると検索が文字化けする）。

Flask には依存しない（テスト容易性のため）。ルート配線は app.py 側。
"""

from collections import OrderedDict
from threading import Lock
from urllib.parse import quote

import requests

WIKI_BASE = "https://yugioh-wiki.net/index.php"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# card_page（/card/<name>）の上限と同じ値
MAX_CARD_NAME_LEN = 50

# 解決済みのURL（名前 → URL）。ページの有無は滅多に変わらないので起動中は持ち続ける
_CACHE_MAX = 4096
_cache = OrderedDict()
_cache_lock = Lock()


def to_wiki_title(name):
    """当サイトのカード名を wiki の表記に寄せる（半角英数字を全角に）。"""
    return "".join(
        chr(ord(c) + 0xFEE0) if c.isascii() and c.isalnum() else c
        for c in name
    )


def page_url(title):
    return f"{WIKI_BASE}?{quote('《' + title + '》')}"


def search_url(name):
    # scope=title: タイトル(曖昧)。本文まで探すと関連カードのページが大量に並ぶ
    return f"{WIKI_BASE}?cmd=search&scope=title&word={quote(name)}"


def _page_exists(url, get):
    """ページがあれば True、無ければ False。通信の失敗は例外のまま返す。"""
    res = get(url, headers=HEADERS, timeout=8, stream=True)
    try:
        if res.status_code == 404:
            return False
        res.raise_for_status()
        return True
    finally:
        res.close()


def resolve_wiki_url(name, get=requests.get):
    """カード名から wiki のURLを返す。ページが無ければタイトル検索のURL。

    通信に失敗したときもタイトル検索のURLを返す（そのときは覚えない）。
    """
    name = (name or "").strip()
    if not name or len(name) > MAX_CARD_NAME_LEN:
        return None
    with _cache_lock:
        if name in _cache:
            _cache.move_to_end(name)
            return _cache[name]

    candidates = [to_wiki_title(name)]
    if candidates[0] != name:
        candidates.append(name)  # 英字のまま載っているカード（《Moonlight at Dawn》）
    url = None
    try:
        for title in candidates:
            if _page_exists(page_url(title), get):
                url = page_url(title)
                break
    except requests.RequestException:
        return search_url(name)
    if url is None:
        url = search_url(name)

    with _cache_lock:
        _cache[name] = url
        _cache.move_to_end(name)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return url
