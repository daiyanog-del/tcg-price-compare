"""
admin_unreleased.py — 未発売カード管理画面 Flask Blueprint

認証:
  - 環境変数 ADMIN_KEY が未設定の場合、全 /api/admin/* は 503 を返す
  - リクエストヘッダ X-Admin-Key を hmac.compare_digest で照合
  - 認証失敗に対してIPごとの簡易レート制限（失敗5回で60秒間429）

Blueprint prefix:
  - GET /admin       → 管理画面HTML（認証なし、データはAPI経由）
  - /api/admin/*     → 全REST API

状態変更系エンドポイントは全て card_display.invalidate_cache() を呼ぶ。
"""

import os
import hmac
import time
import logging
import threading

from flask import Blueprint, request, jsonify, render_template

import card_display as _card_display

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 環境変数
# ──────────────────────────────────────────────

_ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
_SUPABASE_URL = os.environ.get("SUPABASE_URL")
_SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# ──────────────────────────────────────────────
# Supabase クライアント（card_display とは独立）
# ──────────────────────────────────────────────

_supabase = None

if _SUPABASE_URL and _SUPABASE_KEY:
    try:
        from supabase import create_client
        _supabase = create_client(_SUPABASE_URL, _SUPABASE_KEY)
        logger.info("[admin] Supabase 接続成功")
    except Exception as _e:
        logger.warning(f"[admin] Supabase 接続失敗: {_e}")

# ──────────────────────────────────────────────
# 認証失敗レート制限（IPごと5回で60秒間429）
# ──────────────────────────────────────────────

_AUTH_FAIL_LIMIT = 5        # 失敗許容回数
_AUTH_FAIL_WINDOW = 60.0    # 制限時間（秒）

_auth_fail_lock = threading.Lock()
# IP → [失敗タイムスタンプのリスト]
_auth_fail_log: dict[str, list[float]] = {}


def _check_auth_rate_limit(ip: str) -> bool:
    """
    認証失敗レート制限を確認する。
    True = 制限中（429を返すべき）、False = 通過可能
    """
    now = time.time()
    with _auth_fail_lock:
        fails = _auth_fail_log.get(ip, [])
        # ウィンドウ外の古いエントリを除去
        fails = [t for t in fails if now - t < _AUTH_FAIL_WINDOW]
        _auth_fail_log[ip] = fails
        return len(fails) >= _AUTH_FAIL_LIMIT


def _record_auth_fail(ip: str) -> None:
    """認証失敗を記録する"""
    now = time.time()
    with _auth_fail_lock:
        fails = _auth_fail_log.get(ip, [])
        fails = [t for t in fails if now - t < _AUTH_FAIL_WINDOW]
        fails.append(now)
        _auth_fail_log[ip] = fails
        # メモリ肥大化防止（最大1000IP）
        if len(_auth_fail_log) > 1000:
            cutoff = now - _AUTH_FAIL_WINDOW
            stale = [k for k, v in _auth_fail_log.items()
                     if not v or v[-1] < cutoff]
            for k in stale[:500]:
                del _auth_fail_log[k]


# ──────────────────────────────────────────────
# 認証ヘルパー
# ──────────────────────────────────────────────

def _admin_key_enabled() -> bool:
    """ADMIN_KEY が設定済みかどうか"""
    return bool(_ADMIN_KEY)


def _require_admin_key():
    """
    管理APIの認証チェック。
    通過時は None を返す。
    失敗時は Flask レスポンスタプルを返す。
    """
    if not _admin_key_enabled():
        return jsonify({"error": "管理機能が無効です（ADMIN_KEY 未設定）"}), 503

    ip = request.remote_addr or "unknown"
    if _check_auth_rate_limit(ip):
        return jsonify({"error": "認証失敗が多すぎます。しばらく待ってから再試行してください"}), 429

    provided = request.headers.get("X-Admin-Key", "")
    if not hmac.compare_digest(provided, _ADMIN_KEY):
        _record_auth_fail(ip)
        return jsonify({"error": "認証に失敗しました"}), 401

    return None


# ──────────────────────────────────────────────
# Blueprint 定義
# ──────────────────────────────────────────────

admin_bp = Blueprint("admin", __name__)

# ──────────────────────────────────────────────
# GET /admin — 管理画面HTML（認証なし）
# ──────────────────────────────────────────────

@admin_bp.route("/admin")
def admin_page():
    """管理画面HTML。データは全てAPI経由なので未認証では何も見えない"""
    return render_template("admin.html")


# ──────────────────────────────────────────────
# POST /api/admin/auth-check — キー検証のみ
# ──────────────────────────────────────────────

@admin_bp.route("/api/admin/auth-check", methods=["POST"])
def admin_auth_check():
    """ログイン画面からキーを検証するだけのエンドポイント"""
    err = _require_admin_key()
    if err:
        return err
    return jsonify({"ok": True})


# ──────────────────────────────────────────────
# GET /api/admin/unreleased — 未発売カード一覧
# ──────────────────────────────────────────────

@admin_bp.route("/api/admin/unreleased", methods=["GET"])
def admin_list_unreleased():
    """
    未発売カード一覧を返す。
    クエリパラメータ status=pending,needs_review で複数指定可。
    省略時は全件返す。
    official_card_images の有無フラグ（has_image）を付加する。
    """
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    # status フィルタ
    status_param = request.args.get("status", "")
    valid_statuses = {"pending", "approved", "rejected", "linked", "needs_review"}
    if status_param:
        statuses = [s.strip() for s in status_param.split(",") if s.strip() in valid_statuses]
    else:
        statuses = []

    try:
        q = _supabase.table("unreleased_cards").select(
            "id, name, reading, card_type, attribute, race, "
            "level, rank, link_val, atk, def, effect_text, "
            "product_name, release_date, confidence, source_url, "
            "source_domain, extracted_at, status, hidden, konami_id"
        ).order("extracted_at", desc=True)

        if statuses:
            q = q.in_("status", statuses)

        resp = q.execute()
        cards = resp.data or []

        if not cards:
            return jsonify({"cards": []})

        # official_card_images の有無を確認（一括取得）
        card_ids = [c["id"] for c in cards]
        img_resp = (
            _supabase.table("official_card_images")
            .select("unreleased_card_id")
            .in_("unreleased_card_id", card_ids)
            .eq("hidden", False)
            .is_("deleted_at", "null")
            .execute()
        )
        ids_with_image = {row["unreleased_card_id"] for row in (img_resp.data or [])}

        for card in cards:
            card["has_image"] = card["id"] in ids_with_image

        return jsonify({"cards": cards})

    except Exception as e:
        logger.error(f"[admin] unreleased_cards 取得エラー: {e}")
        return jsonify({"error": "データ取得に失敗しました"}), 500


# ──────────────────────────────────────────────
# POST /api/admin/unreleased — 手動登録
# ──────────────────────────────────────────────

@admin_bp.route("/api/admin/unreleased", methods=["POST"])
def admin_create_unreleased():
    """
    未発売カードを手動登録する。
    name は必須。source_url/source_domain は 'manual' に固定。
    status=pending、confidence='high' で登録。
    """
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name は必須です"}), 400

    # 許可フィールド（手動登録時）
    row = {
        "name":          name,
        "reading":       (body.get("reading") or ""),
        "card_type":     (body.get("card_type") or ""),
        "attribute":     (body.get("attribute") or ""),
        "race":          (body.get("race") or ""),
        "level":         body.get("level"),
        "rank":          body.get("rank"),
        "link_val":      body.get("link_val"),
        "atk":           body.get("atk"),
        "def":           (body.get("def") or ""),
        "effect_text":   (body.get("effect_text") or ""),
        "product_name":  (body.get("product_name") or ""),
        "release_date":  body.get("release_date") or None,
        # 手動登録固定値
        "status":        "pending",
        "confidence":    "high",
        "source_url":    "manual",
        "source_domain": "manual",
    }

    try:
        resp = _supabase.table("unreleased_cards").insert(row).execute()
        created = resp.data[0] if resp.data else {}
        _card_display.invalidate_cache()
        return jsonify({"ok": True, "card": created}), 201

    except Exception as e:
        logger.error(f"[admin] unreleased_cards 登録エラー: {e}")
        err_msg = str(e)
        # UNIQUE 制約違反（同名・同パック）
        if "duplicate" in err_msg.lower() or "unique" in err_msg.lower():
            return jsonify({"error": "同じカード名・収録商品のカードが既に存在します"}), 409
        return jsonify({"error": "登録に失敗しました"}), 500


# ──────────────────────────────────────────────
# PUT /api/admin/unreleased/<id> — フィールド更新
# ──────────────────────────────────────────────

# 更新を許可するフィールドのホワイトリスト
_ALLOWED_UPDATE_FIELDS = frozenset([
    "name", "reading", "card_type", "attribute", "race",
    "level", "rank", "link_val", "atk", "def", "effect_text",
    "product_name", "release_date",
])


@admin_bp.route("/api/admin/unreleased/<int:card_id>", methods=["PUT"])
def admin_update_unreleased(card_id: int):
    """
    指定IDの未発売カードのフィールドを更新する。
    ホワイトリスト外のキーは無視する。
    """
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    body = request.get_json(silent=True) or {}
    # ホワイトリストでフィルタリング
    updates = {k: v for k, v in body.items() if k in _ALLOWED_UPDATE_FIELDS}

    if not updates:
        return jsonify({"error": "更新するフィールドがありません（許可フィールド外のキーは無視されます）"}), 400

    try:
        resp = (
            _supabase.table("unreleased_cards")
            .update(updates)
            .eq("id", card_id)
            .execute()
        )
        if not resp.data:
            return jsonify({"error": "対象レコードが見つかりません"}), 404
        _card_display.invalidate_cache()
        return jsonify({"ok": True, "card": resp.data[0]})

    except Exception as e:
        logger.error(f"[admin] unreleased_cards 更新エラー id={card_id}: {e}")
        return jsonify({"error": "更新に失敗しました"}), 500


# ──────────────────────────────────────────────
# POST /api/admin/unreleased/<id>/approve — 承認
# ──────────────────────────────────────────────

@admin_bp.route("/api/admin/unreleased/<int:card_id>/approve", methods=["POST"])
def admin_approve_unreleased(card_id: int):
    """
    指定IDのカードを承認する（status='approved'）。
    rejected / pending / needs_review から変更可。
    """
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    try:
        resp = (
            _supabase.table("unreleased_cards")
            .update({"status": "approved"})
            .eq("id", card_id)
            .in_("status", ["pending", "rejected", "needs_review"])
            .execute()
        )
        if not resp.data:
            return jsonify({"error": "対象レコードが見つかりません（既に承認済みか存在しない可能性があります）"}), 404
        _card_display.invalidate_cache()
        return jsonify({"ok": True, "card": resp.data[0]})

    except Exception as e:
        logger.error(f"[admin] approve エラー id={card_id}: {e}")
        return jsonify({"error": "承認に失敗しました"}), 500


# ──────────────────────────────────────────────
# POST /api/admin/unreleased/<id>/reject — 却下
# ──────────────────────────────────────────────

@admin_bp.route("/api/admin/unreleased/<int:card_id>/reject", methods=["POST"])
def admin_reject_unreleased(card_id: int):
    """指定IDのカードを却下する（status='rejected'）"""
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    try:
        resp = (
            _supabase.table("unreleased_cards")
            .update({"status": "rejected"})
            .eq("id", card_id)
            .execute()
        )
        if not resp.data:
            return jsonify({"error": "対象レコードが見つかりません"}), 404
        _card_display.invalidate_cache()
        return jsonify({"ok": True, "card": resp.data[0]})

    except Exception as e:
        logger.error(f"[admin] reject エラー id={card_id}: {e}")
        return jsonify({"error": "却下に失敗しました"}), 500


# ──────────────────────────────────────────────
# POST /api/admin/unreleased/<id>/toggle-hidden — 個別非表示トグル
# ──────────────────────────────────────────────

@admin_bp.route("/api/admin/unreleased/<int:card_id>/toggle-hidden", methods=["POST"])
def admin_toggle_hidden(card_id: int):
    """指定IDのカードの hidden フラグを反転させる"""
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    try:
        # 現在の hidden 値を取得
        get_resp = (
            _supabase.table("unreleased_cards")
            .select("id, hidden")
            .eq("id", card_id)
            .execute()
        )
        if not get_resp.data:
            return jsonify({"error": "対象レコードが見つかりません"}), 404

        current_hidden = get_resp.data[0]["hidden"]
        new_hidden = not current_hidden

        upd_resp = (
            _supabase.table("unreleased_cards")
            .update({"hidden": new_hidden})
            .eq("id", card_id)
            .execute()
        )
        _card_display.invalidate_cache()
        return jsonify({"ok": True, "hidden": new_hidden, "card": upd_resp.data[0] if upd_resp.data else {}})

    except Exception as e:
        logger.error(f"[admin] toggle-hidden エラー id={card_id}: {e}")
        return jsonify({"error": "非表示切替に失敗しました"}), 500


# ──────────────────────────────────────────────
# DELETE /api/admin/unreleased/<id> — 物理削除
# ──────────────────────────────────────────────

@admin_bp.route("/api/admin/unreleased/<int:card_id>", methods=["DELETE"])
def admin_delete_unreleased(card_id: int):
    """
    指定IDのカードを物理削除する（誤登録の取り消し用）。
    official_card_images は ON DELETE CASCADE で自動削除される。
    """
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    try:
        resp = (
            _supabase.table("unreleased_cards")
            .delete()
            .eq("id", card_id)
            .execute()
        )
        if not resp.data:
            return jsonify({"error": "対象レコードが見つかりません"}), 404
        _card_display.invalidate_cache()
        return jsonify({"ok": True, "deleted_id": card_id})

    except Exception as e:
        logger.error(f"[admin] delete エラー id={card_id}: {e}")
        return jsonify({"error": "削除に失敗しました"}), 500


# ──────────────────────────────────────────────
# GET /api/admin/settings — 設定一覧
# ──────────────────────────────────────────────

@admin_bp.route("/api/admin/settings", methods=["GET"])
def admin_get_settings():
    """app_settings テーブルの全件を返す"""
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    try:
        resp = _supabase.table("app_settings").select("key, value, updated_at").execute()
        return jsonify({"settings": resp.data or []})

    except Exception as e:
        logger.error(f"[admin] settings 取得エラー: {e}")
        return jsonify({"error": "設定取得に失敗しました"}), 500


# ──────────────────────────────────────────────
# POST /api/admin/settings/official-image-display — 公式画像表示トグル
# ──────────────────────────────────────────────

@admin_bp.route("/api/admin/settings/official-image-display", methods=["POST"])
def admin_toggle_official_image():
    """
    OFFICIAL_IMAGE_DISPLAY 設定を更新する。
    リクエストボディ: {"enabled": true/false}
    disabled にすると全サイトの公式取込画像が即時プロキシ表示に縮退する。
    """
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    body = request.get_json(silent=True) or {}
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": "enabled は true または false で指定してください"}), 400

    try:
        resp = (
            _supabase.table("app_settings")
            .upsert(
                {"key": "OFFICIAL_IMAGE_DISPLAY", "value": {"enabled": enabled}},
                on_conflict="key",
            )
            .execute()
        )
        # キャッシュを即時クリアして30秒待たずに全ワーカーに反映
        _card_display.invalidate_cache()
        return jsonify({"ok": True, "enabled": enabled})

    except Exception as e:
        logger.error(f"[admin] official-image-display 更新エラー: {e}")
        return jsonify({"error": "設定更新に失敗しました"}), 500
