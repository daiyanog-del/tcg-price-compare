"""
店舗×レアリティ価格マトリクス（新商品タブの「店舗×レア」）
================================================================
パックの収録カードについて、(店舗 × レアリティ) の最安値を毎朝の収集（price_history）から
組み立てる。読み出し専用。2026-10-22 から新弾だけでなく、どの発売済みパックでも作る
（以前は新弾1つ・直近7日をカード名で探していた。/api/featured/matrix は廃止）。

読み方: カード名 → card_dim のカード番号 → price_history をカード番号＋日付（今日・昨日）で引く。
price_history_v2 の索引 (recorded_at, card_id, ...) が両方の条件で効く形にしてある
（2026-10-22 朝、索引の効かない問い合わせで Disk IO の持ち分を使い切りかけたため）。
昨日の記録は、今日の記録で全店がそろっていないカードのぶんだけ読み、カード×店ごとに今日が無いところを埋める。カード番号の束は同時に読む（下の _fetch_price_rows）。
結果はカード名の並びごとに1時間覚える。

集計の中核 build_matrix() は Supabase の生行 (records) を受ける純関数として
切り出し、ネットワーク無しでテストできるようにしている。
"""
from __future__ import annotations
import logging
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import httpx

from constants import JST
from name_normalize import db_lookup_names, pick_card_ids
from rarity import UNKNOWN_RARITY_LABEL, normalize_rarity
from scraper import DEFAULT_SHOPS
from shop_status import is_suspended

logger = logging.getLogger(__name__)

# PostgRESTの1リクエスト最大1000行の上限に対する分割取得ページサイズ
# （docs/audit-price-logic-2026-08-06.md F1。tests/test_price_history_read_discipline.py
#  が price_history への読み出しに .range()+.order() があることを機械的に強制する）
_PAGE_SIZE = 1000

# カード名の in_ 句はURL長対策でバイト予算ごとにチャンクする（件数ではなくエンコード後の
# 実バイト数で区切る。カード名の長さはばらつくため、件数固定だと長い名前が続くチャンクで
# URL長超過のリスクが残る）
_CHUNK_BYTE_BUDGET = 6000

# カード番号の in_ 句の1回あたりの件数。1日ぶんが1ページ（1000行）に収まる大きさにして、束どうしを同時に読む。
# 実測（2026-10-22・収録100種のパックで1日2,216行＝平均22行/種。40種の束で1,366行＝34行/種の偏りあり）から
# 25種≒850行。1000行を超える束は range で次のページを読むので、外れても欠けはしない（往復が1回増えるだけ）
_ID_CHUNK = 25

# 同時に読む束の数の上限（プロセス全体で共有。何人が同時に開いても、データベースへの同時の問い合わせはこの数まで）。
# 本番（Render オレゴン）→ DB（東京）は1往復が0.4〜1秒かかり、以前は8往復を順に待っていた（2026-10-22 実測）
_FETCH_WORKERS = 4  # TODO: calibrate from data
_executor = ThreadPoolExecutor(max_workers=_FETCH_WORKERS, thread_name_prefix="pack-matrix")

# 読む日数（今日・昨日）。朝5時の収集が途中でも、昨日の値で欠けを埋める（build_matrix が新しい日を優先）
_LOOKBACK_DAYS = 2

# パック（カードの集まり）ごとのメモリキャッシュ。並び順は key に含めない（同じパックで並びが違っても同じ記録を使う。reviewer 中4）
_CACHE_TTL_SEC = 3600  # TODO: calibrate from data（毎朝の収集は1日1回なので長めでよい）
_CACHE_MAX_ENTRIES = 64
# 取得に失敗した集まりは、この秒数だけ失敗を覚えて読み直さない（障害の最中に負荷を積み上げない。reviewer 低7）
_FAIL_TTL_SEC = 30  # TODO: calibrate from data
_cache_lock = threading.Lock()
_cache: dict = {}  # key: tuple(sorted(card_names)) -> {"records": list, "ts": float} または {"error": str, "ts": float}


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


def _execute(build):
    """問い合わせを1回送る。接続ごと切られた（HTTP/2 の GOAWAY＝ConnectionTerminated）ときだけ1回送り直す。

    同じ接続で並んでいた問い合わせがまとめて落ちる種類の失敗で、データベースの遅さとは別物
    （2026-10-22 本番で3回、表が503になった）。遅い・失敗が続くときは送り直さない（負荷を積まない）。
    """
    try:
        return build().execute()
    except httpx.RemoteProtocolError as e:
        logger.info(f"[featured_matrix] 接続が切られたので1回だけ送り直す: {e}")
        return build().execute()


def _fetch_card_ids(sb, card_names: list[str]) -> dict[int, str]:
    """カード名 → card_dim のカード番号（名簿に無い名前は返らない）。名前の束は同時に引く。
    値は呼び出し元の名前。収集と同じそろえ方の名前でも引く（name_normalize.db_lookup_names）。"""
    lookup = db_lookup_names(card_names)

    def one(chunk):
        return _execute(lambda: sb.table("card_dim").select("card_id, card_name").in_("card_name", chunk)).data or []

    rows = [r for data in _executor.map(one, _chunk_card_names_by_bytes(list(lookup))) for r in data]
    return pick_card_ids(rows, lookup)


def _fetch_day_rows(sb, chunk: list[int], day: str) -> list[dict]:
    """カード番号の1束・1日ぶん。1000行を超えたら range で次のページを読む。"""
    rows: list[dict] = []
    offset = 0
    while True:
        def build(offset=offset):
            q = (sb.table("price_history")
                 .select("card_id, shop_id, rarity_id, card_name, shop, rarity, min_price, min_price_any, code, url, recorded_at")
                 .in_("card_id", chunk)
                 .eq("recorded_at", day))
            # 索引 (recorded_at, card_id, shop_id, rarity_id) の順に並べる（名前で並べると並べ替えが入る。reviewer 低6）
            return (q.order("card_id", desc=False)
                    .order("shop_id", desc=False)
                    .order("rarity_id", desc=False)
                    .range(offset, offset + _PAGE_SIZE - 1))
        batch = _execute(build).data or []
        rows.extend(batch)
        if len(batch) < _PAGE_SIZE:
            return rows
        offset += _PAGE_SIZE


def _fetch_price_rows(sb, card_ids: list[int], days: list[str]) -> list[dict]:
    """card_ids の price_history を、新しい日付（JST）から順に等号で引く。

    日付の範囲（gte）ではなく等号にするのは、索引 (recorded_at, card_id, ...) の2列とも条件に
    使わせるため（範囲だと日付の範囲をなめてからカード番号で絞る計画になりうる）。
    古い日は、それより新しい日の記録で全部の店（scraper.DEFAULT_SHOPS）がそろっていないカードだけ読む。
    以前は「新しい日に1行でもある店」をパック全体で除いていたため、深夜に1枚だけ検索されると、その店の
    昨日の記録がパックの全カードぶん読まれず「このパックの版の記録がありません」になった（2026-11-01）。
    どの行を使うかはカード×店ごとに _prepare_records が決める。
    カード番号の束は同時に読み（上限 _FETCH_WORKERS）、PostgRESTの1000行上限に対して range 分割する。

    例外はログしてから再送出する（呼び出し元のルートが503を返す。価格データの取得失敗を
    空の表として黙って見せない）。
    """
    try:
        all_rows: list[dict] = []
        all_shops = set(DEFAULT_SHOPS)
        shops_by_card: dict[int, set[str]] = {}
        pending = list(card_ids)
        for day in days:
            if not pending:
                break
            chunks = [pending[i:i + _ID_CHUNK] for i in range(0, len(pending), _ID_CHUNK)]
            day_rows = [r for rows in _executor.map(lambda c: _fetch_day_rows(sb, c, day), chunks) for r in rows]
            for r in day_rows:
                if r.get("card_id") is not None:
                    shops_by_card.setdefault(int(r["card_id"]), set()).add(r.get("shop"))
            all_rows.extend(day_rows)
            pending = [cid for cid in pending if not shops_by_card.get(cid, set()) >= all_shops]
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
        if is_suspended(shop):
            continue  # 休止中の店は表に出さない（shop_status.py）
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


def _cache_key(card_names, pack_code: str = "") -> tuple:
    """覚えておく鍵。パックの型番の頭を渡したときは版ごとの価格も読むので別の鍵にする"""
    return (pack_code or "",) + tuple(sorted({n for n in card_names if n}))


def is_cached(card_names: list[str], pack_code: str = "") -> bool:
    """この集まりの記録（または直近の失敗）を覚えているか。ルートが受付上限を消費するかの判断に使う"""
    key = _cache_key(card_names, pack_code)
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
    if not hit:
        return False
    ttl = _FAIL_TTL_SEC if "error" in hit else _CACHE_TTL_SEC
    return now - hit["ts"] < ttl


def _prepare_records(records: list[dict]) -> list[dict]:
    """表に使う前の記録の整え（get_pack_matrix 専用。build_matrix は新弾の頃から共通なので触らない）。

    1. 同じカード×店に今日の記録があれば、そのカード×店の昨日の記録は使わない（今日売り切れたレアリティに
       昨日の値段が残らないように。reviewer 中3）。今日の記録が無いカード×店は昨日で埋める（画面は日付を添える）。
       以前は店単位でパック全体に当てていたため、1枚だけ今日の記録があると、その店の昨日の記録が全カードぶん
       消えた（2026-11-01 IMMORTAL PHOENIX）
    2. 表に出す値段（min_price_any・状態を問わない最安）と型番の出品（min_price・通常品の最安）が
       違う行は、型番を空（版不明）にする。別の出品の型番で版を判定しない（reviewer 高2。price_persist の code の意味）
    """
    if not records:
        return records
    newest: dict[tuple, str] = {}
    for r in records:
        key = (r.get("card_name"), r.get("shop"))
        day = str(r.get("recorded_at") or "")
        if day > newest.get(key, ""):
            newest[key] = day
    out = []
    for r in records:
        if str(r.get("recorded_at") or "") != newest.get((r.get("card_name"), r.get("shop")), ""):
            continue
        mp, mpa = r.get("min_price"), r.get("min_price_any")
        if mp is not None and mpa is not None and mp != mpa:
            r = dict(r, code="")
        out.append(r)
    return out


_pack_table_missing = False  # price_history_pack.sql 未適用なら、そのプロセスでは版ごとの価格を読まない


def _fetch_pack_rows(sb, pack_code: str, ids: list[int], days: list[str]) -> list[dict]:
    """パックの版ごとの価格（price_history_pack_v）を days の日付で読む。読めなければ空（表は本表の行だけで出す）"""
    global _pack_table_missing
    if _pack_table_missing or not ids:
        return []
    rows: list[dict] = []
    try:
        for day in days:
            for i in range(0, len(ids), _ID_CHUNK):
                chunk = ids[i:i + _ID_CHUNK]
                offset = 0
                while True:
                    batch = _execute(lambda offset=offset, chunk=chunk, day=day: (
                        sb.table("price_history_pack_v")
                        .select("card_id, card_name, shop, rarity, min_price, min_price_any, code, url, recorded_at")
                        .eq("pack_code", pack_code).eq("recorded_at", day).in_("card_id", chunk)
                        .order("card_id").order("shop_id").order("rarity_id")
                        .range(offset, offset + _PAGE_SIZE - 1))).data or []
                    rows.extend(batch)
                    if len(batch) < _PAGE_SIZE:
                        break
                    offset += _PAGE_SIZE
    except Exception as e:
        text = str(e)
        if "PGRST205" in text or ("price_history_pack_v" in text and "does not exist" in text):
            _pack_table_missing = True
            logger.warning("[featured_matrix] price_history_pack_v が DB に無い（price_history_pack.sql 未適用）。版ごとの価格は重ねない")
        else:
            logger.warning(f"[featured_matrix] 版ごとの価格を読めない（本表だけで表を出す）: {e}")
        return []
    return rows


def pack_cells(pack_records: list[dict]) -> list[dict]:
    """版ごとの価格の行を、画面が品として読む形にする（カード×店×レアリティごとに新しい日の1行）。
    レアリティの正規化・休止中の店・(不明) の除外は build_matrix と同じ。型番はこのパックの版の行なので空にしない"""
    latest: dict[tuple, dict] = {}
    for r in pack_records:
        card, shop = r.get("card_name", ""), r.get("shop", "")
        if not card or not shop or is_suspended(shop):
            continue
        rarity = normalize_rarity(r.get("rarity", "") or "")
        if rarity in ("", UNKNOWN_RARITY_LABEL):
            continue
        price = r.get("min_price_any")
        if price is None:
            price = r.get("min_price")
        if price is None:
            continue
        key = (card, shop, rarity)
        cur = latest.get(key)
        if cur is None or (r.get("recorded_at") or "") >= cur["recorded_at"]:
            latest[key] = {"name": card, "rarity": rarity, "shop": shop, "price": price, "url": r.get("url"),
                           "code": r.get("code"), "recorded_at": r.get("recorded_at") or ""}
    return list(latest.values())


def get_pack_matrix(sb, card_names: list[str], pack_code: str = "") -> dict:
    """card_names（パックの収録カード・正式名・並び順どおり）の店舗×レアリティ表を返す。

    pack_code（パックの型番の頭）を渡すと、新弾用の小さな表（price_history_pack）からこのパックの版の価格も読み、
    "pack_rows" に入れて返す（2026-11-01）。本表は同じレアリティで一番安い版しか残らず、再録カードのこのパックの版が
    表から消えるため。画面（static/packs.js）は本表の行と合わせて品の並びにし、版ごとに分けて出す。
    Returns: {"updated_at": "YYYY-MM-DD", "shops": [...], "rows": [...], "pack_rows": [...]}（rows は build_matrix の形）
    例外: 価格データの取得失敗は送出する（ルートが503にする）。失敗は _FAIL_TTL_SEC 秒覚える。版ごとの価格の失敗は送出しない。
    """
    seen: set[str] = set()
    order: list[str] = []
    for name in card_names:
        if name and name not in seen:
            seen.add(name)
            order.append(name)
    key = _cache_key(order, pack_code)
    now = time.time()
    today_d = datetime.now(JST).date()
    with _cache_lock:
        hit = _cache.get(key)
    if hit and "error" in hit and now - hit["ts"] < _FAIL_TTL_SEC:
        raise RuntimeError(hit["error"])
    if hit and "records" in hit and now - hit["ts"] < _CACHE_TTL_SEC:
        records = hit["records"]
        pack_records = hit.get("pack_records", [])
    else:
        pack_records = []
        days = [(today_d - timedelta(days=k)).isoformat() for k in range(_LOOKBACK_DAYS)]
        try:
            ids = _fetch_card_ids(sb, order) if order else {}
            rows = _fetch_price_rows(sb, sorted(ids), days) if ids else []
            for r in rows:  # 行のカード名は呼び出し元の名前にそろえる（build_matrix は名前で並べる）
                if r.get("card_id") is not None and int(r["card_id"]) in ids:
                    r["card_name"] = ids[int(r["card_id"])]
            records = _prepare_records(rows)
            if pack_code:
                pack_records = _fetch_pack_rows(sb, pack_code, sorted(ids), days)
                for r in pack_records:
                    if r.get("card_id") is not None and int(r["card_id"]) in ids:
                        r["card_name"] = ids[int(r["card_id"])]
        except Exception as e:
            with _cache_lock:
                _cache[key] = {"error": str(e), "ts": time.time()}
            raise
        with _cache_lock:
            if len(_cache) >= _CACHE_MAX_ENTRIES:
                # 古いものから捨てる（件数の上限を守る）
                for k, _v in sorted(_cache.items(), key=lambda kv: kv[1]["ts"])[:len(_cache) - _CACHE_MAX_ENTRIES + 1]:
                    _cache.pop(k, None)
            _cache[key] = {"records": records, "pack_records": pack_records, "ts": time.time()}
        logger.info(f"[featured_matrix] 記録を読込: {len(order)}種 {len(records)}件・版ごと {len(pack_records)}件")
    # 並び順は呼び出しごとに当てる（組み立ては記録の件数ぶんだけなので軽い）
    out = build_matrix(records, order, today_d.isoformat())
    out["pack_rows"] = pack_cells(pack_records)
    return out
