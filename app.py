"""
TCG 価格比較 Web サーバー
"""

import json
import time
import os
import random
import logging
import requests as _http
import threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, jsonify, Response
from flask_compress import Compress

from scraper import (
    SHOPS, DEFAULT_SHOPS, WAIT_SEC, cache_get, cache_set,
    is_target_card, normalize_rarity,
    BUYBACK_SHOPS, DEFAULT_BUYBACK_SHOPS,
    buyback_cache_get, buyback_cache_set,
    kanabell_card_image_url,
)
from meta_scraper import fetch_tier_list, fetch_deck_cards, build_deck_text, build_recipe_text, _cache_read, _DECK_CACHE_TTL, _TIER_CACHE_TTL
from pack_scraper import get_pack_list, fetch_pack_cards
from monitor import tracker, run_health_check

import re as _re
from urllib.parse import quote as _url_quote

# 検索クエリの表記ゆれ正規化
_DASH_CHARS = _re.compile(r'[\u2012\u2013\u2014\u2015\u2212\uFF0D\uFF70]')  # 各種ダッシュ・ハイフン
# あいまい検索用: 中黒・ハイフン・スペース等を除去して比較するための正規化
_FUZZY_STRIP = _re.compile(r'[\s\u3000・\-\u2012\u2013\u2014\u2015\u2212\uFF0D\uFF70\u30FB\uFF65.,()（）「」\u300C\u300D]')
def _normalize_query(q: str) -> str:
    """ユーザー入力のカード名を正規化（ダッシュ統一・全角スペース変換）"""
    q = _DASH_CHARS.sub('-', q)       # 各種ダッシュ → 通常ハイフン
    q = q.replace('\u3000', ' ')      # 全角スペース → 半角
    return q.strip()

def _fuzzy_key(s: str) -> str:
    """あいまい検索用に記号・スペースを除去した比較キーを返す。全角英数→半角も正規化する"""
    # U+FF01-FF5E（全角英数・記号）→対応するASCII（半角）に変換
    s = ''.join(chr(ord(c) - 0xFEE0) if '！' <= c <= '～' else c for c in s)
    return _FUZZY_STRIP.sub('', s).lower()

def _correct_cardname(name: str) -> str:
    """カード名をDBと照合し、補正が必要なら正式名称を返す。不要ならそのまま返す"""
    # O(1) のset検索を使う（_cardnames listへのO(n)検索は避ける）
    if not _cardnames_set or name in _cardnames_set:
        return name
    q_fuzzy = _fuzzy_key(name)
    # 記号除去で一致
    if q_fuzzy in _cardnames_fuzzy:
        return _cardnames_fuzzy[q_fuzzy][0]
    # 読み仮名の完全一致
    if name in _cardnames_reading:
        return _cardnames_reading[name]
    # 読み仮名の記号除去版で一致
    if q_fuzzy in _cardnames_reading_fuzzy:
        return _cardnames_reading_fuzzy[q_fuzzy]
    return name

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
Compress(app)

_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# YGOResources 名前インデックスキャッシュ（初回リクエスト時に1回だけ取得）
_ygores_name_index = None
_ygores_fuzzy_index = None  # あいまいキー → ids の逆引き（全角/半角・ハイフン種のゆれ吸収用）

def _get_ygores_name_index():
    global _ygores_name_index, _ygores_fuzzy_index
    if _ygores_name_index is not None:
        return _ygores_name_index
    try:
        resp = _http.get(
            "https://db.ygoresources.com/data/idx/card/name/ja",
            timeout=30,
            headers={"User-Agent": _BROWSER_UA},
        )
        _ygores_name_index = resp.json() if resp.status_code == 200 else {}
        # あいまい一致用の逆引き辞書を構築（記号・全半角・ハイフン種のゆれを吸収）
        # 衝突時は先勝ち（実データで50件・全てスペース種違いの同一カードのみ）
        _ygores_fuzzy_index = {}
        for nm, ids in _ygores_name_index.items():
            fk = _fuzzy_key(nm)
            if fk and fk not in _ygores_fuzzy_index:
                _ygores_fuzzy_index[fk] = ids
        logger.info(f"[YGOResources] 名前インデックス取得: {len(_ygores_name_index)}件")
    except Exception as e:
        logger.warning(f"[YGOResources] 名前インデックス取得失敗: {e}")
        _ygores_name_index = {}
        _ygores_fuzzy_index = {}
    return _ygores_name_index

def _ygores_image_url(card_name):
    """カード名からYGOResourcesのOCGアートワーク画像URLを返す。未登録の場合はNone"""
    idx = _get_ygores_name_index()
    ids = idx.get(card_name)
    if not ids and _ygores_fuzzy_index:
        # 完全一致しない場合は記号・全半角ハイフン等のゆれを無視して再照合
        # （YGOResourcesは全角ハイフン「－」が標準だが、カード名は半角「-」で渡ることがある）
        ids = _ygores_fuzzy_index.get(_fuzzy_key(card_name))
    if not ids:
        return None
    cid = ids[0]
    path = f"/{cid // 10000}/{(cid % 10000) // 100}/{cid % 100}_1.png"
    return f"https://artworks-jp-n.ygoresources.com{path}"

FAVICON_FILES = {
    'favicon.svg', 'favicon-16.svg', 'favicon-32.png', 'favicon-16.png',
    'apple-touch-icon.png', 'icon-192.png', 'icon-512.png', 'manifest.json',
}

@app.after_request
def add_cache_headers(response):
    """レスポンスタイプに応じたキャッシュ制御"""
    ct = response.content_type
    path = request.path.lstrip('/')
    filename = path.split('/')[-1]
    if filename in FAVICON_FILES:
        # ファビコン・アイコン類はキャッシュさせない
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
    elif 'text/html' in ct:
        response.headers['Cache-Control'] = 'public, max-age=60, stale-while-revalidate=300'
    elif 'application/xml' in ct or 'text/plain' in ct:
        response.headers['Cache-Control'] = 'public, max-age=3600'
    return response

# 同時検索の簡易レートリミット (メモリ内)
_last_search: dict[str, float] = {}
_rate_limit_lock = __import__('threading').Lock()
RATE_LIMIT_SEC = 3
RATE_LIMIT_MAX_ENTRIES = 10000  # これ以上溜まったら古い記録を一括削除
MAX_CARD_NAME_LEN = 50
MAX_DECK_CARDS = 80
MAX_DECK_PARAM_CHARS = 6000
MAX_DECK_QTY = 99
MAX_PACK_PARAM_LEN = 120

# ヘルスチェック用シークレットキー
HEALTH_CHECK_KEY = os.environ.get("HEALTH_CHECK_KEY", "")

# アフィリエイト設定（環境変数から取得）
AFFILIATE_CONFIG = {
    "カーナベル": {
        "sell_a8mat": os.environ.get("A8_KANABELL_SELL", ""),
        "buy_a8mat": os.environ.get("A8_KANABELL_BUY", ""),
    },
    "駿河屋": {
        "user_id": os.environ.get("SURUGAYA_AFF_USER_ID", ""),
    },
}

# VAPID 設定（Web Push 通知用）
VAPID_PUBLIC_KEY  = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_CLAIMS      = {"sub": "mailto:tcg.price.compare@gmail.com"}


# ── 検索ランキング（Supabase永続化 + メモリフォールバック） ──

from collections import defaultdict
from threading import Lock, Thread

# フィードバック（不具合・要望）受付
FEEDBACK_RATE_LIMIT_SEC    = 30    # 同一IP 30秒に1回まで
MAX_FEEDBACK_BODY_CHARS    = 2000
MAX_FEEDBACK_CONTACT_CHARS = 200
_last_feedback: dict[str, float] = {}
_feedback_lock = Lock()
_DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

_ranking_lock = Lock()
_search_recent: list[tuple[float, str]] = []  # メモリフォールバック用
RANKING_RECENT_HOURS = 24

# カード情報キャッシュ（一人回しデッキ読込の高速化）
# konami_id (int) → api_card_info のレスポンスdict をメモリに保持する
# カード情報はほぼ不変のため TTL なし（再起動でリセット）
_card_info_cache: dict[int, dict] = {}
_card_info_lock = Lock()
MAX_CARD_INFO_CACHE = 5000  # これ以上溜まったら一括クリア

# Supabaseクライアント（環境変数が設定されていれば有効）
_supabase_client = None
_SUPABASE_URL = os.environ.get("SUPABASE_URL")
_SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if _SUPABASE_URL and _SUPABASE_KEY:
    try:
        from supabase import create_client
        _supabase_client = create_client(_SUPABASE_URL, _SUPABASE_KEY)
        logger.info("Supabase接続成功 — 検索ランキングを永続化します")
    except Exception as e:
        logger.warning(f"Supabase接続失敗（メモリモードで動作）: {e}")


def _is_debug_mode() -> bool:
    return os.environ.get("FLASK_DEBUG", "1") == "1"


def _consume_rate_limit():
    """重いAPI用のIP単位レートリミット。制限時はレスポンス、通過時はNone。"""
    client_ip = request.remote_addr or "unknown"
    now = time.time()
    with _rate_limit_lock:
        if client_ip in _last_search and now - _last_search[client_ip] < RATE_LIMIT_SEC:
            return jsonify({"error": "しばらく待ってから再度検索してください"}), 429
        _last_search[client_ip] = now
        if len(_last_search) > RATE_LIMIT_MAX_ENTRIES:
            cutoff = now - 3600
            stale = [ip for ip, ts in _last_search.items() if ts < cutoff]
            for ip in stale:
                del _last_search[ip]
    return None


def _parse_limit_param(default: int, maximum: int):
    raw = request.args.get("limit", str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, (jsonify({"error": "limit は整数で指定してください"}), 400)
    return max(1, min(value, maximum)), None


def _parse_deck_entries(names_raw: str, *, correct_names: bool = True):
    if len(names_raw) > MAX_DECK_PARAM_CHARS:
        return None, (jsonify({"error": "入力が長すぎます"}), 400)

    entries = []
    for line in names_raw.split("|"):
        line = line.strip()
        if not line:
            continue
        if len(entries) >= MAX_DECK_CARDS:
            return None, (jsonify({"error": f"一括検索は最大{MAX_DECK_CARDS}枚までです"}), 400)

        m = _re.match(r"^(\d+)\s+(.+)$", line)
        qty = int(m.group(1)) if m else 1
        if qty < 1 or qty > MAX_DECK_QTY:
            return None, (jsonify({"error": f"枚数は1〜{MAX_DECK_QTY}で指定してください"}), 400)

        name = _normalize_query(m.group(2) if m else line)
        if not name:
            continue
        if len(name) > MAX_CARD_NAME_LEN:
            return None, (jsonify({"error": "カード名が長すぎます"}), 400)
        if correct_names:
            name = _correct_cardname(name)
        entries.append({"qty": qty, "name": name})

    if not entries:
        return None, (jsonify({"error": "カード名がありません"}), 400)
    return entries, None


def _require_health_key():
    """ヘルス系APIの認証。本番はキー未設定でも公開しない。"""
    if not HEALTH_CHECK_KEY:
        if _is_debug_mode():
            return None
        return jsonify({"error": "health check key is not configured"}), 503
    if request.args.get("key") != HEALTH_CHECK_KEY:
        return jsonify({"error": "unauthorized"}), 403
    return None


def _claim_startup_job(name: str, ttl_sec: int = 300) -> bool:
    """複数worker起動時に同じプリフェッチを同時実行しないための軽いファイルロック。"""
    cache_dir = os.path.join(os.path.dirname(__file__), ".cache")
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"startup_{name}.lock")
    now = time.time()
    try:
        if os.path.exists(path) and now - os.path.getmtime(path) < ttl_sec:
            return False
        if os.path.exists(path):
            os.remove(path)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(now))
        return True
    except FileExistsError:
        return False
    except Exception as e:
        logger.warning(f"起動時ジョブロック取得失敗 ({name}): {e}")
        return True

# トレンドキャッシュ（Supabaseへのクエリを減らす）
_trending_cache: list[dict] = []
_trending_cache_time: float = 0
_TRENDING_CACHE_SEC = 300  # 5分キャッシュ

def _record_search(card_name: str):
    """検索をランキングに記録（Supabase or メモリ）"""
    # メモリにも記録（フォールバック用）
    with _ranking_lock:
        now = time.time()
        _search_recent.append((now, card_name))
        cutoff = now - RANKING_RECENT_HOURS * 3600
        while _search_recent and _search_recent[0][0] < cutoff:
            _search_recent.pop(0)

    # Supabaseへの書き込みはバックグラウンドで（レスポンスを遅延させない）
    if _supabase_client:
        Thread(target=_save_search_to_supabase, args=(card_name,), daemon=True).start()

def _save_search_to_supabase(card_name: str):
    """Supabaseにログを非同期で保存"""
    try:
        _supabase_client.table("search_logs").insert({"card_name": card_name}).execute()
    except Exception as e:
        logger.warning(f"検索ログ保存失敗: {e}")

def _get_trending(limit: int = 10) -> list[dict]:
    """直近24時間の検索ランキングを返す"""
    global _trending_cache, _trending_cache_time

    # キャッシュが有効ならそれを返す
    now = time.time()
    if _trending_cache and now - _trending_cache_time < _TRENDING_CACHE_SEC:
        return _trending_cache[:limit]

    # Supabaseから取得を試みる
    if _supabase_client:
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=RANKING_RECENT_HOURS)).isoformat()
            resp = _supabase_client.rpc("get_search_ranking", {
                "since_ts": cutoff,
                "max_results": 30
            }).execute()
            if resp.data:
                result = [{"name": r.get("card_name", ""), "count": r.get("search_count", 0)} for r in resp.data if r.get("card_name")]
                _trending_cache = result
                _trending_cache_time = now
                return result[:limit]
        except Exception as e:
            logger.warning(f"ランキング取得失敗（メモリにフォールバック）: {e}")

    # メモリフォールバック
    with _ranking_lock:
        cutoff_ts = now - RANKING_RECENT_HOURS * 3600
        recent_counts: dict[str, int] = defaultdict(int)
        for ts, name in _search_recent:
            if ts >= cutoff_ts:
                recent_counts[name] += 1
        ranked = sorted(recent_counts.items(), key=lambda x: -x[1])
        result = [{"name": name, "count": count} for name, count in ranked[:30]]
        _trending_cache = result
        _trending_cache_time = now
        return result[:limit]


@app.route("/")
def index():
    return render_template("index.html", card_name="", page_mode="search")


@app.route("/card/<path:card_name>")
def card_page(card_name):
    """カード個別ページ — SEO用"""
    card_name = card_name.strip()
    if not card_name or len(card_name) > 50:
        return render_template("index.html", card_name="", page_mode="search"), 404

    # キャッシュに価格データがあれば meta 用に渡す
    cached = cache_get(card_name)
    best_price = None
    shop_count = 0
    if cached:
        in_stock = [r for r in cached if not r.get("sold_out")]
        if in_stock:
            best = min(in_stock, key=lambda x: x["price"])
            best_price = best["price"]
        shop_count = len(set(r["shop"] for r in cached))

    return render_template("index.html",
                           card_name=card_name,
                           page_mode="search",
                           best_price=best_price,
                           shop_count=shop_count)


@app.route("/buy/<path:card_name>")
def buy_page(card_name):
    """買取個別ページ — SEO用"""
    card_name = card_name.strip()
    if not card_name or len(card_name) > 50:
        return render_template("index.html", card_name="", page_mode="buyback"), 404

    cached = buyback_cache_get(card_name)
    best_price = None
    if cached:
        prices = [r["price"] for r in cached if r.get("price")]
        if prices:
            best_price = max(prices)

    return render_template("index.html",
                           card_name=card_name,
                           page_mode="buyback",
                           best_price=best_price,
                           shop_count=0)


@app.route("/sitemap.xml")
def sitemap():
    """動的サイトマップ — 検索されたカードを自動収集"""
    from flask import make_response
    base_url = request.url_root.rstrip("/")

    # ランキングからカード名を取得
    cards = _get_trending(limit=30)
    card_names = [c["name"] for c in cards]

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    # トップページ
    xml += f'  <url><loc>{base_url}/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>\n'
    # 固定ページ
    xml += f'  <url><loc>{base_url}/solitaire</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>\n'
    # カード個別ページ
    for name in card_names:
        from urllib.parse import quote
        xml += f'  <url><loc>{base_url}/card/{quote(name)}</loc><changefreq>daily</changefreq><priority>0.8</priority></url>\n'
        xml += f'  <url><loc>{base_url}/buy/{quote(name)}</loc><changefreq>daily</changefreq><priority>0.7</priority></url>\n'
    xml += '</urlset>'

    resp = make_response(xml)
    resp.headers["Content-Type"] = "application/xml"
    return resp


@app.route("/robots.txt")
def robots():
    """robots.txt"""
    from flask import make_response
    base_url = request.url_root.rstrip("/")
    txt = f"User-agent: *\nAllow: /\nDisallow: /api/\n\nSitemap: {base_url}/sitemap.xml\n"
    resp = make_response(txt)
    resp.headers["Content-Type"] = "text/plain"
    return resp


# ── カード名サジェスト（ローカルファイル参照）──

_cardnames: list[str] = []
_cardnames_set: set[str] = set()               # 存在チェック用（O(1)）
_cardnames_fuzzy: dict[str, list[str]] = {}   # fuzzy_key → [正式カード名]
_cardnames_reading: dict[str, str] = {}       # 読み仮名 → 正式カード名
_cardnames_reading_fuzzy: dict[str, str] = {} # 読み仮名(fuzzy_key) → 正式カード名
_cardnames_loaded = False

def _load_cardnames():
    """data/cardnames_ja.json と reading マップをメモリに読み込む"""
    global _cardnames, _cardnames_set, _cardnames_fuzzy, _cardnames_reading, _cardnames_reading_fuzzy, _cardnames_loaded
    if _cardnames_loaded:
        return
    base = os.path.dirname(__file__)
    cardnames_path = os.path.join(base, "data", "cardnames_ja.json")
    reading_path = os.path.join(base, "data", "cardnames_reading.json")
    try:
        with open(cardnames_path, "r", encoding="utf-8") as f:
            _cardnames = json.loads(f.read())
        # あいまい検索用のインデックスを構築
        fuzzy = {}
        for name in _cardnames:
            key = _fuzzy_key(name)
            fuzzy.setdefault(key, []).append(name)
        _cardnames_set = set(_cardnames)
        _cardnames_fuzzy = fuzzy
        logger.info(f"カード名データベース読込: {len(_cardnames)} 件")
    except FileNotFoundError:
        logger.warning("data/cardnames_ja.json が見つかりません。update_cardnames.py を実行してください。")
        _cardnames = []
        _cardnames_set = set()
    # 読み仮名→正式名称マップ
    try:
        with open(reading_path, "r", encoding="utf-8") as f:
            _cardnames_reading = json.loads(f.read())
        # 読み仮名のfuzzyキーマップも構築（記号除去版で引けるように）
        _cardnames_reading_fuzzy = {_fuzzy_key(r): formal for r, formal in _cardnames_reading.items()}
        logger.info(f"読み仮名マップ読込: {len(_cardnames_reading)} 件")
    except FileNotFoundError:
        _cardnames_reading = {}
        _cardnames_reading_fuzzy = {}
    _cardnames_loaded = True

@app.route("/api/suggest")
def api_suggest():
    """カード名の入力補完候補を返す（あいまい検索対応）"""
    _load_cardnames()
    q = _normalize_query(request.args.get("q", ""))
    if not q or len(q) < 1:
        return jsonify([])

    q_lower = q.lower()
    q_fuzzy = _fuzzy_key(q)

    # 前方一致を優先、次に部分一致、最後にあいまい一致
    prefix_matches = []
    contains_matches = []
    fuzzy_matches = []
    seen = set()
    for name in _cardnames:
        name_lower = name.lower()
        if name_lower.startswith(q_lower):
            prefix_matches.append(name)
            seen.add(name)
        elif q_lower in name_lower:
            contains_matches.append(name)
            seen.add(name)
        if len(prefix_matches) + len(contains_matches) >= 15:
            break

    # あいまい検索（記号除去して比較）: 通常一致が少ない場合のみ
    if len(prefix_matches) + len(contains_matches) < 10 and q_fuzzy:
        for name in _cardnames:
            if name in seen:
                continue
            name_fuzzy = _fuzzy_key(name)
            if name_fuzzy.startswith(q_fuzzy) or q_fuzzy in name_fuzzy:
                fuzzy_matches.append(name)
                seen.add(name)
                if len(prefix_matches) + len(contains_matches) + len(fuzzy_matches) >= 15:
                    break

    # 読み仮名検索: まだ候補が少ない場合、読み仮名から正式名称を引く
    reading_matches = []
    total = len(prefix_matches) + len(contains_matches) + len(fuzzy_matches)
    if total < 10 and _cardnames_reading:
        for reading, formal in _cardnames_reading.items():
            if formal in seen:
                continue
            if reading.startswith(q_lower) or q_lower in reading:
                reading_matches.append(formal)
                seen.add(formal)
                if total + len(reading_matches) >= 15:
                    break

    results = (prefix_matches + contains_matches + fuzzy_matches + reading_matches)[:10]
    return jsonify(results)


@app.route("/api/validate")
def api_validate():
    """カード名の存在チェック — 補正候補があれば suggestion として返す"""
    _load_cardnames()
    q = _normalize_query(request.args.get("q", ""))
    if not q:
        return jsonify({"valid": False, "suggestion": None})
    # DB上の正式名称に補正を試みる
    corrected = _correct_cardname(q)
    if corrected in _cardnames_set:
        # 補正成功、またはそのまま一致
        return jsonify({"valid": True, "name": corrected})
    # 補正できなかった（corrected == q のまま、かつDBに存在しない）
    # TODO: _correct_cardname()が部分一致候補を返せるようになったらsuggestionを活用
    return jsonify({"valid": False, "suggestion": None})


@app.route("/api/search")
def api_search():
    original_name = _normalize_query(request.args.get("q", ""))
    if not original_name:
        return jsonify({"error": "カード名を入力してください"}), 400
    if len(original_name) > MAX_CARD_NAME_LEN:
        return jsonify({"error": "カード名が長すぎます"}), 400

    # カード名の存在チェック — DBに無い場合はあいまい検索・読み仮名で自動補正
    _load_cardnames()
    card_name = _correct_cardname(original_name)
    corrected = card_name if card_name != original_name else ""

    ALL_SHOP_NAMES = [name for name, _ in SHOPS]
    selected = request.args.getlist("shops") or DEFAULT_SHOPS
    selected = [s for s in selected if s in ALL_SHOP_NAMES]
    if not selected:
        return jsonify({"error": "有効な店舗を選択してください"}), 400

    active_shops = [(name, fn) for name, fn in SHOPS if name in selected]

    rate_error = _consume_rate_limit()
    if rate_error:
        return rate_error

    # 第1層: DB非存在のカード名はconfirmedフラグなしでは拒否（API防御）
    confirmed = request.args.get("confirmed", "").lower() == "true"
    if not confirmed and card_name not in _cardnames_set:
        return jsonify({"error": "該当するカード名が見つかりません"}), 404

    # キャッシュヒット
    cached = cache_get(card_name)
    if cached is not None:
        if cached:
            _record_search(card_name)
        filtered = [r for r in cached if r["shop"] in selected]
        def cached_stream():
            for shop_name, _ in active_shops:
                count = sum(1 for r in filtered if r["shop"] == shop_name)
                yield _sse({"type": "shop_done", "shop": shop_name, "count": count, "cached": True})
            yield _sse(_build_done(filtered, corrected))
        return Response(cached_stream(), mimetype="text/event-stream")

    def generate():
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # 補正があった場合、補正通知を最初に送信
        if corrected:
            yield _sse({"type": "corrected", "original": original_name, "corrected": corrected})

        # 全店舗を「検索中」に
        for shop_name, _ in active_shops:
            yield _sse({"type": "progress", "shop": shop_name})

        all_results = []

        def scrape_shop(name, fn):
            try:
                items = fn(card_name)
                if items:
                    tracker.record_success(name, len(items))
                else:
                    tracker.record_failure(name, f"0件取得 (検索: {card_name})")
                return name, items, None
            except Exception as e:
                tracker.record_failure(name, str(e))
                logger.error(f"{name} エラー: {e}")
                return name, [], str(e)

        try:
            with ThreadPoolExecutor(max_workers=len(active_shops)) as executor:
                futures = {
                    executor.submit(scrape_shop, name, fn): name
                    for name, fn in active_shops
                }
                for future in as_completed(futures):
                    shop_name, results, error = future.result()
                    if error:
                        yield _sse({"type": "shop_error", "shop": shop_name, "error": error})
                    # 店舗完了時に結果データも送信（逐次表示用）
                    yield _sse({"type": "shop_done", "shop": shop_name, "count": len(results), "results": results})
                    all_results.extend(results)

            cache_set(card_name, all_results)
            if all_results:
                _record_search(card_name)
        except Exception as e:
            logger.error(f"検索処理で予期しないエラー: {e}")
        finally:
            # エラーが起きても必ず完了メッセージを送る
            yield _sse(_build_done(all_results, corrected))

    return Response(generate(), mimetype="text/event-stream")


# ── ランキング・設定 ──

@app.route("/api/deck")
def api_deck():
    """デッキ一括検索 — 複数カード名を受け取り、各カードの最安値をSSEで返す

    v3: 高速化 — 各店舗1ページ目のみ取得 + カード5枚同時並列
    """
    from functools import partial
    from scraper import scrape_torecolo, scrape_kanabell

    names_raw = request.args.get("cards", "")
    shops_raw = request.args.getlist("shops") or DEFAULT_SHOPS
    if not names_raw:
        return jsonify({"error": "カード名がありません"}), 400
    rate_error = _consume_rate_limit()
    if rate_error:
        return rate_error

    ALL_SHOP_NAMES = [name for name, _ in SHOPS]
    selected = [s for s in shops_raw if s in ALL_SHOP_NAMES]
    if not selected:
        return jsonify({"error": "有効な店舗を選択してください"}), 400

    # デッキ検索用: 複数ページ巡回する店舗は1ページ目のみに制限
    _FAST_OVERRIDES = {
        "トレコロCB": partial(scrape_torecolo, max_pages=1),
        "カーナベル": partial(scrape_kanabell, max_pages=1),
    }
    active_shops = [
        (name, _FAST_OVERRIDES.get(name, fn))
        for name, fn in SHOPS if name in selected
    ]

    _load_cardnames()
    card_entries, parse_error = _parse_deck_entries(names_raw)
    if parse_error:
        return parse_error

    def _search_one_card(i, card_name):
        """1枚のカードを全店舗で検索して結果を返す"""
        # キャッシュチェック
        cached = cache_get(card_name)
        if cached is not None:
            items = [r for r in cached if r["shop"] in selected and not r.get("sold_out")]
            best = min(items, key=lambda x: x["price"]) if items else None
            return {"type": "card_done", "index": i, "name": card_name,
                    "best": best, "count": len(items), "cached": True}

        # 全店舗を並列スクレイピング
        all_items = []
        with ThreadPoolExecutor(max_workers=len(active_shops)) as shop_executor:
            futures = {shop_executor.submit(fn, card_name): name
                       for name, fn in active_shops}
            for future in as_completed(futures):
                try:
                    all_items.extend(future.result())
                except Exception:
                    pass

        cache_set(card_name, all_items)
        in_stock = [r for r in all_items if not r.get("sold_out")]
        best = min(in_stock, key=lambda x: x["price"]) if in_stock else None
        return {"type": "card_done", "index": i, "name": card_name,
                "best": best, "count": len(in_stock)}

    def generate():
        # カード単位で並列検索（最大5カード同時）
        CARD_PARALLEL = 5

        # まず全カードの card_start を送出
        for i, entry in enumerate(card_entries):
            yield _sse({"type": "card_start", "index": i, "name": entry["name"]})

        # 並列でカードを検索
        with ThreadPoolExecutor(max_workers=CARD_PARALLEL) as card_executor:
            futures = {
                card_executor.submit(_search_one_card, i, entry["name"]): i
                for i, entry in enumerate(card_entries)
            }
            for future in as_completed(futures):
                try:
                    result = future.result()
                    yield _sse(result)
                except Exception as e:
                    idx = futures[future]
                    yield _sse({"type": "card_done", "index": idx,
                                "name": card_entries[idx]["name"],
                                "best": None, "count": 0})

        yield _sse({"type": "done"})

    return Response(generate(), mimetype="text/event-stream")

@app.route("/api/deck-buy")
def api_deck_buy():
    """デッキ一括買取検索 — 複数カード名の買取最高値をSSEで返す"""
    names_raw = request.args.get("cards", "")
    shops_raw = request.args.getlist("shops") or DEFAULT_BUYBACK_SHOPS
    if not names_raw:
        return jsonify({"error": "カード名がありません"}), 400
    rate_error = _consume_rate_limit()
    if rate_error:
        return rate_error

    ALL_BUY_NAMES = [name for name, _ in BUYBACK_SHOPS]
    selected = [s for s in shops_raw if s in ALL_BUY_NAMES]
    if not selected:
        return jsonify({"error": "有効な店舗を選択してください"}), 400
    active_shops = [(name, fn) for name, fn in BUYBACK_SHOPS if name in selected]

    _load_cardnames()
    card_entries, parse_error = _parse_deck_entries(names_raw)
    if parse_error:
        return parse_error

    def _search_one_card_buy(i, card_name):
        """1枚のカードを全買取店舗で検索して最高値を返す"""
        cached = buyback_cache_get(card_name)
        if cached is not None:
            items = [r for r in cached if r["shop"] in selected]
            best = max(items, key=lambda x: x["price"]) if items else None
            return {"type": "card_done", "index": i, "name": card_name,
                    "best": best, "count": len(items), "cached": True}

        all_items = []
        with ThreadPoolExecutor(max_workers=len(active_shops)) as shop_executor:
            futures = {shop_executor.submit(fn, card_name): name
                       for name, fn in active_shops}
            for future in as_completed(futures):
                try:
                    all_items.extend(future.result())
                except Exception:
                    pass

        buyback_cache_set(card_name, all_items)
        best = max(all_items, key=lambda x: x["price"]) if all_items else None
        return {"type": "card_done", "index": i, "name": card_name,
                "best": best, "count": len(all_items)}

    def generate():
        CARD_PARALLEL = 5
        for i, entry in enumerate(card_entries):
            yield _sse({"type": "card_start", "index": i, "name": entry["name"]})

        with ThreadPoolExecutor(max_workers=CARD_PARALLEL) as card_executor:
            futures = {
                card_executor.submit(_search_one_card_buy, i, entry["name"]): i
                for i, entry in enumerate(card_entries)
            }
            for future in as_completed(futures):
                try:
                    result = future.result()
                    yield _sse(result)
                except Exception as e:
                    idx = futures[future]
                    yield _sse({"type": "card_done", "index": idx,
                                "name": card_entries[idx]["name"],
                                "best": None, "count": 0})

        yield _sse({"type": "done"})

    return Response(generate(), mimetype="text/event-stream")

# ショップ名からカード検索URLを生成
def _shop_search_url(shop: str, card_name: str) -> str:
    """ショップ名とカード名から検索ページのURLを生成"""
    q = _url_quote(card_name)
    urls = {
        "遊々亭": f"https://yuyu-tei.jp/sell/ygo/s/search?search_word={q}",
        "カードラッシュ": f"https://www.cardrush.jp/product-list?keyword={q}",
        "トレコロCB": f"https://www.torecolo.jp/shop/goods/search.aspx?search=x&keyword={q}&category=&oshiire_code=",
        "カーナベル": f"https://www.ka-nabell.com/?act=sell_search&genre=1&keyword={q}",
        "カードラボ": f"https://www.c-labo-online.jp/product-list?keyword={q}",
        "まんぞく屋": f"https://shopmanzokuya.com/products/list?category_id=1&name={q}&orderby=price_l&disp_number=100",
        "駿河屋": f"https://www.suruga-ya.jp/search?category=501&search_word={q}",
    }
    return urls.get(shop, "")

# 相場キャッシュ — サーバー起動時にSupabaseから全件ロードし、10分ごとに更新
_estimate_cache: dict[str, dict] = {}
_estimate_cache_time: float = 0
_ESTIMATE_CACHE_SEC = 600

def _load_estimate_cache(startup: bool = False):
    """Supabaseから直近7日の全カード最安値をメモリにロード。
    startup=True のときはジッターを入れてワーカー間の同時アクセスを分散させる。
    """
    global _estimate_cache, _estimate_cache_time
    if not _supabase_client:
        return

    if startup:
        jitter = random.uniform(0, 6)
        logger.info(f"相場キャッシュ: {jitter:.1f}秒後に開始")
        time.sleep(jitter)

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            resp = (_supabase_client.rpc("get_card_best_prices", {"cutoff_date": cutoff})
                    .limit(5000)
                    .execute())
            rows = resp.data or []

            card_best = {}
            for row in rows:
                name = row.get("card_name", "")
                if not name:
                    continue
                card_best[name] = {
                    "shop": row.get("shop", ""),
                    "price": row.get("min_price", 0),
                    "rarity": row.get("rarity", ""),
                    "recorded_at": row.get("recorded_at", ""),
                }
            _estimate_cache = card_best
            _estimate_cache_time = time.time()
            logger.info(f"相場キャッシュロード完了: {len(card_best)}カード")
            return
        except Exception as e:
            wait = 5 * attempt
            logger.error(f"相場キャッシュロード失敗 (試行{attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                logger.info(f"  {wait}秒後にリトライします")
                time.sleep(wait)

# サーバー起動時にキャッシュをバックグラウンドでロード（複数workerでは1回だけ）
if _claim_startup_job("estimate_cache"):
    Thread(target=_load_estimate_cache, kwargs={"startup": True}, daemon=True).start()

@app.route("/api/deck-estimate", methods=["GET", "POST"])
def api_deck_estimate():
    """デッキ相場見積もり — メモリキャッシュから即時返却。
    GETは?cards=、POSTはJSON body {"cards":"..."} でカード名を受け取る（URL長制限回避）。
    """
    global _estimate_cache_time
    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        names_raw = body.get("cards", "")
    else:
        names_raw = request.args.get("cards", "")
    if not names_raw:
        return jsonify({"error": "カード名がありません"}), 400

    # キャッシュが古ければバックグラウンドで更新
    if time.time() - _estimate_cache_time > _ESTIMATE_CACHE_SEC:
        Thread(target=_load_estimate_cache, daemon=True).start()

    card_entries, parse_error = _parse_deck_entries(names_raw, correct_names=False)
    if parse_error:
        return parse_error

    results = []
    total = 0
    for entry in card_entries:
        name = entry["name"]
        qty = entry["qty"]
        best = _estimate_cache.get(name)
        if best:
            total += best["price"] * qty
            # DBデータにはURLが無いため、ショップ名から検索URLを生成
            best = {**best, "url": _shop_search_url(best["shop"], name)}
        results.append({"name": name, "qty": qty, "best": best})

    return jsonify({"results": results, "total": total})

@app.route("/api/price-history")
def api_price_history():
    """カードの価格推移データを返す"""
    card_name = request.args.get("card", "").strip()
    if not card_name:
        return jsonify({"card_name": "", "data": []})
    card_name = _normalize_query(card_name)

    # カード名の自動補正
    _load_cardnames()
    card_name = _correct_cardname(card_name)

    if not _supabase_client:
        return jsonify({"card_name": card_name, "data": []})

    try:
        cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        resp = (_supabase_client.table("price_history")
                .select("shop, rarity, min_price, recorded_at")
                .eq("card_name", card_name)
                .gte("recorded_at", cutoff)
                .order("recorded_at", desc=False)
                .limit(2000)
                .execute())
        data = [
            {"date": r.get("recorded_at", "")[:10], "shop": r.get("shop", ""),
             "rarity": r.get("rarity", ""), "price": r.get("min_price", 0)}
            for r in (resp.data or [])
        ]
        return jsonify({"card_name": card_name, "data": data})
    except Exception as e:
        logger.error(f"価格推移取得失敗: {e}")
        return jsonify({"card_name": card_name, "data": []})

TRENDING_MIN_ITEMS = 10  # この件数未満は「利用者が少ない」と見なし非表示

@app.route("/api/trending")
def api_trending():
    """直近24時間の検索ランキングを返す"""
    limit, limit_error = _parse_limit_param(10, 30)
    if limit_error:
        return limit_error
    items = _get_trending(limit)
    if len(items) < TRENDING_MIN_ITEMS:
        return jsonify([])
    return jsonify(items)

# ── 値上がり/値下がりランキング（キャッシュ付き） ──
_movers_cache: dict[str, list] = {}
_movers_cache_time: float = 0
_MOVERS_CACHE_SEC = 3600  # 1時間キャッシュ（収集完了後にサイト表示を当日データへ早く追従させる）

def _aggregate_daily_min(rows: list) -> dict:
    """price_history の行リストから、カードごと・日付ごとの最安値を集計して返す。
    戻り値: {card_name: {date_str: min_price}}
    10円以下の異常値は除外する。
    """
    card_dates: dict = defaultdict(dict)
    for r in rows:
        name = r.get("card_name", "")
        date = r.get("recorded_at", "")[:10]
        price = r.get("min_price", 0)
        if not name or not date:
            continue
        if price <= 10:
            continue
        if date not in card_dates[name] or price < card_dates[name][date]:
            card_dates[name][date] = price
    return card_dates

def _get_price_movers(direction: str, limit: int = 10) -> list[dict]:
    """Supabaseから値上がり/値下がりランキングを取得（DB側集計RPC版）。
    get_price_movers RPC が全件集計を DB 側で処理し、上位 top_n 行だけ返す。
    アプリ側の全件メモリ展開を廃止し、データ増加に対してメモリが増えない構造に変更。
    """
    global _movers_cache, _movers_cache_time

    now = time.time()
    if _movers_cache and now - _movers_cache_time < _MOVERS_CACHE_SEC:
        return _movers_cache.get(direction, [])[:limit]

    if not _supabase_client:
        return []

    try:
        cutoff = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        resp = (_supabase_client
                .rpc("get_price_movers", {"cutoff_date": cutoff, "min_diff": 50, "top_n": 20})
                .execute())
        rows = resp.data or []
        logger.info(f"値動きランキング: {len(rows)}行取得（RPC集計）")

        if not rows:
            return []

        # RPC が direction 別に返すので振り分けるだけ
        up, down = [], []
        date_new = date_old = None
        for row in rows:
            entry = {
                "name":      row.get("card_name", ""),
                "today":     row.get("today_price", 0),
                "yesterday": row.get("prev_price", 0),
                "diff":      row.get("diff", 0),
                "pct":       float(row.get("pct", 0)),
            }
            if row.get("direction") == "up":
                up.append(entry)
            else:
                down.append(entry)
            if date_new is None:
                date_new = row.get("date_new")
                date_old = row.get("date_old")

        # 空データはキャッシュしない（プリロード時点でデータが揃っていなかった場合に24時間空を返し続けるバグを防ぐ）
        if up or down:
            _movers_cache = {"up": up, "down": down, "date_old": date_old, "date_new": date_new}
            _movers_cache_time = now
        return (up if direction == "up" else down)[:limit]
    except Exception as e:
        logger.warning(f"価格変動ランキング取得失敗: {e}")
        return []

@app.route("/api/movers")
def api_movers():
    """値上がり/値下がりランキングを返す"""
    direction = request.args.get("direction", "up")
    if direction not in ("up", "down"):
        return jsonify({"error": "direction は up または down"}), 400
    limit, limit_error = _parse_limit_param(10, 20)
    if limit_error:
        return limit_error
    items = _get_price_movers(direction, limit)
    date_old = _movers_cache.get("date_old") if _movers_cache else None
    date_new = _movers_cache.get("date_new") if _movers_cache else None
    JST = timezone(timedelta(hours=9))
    if _movers_cache_time:
        updated_dt = datetime.fromtimestamp(_movers_cache_time, JST)
        updated_at = f"{updated_dt.hour}:{updated_dt.minute:02d}"
    else:
        updated_at = None
    return jsonify({"items": items, "date_old": date_old, "date_new": date_new, "updated_at": updated_at})


# ── 買取値上がり/値下がりランキング（buyback_history テーブル、最高買取額ベース）──
_buyback_movers_cache: dict[str, list] = {}
_buyback_movers_cache_time: float = 0
_BUYBACK_MOVERS_CACHE_SEC = 3600  # 1時間キャッシュ

def _aggregate_daily_max(rows: list) -> dict:
    """buyback_history の行リストから、カードごと・日付ごとの最高買取額を集計して返す。
    戻り値: {card_name: {date_str: max_price}}
    10円以下の異常値は除外する。
    """
    card_dates: dict = defaultdict(dict)
    for r in rows:
        name = r.get("card_name", "")
        date = r.get("recorded_at", "")[:10]
        price = r.get("max_price", 0)
        if not name or not date:
            continue
        if price <= 10:
            continue
        # 最大値を保持（販売は最小値だが買取は最大値が意味を持つ）
        if date not in card_dates[name] or price > card_dates[name][date]:
            card_dates[name][date] = price
    return card_dates

def _get_buyback_movers(direction: str, limit: int = 10) -> list[dict]:
    """Supabaseから買取の値上がり/値下がりランキングを取得（DB側集計RPC版）。
    get_buyback_movers RPC が全件集計を DB 側で処理し、上位 top_n 行だけ返す。
    アプリ側の全件メモリ展開を廃止し、データ増加に対してメモリが増えない構造に変更。
    """
    global _buyback_movers_cache, _buyback_movers_cache_time

    now = time.time()
    if _buyback_movers_cache and now - _buyback_movers_cache_time < _BUYBACK_MOVERS_CACHE_SEC:
        return _buyback_movers_cache.get(direction, [])[:limit]

    if not _supabase_client:
        return []

    try:
        cutoff = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        resp = (_supabase_client
                .rpc("get_buyback_movers", {"cutoff_date": cutoff, "min_diff": 50, "top_n": 20})
                .execute())
        rows = resp.data or []
        logger.info(f"買取値動きランキング: {len(rows)}行取得（RPC集計）")

        if not rows:
            return []

        # RPC が direction 別に返すので振り分けるだけ
        up, down = [], []
        date_new = date_old = None
        for row in rows:
            entry = {
                "name":      row.get("card_name", ""),
                "today":     row.get("today_price", 0),
                "yesterday": row.get("prev_price", 0),
                "diff":      row.get("diff", 0),
                "pct":       float(row.get("pct", 0)),
            }
            if row.get("direction") == "up":
                up.append(entry)
            else:
                down.append(entry)
            if date_new is None:
                date_new = row.get("date_new")
                date_old = row.get("date_old")

        # 空データはキャッシュしない（データ未蓄積時に24時間空を返し続けるバグを防ぐ）
        if up or down:
            _buyback_movers_cache = {"up": up, "down": down, "date_old": date_old, "date_new": date_new}
            _buyback_movers_cache_time = now
        return (up if direction == "up" else down)[:limit]
    except Exception as e:
        logger.warning(f"買取価格変動ランキング取得失敗: {e}")
        return []

@app.route("/api/buyback-movers")
def api_buyback_movers():
    """買取の値上がり/値下がりランキングを返す"""
    direction = request.args.get("direction", "up")
    if direction not in ("up", "down"):
        return jsonify({"error": "direction は up または down"}), 400
    limit, limit_error = _parse_limit_param(10, 20)
    if limit_error:
        return limit_error
    items = _get_buyback_movers(direction, limit)
    date_old = _buyback_movers_cache.get("date_old") if _buyback_movers_cache else None
    date_new = _buyback_movers_cache.get("date_new") if _buyback_movers_cache else None
    JST = timezone(timedelta(hours=9))
    if _buyback_movers_cache_time:
        updated_dt = datetime.fromtimestamp(_buyback_movers_cache_time, JST)
        updated_at = f"{updated_dt.hour}:{updated_dt.minute:02d}"
    else:
        updated_at = None
    return jsonify({"items": items, "date_old": date_old, "date_new": date_new, "updated_at": updated_at})


# ── 購入候補（wishlist）価格アラート ──

def _track_card_async(card_name: str):
    """tracked_cards に非同期で登録する（既存チェック→なければinsert）。"""
    try:
        resp = (_supabase_client.table("tracked_cards")
                .select("card_name")
                .eq("card_name", card_name)
                .limit(1)
                .execute())
        if not resp.data:
            _supabase_client.table("tracked_cards") \
                .insert({"card_name": card_name, "active": True}) \
                .execute()
            logger.info(f"[track] tracked_cards に追加: {card_name}")
    except Exception as e:
        logger.warning(f"[track] 追加失敗 ({card_name}): {e}")


@app.route("/api/track", methods=["POST"])
def api_track():
    """購入候補に追加されたカードを価格収集対象（tracked_cards）に登録する。
    入力: {"card": "カード名"}
    返却: {"ok": true, "card": "補正後カード名"} または {"ok": false, "reason": "unknown_card"}
    ログイン不要・fire-and-forget で使用する軽量エンドポイント。
    """
    data = request.get_json(silent=True) or {}
    card_name_raw = str(data.get("card", "")).strip()

    if not card_name_raw:
        return jsonify({"error": "カード名を指定してください"}), 400
    if len(card_name_raw) > MAX_CARD_NAME_LEN:
        return jsonify({"error": "カード名が長すぎます"}), 400

    _load_cardnames()
    corrected = _correct_cardname(card_name_raw)

    # カード辞書に存在しない文字列は登録拒否（スパム・誤登録対策）
    # O(1) のset検索を使う（_cardnames listへのO(n)検索は避ける）
    if _cardnames_set and corrected not in _cardnames_set:
        return jsonify({"ok": False, "reason": "unknown_card"}), 200

    if _supabase_client:
        Thread(target=_track_card_async, args=(corrected,), daemon=True).start()

    return jsonify({"ok": True, "card": corrected})


# 値下がり判定の閾値
_WISH_DROP_PCT = -5.0   # -5% 以上の下落
_WISH_DROP_ABS = -50    # かつ 50円以上安い


@app.route("/api/wish-prices", methods=["POST"])
def api_wish_prices():
    """購入候補カードの最新価格と7日前比を返す。
    入力: {"cards": ["カード名", ...]}  最大50件
    返却: {"cards": [{name, latest, base_7d, diff, pct, status}, ...]}
      status: "ready" = データあり, "pending" = まだ収集されていない
    """
    global _estimate_cache_time

    data = request.get_json(silent=True) or {}
    cards_raw = data.get("cards", [])
    if not isinstance(cards_raw, list):
        return jsonify({"error": "cards は配列で指定してください"}), 400

    # 最大50件・名前補正
    _load_cardnames()
    card_names = []
    for name in cards_raw[:50]:
        name = str(name).strip()
        if name and len(name) <= MAX_CARD_NAME_LEN:
            card_names.append(_correct_cardname(name))
    if not card_names:
        return jsonify({"cards": []})

    # _estimate_cache でデータ有無を事前判定（DBクエリを最小化）
    if time.time() - _estimate_cache_time > _ESTIMATE_CACHE_SEC:
        Thread(target=_load_estimate_cache, daemon=True).start()

    have_data = [n for n in card_names if n in _estimate_cache]
    pending_set = set(card_names) - set(have_data)

    def _fetch_price_history(names: list, days: int) -> dict:
        """price_history から指定日数分の日次最安値を取得して返す"""
        if not names or not _supabase_client:
            return {}
        try:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            all_rows: list = []
            page_size = 1000
            offset = 0
            while True:
                resp = (
                    _supabase_client.table("price_history")
                    .select("card_name, min_price, recorded_at")
                    .in_("card_name", names)
                    .gte("recorded_at", cutoff)
                    .order("recorded_at", desc=False)
                    .range(offset, offset + page_size - 1)
                    .execute()
                )
                batch = resp.data or []
                all_rows.extend(batch)
                if len(batch) < page_size:
                    break
                offset += page_size
            return dict(_aggregate_daily_min(all_rows))
        except Exception as e:
            logger.warning(f"[wish-prices] 価格履歴取得失敗: {e}")
            return {}

    # 主クエリ: estimate_cacheにあるカードを8日分で取得
    card_dates: dict = _fetch_price_history(have_data, days=8)

    # フォールバック: estimateキャッシュ未収録のカードを30日分で再検索
    if pending_set:
        fallback = _fetch_price_history(list(pending_set), days=30)
        for name, dates in fallback.items():
            if dates:
                card_dates[name] = dates
                pending_set.discard(name)
                have_data.append(name)

    results = []
    for name in card_names:
        if name in pending_set:
            results.append({"name": name, "latest": None, "base_7d": None,
                            "diff": None, "pct": None, "status": "pending",
                            "image": _ygores_image_url(name)})
            continue

        dates = card_dates.get(name, {})
        dates_sorted = sorted(dates.keys())
        latest_price = dates[dates_sorted[-1]] if dates_sorted else None
        base_7d = dates[dates_sorted[0]] if len(dates_sorted) >= 2 else None

        diff = None
        pct = None
        if latest_price is not None and base_7d is not None and base_7d > 0:
            diff = latest_price - base_7d
            pct = round((diff / base_7d) * 100, 1)

        results.append({
            "name": name,
            "latest": latest_price,
            "base_7d": base_7d,
            "diff": diff,
            "pct": pct,
            "status": "ready",
            "image": _ygores_image_url(name),
        })

    return jsonify({"cards": results})


@app.route("/api/buyback")
def api_buyback():
    """買取価格比較 — 各店舗の買取価格をSSEで返す"""
    card_name = _normalize_query(request.args.get("q", ""))
    if not card_name:
        return jsonify({"error": "カード名を入力してください"}), 400
    if len(card_name) > MAX_CARD_NAME_LEN:
        return jsonify({"error": "カード名が長すぎます"}), 400

    # カード名の自動補正
    _load_cardnames()
    card_name = _correct_cardname(card_name)

    ALL_BUY_NAMES = [name for name, _ in BUYBACK_SHOPS]
    selected = request.args.getlist("shops") or DEFAULT_BUYBACK_SHOPS
    selected = [s for s in selected if s in ALL_BUY_NAMES]
    if not selected:
        return jsonify({"error": "有効な店舗を選択してください"}), 400

    active_shops = [(name, fn) for name, fn in BUYBACK_SHOPS if name in selected]

    rate_error = _consume_rate_limit()
    if rate_error:
        return rate_error

    # 第1層: DB非存在のカード名はconfirmedフラグなしでは拒否（API防御）
    confirmed = request.args.get("confirmed", "").lower() == "true"
    if not confirmed and card_name not in _cardnames_set:
        return jsonify({"error": "該当するカード名が見つかりません"}), 404

    # キャッシュヒット
    cached = buyback_cache_get(card_name)
    if cached is not None:
        if cached:
            _record_search(card_name)
        filtered = [r for r in cached if r["shop"] in selected]
        def cached_stream():
            for shop_name, _ in active_shops:
                count = sum(1 for r in filtered if r["shop"] == shop_name)
                yield _sse({"type": "shop_done", "shop": shop_name, "count": count, "cached": True})
            yield _sse(_build_buyback_done(filtered))
        return Response(cached_stream(), mimetype="text/event-stream")

    def generate():
        for shop_name, _ in active_shops:
            yield _sse({"type": "progress", "shop": shop_name})

        all_results = []

        def scrape_shop(name, fn):
            try:
                items = fn(card_name)
                return name, items, None
            except Exception as e:
                logger.error(f"買取 {name} エラー: {e}")
                return name, [], str(e)

        try:
            with ThreadPoolExecutor(max_workers=len(active_shops)) as executor:
                futures = {
                    executor.submit(scrape_shop, name, fn): name
                    for name, fn in active_shops
                }
                for future in as_completed(futures):
                    shop_name, results, error = future.result()
                    if error:
                        yield _sse({"type": "shop_error", "shop": shop_name, "error": error})
                    yield _sse({"type": "shop_done", "shop": shop_name, "count": len(results)})
                    all_results.extend(results)

            buyback_cache_set(card_name, all_results)
            if all_results:
                _record_search(card_name)
        except Exception as e:
            logger.error(f"買取検索で予期しないエラー: {e}")
        finally:
            yield _sse(_build_buyback_done(all_results))

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/config")
def api_config():
    """フロントエンド用の設定情報"""
    # アフィリエイト設定をフロントエンドに渡す
    aff = {}
    for shop, conf in AFFILIATE_CONFIG.items():
        if conf.get("sell_a8mat") or conf.get("buy_a8mat"):
            aff[shop] = {
                "sell": conf.get("sell_a8mat", ""),
                "buy": conf.get("buy_a8mat", ""),
            }
        elif conf.get("user_id"):
            aff[shop] = {"user_id": conf["user_id"]}
    return jsonify({
        "shops": [{"name": name, "default": name in DEFAULT_SHOPS} for name, _ in SHOPS],
        "buyback_shops": [{"name": name, "default": name in DEFAULT_BUYBACK_SHOPS} for name, _ in BUYBACK_SHOPS],
        "affiliate": aff,
    })


# ── 環境データ（TCG PORTAL） ──

_meta_executor = ThreadPoolExecutor(max_workers=4)

@app.route("/api/meta")
def api_meta():
    """環境Tier表を返す（キャッシュヒットなら即座、ミスならスレッドプール経由）"""
    force = request.args.get("force") == "1"

    # キャッシュがあればExecutorを通さず即返却
    if not force:
        cached = _cache_read("tier_list", _TIER_CACHE_TTL)
        if cached and "tiers" in cached:
            return jsonify(cached["tiers"])

    future = _meta_executor.submit(fetch_tier_list, force)
    try:
        tiers = future.result(timeout=25)
    except Exception as e:
        logger.error(f"Tier表取得エラー: {e}")
        tiers = []
    return jsonify(tiers)

@app.route("/api/meta/deck")
def api_meta_deck():
    """テーマ別の主要カードを返す"""
    theme = request.args.get("theme", "").strip()
    if not theme:
        return jsonify({"error": "テーマ名を指定してください"}), 400
    force = request.args.get("force") == "1"

    # キャッシュがあればExecutorを通さず即返却（ワーカー待ちを回避）
    if not force:
        cached = _cache_read(f"deck_{theme}", _DECK_CACHE_TTL)
        if cached and "cards" in cached:
            cached.pop("_ts", None)
            # フルデッキがあればそちらを優先
            full_deck = cached.get("full_deck", [])
            if full_deck:
                cached["deck_text"] = build_recipe_text(full_deck)
            else:
                cached["deck_text"] = build_deck_text(cached.get("cards", []))
            return jsonify(cached)

    future = _meta_executor.submit(fetch_deck_cards, theme, force)
    try:
        data = future.result(timeout=25)
    except Exception as e:
        logger.error(f"デッキデータ取得エラー ({theme}): {e}")
        data = {"theme": theme, "tier": 0, "share": 0, "cards": [], "full_deck": []}
    # フルデッキがあればそちらを優先
    full_deck = data.get("full_deck", [])
    if full_deck:
        data["deck_text"] = build_recipe_text(full_deck)
    else:
        data["deck_text"] = build_deck_text(data.get("cards", []))
    return jsonify(data)


# ── パック（最新弾）API ──

@app.route("/api/packs")
def api_packs():
    """パック一覧を返す（スクレイピング失敗時はSupabaseから取得）"""
    packs = get_pack_list()
    if not packs and _supabase_client:
        try:
            resp = (_supabase_client.table("pack_list")
                    .select("name, wiki_page, tcg_name, release_date")
                    .order("release_date", desc=True)
                    .limit(10)
                    .execute())
            packs = [{"name": r.get("name", ""), "wiki_page": r.get("wiki_page", ""),
                      "tcg_name": r.get("tcg_name", ""), "date": r.get("release_date", "")}
                     for r in (resp.data or [])]
            logger.info(f"パック一覧をSupabaseから取得: {len(packs)}件")
        except Exception as e:
            logger.error(f"Supabaseパック一覧取得失敗: {e}")

    # 各弾に featured フラグを付与（運用ウィンドウ内の新弾だけ true）
    fp_name = ""
    fp_wiki = ""
    if _supabase_client:
        try:
            from featured_pack import get_featured_pack, is_within_window
            fp = get_featured_pack(_supabase_client)
            if fp and is_within_window(fp):
                fp_name = fp.get("pack_name", "")
                fp_wiki = fp.get("wiki_page", "")
        except Exception as e:
            logger.warning(f"新弾フラグ判定失敗（全 featured=false にフォールバック）: {e}")
    for p in packs:
        p["featured"] = bool(fp_name) and (
            p.get("name") == fp_name or
            (bool(fp_wiki) and p.get("wiki_page") == fp_wiki)
        )

    return jsonify(packs)

@app.route("/api/packs/cards")
def api_pack_cards():
    """パックのカードリストを返す"""
    pack_name = request.args.get("name", "").strip()
    wiki_page = request.args.get("wiki", "").strip()
    tcg_name = request.args.get("tcg", "").strip()
    if not pack_name:
        return jsonify({"error": "パック名を指定してください"}), 400
    if any(len(v) > MAX_PACK_PARAM_LEN for v in (pack_name, wiki_page, tcg_name)):
        return jsonify({"error": "パック指定が長すぎます"}), 400
    rate_error = _consume_rate_limit()
    if rate_error:
        return rate_error
    future = _meta_executor.submit(fetch_pack_cards, pack_name, wiki_page, tcg_name)
    try:
        data = future.result(timeout=30)
    except Exception as e:
        logger.error(f"パックカード取得エラー ({pack_name}): {e}")
        data = {"pack": pack_name, "cards": [], "count": 0}
    return jsonify(data)


# ── バックグラウンド環境データ プリフェッチ ──

def _prefetch_meta():
    """サーバー起動時にTier表をバックグラウンドで取得"""
    import threading
    def _run():
        time.sleep(3)  # 起動完了を待つ
        try:
            tiers = fetch_tier_list()
            logger.info(f"環境データ プリフェッチ完了: {len(tiers)} テーマ")
        except Exception as e:
            logger.warning(f"環境データ プリフェッチ失敗: {e}")
    t = threading.Thread(target=_run, daemon=True)
    t.start()

# Flask debugモードのreloaderと複数workerでの二重起動を防ぐ
if (os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not _is_debug_mode()) and _claim_startup_job("meta_prefetch"):
    _prefetch_meta()


# ── フォント比較プレビュー（一時的） ──

@app.route("/font-preview")
def font_preview():
    return render_template("font_preview.html")


# ── 一人回しシミュレータ ──

@app.route("/solitaire")
def solitaire_page():
    """一人回しシミュレータ"""
    return render_template("solitaire.html")


@app.route("/api/solitaire/replay", methods=["POST"])
def solitaire_replay_save():
    """リプレイデータをSupabaseに保存してIDを返す"""
    import secrets, json as _json
    if not _supabase_client:
        return {"error": "データベース未接続"}, 503
    try:
        data = request.get_json(force=True)
        images = data.get("images", {})
        names  = data.get("names", {})
        logs = data.get("logs", [])
        title = str(data.get("title", ""))[:100]
        if not logs:
            return {"error": "logsが空です"}, 400
        replay_id = secrets.token_urlsafe(8)
        payload = {"id": replay_id, "title": title, "images": images, "names": names, "logs": logs}
        _supabase_client.table("solitaire_replays").insert(payload).execute()
        return {"id": replay_id}
    except Exception as e:
        logger.error(f"リプレイ保存エラー: {e}")
        return {"error": "保存に失敗しました"}, 500


@app.route("/api/solitaire/replay/<replay_id>", methods=["GET"])
def solitaire_replay_get(replay_id):
    """Supabaseからリプレイデータを取得"""
    if not _supabase_client:
        return {"error": "データベース未接続"}, 503
    try:
        resp = _supabase_client.table("solitaire_replays") \
            .select("images, names, logs, title") \
            .eq("id", replay_id) \
            .limit(1) \
            .execute()
        rows = resp.data if resp.data else []
        if not rows:
            return {"error": "見つかりません"}, 404
        row = rows[0]
        return {
            "images": row["images"],
            "names":  row.get("names") or {},
            "logs":   row["logs"],
            "title":  row.get("title", ""),
        }
    except Exception as e:
        logger.error(f"リプレイ取得エラー: {e}")
        return {"error": "取得に失敗しました"}, 500


# ── 新弾フィーチャーページ ──

@app.route("/featured")
def featured_page():
    """新弾フィーチャーページ: 最新弾の収録カード全件と価格推移を掲載する。
    X投稿の CTA リンク先 URL として固定で使用（次回新弾でも URL は変わらない）。
    """
    return render_template("index.html", card_name="", page_mode="featured")


# ── 新弾フィーチャーキャッシュ（メモリ） ──
# Wikiスクレイプを毎リクエストに乗せないよう、結果をメモリにキャッシュする。
# stale-while-revalidate: 古くなったらバックグラウンドで更新しつつ古い値を即返す。

_featured_cache: dict = {"data": None, "ts": 0.0}
_FEATURED_CACHE_SEC = 600  # 10分


def _load_featured_cache():
    """Wikiスクレイプ + 価格付与をバックグラウンドで実行しメモリに格納する。"""
    global _featured_cache
    if not _supabase_client:
        return
    try:
        from featured_pack import get_featured_pack, get_days_since_release, get_featured_cards
        pack = get_featured_pack(_supabase_client)
        if not pack:
            _featured_cache = {"data": {"pack": None, "days_since_release": -1, "cards": []}, "ts": time.time()}
            return
        days = get_days_since_release(pack)
        card_names = get_featured_cards(_supabase_client, pack)
        cards = []
        for name in card_names:
            best = _estimate_cache.get(name)
            if best:
                cards.append({
                    "name": name, "price": best.get("price"),
                    "shop": best.get("shop", ""), "rarity": best.get("rarity", ""),
                    "recorded_at": best.get("recorded_at", ""),
                })
            else:
                cards.append({"name": name, "price": None, "shop": "", "rarity": "", "recorded_at": ""})
        cards.sort(key=lambda x: -(x["price"] or 0))
        _featured_cache = {
            "data": {"pack": pack, "days_since_release": days, "cards": cards},
            "ts": time.time(),
        }
        logger.info(f"フィーチャーキャッシュロード完了: {pack['pack_name']} {len(cards)}枚")
    except Exception as e:
        logger.error(f"フィーチャーキャッシュロード失敗: {e}")


@app.route("/api/featured")
def api_featured():
    """新弾フィーチャーデータをメモリキャッシュから即返す。
    {
      "pack": {"pack_name", "start_date", "window_days"},
      "days_since_release": int,
      "loading": bool,  # True の場合はキャッシュ未確立。数秒後に再試行してください
      "cards": [{"name", "price", "shop", "rarity", "recorded_at"}]
    }
    """
    if not _supabase_client:
        return jsonify({"error": "DBに接続できません"}), 503

    cached = _featured_cache.get("data")
    ts = _featured_cache.get("ts", 0.0)
    age = time.time() - ts

    if cached is not None and age < _FEATURED_CACHE_SEC:
        # キャッシュが新鮮: そのまま返す
        return jsonify(cached)

    if cached is not None and age >= _FEATURED_CACHE_SEC:
        # stale-while-revalidate: 古い値を返しつつバックグラウンドでリフレッシュ
        Thread(target=_load_featured_cache, daemon=True).start()
        return jsonify(cached)

    # コールド（起動直後でキャッシュ未確立）: バックグラウンドロードを起動し loading=True を返す
    Thread(target=_load_featured_cache, daemon=True).start()
    return jsonify({"pack": None, "days_since_release": -1, "loading": True, "cards": []})


# ── Web Push 通知 ──

@app.route("/api/push/vapid-key")
def api_push_vapid_key():
    """フロントエンドに VAPID 公開鍵を返す。"""
    if not VAPID_PUBLIC_KEY:
        return jsonify({"error": "push not configured"}), 503
    return jsonify({"publicKey": VAPID_PUBLIC_KEY})


@app.route("/api/push/subscribe", methods=["POST"])
def api_push_subscribe():
    """プッシュ購読を登録または更新する。
    入力: {subscription: {endpoint, keys:{p256dh, auth}}, cards: ["カード名", ...]}
    """
    data = request.get_json(silent=True) or {}
    sub = data.get("subscription", {})
    endpoint = sub.get("endpoint", "").strip()
    keys = sub.get("keys", {})
    p256dh = keys.get("p256dh", "").strip()
    auth = keys.get("auth", "").strip()
    cards_raw = data.get("cards", [])

    if not endpoint or not p256dh or not auth:
        return jsonify({"error": "subscription が不完全です"}), 400

    _load_cardnames()
    card_names = [_correct_cardname(str(n).strip()) for n in cards_raw[:50]
                  if str(n).strip() and len(str(n).strip()) <= MAX_CARD_NAME_LEN]

    if not _supabase_client:
        return jsonify({"ok": False, "reason": "db_unavailable"}), 200

    try:
        _supabase_client.table("push_subscriptions").upsert({
            "endpoint": endpoint,
            "p256dh": p256dh,
            "auth": auth,
            "card_names": card_names,
        }).execute()
        return jsonify({"ok": True})
    except Exception as e:
        logger.warning(f"[push/subscribe] 登録失敗: {e}")
        return jsonify({"error": "登録に失敗しました"}), 500


@app.route("/api/push/unsubscribe", methods=["POST"])
def api_push_unsubscribe():
    """プッシュ購読を削除する。
    入力: {endpoint: "..."}
    """
    data = request.get_json(silent=True) or {}
    endpoint = data.get("endpoint", "").strip()
    if not endpoint:
        return jsonify({"error": "endpoint が必要です"}), 400

    if not _supabase_client:
        return jsonify({"ok": False, "reason": "db_unavailable"}), 200

    try:
        _supabase_client.table("push_subscriptions") \
            .delete().eq("endpoint", endpoint).execute()
        return jsonify({"ok": True})
    except Exception as e:
        logger.warning(f"[push/unsubscribe] 削除失敗: {e}")
        return jsonify({"error": "削除に失敗しました"}), 500


# ── フィードバック（不具合・要望） ──

@app.route("/api/feedback", methods=["POST"])
def api_feedback():
    """一人回しページ等からの不具合・要望を受け付ける。
    入力: {kind: "bug"|"request"|"other", body: str, contact: str, page: str}
    """
    # フィードバック専用レートリミット（30秒、IP単位）
    client_ip = request.remote_addr or "unknown"
    now = time.time()
    with _feedback_lock:
        last = _last_feedback.get(client_ip, 0)
        if now - last < FEEDBACK_RATE_LIMIT_SEC:
            return jsonify({"error": "しばらく待ってから再度送信してください"}), 429
        _last_feedback[client_ip] = now
        # 古い記録の掃除
        if len(_last_feedback) > RATE_LIMIT_MAX_ENTRIES:
            cutoff = now - 3600
            stale = [ip for ip, ts in _last_feedback.items() if ts < cutoff]
            for ip in stale:
                del _last_feedback[ip]

    data = request.get_json(silent=True) or {}

    # kind の正規化
    kind = str(data.get("kind", "other")).strip()
    if kind not in ("bug", "request", "other"):
        kind = "other"

    # body の検証
    body = str(data.get("body", "")).strip()
    if not body:
        return jsonify({"error": "本文が空です"}), 400
    if len(body) > MAX_FEEDBACK_BODY_CHARS:
        return jsonify({"error": "本文が長すぎます"}), 400

    # その他フィールド
    contact   = str(data.get("contact", "")).strip()[:MAX_FEEDBACK_CONTACT_CHARS]
    page      = str(data.get("page", "")).strip()[:50]
    ua        = (request.headers.get("User-Agent") or "")[:300]

    record = {
        "kind":       kind,
        "body":       body,
        "contact":    contact,
        "page":       page,
        "user_agent": ua,
    }

    # Supabase への永続化（失敗してもOKを返す）
    if _supabase_client:
        try:
            _supabase_client.table("feedback_reports").insert(record).execute()
        except Exception as e:
            logger.warning(f"[feedback] Supabase insert 失敗: {e}")

    # Discord 通知（外部HTTP遅延を避けるため非同期）
    if _DISCORD_WEBHOOK_URL:
        Thread(target=_notify_feedback_discord, args=(record,), daemon=True).start()

    return jsonify({"ok": True})


def _notify_feedback_discord(record: dict):
    """フィードバックを Discord Webhook へ通知する（バックグラウンドスレッドで実行）。"""
    kind_label = {"bug": "[不具合]", "request": "[要望]", "other": "[その他]"}.get(
        record.get("kind", "other"), "[その他]"
    )
    contact = record.get("contact") or "（なし）"
    body_text = record.get("body", "")[:1800]
    content = (
        f"{kind_label} [{record.get('page', '')}]\n"
        f"{body_text}\n"
        f"連絡先: {contact}"
    )
    try:
        _http.post(
            _DISCORD_WEBHOOK_URL,
            json={"content": content[:1990]},
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"[feedback] Discord 通知失敗: {e}")


# ── ヘルスチェック・ステータス ──

@app.route("/api/health")
def api_health():
    """全店舗のスクレイパーをテスト実行して結果を返す

    UptimeRobot 等の外部監視サービスからこのURLを定期的に叩く。
    200以外が返ったらアラート、という設定にする。

    環境変数 HEALTH_CHECK_KEY を設定した場合、
    ?key=XXXXX パラメータが必要。
    """
    auth_error = _require_health_key()
    if auth_error:
        return auth_error

    result = run_health_check()
    status_code = 200 if result["status"] == "ok" else 503
    return jsonify(result), status_code


_ATTR_JA = {"dark":"闇","light":"光","fire":"炎","water":"水","earth":"地","wind":"風","divine":"神"}
_PROP_JA = {
    # YGOResources keys
    "quickplay":"速攻","quick-play":"速攻","continuous":"永続",
    "field":"フィールド","equip":"装備","ritual":"儀式","counter":"カウンター",
    # ygoprodeck race keys（魔法/罠カードのプロパティ）
    "Normal":"","Quick-Play":"速攻","Continuous":"永続",
    "Field":"フィールド","Equip":"装備","Ritual":"儀式","Counter":"カウンター",
}
_TYPE_JA = {
    # 大分類（YGOResources cardType）
    "monster":"モンスター","spell":"魔法","trap":"罠",
    # ygoprodeck type フィールド → 日本語モンスター種別
    "Normal Monster":"通常モンスター",
    "Effect Monster":"効果モンスター",
    "Flip Effect Monster":"リバース・効果モンスター",
    "Flip Monster":"リバースモンスター",
    "Toon Monster":"トゥーンモンスター",
    "Spirit Monster":"スピリットモンスター",
    "Union Effect Monster":"ユニオン・効果モンスター",
    "Gemini Monster":"デュアルモンスター",
    "Tuner Monster":"チューナーモンスター",
    "Fusion Monster":"融合モンスター",
    "Fusion/Effect Monster":"融合・効果モンスター",
    "Ritual Monster":"儀式モンスター",
    "Ritual/Effect Monster":"儀式・効果モンスター",
    "Synchro Monster":"シンクロモンスター",
    "Synchro/Effect Monster":"シンクロ・効果モンスター",
    "Synchro/Tuner Monster":"シンクロ・チューナーモンスター",
    "XYZ Monster":"エクシーズモンスター",
    "XYZ/Effect Monster":"エクシーズ・効果モンスター",
    "Link Monster":"リンクモンスター",
    "Pendulum/Normal Monster":"ペンデュラム・通常モンスター",
    "Pendulum/Effect Monster":"ペンデュラム・効果モンスター",
    "Pendulum Effect Fusion Monster":"フュージョン・ペンデュラムモンスター",
    "Pendulum Synchro Effect Monster":"シンクロ・ペンデュラムモンスター",
    "Pendulum XYZ Effect Monster":"エクシーズ・ペンデュラムモンスター",
    "Token":"トークンモンスター",
    # 魔法/罠（ygoprodeck では "Spell Card" / "Trap Card"）
    "Spell Card":"魔法","Trap Card":"罠",
}
_RACE_JA = {
    "Warrior":"戦士族","Spellcaster":"魔法使い族","Dragon":"ドラゴン族",
    "Beast":"獣族","Beast-Warrior":"獣戦士族","Fiend":"悪魔族","Fairy":"天使族",
    "Insect":"昆虫族","Dinosaur":"恐竜族","Reptile":"爬虫類族","Sea Serpent":"海竜族",
    "Aqua":"水族","Pyro":"炎族","Rock":"岩石族","Machine":"機械族","Plant":"植物族",
    "Winged Beast":"鳥獣族","Zombie":"アンデット族","Thunder":"雷族","Fish":"魚族",
    "Psychic":"サイキック族","Divine-Beast":"幻神獣族","Creator-God":"創造神族",
    "Wyrm":"幻竜族","Cyberse":"サイバース族",
}
# OCGリミットレギュレーション（banlist_info.ban_ocg → 日本語ラベル）
_LIMIT_JA = {"Forbidden":"禁止","Banned":"禁止","Limited":"制限","Semi-Limited":"準制限"}

def _fetch_card_info_by_id(card_id: int, name: str) -> dict:
    """konami_id からカード情報dictを取得して返す（キャッシュ付き）。
    キャッシュヒット時は外部APIを叩かずに即返却する。
    外部HTTP呼び出しはロック外で行い、ロック保持中のネットワーク待ちを避ける。
    """
    # キャッシュ参照（ロック内）
    with _card_info_lock:
        if card_id in _card_info_cache:
            return _card_info_cache[card_id]

    # キャッシュミス → 外部APIで取得（ロック外）
    try:
        resp = _http.get(
            f"https://db.ygoresources.com/data/card/{card_id}",
            timeout=10,
            headers={"User-Agent": _BROWSER_UA},
        )
        if resp.status_code != 200:
            return {"found": False}
        data = resp.json()
        ja = data.get("cardData", {}).get("ja", {})
        card_type = ja.get("cardType", "")
        prints = ja.get("prints", [])
        latest_print = ""
        if prints:
            last = prints[-1]
            latest_print = last if isinstance(last, str) else last.get("code", "")
        race = None
        limit = None
        prop = _PROP_JA.get(ja.get("property") or "", ja.get("property") or "")
        card_subtype = ""
        try:
            pdeck = _http.get(
                f"https://db.ygoprodeck.com/api/v7/cardinfo.php?konami_id={card_id}&misc=yes",
                timeout=5,
                headers={"User-Agent": _BROWSER_UA},
            )
            if pdeck.status_code == 200:
                pitem = pdeck.json().get("data", [{}])[0]
                card_subtype = pitem.get("type", "")
                race_en = pitem.get("race", "")
                if card_type == "monster":
                    race = _RACE_JA.get(race_en, race_en) or None
                else:
                    # 魔法/罠: race フィールドがプロパティ種別
                    prop = _PROP_JA.get(race_en, "")
                ban_ocg = pitem.get("banlist_info", {}).get("ban_ocg")
                limit = _LIMIT_JA.get(ban_ocg) if ban_ocg else None
        except Exception:
            pass
        # サブタイプ優先、なければ大分類
        mapped_type = _TYPE_JA.get(card_subtype) or _TYPE_JA.get(card_type, card_type)
        # EXデッキ判定: YGOResources の properties 配列を優先して判定（ygoprodeck非依存）。
        # 11=Fusion, 18=Xyz, 19=Synchro, 23=Link（/data/meta/mprop で確定・実データ実証済み）。
        # トゥーン融合等 ygoprodeck が "Toon Monster" と返してしまうカードでも正しく判定できる。
        _EX_PROP_IDS = {11, 18, 19, 23}
        props = ja.get("properties") or []
        is_ex = any(p in _EX_PROP_IDS for p in props)
        # properties が欠ける異常系のみ ygoprodeck の card_subtype で補完
        if not is_ex and card_subtype:
            is_ex = any(kw in card_subtype for kw in ("Fusion", "Synchro", "Xyz", "XYZ", "Link"))
        result = {
            "found": True,
            "konami_id": card_id,
            "card_type": mapped_type,
            "broad_type": card_type,   # "monster" | "spell" | "trap"（表示判定用）
            "is_ex": is_ex,            # EXデッキカードか否か
            "atk": ja.get("atk"),
            "def": ja.get("def"),
            "level": ja.get("level"),
            "rank": ja.get("rank"),
            "link_val": ja.get("linkRating"),
            "race": race,
            "attribute": _ATTR_JA.get(ja.get("attribute") or "", ja.get("attribute") or ""),
            "property": prop,
            "effect_text": ja.get("effectText", ""),
            "latest_print": latest_print,
            "limit": limit,
        }
    except Exception as e:
        logger.warning(f"[card-info] {name}: {e}")
        return {"found": False}

    # キャッシュ格納（ロック内）。上限超過時は一括クリアして空きを作る
    with _card_info_lock:
        if len(_card_info_cache) >= MAX_CARD_INFO_CACHE:
            _card_info_cache.clear()
            logger.info("[card-info] キャッシュ上限到達のためクリアしました")
        _card_info_cache[card_id] = result

    return result


@app.route("/api/card-info")
def api_card_info():
    """カード名からYGOResourcesのカード情報（ATK/DEF/効果テキスト等）を返す"""
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    idx = _get_ygores_name_index()
    ids = idx.get(name)
    if not ids and _ygores_fuzzy_index:
        # 完全一致しない場合は記号・全半角ハイフン等のゆれを無視して再照合
        # （画像取得 _ygores_image_url と同じフォールバック。card-info でもゆれを吸収する）
        ids = _ygores_fuzzy_index.get(_fuzzy_key(name))
    if not ids:
        return jsonify({"found": False}), 200
    card_id = ids[0]
    result = _fetch_card_info_by_id(card_id, name)
    resp = jsonify(result)
    if result.get("found"):
        # カード情報はほぼ不変のため長期キャッシュ可（card-image と同様）
        resp.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=604800"
    return resp


@app.route("/api/card-image")
def api_card_image():
    """カード名から画像URLを返す。YGOResources優先、未登録時はカーナベルにフォールバック"""
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    url = _ygores_image_url(name) or kanabell_card_image_url(name)
    resp = jsonify({"url": url})
    # カード名→画像URLの対応はほぼ不変のため長期キャッシュ可
    resp.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=604800"
    return resp


@app.route("/api/card-images", methods=["POST"])
def api_card_images():
    """複数カード名を一括受け取り、画像URLを一括返却するバッチAPI（デッキグリッド高速化用）。
    リクエスト: {"names": ["カード名A", "カード名B", ...]}
    レスポンス: {"images": {"カード名A": url|null, ...}}
    部分成功を許容（解決できない名前は null を返す）。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed as futures_completed

    data = request.get_json(force=True, silent=True) or {}
    names = data.get("names", [])
    if not isinstance(names, list):
        return jsonify({"error": "names must be a list"}), 400
    # 上限をMAX_DECK_CARDSで切る（DoS防止）
    names = [str(n).strip() for n in names if n][:MAX_DECK_CARDS]

    images = {}
    # まずYGOResourcesで一括解決（O(1)・ネットワーク不要）
    needs_fallback = []
    for name in names:
        url = _ygores_image_url(name)
        if url:
            images[name] = url
        else:
            needs_fallback.append(name)

    # カーナベルフォールバック（外部ES検索）は並列化して短縮
    if needs_fallback:
        def _fetch_kanabell(n):
            try:
                url = kanabell_card_image_url(n)
                return n, url if url else None
            except Exception:
                return n, None

        with ThreadPoolExecutor(max_workers=min(len(needs_fallback), 5)) as executor:
            futs = {executor.submit(_fetch_kanabell, n): n for n in needs_fallback}
            for future in futures_completed(futs, timeout=10):
                try:
                    n, url = future.result()
                    images[n] = url
                except Exception:
                    images[futs[future]] = None

    resp = jsonify({"images": images})
    # カード名→画像URLの対応はほぼ不変のため長期キャッシュ可
    resp.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=604800"
    return resp


@app.route("/api/card-infos", methods=["POST"])
def api_card_infos():
    """複数カード名を一括受け取り、カード情報を一括返却するバッチAPI（一人回しデッキ読込高速化用）。
    リクエスト: {"names": ["カード名A", "カード名B", ...]}
    レスポンス: {"infos": {"カード名A": {found, broad_type, is_ex, ...}, ...}}
    部分成功を許容（解決できない名前は {found: false} を返す）。
    _card_info_cache を経由するため、キャッシュ命中分は外部APIを叩かない。
    """
    data = request.get_json(force=True, silent=True) or {}
    names = data.get("names", [])
    if not isinstance(names, list):
        return jsonify({"error": "names must be a list"}), 400
    names = [str(n).strip() for n in names if n][:MAX_DECK_CARDS]

    idx = _get_ygores_name_index()
    infos = {}

    # name → card_id を解決し、キャッシュ命中分は即格納、未命中分を並列取得
    need_fetch = []  # (name, card_id) のリスト
    for name in names:
        ids = idx.get(name)
        if not ids and _ygores_fuzzy_index:
            ids = _ygores_fuzzy_index.get(_fuzzy_key(name))
        if not ids:
            infos[name] = {"found": False}
            continue
        card_id = ids[0]
        # キャッシュ参照（ロック内）
        with _card_info_lock:
            cached = _card_info_cache.get(card_id)
        if cached is not None:
            infos[name] = cached
        else:
            need_fetch.append((name, card_id))

    # キャッシュ未命中分を ThreadPoolExecutor で並列取得
    if need_fetch:
        def _fetch_one(item):
            n, cid = item
            result = _fetch_card_info_by_id(cid, n)  # キャッシュ格納も内部で行う
            return n, result

        with ThreadPoolExecutor(max_workers=min(len(need_fetch), 5)) as executor:
            futs = {executor.submit(_fetch_one, item): item for item in need_fetch}
            for future in as_completed(futs, timeout=25):
                try:
                    n, result = future.result()
                    infos[n] = result
                except Exception:
                    infos[futs[future][0]] = {"found": False}

    resp = jsonify({"infos": infos})
    resp.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=604800"
    return resp


# カード種別キャッシュ（カード名 → "monster"|"spell"|"trap"|"unknown"）
# 種別は変化しないので無期限保持
_card_type_cache: dict = {}

@app.route("/api/card-types", methods=["POST"])
def api_card_types():
    """複数カード名の種別（monster/spell/trap）を一括返却。サーバー側メモリキャッシュ付き。
    リクエスト: {"names": ["カード名A", ...]}
    レスポンス: {"types": {"カード名A": "monster"|"spell"|"trap"|"unknown", ...}}
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed as _ac

    body = request.get_json(force=True, silent=True) or {}
    names = body.get("names", [])
    if not isinstance(names, list):
        return jsonify({"error": "names must be a list"}), 400
    names = [str(n).strip() for n in names if n][:MAX_DECK_CARDS]

    result = {}
    need_fetch = []
    for name in names:
        if name in _card_type_cache:
            result[name] = _card_type_cache[name]
        else:
            need_fetch.append(name)

    if need_fetch:
        def _fetch_one(n):
            try:
                idx = _get_ygores_name_index()
                ids = idx.get(n)
                if not ids and _ygores_fuzzy_index:
                    ids = _ygores_fuzzy_index.get(_fuzzy_key(n))
                if not ids:
                    return n, "unknown"
                cid = ids[0]
                resp = _http.get(
                    f"https://db.ygoresources.com/data/card/{cid}",
                    timeout=5,
                    headers={"User-Agent": _BROWSER_UA},
                )
                if resp.status_code != 200:
                    return n, "unknown"
                ct = resp.json().get("cardData", {}).get("ja", {}).get("cardType", "") or "unknown"
                return n, ct  # "monster" | "spell" | "trap"
            except Exception:
                return n, "unknown"

        with ThreadPoolExecutor(max_workers=min(len(need_fetch), 8)) as executor:
            futs = {executor.submit(_fetch_one, n): n for n in need_fetch}
            for future in _ac(futs, timeout=15):
                try:
                    n, t = future.result()
                    result[n] = t
                    _card_type_cache[n] = t
                except Exception:
                    result[futs[future]] = "unknown"

    return jsonify({"types": result})


@app.route("/api/card-image-proxy")
def api_card_image_proxy():
    """カーナベル画像をRenderサーバー経由でバイナリ転送する（GitHub Actions向け）。
    GitHub ActionsのIPではカーナベル画像が403で弾かれるため、
    RenderサーバーのIPを中継役として使う。
    """
    name = request.args.get("name", "").strip()
    if not name:
        return "name required", 400
    image_url = kanabell_card_image_url(name)
    if not image_url:
        return "not found", 404
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.ka-nabell.com/",
        }
        img_resp = _http.get(image_url, headers=headers, timeout=10)
        logger.info(f"[card-image-proxy] {name}: HTTP {img_resp.status_code} url={image_url}")
        if img_resp.status_code != 200:
            return f"upstream {img_resp.status_code}", 502
        ct = img_resp.headers.get("Content-Type", "image/jpeg")
        proxy_resp = Response(img_resp.content, content_type=ct)
        # 画像実体は不変のため長期キャッシュ可
        proxy_resp.headers["Cache-Control"] = "public, max-age=604800"
        return proxy_resp
    except Exception as e:
        logger.error(f"[card-image-proxy] {name}: {e}")
        return f"error: {e}", 500


@app.route("/api/parse-deck-pdf-debug", methods=["POST"])
def api_parse_deck_pdf_debug():
    """pdfplumberが抽出したraw wordデータを返す（座標確認用）"""
    if "file" not in request.files:
        return jsonify({"error": "ファイルが必要です"}), 400
    f = request.files["file"]
    try:
        import pdfplumber
        with pdfplumber.open(f.stream) as pdf:
            page = pdf.pages[0]
            words = page.extract_words(x_tolerance=2, y_tolerance=1, keep_blank_chars=False)
        # 座標確認用: 全wordをy,xでソートして返す
        out = sorted(
            [{"t": w["text"], "x": round(w["x0"], 1), "y": round(w["top"], 1)} for w in words],
            key=lambda w: (w["y"], w["x"]),
        )
        return jsonify({"words": out, "count": len(out)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/parse-deck-pdf", methods=["POST"])
def api_parse_deck_pdf():
    """ニューロン デッキシートPDFをパースしてカードリストを返す"""
    if "file" not in request.files:
        return jsonify({"error": "ファイルが必要です"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "PDFファイルを選択してください"}), 400

    try:
        import pdfplumber, re as _re

        with pdfplumber.open(f.stream) as pdf:
            if not pdf.pages:
                return jsonify({"error": "PDFにページがありません"}), 400

            page = pdf.pages[0]
            # y_tolerance=1: 行番号(y=142)とカード名(y=140)のy差=2ptを別行に分離
            words = page.extract_words(x_tolerance=2, y_tolerance=1, keep_blank_chars=False)

        result = _parse_neuron_pdf_words(words)
        return jsonify(result)

    except ImportError:
        return jsonify({"error": "PDF解析ライブラリが利用できません（pdfplumber未インストール）"}), 500
    except Exception as e:
        logger.warning(f"[parse-deck-pdf] {e}")
        return jsonify({"error": f"PDF解析エラー: {e}"}), 500


def _parse_neuron_pdf_words(words):
    """pdfplumber extract_words() の結果からデッキシートをパース"""
    import re

    if not words:
        return {
            "ok": False, "main": [], "ex": [],
            "warnings": ["テキストを抽出できませんでした（0単語）"],
        }

    # items: {str, x, y}  (y = page top からの距離; 下方向が大きい)
    items = [{"str": w["text"].strip(), "x": w["x0"], "y": w["top"]}
             for w in words if w["text"].strip()]
    items.sort(key=lambda it: (it["y"], it["x"]))

    # 行番号がカード名に混入した場合に除去するフォールバック
    # 例: "1BF－毒風のシムーン" → "BF－毒風のシムーン"
    def strip_row_num(s):
        cleaned = re.sub(r"^\d+", "", s).strip()
        return cleaned if cleaned else s

    # ヘッダー検出
    def hm(s): return "モンスター" in s
    def hs(s): return "魔法" in s and "カード" in s
    def ht(s): return "罠" in s and "カード" in s
    def he(s): return "エクストラ" in s
    def hsd(s): return "サイド" in s and "デッキ" in s

    mon_h  = next((it for it in items if hm(it["str"]) and not hs(it["str"]) and not ht(it["str"])), None)
    spl_h  = next((it for it in items if hs(it["str"]) and not hm(it["str"])), None)
    trp_h  = next((it for it in items if ht(it["str"]) and not hm(it["str"]) and not hs(it["str"])), None)
    ex_h   = next((it for it in items if he(it["str"])), None)
    side_h = next((it for it in items if hsd(it["str"]) and not he(it["str"])), None)

    debug_info = [
        f"総単語数: {len(items)}",
        f"先頭30: {[it['str'] for it in items[:30]]}",
        f"ヘッダ: mon={mon_h and mon_h['str']}, ex={ex_h and ex_h['str']}",
    ]

    if not mon_h or not ex_h:
        return {
            "ok": False, "main": [], "ex": [],
            "warnings": [
                "ニューロン形式として認識できませんでした。手動で修正してください。",
                *debug_info,
            ],
        }

    # ヘッダ行から枚数列xを取得（ソート済み: [モンスター枚数x, 魔法枚数x, 罠枚数x]）
    main_hdr_y = mon_h["y"]
    hdr_row = sorted([it for it in items if abs(it["y"] - main_hdr_y) < 5], key=lambda it: it["x"])
    main_qty_xs = sorted([it["x"] for it in hdr_row if it["str"].strip() == "枚数"])

    # 除外語
    SKIP = {"モンスターカード","魔法カード","罠カード","エクストラデッキ","サイドデッキ",
            "カード名","枚数","かな","氏名","カードゲームID","参加番号","日付","イベント名",
            "モンスター","魔法","罠","エクストラ","サイド","デッキ","カード"}
    skip_re = [re.compile(p) for p in [r"合計\s*>+", r"^>+", r"^メインデッキ"]]

    def skip_item(s):
        if s in SKIP: return True
        if any(p.search(s) for p in skip_re): return True
        if hm(s) or hs(s) or ht(s) or he(s) or hsd(s): return True
        if re.match(r"^[\d\s]+$", s) and len(s.replace(" ","")) > 4: return True
        return False

    is_digit = lambda s: bool(re.match(r"^\d+$", s))

    main_y   = main_hdr_y
    ex_hdr_y = ex_h["y"]

    # EXヘッダ行から枚数列xを取得（[EX枚数x, サイド枚数x]）
    ex_hdr_row = sorted([it for it in items if abs(it["y"] - ex_hdr_y) < 5], key=lambda it: it["x"])
    ex_qty_xs  = sorted([it["x"] for it in ex_hdr_row if it["str"].strip() == "枚数"])
    ex_qty_x   = ex_qty_xs[0] if ex_qty_xs else None  # EX列の枚数x

    warnings = []
    QTY_TOL = 25   # 枚数列x からの横方向許容幅 (pt)
    ROW_TOL = 7    # 同一行とみなすy方向許容幅 (pt)

    def pair_by_row(name_items, digit_items, qty_xs):
        """カード名トークンを「同一y行 × 同一枚数列」でグループ化し、
        同じセル内の複数トークンをスペース結合した上で枚数と対応付ける。
        スペースを含むカード名（例: 結晶魔術 光の涙）や特殊文字の分割に対応。"""
        if not qty_xs:
            return []
        sorted_qty_xs = sorted(qty_xs)

        # (y_bucket, my_qty_x) をキーにトークンをグループ化
        groups = {}  # key → [token, ...]
        y_rep  = {}  # key → 代表y値
        for nm in name_items:
            my_qty_x = next((qx for qx in sorted_qty_xs if qx > nm["x"]), None)
            if my_qty_x is None:
                continue
            # ROW_TOLで割って丸めることで同一行のトークンが同じバケットに入る
            key = (round(nm["y"] / ROW_TOL), my_qty_x)
            groups.setdefault(key, []).append(nm)
            y_rep.setdefault(key, nm["y"])

        result = []
        for key in sorted(groups.keys()):
            _, my_qty_x = key
            tokens = sorted(groups[key], key=lambda t: t["x"])
            combined_str = " ".join(t["str"] for t in tokens)
            nm_y = y_rep[key]
            tok = next(
                (d for d in digit_items
                 if abs(d["x"] - my_qty_x) <= QTY_TOL and abs(d["y"] - nm_y) <= ROW_TOL),
                None,
            )
            qty = int(tok["str"]) if tok else None
            result.append(({"str": combined_str, "x": tokens[0]["x"], "y": nm_y}, qty))
        return result

    # ── メインデッキ ──────────────────────────────────
    main_all = [it for it in items if main_y < it["y"] < ex_hdr_y and not skip_item(it["str"])]
    main_digits = [it for it in main_all if is_digit(it["str"])]
    main_names  = []
    for it in main_all:
        if not is_digit(it["str"]):
            name_str = strip_row_num(it["str"])
            if name_str and not skip_item(name_str):
                main_names.append({"str": name_str, "x": it["x"], "y": it["y"]})

    # 枚数列未検出時フォールバック: 数字群の中で最も右のxを枚数列とする
    eff_main_qty_xs = main_qty_xs if main_qty_xs else (
        [max(it["x"] for it in main_digits)] if main_digits else []
    )

    result_main = []
    for nm, qty in pair_by_row(main_names, main_digits, eff_main_qty_xs):
        safe = max(1, min(3, qty)) if qty is not None and 1 <= qty <= 3 else 1
        if qty is None:
            warnings.append(f"「{nm['str']}」の枚数が読み取れません（1枚として扱います）")
        result_main.append({"qty": safe, "name": nm["str"]})

    # ── エクストラデッキ ──────────────────────────────
    # EXとサイドは同一y範囲に横並びのため、EX枚数列x より左のカード名のみEXとして取り込む
    # （EX名x≈70 < ex_qty_x≈217 < サイド名x≈240 なので左右で自然に分離できる）
    ex_all = [it for it in items if it["y"] > ex_hdr_y and not skip_item(it["str"])]
    ex_digits = [it for it in ex_all if is_digit(it["str"])]

    # EX枚数列x 未検出時フォールバック
    if ex_qty_x is None:
        dxs = [it["x"] for it in ex_digits]
        ex_qty_x = max(dxs) if dxs else None

    # EX列のカード名のみ: EX枚数列xより左にある名前
    ex_names = []
    for it in ex_all:
        if not is_digit(it["str"]):
            name_str = strip_row_num(it["str"])
            if name_str and not skip_item(name_str):
                if ex_qty_x is None or it["x"] < ex_qty_x:
                    ex_names.append({"str": name_str, "x": it["x"], "y": it["y"]})

    result_ex = []
    for nm, qty in pair_by_row(ex_names, ex_digits, [ex_qty_x] if ex_qty_x else []):
        safe = max(1, min(3, qty)) if qty is not None and 1 <= qty <= 3 else 1
        result_ex.append({"qty": safe, "name": nm["str"]})

    ok = len(result_main) > 0 or len(result_ex) > 0
    if not ok:
        warnings.extend(debug_info)

    return {"ok": ok, "main": result_main, "ex": result_ex, "warnings": warnings}


@app.route("/api/status")
def api_status():
    """エラー追跡状態を返す（テスト実行なし、軽量）"""
    auth_error = _require_health_key()
    if auth_error:
        return auth_error

    return jsonify({
        "status": "ok",
        "tracker": tracker.get_status(),
    })


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _build_buyback_done(results: list[dict]) -> dict:
    """買取価格比較用の完了メッセージ（高い順）"""
    by_rarity = {}
    for r in results:
        key = r.get("rarity") or "(不明)"
        if key not in by_rarity or r["price"] > by_rarity[key]["price"]:
            by_rarity[key] = r
    by_shop = {}
    for r in results:
        key = r["shop"]
        if key not in by_shop or r["price"] > by_shop[key]["price"]:
            by_shop[key] = r

    return {
        "type": "done",
        "results": results,
        "by_rarity": by_rarity,
        "by_shop": by_shop,
        "total": len(results),
    }


def _build_done(results: list[dict], corrected_name: str = "") -> dict:
    in_stock = [r for r in results if not r.get("sold_out")]
    by_rarity = {}
    for r in in_stock:
        key = r.get("rarity") or "(不明)"
        if key not in by_rarity or r["price"] < by_rarity[key]["price"]:
            by_rarity[key] = r
    by_shop = {}
    for r in in_stock:
        key = r["shop"]
        if key not in by_shop or r["price"] < by_shop[key]["price"]:
            by_shop[key] = r

    d = {
        "type": "done",
        "results": results,
        "by_rarity": by_rarity,
        "by_shop": by_shop,
        "total": len(results),
        "in_stock_count": len(in_stock),
        "sold_out_count": len(results) - len(in_stock),
    }
    if corrected_name:
        d["corrected_name"] = corrected_name
    return d


# ── 起動時プリロード ──
def _preload_movers():
    """サーバー起動直後にバックグラウンドでmoversキャッシュを作成"""
    time.sleep(5)  # Supabase接続が確立するのを待つ
    try:
        _get_price_movers("up")
        logger.info("値動きランキングをプリロードしました")
    except Exception as e:
        logger.warning(f"プリロード失敗: {e}")

if _claim_startup_job("movers_preload"):
    threading.Thread(target=_preload_movers, daemon=True).start()

def _preload_buyback_movers():
    """サーバー起動直後にバックグラウンドで買取moversキャッシュを作成"""
    time.sleep(8)  # 販売moversプリロードと少しずらす
    try:
        _get_buyback_movers("up")
        logger.info("買取値動きランキングをプリロードしました")
    except Exception as e:
        logger.warning(f"買取プリロード失敗: {e}")

if _claim_startup_job("buyback_movers_preload"):
    threading.Thread(target=_preload_buyback_movers, daemon=True).start()

if _claim_startup_job("featured_prefetch"):
    threading.Thread(target=_load_featured_cache, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(debug=debug, host="0.0.0.0", port=port)
