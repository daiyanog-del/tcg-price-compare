"""
遊戯王 パックカードリスト スクレイパー
========================================
・公式サイトから最新パック一覧を自動取得
・遊戯王Wikiからパックの収録カードリストを取得（メイン）
・YGOPRODeck APIでTCGセット名から日本語カード名を取得（フォールバック）
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


# ── 公式サイトから最新パック一覧を自動取得 ──

_KONAMI_PRODUCTS_URL = "https://www.yugioh-card.com/japan/products/"
_PACK_CATEGORIES = ["basic", "special", "concept"]
_MAX_PACKS = 2  # 表示するパック数


def _fetch_latest_packs_from_official() -> list[dict]:
    """
    公式サイトの商品ページからパック情報を取得。
    JS内の p[N]={...} 形式のデータを解析する。
    """
    all_packs = []

    for cat in _PACK_CATEGORIES:
        try:
            url = f"{_KONAMI_PRODUCTS_URL}?{cat}"
            res = requests.get(url, headers=HEADERS, timeout=15)
            # p[N]={...} パターンを抽出
            for m in re.finditer(r'p\[\d+\]\s*=\s*\{([^}]+)\}', res.text):
                content = m.group(1)
                title_m = re.search(r'"title":"([^"]+)"', content)
                date_m = re.search(r'"release-date":"([^"]+)"', content)
                if not title_m or not date_m:
                    continue

                title = title_m.group(1)
                raw_date = date_m.group(1)

                # 日付を解析（例: "2025年3月22日(土)"）
                date_match = re.search(r'(\d{4})\D+(\d{1,2})\D+(\d{1,2})', raw_date)
                if not date_match:
                    continue
                y, mo, d = date_match.groups()
                iso_date = f"{y}-{int(mo):02d}-{int(d):02d}"

                # 未来すぎるパック（発売前でカードリスト未公開）を除外
                try:
                    release = datetime.strptime(iso_date, "%Y-%m-%d")
                    if release > datetime.now() + timedelta(days=7):
                        continue
                except ValueError:
                    continue

                # パック名の正規化（公式サイトのダッシュを統一）
                # 公式は半角ハイフン+スペースだが、Wikiは特殊ダッシュ(U+2212)の場合がある
                wiki_page = title.strip()

                all_packs.append({
                    "name": title.strip(),
                    "wiki_page": wiki_page,
                    "tcg_name": "",
                    "date": iso_date,
                    "category": cat,
                })
        except Exception as e:
            print(f"  pack_scraper 公式サイト取得失敗 ({cat}): {e}")

    # 発売日順（新しい順）にソート
    all_packs.sort(key=lambda x: x["date"], reverse=True)
    return all_packs


def _try_wiki_page_variants(pack_name: str) -> str:
    """
    Wikiページ名のバリエーションを試してカードリストが取れるものを返す。
    公式サイトのダッシュ表記とWikiの表記が異なる場合がある。
    """
    # ダッシュのバリエーション
    variants = [
        pack_name,  # そのまま
        pack_name.replace(" - ", " \u2212").replace(" -", "\u2212").replace("- ", "\u2212"),  # 半角→数学マイナス
        re.sub(r'\s*-\s*', ' \u2212', pack_name),  # ハイフンを全てU+2212に
        re.sub(r'\s*-\s*', '\u2212', pack_name),  # スペースなしU+2212
    ]

    seen = set()
    for v in variants:
        v = v.strip()
        if v in seen:
            continue
        seen.add(v)
        cards = _fetch_from_wiki(v, v)
        if cards:
            return v

    return pack_name  # どれも取れなければ元の名前を返す


def get_pack_list() -> list[dict]:
    """
    最新パック一覧を返す。
    公式サイトから自動取得し、Wikiでカード取得可能なパックを返す。
    結果はキャッシュする（24時間）。
    """
    cache_key = "pack_list_auto"
    cached = _cache_read(cache_key, timedelta(hours=24))
    if cached and "packs" in cached and len(cached["packs"]) > 0:
        return cached["packs"]

    official_packs = _fetch_latest_packs_from_official()
    if not official_packs:
        # フォールバック: キャッシュがあれば古くても返す
        old = _cache_read(cache_key, timedelta(days=30))
        if old and "packs" in old:
            return old["packs"]
        return []

    # 直近パックのうち、Wikiでカードリストが取れるものを_MAX_PACKS個選ぶ
    valid_packs = []
    for pack in official_packs:
        if len(valid_packs) >= _MAX_PACKS:
            break

        wiki_page = _try_wiki_page_variants(pack["name"])
        valid_packs.append({
            "name": pack["name"],
            "wiki_page": wiki_page,
            "tcg_name": pack.get("tcg_name", ""),
            "date": pack["date"],
        })

    if valid_packs:
        _cache_write(cache_key, {"packs": valid_packs})

    return valid_packs


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
