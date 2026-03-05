"""
TCG 価格比較 Web サーバー
"""

import json
import time
import os
import logging
import requests as http_requests
from flask import Flask, render_template, request, jsonify, Response

from scraper import (
    SHOPS, WAIT_SEC, cache_get, cache_set, is_target_card, normalize_rarity,
)
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

# ヘルスチェック用シークレットキー（環境変数で設定、未設定なら誰でもアクセス可能）
HEALTH_CHECK_KEY = os.environ.get("HEALTH_CHECK_KEY", "")


@app.route("/")
def index():
    return render_template("index.html")


# ── カード名サジェスト ──

_suggest_cache: dict[str, tuple[float, list]] = {}
SUGGEST_CACHE_TTL = 3600  # 1時間

@app.route("/api/suggest")
def api_suggest():
    """カード名の入力補完候補を返す（YGOProDeck API 経由）"""
    q = request.args.get("q", "").strip()
    if not q or len(q) < 2:
        return jsonify([])

    # キャッシュ確認
    now = time.time()
    if q in _suggest_cache:
        ts, data = _suggest_cache[q]
        if now - ts < SUGGEST_CACHE_TTL:
            return jsonify(data)

    try:
        resp = http_requests.get(
            "https://db.ygoprodeck.com/api/v7/cardinfo.php",
            params={"fname": q, "language": "ja", "num": 12, "offset": 0},
            headers={"User-Agent": "TCGPriceCompare/1.0"},
            timeout=5,
        )
        if resp.status_code != 200:
            return jsonify([])

        cards = resp.json().get("data", [])
        # 日本語名を抽出してユニーク化
        names = []
        seen = set()
        for card in cards:
            name = card.get("name", "")
            if name and name not in seen:
                seen.add(name)
                names.append(name)

        _suggest_cache[q] = (now, names[:10])
        return jsonify(names[:10])

    except Exception:
        return jsonify([])


@app.route("/api/search")
def api_search():
    card_name = request.args.get("q", "").strip()
    if not card_name:
        return jsonify({"error": "カード名を入力してください"}), 400
    if len(card_name) > 50:
        return jsonify({"error": "カード名が長すぎます"}), 400

    # 検索対象店舗（指定がなければ全店舗）
    ALL_SHOP_NAMES = [name for name, _ in SHOPS]
    selected = request.args.getlist("shops") or ALL_SHOP_NAMES
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

    # キャッシュヒットなら即座に返す（選択店舗でフィルタ）
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
        all_results = []
        for shop_name, scraper in active_shops:
            yield _sse({"type": "progress", "shop": shop_name})
            try:
                results = scraper(card_name)
                if results:
                    tracker.record_success(shop_name, len(results))
                else:
                    tracker.record_failure(shop_name, f"0件取得 (検索: {card_name})")
            except Exception as e:
                results = []
                tracker.record_failure(shop_name, str(e))
                yield _sse({"type": "shop_error", "shop": shop_name, "error": str(e)})
                logger.error(f"{shop_name} エラー: {e}")
            yield _sse({"type": "shop_done", "shop": shop_name, "count": len(results)})
            all_results.extend(results)
            time.sleep(WAIT_SEC)

        cache_set(card_name, all_results)
        yield _sse(_build_done(all_results))

    return Response(generate(), mimetype="text/event-stream")


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
