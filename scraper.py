"""
TCG 価格比較スクレイパー v8
===========================
対応店舗: 遊々亭 / カードラッシュ / トレコロCB

v8: 商品ページURL追加, 売り切れ情報追加, キャッシュ対応
"""

import re
import time
import hashlib
import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from pathlib import Path

# ── 共通設定 ──
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}
WAIT_SEC = 1.0
DEBUG_DUMP_HTML = False
STRICT_NAME_FILTER = True
EXCLUDE_SUPPLY = True

# ── キャッシュ設定 ──
CACHE_ENABLED = True
CACHE_TTL_MINUTES = 15
CACHE_DIR = Path(__file__).parent / ".cache"


# ── ユーティリティ ──

# 全角英数記号 → 半角 変換テーブル
_ZEN2HAN = str.maketrans(
    'ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ'
    'ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ'
    '０１２３４５６７８９'
    '：．／＃＋＝＆＠！？',
    'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    'abcdefghijklmnopqrstuvwxyz'
    '0123456789'
    ':./#+=&@!?',
)

def normalize_width(text: str) -> str:
    """全角英数字を半角に統一"""
    return text.translate(_ZEN2HAN)

def parse_price(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None

def safe_get(url: str, timeout: int = 15, retries: int = 1) -> BeautifulSoup | None:
    for attempt in range(1 + retries):
        try:
            res = requests.get(url, headers=HEADERS, timeout=timeout)
            res.raise_for_status()
            return BeautifulSoup(res.text, "html.parser")
        except requests.RequestException as e:
            if attempt < retries:
                time.sleep(2)
            else:
                print(f"  ❌ 取得失敗: {e}")
    return None

def dump_html(name: str, soup: BeautifulSoup):
    if DEBUG_DUMP_HTML and soup:
        fn = f"debug_{name}.html"
        with open(fn, "w", encoding="utf-8") as f:
            f.write(soup.prettify())


# ── キャッシュ ──

import tempfile
from threading import Lock as _Lock
_cache_lock = _Lock()

def _cache_key(card_name: str) -> str:
    return hashlib.md5(card_name.encode()).hexdigest()

def cache_get(card_name: str) -> list[dict] | None:
    if not CACHE_ENABLED:
        return None
    fp = CACHE_DIR / f"{_cache_key(card_name)}.json"
    if not fp.exists():
        return None
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(data["timestamp"])
        if datetime.now() - ts > timedelta(minutes=CACHE_TTL_MINUTES):
            return None
        return data["results"]
    except Exception:
        return None

def cache_set(card_name: str, results: list[dict]):
    if not CACHE_ENABLED:
        return
    CACHE_DIR.mkdir(exist_ok=True)
    fp = CACHE_DIR / f"{_cache_key(card_name)}.json"
    data = {"timestamp": datetime.now().isoformat(), "results": results}
    content = json.dumps(data, ensure_ascii=False)
    # 一時ファイルに書いてからリネームすることで、書き込み途中のファイルを読まれるのを防ぐ
    with _cache_lock:
        tmp_fp = fp.with_suffix(".tmp")
        tmp_fp.write_text(content, encoding="utf-8")
        tmp_fp.replace(fp)


# ── 名前フィルタ ──

SUPPLY_KEYWORDS = [
    "スリーブ", "プレイマット", "デッキケース", "フィールドセンター",
    "デュエルセット", "デュエルフィールド", "鑑定済",
    "PSA", "BGS", "CGC", "ステンレス製",
]

def _is_japanese_char(c: str) -> bool:
    cp = ord(c)
    return (0x3040 <= cp <= 0x309F or 0x30A0 <= cp <= 0x30FF
            or 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF
            or 0xFF66 <= cp <= 0xFF9F or 0xFF10 <= cp <= 0xFF5A)

def _has_japanese_outside_brackets(text: str) -> bool:
    stripped = re.sub(
        r"〔[^〕]*〕|\([^)]*\)|\[[^\]]*\]|☆[^☆]*☆|「[^」]*」|『[^』]*』", "", text
    )
    return any(_is_japanese_char(c) for c in stripped)

def _build_flex_pattern(card_name: str) -> str:
    parts = re.split(r"[・\s　－\-]", card_name)
    parts = [p for p in parts if p]
    return r"[・\s　－\x2d]*".join(re.escape(p) for p in parts)

def is_target_card(card_name: str, product_name: str) -> bool:
    if not STRICT_NAME_FILTER:
        return True
    if EXCLUDE_SUPPLY:
        for kw in SUPPLY_KEYWORDS:
            if kw in product_name:
                return False
    # 全角英数を半角に統一してからマッチング
    norm_card = normalize_width(card_name)
    norm_product = normalize_width(product_name)
    flex_pattern = _build_flex_pattern(norm_card)
    for match in re.finditer(flex_pattern, norm_product):
        start, end = match.start(), match.end()
        if start > 0 and _has_japanese_outside_brackets(norm_product[:start]):
            continue
        if end < len(norm_product):
            nc = norm_product[end]
            if _is_japanese_char(nc):
                continue
            if nc in "・－-ー、,&" and end + 1 < len(norm_product):
                if _is_japanese_char(norm_product[end + 1]):
                    continue
        return True
    return False


# ── レアリティ正規化 ──

_YUYU_MAP = {
    "N": "ノーマル", "R": "レア", "SR": "スーパー", "UR": "ウルトラ",
    "SE": "シークレット", "UL": "アルティメット", "PSE": "プリシク",
    "QCSE": "25thシークレット", "20thSE": "20thシークレット",
    "KC": "KC", "P-N": "ノーマルパラレル", "P-SR": "スーパーパラレル",
    "P-UR": "ウルトラパラレル", "P-HR": "ホログラフィック",
    "HR": "ホログラフィック", "M": "ミレニアム",
    "GR": "ゴールド", "GS": "ゴールドシークレット",
    "EXSE": "エクシク", "RR": "ラッシュレア", "OR": "オーバーラッシュ",
    # カーナベル略称
    "GSE": "ゴールドシークレット", "ESE": "エクシク",
    "NP": "ノーマルパラレル", "UP": "ウルトラパラレル",
    "CR": "コレクターズ", "ML": "ミレニアム",
    "10000SE": "10000シークレット", "AgSE": "アジアシークレット",
    "GXR": "グランドマスター", "GMR": "グランドマスター",
}
_RUSH_MAP = {
    "ノーマル": "ノーマル", "レア": "レア", "スーパー": "スーパー",
    "ウルトラ": "ウルトラ", "シークレット": "シークレット",
    "アルティメット": "アルティメット", "レリーフ": "アルティメット",
    "プリズマティックシークレット": "プリシク",
    "クォーターセンチュリーシークレット": "25thシークレット",
    "20thシークレット": "20thシークレット", "KC": "KC",
    "ノーマルパラレル": "ノーマルパラレル", "ウルトラパラレル": "ウルトラパラレル",
    "ホログラフィック": "ホログラフィック", "ミレニアム": "ミレニアム",
    "ゴールド": "ゴールド", "ゴールドシークレット": "ゴールドシークレット",
    "エクストラシークレット": "エクシク", "ラッシュレア": "ラッシュレア",
    "オーバーラッシュレア": "オーバーラッシュ", "コレクターズ": "コレクターズ",
    "OFウルトラ": "オーバーフレームウルトラ",
    # トレコロCB の表記（「〜レア」形式）
    "ノーマルレア": "ノーマル", "スーパーレア": "スーパー",
    "ウルトラレア": "ウルトラ", "シークレットレア": "シークレット",
    "アルティメットレア": "アルティメット",
    "プリズマティックシークレットレア": "プリシク",
    "クォーターセンチュリーシークレットレア": "25thシークレット",
    "20thシークレットレア": "20thシークレット",
    "ホログラフィックレア": "ホログラフィック",
    "ホログラフィックパラレルレア": "ホログラフィック",
    "ミレニアムレア": "ミレニアム",
    "ゴールドレア": "ゴールド", "ゴールドシークレットレア": "ゴールドシークレット",
    "エクストラシークレットレア": "エクシク",
    "コレクターズレア": "コレクターズ",
    "グランドマスターレア": "グランドマスター", "グランドマスター": "グランドマスター",
    "オーバーフレームウルトラレア": "オーバーフレームウルトラ", "オーバーフレームウルトラ": "オーバーフレームウルトラ",
    "ノーマルパラレルレア": "ノーマルパラレル",
    "ウルトラパラレルレア": "ウルトラパラレル",
    "プレミアムゴールドレア": "プレミアムゴールド",
    "KCレア": "KC",
    # カーナベルの短縮表記
    "シク": "シークレット",
    "プリシク": "プリシク",
    "ウルトラ": "ウルトラ",
    "スーパー": "スーパー",
    "ノーマル": "ノーマル",
    "レア": "レア",
    "25thシク": "25thシークレット",
    "20thシク": "20thシークレット",
    "ホロ": "ホログラフィック",
    "レリ": "アルティメット",
    "ゴルシク": "ゴールドシークレット",
    "エクシク": "エクシク",
    "コレクターズ": "コレクターズ",
    "コレ": "コレクターズ",
    "OFウルトラ": "オーバーフレームウルトラ",
    # 一般的な短縮略称
    "アル": "アルティメット",
    "スー": "スーパー", "ウル": "ウルトラ", "ミレ": "ミレニアム",
    "ノー": "ノーマル",
    "パラ": "パラレル", "ノーパラ": "ノーマルパラレル",
    "ウルパラ": "ウルトラパラレル", "スーパラ": "スーパーパラレル",
    "字レア": "字レア", "ノーレア": "ノーマルレア",
    "ゴルレア": "ゴールド", "PG": "プレミアムゴールド", "プレゴル": "プレミアムゴールド", "Nレア": "ノーマルレア", "Nパラ": "ノーマルパラレル",
}

def normalize_rarity(raw: str) -> str:
    if not raw:
        return ""
    if raw in _YUYU_MAP:
        return _YUYU_MAP[raw]
    if raw in _RUSH_MAP:
        return _RUSH_MAP[raw]
    for key, val in _RUSH_MAP.items():
        if raw.startswith(key):
            return val
    return raw

def _extract_rarity_bracket(s: str) -> str:
    m = re.search(r"【([^】]+)】", s)
    return m.group(1) if m else ""

def _extract_code_brace(s: str) -> str:
    m = re.search(r"\{([^}]+)\}", s)
    return m.group(1) if m else ""

def _extract_condition(s: str) -> str:
    m = re.search(r"〔(状態[^〕]*)〕", s)
    return m.group(1) if m else "-"

def _clean_display_name(raw: str) -> str:
    s = re.sub(r"〔[^〕]*〕", "", raw)
    s = re.sub(r"【[^】]*】", "", s)
    s = re.sub(r"\{[^}]*\}", "", s)
    s = re.sub(r"《[^》]*》", "", s)
    s = re.sub(r"\[[^\]]*\]", "", s)
    return s.strip()


# ── 遊々亭 ──

def scrape_yuyu(card_name: str) -> list[dict]:
    page_url = f"https://yuyu-tei.jp/sell/ygo/s/search?search_word={requests.utils.quote(card_name)}"
    soup = safe_get(page_url, timeout=25, retries=2)
    if not soup:
        return []
    dump_html("yuyu", soup)

    results = []
    for card in soup.select("div.card-product"):
        classes = " ".join(card.get("class", []))
        sold_out = "sold-out" in classes

        name, product_url = "", ""
        for a_tag in card.select("a"):
            text = a_tag.get_text(strip=True)
            href = a_tag.get("href", "")
            if text and "カート" not in text and len(text) > 1 and not a_tag.select_one("img.card"):
                name = text
                product_url = href
                break
            elif not product_url and href and "yuyu-tei.jp" in href:
                product_url = href

        if not name:
            continue

        rarity, code = "", ""
        image_url = ""
        img_el = card.select_one("img.card")
        if img_el:
            image_url = img_el.get("src", "")
            if img_el.get("alt"):
                parts = img_el["alt"].split(" ", 2)
                if len(parts) >= 2:
                    code, rarity = parts[0], parts[1]
        if not code:
            code_el = card.select_one("span.d-block.border")
            code = code_el.get_text(strip=True) if code_el else ""

        price_el = card.select_one("strong.d-block")
        price = parse_price(price_el.get_text()) if price_el else None

        stock_label = card.select_one("label.form-check-label")
        stock_text = stock_label.get_text(strip=True) if stock_label else ""
        stock_match = re.search(r"(\d+)\s*点", stock_text)
        stock = int(stock_match.group(1)) if stock_match else 0

        is_sale = "sale" in classes

        if not price or not is_target_card(card_name, name):
            continue

        results.append({
            "shop": "遊々亭", "name": name,
            "rarity": normalize_rarity(rarity), "code": code,
            "condition": "セール" if is_sale else "-",
            "price": price, "stock": stock,
            "sold_out": sold_out, "url": product_url,
            "image": image_url,
        })
    return results


# ── カードラッシュ ──

def scrape_cardrush(card_name: str) -> list[dict]:
    search_name = card_name.replace("・", "").replace("　", " ")
    page_url = f"https://www.cardrush.jp/product-list?keyword={requests.utils.quote(search_name)}"
    soup = safe_get(page_url)
    if not soup:
        return []
    dump_html("cardrush", soup)

    results = []
    for item in soup.select("li[class*='list_item_cell']"):
        name_el = item.select_one("p.item_name")
        price_el = item.select_one("div.price")
        stock_el = item.select_one("p.stock")
        link_el = item.select_one("a.item_data_link")

        if not name_el or not price_el:
            continue

        raw_name = name_el.get_text(strip=True)
        price = parse_price(price_el.get_text())
        product_url = link_el.get("href", "") if link_el else ""

        stock_text = stock_el.get_text(strip=True) if stock_el else ""
        stock_match = re.search(r"(\d+)", stock_text)
        stock = int(stock_match.group(1)) if stock_match else 0
        sold_out = stock == 0

        rarity = _extract_rarity_bracket(raw_name)
        code = _extract_code_brace(raw_name)
        condition = _extract_condition(raw_name)
        display_name = _clean_display_name(raw_name)

        # 商品画像
        img_el = item.select_one("img")
        image_url = img_el.get("src", "") if img_el else ""

        if not price or not is_target_card(card_name, raw_name):
            continue

        results.append({
            "shop": "カードラッシュ", "name": display_name,
            "rarity": normalize_rarity(rarity), "code": code,
            "condition": condition, "price": price, "stock": stock,
            "sold_out": sold_out, "url": product_url,
            "image": image_url,
        })
    return results


# ── トレコロCB ──

TORECOLO_BASE = "https://www.torecolo.jp"

def scrape_torecolo(card_name: str, max_pages: int = 5) -> list[dict]:
    """トレコロCB — 複数ページ対応、レアリティ取得"""
    base_url = (
        f"{TORECOLO_BASE}/shop/goods/search.aspx"
        f"?search=x&keyword={requests.utils.quote(card_name)}&category=&oshiire_code="
    )
    all_results = []

    for page in range(1, max_pages + 1):
        page_url = base_url if page == 1 else f"{base_url}&p={page}"
        soup = safe_get(page_url)
        if not soup:
            break
        if page == 1:
            dump_html("torecolo", soup)

        items = soup.select("dl.block-thumbnail-t--goods")
        if not items:
            break

        for item in items:
            name_el = item.select_one("a.js-enhanced-ecommerce-goods-name")
            price_el = item.select_one("div.block-thumbnail-t--price")

            if not name_el or not price_el:
                continue

            name = name_el.get_text(strip=True)
            price_text = price_el.get_text(strip=True)

            if "買取" in price_text or "参考" in price_text:
                continue

            price = parse_price(price_text)

            # 在庫判定: btn-sold-out があれば売切、カートボタンがあれば在庫あり
            sold_btn = item.select_one(".btn-sold-out")
            cart_btn = item.select_one("a.block-products--product-sale-cart-button")
            sold_out = sold_btn is not None
            has_stock = cart_btn is not None and not sold_out

            href = name_el.get("href", "")
            product_url = f"{TORECOLO_BASE}{href}" if href.startswith("/") else href

            # ── コンディション判定 ──
            # URLの -K サフィックスまたは商品名の「キズあり」で判定
            is_kizu = "-K/" in href or href.endswith("-K")
            condition = "中古キズあり" if is_kizu else "-"
            if not is_kizu and ("キズあり" in name or "★キズあり★" in name):
                is_kizu = True
                condition = "中古キズあり"

            # ── 商品名の正規化（名前マッチング用） ──
            # トレコロの商品名は "キズあり【遊戯王】レアリティ◇カード名" 形式の場合がある
            match_name = name
            # 「キズあり」プレフィックスを除去
            match_name = re.sub(r"^キズあり", "", match_name).strip()
            # 「★キズあり★」を除去
            match_name = re.sub(r"★キズあり★", "", match_name).strip()
            # 【遊戯王】等のゲーム名タグを除去
            match_name = re.sub(r"【[^】]*】", "", match_name).strip()
            # レアリティ◇ プレフィックスを除去 (例: "ウルトラレア◇")
            match_name = re.sub(r"^[^◇]*◇", "", match_name).strip()
            # （商品状態・XXX）を除去
            match_name = re.sub(r"（[^）]*）", "", match_name).strip()

            # レアリティ: div.block-thumbnail-t--goods-category から取得
            rarity = ""
            cat_el = item.select_one("div.block-thumbnail-t--goods-category")
            if cat_el:
                rarity = cat_el.get_text(strip=True)
                # 全角英数を半角に変換
                rarity = rarity.translate(str.maketrans(
                    'ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ０１２３４５６７８９',
                    'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
                ))

            # カードコード: URLから抽出
            code = ""
            code_match = re.search(r"/g/g([^/]+?)(-[SK])?/", href)
            if code_match:
                code = code_match.group(1)

            # 商品画像
            img_el = item.select_one("img")
            image_url = ""
            if img_el:
                image_url = img_el.get("src", "") or img_el.get("data-src", "")
                if image_url and not image_url.startswith("http"):
                    image_url = f"{TORECOLO_BASE}{image_url}"

            if not price or not is_target_card(card_name, match_name):
                continue

            # 表示名: match_name が空でなければそちらを使用
            display_name = match_name if match_name else name

            all_results.append({
                "shop": "トレコロCB", "name": display_name,
                "rarity": normalize_rarity(rarity), "code": code,
                "condition": condition, "price": price,
                "stock": 1 if has_stock else 0,
                "sold_out": not has_stock, "url": product_url,
                "image": image_url,
            })

        # 次のページがあるか確認
        next_link = soup.select_one("a[href*='p=%d']" % (page + 1))
        if not next_link:
            break
        time.sleep(0.5)

    return all_results


# ── カーナベル (Elasticsearch API) ──

KANABELL_BASE = "https://www.ka-nabell.com"

import base64 as _b64
import os as _os

# カーナベル接続情報（環境変数から取得）
_KANABELL_CLOUD_ID = _os.environ.get("KANABELL_CLOUD_ID", "")
_KANABELL_API_KEY = _os.environ.get("KANABELL_API_KEY", "")
_KANABELL_INDEX = _os.environ.get("KANABELL_INDEX", "ec-cards")

def _kanabell_es_host():
    """Cloud IDからElasticsearchホストURLを構築"""
    encoded = _KANABELL_CLOUD_ID.split(":")[1]
    decoded = _b64.b64decode(encoded).decode()
    parts = decoded.split("$")
    return f"https://{parts[1]}.{parts[0]}"

_KANABELL_ES_URL = None  # lazy init

# 状態ランク: ESフィールド名 → 表示名
_KANABELL_CONDITIONS = [
    ("sa", "状態:SA"),
    ("b",  "状態:B"),
    ("c",  "状態:C"),
    ("d",  "状態:D"),
]

def scrape_kanabell(card_name: str, max_pages: int = 5) -> list[dict]:
    """カーナベル — Elasticsearch API経由で検索（状態別価格対応）"""
    if not _KANABELL_CLOUD_ID or not _KANABELL_API_KEY:
        print("  ⚠️  カーナベル: KANABELL_CLOUD_ID / KANABELL_API_KEY が未設定です")
        return []

    global _KANABELL_ES_URL
    if _KANABELL_ES_URL is None:
        _KANABELL_ES_URL = _kanabell_es_host()

    search_url = f"{_KANABELL_ES_URL}/{_KANABELL_INDEX}/_search"

    # ページあたり件数 (ESの1リクエストで取得)
    page_size = 30 * max_pages  # max_pages=1なら30件、5なら150件

    # Elasticsearch クエリ (build.js の postProcessRequestBodyFn を再現)
    query_body = {
        "size": page_size,
        "_source": [
            "name", "id", "category1_id",
            "sa_selling_price", "b_selling_price", "c_selling_price", "d_selling_price",
            "sa_stock", "b_stock", "c_stock", "d_stock",
            "rarity_abbreviation", "category2_abbr", "category3_abbr",
            "card_image_name1",
        ],
        "query": {
            "bool": {
                "must": [
                    {"bool": {"should": [
                        {"match_phrase_prefix": {"card_name": {"query": card_name, "slop": 2}}},
                        {"match_phrase_prefix": {"replace_card_name": {"query": card_name, "slop": 2}}},
                        {"wildcard": {"card_name": {"value": f"*{card_name}*"}}},
                        {"wildcard": {"replace_card_name": {"value": f"*{card_name}*"}}},
                    ], "minimum_should_match": 1}}
                ],
                "filter": [
                    {"term": {"category1_id": 1}},   # 遊戯王
                    {"term": {"public_status": 1}},
                    {"term": {"del_flag": False}},
                ]
            }
        },
        "sort": [
            {"category2_sort": "asc"},
            {"category3_sort": "asc"},
            {"rarity_sort": "asc"},
            {"sort": "asc"},
        ]
    }

    try:
        res = requests.post(
            search_url,
            json=query_body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"ApiKey {_KANABELL_API_KEY}",
            },
            timeout=15,
        )
        res.raise_for_status()
        data = res.json()
    except requests.RequestException as e:
        print(f"  ❌ カーナベルES検索失敗: {e}")
        return []

    hits = data.get("hits", {}).get("hits", [])
    all_results = []

    for hit in hits:
        src = hit.get("_source", {})
        name_text = src.get("name", "")
        card_id = src.get("id") or hit.get("_id", "")

        if not name_text or not is_target_card(card_name, name_text):
            continue

        # レアリティ
        rarity = src.get("rarity_abbreviation", "")

        # カードコード (カテゴリ略称から組み立て)
        code = ""
        cat3 = src.get("category3_abbr", "")
        if cat3:
            code = cat3

        # 商品URL
        product_url = f"{KANABELL_BASE}/?act=sell_detail&genre=1&id={card_id}"

        # 画像URL
        img_name = src.get("card_image_name1", "")
        image_url = ""
        if img_name:
            image_url = f"{KANABELL_BASE}/img/s/{img_name}"

        # 各状態の価格・在庫を展開
        has_any = False
        for rank_key, cond_label in _KANABELL_CONDITIONS:
            price = src.get(f"{rank_key}_selling_price", 0)
            stock = src.get(f"{rank_key}_stock", 0)

            if not price and not stock:
                continue

            sold_out = stock <= 0
            if not sold_out:
                has_any = True

            if price and price > 0:
                all_results.append({
                    "shop": "カーナベル", "name": name_text,
                    "rarity": normalize_rarity(rarity), "code": code,
                    "condition": cond_label, "price": int(price),
                    "stock": int(stock), "sold_out": sold_out,
                    "url": product_url,
                    "image": image_url,
                })

        # どの状態にも在庫/価格がない場合
        if not has_any and not any(r["url"] == product_url for r in all_results):
            # SA価格があれば売切として記録
            sa_price = src.get("sa_selling_price", 0)
            if sa_price and sa_price > 0:
                all_results.append({
                    "shop": "カーナベル", "name": name_text,
                    "rarity": normalize_rarity(rarity), "code": code,
                    "condition": "状態:SA", "price": int(sa_price),
                    "stock": 0, "sold_out": True,
                    "url": product_url,
                    "image": image_url,
                })

    return all_results


# ── カードラボ ──

CLABO_BASE = "https://www.c-labo-online.jp"

def scrape_clabo(card_name: str) -> list[dict]:
    """カードラボ — 商品検索ページをスクレイピング"""
    page_url = (
        f"{CLABO_BASE}/product-list"
        f"?keyword={requests.utils.quote(card_name)}"
    )
    soup = safe_get(page_url)
    if not soup:
        return []
    dump_html("clabo", soup)

    results = []
    # 各商品は div.inner_item_data 内にリンク・画像・商品情報がまとまっている
    # 親の a タグ (product/XXXXX) からリンクを取得
    for container in soup.select("li:has(div.inner_item_data)"):
        inner = container.select_one("div.inner_item_data")
        if not inner:
            continue

        # 商品名
        name_el = inner.select_one("span.goods_name")
        if not name_el:
            continue
        raw_name = name_el.get_text(strip=True)

        # レアリティとコードを商品名から抽出
        # 形式: 【遊戯】カード名【レアリティ/種類】コード
        rarity = ""
        code = ""
        rarity_match = re.search(r"【([^】]+/[^】]+)】(\S+)$", raw_name)
        if rarity_match:
            rarity_raw = rarity_match.group(1).split("/")[0]
            code = rarity_match.group(2)
            rarity = rarity_raw
        # 【遊戯】を除去してカード名を抽出
        display_name = re.sub(r"【[^】]*】", "", raw_name).strip()
        # 末尾のコードを除去
        if code:
            display_name = display_name.replace(code, "").strip()

        if not is_target_card(card_name, display_name):
            continue

        # 価格
        price_el = inner.select_one("span.figure")
        if not price_el:
            continue
        price = parse_price(price_el.get_text())
        if not price:
            continue

        # 在庫
        stock_el = inner.select_one("p.stock")
        sold_out = False
        stock = 0
        if stock_el:
            if "soldout" in stock_el.get("class", []):
                sold_out = True
            else:
                stock_match = re.search(r"(\d+)", stock_el.get_text())
                stock = int(stock_match.group(1)) if stock_match else 1

        # 商品リンク
        link_el = container.select_one("a[href*='/product/']")
        product_url = link_el.get("href", "") if link_el else ""

        # 画像（data-src に実際の画像URLが入っている）
        image_url = ""
        img_box = inner.select_one("div.async_image_box")
        if img_box:
            image_url = img_box.get("data-src", "")

        results.append({
            "shop": "カードラボ", "name": display_name,
            "rarity": normalize_rarity(rarity), "code": code,
            "condition": "-", "price": price, "stock": stock,
            "sold_out": sold_out, "url": product_url,
            "image": image_url,
        })

    return results


# ── まんぞく屋 ──

# レアリティ表記のマッピング（まんぞく屋独自の括弧表記 → 統一表記）
_MANZOKU_RARITY_MAP = {
    "SE": "シークレット",
    "UR": "ウルトラ",
    "SR": "スーパー",
    "R": "レア",
    "N": "ノーマル",
    "NR": "ノーマルレア",
    "NPR": "ノーマルパラレル",
    "PR": "パラレル",
    "GL": "ゴールド",
    "GS": "ゴールドシークレット",
    "UL": "アルティメット",
    "HR": "ホログラフィック",
    "20SE": "20thシークレット",
    "QCSE": "クォーターセンチュリーシークレット",
    "PSE": "プリズマティックシークレット",
    "PG": "プレミアムゴールド",
    "KC": "KC",
    "ML": "ミレニアム",
    "EX": "エクストラシークレット",
    "10000SE": "10000シークレット",
}

def _parse_manzoku_rarity(text: str) -> str:
    """まんぞく屋の括弧付きレアリティ表記を抽出"""
    # [SE], 〈UR〉, 〔N〕, 【R】 などの形式に対応
    m = re.search(r'[\[〈〔【\(]([A-Z0-9]+)[\]〉〕】\)]', text)
    if m:
        code = m.group(1)
        return _MANZOKU_RARITY_MAP.get(code, code)
    return ""

def scrape_manzoku(card_name: str) -> list[dict]:
    """まんぞく屋 — EC-CUBEベースの遊戯王カード通販"""
    page_url = (
        f"https://shopmanzokuya.com/products/list"
        f"?category_id=1&name={requests.utils.quote(card_name)}"
        f"&orderby=price_l&disp_number=100"
    )
    soup = safe_get(page_url)
    if not soup:
        return []
    dump_html("manzoku", soup)

    results = []
    for li in soup.select("li"):
        # 商品リンクを探す
        link = li.select_one("a[href*='/products/detail/']")
        if not link:
            continue

        text = link.get_text(separator=" ", strip=True)
        if not text:
            continue

        # 価格を抽出（￥1,234 形式）
        price_match = re.search(r'￥([\d,]+)', text)
        if not price_match:
            continue
        price = parse_price(price_match.group(0))
        if not price:
            continue

        # カード番号とカード名を抽出（《カード名》形式）
        name_match = re.search(r'《(.+?)》', text)
        if not name_match:
            continue
        display_name = name_match.group(1).strip()

        if not is_target_card(card_name, display_name):
            continue

        # レアリティ
        rarity = _parse_manzoku_rarity(text)

        # カード番号
        code = ""
        code_match = re.search(r'([A-Z0-9]+-JP[A-Z]?\d+)', text)
        if code_match:
            code = code_match.group(1)

        # 在庫（リンクの外にあるテキストを確認）
        sold_out = False
        stock = 0
        li_text = li.get_text()
        if '品切れ' in li_text or '売り切れ' in li_text:
            sold_out = True
        else:
            stock_match = re.search(r'在庫[:\s]*(\d+)', li_text)
            if stock_match:
                stock = int(stock_match.group(1))
            elif '在庫' in li_text and '◯' in li_text:
                stock = 1

        # 商品URL
        product_url = link.get("href", "")
        if product_url and not product_url.startswith("http"):
            product_url = "https://shopmanzokuya.com" + product_url

        # 画像
        image_url = ""
        img = link.select_one("img")
        if img:
            image_url = img.get("src", "") or img.get("data-src", "")
            if image_url and not image_url.startswith("http"):
                image_url = "https://shopmanzokuya.com" + image_url

        results.append({
            "shop": "まんぞく屋", "name": display_name,
            "rarity": normalize_rarity(rarity), "code": code,
            "condition": "-", "price": price, "stock": stock,
            "sold_out": sold_out, "url": product_url,
            "image": image_url,
        })

    return results


# ══════════════════════════════════════════════════
# 買取価格スクレイパー
# ══════════════════════════════════════════════════

# ── カードラッシュ買取 (Next.js __NEXT_DATA__) ──

CARDRUSH_MEDIA_BASE = "https://cardrush.media"

def scrape_cardrush_buy(card_name: str) -> list[dict]:
    """カードラッシュ — ラッシュメディアの買取価格を取得"""
    page_url = (
        f"{CARDRUSH_MEDIA_BASE}/yugioh/buying_prices"
        f"?name={requests.utils.quote(card_name)}"
    )
    soup = safe_get(page_url, timeout=20)
    if not soup:
        return []

    # __NEXT_DATA__ からJSONデータを取得
    script_el = soup.select_one("script#__NEXT_DATA__")
    if not script_el:
        return []

    try:
        next_data = json.loads(script_el.string)
        buying_prices = next_data["props"]["pageProps"]["buyingPrices"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []

    results = []
    for item in buying_prices:
        name = item.get("name", "")
        if not name or not is_target_card(card_name, name):
            continue
        price = item.get("amount")
        if not price or price <= 0:
            continue

        rarity = item.get("rarity", "")
        model_number = item.get("model_number", "")
        is_hot = item.get("is_hot", False)

        results.append({
            "shop": "カードラッシュ",
            "name": name,
            "rarity": normalize_rarity(rarity),
            "code": model_number,
            "condition": "強化買取中" if is_hot else "-",
            "price": int(price),
            "stock": 1,  # 買取は常に受付中
            "sold_out": False,
            "url": page_url,
            "image": "",
        })
    return results


# ── カーナベル買取 ──

def scrape_kanabell_buy(card_name: str) -> list[dict]:
    """カーナベル — ES APIでカードIDを取得し、買取詳細ページから買取価格を取得"""
    if not _KANABELL_CLOUD_ID or not _KANABELL_API_KEY:
        print("  ⚠️  カーナベル買取: KANABELL_CLOUD_ID / KANABELL_API_KEY が未設定です")
        return []

    global _KANABELL_ES_URL
    if _KANABELL_ES_URL is None:
        _KANABELL_ES_URL = _kanabell_es_host()

    # ES APIでカード名を検索してIDを取得
    search_url = f"{_KANABELL_ES_URL}/{_KANABELL_INDEX}/_search"
    query_body = {
        "size": 30,
        "_source": ["name", "id", "rarity_abbreviation", "category3_abbr", "card_image_name1"],
        "query": {
            "bool": {
                "must": [
                    {"bool": {"should": [
                        {"match_phrase_prefix": {"card_name": {"query": card_name, "slop": 2}}},
                        {"wildcard": {"card_name": {"value": f"*{card_name}*"}}},
                    ], "minimum_should_match": 1}}
                ],
                "filter": [
                    {"term": {"category1_id": 1}},
                    {"term": {"public_status": 1}},
                    {"term": {"del_flag": False}},
                ]
            }
        }
    }

    try:
        res = requests.post(
            search_url, json=query_body,
            headers={"Content-Type": "application/json", "Authorization": f"ApiKey {_KANABELL_API_KEY}"},
            timeout=15,
        )
        res.raise_for_status()
        hits = res.json().get("hits", {}).get("hits", [])
    except requests.RequestException as e:
        print(f"  ❌ カーナベル買取ES検索失敗: {e}")
        return []

    # ユニークなカードIDだけ取得（同名カードを1つにまとめる）
    seen_ids = set()
    cards_to_check = []
    for hit in hits:
        src = hit.get("_source", {})
        name_text = src.get("name", "")
        card_id = src.get("id") or hit.get("_id", "")
        if not name_text or not card_id or card_id in seen_ids:
            continue
        if not is_target_card(card_name, name_text):
            continue
        seen_ids.add(card_id)
        cards_to_check.append(src)

    if not cards_to_check:
        return []

    # 最初のカードの買取詳細ページを取得（1回のアクセスで同名全レアリティの買取価格が見れる）
    first_id = cards_to_check[0].get("id", "")
    buy_url = f"{KANABELL_BASE}/?act=buy_detail&id={first_id}&genre=1"
    soup = safe_get(buy_url, timeout=20)
    if not soup:
        return []

    results = []
    # 画像URL（ES検索結果から取得）
    img_name = cards_to_check[0].get("card_image_name1", "")
    image_url = f"{KANABELL_BASE}/img/s/{img_name}" if img_name else ""

    # 「取扱一覧」セクションのリンクから全バリアントの買取価格・レアリティを取得
    # 各リンクは "¥xxxx円～ 【レアリティ】 シリーズ > セット" の形式
    for a_tag in soup.select("a[href*='act=buy_detail']"):
        link_text = a_tag.get_text(strip=True)
        # 買取終了は除外
        if "買取終了" in link_text:
            continue
        # 価格を抽出（¥xxxx円～ の部分）
        price_match = re.search(r"[¥￥]?\s*(\d[\d,]+)\s*円", link_text)
        if not price_match:
            continue
        price = int(price_match.group(1).replace(",", ""))
        if price <= 0:
            continue
        # レアリティを抽出（【xxx】の部分）
        rarity = ""
        rarity_match = re.search(r"【([^】]+)】", link_text)
        if rarity_match:
            rarity = rarity_match.group(1)
        # hrefから個別の買取詳細URLを構築
        href = a_tag.get("href", "")
        variant_url = f"{KANABELL_BASE}/{href.lstrip('/')}" if href else buy_url
        # シリーズコード抽出（レアリティの後ろの文字列からセット名を取得）
        code = ""
        code_match = re.search(r">\s*(\S+)\s*$", link_text)
        if code_match:
            code = code_match.group(1)

        results.append({
            "shop": "カーナベル",
            "name": cards_to_check[0].get("name", card_name),
            "rarity": normalize_rarity(rarity),
            "code": code,
            "condition": "-",
            "price": price,
            "stock": 1,
            "sold_out": False,
            "url": variant_url,
            "image": image_url,
        })

    # 取扱一覧からの取得が0件の場合、メインカードのパンくずからレアリティを取得してフォールバック
    if not results:
        # パンくず等からメインカードのレアリティを取得
        page_text = soup.get_text(" ", strip=True)
        main_rarity = ""
        main_rarity_match = re.search(r"【([^】]+)】", page_text)
        if main_rarity_match:
            main_rarity = main_rarity_match.group(1)
        # CardListPrice要素から買取価格を取得（フォールバック）
        for price_el in soup.select(".CardListPrice"):
            price_text = price_el.get_text(strip=True)
            if "買取終了" in price_text:
                continue
            pm = re.search(r"(\d[\d,]+)", price_text.replace("¥", ""))
            if not pm:
                continue
            price = int(pm.group(1).replace(",", ""))
            if price <= 0:
                continue
            results.append({
                "shop": "カーナベル",
                "name": cards_to_check[0].get("name", card_name),
                "rarity": normalize_rarity(main_rarity),
                "code": "",
                "condition": "-",
                "price": price,
                "stock": 1,
                "sold_out": False,
                "url": buy_url,
                "image": image_url,
            })
    return results


# ── 遊々亭買取 ──

def scrape_yuyu_buy(card_name: str) -> list[dict]:
    """遊々亭 — 買取検索ページをスクレイピング"""
    page_url = (
        f"https://yuyu-tei.jp/buy/ygo/s/search"
        f"?search_word={requests.utils.quote(card_name)}"
    )
    soup = safe_get(page_url, timeout=25, retries=2)
    if not soup:
        return []

    results = []
    # 販売ページと同様の card-product 構造を想定
    for card in soup.select("div.card-product"):
        name, product_url = "", ""
        for a_tag in card.select("a"):
            text = a_tag.get_text(strip=True)
            href = a_tag.get("href", "")
            if text and "カート" not in text and "買取" not in text and len(text) > 1 and not a_tag.select_one("img.card"):
                name = text
                product_url = href
                break
            elif not product_url and href and "yuyu-tei.jp" in href:
                product_url = href

        if not name or not is_target_card(card_name, name):
            continue

        rarity, code = "", ""
        image_url = ""
        img_el = card.select_one("img.card")
        if img_el:
            image_url = img_el.get("src", "")
            if img_el.get("alt"):
                parts = img_el["alt"].split(" ", 2)
                if len(parts) >= 2:
                    code, rarity = parts[0], parts[1]

        # 買取価格
        price_el = card.select_one("strong.d-block")
        price = parse_price(price_el.get_text()) if price_el else None
        if not price:
            continue

        results.append({
            "shop": "遊々亭",
            "name": name,
            "rarity": normalize_rarity(rarity),
            "code": code,
            "condition": "-",
            "price": price,
            "stock": 1,
            "sold_out": False,
            "url": product_url,
            "image": image_url,
        })
    return results


# ── 買取店舗リスト ──

BUYBACK_SHOPS = [
    ("カードラッシュ", scrape_cardrush_buy),
    ("カーナベル", scrape_kanabell_buy),
    ("遊々亭", scrape_yuyu_buy),
]

DEFAULT_BUYBACK_SHOPS = ["カードラッシュ", "カーナベル", "遊々亭"]


# ── 買取キャッシュ（販売と分離）──

BUYBACK_CACHE_DIR = Path(__file__).parent / ".cache_buy"

def _buyback_cache_key(card_name: str) -> str:
    return hashlib.md5(f"buy_{card_name}".encode()).hexdigest()

def buyback_cache_get(card_name: str) -> list[dict] | None:
    if not CACHE_ENABLED:
        return None
    fp = BUYBACK_CACHE_DIR / f"{_buyback_cache_key(card_name)}.json"
    if not fp.exists():
        return None
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(data["timestamp"])
        if datetime.now() - ts > timedelta(minutes=CACHE_TTL_MINUTES):
            return None
        return data["results"]
    except Exception:
        return None

def buyback_cache_set(card_name: str, results: list[dict]):
    if not CACHE_ENABLED:
        return
    BUYBACK_CACHE_DIR.mkdir(exist_ok=True)
    fp = BUYBACK_CACHE_DIR / f"{_buyback_cache_key(card_name)}.json"
    data = {"timestamp": datetime.now().isoformat(), "results": results}
    content = json.dumps(data, ensure_ascii=False)
    with _cache_lock:
        tmp_fp = fp.with_suffix(".tmp")
        tmp_fp.write_text(content, encoding="utf-8")
        tmp_fp.replace(fp)


# ── 全店舗検索 ──

SHOPS = [
    ("遊々亭", scrape_yuyu),
    ("カードラッシュ", scrape_cardrush),
    ("トレコロCB", scrape_torecolo),
    ("カーナベル", scrape_kanabell),
    ("カードラボ", scrape_clabo),
    ("まんぞく屋", scrape_manzoku),
]

# デフォルトで検索する店舗
DEFAULT_SHOPS = ["遊々亭", "カードラッシュ", "トレコロCB", "カーナベル", "カードラボ", "まんぞく屋"]


def compare_prices(card_name: str, shop_names: list[str] | None = None) -> list[dict]:
    """指定された店舗を並列にスクレイピング"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    target = shop_names or DEFAULT_SHOPS
    active = [(name, fn) for name, fn in SHOPS if name in target]

    all_results = []
    with ThreadPoolExecutor(max_workers=len(active)) as executor:
        futures = {
            executor.submit(fn, card_name): name
            for name, fn in active
        }
        for future in as_completed(futures):
            shop_name = futures[future]
            try:
                results = future.result()
                all_results.extend(results)
            except Exception as e:
                print(f"  ❌ {shop_name}: {e}")

    return all_results


def compare_prices_with_cache(card_name: str) -> list[dict]:
    cached = cache_get(card_name)
    if cached is not None:
        return cached
    results = compare_prices(card_name)
    cache_set(card_name, results)
    return results
