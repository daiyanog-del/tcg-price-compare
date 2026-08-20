"""
solitaire_routes.py — 一人回し(solo play) シミュレータのルート群

このモジュールは一人回し機能を単一の feature flag 背後に閉じた
self-contained な Blueprint として提供する。IP 停止要請が来た際は
環境変数 ENABLE_VISUAL_SOLO_PLAY=0 を設定すれば、この機能だけが止まり、
相場・デッキ・コレクション等のコア機能は無傷で動き続ける（kill-switch）。

OFF 時の挙動:
  - GET /solitaire                      → 503 + solo_disabled.html（案内ページ）
  - POST /api/solitaire/replay          → 404
  - GET  /api/solitaire/replay/<id>     → 404

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

from flask import Blueprint, render_template, request, abort

from client_ip import _client_ip

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

@solitaire_bp.route("/solitaire")
def solitaire_page():
    """一人回しシミュレータ"""
    if not ENABLE_VISUAL_SOLO_PLAY:
        return render_template("solo_disabled.html"), 503
    return render_template("solitaire.html")



# TODO: calibrate from data — 上限値（logs件数・本文サイズ）は実データ未収集のため仮置き。
REPLAY_MAX_LOG_ENTRIES = 2000
REPLAY_MAX_BODY_BYTES = 512 * 1024


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
        logs = data.get("logs", [])
        title = str(data.get("title", ""))[:100]
        # 型チェック: 期待型でなければ拒否
        if not isinstance(logs, list) or not isinstance(images, dict) \
                or not isinstance(names, dict) or not isinstance(ex_card_ids, list):
            return {"error": "不正なリクエストです"}, 400
        if not logs:
            return {"error": "logsが空です"}, 400
        if len(logs) > REPLAY_MAX_LOG_ENTRIES:
            return {"error": "logsが多すぎます"}, 400
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
        _supabase_client.table("solitaire_replays").insert(payload).execute()
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
            "logs":       row["logs"],
            "title":      row.get("title", ""),
        }
    except Exception as e:
        logger.error(f"リプレイ取得エラー: {e}")
        return {"error": "取得に失敗しました"}, 500
