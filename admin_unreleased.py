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

公開中のカードを非公開にする操作（承認待ちに戻す・却下・非表示・削除）は、X に投稿済みなら投稿も削除する
（unreleased_x_post.collect_posted_safe で対象を集め、DB 更新の成功後にバックグラウンドで削除）。
"""

import io
import os
import hmac
import time
import logging
import threading
from collections import Counter
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, render_template

import card_display as _card_display
import unreleased_x_post as _x_post
import unreleased_text_report as _text_report
import unreleased_approve as _approve
from unreleased_auto_approve import reason_label as _auto_approve_label
from unreleased_auto_approve import uncertain_field_labels as _uncertain_field_labels
from unreleased_auto_approve import verdict_reason_labels as _verdict_reason_labels
import fetch_guard as _fetch_guard
from constants import JST as _JST
from client_ip import _client_ip
from ygores_repository import repository as _ygores_repo

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
# 認証失敗レート制限（IP非依存のグローバル上限）
# IP単位のバケットは、攻撃者がリクエストごとに偽装IPを名乗ることで
# 無限に分散でき実質無力化されうる。全IP合計の失敗回数で歯止めをかける
# 「最後の砦」として、IP単位の制限とは別に併用する。
# TODO: calibrate from data — 閾値（60秒に20回）は実データ未収集のため仮置き。
# ──────────────────────────────────────────────

_AUTH_FAIL_GLOBAL_LIMIT = 20     # この回数に達したらブロック開始
_AUTH_FAIL_GLOBAL_WINDOW = 60.0  # 集計ウィンドウ・ブロック継続時間（秒）

_auth_fail_global_lock = threading.Lock()
_auth_fail_global_log: list[float] = []
_auth_fail_global_blocked_until = 0.0


def _check_global_auth_rate_limit() -> bool:
    """
    グローバル認証失敗レート制限を確認する。
    True = 制限中（429を返すべき）、False = 通過可能
    """
    with _auth_fail_global_lock:
        return time.time() < _auth_fail_global_blocked_until


def _record_global_auth_fail() -> None:
    """認証失敗をグローバルに記録し、上限到達時は以後 _AUTH_FAIL_GLOBAL_WINDOW 秒ブロックする"""
    global _auth_fail_global_blocked_until
    now = time.time()
    with _auth_fail_global_lock:
        _auth_fail_global_log[:] = [t for t in _auth_fail_global_log if now - t < _AUTH_FAIL_GLOBAL_WINDOW]
        _auth_fail_global_log.append(now)
        if len(_auth_fail_global_log) >= _AUTH_FAIL_GLOBAL_LIMIT:
            _auth_fail_global_blocked_until = now + _AUTH_FAIL_GLOBAL_WINDOW


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

    # IP偽装でIP単位バケットを分散されても総当たりを止める最後の砦（グローバル上限）
    if _check_global_auth_rate_limit():
        return jsonify({"error": "認証失敗が多すぎます。しばらく待ってから再試行してください"}), 429

    ip = _client_ip()
    if _check_auth_rate_limit(ip):
        return jsonify({"error": "認証失敗が多すぎます。しばらく待ってから再試行してください"}), 429

    provided = request.headers.get("X-Admin-Key", "").strip()
    if not hmac.compare_digest(provided, _ADMIN_KEY):
        _record_auth_fail(ip)
        _record_global_auth_fail()
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
            "level, rank, link_val, atk, def, pendulum_scale, pendulum_effect, effect_text, "
            "product_name, release_date, confidence, source_url, "
            "source_domain, extracted_at, status, hidden, konami_id, "
            "extraction_raw, review_candidates"
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
            .select("unreleased_card_id, source_image_url")
            .in_("unreleased_card_id", card_ids)
            .eq("hidden", False)
            .is_("deleted_at", "null")
            .execute()
        )
        ids_with_image = {row["unreleased_card_id"] for row in (img_resp.data or [])}
        source_url_by_id = {
            row["unreleased_card_id"]: (row.get("source_image_url") or "")
            for row in (img_resp.data or [])
        }

        # 利用者からのテキスト誤り報告（未対応）の件数。テーブルが無い等で読めなければ全て0
        open_reports = _text_report.count_open_by_card(_supabase)

        for card in cards:
            card["has_image"] = card["id"] in ids_with_image
            card["open_report_count"] = open_reports.get(card["id"], 0)
            card["source_image_url"] = source_url_by_id.get(card["id"], "")
            # extraction_raw からカード個別画像URLを取り出してトップレベルに付加
            raw = card.get("extraction_raw") or {}
            card_img_url = (raw.get("card_image_url") or "").strip()
            if not card_img_url:
                # 後方互換: card_image_urls の先頭
                fallback_list = raw.get("card_image_urls") or []
                card_img_url = fallback_list[0] if fallback_list else ""
            card["card_image_url"] = card_img_url
            for key in ("crop_suggestion", "crop_state", "release_date_evidence", "recognition_review", "auto_approve_verdict",
                        "auto_approve_skip", "text_check"):
                card[key] = raw.get(key)
            # 承認前確認で自動承認を見送った理由（判定とは別キー）の日本語ラベル
            skip = card.get("auto_approve_skip")
            if isinstance(skip, dict) and skip.get("reason"):
                card["auto_approve_skip_label"] = _auto_approve_label(skip["reason"])
            # 承認の経路（single / bulk / auto）。公開中一覧で自動承認の札を出すのに使う
            snap = raw.get("approval_snapshot")
            card["approval_via"] = snap.get("via") if isinstance(snap, dict) else None
            # 自動承認判定の理由コードを日本語ラベルにして添える（ラベルの正本は Python 側のみ）
            verdict = card.get("auto_approve_verdict")
            if isinstance(verdict, dict):
                card["auto_approve_reason_labels"] = _verdict_reason_labels(verdict)
            # 抽出AIが「不確か」とした欄（confidence が high 以外のとき）。旧データは None
            uncertain = raw.get("uncertain_fields")
            card["uncertain_fields"] = uncertain if isinstance(uncertain, list) else None
            card["uncertain_field_labels"] = _uncertain_field_labels(uncertain)
            # extraction_raw はフロントエンドに不要なので除外する（サイズ削減）
            card.pop("extraction_raw", None)

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
        "pendulum_scale": body.get("pendulum_scale"),
        "pendulum_effect": (body.get("pendulum_effect") or ""),
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
    "level", "rank", "link_val", "atk", "def",
    "pendulum_scale", "pendulum_effect", "effect_text",
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
        # 名前か発売日が変わったら、照合が付けた「要確認」の印は前提が崩れるので消す
        if "name" in updates or "release_date" in updates:
            cur = (
                _supabase.table("unreleased_cards")
                .select("name, release_date")
                .eq("id", card_id)
                .execute()
            )
            if cur.data:
                old = cur.data[0]
                new_rd = updates.get("release_date") or None
                old_rd = old.get("release_date") or None
                if (("name" in updates and updates["name"] != old.get("name"))
                        or ("release_date" in updates and new_rd != old_rd)):
                    updates["review_candidates"] = None

        resp = (
            _supabase.table("unreleased_cards")
            .update(updates)
            .eq("id", card_id)
            .execute()
        )
        if not resp.data:
            return jsonify({"error": "対象レコードが見つかりません"}), 404
        _card_display.invalidate_cache()
        # 編集後の値で誤字チェックと自動承認判定を取り直す（失敗しても保存は成立）
        _recompute_checks_after_edit(resp.data[0])
        return jsonify({"ok": True, "card": resp.data[0]})

    except Exception as e:
        logger.error(f"[admin] unreleased_cards 更新エラー id={card_id}: {e}")
        return jsonify({"error": "更新に失敗しました"}), 500


def _execute_approve_update(build_query, fields: dict):
    """status を approved にする UPDATE を実行する（本体は unreleased_approve.execute_approve_update）。"""
    return _approve.execute_approve_update(build_query, fields)


def _recompute_checks_after_edit(card: dict) -> None:
    """編集保存後のカード行で extraction_raw.text_check / auto_approve_verdict を計算し直して保存する。

    一覧の札は取込時の値を表示するため、編集して直しても「誤字の疑い」が残り続けてしまうのを防ぐ。
    - 取込時に判定が付いたカード（どちらかのキーが既にある）だけが対象。手動登録など旧データには札を新設しない
    - 全件読み込みが要る文脈（他カードの名前・「」内語）は短時間キャッシュ（自分自身は除いて使う）
    - 例外は警告ログのみ（保存を失敗させない）。card（応答に載せる行）の extraction_raw も更新する
    """
    try:
        raw = card.get("extraction_raw")
        if not (isinstance(raw, dict) and ("auto_approve_verdict" in raw or "text_check" in raw)):
            return
        from unreleased_auto_approve import recompute_card_checks
        from unreleased_image_store import _merge_extraction_raw
        checks = recompute_card_checks(_supabase, card)
        # 判定の計算中に他の書き込み（クロップ状態・承認記録など）が入っていても消さないよう、
        # 書き込み直前に最新の extraction_raw を読み直して、判定の2キーだけ差し替える
        if not _merge_extraction_raw(_supabase, card["id"], checks):
            return
        new_raw = dict(raw)
        new_raw.update(checks)
        card["extraction_raw"] = new_raw
    except Exception as e:
        logger.warning(f"[admin] 編集後の再判定に失敗（保存は成立） id={card.get('id')}: {e}")


# ──────────────────────────────────────────────
# POST /api/admin/unreleased/<id>/text-check — 効果テキストの再検査（保存はしない）
# ──────────────────────────────────────────────

_TEXT_CHECK_FIELDS = ("name", "effect_text", "pendulum_effect")


@admin_bp.route("/api/admin/unreleased/<int:card_id>/text-check", methods=["POST"])
def admin_recheck_text(card_id: int):
    """編集中の値（body）で効果テキストの誤字検証をやり直して結果を返す。DBには書かない。

    body に無い項目は DB の現在値を使う。同名・「」内語の文脈は取込時と同じ材料（他の未発売カード）。
    """
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    body = request.get_json(silent=True) or {}
    try:
        resp = (
            _supabase.table("unreleased_cards")
            .select("id, name, effect_text, pendulum_effect")
            .eq("id", card_id)
            .execute()
        )
        if not resp.data:
            return jsonify({"error": "対象レコードが見つかりません"}), 404
        stored = dict(resp.data[0])
        card = dict(stored)
        for key in _TEXT_CHECK_FIELDS:
            if key in body and (body[key] is None or isinstance(body[key], str)):
                card[key] = body[key] or ""

        from unreleased_auto_approve import compute_text_check, load_card_context
        # 全件の読み込みは1回（名前と「」内語を同時に）。必ず最新を読む（ユーザー操作の検査なのでキャッシュしない）。
        # このカード自身は id で除く: 登録済みの内容（編集前）を「他にも出る語」に数えず、
        # 元から文脈に入っていないカード（rejected など）の分を誤って引くこともない
        ctx = load_card_context(_supabase)
        if ctx is not None:
            others, context = ctx.excluding(card_id)
        else:
            others, context = [], None
        result = compute_text_check(card, others, context, context_has_self=False)
        return jsonify({"ok": True, "text_check": result})
    except Exception as e:
        logger.error(f"[admin] 効果テキスト再検査エラー id={card_id}: {e}")
        return jsonify({"error": "再検査に失敗しました"}), 500


# ──────────────────────────────────────────────
# 利用者からの「テキストの誤り報告」
# ──────────────────────────────────────────────

@admin_bp.route("/api/admin/unreleased/<int:card_id>/text-reports", methods=["GET"])
def admin_list_text_reports(card_id: int):
    """カード1枚への誤り報告の一覧（新しい順）。生IPのハッシュは返さない。"""
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    try:
        return jsonify({"ok": True, "reports": _text_report.list_reports(_supabase, card_id)})
    except Exception as e:
        logger.error(f"[admin] text-reports 取得エラー id={card_id}: {e}")
        return jsonify({"error": "報告の取得に失敗しました"}), 500


@admin_bp.route("/api/admin/text-reports/<int:report_id>/resolve", methods=["POST"])
def admin_resolve_text_report(report_id: int):
    """報告を対応済みにする。カードの公開状態は変えない。"""
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    try:
        row = _text_report.resolve_report(_supabase, report_id)
        if not row:
            return jsonify({"error": "対象の報告が見つかりません"}), 404
        return jsonify({"ok": True, "report": row})
    except Exception as e:
        logger.error(f"[admin] text-report resolve エラー id={report_id}: {e}")
        return jsonify({"error": "対応済みにできませんでした"}), 500


def _backfill_release_date(product_name: str, exclude_id: int) -> str | None:
    """他カードの多数決を使わず、商品に紐付く公式発売日だけを補完する（本体は unreleased_approve）。"""
    return _approve.backfill_release_date(product_name, exclude_id)


# 承認時に読む列（本体は unreleased_approve.APPROVE_SELECT_COLUMNS）。
_APPROVE_SELECT_COLUMNS = _approve.APPROVE_SELECT_COLUMNS


# ──────────────────────────────────────────────
# POST /api/admin/unreleased/<id>/approve — 承認
# ──────────────────────────────────────────────

@admin_bp.route("/api/admin/unreleased/<int:card_id>/approve", methods=["POST"])
def admin_approve_unreleased(card_id: int):
    """
    指定IDのカードを承認する（status='approved'）。
    rejected / pending / needs_review から変更可。
    承認の本体は unreleased_approve.approve_card（Flask 非依存。自動承認と共用）。このルートは呼ぶだけ。
    承認成功後、extraction_raw の image_urls から最初の1枚を取り込もうとする。
    画像取込失敗は承認自体を失敗させない。
    レスポンスに image_fetched: true/false と reason を含める。

    release_date ガード:
      - release_date が NULL の場合、商品名が一致する公式商品情報から補完を試みる
        （補完できれば release_date_note、できなければ release_date_warning を付加）
      - 有効な release_date が今日(JST)より前の場合、抽出時の年誤りの可能性を
        release_date_warning で警告する（承認自体はブロックしない）
    """
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    try:
        outcome = _approve.approve_card(
            _supabase, card_id, "single",
            # 画像取込は admin 側の関数を毎回引く（テストの差し替え・従来の呼び出し形を保つ）
            fetch_image=lambda **kw: _try_fetch_image_from_extraction(**kw),
            invalidate_cache=_card_display.invalidate_cache,
        )
        status = outcome.get("status")
        if status == "not_found":
            return jsonify({"error": "対象レコードが見つかりません"}), 404
        if status == "not_approvable":
            return jsonify({"error": "対象レコードが見つかりません（既に承認済みか存在しない可能性があります）"}), 404
        if status != "ok":
            return jsonify({"error": "承認処理に失敗しました"}), 500

        result = {
            "ok": True,
            "card": outcome["card"],
            "image_fetched": outcome["image_fetched"],
            "image_reason": outcome["image_reason"],
        }
        for key in ("release_date_note", "release_date_warning"):
            if outcome.get(key):
                result[key] = outcome[key]
        return jsonify(result)

    except Exception as e:
        logger.error(f"[admin] approve エラー id={card_id}: {e}")
        return jsonify({"error": "承認に失敗しました"}), 500


# ──────────────────────────────────────────────
# POST /api/admin/unreleased/<id>/link — 発売済みカードへ手動紐付け
# ──────────────────────────────────────────────

# needs_review は、旧方式（照合で status を needs_review に変えていた頃）に作られた既存データ向け。
# 現行の reconcile は status を approved のまま印だけ付けるため、新規には発生しない。
_LINKABLE_STATUSES = ("approved", "needs_review")


@admin_bp.route("/api/admin/unreleased/<int:card_id>/link", methods=["POST"])
def admin_link_unreleased(card_id: int):
    """
    未発売カードを発売済みカード（ygores の konami_id）に手動で紐付ける。
    リクエストボディ: {"konami_id": 12345}
    画像AIの誤読で自動照合できなかったカードを、管理者が候補から選んで紐付ける用途。

    対象は status が approved / needs_review のもの（pending は未レビューなので対象外）。
    konami_id は整数で、ygores 名前インデックスに実在するIDのみ受け付ける。
    更新内容: konami_id 設定・status='linked'・linked_at=現在UTC・review_candidates=NULL
    """
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    body = request.get_json(silent=True) or {}
    konami_id = body.get("konami_id")
    # bool は int のサブクラスなので明示的に除外する
    if isinstance(konami_id, bool) or not isinstance(konami_id, int):
        return jsonify({"error": "konami_id は整数で指定してください"}), 400

    try:
        name_index = _ygores_repo.get_name_index() or {}
    except Exception as e:
        logger.error(f"[admin] link: ygores 名前インデックス取得エラー: {e}")
        return jsonify({"error": "カード名インデックスの取得に失敗しました"}), 500

    valid_ids: set[int] = set()
    for ids in name_index.values():
        for cid in (ids if isinstance(ids, list) else [ids]):
            try:
                valid_ids.add(int(cid))
            except (TypeError, ValueError):
                continue
    if konami_id not in valid_ids:
        return jsonify({"error": "指定された konami_id は発売済みカードに存在しません"}), 400

    try:
        get_resp = (
            _supabase.table("unreleased_cards")
            .select("id, status")
            .eq("id", card_id)
            .execute()
        )
        if not get_resp.data:
            return jsonify({"error": "対象レコードが見つかりません"}), 404
        if get_resp.data[0]["status"] not in _LINKABLE_STATUSES:
            return jsonify({"error": "紐付けできるのは公開中（approved）または要確認（needs_review）のカードのみです"}), 400

        upd_resp = (
            _supabase.table("unreleased_cards")
            .update({
                "konami_id": konami_id,
                "status": "linked",
                "linked_at": datetime.now(timezone.utc).isoformat(),
                "review_candidates": None,
            })
            .eq("id", card_id)
            .in_("status", list(_LINKABLE_STATUSES))
            .execute()
        )
        if not upd_resp.data:
            return jsonify({"error": "紐付けに失敗しました（状態が変わった可能性があります）"}), 409
        _card_display.invalidate_cache()
        logger.info(f"[admin] link: card_id={card_id} -> konami_id={konami_id}")
        return jsonify({"ok": True, "card": upd_resp.data[0]})

    except Exception as e:
        logger.error(f"[admin] link エラー id={card_id}: {e}")
        return jsonify({"error": "紐付けに失敗しました"}), 500


# ──────────────────────────────────────────────
# POST /api/admin/unreleased/<id>/review-dismiss — 「確認済み（まだ未発売）」
# ──────────────────────────────────────────────

@admin_bp.route("/api/admin/unreleased/<int:card_id>/review-dismiss", methods=["POST"])
def admin_review_dismiss(card_id: int):
    """
    reconcile が付けた「要確認」の印を、管理者が確認済み（まだ未発売）にする。
    status='approved' かつ review_candidates があるカードの
    review_candidates.dismissed_at に現在UTCを入れる。以後 reconcile は印を上書きしない。
    """
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    try:
        get_resp = (
            _supabase.table("unreleased_cards")
            .select("id, status, review_candidates")
            .eq("id", card_id)
            .execute()
        )
        if not get_resp.data:
            return jsonify({"error": "対象レコードが見つかりません"}), 404
        card = get_resp.data[0]
        review = card.get("review_candidates")
        if card["status"] != "approved" or not isinstance(review, dict):
            return jsonify({"error": "要確認の印が付いた公開中（approved）のカードのみ対象です"}), 400
        if review.get("reason") == "multiple_exact":
            # 同名の発売済みカードが既にある＝発売済みなので「まだ未発売」にはできない（紐付けのみ）
            return jsonify({"error": "同名の発売済みカードが複数ある場合は確認済みにできません。紐付けてください"}), 400

        new_review = dict(review)
        new_review["dismissed_at"] = datetime.now(timezone.utc).isoformat()
        # 確認済み時点の理由と候補ID。reconcile は理由変化・候補増加を検知して印を付け直す
        new_review["dismissed_signature"] = {
            "reason": review.get("reason"),
            # 切り詰め前の全候補ID（無い旧データは表示用 candidates のIDにフォールバック）
            "candidate_ids": sorted(
                cid for cid in (
                    review.get("all_candidate_ids")
                    or [c.get("konami_id") for c in (review.get("candidates") or [])
                        if isinstance(c, dict)]
                ) if isinstance(cid, int)
            ),
        }
        upd_resp = (
            _supabase.table("unreleased_cards")
            .update({"review_candidates": new_review})
            .eq("id", card_id)
            .eq("status", "approved")
            .execute()
        )
        if not upd_resp.data:
            return jsonify({"error": "更新に失敗しました（状態が変わった可能性があります）"}), 409
        # 公開サイトの表示に影響しない（review_candidates は表示に使わない）ためキャッシュ無効化は不要
        return jsonify({"ok": True, "card": upd_resp.data[0]})

    except Exception as e:
        logger.error(f"[admin] review-dismiss エラー id={card_id}: {e}")
        return jsonify({"error": "確認済みの記録に失敗しました"}), 500


def _try_fetch_image_from_extraction(
    card_id: int,
    source_url: str,
    extraction_raw: dict,
) -> tuple[bool, str]:
    """承認時の画像取込（本体は unreleased_approve.try_fetch_image_from_extraction）。"""
    return _approve.try_fetch_image_from_extraction(
        _supabase, card_id, source_url, extraction_raw, invalidate_cache=_card_display.invalidate_cache)


def _fetch_and_store_image(
    card_id: int,
    image_url: str,
    source_page_url: str,
) -> tuple[bool, str]:
    """指定URLの画像を取得して保存する（本体は unreleased_approve.fetch_and_store_image）。"""
    return _approve.fetch_and_store_image(
        _supabase, card_id, image_url, source_page_url, invalidate_cache=_card_display.invalidate_cache)


# ──────────────────────────────────────────────
# POST /api/admin/unreleased/bulk-approve — 一括承認
# ──────────────────────────────────────────────

# 一括承認の画像取込スレッドで使用するロック（同時実行抑制）
_bulk_image_lock = threading.Lock()


def _notify_snapshot_failed(card_ids: list[int]) -> None:
    """承認記録（計測用データ）を保存できなかったカードを Discord に1通でまとめて知らせる。通知失敗は握りつぶす。"""
    ids = sorted(card_ids)
    shown = ", ".join(str(i) for i in ids[:50]) + (f" ほか{len(ids) - 50}件" if len(ids) > 50 else "")
    logger.warning(f"[admin] 一括承認・承認記録を保存できなかったカード: {shown}")
    try:
        from discord_notify import send_discord_message
        send_discord_message(f"[未発売承認] 一括承認の承認記録（approval_snapshot）を保存できなかったカードが{len(ids)}件あります"
                             f"（承認・X投稿は成立。自動承認の校正用データが欠けます）: card_id={shown}")
    except Exception as exc:
        logger.warning(f"[admin] 承認記録の欠落通知に失敗: {exc}")


@admin_bp.route("/api/admin/unreleased/bulk-approve", methods=["POST"])
def admin_bulk_approve_unreleased():
    """
    複数カードを一括承認する（status='approved'に更新）。
    リクエストボディ: {"ids": [1, 2, 3, ...]}

    処理フロー:
      1. 指定IDのうち pending/needs_review/rejected のものを一括で status='approved' に更新
      2. release_date が NULL のカードは、商品名が一致する公式商品情報から補完を試みる
         （単体承認と同じガード。product_name ごとに補完値をメモ化して重複ルックアップを避ける）
      3. 各カードの画像取込はバックグラウンドスレッドで非同期実行（タイムアウト回避）
      4. 承認完了時点でレスポンスを返す（画像取込の完了を待たない）

    レスポンス: {
        "approved": N, "image_fetch": "バックグラウンドで取込中",
        "release_date_backfilled": N,
        "release_date_warnings": [{"id": ..., "name": ..., "reason": ...}, ...],
    }
    """
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    body = request.get_json(silent=True) or {}
    ids = body.get("ids", [])

    # ids の検証（整数リストであること）
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "ids は1件以上の整数リストで指定してください"}), 400

    try:
        ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        return jsonify({"error": "ids の各要素は整数である必要があります"}), 400

    if len(ids) > 500:
        return jsonify({"error": "一度に承認できるのは最大500件です"}), 400

    try:
        # pending/needs_review/rejected のもののみ対象（既承認・linked は除外）
        target_resp = (
            _supabase.table("unreleased_cards")
            .select(_APPROVE_SELECT_COLUMNS)
            .in_("id", ids)
            .in_("status", ["pending", "needs_review", "rejected"])
            .execute()
        )
        target_cards = target_resp.data or []

        if not target_cards:
            return jsonify({"approved": 0, "image_fetch": "対象カードがありませんでした"}), 200

        target_ids = [c["id"] for c in target_cards]

        # ステータスを一括更新
        # approved_at は X 自動投稿の対象判定に使う。status と同じ UPDATE で入れる
        _execute_approve_update(
            lambda f: _supabase.table("unreleased_cards").update(f).in_("id", target_ids),
            {"status": "approved", "approved_at": datetime.now(timezone.utc).isoformat(), "approved_via": "bulk"},
        )

        approved_count = len(target_ids)
        logger.info(f"[admin] 一括承認: {approved_count}件 (ids={target_ids})")

        _card_display.invalidate_cache()

        # release_date ガード: NULLのカードへ補完を試み、補完不可・過去日は警告として収集
        release_date_backfilled = 0
        release_date_warnings: list[dict] = []
        _backfill_cache: dict[str, str | None] = {}
        today = datetime.now(_JST).strftime("%Y-%m-%d")

        for card_data in target_cards:
            card_id_ = card_data["id"]
            card_name_ = card_data.get("name", "")
            release_date = card_data.get("release_date")
            product_name = card_data.get("product_name") or ""

            if not release_date:
                # product_name ごとに補完値をメモ化（同一商品の重複ルックアップを避ける）
                if product_name not in _backfill_cache:
                    _backfill_cache[product_name] = _backfill_release_date(product_name, card_id_)
                backfilled = _backfill_cache[product_name]

                if backfilled:
                    try:
                        _supabase.table("unreleased_cards").update(
                            {"release_date": backfilled}
                        ).eq("id", card_id_).execute()
                        release_date_backfilled += 1
                        release_date = backfilled
                        logger.info(
                            f"[admin] 一括承認・release_date 補完: card_id={card_id_}, "
                            f"release_date={backfilled}"
                        )
                    except Exception as e:
                        logger.warning(
                            f"[admin] 一括承認・release_date 補完UPDATE失敗 card_id={card_id_}: {e}"
                        )
                        release_date_warnings.append(
                            {"id": card_id_, "name": card_name_, "reason": "発売日未設定"}
                        )
                        continue
                else:
                    release_date_warnings.append(
                        {"id": card_id_, "name": card_name_, "reason": "発売日未設定"}
                    )
                    continue

            if release_date and release_date < today:
                release_date_warnings.append(
                    {"id": card_id_, "name": card_name_, "reason": f"発売日が過去日 ({release_date})"}
                )

        if release_date_warnings:
            logger.warning(
                f"[admin] 一括承認・release_date 要注意: {len(release_date_warnings)}件 "
                f"({release_date_warnings})"
            )

        # 補完UPDATEは先の invalidate_cache() より後に走るため、補完があった場合は
        # もう一度無効化して古い NULL 値がキャッシュTTL分残らないようにする
        if release_date_backfilled > 0:
            _card_display.invalidate_cache()

        # 画像取込をバックグラウンドスレッドで起動
        def _bg_fetch_images(cards: list[dict]) -> None:
            """バックグラウンドで各カードの承認記録を保存し、画像を順次取込し、X投稿する。"""
            fetched_ids: list[int] = []
            with _bulk_image_lock:
                # 承認時の記録（補完前の値で差分を取る）。作成も保存もここ（バックグラウンド）で行い、
                # レスポンスを待たせない。失敗しても画像取込は続ける（record_approval_snapshots は例外を投げない）。
                # 承認記録は計測用で投稿条件ではない。保存できなかったカードは Discord にまとめて1通知らせる
                snapshot_failed: list[int] = []
                try:
                    from unreleased_auto_approve import record_approval_snapshots
                    record_approval_snapshots(_supabase, cards, "bulk", failed_ids=snapshot_failed)
                except Exception as exc:
                    logger.warning(f"[admin] 一括承認・承認記録の保存エラー: {exc}")
                    snapshot_failed = [c["id"] for c in cards]
                if snapshot_failed:
                    _notify_snapshot_failed(snapshot_failed)
                for card_data in cards:
                    try:
                        success, reason = _try_fetch_image_from_extraction(
                            card_id=card_data["id"],
                            source_url=card_data.get("source_url", ""),
                            extraction_raw=card_data.get("extraction_raw") or {},
                        )
                        if success:
                            fetched_ids.append(card_data["id"])
                        logger.info(
                            f"[admin] 一括承認・画像取込: card_id={card_data['id']}, "
                            f"success={success}, reason={reason!r}"
                        )
                    except Exception as exc:
                        logger.error(
                            f"[admin] 一括承認・画像取込エラー card_id={card_data['id']}: {exc}"
                        )
            # X自動投稿（無効時は何もしない）。画像取込に成功した分だけ、間隔を空けて1枚ずつ。
            # 画像取込のロックは先に手放す（投稿の待ち時間で次の一括承認の画像取込を止めないため）。
            # 取込に失敗した分は回収ジョブ（tools/post_unreleased_x.py）が拾う
            # 1回の件数は MAX_BULK_POST_PER_RUN まで。超えた分は予約せず回収ジョブに回る
            if fetched_ids:
                _x_post.post_cards_sequential(_supabase, fetched_ids, max_cards=_x_post.MAX_BULK_POST_PER_RUN)

        t = threading.Thread(
            target=_bg_fetch_images,
            args=(target_cards,),
            daemon=True,
        )
        t.start()

        return jsonify({
            "approved": approved_count,
            "image_fetch": "バックグラウンドで取込中",
            "release_date_backfilled": release_date_backfilled,
            "release_date_warnings": release_date_warnings,
        })

    except Exception as e:
        logger.error(f"[admin] bulk-approve エラー: {e}")
        return jsonify({"error": "一括承認に失敗しました"}), 500


# ──────────────────────────────────────────────
# POST /api/admin/unreleased/bulk-reject — 一括却下
# ──────────────────────────────────────────────

@admin_bp.route("/api/admin/unreleased/bulk-reject", methods=["POST"])
def admin_bulk_reject_unreleased():
    """
    複数カードを一括却下する（status='rejected'に更新）。
    リクエストボディ: {"ids": [1, 2, 3, ...]}

    pending/needs_review のものを対象に一括で status='rejected' に更新する
    （bulk-approve と同じ絞り込み方針。既に rejected/approved/linked のものは除外）。
    却下は画像取込を伴わないため、バックグラウンド処理は不要。

    レスポンス: {"rejected": N, "requested": M, "skipped": [対象外だったID, ...]}
    """
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    body = request.get_json(silent=True) or {}
    ids = body.get("ids", [])

    # ids の検証（整数リストであること）
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "ids は1件以上の整数リストで指定してください"}), 400

    try:
        ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        return jsonify({"error": "ids の各要素は整数である必要があります"}), 400

    if len(ids) > 500:
        return jsonify({"error": "一度に却下できるのは最大500件です"}), 400

    try:
        # pending/needs_review のもののみ対象（既rejected・approved・linked は除外）
        target_resp = (
            _supabase.table("unreleased_cards")
            .select("id")
            .in_("id", ids)
            .in_("status", ["pending", "needs_review"])
            .execute()
        )
        target_cards = target_resp.data or []
        target_ids = [c["id"] for c in target_cards]
        skipped_ids = [i for i in ids if i not in target_ids]

        if not target_cards:
            return jsonify({"rejected": 0, "requested": len(ids), "skipped": skipped_ids}), 200

        # ステータスを一括更新
        # TOCTOU対策: SELECT〜UPDATEの間に他の操作で状態が変わった行を確実に除外するため、
        # UPDATE側にも同じstatus条件を付ける（SELECT時点のtarget_idsだけを信用しない）。
        _supabase.table("unreleased_cards").update({"status": "rejected"}).in_(
            "id", target_ids
        ).in_("status", ["pending", "needs_review"]).execute()

        rejected_count = len(target_ids)
        logger.info(f"[admin] 一括却下: {rejected_count}件 (ids={target_ids})")

        _card_display.invalidate_cache()

        return jsonify({"rejected": rejected_count, "requested": len(ids), "skipped": skipped_ids})

    except Exception as e:
        logger.error(f"[admin] bulk-reject エラー: {e}")
        return jsonify({"error": "一括却下に失敗しました"}), 500


# ──────────────────────────────────────────────
# POST /api/admin/unreleased/<id>/reject — 却下
# ──────────────────────────────────────────────

@admin_bp.route("/api/admin/unreleased/<int:card_id>/unpublish", methods=["POST"])
def admin_unpublish_unreleased(card_id: int):
    """
    公開中カードを承認待ち（status='pending'）に戻す。
    レコード・画像レコードは保持したまま、承認待ちタブに戻す（やり直し用）。

    却下（reject）との違い:
      - reject は status='rejected' でブロックリストに残り、自動巡回でも今後拾わない
      - unpublish は pending に戻すだけなので、編集・再クロップ・再承認ができる
    """
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    try:
        # 承認済み（approved/linked）のもののみ対象
        get_resp = (
            _supabase.table("unreleased_cards")
            .select("id, status")
            .eq("id", card_id)
            .execute()
        )
        if not get_resp.data:
            return jsonify({"error": "対象レコードが見つかりません"}), 404
        if get_resp.data[0]["status"] not in ("approved", "linked"):
            return jsonify({"error": "公開中（approved/linked）のカードのみ承認待ちに戻せます"}), 400

        # X に投稿済みなら、非公開にした後で投稿も削除する（誤字を直して再承認すれば再投稿される）
        posted = _x_post.collect_posted_safe(_supabase, [card_id])
        resp = (
            _supabase.table("unreleased_cards")
            .update({"status": "pending"})
            .eq("id", card_id)
            .execute()
        )
        _card_display.invalidate_cache()
        _x_post.start_delete_thread(_supabase, posted)
        return jsonify({"ok": True, "card": resp.data[0] if resp.data else {}})

    except Exception as e:
        logger.error(f"[admin] unpublish エラー id={card_id}: {e}")
        return jsonify({"error": "承認待ちに戻す処理に失敗しました"}), 500


@admin_bp.route("/api/admin/unreleased/<int:card_id>/reject", methods=["POST"])
def admin_reject_unreleased(card_id: int):
    """指定IDのカードを却下する（status='rejected'）"""
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    try:
        # 承認済みを却下した場合は、X に投稿済みなら投稿も削除する（承認前のカードには投稿が無いので空）
        posted = _x_post.collect_posted_safe(_supabase, [card_id])
        resp = (
            _supabase.table("unreleased_cards")
            .update({"status": "rejected"})
            .eq("id", card_id)
            .execute()
        )
        if not resp.data:
            return jsonify({"error": "対象レコードが見つかりません"}), 404
        _card_display.invalidate_cache()
        _x_post.start_delete_thread(_supabase, posted)
        return jsonify({"ok": True, "card": resp.data[0]})

    except Exception as e:
        logger.error(f"[admin] reject エラー id={card_id}: {e}")
        return jsonify({"error": "却下に失敗しました"}), 500


# ──────────────────────────────────────────────
# POST /api/admin/unreleased/<id>/restore — 却下済みを承認待ちに戻す
# ──────────────────────────────────────────────

@admin_bp.route("/api/admin/unreleased/<int:card_id>/restore", methods=["POST"])
def admin_restore_unreleased(card_id: int):
    """
    却下済み（status='rejected'）のカードを承認待ち（status='pending'）に戻す。
    unpublish（approved/linked→pending）とは対象ステータスが異なる専用処理。
    """
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    try:
        get_resp = (
            _supabase.table("unreleased_cards")
            .select("id, status")
            .eq("id", card_id)
            .execute()
        )
        if not get_resp.data:
            return jsonify({"error": "対象レコードが見つかりません"}), 404
        if get_resp.data[0]["status"] != "rejected":
            return jsonify({"error": "却下済みのカードのみ承認待ちに戻せます"}), 400

        resp = (
            _supabase.table("unreleased_cards")
            .update({"status": "pending"})
            .eq("id", card_id)
            .execute()
        )
        _card_display.invalidate_cache()
        return jsonify({"ok": True, "card": resp.data[0] if resp.data else {}})

    except Exception as e:
        logger.error(f"[admin] restore エラー id={card_id}: {e}")
        return jsonify({"error": "承認待ちに戻す処理に失敗しました"}), 500


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

        # 非表示にしたら X の投稿も削除する（行も消す）。再表示すると、公開中（approved）・approved_at あり・
        # 投稿行なしの条件を満たすので、回収ジョブ（tools/post_unreleased_x.py・3時間ごと）が再投稿しうる。
        # 内容を確認した上での再表示なので、これは意図した挙動（decisions.md 参照）
        posted = _x_post.collect_posted_safe(_supabase, [card_id]) if new_hidden else []
        upd_resp = (
            _supabase.table("unreleased_cards")
            .update({"hidden": new_hidden})
            .eq("id", card_id)
            .execute()
        )
        _card_display.invalidate_cache()
        _x_post.start_delete_thread(_supabase, posted)
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

    ただし次の場合は物理削除せず、却下（rejected）扱いにして行を残す（レスポンスに rejected_instead と note）:
      - 公開中（approved/linked）のカード
      - 取込由来（source_url が manual 以外）の承認待ち（pending/needs_review）のカード
    行を消すと、同じ投稿元の再取込が新規カードとして入り直し、自動承認で再び公開されてしまうため
    （rejected の行が「取り込み済み」の目印になり、取込の重複除外と same_source_history が効く）。
    X 投稿の削除は従来どおり行う。manual（手動登録）の承認待ちと、却下済み（rejected）の削除は従来どおり物理削除
    （完全に消したいときは却下タブから削除する）。
    """
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    try:
        cur = _supabase.table("unreleased_cards").select("id, status, source_url").eq("id", card_id).execute()
        cur_row = cur.data[0] if cur.data else {}
        imported = (cur_row.get("source_url") or "").strip() not in ("", "manual")
        if cur_row and (cur_row.get("status") in ("approved", "linked")
                        or (cur_row.get("status") in ("pending", "needs_review") and imported)):
            posted = _x_post.collect_posted_safe(_supabase, [card_id])
            upd = _supabase.table("unreleased_cards").update({"status": "rejected"}).eq("id", card_id).execute()
            if not upd.data:
                return jsonify({"error": "対象レコードが見つかりません"}), 404
            _card_display.invalidate_cache()
            _x_post.start_delete_thread(_supabase, posted)
            return jsonify({"ok": True, "deleted_id": card_id, "rejected_instead": True,
                            "note": "公開中、または取込由来のカードは物理削除せず却下扱いにしました（再取込で入り直さないため）。"
                                    "完全に消すには、却下タブから削除してください"})

        # 退避した元画像のパスは、行を消す前でないと extraction_raw を読めない
        original_paths: list[str] = []
        try:
            from unreleased_image_store import collect_original_paths
            original_paths = collect_original_paths(_supabase, [card_id])
        except Exception as e:
            logger.warning(f"[admin] 退避元画像パスの収集失敗（無視）: id={card_id}: {e}")

        # X の投稿IDも行を消す前に読む（unreleased_x_posts は ON DELETE CASCADE で一緒に消え、後からは読めない）
        posted = _x_post.collect_posted_safe(_supabase, [card_id])
        resp = (
            _supabase.table("unreleased_cards")
            .delete()
            .eq("id", card_id)
            .execute()
        )
        if not resp.data:
            return jsonify({"error": "対象レコードが見つかりません"}), 404
        _x_post.start_delete_thread(_supabase, posted)

        # バケットは公開のため、退避元画像を残すと削除後も閲覧できてしまう。失敗しても削除は成功扱い
        if original_paths:
            try:
                _supabase.storage.from_("official-card-images").remove(original_paths)
            except Exception as e:
                logger.warning(f"[admin] 退避元画像の削除失敗（無視）: id={card_id}: {e}")
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
            .select("id, unreleased_card_id, storage_path, hidden, deleted_at")
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
            # 退避した元画像（バケットは公開のため、残すと削除後も閲覧できてしまう）も消す
            try:
                from unreleased_image_store import collect_original_paths
                card_ids = sorted({r["unreleased_card_id"] for r in targets if r.get("unreleased_card_id")})
                storage_paths += collect_original_paths(_supabase, card_ids)
            except Exception as e:
                logger.warning(f"[admin] 退避元画像パスの収集失敗（無視）: {e}")

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


# ──────────────────────────────────────────────
# POST /api/admin/unreleased/fetch-x-post — XポストURLから手動取り込み
# ──────────────────────────────────────────────

import re as _re

_TWEET_ID_RE = _re.compile(r"/status/(\d+)")


# ──────────────────────────────────────────────
# POST /api/admin/unreleased/<id>/crop-image — 画像クロップ
# ──────────────────────────────────────────────

@admin_bp.route("/api/admin/unreleased/<int:card_id>/crop-image", methods=["POST"])
def admin_crop_image(card_id: int):
    """
    保存済み画像を指定割合でクロップし直す。
    リクエストボディ: {"left": 0.05, "top": 0.02, "right": 0.55, "bottom": 0.98}
    （画像全体を 1.0 とした割合）
    クロップ後の画像で Storage を上書きし、管理画面のプレビューURLを返す。
    """
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    body = request.get_json(silent=True) or {}
    try:
        left   = float(body["left"])
        top    = float(body["top"])
        right  = float(body["right"])
        bottom = float(body["bottom"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "left/top/right/bottom を 0.0〜1.0 の数値で指定してください"}), 400

    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        return jsonify({"error": "クロップ範囲が不正です（left < right、top < bottom を満たす必要があります）"}), 400

    from unreleased_image_store import crop_and_save_image
    ok, result = crop_and_save_image(_supabase, card_id, left, top, right, bottom)
    if ok:
        _card_display.invalidate_cache()
        return jsonify({"ok": True, "new_url": result})
    else:
        return jsonify({"ok": False, "error": result}), 422


# ──────────────────────────────────────────────
# POST /api/admin/unreleased/<id>/restore-original-image — 元画像に戻す
# ──────────────────────────────────────────────

@admin_bp.route("/api/admin/unreleased/<int:card_id>/restore-original-image", methods=["POST"])
def admin_restore_original_image(card_id: int):
    """クロップ済みの画像を、退避してある元画像（無ければ source_image_url）に戻す。"""
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    from unreleased_image_store import restore_original_image
    ok, result = restore_original_image(_supabase, card_id)
    if ok:
        _card_display.invalidate_cache()
        return jsonify({"ok": True, "new_url": result})
    return jsonify({"ok": False, "error": result}), 422


@admin_bp.route("/api/admin/unreleased/fetch-x-post", methods=["POST"])
def admin_fetch_x_post():
    """
    X（Twitter）のポストURLを受け取り、カード情報を抽出してunreleased_cardsに登録する。
    リクエストボディ: {"tweet_url": "https://x.com/YuGiOh_OCG_INFO/status/..."}

    フィルタリングはwatch_x_unreleasedと同じロジック（◤◢チェック・重複チェック・Vision抽出）を適用する。
    ただし管理者が明示的に指定したURLのため、重複チェックはスキップして強制実行するオプションも提供する。
    リクエストボディ: {"tweet_url": "...", "force": false}
      force=true: unreleased_cards 重複チェックをスキップしてVisionを強制実行
    """
    err = _require_admin_key()
    if err:
        return err

    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503

    body = request.get_json(silent=True) or {}
    tweet_url = (body.get("tweet_url") or "").strip()
    force = bool(body.get("force", False))

    if not tweet_url:
        return jsonify({"error": "tweet_url は必須です"}), 400

    # tweet_id を抽出
    m = _TWEET_ID_RE.search(tweet_url)
    if not m:
        return jsonify({"error": "tweet_url から tweet_id を取得できませんでした"}), 400
    tweet_id = m.group(1)

    # X_BEARER_TOKEN の確認
    x_bearer_token = os.environ.get("X_BEARER_TOKEN", "")
    if not x_bearer_token:
        return jsonify({"error": "X_BEARER_TOKEN が未設定です"}), 503

    # X API でツイートを取得
    try:
        import requests as _requests
        resp = _requests.get(
            f"https://api.twitter.com/2/tweets/{tweet_id}",
            headers={"Authorization": f"Bearer {x_bearer_token}"},
            params={
                "expansions": "attachments.media_keys",
                "media.fields": "url",
                "tweet.fields": "text,attachments",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"[admin] X API ツイート取得失敗 id={tweet_id}: {e}")
        return jsonify({"error": f"X API 取得失敗: {e}"}), 502

    tweet_data = data.get("data")
    if not tweet_data:
        return jsonify({"error": "ツイートが見つかりません（削除済み or 非公開の可能性）"}), 404

    tweet_text = tweet_data.get("text", "")

    # media_key → URL のマップ
    media_map: dict[str, str] = {}
    for media in (data.get("includes") or {}).get("media", []):
        key = media.get("media_key", "")
        url = media.get("url", "")
        if key and url:
            media_map[key] = url

    # ◤◢ パターンフィルタ（force=False のときのみチェック、forceでもログは残す）
    from watch_x_unreleased import _CARD_NAME_RE
    names = _CARD_NAME_RE.findall(tweet_text)
    if not names and not force:
        return jsonify({
            "ok": False,
            "skipped": True,
            "reason": "◤◢ パターンが見つかりません。force=true で強制実行できます",
        })

    # 重複チェック（force=False のときのみ）
    if not force and names:
        from name_normalize import fuzzy_key
        existing_resp = _supabase.table("unreleased_cards").select("name").execute()
        existing_keys = {fuzzy_key(r["name"]) for r in (existing_resp.data or []) if r.get("name")}
        if all(fuzzy_key(n) in existing_keys for n in names):
            return jsonify({
                "ok": False,
                "skipped": True,
                "reason": f"全カードが取込済みです: {names}。force=true で強制実行できます",
            })

    # 画像URL取得
    media_keys = (tweet_data.get("attachments") or {}).get("media_keys", [])
    image_urls = [media_map[k] for k in media_keys if k in media_map]
    if not image_urls:
        return jsonify({"error": "ツイートに画像が見つかりません"}), 400

    # Vision 抽出
    try:
        from unreleased_extractor import extract_cards_from_tweet
        card_rows = extract_cards_from_tweet(tweet_text, image_urls, tweet_url)
    except Exception as e:
        logger.error(f"[admin] Vision抽出失敗 {tweet_url}: {e}")
        return jsonify({"error": f"Vision抽出失敗: {e}"}), 500

    if not card_rows:
        return jsonify({"ok": True, "inserted": 0, "cards": [], "reason": "SAMPLEウォーターマークなし（発売済みカードの可能性）"})

    # upsert
    from unreleased_auto_approve import annotate_row, load_active_names, load_text_context
    # 自動承認判定用（シャドーモード: 記録のみ）。rejected以外の既存カード名を1回読む
    active_names = load_active_names(_supabase)
    text_context = load_text_context(_supabase)  # 効果テキスト検証用（他カードの「」内語）
    inserted_cards = []
    for row in card_rows:
        try:
            annotate_row(row, active_names, text_context)
            ins_resp = _supabase.table("unreleased_cards").upsert(
                row,
                on_conflict="name,product_name",
                ignore_duplicates=True,
            ).execute()
            if ins_resp.data:
                inserted_cards.extend(ins_resp.data)
                if active_names is not None:
                    active_names.append(row.get("name", ""))
        except Exception as e:
            logger.warning(f"[admin] fetch-x-post upsert失敗 ({row.get('name', '?')}): {e}")

    if inserted_cards:
        _card_display.invalidate_cache()
        # 取り込み時点で即座に画像をクロップ・保存（管理画面で承認前に確認できるようにする）
        from unreleased_image_store import ingest_x_card_image
        for card in inserted_cards:
            cid = card.get("id")
            raw = card.get("extraction_raw") or {}
            img_url = (raw.get("card_image_url") or "").strip()
            if not img_url:
                img_urls = raw.get("card_image_urls") or []
                img_url = img_urls[0] if img_urls else ""
            if cid and img_url:
                ok, reason = ingest_x_card_image(_supabase, cid, img_url, tweet_url)
                logger.info(f"[admin] fetch-x-post 画像保存: card_id={cid}, ok={ok}, reason={reason!r}")

    # 自動承認（AUTO_APPROVE_ENABLED=1 のときだけ）。画像の保存後に呼ぶ。失敗しても取込の結果は返す
    from unreleased_approve import maybe_auto_approve
    auto = maybe_auto_approve(_supabase, inserted_cards)

    logger.info(f"[admin] fetch-x-post: {len(inserted_cards)}件挿入 ({tweet_url})")
    return jsonify({
        "ok": True,
        "inserted": len(inserted_cards),
        "cards": inserted_cards,
        "auto_approved": auto.get("approved", 0),
    }), 201


@admin_bp.route("/api/admin/unreleased/<int:card_id>/suggest-crop", methods=["POST"])
def admin_suggest_crop(card_id: int):
    """既存の元画像から確認用の枠だけを返す。画像・DBへ保存しない。"""
    err = _require_admin_key()
    if err:
        return err
    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503
    body = request.get_json(silent=True)
    if body not in (None, {}):
        return jsonify({"error": "画像URL等の指定は受け付けません"}), 400
    try:
        cards = _supabase.table("unreleased_cards").select("name").eq("id", card_id).execute().data or []
        if not cards:
            return jsonify({"error": "カードが見つかりません"}), 404
        images = (_supabase.table("official_card_images").select("source_image_url")
                  .eq("unreleased_card_id", card_id).eq("hidden", False)
                  .is_("deleted_at", "null").execute().data or [])
        source_urls = {row.get("source_image_url") for row in images if row.get("source_image_url")}
        if len(source_urls) != 1:
            return jsonify({"error": "元画像を一意に確認できません。元画像の登録を確認してください"}), 422
        from crop_suggestion_service import suggest_crop
        suggestion = suggest_crop(cards[0]["name"], source_urls.pop())
        return jsonify({"ok": True, "crop_suggestion": suggestion})
    except ValueError as exc:
        logger.warning("[admin] 枠候補生成失敗 card_id=%s: %s", card_id, exc)
        return jsonify({"error": str(exc)}), 422
    except Exception:
        logger.exception("[admin] 枠候補生成失敗 card_id=%s", card_id)
        return jsonify({"error": "枠候補の生成に失敗しました。時間を置いて再試行してください"}), 502


# ──────────────────────────────────────────────
# 公開リプレイ（みんなのリプレイ）の管理
# ──────────────────────────────────────────────

_PUBLIC_REPLAY_ADMIN_LIMIT = 100  # 1回に返す件数（仮置き。TODO: calibrate from data）。続きは before で読む
from solitaire_routes import _BEFORE_RE as _PUBLIC_BEFORE_RE  # published_at の続き読み位置の形式（公開一覧と同じ）
import replay_og_image as _replay_og_image


@admin_bp.route("/api/admin/public-replays", methods=["GET"])
def admin_list_public_replays():
    """公開中のリプレイを新しい順に返す（非表示のものも含め hidden の状態を付ける）。
    ?before=<published_at> で続きを読む。?id=<id> は、その1件だけを返す（直接探す用）。
    戻り値: {"items": [...], "next": 続きを読むときの before、無ければ null}"""
    err = _require_admin_key()
    if err:
        return err
    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503
    before = request.args.get("before", "").strip().replace(" ", "+")
    if before and not _PUBLIC_BEFORE_RE.match(before):
        return jsonify({"error": "before が不正です"}), 400
    replay_id = request.args.get("id", "").strip()
    try:
        q = (
            _supabase.table("solitaire_replays")
            .select("id, title, card_names, steps, published_at, hidden")
            .eq("is_public", True)
        )
        if replay_id:
            rows = q.eq("id", replay_id).limit(1).execute().data or []
            return jsonify({"items": rows, "next": None})
        if before:
            q = q.lt("published_at", before)
        # 続きの有無を知るため1件多く読む
        rows = q.order("published_at", desc=True).limit(_PUBLIC_REPLAY_ADMIN_LIMIT + 1).execute().data or []
        has_more = len(rows) > _PUBLIC_REPLAY_ADMIN_LIMIT
        rows = rows[:_PUBLIC_REPLAY_ADMIN_LIMIT]
        return jsonify({"items": rows, "next": rows[-1]["published_at"] if has_more and rows else None})
    except Exception as e:
        logger.error(f"[admin] 公開リプレイ一覧エラー: {e}")
        return jsonify({"error": "公開リプレイの取得に失敗しました"}), 500


@admin_bp.route("/api/admin/public-replays/<replay_id>/toggle-hidden", methods=["POST"])
def admin_toggle_public_replay_hidden(replay_id: str):
    """公開リプレイの hidden を反転する。非表示にすると一覧・直リンク（API・OGメタ情報・共有画像）とも
    「無いもの」として扱われる。戻すと元どおり。行は残る。"""
    err = _require_admin_key()
    if err:
        return err
    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503
    try:
        cur = (
            _supabase.table("solitaire_replays")
            .select("id, hidden")
            .eq("id", replay_id)
            .eq("is_public", True)
            .execute()
        )
        if not cur.data:
            return jsonify({"error": "対象レコードが見つかりません"}), 404
        new_hidden = not cur.data[0]["hidden"]
        (
            _supabase.table("solitaire_replays")
            .update({"hidden": new_hidden})
            .eq("id", replay_id)
            .execute()
        )
        if new_hidden:
            # タイトル入りの共有画像が残って見え続けないよう、キャッシュを消す
            _replay_og_image.purge_cache(replay_id)
        return jsonify({"ok": True, "hidden": new_hidden})
    except Exception as e:
        logger.error(f"[admin] 公開リプレイ非表示切替エラー id={replay_id}: {e}")
        return jsonify({"error": "非表示の切替に失敗しました"}), 500


@admin_bp.route("/api/admin/public-replays/<replay_id>", methods=["DELETE"])
def admin_delete_public_replay(replay_id: str):
    """公開リプレイの行を消す（元に戻せない）。公開中の行だけが対象。"""
    err = _require_admin_key()
    if err:
        return err
    if not _supabase:
        return jsonify({"error": "Supabase 未接続"}), 503
    try:
        resp = (
            _supabase.table("solitaire_replays")
            .delete()
            .eq("id", replay_id)
            .eq("is_public", True)
            .execute()
        )
        if not resp.data:
            return jsonify({"error": "対象レコードが見つかりません"}), 404
        _replay_og_image.purge_cache(replay_id)
        return jsonify({"ok": True, "deleted_id": replay_id})
    except Exception as e:
        logger.error(f"[admin] 公開リプレイ削除エラー id={replay_id}: {e}")
        return jsonify({"error": "削除に失敗しました"}), 500
