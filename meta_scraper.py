"""
TCG PORTAL 環境データスクレイパー
=================================
・Tier表（テーマ名・Tier・シェア率・入賞数）
・テーマ別 主要カード（カード名・採用率・平均枚数）
"""

import re
import json
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from pathlib import Path

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

TCG_PORTAL_BASE = "https://tcg-portal.jp"

# ── キャッシュ ──
_CACHE_DIR = Path(__file__).parent / ".cache" / "meta"
_TIER_CACHE_TTL = timedelta(hours=3)
_DECK_CACHE_TTL = timedelta(hours=6)


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


def _fetch_soup(url: str, timeout: int = 20) -> BeautifulSoup | None:
    try:
        res = requests.get(url, headers=HEADERS, timeout=timeout)
        res.raise_for_status()
        return BeautifulSoup(res.text, "html.parser")
    except requests.RequestException as e:
        print(f"  ❌ meta_scraper 取得失敗: {url} — {e}")
        return None


# ── Tier表 ──

def fetch_tier_list(force: bool = False) -> list[dict]:
    """
    TCG PORTAL の環境分析ページから Tier 表を取得。

    Returns:
        [{"name": "巳剣", "tier": 1, "share": 14.9, "tops": 18, "rank": 1}, ...]
    """
    cache_key = "tier_list"
    if not force:
        cached = _cache_read(cache_key, _TIER_CACHE_TTL)
        if cached:
            return cached["tiers"]

    url = f"{TCG_PORTAL_BASE}/yugioh/meta-analysis"
    soup = _fetch_soup(url)
    if not soup:
        # フォールバック: 期限切れキャッシュでも返す
        cached = _cache_read(cache_key, timedelta(days=7))
        return cached["tiers"] if cached else []

    tiers = []
    rank = 0
    seen_names = set()

    # 各デッキエントリは /yugioh/deck-guides/XXXX へのリンクを持つブロック
    # ※ページ内に同一リストが2回出現するため重複排除が必要
    for link_el in soup.select("a[href*='/yugioh/deck-guides/']"):
        block = link_el

        # テーマ名: h3
        name_el = block.select_one("h3")
        if not name_el:
            continue
        name = name_el.get_text(strip=True)
        if not name or name in seen_names:
            continue
        seen_names.add(name)

        rank += 1

        # 代表カード画像
        image_url = ""
        img_el = block.select_one("img[src]")
        if img_el:
            image_url = img_el.get("src", "")

        # Tier 値
        tier = 0
        tier_text = block.get_text()
        tier_m = re.search(r"Tier\s*(\d+)", tier_text)
        if tier_m:
            tier = int(tier_m.group(1))

        # シェア率
        share = 0.0
        share_m = re.search(r"(\d+(?:\.\d+)?)%", tier_text)
        if share_m:
            share = float(share_m.group(1))

        # 入賞回数
        tops = 0
        tops_m = re.search(r"(\d+)回", tier_text)
        if tops_m:
            tops = int(tops_m.group(1))

        tiers.append({
            "name": name,
            "tier": tier,
            "share": share,
            "tops": tops,
            "rank": rank,
            "image": image_url,
        })

    if tiers:
        _cache_write(cache_key, {"tiers": tiers})

    return tiers


# ── テーマ別 主要カード ──

def fetch_deck_cards(theme_name: str, force: bool = False) -> dict:
    """
    TCG PORTAL のデッキ解説ページから主要カードを取得。

    Returns:
        {
            "theme": "巳剣",
            "tier": 1,
            "share": 14.9,
            "cards": [
                {"name": "天羽々斬之巳剣", "adoption": 100.0, "avg": 3.0},
                ...
            ]
        }
    """
    cache_key = f"deck_{theme_name}"
    if not force:
        cached = _cache_read(cache_key, _DECK_CACHE_TTL)
        if cached and "cards" in cached:
            return cached

    encoded = requests.utils.quote(theme_name)
    url = f"{TCG_PORTAL_BASE}/yugioh/deck-guides/{encoded}"
    soup = _fetch_soup(url)
    if not soup:
        cached = _cache_read(cache_key, timedelta(days=7))
        return cached if cached and "cards" in cached else {
            "theme": theme_name, "tier": 0, "share": 0, "cards": []
        }

    # ── 環境での評価 (Tier / シェア) ──
    tier, share = 0, 0.0
    page_text = soup.get_text()
    tier_m = re.search(r"Tier\s*(\d+)", page_text)
    if tier_m:
        tier = int(tier_m.group(1))
    share_m = re.search(r"使用率\s*(\d+(?:\.\d+)?)%", page_text)
    if share_m:
        share = float(share_m.group(1))

    # ── 主要カード ──
    # 各カードは /yugioh/cards/XXX へのリンクを持ち、
    # テキストに「採用率:XX.X%」「平均:X.X枚」が含まれる
    cards = []
    seen = set()

    for card_link in soup.select("a[href*='/yugioh/cards/']"):
        # カード名: h3 内のテキスト
        h3 = card_link.select_one("h3")
        if not h3:
            continue
        card_name = h3.get_text(strip=True)
        if not card_name or card_name in seen:
            continue

        # カード画像: リンク内の img[src]
        image_url = ""
        img_el = card_link.select_one("img[src]")
        if img_el:
            image_url = img_el.get("src", "")

        # 親ブロックのテキストから採用率・平均枚数を取得
        # カードリンクの親要素を遡って数値を探す
        parent = card_link.parent
        if parent is None:
            parent = card_link
        # さらに上位を含むテキスト
        context = parent.get_text() if parent else card_link.get_text()
        # 隣接要素も探す (構造によってはsiblingに数値がある)
        if parent and parent.parent:
            context = parent.parent.get_text()

        adoption = 0.0
        avg = 0.0

        adopt_m = re.search(r"採用率\s*[:：]?\s*(\d+(?:\.\d+)?)%", context)
        if adopt_m:
            adoption = float(adopt_m.group(1))

        avg_m = re.search(r"平均\s*[:：]?\s*(\d+(?:\.\d+)?)枚", context)
        if avg_m:
            avg = float(avg_m.group(1))

        # 採用率がある場合のみ（= 主要カードセクション）
        if adoption > 0:
            seen.add(card_name)
            cards.append({
                "name": card_name,
                "adoption": adoption,
                "avg": avg,
                "image": image_url,
            })

    # ── 入賞デッキレシピリンク ──
    # /yugioh/tournament-results/XXX へのリンクで「デッキレシピ」テキストを含むもの
    recipes = []
    for recipe_link in soup.select("a[href*='/yugioh/tournament-results/']"):
        link_text = recipe_link.get_text(strip=True)
        if "デッキレシピ" not in link_text:
            continue
        href = recipe_link.get("href", "")
        full_url = f"{TCG_PORTAL_BASE}{href}" if href.startswith("/") else href

        # 親要素から大会情報を取得
        parent = recipe_link.parent
        while parent and parent.name not in ("div", "li", "section", "body"):
            parent = parent.parent
        context_text = parent.get_text(" ", strip=True) if parent else ""

        # 日付を抽出
        date = ""
        date_m = re.search(r"(\d{4}/\d{1,2}/\d{1,2})", context_text)
        if date_m:
            date = date_m.group(1)

        # 大会名を抽出 (h3 タグ等)
        title = ""
        if parent:
            h3 = parent.select_one("h3")
            if h3:
                title = h3.get_text(strip=True)

        recipes.append({
            "url": full_url,
            "date": date,
            "title": title,
        })

    # 最新5件に限定
    recipes = recipes[:5]

    result = {
        "theme": theme_name,
        "tier": tier,
        "share": share,
        "cards": cards,
        "recipes": recipes,
    }

    if cards:
        _cache_write(cache_key, result)

    return result


def build_deck_text(cards: list[dict], min_adoption: float = 60.0) -> str:
    """
    主要カードリストからデッキ計算用のテキストを生成。

    採用率が min_adoption% 以上のカードを、平均枚数（四捨五入）で出力。
    """
    lines = []
    for c in cards:
        if c["adoption"] < min_adoption:
            continue
        qty = round(c["avg"]) or 1
        lines.append(f"{qty} {c['name']}")
    return "\n".join(lines)
