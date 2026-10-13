"""メルカリ参考相場（カード詳細の別枠表示専用）。

方針（ユーザー承認済み）:
  - メルカリは店舗として SHOPS/DEFAULT_SHOPS に入れない。最安比較・夜間収集・
    値動きランキング・X投稿・送料計算には一切混ぜない。カード詳細に別枠で出すだけ
  - 対象は出品中のみ（売り切れ除外）。1検索1リクエスト（ページ送りしない）
  - 出品画像は扱わない（メルカリアンバサダーのガイドラインで画像流用が禁止のため）
  - 取得できなかった（fetch_failed）と0件（在庫なし）を区別して返す

取得: POST https://api.mercari.jp/v2/entities:search に DPoP ヘッダ（プロセス内で
生成した P-256 鍵による ES256 署名 JWT）と X-Platform: web を付ける。ログイン不要。
"""
from __future__ import annotations

import base64
import json
import logging
import re
import statistics
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timedelta
from typing import Callable
from urllib.parse import quote

import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

import rarity as _rarity
from card_code import looks_like_card_code
from constants import JST

logger = logging.getLogger(__name__)

SEARCH_API_URL = "https://api.mercari.jp/v2/entities:search"
CATEGORY_ID_YUGIOH_SINGLE = 1152  # 遊戯王シングル（プレイマット等は別カテゴリで除ける）
PAGE_SIZE = 120
REQUEST_TIMEOUT_SEC = 15

CACHE_TTL_SEC = 15 * 60  # TODO: calibrate from data
CACHE_MAX_ENTRIES = 500
MERCARI_MEDIAN_MIN_COUNT = 3  # TODO: calibrate from data（これ未満は中央値を出さない）
MERCARI_MIN_LISTING_PRICE = 300  # メルカリの最低出品価格。min がこれなら「下限張り付き」
CODE_LOOKBACK_DAYS = 90  # 型番→レアリティ補完で見る価格履歴の期間
CODE_MAX_PAGES = 10  # 価格履歴の読み出し上限（1000行×10ページ）

# ── 除外規則（商品名） ──
# TODO: calibrate from data（実データで除外漏れ・過剰除外を確認しながら増減する）
GRADED_PATTERN = re.compile(r"PSA|BGS|ARS|CGC|鑑定")
MULTI_PATTERN = re.compile(
    r"(?<![0-9A-Z])(?:[2-9]|[1-9][0-9]+)\s*枚"  # 「2枚」「10枚」（「1枚」は単品）
    r"|セット|まとめ|計\s*[0-9]+\s*枚|大量"
)
OVERSEAS_PATTERN = re.compile(r"英語|アジア|韓国|海外|北米|EU版|(?<![A-Z])EU(?![A-Z])|欧州")
SUPPLY_PATTERN = re.compile(
    r"スリーブ|プロテクター|プレイマット|ストレージ|デッキケース|ラバーマット|"
    r"フィールドセンター|デュエルセット|デュエルフィールド|ステンレス|"
    r"ローダー|カードケース|トレカケース|ぬいぐるみ|フィギュア"
)
# OCGの通常カードではない別商品（旧バンダイ版・カードダス・手描きトークン等）
NON_OCG_PATTERN = re.compile(r"バンダイ|BANDAI|カードダス|手描き|トークン|ハンドメイド")


class MercariFetchError(Exception):
    """取得失敗（タイムアウト・401/403/429・JSON不正など）。0件とは区別する。"""


# ── DPoP 署名 ──

def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


_key_lock = threading.Lock()
_key = None
_jwk = None


def _ensure_key():
    """プロセス内で1回だけ P-256 鍵を生成する。"""
    global _key, _jwk
    with _key_lock:
        if _key is None:
            _key = ec.generate_private_key(ec.SECP256R1())
            nums = _key.public_key().public_numbers()
            _jwk = {
                "crv": "P-256", "kty": "EC",
                "x": _b64url(nums.x.to_bytes(32, "big")),
                "y": _b64url(nums.y.to_bytes(32, "big")),
            }
        return _key, _jwk


def _dpop_token(method: str, url: str) -> str:
    key, jwk = _ensure_key()
    header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": jwk}
    payload = {"iat": int(time.time()), "jti": str(uuid.uuid4()),
               "htu": url, "htm": method, "uuid": str(uuid.uuid4())}
    signing_input = (_b64url(json.dumps(header, separators=(",", ":")).encode())
                     + "." + _b64url(json.dumps(payload, separators=(",", ":")).encode()))
    r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    # DER → r||s 各32バイトの raw 形式（JWS ES256）
    return signing_input + "." + _b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def _build_request_body(keyword: str) -> dict:
    return {
        "userId": "", "pageSize": PAGE_SIZE, "pageToken": "",
        "searchSessionId": uuid.uuid4().hex,
        "indexRouting": "INDEX_ROUTING_UNSPECIFIED", "thumbnailTypes": [],
        "searchCondition": {
            "keyword": keyword, "excludeKeyword": "",
            "sort": "SORT_SCORE", "order": "ORDER_DESC",
            "status": ["STATUS_ON_SALE"],
            "sizeId": [], "categoryId": [CATEGORY_ID_YUGIOH_SINGLE], "brandId": [],
            "sellerId": [], "priceMin": 0, "priceMax": 0, "itemConditionId": [],
            "shippingPayerId": [], "shippingFromArea": [], "shippingMethod": [],
            "colorId": [], "hasCoupon": False, "attributes": [],
            "itemTypes": [], "skuIds": [],
        },
        "defaultDatasets": ["DATASET_TYPE_MERCARI", "DATASET_TYPE_BEYOND"],
        "serviceFrom": "suruga", "withItemBrand": True, "withItemSize": False,
        "withItemPromotions": True, "withItemSizes": True, "withShopname": False,
    }


def fetch_raw(keyword: str) -> dict:
    """検索APIを1回叩いて生レスポンス(dict)を返す。失敗は MercariFetchError。"""
    try:
        resp = requests.post(
            SEARCH_API_URL, json=_build_request_body(keyword),
            headers={"DPoP": _dpop_token("POST", SEARCH_API_URL), "X-Platform": "web",
                     "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            timeout=REQUEST_TIMEOUT_SEC,
        )
    except requests.RequestException as e:
        raise MercariFetchError(f"通信失敗: {type(e).__name__}") from e
    if resp.status_code != 200:
        raise MercariFetchError(f"HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as e:
        raise MercariFetchError("JSON不正") from e
    if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
        raise MercariFetchError("レスポンス形式不正")
    return data


# ── リンク ──

def build_search_url(card_name: str) -> str:
    """「メルカリで見る」の遷移先。アフィリエイト化する場合はこの関数だけ差し替える。"""
    return ("https://jp.mercari.com/search?keyword=" + quote(card_name, safe="")
            + "&status=on_sale&category_id=" + str(CATEGORY_ID_YUGIOH_SINGLE))


# ── 絞り込み ──

def _norm(text: str) -> str:
    """NFKC（全角半角の統一。scraper.normalize_width と同じ）。"""
    return unicodedata.normalize("NFKC", text or "")


def _name_key(text: str) -> str:
    """カード名照合用キー: NFKC・小文字化し、空白と区切り記号（scraper の
    _FLEX_SEP_ALL＝中黒・ハイフン・各種括弧記号など）を落とす。"""
    import scraper
    t = _norm(text).lower()
    return "".join(c for c in t if not c.isspace() and c not in scraper._FLEX_SEP_ALL)


def build_name_matcher(card_name: str, items: list[dict],
                       known_names=None) -> Callable[[str], bool]:
    """出品名が「そのカード単品」を指しているかを判定する関数を作る。

    scraper.is_target_card は「商品名がカード名で始まる」店舗商品名向けで、メルカリの
    自由な題名（「遊戯王 灰流うらら ウルトラ」等）は前置きの日本語で全件弾かれるため
    流用できない（実データで確認）。代わりに:
      1. カード名が題名に含まれる（全角半角・区切り記号の差は無視）
      2. known_names（全カード名）が渡されたとき:
         a. カード名を含む別の長い名前（例: ブラック・マジシャン → …・ガール）の一部に
            なっているだけの出現は数えない
         b. 題名に別のカード名（4文字以上）も並んでいれば詰め合わせとみなして除外
    """
    key = _name_key(card_name)
    supers: list[str] = []
    others: list[str] = []
    if known_names:
        all_titles = "\n".join(_name_key(it.get("name") or "") for it in items)
        for n in known_names:
            nk = _name_key(n)
            if not nk or nk == key:
                continue
            if key and key in nk:
                supers.append(nk)
            elif len(nk) >= 4 and nk not in key and nk in all_titles:
                others.append(nk)
        supers = list(dict.fromkeys(supers))
        others = list(dict.fromkeys(others))

    def matches(title: str) -> bool:
        tk = _name_key(title)
        if not key or key not in tk:
            return False
        if any(o in tk for o in others):
            return False
        if supers:
            covered = []
            for sup in supers:
                start = tk.find(sup)
                while start != -1:
                    covered.append((start, start + len(sup)))
                    start = tk.find(sup, start + 1)
            pos = tk.find(key)
            while pos != -1:
                end = pos + len(key)
                if not any(cs <= pos and end <= ce for cs, ce in covered):
                    return True  # 単独で現れる出現が1つでもあればよい
                pos = tk.find(key, pos + 1)
            return False
        return True

    return matches


def exclusion_reason(card_name: str, item: dict,
                     name_matches: Callable[[str], bool] | None = None) -> str | None:
    """除外すべき出品なら理由、対象ならNone。name_matches 省略時は build_name_matcher の
    最小形（含有のみ）を使う。"""
    name = item.get("name") or ""
    if item.get("auction"):
        return "auction"
    if item.get("status") not in (None, "", "ITEM_STATUS_ON_SALE", "STATUS_ON_SALE"):
        return "not_on_sale"
    try:
        price = int(item.get("price"))
    except (TypeError, ValueError):
        return "bad_price"
    if price <= 0:
        return "bad_price"
    if name_matches is None:
        name_matches = build_name_matcher(card_name, [item])
    if not name_matches(name):
        return "name_mismatch"
    # 注意書き類の語がカード名自体に含まれる場合（例: 「…セット」）に誤爆しないよう、
    # カード名部分を除いた残りで判定する
    import scraper
    residual = _norm(name)
    flex = scraper._build_flex_pattern(_norm(card_name))
    if flex:
        residual = re.sub(flex, " ", residual, flags=re.IGNORECASE)
    upper = residual.upper()
    if GRADED_PATTERN.search(upper):
        return "graded"
    if MULTI_PATTERN.search(upper):
        return "multi"
    if OVERSEAS_PATTERN.search(upper):
        return "overseas"
    if SUPPLY_PATTERN.search(upper):
        return "supply"
    if NON_OCG_PATTERN.search(upper):
        return "non_ocg"
    if scraper._is_rush_duel(_norm(name)):
        return "rush_duel"
    return None


# ── レアリティ判定 ──

_CODE_IN_TEXT = re.compile(r"[A-Za-z0-9]{2,6}-[A-Za-z]{2}[A-Za-z]?\d{2,4}")


def extract_code(item_name: str) -> str | None:
    """商品名から型番（RC04-JP009 形式）を1つ取り出す。複数あれば None（曖昧）。"""
    norm = unicodedata.normalize("NFKC", item_name or "").upper()
    codes = {m.group(0) for m in _CODE_IN_TEXT.finditer(norm) if looks_like_card_code(m.group(0))}
    return next(iter(codes)) if len(codes) == 1 else None


def _text_for_rarity(card_name: str, item_name: str) -> str:
    """レアリティ判定にかける文字列。カード名と型番は先に取り除く
    （カード名の一部や型番の英字を略号と取り違えないため）。"""
    text = unicodedata.normalize("NFKC", item_name or "")
    norm_card = unicodedata.normalize("NFKC", card_name or "")
    if norm_card:
        text = re.sub(re.escape(norm_card), " ", text, flags=re.IGNORECASE)
    text = _CODE_IN_TEXT.sub(" ", text)
    return text


def classify_rarity(card_name: str, item_name: str,
                    code_rarity_lookup: Callable[[], dict[str, set[str]]] | None = None) -> str | None:
    """出品名からレアリティ(正規名)を判定する。判定できなければ None。

    1. 商品名のレアリティ表記
    2. だめなら、型番が商品名にあり、そのカードで型番→レアリティが1種類にしか
       対応しないときだけ補う（複数対応の型番は補わない）
    """
    r = _rarity.extract_rarity_from_text(_text_for_rarity(card_name, item_name))
    if r:
        return r
    if code_rarity_lookup is None:
        return None
    code = extract_code(item_name)
    if not code:
        return None
    try:
        mapping = code_rarity_lookup()
    except Exception as e:  # 補完はあくまで補助。失敗しても集計自体は続ける
        logger.warning("[mercari] 型番補完の取得失敗: %s", e)
        return None
    rarities = mapping.get(code)
    if rarities and len(rarities) == 1:
        return next(iter(rarities))
    return None


# ── 集計 ──

def aggregate(card_name: str, items: list[dict],
              code_rarity_lookup: Callable[[], dict[str, set[str]]] | None = None,
              known_names=None) -> dict:
    """生の出品リストを除外→レアリティ判定→レアリティ別集計にする（通信なし）。"""
    groups: dict[str, list[int]] = {}
    unclassified = 0
    considered = 0
    # 型番補完の問い合わせはカード名単位で1回に抑える（遅延評価＋メモ化）
    memo: dict = {}

    def lookup():
        if "v" not in memo:
            memo["v"] = code_rarity_lookup() if code_rarity_lookup else {}
        return memo["v"]

    matcher = build_name_matcher(card_name, items, known_names)
    for it in items:
        if exclusion_reason(card_name, it, matcher):
            continue
        considered += 1
        price = int(it["price"])
        r = classify_rarity(card_name, it.get("name") or "", lookup if code_rarity_lookup else None)
        if r is None:
            unclassified += 1
            continue
        groups.setdefault(r, []).append(price)
    by_rarity: dict[str, dict] = {}
    for r in sorted(groups, key=_rarity.order_of):
        prices = groups[r]
        mn = min(prices)
        by_rarity[r] = {
            "count": len(prices),
            "min": mn,
            "median": int(statistics.median(prices)) if len(prices) >= MERCARI_MEDIAN_MIN_COUNT else None,
            "min_is_floor": mn == MERCARI_MIN_LISTING_PRICE,
        }
    return {"by_rarity": by_rarity, "unclassified_count": unclassified, "total_considered": considered}


# ── 型番→レアリティ（Supabase 価格履歴ビュー） ──

_code_map_cache: dict[str, tuple[float, dict]] = {}
_code_map_lock = threading.Lock()
CODE_MAP_TTL_SEC = 6 * 3600  # TODO: calibrate from data（夜間収集で日次にしか変わらない）


def load_code_rarity_map(client, card_name: str) -> dict[str, set[str]]:
    """カード名の 型番→{レアリティ} を直近90日の価格履歴から作る。

    price_history ビューは price_history_v2 を card_dim / rarity_dim と結合したもの
    （/api/price-history と同じ読み方）。型番として正しい形のものだけ使う
    （カーナベルの弾略号・トレコロの数値IDは除く）。1000行上限を超えるため
    order 付き range でページ送りし、上限ページに達して打ち切られた場合は
    「一意」の判定が信用できないので空を返す（補完しない）。
    カード名単位でキャッシュする。
    """
    now = time.time()
    with _code_map_lock:
        hit = _code_map_cache.get(card_name)
        if hit and now - hit[0] < CODE_MAP_TTL_SEC:
            return hit[1]
    cutoff = (datetime.now(JST) - timedelta(days=CODE_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    page_size = 1000
    offset = 0
    mapping: dict[str, set[str]] = {}
    truncated = False
    while True:
        resp = (client.table("price_history")
                .select("rarity, code, recorded_at")
                .eq("card_name", card_name)
                .gte("recorded_at", cutoff)
                .neq("code", "")
                .order("recorded_at", desc=True)
                .order("shop", desc=False)
                .order("rarity", desc=False)
                .range(offset, offset + page_size - 1)
                .execute())
        batch = resp.data or []
        for row in batch:
            code = unicodedata.normalize("NFKC", str(row.get("code") or "")).strip().upper()
            if not looks_like_card_code(code):
                continue
            canon = _rarity.normalize_rarity(row.get("rarity") or "")
            if not canon or canon == _rarity.UNKNOWN_RARITY_LABEL:
                continue
            mapping.setdefault(code, set()).add(canon)
        if len(batch) < page_size:
            break
        offset += page_size
        if offset >= CODE_MAX_PAGES * page_size:
            truncated = True
            logger.warning("[mercari] 型番補完: %dページで打ち切り [%s] 補完しない", CODE_MAX_PAGES, card_name)
            break
    if truncated:
        mapping = {}
    with _code_map_lock:
        if len(_code_map_cache) >= CACHE_MAX_ENTRIES:
            _code_map_cache.clear()
        _code_map_cache[card_name] = (now, mapping)
    return mapping


# ── 公開: キャッシュ付き取得 ──

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def get_reference(card_name: str, supabase_client=None, known_names=None) -> dict:
    """/api/mercari の本体。成功: {"ok":True,...,"cached":bool} / 失敗: {"ok":False,"error":"fetch_failed"}。

    失敗結果はキャッシュしない。supabase_client が None なら型番補完をスキップする。
    """
    key = unicodedata.normalize("NFKC", card_name).strip()
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_SEC:
            return {**hit[1], "cached": True}
    try:
        raw = fetch_raw(card_name)
    except MercariFetchError as e:
        logger.warning("[mercari] 取得失敗 [%s]: %s", card_name, e)
        return {"ok": False, "error": "fetch_failed"}
    lookup = None
    if supabase_client is not None:
        lookup = lambda: load_code_rarity_map(supabase_client, card_name)  # noqa: E731
    try:
        agg = aggregate(card_name, raw.get("items", []), lookup, known_names)
    except Exception as e:
        logger.error("[mercari] 集計失敗 [%s]: %s", card_name, e)
        return {"ok": False, "error": "fetch_failed"}
    result = {"ok": True, **agg, "search_url": build_search_url(card_name)}
    with _cache_lock:
        if len(_cache) >= CACHE_MAX_ENTRIES:
            _cache.clear()
        _cache[key] = (now, result)
    return {**result, "cached": False}
