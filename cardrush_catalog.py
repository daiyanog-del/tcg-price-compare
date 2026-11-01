"""カードラッシュ販売 — 型番の頭の検索でまとめて読む夜間収集の試し読み（2026-11-01）
=====================================================================

背景:
  毎朝の価格収集はカードラッシュを1枚ずつカード名で検索しており、1日約3,400回。2026-11-01 にまんぞく屋が
  本番の接続元を丸ごと止めたため、ほかの店も取得量を減らす調査をした（TASKS.md「ほかの店も同じ遮断のリスク」の②）。
  カードラッシュはお店のサーバー（Apache）に直接つながる作りで、まんぞく屋に一番近い。

一覧をまとめて読む道の実測（2026-11-01・自宅回線）:
  - 全商品の一覧（キーワード無しの product-list）・種類別の一覧（/product-list/<番号>）・sitemap.xml は重い。
    30秒で時間切れ・100秒待って接続を切られる。使わない
  - パック別（/product-group/<番号>）は番号の8割が404で、あるのは新しいパック・テーマ・特集だけ。全カードは覆えない
  - 検索欄に型番の頭（例 "IMPH"）を入れる検索は、新弾の収集（pack_search.py）でも使っている。商品名に {IMPH-JP001}
    が入っているので、そのパックの商品がまとめて並ぶ。並びは在庫あり → 在庫なしなので、在庫なしが出たページで止めれば
    在庫ありは読み切っている（保存は売り切れの行を捨てるので、売り切れ側は読まなくてよい）。
    10種で試すと、ほとんどの頭は1ページ、大きなパックの QCCU が7ページ・RC04 が6ページ。頭は約670種なので、
    全部読むと1日約800〜900回（今の約3,400回より少ない）
  - ただし初めて出す検索語は重い: 型番の頭の1回目は6〜24秒（30秒で時間切れも）、同じ語の2回目は0.1〜6秒
    （お店の側に検索結果の控えがある）。カード名の検索は、最近出していない語でも0.1〜0.8秒。
    控えが1晩もたないと、毎朝約670回の重い検索になり、お店の処理の時間はかえって増える

そこで、まずは試し読み（probe）:
  毎晩、同じ PROBE_HEADS 種の頭だけを読み、ページごとの応答時間を collection_runs.shop_stats["カードラッシュ"]
  ["catalog"] に残す。2晩目以降に速くなる（控えが1晩もつ）と分かったら、全頭に広げ（CARDRUSH_CATALOG_HEADS=all）、
  shadow の比べ（collect_prices._compare_catalog_shadow）で取りこぼしを見てから on にする。
  試し読みの晩は名寄せ・保存・比べをしない（全頭を読んでいないので、カードの最安が決まらない）。

ページの境目のずれ:
  並びの先頭が在庫ありなので、読んでいる途中に前のページの商品が売り切れると、次のページの先頭の商品を1つ読み落とす。
  1つの頭は数ページで数秒なので、ずれる機会は少ない。入荷で後ろへずれた重複は URL で除き duplicates に数える。

お店への気づかい:
  ページの間は CATALOG_PAGE_WAIT 秒以上空け、遅い応答のあとは、その応答にかかった時間だけ余計に待つ。
  取得に失敗したら、その晩の巡回をやめる。全体の時間の上限も置く。
"""
from __future__ import annotations

import json
import os
import re
import time
import zlib

import requests

import scraper
from clabo_catalog import build_match_index, match_products

CARDRUSH_SEARCH_URL = "https://www.cardrush.jp/product-list"
HEADS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cardrush_code_heads.json")

# 試し読みで毎晩読む頭の数（毎晩同じ頭。2晩目以降に速くなるかを見る）
PROBE_HEADS = 100              # TODO: calibrate from data（試し読みの晩の page_sec を見て広げる）
CATALOG_PAGE_WAIT = 1.5        # ページの間の最低の待ち（秒）
PAGE_TIMEOUT = 30              # 1ページの待ち（秒）
HEAD_MAX_PAGES = 20            # 1つの頭で読むページの上限。QCCU（在庫あり697件）で7ページ。届いたら読み切れていない
CATALOG_TIME_BUDGET = 1500     # 巡回全体の上限（秒）。TODO: calibrate from data（試し読みの晩の所要時間を見る）
SLOW_PAGE_SEC = 10             # これより遅いページを slow_pages に数える（控えの無い重い検索の目安）
# 全頭を読んだ晩に成立とする在庫ありの商品の下限（2026-11-01 の推定で在庫ありは約2.8万件）
MIN_IN_STOCK = 20000           # TODO: calibrate from data（全頭を読んだ晩の listed を見る）
ORDER_BREAK_TOLERANCE = 3      # 在庫なしの後ろに在庫ありが来た回数の許し。TODO: calibrate from data

# 巡回全体の時間を測る時計（テストでこれだけ差し替える）
_now = time.monotonic


def load_heads() -> list[str]:
    with open(HEADS_PATH, encoding="utf-8") as f:
        return list(json.load(f)["heads"])


def probe_heads(heads: list[str], n: int) -> list[str]:
    """試し読みの頭。毎晩同じになるよう crc32 の順に並べて先頭から n 種（パックの新旧・大小が混ざる）。"""
    return sorted(heads, key=lambda h: zlib.crc32(h.encode("ascii")))[:n]


def _search_url(head: str, page: int) -> str:
    url = f"{CARDRUSH_SEARCH_URL}?keyword={requests.utils.quote(head)}"
    return url if page == 1 else f"{url}&page={page}"


def _fetch_page(head: str, page: int):
    """1ページ読む。読めなければ None（scraper.safe_get が 403 などを数えてホストを一時停止する）。"""
    return scraper.safe_get(_search_url(head, page), timeout=PAGE_TIMEOUT)


def crawl_heads(heads: list[str], page_wait: float = CATALOG_PAGE_WAIT) -> tuple[dict[str, list[dict]], dict]:
    """型番の頭ごとに在庫ありの範囲を読み、元の商品名 → 行（在庫ありだけ）を返す（商品の URL で重複を除く）。

    元の商品名ごとにまとめるのは、名寄せ（is_target_card）を元の商品名で商品名ごとに1回だけ行うため。
    1ページでも失敗したら、その晩の巡回をやめる（店が弱っているときに読み続けない）。"""
    rows_by_name: dict[str, list[dict]] = {}
    seen_urls: set[str] = set()
    stats = {"heads": len(heads), "heads_read": 0, "pages": 0, "listed": 0, "duplicates": 0, "order_breaks": 0,
             "skipped_items": 0, "slow_pages": 0, "page_sec_sum": 0.0, "page_sec_max": 0.0,
             "failed": False, "time_truncated": False, "truncated_heads": [], "per_head": []}
    started = _now()
    for head in heads:
        head_stat = {"head": head, "pages": 0, "in_stock": 0, "first_sec": None}
        for page in range(1, HEAD_MAX_PAGES + 2):
            if _now() - started > CATALOG_TIME_BUDGET:
                stats["time_truncated"] = True
                break
            if page > HEAD_MAX_PAGES:
                print(f"  カードラッシュ 型番の頭 {head}: {HEAD_MAX_PAGES}ページで上限に届いた（読み切れていない）")
                stats["truncated_heads"].append(head)
                break
            t = _now()
            soup = _fetch_page(head, page)
            sec = round(_now() - t, 2)
            if soup is None:
                print(f"  カードラッシュ 型番の頭 {head}: {page}ページ目を読めなかった。今夜の巡回をやめます")
                stats["failed"] = True
                break
            stats["pages"] += 1
            head_stat["pages"] += 1
            if head_stat["first_sec"] is None:
                head_stat["first_sec"] = sec
            stats["page_sec_sum"] = round(stats["page_sec_sum"] + sec, 2)
            stats["page_sec_max"] = max(stats["page_sec_max"], sec)
            if sec >= SLOW_PAGE_SEC:
                stats["slow_pages"] += 1
            items = soup.select("li[class*='list_item_cell']")
            seen_sold_out = False
            new_on_page = 0
            for item in items:
                parsed = scraper.cardrush_row_from_item(item)
                if parsed is None:
                    # 【-】の封入商品・グッズ・値段の無い商品
                    stats["skipped_items"] += 1
                    continue
                raw_name, row = parsed
                if row["sold_out"]:
                    # 在庫の欄が無い商品（【当店初利用の方限定特価品】のまとめ売り）も在庫0＝売り切れの行になる
                    # （検索と同じ。保存では捨てられる）。在庫なしの範囲に入った印には在庫の欄がある商品だけを使う
                    if item.select_one("p.stock") is not None:
                        seen_sold_out = True
                    else:
                        stats["skipped_items"] += 1
                    continue
                if seen_sold_out:
                    stats["order_breaks"] += 1
                url = row["url"]
                if url in seen_urls:
                    stats["duplicates"] += 1
                    continue
                if url:
                    seen_urls.add(url)
                new_on_page += 1
                head_stat["in_stock"] += 1
                rows_by_name.setdefault(raw_name, []).append(row)
            # 遅い応答のあとは、その分だけ余計に待つ（お店が混んでいるときに続けて重い検索を送らない）
            time.sleep(max(page_wait, sec))
            if seen_sold_out or len(items) < scraper.CARDRUSH_PAGE_SIZE:
                break
            if page > 1 and new_on_page == 0 and not seen_sold_out:
                # 存在しないページは1ページ目が返る（scrape_cardrush と同じ）。新しい商品が無ければ読み終えている
                break
        stats["per_head"].append(head_stat)
        if stats["failed"] or stats["time_truncated"]:
            break
        stats["heads_read"] += 1
    if stats["time_truncated"]:
        print(f"  カードラッシュ 型番の頭の巡回: {stats['heads_read']}/{len(heads)}種で時間の上限に届いた")
    stats["listed"] = len(seen_urls)
    stats["rows"] = sum(len(v) for v in rows_by_name.values())
    return rows_by_name, stats


def match_rows(rows_by_name: dict[str, list[dict]], card_names: list[str]) -> tuple[dict[str, list[dict]], dict]:
    """行を監視カードへ名寄せする。カード名の検索と同じく、元の商品名で is_target_card を通す
    （clabo_catalog の二段名寄せ。粗い候補 → is_target_card）。

    検索と違い、お店の側のカード名での絞り込みが無いので、照合は is_target_card だけで決まる。
    統計の matched_products / unmatched_products は商品（URL）の数。"""
    matched_names, name_stats = match_products([{"name": n} for n in rows_by_name],
                                               build_match_index(card_names))
    matched: dict[str, list[dict]] = {}
    matched_urls: set[str] = set()
    for card, products in matched_names.items():
        card_rows = [r for p in products for r in rows_by_name[p["name"]]]
        matched_urls.update(r["url"] for r in card_rows)
        matched[card] = card_rows
    all_urls = {r["url"] for rows in rows_by_name.values() for r in rows}
    stats = {"matched_products": len(matched_urls), "unmatched_products": len(all_urls - matched_urls),
             "matched_names": name_stats["matched_products"],
             "multi_match_names": name_stats["multi_match_products"]}
    return matched, stats


def collect_cardrush_catalog(card_names: list[str], heads_limit: int | None = PROBE_HEADS
                             ) -> tuple[dict[str, list[dict]] | None, dict]:
    """型番の頭の巡回 → 名寄せ。成立しなければ (None, 統計)。

    heads_limit が全頭より少ない晩は試し読み（統計に probe=True）。名寄せせず、常に (None, 統計) を返す。"""
    heads = load_heads()
    probe = heads_limit is not None and heads_limit < len(heads)
    if probe:
        heads = probe_heads(heads, heads_limit)
    rows_by_name, stats = crawl_heads(heads)
    stats["probe"] = probe
    if probe:
        return None, stats
    # 全頭（約670種）の晩は、頭ごとの記録を遅かった頭だけに絞る（collection_runs の JSON を大きくしない）
    stats["per_head"] = [h for h in stats["per_head"] if (h["first_sec"] or 0) >= SLOW_PAGE_SEC]
    short = stats["listed"] < MIN_IN_STOCK or stats["order_breaks"] > ORDER_BREAK_TOLERANCE
    if short:
        print(f"  カードラッシュ 型番の頭の巡回: 在庫あり{stats['listed']}商品・並びの崩れ{stats['order_breaks']}件"
              "（読み切れていない疑い）")
    stats["short"] = short
    ok = (not stats["failed"] and not stats["time_truncated"] and not stats["truncated_heads"] and not short
          and stats["rows"] > 0)
    if not ok:
        return None, stats
    matched, match_stats = match_rows(rows_by_name, card_names)
    return matched, {**stats, **match_stats}
