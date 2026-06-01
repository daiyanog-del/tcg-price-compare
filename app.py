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
    """あいまい検索用に記号・スペースを除去した比較キーを返す"""
    return _FUZZY_STRIP.sub('', s).lower()

def _correct_cardname(name: str) -> str:
    """カード名をDBと照合し、補正が必要なら正式名称を返す。不要ならそのまま返す"""
    if not _cardnames or name in _cardnames:
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

def _get_ygores_name_index():
    global _ygores_name_index
    if _ygores_name_index is not None:
        return _ygores_name_index
    try:
        resp = _http.get(
            "https://db.ygoresources.com/data/idx/card/name/ja",
            timeout=30,
            headers={"User-Agent": _BROWSER_UA},
        )
        _ygores_name_index = resp.json() if resp.status_code == 200 else {}
        logger.info(f"[YGOResources] 名前インデックス取得: {len(_ygores_name_index)}件")
    except Exception as e:
        logger.warning(f"[YGOResources] 名前インデックス取得失敗: {e}")
        _ygores_name_index = {}
    return _ygores_name_index

def _ygores_image_url(card_name):
    """カード名からYGOResourcesのOCGアートワーク画像URLを返す。未登録の場合はNone"""
    idx = _get_ygores_name_index()
    ids = idx.get(card_name)
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


# ── 検索ランキング（Supabase永続化 + メモリフォールバック） ──

from collections import defaultdict
from threading import Lock, Thread

_ranking_lock = Lock()
_search_recent: list[tuple[float, str]] = []  # メモリフォールバック用
RANKING_RECENT_HOURS = 24

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
            all_rows = []
            page_size = 1000
            offset = 0
            while True:
                resp = (_supabase_client.table("price_history")
                        .select("card_name, shop, rarity, min_price, recorded_at")
                        .gte("recorded_at", cutoff)
                        .order("min_price", desc=False)
                        .range(offset, offset + page_size - 1)
                        .execute())
                rows = resp.data or []
                all_rows.extend(rows)
                if len(rows) < page_size:
                    break
                offset += page_size

            card_best = {}
            for row in all_rows:
                name = row.get("card_name", "")
                if not name or name in card_best:
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

@app.route("/api/deck-estimate")
def api_deck_estimate():
    """デッキ相場見積もり — メモリキャッシュから即時返却"""
    global _estimate_cache_time
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
    """Supabaseから値上がり/値下がりランキングを取得（直接クエリ版）"""
    global _movers_cache, _movers_cache_time

    now = time.time()
    if _movers_cache and now - _movers_cache_time < _MOVERS_CACHE_SEC:
        return _movers_cache.get(direction, [])[:limit]

    if not _supabase_client:
        return []

    try:
        # 直近3日分のデータをページングで全件取得
        cutoff = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        all_rows = []
        page_size = 1000
        offset = 0
        while True:
            resp = (_supabase_client.table("price_history")
                    .select("card_name, min_price, recorded_at")
                    .gte("recorded_at", cutoff)
                    .order("recorded_at", desc=False)
                    .range(offset, offset + page_size - 1)
                    .execute())
            batch = resp.data or []
            all_rows.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size

        logger.info(f"値動きランキング: {len(all_rows)}行取得")
        if not all_rows:
            return []

        # 日付ごと・カードごとの最安値を集計
        card_dates = _aggregate_daily_min(all_rows)

        # 全カード共通で比較に使う2日分を特定
        all_dates_set = set()
        for dates in card_dates.values():
            all_dates_set.update(dates.keys())
        all_dates_sorted = sorted(all_dates_set)
        if len(all_dates_sorted) < 2:
            return []
        date_new = all_dates_sorted[-1]
        date_old = all_dates_sorted[-2]

        # 各カードの比較
        movers = []
        for name, dates in card_dates.items():
            if date_new not in dates or date_old not in dates:
                continue
            today_price = dates[date_new]
            yesterday_price = dates[date_old]
            if yesterday_price == 0:
                continue
            diff = today_price - yesterday_price
            if diff == 0:
                continue
            pct = round((diff / yesterday_price) * 100, 1)
            movers.append({
                "name": name, "today": today_price,
                "yesterday": yesterday_price, "diff": diff, "pct": pct
            })

        up_all = sorted([m for m in movers if m["diff"] > 0], key=lambda x: -x["pct"])
        down_all = sorted([m for m in movers if m["diff"] < 0], key=lambda x: x["pct"])
        # 50円以上の変動を優先、なければ全件からフォールバック
        up = [m for m in up_all if abs(m["diff"]) >= 50][:20] or up_all[:20]
        down = [m for m in down_all if abs(m["diff"]) >= 50][:20] or down_all[:20]

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
    if _cardnames and corrected not in _cardnames:
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

    # データあり分: price_history から直近8日分を一括取得して日次最安値を計算
    card_dates: dict = {}
    if have_data and _supabase_client:
        try:
            cutoff = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d")
            all_rows: list = []
            page_size = 1000
            offset = 0
            while True:
                resp = (
                    _supabase_client.table("price_history")
                    .select("card_name, min_price, recorded_at")
                    .in_("card_name", have_data)
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
            card_dates = dict(_aggregate_daily_min(all_rows))
        except Exception as e:
            logger.warning(f"[wish-prices] 価格履歴取得失敗: {e}")

    results = []
    for name in card_names:
        if name in pending_set:
            results.append({"name": name, "latest": None, "base_7d": None,
                            "diff": None, "pct": None, "status": "pending"})
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
_PROP_JA = {"quickplay":"速攻","quick-play":"速攻","continuous":"永続","field":"フィールド","equip":"装備","ritual":"儀式","counter":"カウンター"}
_TYPE_JA = {"monster":"モンスター","spell":"魔法","trap":"罠"}

@app.route("/api/card-info")
def api_card_info():
    """カード名からYGOResourcesのカード情報（ATK/DEF/効果テキスト等）を返す"""
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    idx = _get_ygores_name_index()
    ids = idx.get(name)
    if not ids:
        return jsonify({"found": False}), 200
    card_id = ids[0]
    try:
        resp = _http.get(
            f"https://db.ygoresources.com/data/card/{card_id}",
            timeout=10,
            headers={"User-Agent": _BROWSER_UA},
        )
        if resp.status_code != 200:
            return jsonify({"found": False}), 200
        data = resp.json()
        ja = data.get("cardData", {}).get("ja", {})
        card_type = ja.get("cardType", "")
        prints = ja.get("prints", [])
        latest_print = ""
        if prints:
            last = prints[-1]
            latest_print = last if isinstance(last, str) else last.get("code", "")
        return jsonify({
            "found": True,
            "konami_id": card_id,
            "card_type": _TYPE_JA.get(card_type, card_type),
            "atk": ja.get("atk"),
            "def": ja.get("def"),
            "level": ja.get("level"),
            "rank": ja.get("rank"),
            "attribute": _ATTR_JA.get(ja.get("attribute") or "", ja.get("attribute") or ""),
            "property": _PROP_JA.get(ja.get("property") or "", ja.get("property") or ""),
            "effect_text": ja.get("effectText", ""),
            "latest_print": latest_print,
        })
    except Exception as e:
        logger.warning(f"[card-info] {name}: {e}")
        return jsonify({"found": False}), 200


@app.route("/api/card-image")
def api_card_image():
    """カード名から画像URLを返す。YGOResources優先、未登録時はカーナベルにフォールバック"""
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    url = _ygores_image_url(name) or kanabell_card_image_url(name)
    return jsonify({"url": url})


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
        return Response(img_resp.content, content_type=ct)
    except Exception as e:
        logger.error(f"[card-image-proxy] {name}: {e}")
        return f"error: {e}", 500


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

if _claim_startup_job("featured_prefetch"):
    threading.Thread(target=_load_featured_cache, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(debug=debug, host="0.0.0.0", port=port)
