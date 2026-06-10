"""
外部値上がりランキングスクレイパー（ベストエフォート）
======================================================
toreca.net 等の値上がりランキングからカード名を取得する。
JS描画やbotブロックで取得できない場合は [] を返す（エラーにしない）。
取得できた場合のみ collect_prices.py の sync_trending_cards が DB に追加する。
"""

import re
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# YGOResources の日本語カード名インデックス（正規名との突合に使用）
# 取得は ygores_repository 経由（Supabaseキャッシュ優先・レート制限つき）
from ygores_repository import repository as _ygores_repo

_valid_names: set[str] | None = None


def _get_valid_names() -> set[str]:
    """正規カード名セットを取得。失敗時は空セット（検証スキップ）。"""
    global _valid_names
    if _valid_names is not None:
        return _valid_names
    try:
        _valid_names = set(_ygores_repo.get_name_index().keys())
    except Exception:
        _valid_names = set()
    return _valid_names


def _scrape_toreca_net() -> list[str]:
    """toreca.net の遊戯王値上がりランキングからカード名を取得。失敗時は []。"""
    url = "https://toreca.net/yugioh/price_up"
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        if res.status_code != 200:
            return []
        soup = BeautifulSoup(res.text, "html.parser")
    except Exception:
        return []

    cards = []
    seen: set[str] = set()

    # toreca.net のランキング要素からカード名を抽出
    # 実ページの構造に合わせて調整が必要になる可能性がある
    for el in soup.select(".card-name, .item-name, h3, h4, .name"):
        text = el.get_text(strip=True)
        if text and len(text) >= 3 and text not in seen:
            # 明らかに非カード名（数字のみ、英数字のみ等）を除外
            if re.search(r'[　-鿿]', text):  # 日本語文字を含む
                seen.add(text)
                cards.append(text)

    return cards


def fetch_trending_cards() -> list[str]:
    """
    外部値上がりランキングからカード名リストを返す。
    取得できない場合・エラーの場合は必ず [] を返す。
    取得名は YGOResources 正規名と突合し、正規名に存在するもののみ返す。
    """
    try:
        raw = _scrape_toreca_net()
    except Exception:
        return []

    if not raw:
        return []

    # 正規名と突合（誤名の流入防止）
    valid = _get_valid_names()
    if valid:
        confirmed = [name for name in raw if name in valid]
    else:
        # 正規名取得失敗時は全件そのまま返す（ノイズが入る可能性あり）
        confirmed = raw

    return confirmed
