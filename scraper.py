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
import unicodedata
import urllib.parse
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from pathlib import Path
from rarity import normalize_rarity  # 正規化は rarity.py に一元化

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

def _normalize_fullwidth(text: str) -> str:
    """全角英数字・一部記号・互換文字（ローマ数字等）を半角に変換"""
    text = unicodedata.normalize('NFKC', text)  # ローマ数字(Ⅹ→X等)・全角ハイフン等を一括変換
    result = []
    for ch in text:
        cp = ord(ch)
        # 全角英数字（Ａ-Ｚ, ａ-ｚ, ０-９）→ 半角
        if 0xFF21 <= cp <= 0xFF3A or 0xFF41 <= cp <= 0xFF5A or 0xFF10 <= cp <= 0xFF19:
            result.append(chr(cp - 0xFEE0))
        # 全角スペース → 半角
        elif cp == 0x3000:
            result.append(' ')
        # 全角コロン → 半角
        elif cp == 0xFF1A:
            result.append(':')
        else:
            result.append(ch)
    return ''.join(result)


def _squeeze_spaces(s: str) -> str:
    """連続スペースを1つに畳み、前後のスペースを落とす（検索語生成の共通後処理）。

    記号をスペースへ置換すると、記号が連続する箇所や名前の末尾で余分なスペースが残る。
    店舗の検索エンジンはこれを別語として扱うことがあり、そのまま投げると0件になる。

    2026-08-03 実測（カード名15,522件中、圧縮で検索語が変わる101件を旧新で比較）:
      - カードラボ販売 124件 → 133件（'聖なるバリア －ミラーフォース－' 0→4件、
        '黒魔術のバリア －ミラーフォース－' 0→1件、'冀望郷－バリアン－' 0→4件）
      - 遊々亭 販売278/買取177・カードラボ買取18・まんぞく屋販売69 はいずれも増減ゼロ
      - 減少した経路・カードは1件も無い
    """
    return re.sub(r"\s+", " ", s).strip()


def _normalize_search_query(card_name: str) -> str:
    """検索クエリ用にカード名を正規化（全角英数→半角、中黒・ハイフン系・コロン→スペース）。

    遊々亭・カードラボの販売/買取とまんぞく屋販売の5経路が共有する。

    コロンを落とす根拠（2026-08-03 実測、コロンを含むカード名9件を残す/落とすで比較）:
      - 遊々亭販売 7件→16件、遊々亭買取 6件→29件、カードラボ買取 17件→18件
        （'I：Pマスカレーナ' は遊々亭販売0→23件・買取0→19件と丸ごと欠測していた）
      - カードラボ販売・まんぞく屋販売は増減ゼロ。減少した経路・カードは無い
      - 照合側（_FLEX_SEP / _FLEX_SPLIT）は元から「:」「：」を区切り扱いしており、
        検索語側だけが揃っていなかった
    """
    name = normalize_width(card_name)
    name = name.replace("・", " ").replace("　", " ")
    # ハイフン系記号・コロンをスペースに置換
    for ch in "-－―‐—–:：":
        name = name.replace(ch, " ")
    return _squeeze_spaces(name)


def _cardrush_search_query(card_name: str) -> str:
    """カードラッシュ用の検索語（販売・買取で共通）。

    カードラッシュは商品名を記号なしで登録しており、検索語に記号が残っていると
    ヒットしない。共通の _normalize_search_query に加えて _FLEX_SEP_CHARS の記号も
    スペースへ寄せる。

    コロン（: ：）は _normalize_search_query が落とすため、ここでは扱わない。
    2026-08-03 実測: 買取はコロンを残すとコロンを含む9カード全てが0件で、落とすと
    合計27件になる（'I：Pマスカレーナ' 0→17件、'S：Pリトルナイト' 0→6件）。
    販売は店舗側がコロンを無視するため増減ゼロだった（「EM：Pグレニャード」11件のまま）。

    2026-08-03 実測（照合修正と併用したときの取得件数）:
      - 販売: 検索語も直すと 253件 → 289件（照合のみの修正では届かない分がある）
        例「セリオンズ“キング”レギュラス」2件→24件、「Live☆Twin リィラ」4件→15件
      - 買取: 79件 → 109件。買取は検索語を直さないと0件のままのカードが多い
        （'炎舞－「天枢」'→0件 / '炎舞 天枢'→2件）
      - 他店（遊々亭・カードラボ・まんぞく屋・トレコロ）では増減ゼロだったため、
        共通関数ではなくカードラッシュ専用にして影響を閉じている
    """
    name = _normalize_search_query(card_name)
    for ch in _FLEX_SEP_CHARS:
        name = name.replace(ch, " ")
    return _squeeze_spaces(name)


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
    """全角英数字・互換文字（ローマ数字等）を半角に統一"""
    text = unicodedata.normalize('NFKC', text)  # ローマ数字(Ⅹ→X等)・全角ハイフン等を一括変換
    return text.translate(_ZEN2HAN)

def to_fullwidth_alnum(text: str) -> str:
    """英数字だけを全角に変換する（記号・空白は変換しない）。

    カーナベルESの card_name には全角で登録されたレコードが混在するため、
    半角の検索語だけでは一致しない。その対策で全角版の検索値を作るのに使う。

    記号まで全角化してはいけない。2026-08-03 実測: カーナベルには
    「Ｎｏ.１０４ 仮面魔踏士シャイニングＶ」のように英数字だけ全角でピリオドは
    半角という混在表記があり、ピリオドも全角化すると0件になる（英数字のみなら3件）。
    ワイルドカードのメタ文字（* ?）を変換しない点でも、半角版と意味を揃えられる。
    """
    return "".join(
        chr(ord(ch) + 0xFEE0)
        if ("0" <= ch <= "9" or "A" <= ch <= "Z" or "a" <= ch <= "z")
        else ch
        for ch in text
    )

def parse_price(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None

# ── 取得失敗の記録（「0件＝在庫なし」と「取得失敗」を区別するため） ──
# compare_prices が店舗ごとに別スレッドで実行するため、スレッドローカルで数える
import threading as _threading
_fetch_stats = _threading.local()

def _reset_fetch_errors():
    _fetch_stats.errors = 0

def _note_fetch_error():
    _fetch_stats.errors = getattr(_fetch_stats, "errors", 0) + 1

def _get_fetch_errors() -> int:
    return getattr(_fetch_stats, "errors", 0)

# ── HTTPセッションとホスト単位のサーキットブレーカ ──
# 2026-08-03 追加。カードラボ（www.c-labo-online.jp）が夜間収集の途中から
# 403 Forbidden を返し続ける問題への対処（調査結果は docs/decisions.md）。
#   ① セッション使い回し: 従来は毎リクエストが新しい PHPSESSID を発行させており、
#      1晩に数千個のセッションを店舗側に作らせていた。接続の再利用も効く
#   ② 403 は再試行しない: 通らないうえ店舗への負荷を2倍にするだけ
#   ③ 連続403が閾値を超えたホストは一定時間スキップする（叩き続けない）
#      TTLを設けるのは app.py が常駐プロセスで、恒久ブロックだと
#      ライブ検索が再起動まで復旧しなくなるため
_HOST_BLOCK_THRESHOLD = 5
_HOST_BLOCK_TTL_SEC = 1800

_session_lock = _threading.Lock()
_session: "requests.Session | None" = None
_host_block_lock = _threading.Lock()
_host_block: dict = {}  # {host: {"streak": 連続403数, "until": 解除時刻(epoch秒)}}


def _get_session() -> requests.Session:
    """全スレッドで共有する requests.Session を返す（クッキーと接続の使い回し用）"""
    global _session
    with _session_lock:
        if _session is None:
            _session = requests.Session()
            _session.headers.update(HEADERS)
        return _session


def _host_of(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc


def _is_host_blocked(host: str) -> bool:
    with _host_block_lock:
        st = _host_block.get(host)
        if not st or st["until"] <= 0:
            return False
        if time.time() >= st["until"]:
            # TTL切れ。ゼロから数え直して様子を見る
            st["until"] = 0
            st["streak"] = 0
            return False
        return True


def _note_host_result(host: str, forbidden: bool):
    """403の連続回数を数え、閾値に達したホストを一定時間スキップ対象にする"""
    with _host_block_lock:
        st = _host_block.setdefault(host, {"streak": 0, "until": 0})
        if not forbidden:
            st["streak"] = 0
            return
        st["streak"] += 1
        if st["streak"] >= _HOST_BLOCK_THRESHOLD and st["until"] <= 0:
            st["until"] = time.time() + _HOST_BLOCK_TTL_SEC
            print(
                f"  ⛔ {host}: 403が{st['streak']}回連続。"
                f"{_HOST_BLOCK_TTL_SEC // 60}分間このホストへのリクエストを停止します"
            )


def safe_get(url: str, timeout: int = 15, retries: int = 1) -> BeautifulSoup | None:
    host = _host_of(url)
    if _is_host_blocked(host):
        _note_fetch_error()
        return None

    for attempt in range(1 + retries):
        try:
            res = _get_session().get(url, timeout=timeout)
            res.raise_for_status()
            _note_host_result(host, forbidden=False)
            return BeautifulSoup(res.text, "html.parser")
        except requests.RequestException as e:
            status = getattr(e.response, "status_code", None)
            _note_host_result(host, forbidden=(status == 403))
            # 403（アクセス拒否）は再試行しても通らない。他の4xxも同様なので、
            # 一時的な過負荷を示す429だけは従来どおり再試行する
            no_retry = status is not None and 400 <= status < 500 and status != 429
            if attempt < retries and not no_retry:
                # 根拠不明（導入: abe83370 2026-03-05、理由の記録なし。初回一括アップロードで既に存在）
                time.sleep(2)
            else:
                _note_fetch_error()
                print(f"  ❌ 取得失敗: {e}")
                break
    return None

def dump_html(name: str, soup: BeautifulSoup):
    if DEBUG_DUMP_HTML and soup:
        fn = f"debug_{name}.html"
        with open(fn, "w", encoding="utf-8") as f:
            f.write(soup.prettify())


# ── キャッシュ（店舗束形式） ──
#
# ファイル形式（2026-08-03改訂）: 1カード1ファイルは維持しつつ、店舗ごとに
# 独立したエントリ（タイムスタンプ・partial印付き）の束で持つ。
#   {"shops": {"店舗名": {"timestamp": ISO8601, "partial": bool, "results": [...]}}}
#
# 旧形式はカード名だけをキーに「検索結果の全体」を1枚で持っており、どの店舗を
# 調べた結果かを記録しなかった。そのため店舗を絞った検索が全体キャッシュを
# 上書きし、TTLの15分間ほかの店舗が「0件」に見える汚染が起きていた
# （2026-08-03 本番で実証）。旧形式ファイルは期限切れ扱い（全店ミス）で読む。
#
# 約束事:
# - 取得失敗した店舗は呼び出し側が store に渡さない＝キャッシュされず次回再試行。
#   「0件」と「取得失敗」を区別する原則のキャッシュ層への延長。
#   ただし検出できるのはネットワーク層の失敗（safe_get の RequestException 等で
#   _note_fetch_error が立つもの）のみ。店舗サイトの構造変更・セレクタ不一致による
#   「例外なしの0件」は在庫なしと区別できず、従来どおりTTLの間キャッシュされる
# - partial=True は /api/deck の1ページ目限定スクレイプ由来。通常検索
#   (include_partial=False) では不足扱いにして完全版を取り直す

import tempfile
import os
from threading import Lock as _Lock
_cache_lock = _Lock()

def _cache_key(card_name: str) -> str:
    return hashlib.md5(card_name.encode()).hexdigest()

def _shop_cache_load(fp: Path) -> dict:
    """店舗束キャッシュを読み、期限内の店舗エントリだけ返す。
    旧形式・破損・不在は {} ＝全店ミス扱い。"""
    if not fp.exists():
        return {}
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        shops = data.get("shops")
        if not isinstance(shops, dict):
            return {}  # 旧形式
        now = datetime.now()
        fresh = {}
        for shop, ent in shops.items():
            try:
                ts = datetime.fromisoformat(ent["timestamp"])
            except Exception:
                continue
            if now - ts > timedelta(minutes=CACHE_TTL_MINUTES):
                continue
            results = ent.get("results")
            if not isinstance(results, list):
                continue
            fresh[shop] = {"timestamp": ent["timestamp"],
                           "partial": bool(ent.get("partial")),
                           "results": results}
        return fresh
    except Exception:
        return {}

def _shop_cache_get(cache_dir: Path, key: str, shops: list[str],
                    include_partial: bool) -> tuple[dict[str, list], list[str]]:
    if not CACHE_ENABLED:
        return {}, list(shops)
    fresh = _shop_cache_load(cache_dir / f"{key}.json")
    hit: dict[str, list] = {}
    missing: list[str] = []
    for shop in shops:
        ent = fresh.get(shop)
        if ent is None or (ent["partial"] and not include_partial):
            missing.append(shop)
        else:
            hit[shop] = ent["results"]
    return hit, missing

def _shop_cache_store(cache_dir: Path, key: str, shop_results: dict[str, list],
                      partial_shops=frozenset()):
    if not CACHE_ENABLED or not shop_results:
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    fp = cache_dir / f"{key}.json"
    # 読み直し→マージ→原子的書き込みをロック内で行い、並行する別店舗の書き込みを消さない
    with _cache_lock:
        fresh = _shop_cache_load(fp)  # 期限切れエントリはここで間引かれる
        now_iso = datetime.now().isoformat()
        for shop, results in shop_results.items():
            fresh[shop] = {"timestamp": now_iso,
                           "partial": shop in partial_shops,
                           "results": results}
        content = json.dumps({"shops": fresh}, ensure_ascii=False)
        # 一時ファイルに書いてからリネームすることで、書き込み途中のファイルを読まれるのを防ぐ
        tmp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=cache_dir, prefix=fp.stem + "_", suffix=".tmp", delete=False
            ) as tmp:
                tmp.write(content)
                tmp_name = tmp.name
            os.replace(tmp_name, fp)
        finally:
            if tmp_name and os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass

def cache_get_shops(card_name: str, shops: list[str],
                    include_partial: bool = False) -> tuple[dict[str, list], list[str]]:
    """選択店舗のうちキャッシュ命中分と、スクレイプが必要な店舗を返す。
    返り値: ({店舗名: results}, [不足店舗名])。「調べて0件」は命中（空リスト）、
    「調べていない」は不足として区別される。"""
    return _shop_cache_get(CACHE_DIR, _cache_key(card_name), shops, include_partial)

def cache_store_shops(card_name: str, shop_results: dict[str, list],
                      partial_shops=frozenset()):
    """店舗ごとの結果をキャッシュへ追記する。取得失敗した店舗は渡さないこと。"""
    _shop_cache_store(CACHE_DIR, _cache_key(card_name), shop_results, partial_shops)

def cache_get(card_name: str) -> list[dict] | None:
    """互換API: 期限内の全店舗分を平坦化して返す（メタ表示用）。1店も無ければ None。
    どの店舗を調べた結果かを区別せず、partial（デッキ検索の1ページ目限定）由来の
    エントリも混ざるため、検索経路では cache_get_shops を使うこと。"""
    if not CACHE_ENABLED:
        return None
    fresh = _shop_cache_load(CACHE_DIR / f"{_cache_key(card_name)}.json")
    if not fresh:
        return None
    return [r for ent in fresh.values() for r in ent["results"]]


# ── 名前フィルタ ──

SUPPLY_KEYWORDS = [
    "スリーブ", "プレイマット", "デッキケース", "フィールドセンター",
    "デュエルセット", "デュエルフィールド", "鑑定済",
    "PSA", "BGS", "CGC", "ステンレス製",
]

# ラッシュデュエル除外用キーワード（遊戯王OCG専用サービスのため）
RUSH_DUEL_KEYWORDS = [
    "ラッシュデュエル", "RUSH DUEL",
]
# RDカードコードのパターン（RD/XX や RD-XX 形式）
_RD_CODE_PATTERN = re.compile(r"(?:^|[\s{（(])RD[/\-]", re.IGNORECASE)

def _is_rush_duel(text: str) -> bool:
    """テキストにラッシュデュエル関連の識別子が含まれるか判定"""
    upper = text.upper()
    for kw in RUSH_DUEL_KEYWORDS:
        if kw.upper() in upper:
            return True
    if _RD_CODE_PATTERN.search(text):
        return True
    return False

def _is_japanese_char(c: str) -> bool:
    cp = ord(c)
    return (0x3040 <= cp <= 0x309F or 0x30A0 <= cp <= 0x30FF
            or 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF
            or 0xFF66 <= cp <= 0xFF9F or 0xFF10 <= cp <= 0xFF5A)

def _is_alpha(c: str) -> bool:
    """半角英字か（全角英字は normalize_width で半角化済みの前提）"""
    return ("A" <= c <= "Z") or ("a" <= c <= "z")

def _has_name_text_outside_brackets(text: str) -> bool:
    """括弧外に日本語または英字が含まれるか。
    マッチした基底名の前にこれらがある場合、基底名はより長い別カード名
    （例: 'Sin 青眼の白龍' の 'Sin '、'SNo.39…' の 'S'）の一部とみなして除外する。
    型番・レアリティ等の括弧注記は除去してから判定する。"""
    stripped = re.sub(
        r"〔[^〕]*〕|\([^)]*\)|\[[^\]]*\]|☆[^☆]*☆|「[^」]*」|『[^』]*』", "", text
    )
    return any(_is_japanese_char(c) or _is_alpha(c) for c in stripped)

# 基底名がぴったり括弧で包まれているか判定するための開き/閉じ括弧
_OPEN_BRACKETS = "(（〔[「『【{《〈"
_CLOSE_BRACKETS = ")）〕]」』】}》〉"

def _is_exactly_bracketed(text: str, start: int, end: int) -> bool:
    """マッチ範囲 [start:end) が直前=開き括弧・直後=閉じ括弧でぴったり包まれているか。
    'BLUE EYES WHITE DRAGON(青眼の白龍)' の (青眼の白龍) のような併記注記を
    同一カードとして許容するために、前方チェックを免除する条件として使う。"""
    if start <= 0 or end >= len(text):
        return False
    return text[start - 1] in _OPEN_BRACKETS and text[end] in _CLOSE_BRACKETS

# カード名の「区切り」として扱う記号。店舗によっては商品名から落として登録するため
# （例: カードラッシュ「召喚魔術－「剣」」→「召喚魔術剣」）、あってもなくても一致させる。
#
# 2026-08-03 実測でこの集合を確定した（判定基準は「店舗が商品名から落とすか否か」）:
#   落とす（＝ここに入れる）: 「」『』☆★“”〜～＠＆！？．／×  ＋ 従来の ・空白－:：
#   落とさない（＝入れてはいけない）: ギリシャ文字 α β γ ζ Σ Ω、＋、∀ 等の識別子。
#     区切りにすると「磁石の戦士α」と「磁石の戦士β」が同一カード扱いになる
#
# is_target_card は normalize_width（NFKC）後の文字列で判定するため、全角記号は
# 半角形（@ & ! ? . / < > = ~）で列挙する。☆★“”×〜(U+301C) はNFKCで変換されない。
# この定数はパターン生成と分割の両方で使う（以前は同じ集合が2箇所に重複しており、
# 片方だけ直すと壊れる状態だった）。
_FLEX_SEP_CHARS = "「」『』【】《》〈〉〔〕☆★“”’'〜～×@&!?./<>=~"
_FLEX_SEP = r"[・\s　－\x2d:：" + re.escape(_FLEX_SEP_CHARS) + r"]*"
_FLEX_SPLIT = r"[・\s　－\x2d:：" + re.escape(_FLEX_SEP_CHARS) + r"]"

def _build_flex_pattern(card_name: str) -> str:
    parts = re.split(_FLEX_SPLIT, card_name)
    parts = [p for p in parts if p]
    # 各パーツをさらに英字/数字と日本語の境界で分割してflex区切りを挿入
    expanded = []
    for p in parts:
        # 英数→日本語、日本語→英数 の境界で分割
        sub = re.split(r'(?<=[A-Za-z0-9])(?=[^\x00-\x7F])|(?<=[^\x00-\x7F])(?=[A-Za-z0-9])', p)
        expanded.extend(sub)
    expanded = [s for s in expanded if s]
    return _FLEX_SEP.join(re.escape(p) for p in expanded)

def is_target_card(card_name: str, product_name: str) -> bool:
    if not STRICT_NAME_FILTER:
        return True
    if EXCLUDE_SUPPLY:
        for kw in SUPPLY_KEYWORDS:
            if kw in product_name:
                return False
    # ラッシュデュエル（別ゲーム）のカードを除外
    if _is_rush_duel(product_name):
        return False
    # 全角英数を半角に統一してからマッチング
    norm_card = normalize_width(card_name)
    norm_product = normalize_width(product_name)
    flex_pattern = _build_flex_pattern(norm_card)
    # カード名が区切り記号だけ（例: "☆"・"「」"）だとパターンが空になり、
    # 空マッチが全位置で成立して無関係な商品を拾ってしまう。照合対象なしとして弾く。
    # 通常のカード名では起こらないが、ユーザー入力が /api/search に直接届くため防ぐ。
    if not flex_pattern:
        return False
    for match in re.finditer(flex_pattern, norm_product):
        start, end = match.start(), match.end()
        # 前方に日本語/英字があれば別カード名の一部とみなして除外。
        # ただし基底名がぴったり括弧で包まれている併記注記（例: '…(青眼の白龍)'）は許容する。
        if (start > 0 and _has_name_text_outside_brackets(norm_product[:start])
                and not _is_exactly_bracketed(norm_product, start, end)):
            continue
        if end < len(norm_product):
            nc = norm_product[end]
            # 後方が日本語/英字なら別カード名の延長とみなして除外（例: 末尾 'ONE'）
            if _is_japanese_char(nc) or _is_alpha(nc):
                continue
            if nc in "・－-ー、,&" and end + 1 < len(norm_product):
                nxt = norm_product[end + 1]
                if _is_japanese_char(nxt) or _is_alpha(nxt):
                    continue
        return True
    return False


# ── レアリティ正規化 ──


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
    search_name = _normalize_search_query(card_name)
    page_url = f"https://yuyu-tei.jp/sell/ygo/s/search?search_word={requests.utils.quote(search_name)}"
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

# カードラッシュは1ページ100件。検索語から記号を落とすと母集団が広がるため、
# ページ送りが無いと人気カードで正解商品が枠から押し出される
# （実測: 「No.39 希望皇ホープ」は総件数200件で、1ページのみだと通過57→48件に減った）
CARDRUSH_PAGE_SIZE = 100

def scrape_cardrush(card_name: str, max_pages: int = 5) -> list[dict]:
    search_name = _cardrush_search_query(card_name)
    base_url = f"https://www.cardrush.jp/product-list?keyword={requests.utils.quote(search_name)}"

    results = []
    seen_items = set()   # 終端検出用。存在しないページは1ページ目が返るため内容で判定する
    for page in range(1, max_pages + 1):
        page_url = base_url if page == 1 else f"{base_url}&page={page}"
        soup = safe_get(page_url)
        if not soup:
            break
        if page == 1:
            dump_html("cardrush", soup)

        items = soup.select("li[class*='list_item_cell']")
        if not items:
            break

        page_keys = set()
        for item in items:
            link = item.select_one("a.item_data_link")
            nm = item.select_one("p.item_name")
            page_keys.add((link.get("href", "") if link else "",
                           nm.get_text(strip=True) if nm else ""))
        # このページが全て既出＝ページ範囲を超えて1ページ目が返っている
        if page_keys and page_keys <= seen_items:
            break
        seen_items |= page_keys

        results.extend(_parse_cardrush_items(card_name, items))

        # 1ページ分に満たなければ最終ページ
        if len(items) < CARDRUSH_PAGE_SIZE:
            break
        time.sleep(0.5)

    return results


def _parse_cardrush_items(card_name: str, items) -> list[dict]:
    """カードラッシュの商品要素リストを解析する（ページ送りで共通利用）"""
    results = []
    for item in items:
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

        # カードラッシュは単品カードに必ず【レアリティ】を付ける。【-】は「単品カードでは
        # ない」の印で、実体は封入商品・グッズ（ストラクチャーデッキ／ストレージ／未開封
        # サイコロ／アクリルスタンド／金属製カード等）。カード価格として記録すると
        # デッキ1個分の値段がそのカードの価格系列に混ざるため、ここで捨てる。
        # 実測（2026-08-03・カードラッシュ58カードで照合通過2,467件）: 【-】は14件で
        # 全てが封入商品・グッズ、型番も {-}。単品カードの巻き込みは0件。
        # 併せてカテゴリも《未開封BOX》《その他》《アクリルスタンド》のみで、これらの
        # カテゴリにレアリティ付きの出品は存在しなかった。
        if rarity.strip() == "-":
            continue

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

# トレコロ検索語で全角ハイフンへ寄せるダッシュ類（長音 ー U+30FC はカード名の一部なので含めない）
_TORECOLO_DASHES = "-‐‑‒–—―−"

def _torecolo_search_query(card_name: str) -> str:
    """トレコロ用の検索語を作る（販売・買取で共通）。

    トレコロの検索は「ダッシュが全角ハイフン（U+FF0D）であること」だけを要求する。
    2026-08-03 実測（同一カードで条件を振って比較）:
      - U+FF0D なら7件、半角ハイフン U+002D なら0件
      - 他のダッシュ異体（U+2010/U+2015/U+2212 等）とスペース置換もいずれも0件
      - 英数字の全角/半角は結果に影響しない
        （'D－HERO …'=7件 と 'Ｄ－ＨＥＲＯ …'=7件、'No.39 …'=50件 と 'Ｎｏ．３９ …'=50件）
      - 旧実装はダッシュをスペースへ置換していたため0件を招いていた（これが不具合の原因）

    NFKC（normalize_width）は通さない。トレコロはローマ数字を Ⅹ のまま登録しており、
    NFKC で X へ潰すと逆に取りこぼすため（「アルカナフォースⅩⅩⅠ」4件→13件）。
    中黒（・）も残さないとヒットしないため、除去も置換もしない。
    """
    s = card_name.replace("　", " ")
    for ch in _TORECOLO_DASHES:
        s = s.replace(ch, "－")
    return s

# トレコロの検索フォームが持つTCG別カテゴリの絞り込み。遊戯王＝1010。
# 他の値: 1020=デュエル・マスターズ / 1030=ヴァイスシュヴァルツ / 1050=ヴァンガード /
#         1073=ワンピース / 1074=ポケモン / 1034=遊戯王ラッシュデュエル ほか。
#
# トレコロは複数TCGを扱う店で、指定しないとカード名が一致した別ゲームの商品を拾う。
# URLに元からある `category=` は別軸のパラメータで、ここに 1010 を入れると0件になる
# （2026-08-03 実測）。正しいのはフォームの select 名と同じ `ct2`。
_TORECOLO_YGO_CT2 = "1010"

# 買取エントリは販売とは別のカテゴリ体系に属する（売る側は 20 系、遊戯王＝2010）。
# 販売用の 1010 を買取に使うと結果が丸ごと0件になる（2026-08-03 実測: 303件→0件）。
_TORECOLO_YGO_CT2_BUY = "2010"

def scrape_torecolo(card_name: str, max_pages: int = 5,
                    ct2: str = _TORECOLO_YGO_CT2) -> list[dict]:
    """トレコロCB — 複数ページ対応、レアリティ取得

    ct2: TCG別カテゴリの絞り込み。既定は遊戯王（_TORECOLO_YGO_CT2）。
         空文字を渡すと全TCG横断になる（旧挙動。新旧比較の検証用）。
    """
    search_name = _torecolo_search_query(card_name)
    base_url = (
        f"{TORECOLO_BASE}/shop/goods/search.aspx"
        f"?search=x&keyword={requests.utils.quote(search_name)}&category=&oshiire_code="
    )
    if ct2:
        base_url += f"&ct2={ct2}"
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

            # ラッシュデュエルのカードを除外（元の商品名・URL・カードコードで判定）
            if _is_rush_duel(name) or _is_rush_duel(href) or _is_rush_duel(code):
                continue

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
    try:
        parts_colon = _KANABELL_CLOUD_ID.split(":")
        if len(parts_colon) < 2:
            print("  [KANABELL] CLOUD_ID形式エラー: コロンがありません")
            return None
        encoded = parts_colon[1]
        decoded = _b64.b64decode(encoded).decode()
        parts = decoded.split("$")
        if len(parts) < 2:
            print(f"  [KANABELL] CLOUD_IDデコード結果が不正: parts数={len(parts)}")
            return None
        url = f"https://{parts[1]}.{parts[0]}"
        print("  [KANABELL] ESホストURL構築成功")
        return url
    except Exception as e:
        print(f"  [KANABELL] ESホストURL構築失敗: {type(e).__name__}")
        return None

_KANABELL_ES_URL = None  # lazy init

# カーナベルは遊戯王ジャンル（category1_id=1）の中に、OCGの紙カードではないものを
# 混ぜて持っている。商品名にはその印が出ず rarity_abbreviation にだけ現れるため、
# _is_rush_duel（商品名・型番ベース）をすり抜ける。レアリティ欄で弾く。
#   ラッシュ   : 遊戯王ラッシュデュエル（別ゲーム）。型番は RD/ が落ちた形で入り、
#                category3_abbr は EXT01・KP04・「ラッシュ本付属　ら」「プロモ は行」
#   ステンレス : 20th ANNIVERSARY 等のステンレス製記念カード。金属製で紙のOCGとは別物
#                （SUPPLY_KEYWORDS の「ステンレス製」は商品名にしか効かない）
# 遊戯王OCGにこの2つのレアリティは存在しない（rarity.py の canonical にも無く、
# normalize_rarity は「未知の表記」として生のまま返す）。
_KANABELL_EXCLUDED_RARITIES = frozenset({"ラッシュ", "ステンレス"})

# 状態ランク: ESフィールド名 → 表示名
_KANABELL_CONDITIONS = [
    ("sa", "状態:SA"),
    ("b",  "状態:B"),
    ("c",  "状態:C"),
    ("d",  "状態:D"),
]

# ── カーナベルES 検索クエリ用ヘルパー ──
# カーナベルの card_name は text型（.keyword サブフィールド付き）で、ダッシュの表記が
# カードごとに不統一（長音「ー」/全角ダッシュ「―」/ハイフン「-」等）。検索語と格納値で
# 区切り表記が食い違うと wildcard も match_phrase_prefix も外れてヒット0件になるため、
# 区切りを正規化して吸収する。（2026-06-17 ES実機検証で確定）
_KANABELL_DASH = "－―‐‑‒–—−ーｰ-"  # ダッシュ・長音の各種表記
_KANABELL_DASH_RE = re.compile(f"[{re.escape(_KANABELL_DASH)}]")
_KANABELL_SEP_RE = re.compile(f"[・\\s　:：{re.escape(_KANABELL_DASH)}]+")

def _kanabell_canon_dash(text: str) -> str:
    """ダッシュ・長音の各種表記を半角ハイフンに統一（is_target_card の表記揺れ吸収用）"""
    return _KANABELL_DASH_RE.sub("-", text)

def _kanabell_wildcard_value(card_name: str) -> str:
    """card_name.keyword 用の wildcard 値を作る。区切り（中黒・ダッシュ・空白・コロン）を
    '*' に置換し、格納側の区切り表記揺れに依存せずマッチさせる。
    例: 「閃刀姫－レイ」→ *閃刀姫*レイ* （格納値「閃刀姫ーレイ」にもヒット）"""
    parts = [p for p in _KANABELL_SEP_RE.split(card_name) if p]
    return "*" + "*".join(parts) + "*" if parts else f"*{card_name}*"

def _kanabell_wildcard_values(card_name: str) -> list[str]:
    """半角版と全角版の wildcard 値を返す（同じ値になる場合は1つだけ）。

    カーナベルのESは card_name を半角で持つレコードと全角で持つレコードが混在する。
    半角の検索語だけでは全角側に一致せず、対象カードが丸ごと取得できなくなる。

    2026-08-02〜03 実測（ESへ直接問い合わせて確認）:
      - 旧カード「D-HERO ディアボリックガイ」は card_name も半角
      - 新カード「Ｄ−ＨＥＲＯ デスドグマガイ」は card_name が全角 → 半角検索では0件
      - 全角英数を含む card_name は全39,994件中32件（14種類）。うち12種類が追跡対象
      - 14種類のうち2種類は「Ｎｏ.１０４ …」のようにピリオドだけ半角の混在表記。
        このため全角版は英数字だけを変換する（記号も変換すると当該2種類が0件になる）
      - 全角版を should に足しても既存のヒット件数は変わらない（灰流うらら37件等で確認）
    """
    half = _kanabell_wildcard_value(card_name)
    full = _kanabell_wildcard_value(to_fullwidth_alnum(card_name))
    return [half] if half == full else [half, full]

def scrape_kanabell(card_name: str, max_pages: int = 5) -> list[dict]:
    """カーナベル — Elasticsearch API経由で検索（状態別価格対応）"""
    if not _KANABELL_CLOUD_ID or not _KANABELL_API_KEY:
        print("  ⚠️  カーナベル: KANABELL_CLOUD_ID / KANABELL_API_KEY が未設定です")
        _note_fetch_error()  # 設定不足は「在庫なし」ではなく取得失敗として扱う
        return []

    # 検索語は半角に正規化したうえで、全角格納レコード用の wildcard も併せて投げる
    card_name = _normalize_fullwidth(card_name)
    wildcard_values = _kanabell_wildcard_values(card_name)

    global _KANABELL_ES_URL
    if _KANABELL_ES_URL is None:
        _KANABELL_ES_URL = _kanabell_es_host()
    if _KANABELL_ES_URL is None:
        print("  [KANABELL] ESホストURLの構築に失敗したため検索をスキップします")
        _note_fetch_error()
        return []

    search_url = f"{_KANABELL_ES_URL}/{_KANABELL_INDEX}/_search"

    # ページあたり件数 (ESの1リクエストで取得)
    page_size = 30 * max_pages  # max_pages=1なら30件、5なら150件

    # Elasticsearch クエリ (build.js の postProcessRequestBodyFn を再現)
    query_body = {
        "size": page_size,
        "_source": [
            "name", "id", "category1_id", "category2_id",
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
                    ] + [
                        {"wildcard": {field: {"value": v}}}
                        for v in wildcard_values
                        for field in ("card_name.keyword", "replace_card_name.keyword")
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
        print(f"  [KANABELL] 価格検索レスポンス: HTTP {res.status_code} [{card_name}]")
        res.raise_for_status()
        data = res.json()
    except requests.RequestException as e:
        print(f"  ❌ カーナベルES検索失敗: {e}")
        _note_fetch_error()
        return []

    hits = data.get("hits", {}).get("hits", [])
    all_results = []

    for hit in hits:
        src = hit.get("_source", {})
        name_text = src.get("name", "")
        card_id = src.get("id") or hit.get("_id", "")

        # ラッシュデュエルのカードを除外（全フィールドで判定）
        cat3 = src.get("category3_abbr", "")
        all_text = " ".join(str(v) for v in src.values() if isinstance(v, str))
        if _is_rush_duel(all_text):
            continue

        if not name_text or not is_target_card(_kanabell_canon_dash(card_name), _kanabell_canon_dash(name_text)):
            continue

        # レアリティ
        rarity = src.get("rarity_abbreviation", "")

        # ラッシュデュエル・ステンレス製記念カードを除外（レアリティ欄にだけ印が出る）
        if rarity.strip() in _KANABELL_EXCLUDED_RARITIES:
            continue

        # カードコード (カテゴリ略称から組み立て)
        code = ""
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


def kanabell_card_image_url(card_name: str) -> str:
    """カーナベルESから最初にヒットしたカードの画像URLを返す。失敗時は空文字"""
    if not _KANABELL_CLOUD_ID or not _KANABELL_API_KEY:
        return ""

    card_name = _normalize_fullwidth(card_name)
    wildcard_values = _kanabell_wildcard_values(card_name)

    global _KANABELL_ES_URL
    if _KANABELL_ES_URL is None:
        _KANABELL_ES_URL = _kanabell_es_host()
    if _KANABELL_ES_URL is None:
        print(f"  [KANABELL] ESホストURLの構築に失敗したため画像取得をスキップします [{card_name}]")
        return ""

    search_url = f"{_KANABELL_ES_URL}/{_KANABELL_INDEX}/_search"
    query_body = {
        "size": 20,
        "_source": ["card_image_name1"],
        "query": {
            "bool": {
                "must": [
                    {"bool": {"should": [
                        {"match_phrase_prefix": {"card_name": {"query": card_name, "slop": 2}}},
                        {"match_phrase_prefix": {"replace_card_name": {"query": card_name, "slop": 2}}},
                    ] + [
                        {"wildcard": {field: {"value": v}}}
                        for v in wildcard_values
                        for field in ("card_name.keyword", "replace_card_name.keyword")
                    ], "minimum_should_match": 1}}
                ],
                "filter": [
                    {"term": {"category1_id": 1}},
                    {"term": {"public_status": 1}},
                    {"term": {"del_flag": False}},
                    {"exists": {"field": "card_image_name1"}},
                ]
            }
        },
    }

    try:
        res = requests.post(
            search_url,
            json=query_body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"ApiKey {_KANABELL_API_KEY}",
            },
            timeout=8,
        )
        print(f"  [KANABELL] 画像検索レスポンス: HTTP {res.status_code} [{card_name}]")
        res.raise_for_status()
        hits = res.json().get("hits", {}).get("hits", [])
        if not hits:
            return ""
        # card_image_name1 が設定されている最初のドキュメントを使用
        # （レアリティによっては画像未登録のドキュメントが先頭に来ることがあるため）
        for hit in hits:
            img_name = hit.get("_source", {}).get("card_image_name1") or ""
            if img_name:
                return f"{KANABELL_BASE}/img/s/{img_name}"
        return ""
    except Exception as e:
        print(f"  カーナベル画像取得失敗 [{card_name}]: {e}")
        return ""


# ── カードラボ ──

CLABO_BASE = "https://www.c-labo-online.jp"

# カードラボ商品検索の1ページあたり件数（2026-08-03 実測。表示数120件はJS側の設定で
# URLパラメータからは指定できない ── disp_number/view_count/limit いずれも60件のまま）
CLABO_PAGE_SIZE = 60


def _clabo_last_page(soup) -> int:
    """カードラボのページャから最終ページ番号を読む（ページャが無ければ 1）。

    ページャは `<div class="pager">` 内に `page=N` のリンクを並べており、
    最終ページへのリンクは `a.to_last_page`。省略記号（...）で中間が省かれても
    最終ページのリンクは残るため、リンク中の page=N の最大値を採用する。
    """
    last = 1
    for a in soup.select("div.pager a[href*='page=']"):
        m = re.search(r"[?&]page=(\d+)", a.get("href", ""))
        if m:
            last = max(last, int(m.group(1)))
    return last


def scrape_clabo(card_name: str, max_pages: int = 5,
                 require_game_tag: bool = True) -> list[dict]:
    """カードラボ — 商品検索ページをスクレイピング（複数ページ対応）

    1ページ60件が上限で、60件を超えるカード（例:「ブラック・マジシャン」246件）は
    2ページ目以降を取らないと取りこぼす（2026-08-03 実測）。
    範囲外のページを要求すると1ページ目の内容が返るため、終端は
    「ページャの最終ページ番号」「60件未満」「全商品が既出」の3条件で検出する。
    """
    search_name = _normalize_search_query(card_name)
    base_url = (
        f"{CLABO_BASE}/product-list"
        f"?keyword={requests.utils.quote(search_name)}"
    )

    results = []
    seen_items = set()   # 終端検出用。存在しないページは1ページ目が返るため内容で判定する
    last_page = None
    for page in range(1, max_pages + 1):
        page_url = base_url if page == 1 else f"{base_url}&page={page}"
        soup = safe_get(page_url)
        if not soup:
            break
        if page == 1:
            dump_html("clabo", soup)
            last_page = _clabo_last_page(soup)

        containers = soup.select("li:has(div.inner_item_data)")
        if not containers:
            break

        page_keys = set()
        for c in containers:
            link = c.select_one("a[href*='/product/']")
            nm = c.select_one("span.goods_name")
            page_keys.add((link.get("href", "") if link else "",
                           nm.get_text(strip=True) if nm else ""))
        # このページが全て既出＝ページ範囲を超えて1ページ目が返っている
        if page_keys and page_keys <= seen_items:
            break
        seen_items |= page_keys

        results.extend(_parse_clabo_items(card_name, containers, require_game_tag))

        # 1ページ分に満たなければ最終ページ
        if len(containers) < CLABO_PAGE_SIZE:
            break
        if last_page is not None and page >= last_page:
            break
        time.sleep(0.5)

    return results


# カードラボは複数TCGを1つの検索窓で扱い、商品名にゲーム種別のタグを必ず付ける。
# 例: 【遊戯】赤き竜【ウルトラ/☆12】DUNE-JP038 / 【DM】宿命の決闘【VR】26EX2 30/89 /
#     【WS】赤き竜 ビィ【TD】GBF/S134-T01 / 【SV】ペガサスナイト【BR】BP14-102 /
#     【LO】ペガサス組の級長 アリアンナ・ハートベル【KR】LO-6143-K
# タグが遊戯王でない出品は別ゲームの同名カードなので捨てる。
#
# 判定は「先頭が【遊戯】か」ではなく「最初に現れる【…】が【遊戯】か」で行う。
# 買取サイトには《未開封》【遊戯】青眼の白龍… のように状態が前置される商品があり、
# 先頭一致だと遊戯王を巻き込むため（2026-08-03 実測で2件確認）。
#
# カテゴリ絞り込み（main_category）は遊戯王OCGだけで 672=シンクロ・676=効果…と
# カード種別ごとにIDが割れており、検索結果に出たものしか選択肢に現れないため使えない。
_CLABO_TAG_RE = re.compile(r"【([^】]*)】")
_CLABO_YGO_TAG = "遊戯"

def _is_clabo_ygo(raw_name: str) -> bool:
    """カードラボの商品名が遊戯王のものか（最初のゲームタグで判定）"""
    m = _CLABO_TAG_RE.search(raw_name)
    return bool(m) and m.group(1) == _CLABO_YGO_TAG

def _parse_clabo_items(card_name: str, containers,
                       require_game_tag: bool = True) -> list[dict]:
    """カードラボの商品要素リストを解析する（ページ送りで共通利用）

    require_game_tag=False で他TCG除外を外した旧挙動になる（新旧比較の検証用）。
    """
    results = []
    # 各商品は div.inner_item_data 内にリンク・画像・商品情報がまとまっている
    # 親の a タグ (product/XXXXX) からリンクを取得
    for container in containers:
        inner = container.select_one("div.inner_item_data")
        if not inner:
            continue

        # 商品名
        name_el = inner.select_one("span.goods_name")
        if not name_el:
            continue
        raw_name = name_el.get_text(strip=True)

        # 他TCG（デュエマ・ヴァイス・シャドバ等）の同名カードを除外
        if require_game_tag and not _is_clabo_ygo(raw_name):
            continue

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

        # ラッシュデュエルのカードを除外（元の商品名・カードコードで判定）
        if _is_rush_duel(raw_name) or _is_rush_duel(code):
            continue

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

def _parse_manzoku_rarity(text: str) -> str:
    """まんぞく屋の括弧付きレアリティ表記を抽出し正規名に変換する。
    抽出した略号は rarity.normalize_rarity で正規名に統一される。"""
    # カード名《》より前の部分からレアリティを探す
    before_card = text.split('《')[0] if '《' in text else text
    # [SE], 〈 UR 〉, 〔N〕, 【R】, (O-UR) などの形式に対応
    m = re.search(r'[\[〈〔【\(]\s*([A-Z0-9]+(?:-[A-Z0-9]+)?)\s*[\]〉〕】\)]', before_card)
    if m:
        return normalize_rarity(m.group(1))
    # 《NP》のような形式（カード名の前にある場合のみ）
    m2 = re.match(r'^《\s*([A-Z0-9]+)\s*》', text.strip())
    if m2:
        return normalize_rarity(m2.group(1))
    return ""

def scrape_manzoku(card_name: str) -> list[dict]:
    """まんぞく屋 — EC-CUBEベースの遊戯王カード通販"""
    search_name = _normalize_search_query(card_name)
    page_url = (
        f"https://shopmanzokuya.com/products/list"
        f"?category_id=1&name={requests.utils.quote(search_name)}"
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

        # ラッシュデュエルのカードを除外（テキスト全体で判定）
        if _is_rush_duel(text):
            continue

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


# ── 駿河屋 ──

SURUGAYA_BASE = "https://www.suruga-ya.jp"

# 駿河屋のレアリティ略号は rarity.normalize_rarity で統一処理（個別マップ廃止）

def scrape_surugaya(card_name: str) -> list[dict]:
    """駿河屋 — ecommerce_items JSデータから価格を取得（カテゴリ501）"""
    page_url = (
        f"{SURUGAYA_BASE}/search"
        f"?category=501&search_word={requests.utils.quote(normalize_width(card_name))}"
    )
    # 駿河屋はBot判定が厳しいため、Sessionでトップページ→Cookie取得→検索の流れを模倣
    surugaya_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
    }
    try:
        session = requests.Session()
        session.headers.update(surugaya_headers)
        # まずトップページにアクセスしてCookieを取得
        session.get(SURUGAYA_BASE, timeout=10)
        # 検索リクエスト（Referer付き）
        session.headers["Referer"] = SURUGAYA_BASE + "/"
        session.headers["Sec-Fetch-Site"] = "same-origin"
        res = session.get(page_url, timeout=20)
        res.raise_for_status()
        html_text = res.text
    except requests.RequestException as e:
        print(f"  ❌ 駿河屋取得失敗: {e}")
        _note_fetch_error()
        return []

    # Cloudflareブロック検知
    if 'challenge-platform' in html_text and 'ecommerce_items' not in html_text:
        print("  ⚠ 駿河屋: Cloudflareにブロックされた可能性")
        _note_fetch_error()
        return []

    print(f"  駿河屋: HTML {len(html_text)}文字, ecommerce_items={'あり' if 'ecommerce_items' in html_text else 'なし'}")

    # ecommerce_items 内の各商品データを抽出
    # item_name はカードコードのみ（例: RC04-JP009[QC）、]が欠落する場合あり
    results = []
    seen_ids = set()

    for m in re.finditer(
        r"item_id:\s*common\.htmlDecode\('([^']+)'\).*?"
        r"item_name:\s*common\.htmlDecode\('([^']+)'\).*?"
        r"price:\s*(\d+)",
        html_text, re.DOTALL
    ):
        item_id = m.group(1)
        raw_name = m.group(2)
        price = int(m.group(3))

        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)

        if price <= 0:
            continue

        # カード番号を抽出（例: RC04-JP009）
        code = ""
        code_match = re.search(r'([A-Z0-9]+-JP[A-Z]?\d+)', raw_name)
        if code_match:
            code = code_match.group(1)

        # レアリティを抽出（]が欠落する場合にも対応）
        rarity = ""
        rarity_match = re.search(r'\[([A-Z0-9-]+)\]?', raw_name)
        if rarity_match:
            rarity = normalize_rarity(rarity_match.group(1))

        product_url = f"{SURUGAYA_BASE}/product/detail/{item_id}"

        results.append({
            "shop": "駿河屋", "name": card_name,
            "rarity": normalize_rarity(rarity), "code": code,
            "condition": "-", "price": price, "stock": 1,
            "sold_out": False, "url": product_url,
            "image": "",
        })

    return results


# ══════════════════════════════════════════════════
# 買取価格スクレイパー
# ══════════════════════════════════════════════════

# ── カードラッシュ買取 (Next.js __NEXT_DATA__) ──

CARDRUSH_MEDIA_BASE = "https://cardrush.media"

CARDRUSH_BUY_PAGE_SIZE = 100


def scrape_cardrush_buy(card_name: str, max_pages: int = 5) -> list[dict]:
    """カードラッシュ — ラッシュメディアの買取価格を取得（複数ページ対応）

    1ページ100件が上限で、100件を超えるカード（例:「ブラック・マジシャン」182件）は
    2ページ目以降を取らないと取りこぼす（2026-08-03 実測）。
    総ページ数は `props.pageProps.lastPage` に入っており、範囲外のページは
    空の buyingPrices を返す（販売側と違い1ページ目へは戻らない）。
    """
    search_name = _cardrush_search_query(card_name)
    base_url = (
        f"{CARDRUSH_MEDIA_BASE}/yugioh/buying_prices"
        f"?name={requests.utils.quote(search_name)}"
    )

    results = []
    for page in range(1, max_pages + 1):
        page_url = base_url if page == 1 else f"{base_url}&page={page}"
        soup = safe_get(page_url, timeout=20)
        if not soup:
            break

        # __NEXT_DATA__ からJSONデータを取得
        script_el = soup.select_one("script#__NEXT_DATA__")
        if not script_el:
            break

        try:
            next_data = json.loads(script_el.string)
            page_props = next_data["props"]["pageProps"]
            buying_prices = page_props["buyingPrices"]
        except (json.JSONDecodeError, KeyError, TypeError):
            _note_fetch_error()  # サイト構造変更の可能性（JSON構造が想定と不一致）
            break

        if not buying_prices:
            break

        results.extend(_parse_cardrush_buy_items(card_name, buying_prices, page_url))

        last_page = page_props.get("lastPage")
        if isinstance(last_page, int) and page >= last_page:
            break
        # lastPage が取れない場合の保険（1ページ分に満たなければ最終ページ）
        if len(buying_prices) < CARDRUSH_BUY_PAGE_SIZE:
            break
        time.sleep(0.5)

    return results


def _parse_cardrush_buy_items(card_name: str, buying_prices: list, page_url: str) -> list[dict]:
    """カードラッシュ買取のJSON要素リストを解析する（ページ送りで共通利用）"""
    results = []
    for item in buying_prices:
        name = item.get("name", "")
        model_number = item.get("model_number", "")
        # ラッシュデュエルのカードを除外
        if _is_rush_duel(name) or _is_rush_duel(model_number):
            continue
        if not name or not is_target_card(card_name, name):
            continue
        price = item.get("amount")
        if not price or price <= 0:
            continue

        rarity = item.get("rarity", "")
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
    """カーナベル買取 — ES APIから直接買取価格(sa_buying_price)を取得する。

    買取価格は ES index の `sa_buying_price` に入っており、`sa_limit_flag`(買取枠フラグ)が
    True のものが現在買取中（=HTMLの「買取終了」非表示と一致）。買取枠切れ(終了)は
    sa_limit_flag=False で、古い価格が残っているため必ず除外する。
    買取詳細HTML(?act=buy_detail)はデータセンターIP(Render/GitHub Actions)から403で
    ブロックされるため、ES方式に統一してどの環境でも取得できるようにした。
    （2026-06-16 実データ検証: ES sa_buying_price はHTML買取価格と完全一致、
      sa_limit_flag で有効/終了が150件中100%分離することを確認）
    """
    if not _KANABELL_CLOUD_ID or not _KANABELL_API_KEY:
        print("  ⚠️  カーナベル買取: KANABELL_CLOUD_ID / KANABELL_API_KEY が未設定です")
        _note_fetch_error()
        return []

    # 検索語は半角に正規化したうえで、全角格納レコード用の wildcard も併せて投げる
    card_name = _normalize_fullwidth(card_name)
    wildcard_values = _kanabell_wildcard_values(card_name)

    global _KANABELL_ES_URL
    if _KANABELL_ES_URL is None:
        _KANABELL_ES_URL = _kanabell_es_host()
    if _KANABELL_ES_URL is None:
        print("  [KANABELL] ESホストURLの構築に失敗したため買取検索をスキップします")
        _note_fetch_error()
        return []

    # ES APIでカード名を検索（買取価格・買取枠フラグ・レアリティを直接取得）
    search_url = f"{_KANABELL_ES_URL}/{_KANABELL_INDEX}/_search"
    query_body = {
        "size": 100,
        "_source": ["name", "id", "rarity_abbreviation", "category2_abbr", "category3_abbr",
                    "card_image_name1", "sa_buying_price", "sa_limit_flag"],
        "query": {
            "bool": {
                "must": [
                    {"bool": {"should": [
                        {"match_phrase_prefix": {"card_name": {"query": card_name, "slop": 2}}},
                    ] + [
                        {"wildcard": {field: {"value": v}}}
                        for v in wildcard_values
                        for field in ("card_name.keyword", "replace_card_name.keyword")
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
        _note_fetch_error()
        return []

    results = []
    seen_ids = set()
    for hit in hits:
        src = hit.get("_source", {})
        name_text = src.get("name", "")
        card_id = src.get("id") or hit.get("_id", "")
        if not name_text or not card_id or card_id in seen_ids:
            continue
        # ラッシュデュエルのカードを除外（全フィールドで判定）
        all_text = " ".join(str(v) for v in src.values() if isinstance(v, str))
        if _is_rush_duel(all_text):
            continue
        if not is_target_card(_kanabell_canon_dash(card_name), _kanabell_canon_dash(name_text)):
            continue
        # ラッシュデュエル・ステンレス製記念カードを除外（販売側と同じ判定）
        if src.get("rarity_abbreviation", "").strip() in _KANABELL_EXCLUDED_RARITIES:
            continue
        seen_ids.add(card_id)

        # 買取中（買取枠フラグTrue）かつ有効価格のみ採用。枠切れ(終了)は sa_limit_flag=False。
        if not src.get("sa_limit_flag"):
            continue
        price = src.get("sa_buying_price") or 0
        if price <= 0:
            continue

        img_name = src.get("card_image_name1", "")
        image_url = f"{KANABELL_BASE}/img/s/{img_name}" if img_name else ""
        results.append({
            "shop": "カーナベル",
            "name": name_text,
            "rarity": normalize_rarity(src.get("rarity_abbreviation", "")),
            "code": src.get("category3_abbr", ""),
            "condition": "-",
            "price": int(price),
            "stock": 1,
            "sold_out": False,
            "url": f"{KANABELL_BASE}/?act=buy_detail&id={card_id}&genre=1",
            "image": image_url,
        })
    return results


# ── 遊々亭買取 ──

def scrape_yuyu_buy(card_name: str) -> list[dict]:
    """遊々亭 — 買取検索ページをスクレイピング"""
    search_name = _normalize_search_query(card_name)
    page_url = (
        f"https://yuyu-tei.jp/buy/ygo/s/search"
        f"?search_word={requests.utils.quote(search_name)}"
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

        if not name or _is_rush_duel(name) or not is_target_card(card_name, name):
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


# ── トレコロCB買取 ──

def scrape_torecolo_buy(card_name: str, max_pages: int = 3,
                        ct2: str = _TORECOLO_YGO_CT2_BUY) -> list[dict]:
    """トレコロCB買取 — 販売と同じ search.aspx の結果に混在する買取エントリを抽出する。

    買取エントリは price 要素が「(強化/参考)買取価格X円」形式。販売スクレイパー
    (scrape_torecolo)はこれを除外しているので、本関数は逆にこれだけを拾う。
    レアリティは買取エントリでは空のことが多い（カード名に版違い情報が入る）。

    ct2 は販売側と同じ意味だが値が違う（既定＝買取側の遊戯王 2010。空文字で旧挙動）。
    """
    search_name = _torecolo_search_query(card_name)
    base_url = (
        f"{TORECOLO_BASE}/shop/goods/search.aspx"
        f"?search=x&keyword={requests.utils.quote(search_name)}&category=&oshiire_code="
    )
    if ct2:
        base_url += f"&ct2={ct2}"
    results = []

    for page in range(1, max_pages + 1):
        page_url = base_url if page == 1 else f"{base_url}&p={page}"
        soup = safe_get(page_url)
        if not soup:
            break
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

            # 買取エントリのみ対象（販売は除外）
            if "買取" not in price_text:
                continue
            price_match = re.search(r"([\d,]+)\s*円", price_text)
            if not price_match:  # 「参考買取価格」など金額無しは除外
                continue
            price = int(price_match.group(1).replace(",", ""))
            if price <= 0:
                continue

            href = name_el.get("href", "")
            product_url = f"{TORECOLO_BASE}{href}" if href.startswith("/") else href

            # 商品名の正規化（販売版 scrape_torecolo と同じ）
            match_name = name
            match_name = re.sub(r"^キズあり", "", match_name).strip()
            match_name = re.sub(r"★キズあり★", "", match_name).strip()
            match_name = re.sub(r"【[^】]*】", "", match_name).strip()
            match_name = re.sub(r"^[^◇]*◇", "", match_name).strip()
            match_name = re.sub(r"（[^）]*）", "", match_name).strip()

            # レアリティ: 買取エントリでは空/「-」のことが多い
            rarity = ""
            cat_el = item.select_one("div.block-thumbnail-t--goods-category")
            if cat_el:
                rarity = cat_el.get_text(strip=True)
                rarity = rarity.translate(str.maketrans(
                    'ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ０１２３４５６７８９',
                    'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
                ))
                if rarity == "-":
                    rarity = ""

            code = ""
            code_match = re.search(r"/g/g([^/]+?)(-[SK])?/", href)
            if code_match:
                code = code_match.group(1)

            img_el = item.select_one("img")
            image_url = ""
            if img_el:
                image_url = img_el.get("src", "") or img_el.get("data-src", "")
                if image_url and not image_url.startswith("http"):
                    image_url = f"{TORECOLO_BASE}{image_url}"

            # ラッシュデュエル除外
            if _is_rush_duel(name) or _is_rush_duel(href) or _is_rush_duel(code):
                continue
            if not is_target_card(card_name, match_name):
                continue

            results.append({
                "shop": "トレコロCB", "name": match_name or name,
                "rarity": normalize_rarity(rarity), "code": code,
                "condition": "-", "price": price, "stock": 1,
                "sold_out": False, "url": product_url, "image": image_url,
            })

        next_link = soup.select_one("a[href*='p=%d']" % (page + 1))
        if not next_link:
            break
        time.sleep(0.5)

    return results


# ── カードラボ買取 ──

CLABO_KAITORI_BASE = "https://www.c-labo-kaitori.jp"

def scrape_clabo_buy(card_name: str, require_game_tag: bool = True) -> list[dict]:
    """カードラボ買取 — 買取専用サイト c-labo-kaitori.jp を検索する。

    販売(c-labo-online.jp)と同一ECプラットフォームでHTML構造も同じ。
    figure 要素が買取価格。販売版 scrape_clabo とほぼ同じ構造。

    require_game_tag=False で他TCG除外を外した旧挙動になる（新旧比較の検証用）。
    """
    search_name = _normalize_search_query(card_name)
    page_url = (
        f"{CLABO_KAITORI_BASE}/product-list"
        f"?keyword={requests.utils.quote(search_name)}"
    )
    soup = safe_get(page_url)
    if not soup:
        return []

    results = []
    for container in soup.select("li:has(div.inner_item_data)"):
        inner = container.select_one("div.inner_item_data")
        if not inner:
            continue
        name_el = inner.select_one("span.goods_name")
        if not name_el:
            continue
        raw_name = name_el.get_text(strip=True)

        # 他TCG（デュエマ・ロルカナ等）の同名カードを除外（販売版と同じ判定）
        if require_game_tag and not _is_clabo_ygo(raw_name):
            continue

        # レアリティとコードを商品名から抽出（販売版と同じ形式）
        rarity = ""
        code = ""
        rarity_match = re.search(r"【([^】]+/[^】]+)】(\S+)$", raw_name)
        if rarity_match:
            rarity = rarity_match.group(1).split("/")[0]
            code = rarity_match.group(2)
        display_name = re.sub(r"【[^】]*】", "", raw_name).strip()
        if code:
            display_name = display_name.replace(code, "").strip()

        if _is_rush_duel(raw_name) or _is_rush_duel(code):
            continue
        if not is_target_card(card_name, display_name):
            continue

        price_el = inner.select_one("span.figure")
        if not price_el:
            continue
        price = parse_price(price_el.get_text())
        if not price:
            continue

        # 買取停止（soldoutクラス）は除外対象としてマーク
        stock_el = inner.select_one("p.stock")
        sold_out = bool(stock_el and "soldout" in stock_el.get("class", []))

        link_el = container.select_one("a[href*='/product/']")
        product_url = link_el.get("href", "") if link_el else ""

        image_url = ""
        img_box = inner.select_one("div.async_image_box")
        if img_box:
            image_url = img_box.get("data-src", "")

        results.append({
            "shop": "カードラボ", "name": display_name,
            "rarity": normalize_rarity(rarity), "code": code,
            "condition": "-", "price": price, "stock": 1,
            "sold_out": sold_out, "url": product_url, "image": image_url,
        })

    return results


# ── 買取店舗リスト ──

BUYBACK_SHOPS = [
    ("カードラッシュ", scrape_cardrush_buy),
    ("カーナベル", scrape_kanabell_buy),
    ("遊々亭", scrape_yuyu_buy),
    ("トレコロCB", scrape_torecolo_buy),
    ("カードラボ", scrape_clabo_buy),
]

DEFAULT_BUYBACK_SHOPS = ["カードラッシュ", "カーナベル", "遊々亭", "トレコロCB", "カードラボ"]


# ── 買取キャッシュ（販売と分離・同じ店舗束形式）──

BUYBACK_CACHE_DIR = Path(__file__).parent / ".cache_buy"

def _buyback_cache_key(card_name: str) -> str:
    return hashlib.md5(f"buy_{card_name}".encode()).hexdigest()

def buyback_cache_get_shops(card_name: str, shops: list[str],
                            include_partial: bool = False) -> tuple[dict[str, list], list[str]]:
    """買取版 cache_get_shops（挙動は販売と同じ・保存先が別）"""
    return _shop_cache_get(BUYBACK_CACHE_DIR, _buyback_cache_key(card_name),
                           shops, include_partial)

def buyback_cache_store_shops(card_name: str, shop_results: dict[str, list],
                              partial_shops=frozenset()):
    """買取版 cache_store_shops。取得失敗した店舗は渡さないこと。"""
    _shop_cache_store(BUYBACK_CACHE_DIR, _buyback_cache_key(card_name),
                      shop_results, partial_shops)

def buyback_cache_get(card_name: str) -> list[dict] | None:
    """互換API: 期限内の全店舗分を平坦化して返す（メタ表示用）。1店も無ければ None。"""
    if not CACHE_ENABLED:
        return None
    fresh = _shop_cache_load(BUYBACK_CACHE_DIR / f"{_buyback_cache_key(card_name)}.json")
    if not fresh:
        return None
    return [r for ent in fresh.values() for r in ent["results"]]


# ── 全店舗検索 ──

SHOPS = [
    ("遊々亭", scrape_yuyu),
    ("カードラッシュ", scrape_cardrush),
    ("トレコロCB", scrape_torecolo),
    ("カーナベル", scrape_kanabell),
    ("カードラボ", scrape_clabo),
    ("まんぞく屋", scrape_manzoku),
    ("駿河屋", scrape_surugaya),
]

# デフォルトで検索する店舗
DEFAULT_SHOPS = ["遊々亭", "カードラッシュ", "トレコロCB", "カーナベル", "カードラボ", "まんぞく屋"]


def run_shop_with_status(fn, card_name: str) -> tuple[list[dict], int]:
    """店舗スクレイパーを実行し、(結果, 取得エラー数) を返す（ワーカースレッド内で実行）。
    「0件かつ取得エラー>0」は在庫なしではなく取得失敗。app.py の検索経路が
    キャッシュ可否（失敗店舗はキャッシュしない）の判定にも使う。"""
    _reset_fetch_errors()
    items = fn(card_name)
    return items, _get_fetch_errors()


def compare_prices(card_name: str, shop_names: list[str] | None = None,
                   status_out: dict | None = None) -> list[dict]:
    """指定された店舗を並列にスクレイピング

    status_out に dict を渡すと、店舗ごとの取得状況を書き込む:
      {店舗名: {"count": 件数, "fetch_errors": 取得エラー数, "exception": エラー文字列(任意)}}
    「0件かつ fetch_errors > 0」は在庫なしではなく取得失敗を意味する。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    target = shop_names or DEFAULT_SHOPS
    active = [(name, fn) for name, fn in SHOPS if name in target]

    all_results = []
    with ThreadPoolExecutor(max_workers=len(active)) as executor:
        futures = {
            executor.submit(run_shop_with_status, fn, card_name): name
            for name, fn in active
        }
        for future in as_completed(futures):
            shop_name = futures[future]
            try:
                results, fetch_errors = future.result()
                all_results.extend(results)
                if status_out is not None:
                    status_out[shop_name] = {"count": len(results), "fetch_errors": fetch_errors}
            except Exception as e:
                print(f"  ❌ {shop_name}: {e}")
                if status_out is not None:
                    status_out[shop_name] = {"count": 0, "fetch_errors": 1, "exception": str(e)}

    return all_results


def compare_buyback(card_name: str, shop_names: list[str] | None = None,
                    status_out: dict | None = None) -> list[dict]:
    """指定された買取店舗を並列にスクレイピング（compare_prices の買取版）"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    target = shop_names or DEFAULT_BUYBACK_SHOPS
    active = [(name, fn) for name, fn in BUYBACK_SHOPS if name in target]
    if not active:
        return []

    all_results = []
    with ThreadPoolExecutor(max_workers=len(active)) as executor:
        futures = {
            executor.submit(run_shop_with_status, fn, card_name): name
            for name, fn in active
        }
        for future in as_completed(futures):
            shop_name = futures[future]
            try:
                results, fetch_errors = future.result()
                all_results.extend(results)
                if status_out is not None:
                    status_out[shop_name] = {"count": len(results), "fetch_errors": fetch_errors}
            except Exception as e:
                print(f"  ❌ {shop_name}: {e}")
                if status_out is not None:
                    status_out[shop_name] = {"count": 0, "fetch_errors": 1, "exception": str(e)}

    return all_results
