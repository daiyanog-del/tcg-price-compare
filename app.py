"""
TCG 価格比較 Web サーバー
"""

import json
import time
import os
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, jsonify, Response

from scraper import (
    SHOPS, DEFAULT_SHOPS, WAIT_SEC, cache_get, cache_set,
    is_target_card, normalize_rarity,
)
from meta_scraper import fetch_tier_list, fetch_deck_cards, build_deck_text
from monitor import tracker, run_health_check

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 同時検索の簡易レートリミット (メモリ内)
_last_search: dict[str, float] = {}
RATE_LIMIT_SEC = 3

# ヘルスチェック用シークレットキー
HEALTH_CHECK_KEY = os.environ.get("HEALTH_CHECK_KEY", "")


# ── 検索ランキング（メモリ内、再起動でリセット） ──

from collections import defaultdict
from threading import Lock

_ranking_lock = Lock()
_search_counts: dict[str, int] = defaultdict(int)  # カード名 → 検索回数
_search_recent: list[tuple[float, str]] = []         # (timestamp, card_name)
RANKING_RECENT_HOURS = 24

def _record_search(card_name: str):
    """検索をランキングに記録"""
    with _ranking_lock:
        _search_counts[card_name] += 1
        now = time.time()
        _search_recent.append((now, card_name))
        # 古いエントリを削除
        cutoff = now - RANKING_RECENT_HOURS * 3600
        while _search_recent and _search_recent[0][0] < cutoff:
            _search_recent.pop(0)

def _get_trending(limit: int = 10) -> list[dict]:
    """直近24時間の検索ランキングを返す"""
    with _ranking_lock:
        cutoff = time.time() - RANKING_RECENT_HOURS * 3600
        recent_counts: dict[str, int] = defaultdict(int)
        for ts, name in _search_recent:
            if ts >= cutoff:
                recent_counts[name] += 1
        ranked = sorted(recent_counts.items(), key=lambda x: -x[1])
        return [{"name": name, "count": count} for name, count in ranked[:limit]]


@app.route("/")
def index():
    return render_template("index.html")


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
    q = request.args.get("q", "").strip()
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
    card_name = request.args.get("q", "").strip()
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
    if client_ip in _last_search and now - _last_search[client_ip] < RATE_LIMIT_SEC:
        return jsonify({"error": "しばらく待ってから再度検索してください"}), 429
    _last_search[client_ip] = now

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
        import queue

        # 全店舗を「検索中」に
        for shop_name, _ in active_shops:
            yield _sse({"type": "progress", "shop": shop_name})

        all_results = []
        result_queue = queue.Queue()

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

        cache_set(card_name, all_results)
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
        import re as _re
        m = _re.match(r"^(\d+)\s+(.+)$", line)
        if m:
            card_entries.append({"qty": int(m.group(1)), "name": m.group(2).strip()})
        else:
            card_entries.append({"qty": 1, "name": line})

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

@app.route("/api/trending")
def api_trending():
    """直近24時間の検索ランキングを返す"""
    limit = min(int(request.args.get("limit", 10)), 30)
    return jsonify(_get_trending(limit))

@app.route("/api/config")
def api_config():
    """フロントエンド用の設定情報"""
    return jsonify({
        "shops": [{"name": name, "default": name in DEFAULT_SHOPS} for name, _ in SHOPS],
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
