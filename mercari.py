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
import math
import os
import re
import statistics
import collections
import threading
import time
import unicodedata
import uuid
from typing import Callable
from urllib.parse import quote

import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

import rarity as _rarity

logger = logging.getLogger(__name__)

SEARCH_API_URL = "https://api.mercari.jp/v2/entities:search"
CATEGORY_ID_YUGIOH_SINGLE = 1152  # 遊戯王シングル（プレイマット等は別カテゴリで除ける）
PAGE_SIZE = 120
REQUEST_CONNECT_TIMEOUT_SEC = 5
REQUEST_READ_TIMEOUT_SEC = 5  # 1回の受信待ち。合計は REQUEST_TOTAL_TIMEOUT_SEC で打ち切る
REQUEST_TOTAL_TIMEOUT_SEC = 15  # TODO: calibrate from data（接続〜本文受信の合計上限）

# 負荷対策（プロセス単位。gunicornワーカーごとに効く）
# TODO: calibrate from data
MAX_CONCURRENT_FETCHES = 2   # メルカリへの同時リクエスト上限。空きが無ければ待たずに失敗応答
MAX_FETCHES_PER_MIN = 20     # メルカリへの実リクエスト数の毎分上限（キャッシュ命中は数えない）
# 同一カードの取得中に合流して待つ上限。取得側の最悪時間（合計15秒＋最後の受信待ち5秒）と
# 集計の余裕を足した値にして、取得が終わる前に待ち手だけ失敗にならないようにする
SINGLE_FLIGHT_WAIT_SEC = REQUEST_TOTAL_TIMEOUT_SEC + REQUEST_READ_TIMEOUT_SEC + 10
MAX_SINGLE_FLIGHT_WAITERS = 8  # 1つの取得に合流できる待ち手の数。超えたら即 busy
MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 受信本文の上限。超えたら取得失敗

CACHE_TTL_SEC = 15 * 60  # TODO: calibrate from data
CACHE_MAX_ENTRIES = 500
MERCARI_MEDIAN_MIN_COUNT = 3  # TODO: calibrate from data（これ未満は中央値を出さない）
MERCARI_MIN_LISTING_PRICE = 300  # メルカリの最低出品価格。min がこれなら「下限張り付き」

# ── 除外規則（商品名） ──
# TODO: calibrate from data（実データで除外漏れ・過剰除外を確認しながら増減する）
GRADED_PATTERN = re.compile(r"(?<![A-Z])(?:PSA|BGS|ARS|CGC)(?![A-Z])|鑑定")
MULTI_PATTERN = re.compile(
    r"(?<![0-9A-Z])(?:[2-9]|[1-9][0-9]+)\s*枚(?!目)|[二三四五六七八九十]\s*枚(?!目)"  # 「2枚」「10枚」「三枚」（「1枚」は単品）
    r"|セット|まとめ|計\s*[0-9]+\s*枚(?!目)|大量"
    r"|[×✖]️?\s*(?:[2-9]|[1-9][0-9]+)|(?<![A-Z0-9])X\s*(?:[2-9]|[1-9][0-9]+)"  # 「×2」「x3」（EX2 等は除く）
    r"|[2-9]\s*体"             # 「3体」
)
# 同一カードのセット（2〜3枚組）として拾う条件。TODO: calibrate from data
SET_QTY_CHOICES = (2, 3)  # 1枚あたり価格を出すセット枚数。4枚以上は拾わない
_KANJI_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
# 枚数表記: 「N枚」「×N」「xN」（全角数字は NFKC 済み。漢数字は一〜十）
_QTY_PATTERNS = (
    # 漢数字は連なり（「十二」）ごと読む。「N枚目」は枚数ではない（「2枚目以降送料無料」）
    re.compile(r"(?<![0-9A-Z一二三四五六七八九十])(\d+|[一二三四五六七八九十]+)\s*枚(?!目)"),
    re.compile(r"[×✖]️?\s*(\d+)"),
    re.compile(r"(?<![A-Z0-9])X\s*(\d+)"),
)
# これがあるセットは「同じカードだけの組」と言えないので従来どおり除外
# 幅表記（「2〜3枚」「2-3枚」「2~3枚」）は枚数が確定しないので対象外
SET_DISQUALIFY_PATTERN = re.compile(
    r"各|計|まとめ|他|関連|デッキ|パーツ|詰め合わせ|大量|\d\s*体"
    r"|[0-9一二三四五六七八九十]\s*[〜~\-–]\s*[0-9一二三四五六七八九十]\s*枚"
)

# 専用出品。実際の形だけを見る: 「様専用」「様用」、否定形でない「専用」、
# 題名先頭の「〇〇様」（伏せ字含む「ボ*ブ様 …」）。「王様」「神様」「お客様」「皆様」「異様」
# 「専用ではありません」等は除外しない（題名先頭の「様」は下の否定先読みで通常語を外す）
DEDICATED_PATTERN = re.compile(
    r"様専用|様用|専用(?!ではあり|じゃあり|では無|ではな)"
    r"|^(?!お客様|皆様|神様|王様|異様|同様|多様|模様)\S{1,10}様[\s　]"
)
# 単品に付属品が付く表記（サプライ扱いにしない）
ACCESSORY_NOTE_PATTERN = re.compile(r"(?:スリーブ|ローダー|トップローダー|ケース)\s*(?:付き?|入り?)")
# 他言語版の除外。「日版」「日本版」は除外しない。「亜」単独は誤爆しやすいので版・レアリティ略称の前だけ見る
# 型番の地域コード（BLZD-EN024 等）は日本語版の -JP と区別できるので除外する。TODO: calibrate from data
OVERSEAS_PATTERN = re.compile(
    r"英語|アジア|韓国|海外|北米|EU版|(?<![A-Z])EU(?![A-Z])|欧州"
    r"|泰亜|亜版|亜(?=シク|ウル|スー|レア)"
    r"|英版|英字(?!なし|無し)|(?<![A-Z])ENG(?![A-Z])|ENGLISH|中国|中文|繁体|繁體|簡体|簡體|简体|台湾|香港|TCG版"
    r"|(?<![A-Z0-9])[A-Z0-9]{2,6}-(?:EN|KR|AE|SC|TC|DE|FR|IT|SP|PT|EU)\d"
)
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
        # これが無いとオークション出品でも auction が null で返り、evaluate_listing の
        # オークション除外が効かず途中の入札額が相場に混ざる（2026-10-31 実応答で確認）
        "withAuction": True,
    }


def fetch_raw(keyword: str) -> dict:
    """検索APIを1回叩いて生レスポンス(dict)を返す。失敗は MercariFetchError。"""
    deadline = time.monotonic() + REQUEST_TOTAL_TIMEOUT_SEC
    resp = None
    try:
        resp = requests.post(
            SEARCH_API_URL, json=_build_request_body(keyword),
            headers={"DPoP": _dpop_token("POST", SEARCH_API_URL), "X-Platform": "web",
                     "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            timeout=(REQUEST_CONNECT_TIMEOUT_SEC, REQUEST_READ_TIMEOUT_SEC), stream=True,
        )
        if resp.status_code != 200:
            raise MercariFetchError(f"HTTP {resp.status_code}")
        # 本文は少しずつ読み、合計時間が上限を超えたら打ち切る（読み取りタイムアウトは
        # 1回の待ちにしか効かず、少しずつ送られ続けると合計で長引くため）
        chunks = []
        received = 0
        for chunk in resp.iter_content(65536):
            chunks.append(chunk)
            received += len(chunk)
            if received > MAX_RESPONSE_BYTES:
                raise MercariFetchError("応答が大きすぎる")
            if time.monotonic() > deadline:
                raise MercariFetchError("合計タイムアウト")
        if time.monotonic() > deadline:
            raise MercariFetchError("合計タイムアウト")
        data = json.loads(b"".join(chunks).decode("utf-8"))
    except requests.RequestException as e:
        raise MercariFetchError(f"通信失敗: {type(e).__name__}") from e
    except (ValueError, UnicodeDecodeError) as e:
        raise MercariFetchError("JSON不正") from e
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        # items が無い応答は「0件」ではなく形式不正。0件成功としてキャッシュしない
        raise MercariFetchError("レスポンス形式不正")
    return data


# ── リンク ──

_AFID_WARNED = False  # 不正値のWARNを1回だけ出すためのフラグ


def get_afid() -> str:
    """メルカリアンバサダーの識別子。環境変数 MERCARI_AFID（数字のみ）を呼び出しごとに読む
    （テストで差し替えやすくするため）。未設定・空・不正値は空文字（＝アフィリエイトなし）。"""
    global _AFID_WARNED
    raw = os.environ.get("MERCARI_AFID", "").strip()
    if not raw:
        return ""
    if raw.isascii() and raw.isdigit():
        return raw
    if not _AFID_WARNED:
        _AFID_WARNED = True
        logger.warning("[mercari] MERCARI_AFID が数字のみではないため無視します（アフィリエイトなしで動作）")
    return ""


def build_search_url(card_name: str) -> str:
    """「メルカリで見る」の遷移先。MERCARI_AFID があればアンバサダー用の afid を付ける。"""
    url = ("https://jp.mercari.com/search?keyword=" + quote(card_name, safe="")
           + "&status=on_sale&category_id=" + str(CATEGORY_ID_YUGIOH_SINGLE))
    afid = get_afid()
    if afid:
        url += "&afid=" + afid
    return url


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


# セット判定で「別カードあり」とみなす短いカード名の最大長（2〜3文字）。TODO: calibrate from data
SHORT_NAME_MAX_LEN = 3
# 短いカード名の独立トークン判定の区切り（空白・全角空白・「・」「、」「,」「/」「＆」「&」・各種括弧）
_TOKEN_SPLIT_RE = re.compile(r"[\s　・、,/／&＆【】「」『』()（）\[\]〔〕]+")


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
    返す関数には属性 has_short_other_card(title) も付く。セット判定専用で、2〜3文字の短い
    カード名（「天罰」等）が区切りで挟まれた独立トークンとして並んでいるかを返す
    （単品判定では使わない。短い名前は一般語と衝突しやすいため、セット判定だけに限定）。
    """
    key = _name_key(card_name)
    supers: list[str] = []
    others: list[str] = []
    short_others: set[str] = set()
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
            elif 2 <= len(nk) <= SHORT_NAME_MAX_LEN and nk not in key and nk in all_titles:
                short_others.add(nk)
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

    def has_short_other_card(title: str) -> bool:
        if not short_others:
            return False
        for token in _TOKEN_SPLIT_RE.split(_norm(title)):
            if token and _name_key(token) in short_others:
                return True
        return False

    matches.has_short_other_card = has_short_other_card
    return matches


def same_card_set_qty(text: str) -> int | None:
    """題名（カード名除去・NFKC・大文字化済み）が「同じカードだけの2〜3枚組」なら枚数、
    そうでなければ None。

    枚数は「N枚」「N枚セット」「N枚組」「×N」「xN」から読む。次のどれかなら None
    （従来どおり複数枚として除外）: 4枚以上／枚数表記が複数で食い違う／枚数が読めない
    （「セット」だけ）／「各」「計」「まとめ」「他」「関連」「デッキ」「パーツ」「詰め合わせ」
    「N体」。他カード名の混在は呼び出し側（build_name_matcher）で先に除外される。
    """
    if SET_DISQUALIFY_PATTERN.search(text):
        return None
    qtys = set()
    for pat in _QTY_PATTERNS:
        for m in pat.finditer(text):
            g = m.group(1)
            if g.isdigit():
                qtys.add(int(g))
            else:
                # 漢数字: 1文字なら一〜九、「十」を含む・複数文字（十二等）は10以上として扱う
                qtys.add(_KANJI_NUM[g] if len(g) == 1 and g != "十" else 99)
    if len(qtys) != 1:
        return None
    q = next(iter(qtys))
    return q if q in SET_QTY_CHOICES else None


def exclusion_reason(card_name: str, item: dict,
                     name_matches: Callable[[str], bool] | None = None) -> str | None:
    """除外すべき出品なら理由、対象ならNone（複数枚は同一カードのセットも「multi」で除外）。
    name_matches 省略時は build_name_matcher の最小形（含有のみ）を使う。"""
    return evaluate_listing(card_name, item, name_matches, allow_set=False)[0]


def evaluate_listing(card_name: str, item: dict,
                     name_matches: Callable[[str], bool] | None = None,
                     allow_set: bool = True) -> tuple[str | None, int]:
    """出品を評価して (除外理由, 枚数) を返す。除外なら理由が入り、対象なら理由は None で
    枚数は 1（単品）または 2〜3（同一カードのセット。allow_set=True のときだけ）。"""
    name = item.get("name") or ""
    if item.get("auction"):
        return "auction", 1
    if item.get("status") not in (None, "", "ITEM_STATUS_ON_SALE", "STATUS_ON_SALE"):
        return "not_on_sale", 1
    try:
        price = int(item.get("price"))
    except (TypeError, ValueError):
        return "bad_price", 1
    if price <= 0:
        return "bad_price", 1
    if name_matches is None:
        name_matches = build_name_matcher(card_name, [item])
    if not name_matches(name):
        return "name_mismatch", 1
    # 注意書き類の語がカード名自体に含まれる場合（例: 「…セット」）に誤爆しないよう、
    # カード名部分を除いた残りで判定する
    import scraper
    residual = _norm(name)
    flex = scraper._build_flex_pattern(_norm(card_name))
    if flex:
        residual = re.sub(flex, " ", residual, flags=re.IGNORECASE)
    upper = residual.upper()
    if GRADED_PATTERN.search(upper):
        return "graded", 1
    set_qty = 1
    if MULTI_PATTERN.search(upper):
        set_qty = same_card_set_qty(upper) if allow_set else None
        if set_qty is None:
            return "multi", 1
    if OVERSEAS_PATTERN.search(upper):
        return "overseas", 1
    if DEDICATED_PATTERN.search(residual) or DEDICATED_PATTERN.search(_norm(name).strip()):
        return "dedicated", 1
    # 「スリーブ付き」等は単品の付属表記なのでサプライ扱いにしない
    if SUPPLY_PATTERN.search(ACCESSORY_NOTE_PATTERN.sub(" ", upper)):
        return "supply", 1
    if NON_OCG_PATTERN.search(upper):
        return "non_ocg", 1
    if scraper._is_rush_duel(_norm(name)):
        return "rush_duel", 1
    if set_qty > 1:
        # セットは他カードの組を除くため、短いカード名（「天罰」等）の独立トークンも見る
        check = getattr(name_matches, "has_short_other_card", None)
        if check is not None and check(name):
            return "name_mismatch", 1
    return None, set_qty


# ── レアリティ判定 ──

_CODE_IN_TEXT = re.compile(r"[A-Za-z0-9]{2,6}-[A-Za-z]{2}[A-Za-z]?\d{2,4}")


def _text_for_rarity(card_name: str, item_name: str) -> str:
    """レアリティ判定にかける文字列。カード名と型番は先に取り除く
    （カード名の一部や型番の英字を略号と取り違えないため）。
    カード名は exclusion_reason と同じ区切り無視の照合（scraper._build_flex_pattern）で除く。
    「ソーラーフレアドラゴン」のように中黒・空白を省いた出品名でも、カード名の一部
    （「フレア」「チャージ」等）がレアリティと判定されないようにするため。"""
    import scraper
    text = _norm(item_name)
    flex = scraper._build_flex_pattern(_norm(card_name))
    if flex:
        text = re.sub(flex, " ", text, flags=re.IGNORECASE)
    text = _CODE_IN_TEXT.sub(" ", text)
    return text


def classify_rarity(card_name: str, item_name: str) -> str | None:
    """出品名のレアリティ表記から正規名を判定する。判定できなければ None。

    型番からの補完は行わない（2026-10-13 不採用。docs/decisions.md 参照）。
    """
    return _rarity.extract_rarity_from_text(_text_for_rarity(card_name, item_name))


# ── 集計 ──

def _half_up(x: float) -> int:
    """四捨五入（偶数丸めにしない）。"""
    return int(math.floor(x + 0.5))


def aggregate(card_name: str, items: list[dict], known_names=None) -> dict:
    """生の出品リストを除外→レアリティ判定→レアリティ別集計にする（通信なし）。

    単品の統計（count/min/median）と、同じカードの2〜3枚組（セット）の
    1枚あたり価格 set={unit_min, unit_min_qty, count} は別々に持つ（セットは単品に混ぜない）。
    """
    singles: dict[str, list[int]] = {}
    sets: dict[str, list[tuple[int, int, int]]] = {}  # レアリティ → [(1枚あたり価格, 枚数, 組の総額)]
    unclassified_single = unclassified_set = 0
    considered = 0
    matcher = build_name_matcher(card_name, items, known_names)
    for it in items:
        reason, qty = evaluate_listing(card_name, it, matcher)
        if reason:
            continue
        considered += 1
        price = int(it["price"])
        r = classify_rarity(card_name, it.get("name") or "")
        if r is None:
            if qty == 1:
                unclassified_single += 1
            else:
                unclassified_set += 1
            continue
        if qty == 1:
            singles.setdefault(r, []).append(price)
        else:
            sets.setdefault(r, []).append((_half_up(price / qty), qty, price))
    by_rarity: dict[str, dict] = {}
    for r in sorted(set(singles) | set(sets), key=_rarity.order_of):
        prices = singles.get(r, [])
        mn = min(prices) if prices else None
        unit = sets.get(r)
        by_rarity[r] = {
            "count": len(prices),
            "min": mn,
            "median": (_half_up(statistics.median(prices))
                       if len(prices) >= MERCARI_MEDIAN_MIN_COUNT else None),
            "min_is_floor": mn == MERCARI_MIN_LISTING_PRICE,
            # 同額なら枚数の多い方（3枚組）を優先
            "set": ({"unit_min": min(unit, key=lambda t: (t[0], -t[1]))[0],
                     "unit_min_qty": min(unit, key=lambda t: (t[0], -t[1]))[1],
                     # 最安の組の総額（1枚あたり×枚数だと端数がずれるため実際の出品価格を返す）
                     "unit_min_total": min(unit, key=lambda t: (t[0], -t[1]))[2],
                     "count": len(unit)} if unit else None),
        }
    return {"by_rarity": by_rarity,
            "unclassified_count": unclassified_single + unclassified_set,
            "unclassified_single": unclassified_single,
            "unclassified_set": unclassified_set,
            "total_considered": considered}


# ── 公開: キャッシュ・負荷対策つき取得 ──

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()

# 負荷対策の状態（プロセス単位）
_fetch_slots = threading.BoundedSemaphore(MAX_CONCURRENT_FETCHES)
_fetch_times: "collections.deque[float]" = collections.deque()
_fetch_times_lock = threading.Lock()
_inflight: dict[str, "_Flight"] = {}
_inflight_lock = threading.Lock()

BUSY_RESULT = {"ok": False, "error": "busy"}
FAILED_RESULT = {"ok": False, "error": "fetch_failed"}


class _Flight:
    """同じカードの取得を1本にまとめる（single-flight）ための待ち合わせ。"""
    def __init__(self):
        self.event = threading.Event()
        self.result: dict | None = None
        self.waiters = 0


def _take_minute_budget(now: float) -> bool:
    """直近60秒の実リクエスト数が上限未満なら1回分を消費して True。"""
    with _fetch_times_lock:
        while _fetch_times and now - _fetch_times[0] >= 60:
            _fetch_times.popleft()
        if len(_fetch_times) >= MAX_FETCHES_PER_MIN:
            return False
        _fetch_times.append(now)
        return True


def _reset_load_state():
    """テスト専用: キャッシュ以外の負荷対策の状態を初期化する。本番コードから呼ばないこと
    （進行中の取得の待ち合わせや上限の記録を消してしまうため）。"""
    global _fetch_slots
    _fetch_slots = threading.BoundedSemaphore(MAX_CONCURRENT_FETCHES)
    with _fetch_times_lock:
        _fetch_times.clear()
    with _inflight_lock:
        _inflight.clear()


def _fetch_and_aggregate(card_name: str, known_names) -> dict:
    """上限チェック→取得→集計。成功/失敗の結果dict(キャッシュ前)を返す。"""
    # 同時実行枠を取れてから毎分枠を消費する（busy で終わる要求が毎分枠を食わないように）
    if not _fetch_slots.acquire(blocking=False):
        logger.warning("[mercari] 同時実行上限(%d)超過 [%s]", MAX_CONCURRENT_FETCHES, card_name)
        return BUSY_RESULT
    if not _take_minute_budget(time.time()):
        _fetch_slots.release()
        logger.warning("[mercari] 毎分上限(%d)超過 [%s]", MAX_FETCHES_PER_MIN, card_name)
        return BUSY_RESULT
    try:
        raw = fetch_raw(card_name)
    except MercariFetchError as e:
        logger.warning("[mercari] 取得失敗 [%s]: %s", card_name, e)
        return FAILED_RESULT
    finally:
        _fetch_slots.release()
    items = raw.get("items", [])
    try:
        agg = aggregate(card_name, items, known_names)
    except Exception as e:
        logger.error("[mercari] 集計失敗 [%s]: %s", card_name, e)
        return FAILED_RESULT
    try:
        num_found = int((raw.get("meta") or {}).get("numFound"))
    except (TypeError, ValueError):
        num_found = None
    # search_url / affiliate はキャッシュに入れず get_reference が返却時に付ける
    return {"ok": True, **agg, "fetched_count": len(items), "num_found": num_found}


def get_reference(card_name: str, known_names=None) -> dict:
    """/api/mercari の本体。成功時は search_url / affiliate を返却時に組み立てて付ける
    （キャッシュ値には含めない＝環境変数の変更が15分キャッシュに残らない）。"""
    res = _get_reference_cached(card_name, known_names)
    if res.get("ok"):
        return {**res, "search_url": build_search_url(card_name), "affiliate": bool(get_afid())}
    return res


def _get_reference_cached(card_name: str, known_names=None) -> dict:
    """キャッシュ・負荷対策つき取得の本体。

    成功: {"ok":True,...,"cached":bool}
    失敗: {"ok":False,"error":"fetch_failed"}（取得失敗）/ {"ok":False,"error":"busy"}（負荷制限）
    失敗結果はキャッシュしない。DBには一切問い合わせない（型番補完は不採用）。
    同じカードの取得が進行中なら、新しく取りに行かずその結果を待って共有する。
    """
    key = unicodedata.normalize("NFKC", card_name).strip()
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_SEC:
            return {**hit[1], "cached": True}
    with _inflight_lock:
        flight = _inflight.get(key)
        leader = flight is None
        if leader:
            flight = _inflight[key] = _Flight()
        elif flight.waiters >= MAX_SINGLE_FLIGHT_WAITERS:
            return BUSY_RESULT   # 待ち手が多すぎる。待たせず失敗応答
        else:
            flight.waiters += 1
    if not leader:
        if flight.event.wait(SINGLE_FLIGHT_WAIT_SEC) and flight.result is not None:
            res = flight.result
            return {**res, "cached": True} if res.get("ok") else res
        return FAILED_RESULT
    result = FAILED_RESULT
    try:
        result = _fetch_and_aggregate(card_name, known_names)
        if result.get("ok"):
            with _cache_lock:
                if len(_cache) >= CACHE_MAX_ENTRIES:
                    _cache.clear()
                _cache[key] = (now, result)
    finally:
        flight.result = result
        flight.event.set()
        with _inflight_lock:
            _inflight.pop(key, None)
    return {**result, "cached": False} if result.get("ok") else result
