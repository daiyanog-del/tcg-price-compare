"""
TCG 価格比較 Web サーバー
"""

import json
import time
import os
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, jsonify, Response
from flask_compress import Compress

from scraper import (
    SHOPS, DEFAULT_SHOPS, WAIT_SEC, cache_get, cache_set,
    is_target_card, normalize_rarity,
    BUYBACK_SHOPS, DEFAULT_BUYBACK_SHOPS,
    buyback_cache_get, buyback_cache_set,
)
from meta_scraper import fetch_tier_list, fetch_deck_cards, build_deck_text
from pack_scraper import get_pack_list, fetch_pack_cards
from monitor import tracker, run_health_check

import re as _re

# 検索クエリの表記ゆれ正規化
_DASH_CHARS = _re.compile(r'[\u2012\u2013\u2014\u2015\u2212\uFF0D\uFF70]')  # 各種ダッシュ・ハイフン
def _normalize_query(q: str) -> str:
    """ユーザー入力のカード名を正規化（ダッシュ統一・全角スペース変換）"""
    q = _DASH_CHARS.sub('-', q)       # 各種ダッシュ → 通常ハイフン
    q = q.replace('\u3000', ' ')      # 全角スペース → 半角
    return q.strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
Compress(app)

@app.after_request
def add_cache_headers(response):
    """レスポンスタイプに応じたキャッシュ制御"""
    ct = response.content_type
    if 'text/html' in ct:
        # HTMLは短いキャッシュ（価格データは変動するため）
        response.headers['Cache-Control'] = 'public, max-age=60, stale-while-revalidate=300'
    elif 'application/xml' in ct or 'text/plain' in ct:
        # sitemap.xml, robots.txt
        response.headers['Cache-Control'] = 'public, max-age=3600'
    return response

# 同時検索の簡易レートリミット (メモリ内)
_last_search: dict[str, float] = {}
_rate_limit_lock = __import__('threading').Lock()
RATE_LIMIT_SEC = 3
RATE_LIMIT_MAX_ENTRIES = 10000  # これ以上溜まったら古い記録を一括削除

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
            from datetime import datetime, timedelta, timezone
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=RANKING_RECENT_HOURS)).isoformat()
            resp = _supabase_client.rpc("get_search_ranking", {
                "since_ts": cutoff,
                "max_results": 30
            }).execute()
            if resp.data:
                result = [{"name": r["card_name"], "count": r["search_count"]} for r in resp.data]
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
_cardnames_loaded = False

def _load_cardnames():
    """data/cardnames_ja.json をメモリに読み込む"""
    global _cardnames, _cardnames_loaded
    if _cardnames_loaded:
        return
    cardnames_path = os.path.join(os.path.dirname(__file__), "data", "cardnames_ja.json")
    try:
        with open(cardnames_path, "r", encoding="utf-8") as f:
            _cardnames = json.loads(f.read())
        logger.info(f"カード名データベース読込: {len(_cardnames)} 件")
    except FileNotFoundError:
        logger.warning("data/cardnames_ja.json が見つかりません。update_cardnames.py を実行してください。")
        _cardnames = []
    _cardnames_loaded = True

@app.route("/api/suggest")
def api_suggest():
    """カード名の入力補完候補を返す（ローカルファイル参照、外部API不使用）"""
    _load_cardnames()
    q = _normalize_query(request.args.get("q", ""))
    if not q or len(q) < 1:
        return jsonify([])

    q_lower = q.lower()
    # 前方一致を優先、次に部分一致
    prefix_matches = []
    contains_matches = []
    for name in _cardnames:
        name_lower = name.lower()
        if name_lower.startswith(q_lower):
            prefix_matches.append(name)
        elif q_lower in name_lower:
            contains_matches.append(name)
        if len(prefix_matches) + len(contains_matches) >= 15:
            break

    results = (prefix_matches + contains_matches)[:10]
    return jsonify(results)


@app.route("/api/search")
def api_search():
    card_name = _normalize_query(request.args.get("q", ""))
    if not card_name:
        return jsonify({"error": "カード名を入力してください"}), 400
    if len(card_name) > 50:
        return jsonify({"error": "カード名が長すぎます"}), 400

    ALL_SHOP_NAMES = [name for name, _ in SHOPS]
    selected = request.args.getlist("shops") or DEFAULT_SHOPS
    selected = [s for s in selected if s in ALL_SHOP_NAMES]
    if not selected:
        return jsonify({"error": "有効な店舗を選択してください"}), 400

    active_shops = [(name, fn) for name, fn in SHOPS if name in selected]

    # レートリミット
    client_ip = request.remote_addr or "unknown"
    now = time.time()
    with _rate_limit_lock:
        if client_ip in _last_search and now - _last_search[client_ip] < RATE_LIMIT_SEC:
            return jsonify({"error": "しばらく待ってから再度検索してください"}), 429
        _last_search[client_ip] = now
        # 古い記録を定期的に掃除（メモリリーク防止）
        if len(_last_search) > RATE_LIMIT_MAX_ENTRIES:
            cutoff = now - RATE_LIMIT_SEC
            stale = [ip for ip, ts in _last_search.items() if ts < cutoff]
            for ip in stale:
                del _last_search[ip]

    # ランキング記録
    _record_search(card_name)

    # キャッシュヒット
    cached = cache_get(card_name)
    if cached is not None:
        filtered = [r for r in cached if r["shop"] in selected]
        def cached_stream():
            for shop_name, _ in active_shops:
                count = sum(1 for r in filtered if r["shop"] == shop_name)
                yield _sse({"type": "shop_done", "shop": shop_name, "count": count, "cached": True})
            yield _sse(_build_done(filtered))
        return Response(cached_stream(), mimetype="text/event-stream")

    def generate():
        from concurrent.futures import ThreadPoolExecutor, as_completed

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
        except Exception as e:
            logger.error(f"検索処理で予期しないエラー: {e}")
        finally:
            # エラーが起きても必ず完了メッセージを送る
            yield _sse(_build_done(all_results))

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

    ALL_SHOP_NAMES = [name for name, _ in SHOPS]
    selected = [s for s in shops_raw if s in ALL_SHOP_NAMES]

    # デッキ検索用: 複数ページ巡回する店舗は1ページ目のみに制限
    _FAST_OVERRIDES = {
        "トレコロCB": partial(scrape_torecolo, max_pages=1),
        "カーナベル": partial(scrape_kanabell, max_pages=1),
    }
    active_shops = [
        (name, _FAST_OVERRIDES.get(name, fn))
        for name, fn in SHOPS if name in selected
    ]

    card_entries = []
    for line in names_raw.split("|"):
        line = line.strip()
        if not line:
            continue
        m = _re.match(r"^(\d+)\s+(.+)$", line)
        if m:
            card_entries.append({"qty": int(m.group(1)), "name": _normalize_query(m.group(2))})
        else:
            card_entries.append({"qty": 1, "name": _normalize_query(line)})

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

    ALL_BUY_NAMES = [name for name, _ in BUYBACK_SHOPS]
    selected = [s for s in shops_raw if s in ALL_BUY_NAMES]
    active_shops = [(name, fn) for name, fn in BUYBACK_SHOPS if name in selected]

    card_entries = []
    for line in names_raw.split("|"):
        line = line.strip()
        if not line:
            continue
        m = _re.match(r"^(\d+)\s+(.+)$", line)
        if m:
            card_entries.append({"qty": int(m.group(1)), "name": _normalize_query(m.group(2))})
        else:
            card_entries.append({"qty": 1, "name": _normalize_query(line)})

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

@app.route("/api/trending")
def api_trending():
    """直近24時間の検索ランキングを返す"""
    limit = min(int(request.args.get("limit", 10)), 30)
    return jsonify(_get_trending(limit))

# ── 値上がり/値下がりランキング（キャッシュ付き） ──
_movers_cache: dict[str, list] = {}
_movers_cache_time: float = 0
_MOVERS_CACHE_SEC = 600  # 10分キャッシュ

def _get_price_movers(direction: str, limit: int = 10) -> list[dict]:
    """Supabaseから値上がり/値下がりランキングを取得"""
    global _movers_cache, _movers_cache_time

    now = time.time()
    if _movers_cache and now - _movers_cache_time < _MOVERS_CACHE_SEC:
        return _movers_cache.get(direction, [])[:limit]

    if not _supabase_client:
        return []

    try:
        up_resp = _supabase_client.rpc("get_price_movers", {
            "direction": "up", "max_results": 20
        }).execute()
        down_resp = _supabase_client.rpc("get_price_movers", {
            "direction": "down", "max_results": 20
        }).execute()

        _movers_cache = {
            "up": [{"name": r["card_name"], "today": r["today_price"],
                     "yesterday": r["yesterday_price"], "diff": r["price_diff"],
                     "pct": float(r["change_pct"])} for r in (up_resp.data or [])],
            "down": [{"name": r["card_name"], "today": r["today_price"],
                       "yesterday": r["yesterday_price"], "diff": r["price_diff"],
                       "pct": float(r["change_pct"])} for r in (down_resp.data or [])],
        }
        _movers_cache_time = now
        return _movers_cache.get(direction, [])[:limit]
    except Exception as e:
        logger.warning(f"価格変動ランキング取得失敗: {e}")
        return []

@app.route("/api/movers")
def api_movers():
    """値上がり/値下がりランキングを返す"""
    direction = request.args.get("direction", "up")
    if direction not in ("up", "down"):
        return jsonify({"error": "direction は up または down"}), 400
    limit = min(int(request.args.get("limit", 10)), 20)
    return jsonify(_get_price_movers(direction, limit))

@app.route("/api/buyback")
def api_buyback():
    """買取価格比較 — 各店舗の買取価格をSSEで返す"""
    card_name = _normalize_query(request.args.get("q", ""))
    if not card_name:
        return jsonify({"error": "カード名を入力してください"}), 400
    if len(card_name) > 50:
        return jsonify({"error": "カード名が長すぎます"}), 400

    ALL_BUY_NAMES = [name for name, _ in BUYBACK_SHOPS]
    selected = request.args.getlist("shops") or DEFAULT_BUYBACK_SHOPS
    selected = [s for s in selected if s in ALL_BUY_NAMES]
    if not selected:
        return jsonify({"error": "有効な店舗を選択してください"}), 400

    active_shops = [(name, fn) for name, fn in BUYBACK_SHOPS if name in selected]

    # レートリミット
    client_ip = request.remote_addr or "unknown"
    now = time.time()
    with _rate_limit_lock:
        if client_ip in _last_search and now - _last_search[client_ip] < RATE_LIMIT_SEC:
            return jsonify({"error": "しばらく待ってから再度検索してください"}), 429
        _last_search[client_ip] = now

    # ランキング記録
    _record_search(card_name)

    # キャッシュヒット
    cached = buyback_cache_get(card_name)
    if cached is not None:
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

_meta_executor = ThreadPoolExecutor(max_workers=2)

@app.route("/api/meta")
def api_meta():
    """環境Tier表を返す（キャッシュヒットなら即座、ミスならスレッドプール経由）"""
    force = request.args.get("force") == "1"
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
    future = _meta_executor.submit(fetch_deck_cards, theme, force)
    try:
        data = future.result(timeout=25)
    except Exception as e:
        logger.error(f"デッキデータ取得エラー ({theme}): {e}")
        data = {"theme": theme, "tier": 0, "share": 0, "cards": []}
    data["deck_text"] = build_deck_text(data.get("cards", []))
    return jsonify(data)


# ── パック（最新弾）API ──

@app.route("/api/packs")
def api_packs():
    """パック一覧を返す"""
    packs = get_pack_list()
    return jsonify(packs)

@app.route("/api/packs/cards")
def api_pack_cards():
    """パックのカードリストを返す"""
    pack_name = request.args.get("name", "").strip()
    wiki_page = request.args.get("wiki", "").strip()
    tcg_name = request.args.get("tcg", "").strip()
    if not pack_name:
        return jsonify({"error": "パック名を指定してください"}), 400
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

# Flask debugモードのreloaderで二重起動を防ぐ
if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not os.environ.get("FLASK_DEBUG"):
    _prefetch_meta()


# ── ヘルスチェック・ステータス ──

@app.route("/api/health")
def api_health():
    """全店舗のスクレイパーをテスト実行して結果を返す

    UptimeRobot 等の外部監視サービスからこのURLを定期的に叩く。
    200以外が返ったらアラート、という設定にする。

    環境変数 HEALTH_CHECK_KEY を設定した場合、
    ?key=XXXXX パラメータが必要。
    """
    if HEALTH_CHECK_KEY:
        if request.args.get("key") != HEALTH_CHECK_KEY:
            return jsonify({"error": "unauthorized"}), 403

    result = run_health_check()
    status_code = 200 if result["status"] == "ok" else 503
    return jsonify(result), status_code


@app.route("/api/status")
def api_status():
    """エラー追跡状態を返す（テスト実行なし、軽量）"""
    if HEALTH_CHECK_KEY:
        if request.args.get("key") != HEALTH_CHECK_KEY:
            return jsonify({"error": "unauthorized"}), 403

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


def _build_done(results: list[dict]) -> dict:
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

    return {
        "type": "done",
        "results": results,
        "by_rarity": by_rarity,
        "by_shop": by_shop,
        "total": len(results),
        "in_stock_count": len(in_stock),
        "sold_out_count": len(results) - len(in_stock),
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(debug=debug, host="0.0.0.0", port=port)
