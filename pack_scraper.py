"""
遊戯王Wiki パックカードリスト スクレイパー
============================================
・遊戯王Wikiからパックのカードリスト（カード名一覧）を取得
・パック一覧はJSONファイルで管理（定期的に手動更新）
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


# ── パック内カードリスト取得（遊戯王Wikiスクレイピング）──

def fetch_pack_cards(pack_name: str, wiki_page: str = "") -> dict:
    """
    遊戯王Wikiからパックのカードリストを取得。

    Args:
        pack_name: パック名（表示用）
        wiki_page: Wikiページ名（URLエンコード前）。未指定ならpack_nameをそのまま使う

    Returns:
        {
            "pack": "ALLIANCE INSIGHT",
            "cards": ["カード名1", "カード名2", ...],
            "count": 80
        }
    """
    page = wiki_page or pack_name
    cache_key = f"pack_{page}"
    cached = _cache_read(cache_key, _PACK_LIST_CACHE_TTL)
    if cached and "cards" in cached:
        return cached

    # 遊戯王WikiはEUC-JPエンコーディング
    url = f"{WIKI_BASE}?{quote(page, encoding='euc-jp')}"

    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        res.raise_for_status()
        # レスポンスのエンコーディングを正しく設定
        res.encoding = res.apparent_encoding or 'euc-jp'
        soup = BeautifulSoup(res.text, "html.parser")
    except requests.RequestException as e:
        print(f"  pack_scraper 取得失敗: {url} — {e}")
        # 期限切れキャッシュでも返す
        cached = _cache_read(cache_key, timedelta(days=30))
        if cached and "cards" in cached:
            return cached
        return {"pack": pack_name, "cards": [], "count": 0}

    cards = []
    seen = set()

    # パターン: <li>内に カード番号 + 「カード名」のaタグ
    # 例: <li>ALIN-JP001 <a href="...">「ウィザード♯イグニスター」</a> ...
    for li in soup.select("li"):
        text = li.get_text()
        # カード番号パターンを検出（XX-JP000 形式）
        if not re.search(r'[A-Z0-9]+-JP\d{3}', text):
            continue

        # li内の最初のaタグからカード名を取得
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
                break  # 最初のカード名リンクだけ取得

    result = {
        "pack": pack_name,
        "cards": cards,
        "count": len(cards),
    }

    if cards:
        _cache_write(cache_key, result)

    return result
