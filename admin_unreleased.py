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

import io
import os
import hmac
import time
import logging
import threading
from urllib.parse import urlparse

from flask import Blueprint, request, jsonify, render_template

import card_display as _card_display
import fetch_guard as _fetch_guard

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 環境変数
# ──────────────────────────────────────────────

# 前後の空白・改行を除去する。環境変数の設定時にコピペで末尾改行等が
# 混入しても認証が通るようにするため（クライアント側も入力を trim している）。
_ADMIN_KEY = os.environ.get("ADMIN_KEY", "").strip()
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

    provided = request.headers.get("X-Admin-Key", "").strip()
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
    承認成功後、extraction_raw の image_urls から最初の1枚を取り込もうとする。
    画像取込失敗は承認自体を失敗させない。
    レスポンスに image_fetched: true/false と reason を含める。
    """
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    try:
        # まずカードの extraction_raw を取得
        card_resp = (
            _supabase.table("unreleased_cards")
            .select("id, name, source_url, extraction_raw, status")
            .eq("id", card_id)
            .execute()
        )
        if not card_resp.data:
            return jsonify({"error": "対象レコードが見つかりません"}), 404

        card_data = card_resp.data[0]
        if card_data["status"] not in ("pending", "rejected", "needs_review"):
            return jsonify({"error": "対象レコードが見つかりません（既に承認済みか存在しない可能性があります）"}), 404

        # ステータスを承認済みに更新
        upd_resp = (
            _supabase.table("unreleased_cards")
            .update({"status": "approved"})
            .eq("id", card_id)
            .execute()
        )
        if not upd_resp.data:
            return jsonify({"error": "承認処理に失敗しました"}), 500

        _card_display.invalidate_cache()

        # 画像取込を試みる（失敗しても承認は成立）
        image_fetched, image_reason = _try_fetch_image_from_extraction(
            card_id=card_id,
            source_url=card_data.get("source_url", ""),
            extraction_raw=card_data.get("extraction_raw") or {},
        )

        return jsonify({
            "ok": True,
            "card": upd_resp.data[0],
            "image_fetched": image_fetched,
            "image_reason": image_reason,
        })

    except Exception as e:
        logger.error(f"[admin] approve エラー id={card_id}: {e}")
        return jsonify({"error": "承認に失敗しました"}), 500


def _try_fetch_image_from_extraction(
    card_id: int,
    source_url: str,
    extraction_raw: dict,
) -> tuple[bool, str]:
    """
    extraction_raw の image_urls から最初の1枚を取り込む。

    Returns:
        (成功フラグ, 理由文字列)
    """
    image_urls = extraction_raw.get("image_urls", [])
    if not image_urls:
        return False, "image_urls が空です"

    image_url = image_urls[0]
    return _fetch_and_store_image(card_id, image_url, source_url)


def _fetch_and_store_image(
    card_id: int,
    image_url: str,
    source_page_url: str,
) -> tuple[bool, str]:
    """
    指定URLの画像を取得してStorageに保存し、official_card_images にINSERTする。
    ホワイトリスト検証・Content-Type確認・5MB上限チェックを行う。

    Returns:
        (成功フラグ, 理由文字列)
    """
    if not _supabase:
        return False, "Supabase 未接続"

    # ホワイトリスト検証
    if not _fetch_guard.is_whitelisted(image_url):
        logger.warning(f"[admin] 画像URLがホワイトリスト外のためスキップ: {image_url!r}")
        return False, f"ホワイトリスト外のURL: {image_url!r}"

    # 画像取得
    try:
        resp = _fetch_guard.fetch_whitelisted(image_url)
    except _fetch_guard.WhitelistViolation as e:
        logger.warning(f"[admin] 画像取得ホワイトリスト違反: {e}")
        return False, f"ホワイトリスト違反: {e}"
    except Exception as e:
        logger.warning(f"[admin] 画像取得失敗: {e}")
        return False, f"取得エラー: {e}"

    # Content-Type の確認
    content_type = resp.headers.get("Content-Type", "")
    if not content_type.startswith("image/"):
        logger.warning(f"[admin] Content-Type が image/* でない: {content_type!r} ({image_url!r})")
        return False, f"Content-Type が image/* でない: {content_type!r}"

    # サイズ上限チェック（5MB）
    MAX_SIZE = 5 * 1024 * 1024
    content = resp.content
    if len(content) > MAX_SIZE:
        logger.warning(f"[admin] 画像が5MBを超えています: {len(content):,}バイト ({image_url!r})")
        return False, f"画像サイズ超過: {len(content):,}バイト（上限5MB）"

    # 拡張子を推定（Content-Type から）
    ext_map = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
        "image/avif": "avif",
    }
    mime = content_type.split(";")[0].strip().lower()
    ext = ext_map.get(mime, "jpg")

    # Storage パスを決定
    storage_path = f"unreleased/{card_id}.{ext}"

    # Supabase Storage にアップロード（既存は上書き: upsert）
    try:
        # supabase-py: storage.from_(bucket).upload(path, data, options)
        # upsert に相当するには file_options で upsert=True を指定する
        _supabase.storage.from_("official-card-images").upload(
            path=storage_path,
            file=content,
            file_options={
                "content-type": mime,
                "upsert": "true",
            },
        )
    except Exception as e:
        # アップロードエラーをログに記録
        logger.error(f"[admin] Storage アップロード失敗: {e} (path={storage_path!r})")
        return False, f"Storage アップロード失敗: {e}"

    # public URL を取得
    try:
        url_resp = _supabase.storage.from_("official-card-images").get_public_url(storage_path)
        # supabase-py v2: get_public_url は文字列を返す
        if isinstance(url_resp, str):
            public_url = url_resp
        else:
            public_url = url_resp.get("publicUrl", "")
    except Exception as e:
        logger.warning(f"[admin] public URL 取得失敗: {e}")
        public_url = ""

    # source_domain を取得
    try:
        source_domain = urlparse(image_url).netloc
    except Exception:
        source_domain = ""

    # official_card_images に来歴付き INSERT
    try:
        _supabase.table("official_card_images").insert({
            "unreleased_card_id": card_id,
            "storage_path": storage_path,
            "public_url": public_url,
            "source_image_url": image_url,
            "source_page_url": source_page_url,
            "source_domain": source_domain,
        }).execute()
    except Exception as e:
        logger.error(f"[admin] official_card_images INSERT 失敗: {e}")
        return False, f"DB 登録失敗: {e}"

    _card_display.invalidate_cache()
    logger.info(f"[admin] 画像取込完了: card_id={card_id}, path={storage_path!r}")
    return True, f"取込成功: {storage_path}"


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


# ──────────────────────────────────────────────
# POST /api/admin/unreleased/<id>/fetch-image — 手動画像取込
# ──────────────────────────────────────────────

@admin_bp.route("/api/admin/unreleased/<int:card_id>/fetch-image", methods=["POST"])
def admin_fetch_image(card_id: int):
    """
    指定URLの画像を手動で取り込む。
    リクエストボディ: {"image_url": "https://..."}
    承認済み・linked ステータスのカードのみ対象。
    ホワイトリスト必須（ホワイトリスト外URLはエラー）。
    """
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    body = request.get_json(silent=True) or {}
    image_url = (body.get("image_url") or "").strip()
    if not image_url:
        return jsonify({"error": "image_url は必須です"}), 400

    # ホワイトリスト事前チェック（エラーにする）
    if not _fetch_guard.is_whitelisted(image_url):
        return jsonify({"error": f"ホワイトリスト外のURLは取り込めません: {image_url!r}"}), 400

    # カードが存在するか確認
    try:
        card_resp = (
            _supabase.table("unreleased_cards")
            .select("id, source_url, status")
            .eq("id", card_id)
            .execute()
        )
        if not card_resp.data:
            return jsonify({"error": "対象レコードが見つかりません"}), 404

        card_data = card_resp.data[0]
        if card_data["status"] not in ("approved", "linked"):
            return jsonify({"error": "承認済み（approved/linked）のカードにのみ画像を取り込めます"}), 400

    except Exception as e:
        logger.error(f"[admin] fetch-image カード確認エラー: {e}")
        return jsonify({"error": "カード確認に失敗しました"}), 500

    # 画像取込
    success, reason = _fetch_and_store_image(
        card_id=card_id,
        image_url=image_url,
        source_page_url=card_data.get("source_url", ""),
    )

    if success:
        return jsonify({"ok": True, "reason": reason})
    else:
        return jsonify({"ok": False, "error": reason}), 422


# ──────────────────────────────────────────────
# GET /api/admin/images/domains — 画像出所ドメイン一覧
# ──────────────────────────────────────────────

@admin_bp.route("/api/admin/images/domains", methods=["GET"])
def admin_list_image_domains():
    """
    official_card_images の source_domain ごとの件数・hidden件数を返す。
    deleted_at IS NULL（未削除）の行のみ集計する。
    """
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    try:
        # deleted_at IS NULL の全行を取得して集計（件数が多い場合も現実的に問題ない規模）
        resp = (
            _supabase.table("official_card_images")
            .select("source_domain, hidden")
            .is_("deleted_at", "null")
            .execute()
        )
        rows = resp.data or []

        # ドメインごとに集計
        domain_stats: dict[str, dict] = {}
        for row in rows:
            domain = row["source_domain"] or "(不明)"
            if domain not in domain_stats:
                domain_stats[domain] = {"domain": domain, "total": 0, "hidden": 0}
            domain_stats[domain]["total"] += 1
            if row["hidden"]:
                domain_stats[domain]["hidden"] += 1

        # ソート（total 降順）
        domains = sorted(domain_stats.values(), key=lambda x: x["total"], reverse=True)
        return jsonify({"domains": domains})

    except Exception as e:
        logger.error(f"[admin] images/domains 取得エラー: {e}")
        return jsonify({"error": "ドメイン一覧取得に失敗しました"}), 500


# ──────────────────────────────────────────────
# POST /api/admin/images/purge-domain — ドメイン一括削除
# ──────────────────────────────────────────────

@admin_bp.route("/api/admin/images/purge-domain", methods=["POST"])
def admin_purge_image_domain():
    """
    指定ドメインの画像を一括処理する。
    リクエストボディ: {"domain": "yu-gi-oh.jp", "physical": true/false}
      - physical=false（省略時）: hidden=true に更新（第1段階・即時非表示）
      - physical=true           : hidden=true ＋ Storage物理削除 ＋ deleted_at 記録（第2段階）

    レスポンス:
      {"ok": true, "hidden_count": N, "deleted_count": N, "errors": [...]}
    """
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    body = request.get_json(silent=True) or {}
    domain = (body.get("domain") or "").strip()
    if not domain:
        return jsonify({"error": "domain は必須です"}), 400

    physical = bool(body.get("physical", False))

    errors = []

    try:
        # 対象行を取得（storage_path も一緒に取得）
        target_resp = (
            _supabase.table("official_card_images")
            .select("id, storage_path, hidden, deleted_at")
            .eq("source_domain", domain)
            .is_("deleted_at", "null")
            .execute()
        )
        targets = target_resp.data or []

        if not targets:
            return jsonify({"ok": True, "hidden_count": 0, "deleted_count": 0, "errors": []})

        target_ids = [r["id"] for r in targets]

        # 第1段階: hidden=true
        hidden_resp = (
            _supabase.table("official_card_images")
            .update({"hidden": True})
            .in_("id", target_ids)
            .execute()
        )
        hidden_count = len(hidden_resp.data or [])
        logger.info(f"[admin] purge-domain: domain={domain!r}, hidden={hidden_count}件")

        deleted_count = 0

        if physical:
            # 第2段階: Storage物理削除 ＋ deleted_at 記録
            storage_paths = [r["storage_path"] for r in targets if r.get("storage_path")]

            if storage_paths:
                try:
                    _supabase.storage.from_("official-card-images").remove(storage_paths)
                    logger.info(f"[admin] Storage 削除: {len(storage_paths)}件")
                except Exception as e:
                    err_msg = f"Storage 削除失敗: {e}"
                    logger.error(f"[admin] {err_msg}")
                    errors.append(err_msg)

            # deleted_at を記録（Storage削除失敗分も含めて記録する）
            from datetime import datetime, timezone
            now_iso = datetime.now(timezone.utc).isoformat()
            del_resp = (
                _supabase.table("official_card_images")
                .update({"deleted_at": now_iso})
                .in_("id", target_ids)
                .execute()
            )
            deleted_count = len(del_resp.data or [])
            logger.info(f"[admin] purge-domain: deleted_at 記録={deleted_count}件")

        _card_display.invalidate_cache()

        return jsonify({
            "ok": True,
            "hidden_count": hidden_count,
            "deleted_count": deleted_count,
            "errors": errors,
        })

    except Exception as e:
        logger.error(f"[admin] purge-domain エラー: {e}")
        return jsonify({"error": "一括削除に失敗しました"}), 500
