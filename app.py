"""
TCG 価格比較 Web サーバー
"""

import json
import time
import os
from flask import Flask, render_template, request, jsonify, Response

from scraper import (
    SHOPS, WAIT_SEC, cache_get, cache_set, is_target_card, normalize_rarity,
)

app = Flask(__name__)

# 同時検索の簡易レートリミット (メモリ内)
_last_search: dict[str, float] = {}
RATE_LIMIT_SEC = 3


@app.route("/")
def index():
    return render_template("index.html")


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
            except Exception as e:
                results = []
                yield _sse({"type": "shop_error", "shop": shop_name, "error": str(e)})
            yield _sse({"type": "shop_done", "shop": shop_name, "count": len(results)})
            all_results.extend(results)
            time.sleep(WAIT_SEC)

        cache_set(card_name, all_results)
        yield _sse(_build_done(all_results))

    return Response(generate(), mimetype="text/event-stream")


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
