"""
TCG PORTAL 環境データスクレイパー
=================================
・Tier表（テーマ名・Tier・シェア率・入賞数）
・テーマ別 主要カード（カード名・採用率・平均枚数）
"""

import re
import json
import time
import os
import tempfile
from threading import Lock
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
_CACHE_IO_LOCK = Lock()


def _cache_path(key: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\-]", "_", key)
    return _CACHE_DIR / f"{safe}.json"


def _cache_read(key: str, ttl: timedelta) -> dict | None:
    fp = _cache_path(key)
    try:
        # Windowsでも同一プロセスの読込と置換が競合しないようにする。
        # 存在確認も鍵の内側で行う。Windows の os.stat はファイルを共有不可で一瞬開くため、
        # 鍵の外で exists() すると別スレッドの os.replace が PermissionError で失敗する。
        with _CACHE_IO_LOCK:
            if not fp.exists():
                return None
            data = json.loads(fp.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(data["_ts"])
        if datetime.now() - ts > ttl:
            return None
        return data
    except Exception:
        return None


def _cache_write(key: str, data: dict):
    fp = _cache_path(key)
    # 本文と取得条件を同じファイルへ原子的に保存し、並行読込時の欠損を防ぐ。
    payload = {**data, "_ts": datetime.now().isoformat()}
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=fp.parent,
                                         suffix=".tmp", delete=False) as tmp:
            tmp_name = tmp.name
            json.dump(payload, tmp, ensure_ascii=False)
        with _CACHE_IO_LOCK:
            os.replace(tmp_name, fp)
    finally:
        if tmp_name and os.path.exists(tmp_name):
            os.remove(tmp_name)


def _fetch_soup(url: str, timeout: int = 20, retries: int = 2) -> BeautifulSoup | None:
    for attempt in range(retries + 1):
        try:
            res = requests.get(url, headers=HEADERS, timeout=timeout)
            res.raise_for_status()
            return BeautifulSoup(res.text, "html.parser")
        except requests.RequestException as e:
            if attempt < retries:
                time.sleep(1)
                continue
            print(f"  meta_scraper 取得失敗: {url} — {e}")
            return None


# ── Tier表 ──

# 集計期間の方針（アダプティブ）:
#   相手サイトは不定期スナップショットで環境データを公開しており、直近30日に
#   充実したスナップショットが無いと数テーマしか返らない。そこでまず30日窓を
#   試し、テーマ数が閾値未満なら60日まで自動で広げて最新の充実データを拾う。
#   ※ 90日以上にすると API が空配列を返すため上限は60日。
_TIER_WINDOW_DAYS = [30, 60]
_TIER_MIN_THEMES = 5


def _fetch_meta_complete(date_from: str, date_to: str) -> list[dict] | None:
    """meta-analysis/complete API を叩いて deckSummaries を返す。失敗時 None。"""
    url = (f"{TCG_PORTAL_BASE}/api/yugioh/meta-analysis/complete"
           f"?from={date_from}&to={date_to}")
    for attempt in range(3):
        try:
            res = requests.get(url, headers=HEADERS, timeout=20)
            res.raise_for_status()
            data = res.json()
            if isinstance(data, dict):
                return data.get("deckSummaries") or []
            return []
        except (requests.RequestException, ValueError) as e:
            if attempt < 2:
                time.sleep(1)
                continue
            print(f"  meta_scraper Tier API 取得失敗: {url} — {e}")
    return None


def tier_snapshot(cached: dict | None, *, stale: bool = False,
                  refresh_error: str | None = None) -> dict:
    """行と取得条件を一体で返す。旧キャッシュにない条件は推測しない。"""
    cached = cached or {}
    tiers = cached.get("tiers", [])
    metadata = {"from": None, "to": None, "fetched_at": None,
                "stale": False, "sample_size": None, "theme_count": len(tiers),
                "refresh_error": None}
    metadata.update(cached.get("metadata") or {})
    metadata.update(stale=stale, refresh_error=refresh_error)
    return {"tiers": tiers, "metadata": metadata}


def fetch_tier_list(force: bool = False) -> list[dict]:
    """既存呼び出し用に、従来どおりTier行のリストだけを返す。"""
    return fetch_tier_snapshot(force)["tiers"]


def fetch_tier_snapshot(force: bool = False) -> dict:
    """
    TCG PORTAL の環境分析データから Tier 表を取得。

    取得元は meta-analysis ページの裏で叩かれている JSON API
    `/api/yugioh/meta-analysis/complete?from=...&to=...`。
    ※ 2026-06 に同ページが Next.js の SPA 化し、サーバーレンダリングHTMLには
       2テーマしか含まれなくなったため、HTMLスクレイプから API 直叩きへ移行した。
       （from/to を明示しないと API 側も少数しか返さないため期間は必須。
        期間の決め方は _TIER_WINDOW_DAYS のアダプティブ方式を参照）

    Returns:
        {"tiers": [...], "metadata": {"from": ..., "to": ..., "stale": ...}}
    """
    cache_key = "tier_list"
    if not force:
        cached = _cache_read(cache_key, _TIER_CACHE_TTL)
        if cached:
            return tier_snapshot(cached)

    today = datetime.now()
    date_to = today.strftime("%Y-%m-%d")

    # アダプティブ: 窓を順に広げ、閾値を満たした時点で確定。
    # どの窓も閾値未満なら最もテーマ数が多かった結果を採用する。
    best_summaries: list[dict] | None = None
    best_from = None
    api_reached = False
    for days in _TIER_WINDOW_DAYS:
        date_from = (today - timedelta(days=days)).strftime("%Y-%m-%d")
        summaries = _fetch_meta_complete(date_from, date_to)
        if summaries is None:
            continue  # 通信失敗。次の窓へ
        api_reached = True
        if best_summaries is None or len(summaries) > len(best_summaries):
            best_summaries = summaries
            best_from = date_from
        if len(summaries) >= _TIER_MIN_THEMES:
            break

    if not best_summaries:
        # API応答が空/全窓失敗 → 期限切れキャッシュでも返す
        cached = _cache_read(cache_key, timedelta(days=7))
        return tier_snapshot(cached, stale=bool(cached),
                             refresh_error="empty_response" if api_reached else "fetch_failed")

    tiers = []
    for i, s in enumerate(best_summaries):
        name = (s.get("deckGuideName") or "").strip()
        if not name:
            continue
        tiers.append({
            "name": name,
            "tier": int(s.get("tier") or 0),
            "share": float(s.get("usageRate") or 0.0),  # 使用率(%)
            "tops": int(s.get("totalDecks") or 0),       # 入賞デッキ数
            "rank": int(s.get("rank") or (i + 1)),
            "image": s.get("representativeCardImage") or "",
        })

    if tiers:
        snapshot = tier_snapshot({"tiers": tiers, "metadata": {
            "from": best_from, "to": date_to,
            "fetched_at": datetime.now().astimezone().isoformat(),
            # totalDecksの総和が重複のない母数かは未確認なので合計を断定しない。
            "sample_size": None,
        }})
        _cache_write(cache_key, snapshot)
        return snapshot
    elif api_reached:
        # API は応答したが有効テーマ0件。古いキャッシュを優先して空表示を避ける
        cached = _cache_read(cache_key, timedelta(days=7))
        return tier_snapshot(cached, stale=bool(cached), refresh_error="empty_response")

    return tier_snapshot(None, refresh_error="fetch_failed")


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
    # 2026-10 のサイト改修で、主要カードは data-quote-row="usage" の行になった
    # （カード名はリンクではなくボタン内の span・採用率は最初の <b>「100%」・
    # 平均枚数は行内の「平均X.X枚」）。旧構造（/yugioh/cards/ リンク＋「採用率:XX%」）は
    # サイドデッキ採用傾向の欄にしか残っておらず、拾うと別の数字になるため使わない。
    cards = []
    seen = set()

    for row in soup.select('[data-quote-row="usage"]'):
        name_el = row.select_one("button span")
        if not name_el:
            continue
        card_name = name_el.get_text(strip=True)
        if not card_name or card_name in seen:
            continue

        adoption = 0.0
        avg = 0.0
        row_text = row.get_text("", strip=True)
        adopt_el = row.select_one("b")
        adopt_text = adopt_el.get_text(strip=True) if adopt_el else ""
        adopt_m = re.fullmatch(r"(\d+(?:\.\d+)?)%", adopt_text)
        if adopt_m:
            adoption = float(adopt_m.group(1))
        avg_m = re.search(r"平均\s*(\d+(?:\.\d+)?)\s*枚", row_text)
        if avg_m:
            avg = float(avg_m.group(1))

        img_el = row.select_one("img[src]")
        image_url = img_el.get("src", "") if img_el else ""

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

    # ── 最新レシピからフルデッキリストを取得 ──
    full_deck = []
    if recipes:
        full_deck = fetch_recipe_deck(recipes[0]["url"])

    if not cards and "カード使用頻度データがありません" in page_text:
        # 使用率の低いデッキはサイト側に主要カードのデータが無い（2026-10-29 Tier 4 の7件）。構造変更ではない
        print(f"  meta_scraper: 主要カードのデータがサイトに無いデッキです: {url}")
    elif not cards:
        # ページは取れたのに主要カードが0件＝相手サイトの構造変更の疑い（07-10・10-28 と2回とも黙って空になった）
        print(f"  meta_scraper 警告: 主要カードを読めませんでした（構造変更の疑い）: {url}")

    result = {
        "theme": theme_name,
        "tier": tier,
        "share": share,
        "cards": cards,
        "recipes": recipes,
        "full_deck": full_deck,
    }

    if cards or full_deck:
        _cache_write(cache_key, result)

    return result


def fetch_recipe_deck(recipe_url: str) -> list[dict]:
    """
    入賞デッキレシピページからフルデッキリスト（メイン+EX）を取得。

    Returns:
        [{"name": "カード名", "qty": 3, "is_ex": False}, ...]
    """
    soup = _fetch_soup(recipe_url)
    if not soup:
        return []

    # 2026-10 のサイト改修で、見出しは「メインデッキ（40枚）」のように枚数つきになり、
    # 表（table）は data-quote-row="deck" の行に変わった（枚数は data-deck-qty・
    # カード名はボタン内の span）。サイドデッキは数えない。
    cards = []
    for h3 in soup.select("h3"):
        section = h3.get_text(strip=True)
        if section.startswith("メインデッキ"):
            is_ex = False
        elif section.startswith("EXデッキ"):
            is_ex = True
        else:
            continue
        for el in h3.find_all_next():
            if el.name == "h3":
                break  # 次の区分に入った
            if el.get("data-quote-row") != "deck":
                continue
            name_el = el.select_one("button span")
            name = name_el.get_text(strip=True) if name_el else ""
            if not name:
                continue
            qty_el = el.select_one("[data-deck-qty]")
            qty_text = qty_el.get_text(strip=True) if qty_el else ""
            qty = int(qty_text) if qty_text.isdigit() else 1
            cards.append({"name": name, "qty": qty, "is_ex": is_ex})

    if not cards:
        # ページは取れたのに1枚も読めない＝相手サイトの構造変更の疑い（10-28 は黙って空になった）
        print(f"  meta_scraper 警告: レシピからカードを読めませんでした（構造変更の疑い）: {recipe_url}")

    return cards


def build_deck_text(cards: list[dict], min_adoption: float = 40.0) -> str:
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


def build_recipe_text(recipe_cards: list[dict]) -> str:
    """フルデッキリストからデッキ計算用のテキストを生成。is_ex フラグで [EX] 区切りを付ける。"""
    main = [c for c in recipe_cards if not c.get("is_ex")]
    ex   = [c for c in recipe_cards if c.get("is_ex")]
    lines = [f"{c['qty']} {c['name']}" for c in main]
    if ex:
        lines.append("[EX]")
        lines.extend(f"{c['qty']} {c['name']}" for c in ex)
    return "\n".join(lines)
