"""トレコロCB販売 — 在庫ありの一覧を系列ごとに読む夜間収集（2026-10-31）
=====================================================================

背景:
  毎晩の価格収集はトレコロを1枚ずつカード名で検索しており、1晩約3,900回（2026-10-30 は3,930回）。
  トレコロは 2026-10-11 に負荷で応答しなくなり、その後 WAF で止められた経緯がある（UA は偽らず名乗る方針）。
  トレコロの検索は「キーワード無し・遊戯王（ct2=1010）・系列（ct3）・在庫あり（filtercode13=1）・商品コード順（seq=g）」
  で一覧を並べられる。検索結果は1回1万件で打ち切られるが、在庫ありは全体で約2.5万件あるので、店の系列（41種・
  いちばん大きい RARITY COLLECTION でも約3,900件）ごとに読む。カードラボのカタログ巡回（clabo_catalog.py）と同じ形で、
  名寄せも clabo_catalog の二段名寄せをそのまま使う。

2026-10-31 の実測:
  41系列で530ページ・約18分（ページの間1.2秒）・在庫あり25,337件（キズあり4,673件・ノーマル5,772件を含む）。
  40枚をカード名で検索して出た在庫ありの商品325件が、すべて同じ URL・同じ値段で巡回にもあった。
  型番の頭で探す方式（pack_search.py）は、トレコロのノーマルがカードパスワードごとの共通の商品で型番の頭の検索に
  ほとんど出ないので使えない（白紙の監査で確認）。この方式は型番にも収録情報（ygores）にも頼らない。

在庫あり（filtercode13）だけを読むので売り切れの商品は出ないが、売り切れは build_min_price_rows でも除外される
（カード名の検索と同じ行になる）。

成立条件（clabo_catalog と同じ考え方）: 全系列で取得の失敗が無く、どの系列もページの上限に届かず、商品が1件以上。
不成立なら呼び出し側（collect_prices.py）はトレコロをカード名の検索に戻す。
"""
from __future__ import annotations

from functools import partial

import scraper
from clabo_catalog import build_match_index, match_products

TORECOLO_BASE = scraper.TORECOLO_BASE

# 2026-10-31 に検索ページの系列のセレクト（name="ct3"）から写した41種。
# 「ノーマルカード」（1010EU）は在庫ありが0件だったが、商品が入ることもありうるので読む
TORECOLO_SERIES = {
    "1010BK": "第１３期", "1010BL": "第１２期", "1010BM": "第１１期", "1010BN": "第１０期", "1010BO": "第９期",
    "1010BP": "第８期", "1010BQ": "第７期", "1010BR": "第６期", "1010BS": "第５期", "1010BT": "第４期",
    "1010BU": "第３期", "1010BV": "第２期", "1010BW": "第１期", "1010CJ": "REVOLUTION BOOSTER",
    "1010CK": "デッキビルドパック", "1010CL": "RARITY COLLECTIONシリーズ", "1010CM": "ブースターSP",
    "1010CN": "コレクターズパック系（CP・AC・HC）", "1010CO": "ゴールドシリーズ", "1010CP": "デュエリストパック",
    "1010CQ": "プレミアムパック", "1010CR": "エクストラパック・ワールドプレミアパック", "1010CS": "復刻パック",
    "1010CT": "BOOSTER", "1010CU": "デュエルターミナル", "1010CV": "アジア版", "1010CW": "パック(その他)",
    "1010DS": "ストラクチャーデッキ（第１１期以降）", "1010DT": "ストラクチャーデッキ（第１０期以前）",
    "1010DU": "TACTICAL-TRY DECK", "1010DV": "STARTER DECK", "1010DW": "デッキ(その他)", "1010EA": "特殊セット",
    "1010EM": "応募者全員サービス限定パック", "1010EO": "プロモーションパック", "1010ER": "トーナメントパック",
    "1010ES": "書籍等付属", "1010ET": "プロモカード", "1010EU": "ノーマルカード", "1010EV": "優良ノーマル",
    "1010EW": "そのほか",
}

PAGE_SIZE = 50                 # 1ページの件数（ps を変えても50件。2026-10-31 監査で確認）
CATALOG_MAX_PAGES = 200        # 検索は1万件（200ページ）で打ち切られる。届いたらその系列は読み切れていない
CATALOG_PAGE_WAIT = 1.2        # ページの間の待ち（秒）。カード名の検索（0.5秒）より長くして店への負担をならす
# 系列の総件数の表示（1ページ目の「N 件あります」）に対し、読んだ商品の数がこれだけ足りなければ読み切れていないとみなす。
# 読んでいる間（系列あたり最大約3分）の売り切れでも少し減るので、ぴったりは求めない
MISSING_TOLERANCE = 3          # TODO: calibrate from data（shadow の晩の per_series の total と listed の差を見る）
SERIES_URL_QUERY = "&ct3={sr}&filtercode13=1&seq=g"  # 系列・在庫あり・商品コード順


def _series_url(sr: str) -> str:
    return (f"{TORECOLO_BASE}/shop/goods/search.aspx?search=x&keyword=&category=&oshiire_code="
            f"&ct2={scraper._TORECOLO_YGO_CT2}" + SERIES_URL_QUERY.format(sr=sr))


def harvest_series() -> dict[str, str] | None:
    """検索ページの系列のセレクトを読む（系列の増減を見張るため）。読めなければ None。"""
    soup = scraper.safe_get(_series_url(next(iter(TORECOLO_SERIES))))
    if soup is None:
        return None
    sel = soup.select_one('select[name="ct3"]')
    if sel is None:
        return None
    found = {o.get("value", ""): o.get_text(strip=True) for o in sel.select("option") if o.get("value")}
    return found or None


def check_series_drift(harvested: dict[str, str] | None) -> list[str] | None:
    """既知の41種と比べた増減の警告。読めなかったときは None。"""
    if harvested is None:
        return None
    warnings = [f"新しい系列: {v}（{k}）" for k, v in harvested.items() if k not in TORECOLO_SERIES]
    warnings += [f"消えた系列: {v}（{k}）" for k, v in TORECOLO_SERIES.items() if k not in harvested]
    return warnings


def crawl_catalog(series: dict[str, str] | None = None) -> tuple[list[dict], dict]:
    """系列ごとに在庫ありの一覧を最後のページまで読み、全商品を返す（URL で重複を除く）。

    系列ごとに、1ページ目の総件数の表示と、絞り込み前に並んでいた商品の数を突き合わせる。表示が読めない・
    MISSING_TOLERANCE より多く足りない系列は読み切れていない（途中のページが空で返った・ページ送りの形が変わった
    などを黙って成立にしない。reviewer 高2）。1つの系列が失敗したら、その晩の巡回をやめる（店が弱っているときに
    読み続けない。reviewer 中4）。"""
    series = series or TORECOLO_SERIES
    products: dict[str, dict] = {}
    stats = {"series": len(series), "series_failed": [], "series_truncated": [], "series_short": [],
             "per_series": {}, "stopped_early": False}
    for sr, label in series.items():
        meta: dict = {}
        fn = partial(scraper.scrape_torecolo, max_pages=CATALOG_MAX_PAGES,
                     extra_query=SERIES_URL_QUERY.format(sr=sr), page_wait=CATALOG_PAGE_WAIT, meta_out=meta)
        try:
            with scraper.name_filter_off():
                items, fetch_errors = scraper.run_shop_with_status(fn, "")
        except Exception as e:
            print(f"  トレコロ 系列 {label}: 失敗 {e}")
            fetch_errors, items = 1, []
        total, listed, pages = meta.get("total"), meta.get("listed", 0), meta.get("pages", 0)
        stats["per_series"][sr] = {"total": total, "listed": listed, "items": len(items), "pages": pages}
        if fetch_errors:
            print(f"  トレコロ 系列 {label}: 取得の失敗 {fetch_errors}件。今夜の巡回をやめます")
            stats["series_failed"].append(sr)
            stats["stopped_early"] = True
            break
        if pages >= CATALOG_MAX_PAGES:
            print(f"  トレコロ 系列 {label}: {pages}ページで検索の上限に届いた（読み切れていない）")
            stats["series_truncated"].append(sr)
            continue
        if total is None or listed + MISSING_TOLERANCE < total:
            print(f"  トレコロ 系列 {label}: 総件数 {total} に対し {listed}件しか読めなかった（読み切れていない）")
            stats["series_short"].append(sr)
            continue
        for it in items:
            if it.get("url"):
                products.setdefault(it["url"], it)
    stats["products"] = len(products)
    return list(products.values()), stats


def collect_torecolo_catalog(card_names: list[str]) -> tuple[dict[str, list[dict]] | None, dict]:
    """系列の見張り → 巡回 → 二段名寄せ。成立しなければ (None, 統計)。

    新しい系列が出たら不成立にする（その系列の商品を読まないまま「在庫なし」にしないため。reviewer 高3）。
    見張りが読めないときは既知の41系列で続ける（巡回の側の総件数の突き合わせで読み漏れは見張る）。"""
    drift = check_series_drift(harvest_series())
    if drift is None:
        print("  トレコロ: 系列の見張りが読めませんでした。既知の41系列で巡回を続けます")
    else:
        for w in drift:
            print(f"  トレコロ 系列の増減の警告: {w}")
    if drift and any(w.startswith("新しい系列") for w in drift):
        print("  トレコロ: 新しい系列があるため、今夜の巡回は不成立にします（TORECOLO_SERIES に足す）")
        return None, {"series_drift": drift, "series_failed": [], "series_truncated": [], "series_short": [],
                      "products": 0, "matched_products": 0}
    products, crawl_stats = crawl_catalog()
    matched, match_stats = match_products(products, build_match_index(card_names))
    stats = {**crawl_stats, **match_stats, "series_drift": drift}
    ok = (not crawl_stats["series_failed"] and not crawl_stats["series_truncated"]
          and not crawl_stats["series_short"] and crawl_stats["products"] > 0)
    return (matched if ok else None), stats
