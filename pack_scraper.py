"""
遊戯王 パックカードリスト スクレイパー
========================================
・遊戯王Wikiからパックの収録カードリストを取得（メイン）
・YGOPRODeck APIでTCGセット名から日本語カード名を取得（フォールバック）
・パック一覧はJSONファイルで管理
"""

import re
import json
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

WIKI_BASE = "https://yugioh-wiki.net/index.php"
YGOPRODECK_API = "https://db.ygoprodeck.com/api/v7"

# ── キャッシュ ──
_CACHE_DIR = Path(__file__).parent / ".cache" / "packs"
_PACK_LIST_CACHE_TTL = timedelta(hours=12)

def _cache_path(key: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\-]", "_", key)
    return _CACHE_DIR / f"{safe}.json"

def _cache_read(key: str, ttl: timedelta) -> dict | None:
    fp = _cache_path(key)
    if not fp.exists():
        return None
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(data["_ts"])
        if datetime.now() - ts > ttl:
            return None
        return data
    except Exception:
        return None

def _cache_write(key: str, data: dict):
    fp = _cache_path(key)
    data["_ts"] = datetime.now().isoformat()
    fp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ── パック一覧（JSONファイルから読み込み）──

_PACKS_FILE = Path(__file__).parent / "data" / "packs.json"

def get_pack_list() -> list[dict]:
    """パック一覧を返す"""
    if not _PACKS_FILE.exists():
        return []
    try:
        return json.loads(_PACKS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


# ── 方法1: 遊戯王Wikiからカードリスト取得 ──

def _fetch_from_wiki(pack_name: str, wiki_page: str) -> list[str]:
    """遊戯王Wikiからパックのカード名リストを取得"""
    page = wiki_page or pack_name
    url = f"{WIKI_BASE}?{quote(page, encoding='euc-jp')}"

    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        res.raise_for_status()
        res.encoding = res.apparent_encoding or 'euc-jp'
        soup = BeautifulSoup(res.text, "html.parser")
    except requests.RequestException as e:
        print(f"  pack_scraper Wiki取得失敗: {url} — {e}")
        return []

    cards = []
    seen = set()

    for li in soup.select("li"):
        text = li.get_text()
        if not re.search(r'[A-Z0-9]+-JP\d{3}', text):
            continue

        links = li.select("a")
        for a in links:
            a_text = a.get_text(strip=True)
            # 《》「」で囲まれたカード名を抽出
            m = re.match(r'[\u300A\u300C\u300E\u3008](.+?)[\u300B\u300D\u300F\u3009]', a_text)
            if m:
                card_name = m.group(1)
                if card_name not in seen:
                    seen.add(card_name)
                    cards.append(card_name)
                break

    return cards


# ── 方法2: YGOPRODeck APIからカードリスト取得 ──

def _fetch_from_ygoprodeck(tcg_set_name: str) -> list[str]:
    """YGOPRODeck APIからTCGセット名で日本語カード名を取得"""
    if not tcg_set_name:
        return []

    url = f"{YGOPRODECK_API}/cardinfo.php?cardset={quote(tcg_set_name)}&language=ja"

    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        if res.status_code != 200:
            return []
        data = res.json()
        cards = []
        seen = set()
        for card in data.get("data", []):
            name = card.get("name", "")
            if name and name not in seen:
                seen.add(name)
                cards.append(name)
        return cards
    except Exception as e:
        print(f"  pack_scraper YGOPRODeck取得失敗: {tcg_set_name} — {e}")
        return []


# ── メイン関数 ──

def fetch_pack_cards(pack_name: str, wiki_page: str = "", tcg_name: str = "") -> dict:
    """
    パックのカードリストを取得。Wiki → YGOPRODeck APIの順で試行。

    Args:
        pack_name: パック名（表示用）
        wiki_page: Wikiページ名（URLエンコード前）
        tcg_name: TCG版のセット名（YGOPRODeck API用）

    Returns:
        {"pack": "...", "cards": [...], "count": N}
    """
    cache_key = f"pack_{pack_name}"
    cached = _cache_read(cache_key, _PACK_LIST_CACHE_TTL)
    if cached and "cards" in cached and len(cached["cards"]) > 0:
        return cached

    # 方法1: 遊戯王Wiki
    cards = _fetch_from_wiki(pack_name, wiki_page)

    # 方法2: Wikiで取得できなければYGOPRODeck API
    if not cards and tcg_name:
        cards = _fetch_from_ygoprodeck(tcg_name)

    result = {
        "pack": pack_name,
        "cards": cards,
        "count": len(cards),
    }

    if cards:
        _cache_write(cache_key, result)
    else:
        # 期限切れキャッシュがあればそれを返す
        old = _cache_read(cache_key, timedelta(days=30))
        if old and "cards" in old and len(old["cards"]) > 0:
            return old

    return result
