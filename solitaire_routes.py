"""
solitaire_routes.py — 一人回し(solo play) シミュレータのルート群

このモジュールは一人回し機能を単一の feature flag 背後に閉じた
self-contained な Blueprint として提供する。IP 停止要請が来た際は
環境変数 ENABLE_VISUAL_SOLO_PLAY=0 を設定すれば、この機能だけが止まり、
相場・デッキ・コレクション等のコア機能は無傷で動き続ける（kill-switch）。

OFF 時の挙動:
  - GET /solitaire                      → 503 + solo_disabled.html（案内ページ）
  - GET /solitaire/replays              → 503 + solo_disabled.html（リプレイ一覧ページ）
  - POST /api/solitaire/replay          → 404
  - GET  /api/solitaire/replay/<id>     → 404
  - GET  /solitaire/og/<id>.png         → 404

依存注入（循環 import 回避）:
  app.py は _supabase_client を保持するため、起動時に init_solitaire() で
  Supabase クライアントへのアクセサ関数を注入する。card_display.py の
  register_released_resolver() と同じ関数注入方式。
"""

import os
import time
import secrets
import logging
import threading
from datetime import datetime, timezone

from flask import Blueprint, render_template, request, abort, Response, send_file

from client_ip import _client_ip
from replay_board import validate_board
import replay_og_image

logger = logging.getLogger(__name__)

# ── feature flag（import 時評価。停止時は env で 0 を明示セット） ──
ENABLE_VISUAL_SOLO_PLAY = os.environ.get("ENABLE_VISUAL_SOLO_PLAY", "1") == "1"

solitaire_bp = Blueprint("solitaire", __name__)

# ── リプレイ保存の専用レートリミット（同一IPからの連投による保存乱用対策） ──
# TODO: calibrate from data — 閾値は実データ未収集のため仮置き。
# 60秒10回（当初3回から緩和 2026-08-20）: 「共有リンクコピー」「X投稿」経路が
# 都度サーバー保存を行うため、通常の共有フローだけで3回を踏むことを実測確認したため。
REPLAY_RATE_LIMIT_WINDOW_SEC = 60
REPLAY_RATE_LIMIT_MAX_REQUESTS = 10
REPLAY_RATE_LIMIT_MAX_TRACKED_IPS = 10000  # 掃除用上限（他モジュールの慣例に合わせる）
_replay_rate_lock = threading.Lock()
_replay_rate_log: dict[str, list[float]] = {}


def _consume_replay_rate_limit():
    """リプレイ保存のIP単位レートリミット。通過時はNone、制限時は(dict, status)。"""
    ip = _client_ip()
    now = time.time()
    with _replay_rate_lock:
        timestamps = _replay_rate_log.setdefault(ip, [])
        timestamps[:] = [t for t in timestamps if now - t < REPLAY_RATE_LIMIT_WINDOW_SEC]
        if len(timestamps) >= REPLAY_RATE_LIMIT_MAX_REQUESTS:
            return {"error": "しばらく待ってから再度保存してください"}, 429
        timestamps.append(now)
        if len(_replay_rate_log) > REPLAY_RATE_LIMIT_MAX_TRACKED_IPS:
            # ウィンドウが完全に空になった（=直近アクセスがウィンドウ外）IPだけを掃除。
            # 旧実装の `if not v` は空リストになったキーは setdefault で即座に消えるため
            # 恒久的にヒットしないno-opだった。
            stale = [k for k, v in _replay_rate_log.items()
                     if not v or now - v[-1] >= REPLAY_RATE_LIMIT_WINDOW_SEC]
            for k in stale:
                del _replay_rate_log[k]
            # それでも上限超過なら、最終アクセスが古い順に強制削除する
            # （継続フラッド下ではstaleが1件も出ない可能性があるための保険）
            if len(_replay_rate_log) > REPLAY_RATE_LIMIT_MAX_TRACKED_IPS:
                overflow = len(_replay_rate_log) - REPLAY_RATE_LIMIT_MAX_TRACKED_IPS
                oldest_keys = sorted(
                    _replay_rate_log, key=lambda k: _replay_rate_log[k][-1]
                )[:overflow]
                for k in oldest_keys:
                    del _replay_rate_log[k]
    return None

# app.py から注入される Supabase クライアントアクセサ（引数なし → client or None）
_get_supabase = lambda: None


def init_solitaire(get_supabase):
    """app.py 起動時に一度だけ呼ぶ。Supabase クライアントのアクセサを登録する。

    例:
        from solitaire_routes import solitaire_bp, init_solitaire
        init_solitaire(lambda: _supabase_client)
        app.register_blueprint(solitaire_bp)
    """
    global _get_supabase
    _get_supabase = get_supabase


# ── 一人回しシミュレータ ──

REPLAY_DEFAULT_TITLE = "共有リプレイ"


def _lookup_replay_og_meta(replay_id):
    """共有リプレイの (title, board) を DB から引く。

    戻り値: ("found", title, board_or_None) / ("missing",) / ("unknown",)
    unknown は DB 未接続・取得失敗（ページ側は一過性の失敗を固定しないよう、生成ルートに任せる）。
    """
    sb = _get_supabase()
    if not sb:
        return ("unknown",)
    try:
        resp = (sb.table("solitaire_replays")
                .select("title, board")
                .eq("id", replay_id)
                .limit(1)
                .execute())
    except Exception as e:
        logger.warning(f"リプレイのOGP情報取得エラー: {e}")
        return ("unknown",)
    rows = resp.data if resp.data else []
    if not rows:
        return ("missing",)
    return ("found", (rows[0].get("title") or "").strip(), rows[0].get("board"))


# ── 一人回しシミュレータ ──

@solitaire_bp.route("/solitaire")
def solitaire_page():
    """一人回しシミュレータ。?replay=<id> 付きなら、リンク画像（og:image）をそのリプレイの盤面画像にする。

    og:title のためにリプレイ名を DB から1件引く（主キー検索・数十バイトのため許容。
    取得失敗時は既定の題名のまま続行する）。#replay=（ハッシュ方式）はサーバーから見えないので汎用画像のまま。
    """
    if not ENABLE_VISUAL_SOLO_PLAY:
        return render_template("solo_disabled.html"), 503
    og_image_path = None   # None = 汎用画像（static/og-solitaire.png）
    og_title = None
    og_url_path = None     # None = /solitaire（?replay= 付きのときはそのURL）
    replay_id = request.args.get("replay", "")
    if replay_og_image.is_valid_replay_id(replay_id):
        og_url_path = f"/solitaire?replay={replay_id}"
        meta = _lookup_replay_og_meta(replay_id)
        if meta[0] == "found":
            og_title = meta[1] or REPLAY_DEFAULT_TITLE
            if validate_board(meta[2]):
                og_image_path = f"/solitaire/og/{replay_id}.png"
        elif meta[0] == "unknown":
            og_image_path = f"/solitaire/og/{replay_id}.png"   # 生成ルート側で board 無し等なら汎用画像を返す
    return render_template("solitaire.html", og_image_path=og_image_path, og_title=og_title,
                           og_url_path=og_url_path)


@solitaire_bp.route("/solitaire/replays")
def solitaire_replays_page():
    """リプレイ一覧ページ。このブラウザ（localStorage）に残した端末保存・共有履歴を並べる。

    一覧の中身はすべてブラウザ側（static/solitaire/js/replays-page.js）で組み立てるため、
    サーバーは枠だけを返す（ユーザーごとの情報は持たない）。
    """
    if not ENABLE_VISUAL_SOLO_PLAY:
        return render_template("solo_disabled.html"), 503
    return render_template("solitaire_replays.html")


@solitaire_bp.route("/solitaire/og/<replay_id>.png")
def solitaire_og_image(replay_id):
    """共有リプレイのリンク画像（1200x630）。board があれば盤面画像、無ければ汎用画像。"""
    if not ENABLE_VISUAL_SOLO_PLAY:
        abort(404)
    if not replay_og_image.is_valid_replay_id(replay_id):
        abort(404)

    hit = replay_og_image.read_any_cached(replay_id)
    if hit is not None:
        png, degraded = hit
        # 画像取得に失敗して不完全だった描画（メモリの短期キャッシュ）は、ブラウザ側の期限も短くする
        return _og_png_response(png, 60 if degraded else 86400)

    meta = _lookup_replay_og_meta(replay_id)
    board = validate_board(meta[2]) if meta[0] == "found" else None
    if board is None:
        # 旧リプレイ（board 無し）・存在しない id・DB 失敗: 汎用画像。一過性の失敗を長く固定しないため短い期限にする
        return _generic_og_response(86400 if meta[0] == "found" else 300)

    try:
        png, degraded = replay_og_image.get_or_render(replay_id, board, meta[1])
    except replay_og_image.RenderBusy:
        return _generic_og_response(60)   # 描画枠が空かない・描画の予算超過: 待たずに汎用画像
    except Exception as e:
        logger.error(f"リプレイ画像の生成エラー {replay_id}: {e}")
        return _generic_og_response(60)
    # カード画像が一部取れなかった描画は期限を短くする（次の取得で復旧できるように）
    return _og_png_response(png, 60 if degraded else 86400)


def _og_png_response(png, max_age):
    return Response(png, mimetype="image/png", headers={"Cache-Control": f"public, max-age={max_age}"})


def _generic_og_response(max_age):
    return send_file(replay_og_image.GENERIC_IMAGE_PATH, mimetype="image/png", max_age=max_age)


# TODO: calibrate from data — 上限値（logs件数・本文サイズ）は実データ未収集のため仮置き。
REPLAY_MAX_LOG_ENTRIES = 2000
REPLAY_MAX_BODY_BYTES = 512 * 1024


_CARD_TYPES = ("monster", "spell", "trap")


def _clean_card_types(card_types, images):
    """cardTypes（{cardId: 'monster'|'spell'|'trap'}）を検証して返す。
    images に無いカードID・不正な種類の値は捨てる（任意の値をDBに溜めないため）。"""
    if not isinstance(card_types, dict):
        return {}
    return {
        k: v for k, v in card_types.items()
        if isinstance(k, str) and k in images and v in _CARD_TYPES
    }


@solitaire_bp.route("/api/solitaire/replay", methods=["POST"])
def solitaire_replay_save():
    """リプレイデータをSupabaseに保存してIDを返す"""
    if not ENABLE_VISUAL_SOLO_PLAY:
        abort(404)

    rate_error = _consume_replay_rate_limit()
    if rate_error:
        return rate_error

    # リクエスト本文サイズの上限チェック（JSONパース前に生バイト長で判定）
    if len(request.get_data()) > REPLAY_MAX_BODY_BYTES:
        return {"error": "リクエストサイズが大きすぎます"}, 413

    _supabase_client = _get_supabase()
    if not _supabase_client:
        return {"error": "データベース未接続"}, 503
    try:
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            return {"error": "不正なリクエストです"}, 400
        images = data.get("images", {})
        names  = data.get("names", {})
        ex_card_ids = data.get("exCardIds", [])
        card_types_in = data.get("cardTypes")   # 省略可（旧クライアントは送らない）
        logs = data.get("logs", [])
        title = str(data.get("title", ""))[:100]
        # 型チェック: 期待型でなければ拒否
        if not isinstance(logs, list) or not isinstance(images, dict) \
                or not isinstance(names, dict) or not isinstance(ex_card_ids, list):
            return {"error": "不正なリクエストです"}, 400
        if card_types_in is not None and not isinstance(card_types_in, dict):
            return {"error": "不正なリクエストです"}, 400
        if not logs:
            return {"error": "logsが空です"}, 400
        if len(logs) > REPLAY_MAX_LOG_ENTRIES:
            return {"error": "logsが多すぎます"}, 400
        # board（リンク画像用の最後の盤面）。不備があっても共有自体は失敗させない（捨てて保存する）
        board_in = data.get("board")
        board = validate_board(board_in)
        if board_in is not None and board is None:
            logger.warning("リプレイ保存: board が不正のため破棄して保存を続行")
        replay_id = secrets.token_urlsafe(8)
        now_iso = datetime.now(timezone.utc).isoformat()
        payload = {
            "id": replay_id,
            "title": title,
            "images": images,
            "names": names,
            "ex_card_ids": ex_card_ids,
            "logs": logs,
            "created_at": now_iso,
        }
        if board:
            payload["board"] = board
        card_types = _clean_card_types(card_types_in, images)
        if card_types:
            payload["card_types"] = card_types
        try:
            _supabase_client.table("solitaire_replays").insert(payload).execute()
        except Exception as e:
            if "card_types" not in payload:
                raise
            # 列追加SQL（solitaire_replays_card_types.sql）の適用前でも共有自体は止めない。
            # カードの種類だけ失われる（再生後の「場に出す」メニューが魔法罠用になる）ため、警告を残す
            logger.warning(f"リプレイ保存: card_types 列なしで再試行します（SQL未適用の可能性）: {e}")
            payload.pop("card_types")
            _supabase_client.table("solitaire_replays").insert(payload).execute()
        if board:
            # X のクローラーは待ち時間が短いので、投稿される前に画像を先に描いてキャッシュを温める
            replay_og_image.warm_cache_async(replay_id, board, title)
        return {"id": replay_id}
    except Exception as e:
        logger.error(f"リプレイ保存エラー: {e}")
        return {"error": "保存に失敗しました"}, 500


@solitaire_bp.route("/api/solitaire/replay/<replay_id>", methods=["GET"])
def solitaire_replay_get(replay_id):
    """Supabaseからリプレイデータを取得"""
    if not ENABLE_VISUAL_SOLO_PLAY:
        abort(404)
    _supabase_client = _get_supabase()
    if not _supabase_client:
        return {"error": "データベース未接続"}, 503
    try:
        try:
            resp = _supabase_client.table("solitaire_replays") \
                .select("images, names, ex_card_ids, logs, title, card_types") \
                .eq("id", replay_id) \
                .limit(1) \
                .execute()
        except Exception as e:
            # 列追加SQL（solitaire_replays_card_types.sql）の適用前でも読み込めるようにする
            logger.warning(f"リプレイ取得: card_types 列なしで再試行します（SQL未適用の可能性）: {e}")
            resp = _supabase_client.table("solitaire_replays") \
                .select("images, names, ex_card_ids, logs, title") \
                .eq("id", replay_id) \
                .limit(1) \
                .execute()
        rows = resp.data if resp.data else []
        if not rows:
            return {"error": "見つかりません"}, 404
        row = rows[0]
        return {
            "images":     row["images"],
            "names":      row.get("names") or {},
            "exCardIds":  row.get("ex_card_ids") or [],
            "cardTypes":  row.get("card_types") or {},
            "logs":       row["logs"],
            "title":      row.get("title", ""),
        }
    except Exception as e:
        logger.error(f"リプレイ取得エラー: {e}")
        return {"error": "取得に失敗しました"}, 500
