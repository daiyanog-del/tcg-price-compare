"""カーナベル販売 — 販売中の商品を全件まとめて読む夜間収集（2026-11-01）
=====================================================================

背景:
  毎朝の価格収集はカーナベルを1枚ずつカード名で検索しており、1日約3,400回。2026-11-01 にまんぞく屋が
  本番の接続元を丸ごと止めたため、ほかの店も取得量を減らす調査をした（TASKS.md「ほかの店も同じ遮断のリスク」）。
  カーナベルの遊戯王は Elasticsearch（検索用の API）を直接引く作りで、キーワード条件を外せば
  公開期間内の販売中の商品を `inventory_product_id` 順に `search_after` でページ送りして全件読める。
  並びが一意の商品IDなので、読んでいる間に商品が売れても、後ろの商品が前のページへずれて読み落とすことは無い
  （トレコロの一覧とは違う）。

2026-11-01 の実測（自宅回線）:
  対象 37,476商品（うち在庫あり 35,466）。`size` は1000も2000も通り、2000件で 2.7MB・0.4秒。
  1日約3,400回 → 全件で 38ページ（size=1000）・187,340行（状態S〜D）・行はメモリ約86MB。
  30枚をカード名で検索した行と (URL・状態・値段・在庫) で比べて29枚が完全一致。残る1枚
  （閃刀姫－アザレア・テンペランス）は、検索がダッシュ「―」表記の5商品を取りこぼしていた（巡回が正しい）。

読み方は scrape_kanabell と同じ（`scraper.kanabell_rows_from_source` を共有）で、名寄せは clabo_catalog の
二段名寄せ（match_products）を使う。カーナベルは商品名のダッシュ・長音の表記がカードごとに不統一なので、
名寄せの前に双方を `_kanabell_canon_dash` で半角ハイフンへそろえる（検索方式の is_target_card 呼び出しと同じ）。

成立条件: 取得の失敗（ES の一部の区画の失敗・時間切れを含む）が無く、読んだ商品の数が ES の総件数に対して
MISSING_TOLERANCE 以内で、かつ MIN_PRODUCTS 以上。不成立なら呼び出し側（collect_prices.py）はカーナベルを
カード名の検索に戻す。
"""
from __future__ import annotations

import time

import requests

import scraper
from clabo_catalog import build_match_index, match_products

PAGE_SIZE = 1000               # 1ページの件数（2000まで通ることは実測。店への負担を見て控えめ）
CATALOG_MAX_PAGES = 100        # 暴走止め。通常は38ページ。届いたら読み切れていない扱い
CATALOG_PAGE_WAIT = 1.5        # ページの間の待ち（秒）
PAGE_TIMEOUT = 30              # 1ページの待ち（秒）。2000件でも0.4秒なので十分
CATALOG_TIME_BUDGET = 600      # 巡回全体の上限（秒）。遅いだけの晩に収集の5時間を食わない
# ES の総件数に対し、読んだ商品の数がこれだけ足りなければ読み切れていないとみなす。
# 読んでいる間（約1分）の公開終了・売止めでも少し減るので、ぴったりは求めない
MISSING_TOLERANCE = 20         # TODO: calibrate from data（shadow の晩の total と listed の差を見る）
# 総件数そのものが急に減った（検索条件の項目名が変わって母集団が縮んだ等）ときに成立にしない下限。
# 2026-11-01 は 37,476商品
MIN_PRODUCTS = 30000           # TODO: calibrate from data（shadow の晩の listed の推移を見る）

# 巡回全体の時間を測る時計（テストでこれだけ差し替える。time.monotonic をプロセスごと差し替えないため）
_now = time.monotonic


def _base_query() -> dict:
    """公開期間内で、購入上限が0でない商品（scrape_kanabell の検索と同じ母集団）。"""
    return {"bool": {"filter": [scraper._kanabell_published()],
                     "must_not": [{"term": {"selling_num_limit": 0}}]}}


def _fetch_page(search_after: list | None, timeout: int = PAGE_TIMEOUT) -> tuple[list[dict], int | None]:
    """1ページ読む。戻り値は (ヒット [{_source, sort}, ...], 総件数)。

    失敗は requests.RequestException か ValueError。ES は一部の区画（shard）が失敗・時間切れでも 200 で
    一部の結果を返し、総件数もその分しか数えないので、それも ValueError（取得失敗）にする（reviewer 中1）。"""
    body = {
        "size": PAGE_SIZE, "track_total_hits": True,
        "_source": scraper._KANABELL_SELL_SOURCE,
        "sort": [{"inventory_product_id": "asc"}],
        "query": _base_query(),
    }
    if search_after:
        body["search_after"] = search_after
    scraper.count_request(scraper._KANABELL_ES_ENDPOINT)  # safe_get を通らないのでここで数える
    res = requests.post(
        f"{scraper._KANABELL_ES_ENDPOINT}/{scraper._KANABELL_INDEX}/_search",
        json=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"ApiKey {scraper._KANABELL_ES_API_KEY}"},
        timeout=timeout,
    )
    res.raise_for_status()
    data = res.json()
    if data.get("timed_out"):
        raise ValueError("ES の応答が時間切れ（timed_out）")
    shards = data.get("_shards") or {}
    if (shards.get("failed") or 0) > 0:
        raise ValueError(f"ES の一部の区画が失敗（_shards.failed={shards.get('failed')}）")
    hits = data.get("hits", {})
    total = hits.get("total")
    total = total.get("value") if isinstance(total, dict) else total
    return hits.get("hits", []), total


def crawl_catalog(page_wait: float = CATALOG_PAGE_WAIT) -> tuple[list[dict], dict]:
    """販売中の全商品を最後のページまで読み、状態ごとの価格行にして返す（商品の URL と状態で重複を除く）。

    1ページでも失敗したら、その晩の巡回をやめる（店が弱っているときに読み続けない）。"""
    rows_by_key: dict[tuple[str, str], dict] = {}
    stats = {"total": None, "listed": 0, "pages": 0, "failed": False, "truncated": False, "short": False}
    after = None
    seen_ids: set[str] = set()
    started = _now()
    while True:
        if stats["pages"] >= CATALOG_MAX_PAGES or _now() - started > CATALOG_TIME_BUDGET:
            print(f"  カーナベル 全件巡回: {stats['pages']}ページで上限に届いた（読み切れていない）")
            stats["truncated"] = True
            break
        try:
            hits, total = _fetch_page(after)
        except (requests.RequestException, ValueError) as e:
            print(f"  カーナベル 全件巡回: {stats['pages'] + 1}ページ目の取得に失敗 {e}。今夜の巡回をやめます")
            stats["failed"] = True
            break
        stats["pages"] += 1
        if stats["total"] is None:
            stats["total"] = total
        if not hits:
            break
        for h in hits:
            src = h.get("_source") or {}
            pid = src.get("inventory_product_id")
            if pid:
                seen_ids.add(pid)
            for row in scraper.kanabell_rows_from_source(src):
                rows_by_key.setdefault((row["url"], row["condition"]), row)
        after = hits[-1].get("sort")
        if not after or len(hits) < PAGE_SIZE:
            break
        time.sleep(page_wait)
    stats["listed"] = len(seen_ids)
    stats["rows"] = len(rows_by_key)
    total = stats["total"]
    if not stats["failed"] and not stats["truncated"]:
        if total is None or stats["listed"] + MISSING_TOLERANCE < total:
            print(f"  カーナベル 全件巡回: 総件数 {total} に対し {stats['listed']}商品しか読めなかった（読み切れていない）")
            stats["short"] = True
        elif stats["listed"] < MIN_PRODUCTS:
            print(f"  カーナベル 全件巡回: 読めたのが {stats['listed']}商品で下限 {MIN_PRODUCTS} に届かない"
                  "（母集団が縮んだ疑い）")
            stats["short"] = True
    return list(rows_by_key.values()), stats


def match_rows(rows: list[dict], card_names: list[str]) -> tuple[dict[str, list[dict]], dict]:
    """行を監視カードへ名寄せする。ダッシュ類は双方を半角ハイフンへそろえて比べる（検索方式と同じ）。

    1商品は状態S〜Dで最大5行あり、同じ商品名の商品も多いので、名寄せはそろえた商品名ごとに1回だけ行い、
    結果を行へ広げる（行ごとだと18.7万行×約3,400パターンで本番の5時間を数分食う。reviewer 中4）。
    統計の matched_products / unmatched_products は商品（URL）の数。"""
    canon_to_names: dict[str, list[str]] = {}
    for n in card_names:
        canon_to_names.setdefault(scraper._kanabell_canon_dash(n), []).append(n)
    rows_by_name: dict[str, list[dict]] = {}
    for r in rows:
        rows_by_name.setdefault(scraper._kanabell_canon_dash(r["name"]), []).append(r)
    matched_canon, name_stats = match_products([{"name": n} for n in rows_by_name],
                                               build_match_index(list(canon_to_names)))
    matched: dict[str, list[dict]] = {}
    matched_urls: set[str] = set()
    for canon_card, products in matched_canon.items():
        card_rows = [r for p in products for r in rows_by_name[p["name"]]]
        matched_urls.update(r["url"] for r in card_rows)
        for name in canon_to_names[canon_card]:
            matched.setdefault(name, []).extend(card_rows)
    all_urls = {r["url"] for r in rows}
    stats = {"matched_products": len(matched_urls), "unmatched_products": len(all_urls - matched_urls),
             "matched_names": name_stats["matched_products"],
             "multi_match_names": name_stats["multi_match_products"]}
    return matched, stats


def collect_kanabell_catalog(card_names: list[str]) -> tuple[dict[str, list[dict]] | None, dict]:
    """巡回 → 名寄せ。成立しなければ (None, 統計)。"""
    if not scraper.kanabell_configured():
        print("  ⚠️  カーナベル: KANABELL_ES_ENDPOINT / KANABELL_ES_API_KEY が未設定です")
        return None, {"configured": False}
    rows, crawl_stats = crawl_catalog()
    ok = (not crawl_stats["failed"] and not crawl_stats["truncated"] and not crawl_stats["short"]
          and crawl_stats["rows"] > 0)
    if not ok:
        return None, crawl_stats
    matched, match_stats = match_rows(rows, card_names)
    return matched, {**crawl_stats, **match_stats}
