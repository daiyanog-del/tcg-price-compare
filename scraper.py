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
    fp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


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
    "OFウルトラ": "OFウルトラ",
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
    "グランドマスターレア": "グランドマスター",
    "ノーマルパラレルレア": "ノーマルパラレル",
    "ウルトラパラレルレア": "ウルトラパラレル",
    "プレミアムゴールドレア": "ゴールド",
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
        img_el = card.select_one("img.card")
        if img_el and img_el.get("alt"):
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

        if not price or not is_target_card(card_name, raw_name):
            continue

        results.append({
            "shop": "カードラッシュ", "name": display_name,
            "rarity": normalize_rarity(rarity), "code": code,
            "condition": condition, "price": price, "stock": stock,
            "sold_out": sold_out, "url": product_url,
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
            })

        # 次のページがあるか確認
        next_link = soup.select_one("a[href*='p=%d']" % (page + 1))
        if not next_link:
            break
        time.sleep(0.5)

    return all_results


# ── カーナベル ──

KANABELL_BASE = "https://www.ka-nabell.com"

# カーナベルの状態ランク対応表 (img alt → 表示用)
_KANABELL_CONDITION_MAP = {
    "完美品": "状態:S",
    "超～美": "状態:SA",
    "美品":   "状態:A",
    "少傷品": "状態:B",
    "傷あり": "状態:C",
    "大傷品": "状態:D",
}

def scrape_kanabell(card_name: str, max_pages: int = 5) -> list[dict]:
    """カーナベル — 遊戯王専門の大手通販サイト（状態別価格対応）"""
    base_url = (
        f"{KANABELL_BASE}/?act=sell_search&genre=1&type=4"
        f"&keyword={requests.utils.quote(card_name)}"
    )
    all_results = []

    for page in range(1, max_pages + 1):
        page_url = base_url if page == 1 else f"{base_url}&page={page}"
        soup = safe_get(page_url)
        if not soup:
            break
        if page == 1:
            dump_html("kanabell", soup)

        # 各カードは div[id^="card_"] に格納
        card_divs = soup.select('div[id^="card_"]')
        if not card_divs:
            break

        for card_div in card_divs:
            # ── カード名 & URL ──
            name_el = card_div.select_one("thead th div a")
            if not name_el:
                continue
            card_name_text = name_el.get_text(strip=True)
            href = name_el.get("href", "")
            product_url = (
                f"{KANABELL_BASE}/{href}" if href and not href.startswith("http")
                else href
            )

            # ── レアリティ: td.ListSellHeadRar 内の span ──
            rarity = ""
            rar_span = card_div.select_one("td.ListSellHeadRar span")
            if rar_span:
                rarity = rar_span.get_text(strip=True)

            # ── カードコード: ヘッダーのパック情報から抽出を試行 ──
            code = ""
            rar_p = card_div.select_one("td.ListSellHeadRar p")
            if rar_p:
                pack_text = rar_p.get_text(strip=True)
                # "プリシク13期 > BLZD(1304)" → "BLZD" を抽出
                # ※ \xa0 (non-breaking space) が使われている場合がある
                code_match = re.search(r">\s*([A-Z0-9\-]+)", pack_text)
                if code_match:
                    code = code_match.group(1)

            # ── 名前フィルタ ──
            if not is_target_card(card_name, card_name_text):
                continue

            # ── 状態別の価格・在庫: td.Detail 内の内部テーブル行 ──
            detail_table = card_div.select_one("td.Detail table")
            if not detail_table:
                # テーブルが無い = 全状態売切
                all_results.append({
                    "shop": "カーナベル", "name": card_name_text,
                    "rarity": normalize_rarity(rarity), "code": code,
                    "condition": "-", "price": 0,
                    "stock": 0, "sold_out": True, "url": product_url,
                })
                continue

            rows = detail_table.select("tr")
            has_any_stock = False

            for row in rows:
                # 状態画像の alt で判別
                cond_img = row.select_one("img[alt]")
                if not cond_img or not cond_img.get("alt"):
                    continue
                cond_alt = cond_img["alt"].strip()
                if cond_alt not in _KANABELL_CONDITION_MAP:
                    continue

                condition = _KANABELL_CONDITION_MAP[cond_alt]

                # 価格
                price_span = row.select_one("span[style*='color']")
                if not price_span:
                    continue
                price = parse_price(price_span.get_text())
                if not price:
                    continue

                # 在庫: select.pieceNum の option 数 (0枚 を除く)
                sel = row.select_one("select.pieceNum")
                stock = 0
                if sel:
                    options = sel.select("option")
                    stock = max(0, len(options) - 1)  # "0枚" を除く

                sold_out = stock == 0
                if not sold_out:
                    has_any_stock = True

                all_results.append({
                    "shop": "カーナベル", "name": card_name_text,
                    "rarity": normalize_rarity(rarity), "code": code,
                    "condition": condition, "price": price,
                    "stock": stock, "sold_out": sold_out,
                    "url": product_url,
                })

            # 状態行が一つもなかった場合（全売切）
            if not rows or not has_any_stock:
                # 在庫ありの行が既に追加されている場合はスキップ
                if not any(
                    r["url"] == product_url and not r["sold_out"]
                    for r in all_results
                ):
                    # 価格情報すらない場合のフォールバック
                    if not any(r["url"] == product_url for r in all_results):
                        all_results.append({
                            "shop": "カーナベル", "name": card_name_text,
                            "rarity": normalize_rarity(rarity), "code": code,
                            "condition": "-", "price": 0,
                            "stock": 0, "sold_out": True,
                            "url": product_url,
                        })

        # 次のページがあるか確認
        next_link = soup.select_one("a[href*='page=%d']" % (page + 1))
        if not next_link:
            break
        time.sleep(0.5)

    return all_results


# ── 全店舗検索 ──

SHOPS = [
    ("遊々亭", scrape_yuyu),
    ("カードラッシュ", scrape_cardrush),
    ("トレコロCB", scrape_torecolo),
    ("カーナベル", scrape_kanabell),
]

# デフォルトで検索する店舗（カーナベルは任意ON）
DEFAULT_SHOPS = ["遊々亭", "カードラッシュ", "トレコロCB", "カーナベル"]


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
