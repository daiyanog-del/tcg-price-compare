"""
新弾フィーチャー「店舗×レアリティ価格マトリクス」モジュール
================================================================
/featured ページに、新弾収録カードの (店舗 × レアリティ) 最安値を
DBから即時表示するためのデータを組み立てる。

書き込み側（新弾週の10:15 Cron）は collect_featured.py。本モジュールは
price_history からの読み出し専用。

集計の中核 build_matrix() は Supabase の生行 (records) を受ける純関数として
切り出し、ネットワーク無しでテストできるようにしている。

キャッシュ方式は app.py の /api/featured（_featured_cache / _load_featured_cache）と
同じ stale-while-revalidate: キャッシュ未確立（コールド）時は即座に loading=True を
返しつつバックグラウンドで構築、TTL切れ時は古い値を返しつつ裏で更新する。
リクエストのたびにDB読み出しをブロッキングで待たせない。
"""
from __future__ import annotations
import logging
import threading
import time
import urllib.parse
from datetime import datetime, timedelta

from constants import JST
from featured_pack import get_featured_pack, get_featured_cards
from rarity import UNKNOWN_RARITY_LABEL, normalize_rarity
from scraper import DEFAULT_SHOPS

logger = logging.getLogger(__name__)

# PostgRESTの1リクエスト最大1000行の上限に対する分割取得ページサイズ
# （docs/audit-price-logic-2026-08-06.md F1。tests/test_price_history_read_discipline.py
#  が price_history への読み出しに .range()+.order() があることを機械的に強制する）
_PAGE_SIZE = 1000

# カード名の in_ 句はURL長対策でバイト予算ごとにチャンクする（件数ではなくエンコード後の
# 実バイト数で区切る。カード名の長さはばらつくため、件数固定だと長い名前が続くチャンクで
# URL長超過のリスクが残る）
_CHUNK_BYTE_BUDGET = 6000

# マトリクスは直近7日（JST基準）の観測を対象にする
_LOOKBACK_DAYS = 7

# モジュール内メモリキャッシュ（/api/featured と同じ stale-while-revalidate 方式）
_CACHE_TTL_SEC = 300
_cache_lock = threading.Lock()
_cache: dict = {"data": None, "ts": 0.0}

# バックグラウンド構築の二重起動防止フラグ
_building_lock = threading.Lock()
_building = False


def _today_jst() -> str:
    return datetime.now(JST).date().isoformat()


def _chunk_card_names_by_bytes(card_names: list[str], byte_budget: int = _CHUNK_BYTE_BUDGET) -> list[list[str]]:
    """card_names を URL長対策のバイト予算でチャンクに分割する。

    urllib.parse.quote() でエンコード後のバイト数を実測しながら詰める（件数固定だと
    長いカード名が連続した場合にURL長超過のリスクが残るため）。1件だけで予算を
    超える場合もその1件だけのチャンクにする（無限ループ・チャンク欠落を防ぐ）。
    """
    chunks: list[list[str]] = []
    current: list[str] = []
    current_bytes = 0
    for name in card_names:
        # +3 はカンマ・クォート等のURLエンコード時の余裕分（実測値ではなく安全マージン）
        encoded_len = len(urllib.parse.quote(name)) + 3
        if current and current_bytes + encoded_len > byte_budget:
            chunks.append(current)
            current = []
            current_bytes = 0
        current.append(name)
        current_bytes += encoded_len
    if current:
        chunks.append(current)
    return chunks


def _fetch_price_rows(sb, card_names: list[str], cutoff: str) -> list[dict]:
    """card_names（直近 _LOOKBACK_DAYS 日、JST基準）の price_history 生行を全件取得する。

    PostgRESTの1000行上限に対して range 分割し、URL長対策でカード名をバイト予算ごとに
    チャンクして in_() を投げる。

    例外は握りつぶさずログしてから再送出する（呼び出し元 _load_matrix_cache_inner が
    捕捉し、キャッシュへエラー状態を記録して /api/featured/matrix ルートが503を
    返せるようにするため。featured_pack.py の「捕捉して空へフォールバック」とは異なり、
    価格データの取得失敗を空のマトリクスとして黙って見せるより、エラーとして
    明示的に伝えるべき判断）。
    """
    try:
        all_rows: list[dict] = []
        for chunk in _chunk_card_names_by_bytes(card_names):
            offset = 0
            while True:
                resp = (sb.table("price_history")
                        .select("card_name, shop, rarity, min_price, min_price_any, code, url, recorded_at")
                        .in_("card_name", chunk)
                        .gte("recorded_at", cutoff)
                        .order("card_name", desc=False)
                        .order("shop", desc=False)
                        .order("rarity", desc=False)
                        .order("recorded_at", desc=False)
                        .range(offset, offset + _PAGE_SIZE - 1)
                        .execute())
                batch = resp.data or []
                all_rows.extend(batch)
                if len(batch) < _PAGE_SIZE:
                    break
                offset += _PAGE_SIZE
        return all_rows
    except Exception as e:
        logger.warning(f"[featured_matrix] price_history 取得失敗: {e}")
        raise


def build_matrix(records: list[dict], card_order: list[str], today: str) -> dict:
    """price_history の生行からマトリクス応答形（pack を除く）を組み立てる純関数。

    手順:
      1. rarity は読み出し境界で normalize_rarity を適用する（aggregations.py /
         notify.py の代表レアリティ選定と同じ流儀）。正規化後に空文字・
         UNKNOWN_RARITY_LABEL("(不明)") の行はマトリクスから除外する（司令塔裁定:
         rarity抽出失敗の疑似系列をユーザーに見せない。集計側の「他に候補が無ければ
         フォールバック」とは異なり、単純除外）
      2. (card, shop, rarity) ごとに最新 recorded_at の行を採用
      3. セルの price は min_price_any 優先、NULL/欠落なら min_price にフォールバック
         （両方 NULL の行はセルとして採用しない）
      4. shops は scraper.DEFAULT_SHOPS の順序で、観測が1件以上ある店舗のみ
      5. rows は card_order の順。同一カード内のレアリティは最安セル価格の降順
      6. DBに観測が1行も無いカードも rows に含める（cells 空）

    Returns: {"updated_at": "YYYY-MM-DD", "shops": [...], "rows": [...]}
    updated_at は全セル中で最新の recorded_at（観測が1件も無ければ today）。
    """
    # (card, shop, rarity) -> 採用行（recorded_at 最新を優先）
    latest: dict[tuple[str, str, str], dict] = {}
    for r in records:
        card = r.get("card_name", "")
        shop = r.get("shop", "")
        if not card or not shop:
            continue
        rarity = normalize_rarity(r.get("rarity", "") or "")
        if rarity in ("", UNKNOWN_RARITY_LABEL):
            continue
        key = (card, shop, rarity)
        cur = latest.get(key)
        if cur is None or (r.get("recorded_at") or "") >= (cur.get("recorded_at") or ""):
            latest[key] = r

    # (card, rarity) -> {shop: セル}
    card_rarity_cells: dict[tuple[str, str], dict[str, dict]] = {}
    observed_shops: set[str] = set()
    latest_recorded_at = ""

    for (card, shop, rarity), row in latest.items():
        price = row.get("min_price_any")
        if price is None:
            price = row.get("min_price")
        if price is None:
            continue
        recorded_at = row.get("recorded_at", "") or ""
        if recorded_at > latest_recorded_at:
            latest_recorded_at = recorded_at
        cell = {
            "price": price, "url": row.get("url"),
            "code": row.get("code"), "recorded_at": recorded_at,
        }
        card_rarity_cells.setdefault((card, rarity), {})[shop] = cell
        observed_shops.add(shop)

    shops = [s for s in DEFAULT_SHOPS if s in observed_shops]

    def _min_cell_price(card: str, rarity: str) -> int:
        cells = card_rarity_cells.get((card, rarity), {})
        prices = [c["price"] for c in cells.values() if c.get("price") is not None]
        return min(prices) if prices else -1

    rows = []
    for card in card_order:
        rarities_for_card = sorted({r for (c, r) in card_rarity_cells.keys() if c == card})
        if not rarities_for_card:
            rows.append({"name": card, "rarity": "", "cells": {}})
            continue

        # 最安セル価格の降順（安定ソートのため同価格は上のsortedによる辞書順を維持）
        rarities_for_card.sort(key=lambda r: -_min_cell_price(card, r))

        for rarity in rarities_for_card:
            rows.append({
                "name": card, "rarity": rarity,
                "cells": card_rarity_cells.get((card, rarity), {}),
            })

    return {"updated_at": latest_recorded_at or today, "shops": shops, "rows": rows}


def _load_matrix_cache_inner(sb) -> None:
    """マトリクスを実際に構築し _cache へ格納する（バックグラウンドスレッドで実行）。"""
    pack = get_featured_pack(sb)
    today = _today_jst()

    if not pack:
        result = {"pack": None, "updated_at": today, "shops": [], "rows": []}
        with _cache_lock:
            _cache["data"] = result
            _cache["ts"] = time.time()
        return

    raw_cards = get_featured_cards(sb, pack)
    seen: set[str] = set()
    card_order: list[str] = []
    for name in raw_cards:
        if name and name not in seen:
            seen.add(name)
            card_order.append(name)

    cutoff = (datetime.now(JST) - timedelta(days=_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    try:
        records = _fetch_price_rows(sb, card_order, cutoff) if card_order else []
    except Exception as e:
        # 取得失敗はキャッシュへエラー状態を記録する。/api/featured/matrix ルートが
        # これを検知して503を返す（無限に loading=True を返し続けるのを防ぐ）
        result = {
            "pack": pack, "error": f"価格データの取得に失敗しました: {e}",
            "updated_at": today, "shops": [], "rows": [],
        }
        with _cache_lock:
            _cache["data"] = result
            _cache["ts"] = time.time()
        return

    built = build_matrix(records, card_order, today)
    result = {"pack": pack, **built}
    with _cache_lock:
        _cache["data"] = result
        _cache["ts"] = time.time()
    logger.info(f"[featured_matrix] マトリクスキャッシュ構築完了: {pack['pack_name']} {len(built['rows'])}行")


def _load_matrix_cache(sb) -> None:
    """_load_matrix_cache_inner を二重起動防止つきで実行する。"""
    global _building
    with _building_lock:
        if _building:
            return  # 別スレッドが構築中
        _building = True
    try:
        _load_matrix_cache_inner(sb)
    except Exception as e:
        logger.error(f"[featured_matrix] マトリクスキャッシュ構築失敗: {e}")
    finally:
        with _building_lock:
            _building = False


def get_matrix(sb) -> dict:
    """新弾フィーチャーの店舗×レアリティ価格マトリクスを返す。

    /api/featured と同じ stale-while-revalidate 方式でDB読みをリクエストの
    クリティカルパスに乗せない:
      - キャッシュ未確立（コールド）: バックグラウンド構築を起動し loading=True を即返す
      - キャッシュが新鮮（age < TTL）: そのまま返す
      - キャッシュが古い（age >= TTL）: 古い値を返しつつバックグラウンドで更新

    Returns:
      コールド時   : {"pack": None, "loading": True, "updated_at": "", "shops": [], "rows": []}
      通常時       : {"pack": {...} or None, "updated_at": "YYYY-MM-DD", "shops": [...], "rows": [...]}
      取得失敗時   : 上記に "error": str が追加される（ルート側が503に変換する）
    """
    with _cache_lock:
        cached = _cache["data"]
        ts = _cache["ts"]
    age = time.time() - ts

    if cached is not None and age < _CACHE_TTL_SEC:
        return cached

    if cached is not None and age >= _CACHE_TTL_SEC:
        threading.Thread(target=_load_matrix_cache, args=(sb,), daemon=True).start()
        return cached

    # コールド（キャッシュ未確立）: バックグラウンドロードを起動し loading=True を返す
    threading.Thread(target=_load_matrix_cache, args=(sb,), daemon=True).start()
    return {"pack": None, "loading": True, "updated_at": "", "shops": [], "rows": []}
