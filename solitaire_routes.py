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
import secrets
import logging
from datetime import datetime, timezone

from flask import Blueprint, render_template, request, abort

logger = logging.getLogger(__name__)

# ── feature flag（import 時評価。停止時は env で 0 を明示セット） ──
ENABLE_VISUAL_SOLO_PLAY = os.environ.get("ENABLE_VISUAL_SOLO_PLAY", "1") == "1"

solitaire_bp = Blueprint("solitaire", __name__)

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


@solitaire_bp.route("/api/solitaire/replay", methods=["POST"])
def solitaire_replay_save():
    """リプレイデータをSupabaseに保存してIDを返す"""
    if not ENABLE_VISUAL_SOLO_PLAY:
        abort(404)
    _supabase_client = _get_supabase()
    if not _supabase_client:
        return {"error": "データベース未接続"}, 503
    try:
        data = request.get_json(force=True)
        images = data.get("images", {})
        names  = data.get("names", {})
        ex_card_ids = data.get("exCardIds", [])
        logs = data.get("logs", [])
        title = str(data.get("title", ""))[:100]
        if not logs:
            return {"error": "logsが空です"}, 400
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
