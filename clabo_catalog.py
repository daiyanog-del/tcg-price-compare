"""カードラボ販売 — カタログ巡回による夜間収集（P1）
==================================================

背景（docs/audit-403-plan-2026-08-08.md・docs/decisions.md 2026-08-07 F10）:
  夜間収集は監視カード約2,700枚を1枚ずつ「キーワード検索」しており、カードラボは
  毎晩約1,000リクエスト強で403遮断され約6割が欠測する。対策として、カードラボ
  だけ「カード単位の検索」をやめ、遊戯王カテゴリ14個のカタログページ（120件/ページ・
  計約176リクエスト）を巡回して全商品を取得し、ローカルで監視カードに名寄せする。

  ライブ検索（app.py /api/search → scraper.scrape_clabo）はこのモジュールの対象外。
  従来どおりカード単位の検索方式のまま残る。
"""
from __future__ import annotations

import re
import time

import requests

import scraper

CLABO_BASE = scraper.CLABO_BASE

# 2026-08-08 監査実測の遊戯王OCGカテゴリ14個（白紙監査H4）。
# 739=キズ特価品・733=トークン・1637=限定プロモ・2029=その他 は現行の検索方式でも
# 商品集合に含まれているため、parity維持のため含める（除外の変更は別デプロイの判断）。
CLABO_CATEGORIES = {
    "679": "通常", "676": "効果", "673": "融合", "674": "儀式",
    "672": "シンクロ", "671": "エクシーズ", "675": "ペンデュラム",
    "1219": "リンク", "677": "魔法", "678": "罠",
    "739": "キズ特価品", "733": "トークン", "1637": "限定プロモ", "2029": "その他",
}

# カテゴリごとの安全上限（無限ループ防止）。実測最大は676=約64ページ（7,675件/120件）で
# 十分な余裕を持たせる
CLABO_CATALOG_MAX_PAGES = 90

# カテゴリ差分監視: 全商品名に【遊戯】タグが入るため keyword=遊戯 1回で
# 遊戯王OCGの全カテゴリがmain_categoryセレクトに列挙される（白紙監査H4）
CLABO_CATEGORY_SEARCH_URL = (
    f"{CLABO_BASE}/product-list?keyword={requests.utils.quote('遊戯')}"
    f"&num={scraper.CLABO_PAGE_SIZE}"
)
# セレクトのラベルは「遊戯王OCG:通常」のような形式。「遊戯王ラッシュデュエル」は
# 別ゲーム（このモジュールの対象外）のため除外する
_CATEGORY_LABEL_PREFIX = "遊戯王OCG:"


def _catalog_page_url(cid: str, page: int) -> str:
    url = f"{CLABO_BASE}/product-list/{cid}?num={scraper.CLABO_PAGE_SIZE}"
    if page > 1:
        url += f"&page={page}"
    return url


def harvest_categories() -> list[tuple[str, str]] | None:
    """検索結果ページ（keyword=遊戯）の main_category セレクトから
    遊戯王OCGカテゴリの (value, label) 一覧を収穫する。

    取得失敗時、またはセレクト自体が見つからない（構造破損）場合は None を返す
    （呼び出し側は警告のみ出し、既知14個で巡回を続行する）。セレクトが見つからない
    場合に空リストを返すと「全カテゴリが消滅した」という偽の差分警告14件が
    check_category_drift から出るため、区別して None にする（reviewer低3）。
    """
    soup = scraper.safe_get(CLABO_CATEGORY_SEARCH_URL, retries=2)
    if not soup:
        return None
    select = soup.select_one("select[name=main_category]")
    if not select:
        return None
    harvested = []
    for opt in select.select("option"):
        label = opt.get_text(strip=True)
        if not label.startswith(_CATEGORY_LABEL_PREFIX):
            continue
        value = opt.get("value", "")
        if not value:
            continue
        harvested.append((value, label))
    return harvested


def check_category_drift(harvested: list[tuple[str, str]] | None) -> list[str] | None:
    """収穫結果と CLABO_CATEGORIES を突合し、追加/消滅の警告文字列リストを返す。

    harvested が None（harvest_categories が取得失敗）の場合はそのまま None を返す。
    """
    if harvested is None:
        return None

    warnings: list[str] = []
    harvested_ids = {cid for cid, _ in harvested}
    known_ids = set(CLABO_CATEGORIES.keys())

    for cid, label in harvested:
        if cid not in known_ids:
            warnings.append(f"新しいカテゴリを検出: {cid} = {label}（CLABO_CATEGORIES未登録）")
    for cid, label in CLABO_CATEGORIES.items():
        if cid not in harvested_ids:
            warnings.append(f"既知カテゴリが消滅: {cid} = {label}（検索結果に現れず）")

    return warnings


# item_count とページ巡回で実際に見えたユニーク商品数との許容乖離。巡回中の在庫変動
# （売り切れ削除・新規出品）ぶんを吸収するための余裕。値は未校正の仮置き
# TODO: calibrate from data（実運用のitem_count vs seen件数の分布が溜まったら見直す）
_ITEM_COUNT_MIN_GAP = 6
_ITEM_COUNT_GAP_RATIO = 0.005

# 検索エンジンのヒット件数キャップとみられるセンチネル値（白紙監査 H4 注記）。
# この値が出た場合は item_count を「実数」として信頼せず、seen件数との突合を行わない
_ITEM_COUNT_CAP_SENTINEL = 10000


def crawl_catalog() -> tuple[list[dict], dict]:
    """カードラボ販売のカテゴリ14個を巡回し、遊戯王OCGの全商品を集める。

    reviewer監査（高1/高2/低1）により終端判定を再設計:
      - break条件は「120件未満（最終ページ）」「全商品既出（重複検出）」
        「カテゴリごとの安全上限 CLABO_CATALOG_MAX_PAGES」の3つのみ。
        item_count（.item_count）由来のページ数はbreak条件から外した
        （壊れると「1ページだけ取って成立扱い」になり検出できないため）
      - 範囲外ページを要求すると1ページ目の内容が返る仕様のため、ちょうど120件で
        終わるカテゴリは次ページが重複検出で+1リクエストになるだけ（最大14回/晩）
      - item_count は代わりに「検証専用」にする: カテゴリ巡回後に
        `.item_count の読み値` と `実際に見たユニーク商品数(len(seen_items))` を
        突合し、乖離が大きければそのカテゴリを categories_failed に入れる
      - MAX_PAGES に達してもまだ120件フルが続いていた場合は打ち切りの疑いとして
        categories_failed に入れ、警告を print する

    reviewer監査2巡目（中2/中3b）で検証ロジックを追加調整:
      - total==0（正常時は14カテゴリ全て2件以上あり得ない）は不成立にする
      - stats["products"]==0 は呼び出し側（collect_clabo_catalog）で全体不成立の
        保険条件として扱う
      - **total（item_count）が読めなくても、ページ巡回が「120件未満」または
        「重複検出」という別の信号で自然に終端していれば、不成立にはしない**
        （誤爆コストの見直し。フォールバックは検索方式2,700リクエストへの
        後退で安全側ではないため）。この場合は categories_stats に
        "verified": False を残し、stats["categories_unverified"] に積む。
        MAX_PAGES到達（打ち切りの疑い）の場合は従来どおり不成立のまま緩めない

    各商品は scraper._extract_clabo_item で抽出し、【遊戯】タグ判定
    （scraper._is_clabo_ygo）とラッシュデュエル除外（scraper._is_rush_duel）を
    ここで適用する（カード照合はまだ行わない。match_products の役割）。

    ページ取得失敗（safe_get が None。retries=2 で一時的な失敗はある程度吸収する）は
    そのカテゴリの残りページを打ち切り、categories_failed / failed_pages に計上する
    （取れた分は捨てない）。

    stats["categories"] にカテゴリ別の検証情報 {total, seen, pages, ok, verified} を残す。
    """
    all_products: list[dict] = []
    categories_failed: list[str] = []
    categories_unverified: list[str] = []
    pages_fetched = 0
    failed_pages = 0
    categories_stats: dict[str, dict] = {}

    for cid in CLABO_CATEGORIES:
        seen_items: set = set()
        total: int | None = None
        pages_used = 0
        category_ok = True
        verified = True
        page_fetch_failed = False
        # 終端理由。"under_page_size"/"duplicate"/"empty_page" が自然終端、
        # "max_pages" は安全上限到達（打ち切りの疑い）
        end_reason: str | None = None

        for page in range(1, CLABO_CATALOG_MAX_PAGES + 1):
            url = _catalog_page_url(cid, page)
            soup = scraper.safe_get(url, retries=2)
            if not soup:
                failed_pages += 1
                category_ok = False
                page_fetch_failed = True
                end_reason = "page_fail"
                break
            pages_fetched += 1
            pages_used = page

            if page == 1:
                total = scraper._clabo_total_count(soup)

            containers = soup.select("li:has(div.inner_item_data)")
            if not containers:
                end_reason = "empty_page"
                break

            page_keys = set()
            for c in containers:
                link = c.select_one("a[href*='/product/']")
                nm = c.select_one("span.goods_name")
                page_keys.add((link.get("href", "") if link else "",
                               nm.get_text(strip=True) if nm else ""))
            # このページが全て既出＝ページ範囲を超えて1ページ目が返っている
            if page_keys and page_keys <= seen_items:
                end_reason = "duplicate"
                break
            seen_items |= page_keys

            for container in containers:
                item = scraper._extract_clabo_item(container)
                if item is None:
                    continue
                raw_name = item.pop("raw_name")
                if not scraper._is_clabo_ygo(raw_name):
                    continue
                if scraper._is_rush_duel(raw_name) or scraper._is_rush_duel(item["code"]):
                    continue
                all_products.append(item)

            # 1ページ分に満たなければ最終ページ
            if len(containers) < scraper.CLABO_PAGE_SIZE:
                end_reason = "under_page_size"
                break
            time.sleep(0.5)
        else:
            # for が break せずに終了＝MAX_PAGESに達してもまだ120件フルだった。
            # 打ち切りの可能性があるので不成立側に倒す（ここは緩めない）
            print(
                f"  ⚠ カードラボ カテゴリ{cid}: 安全上限 {CLABO_CATALOG_MAX_PAGES} ページに"
                f"到達してもまだ120件フルでした。打ち切りの疑いがあるため検証失敗扱いにします"
            )
            category_ok = False
            end_reason = "max_pages"

        # item_count による検証（ページ取得失敗で既に不成立の場合は重ねて判定しない）
        if not page_fetch_failed:
            if total == 0:
                # 正常時は14カテゴリ全て2件以上あり、0件はあり得ない（reviewer中2）
                category_ok = False
            elif total is None:
                if end_reason == "max_pages":
                    # 打ち切りの疑いがある巡回はitem_countが読めなくても不成立のまま
                    category_ok = False
                else:
                    # 自然終端（120件未満／重複検出）はできているため、item_countが
                    # 読めないだけで検索方式2,700リクエストへフォールバックするのは
                    # 誤爆コストが大きい。成立扱いにしつつ「未検証」の印を残す
                    # （reviewer中3b）
                    verified = False
                    print(
                        f"  ⚠ カードラボ カテゴリ{cid}: item_countが読めませんでした"
                        f"（終端理由={end_reason}）。巡回自体は自然終端のため成立扱い・"
                        f"未検証としてマークします"
                    )
            elif total != _ITEM_COUNT_CAP_SENTINEL:
                allowed_gap = max(_ITEM_COUNT_MIN_GAP, total * _ITEM_COUNT_GAP_RATIO)
                if len(seen_items) < total - allowed_gap:
                    category_ok = False

        if not category_ok:
            categories_failed.append(cid)
        if not verified:
            categories_unverified.append(cid)

        categories_stats[cid] = {
            "total": total, "seen": len(seen_items), "pages": pages_used,
            "ok": category_ok, "verified": verified,
        }

    stats = {
        "categories_total": len(CLABO_CATEGORIES),
        "categories_failed": categories_failed,
        "categories_unverified": categories_unverified,
        "pages_fetched": pages_fetched,
        "products": len(all_products),
        "failed_pages": failed_pages,
        "categories": categories_stats,
    }
    return all_products, stats


def build_match_index(card_names: list[str]) -> list[tuple[str, "re.Pattern"]]:
    """監視カード名から、カタログ商品名を絞り込むための粗い候補パターンを事前コンパイルする。

    scraper.is_target_card は境界チェック（前後に別カード名の続きがないか等）を含み、
    商品×カード全組合せに素直に呼ぶと実測96.5µs/組＝約2,700枚×2万品目で約510分CPU
    かかる（docs/audit-403-plan-2026-08-08.md H5）。

    ここでは is_target_card が内部で使っているのと同じ scraper._build_flex_pattern
    （区切り記号ゆれを許容する正規表現）をそのまま再利用して候補を粗く絞り込む。
    境界チェックを持たない分、is_target_card の真の一致は必ずこの粗い判定を
    通過する（＝候補集合は真集合を包含する。取りこぼしゼロ）。候補ペアにだけ
    match_products が本物の is_target_card を適用して確定させる。

    自前の正規化・パターン生成は行わない（scraper.normalize_width /
    scraper._build_flex_pattern をそのまま呼ぶ）。
    """
    index: list[tuple[str, "re.Pattern"]] = []
    for name in card_names:
        norm = scraper.normalize_width(name)
        pattern_str = scraper._build_flex_pattern(norm)
        if not pattern_str:
            # is_target_card はパターンが空だと常に False を返す
            # （区切り記号だけのカード名など）。候補に残しても stage2 で必ず
            # 落ちるため、事前に候補から外して無駄なマッチングをしない
            continue
        index.append((name, re.compile(pattern_str)))
    return index


def match_products(products: list[dict], index: list[tuple[str, "re.Pattern"]]
                    ) -> tuple[dict[str, list[dict]], dict]:
    """カタログ商品を監視カードへ名寄せする（二段名寄せの第2段）。

    第1段（build_match_index の粗いパターン）で候補カードを絞り込み、
    候補ペアにだけ本物の scraper.is_target_card を適用して確定させる。
    1商品が複数カードに一致するのは検索方式でも起きる正常挙動として許容する。
    """
    matched: dict[str, list[dict]] = {}
    matched_products = 0
    unmatched_products = 0
    multi_match_products = 0

    for product in products:
        product_name = product.get("name", "")
        norm_product = scraper.normalize_width(product_name)
        candidates = [name for name, pattern in index if pattern.search(norm_product)]
        hits = [name for name in candidates if scraper.is_target_card(name, product_name)]

        if not hits:
            unmatched_products += 1
            continue

        matched_products += 1
        if len(hits) > 1:
            multi_match_products += 1
        for name in hits:
            matched.setdefault(name, []).append(product)

    stats = {
        "matched_products": matched_products,
        "unmatched_products": unmatched_products,
        "multi_match_products": multi_match_products,
    }
    return matched, stats


def collect_clabo_catalog(card_names: list[str]) -> tuple[dict[str, list[dict]] | None, dict]:
    """カタログ巡回による夜間収集のオーケストレータ。

    harvest（カテゴリ差分監視）→ crawl（カタログ巡回）→ match（二段名寄せ）を
    まとめて実行する。

    成立条件: crawl_catalog の categories_failed が空 かつ failed_pages == 0
    かつ products > 0（reviewer中2。カテゴリ別の検証をすべて素通りしても
    「全カテゴリ0件」という空のカタログを成立扱いにしないための保険）。
    不成立なら (None, 統計) を返し、呼び出し側（collect_prices.py）は
    従来の検索方式へフォールバックする。

    カテゴリ差分警告は stats["category_drift"] にも残す（reviewer中2）。
    collection_runs.shop_stats を経由して永続化され、send_daily_report が
    警告行を足せるようにするため（print だけだと収集ログにしか残らない）。
    """
    harvested = harvest_categories()
    drift_warnings = check_category_drift(harvested)
    if drift_warnings is None:
        print("  カードラボ: カテゴリ差分チェックが取得失敗。既知14カテゴリで巡回を続行します")
    else:
        for w in drift_warnings:
            print(f"  カードラボ カテゴリ差分警告: {w}")

    products, crawl_stats = crawl_catalog()

    index = build_match_index(card_names)
    matched, match_stats = match_products(products, index)

    stats = {**crawl_stats, **match_stats, "category_drift": drift_warnings}

    ok = (
        not crawl_stats["categories_failed"]
        and crawl_stats["failed_pages"] == 0
        and crawl_stats["products"] > 0
    )
    if not ok:
        return None, stats
    return matched, stats
