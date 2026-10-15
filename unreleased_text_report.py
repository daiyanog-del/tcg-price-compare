"""未発売カードの「テキストの誤り報告」（ユーザーからの報告の受付・保存・通知・管理画面用の集計）。

2026-10-15 追加。自動承認の開始に先立ち、読み取りミスが公開されたままになるのを利用者の目でも拾うための窓口。
  - 受付: POST /api/card-text-report（app.py は検証済みのIPとリクエスト本文を渡すだけ）
  - 保存: テーブル unreleased_text_reports（unreleased_auto_approve_go.sql）。生のIPは保存せず、ソルト付きハッシュだけ
  - 通知: Discord（メンションは無効化・本文は切り詰め）
  - 報告だけでカードを非公開にはしない（管理者が見て判断する）

Flask に依存しない。Supabase クライアントは引数で受け取る。
"""

import hashlib
import logging
import os
import threading
from datetime import datetime, timezone

from name_normalize import fuzzy_key

logger = logging.getLogger(__name__)

TABLE = "unreleased_text_reports"

# 報告できる欄（キー → 管理画面などの表示名）。フロント（static/shared/text-report.js）の選択肢と揃える
FIELD_LABELS = {
    "effect_text": "効果テキスト",
    "pendulum_effect": "ペンデュラム効果",
    "card_name": "カード名",
    "stats": "種族・属性・数値",
    "other": "その他",
}

# ひとことの上限（文字）。ユーザー決定: 500字
NOTE_MAX = 500
# カード名の上限（文字）。未発売カード名の実測最長より十分長い値で、異常な入力だけを弾く
NAME_MAX = 200

# Discord に載せるひとことの最大文字数（通知は短く。全文は管理画面で見る）
_DISCORD_NOTE_MAX = 300

# 公開中（サイトのカード詳細に出る）とみなす status。card_display の未発売取得条件と同じ
_PUBLIC_STATUSES = ("approved", "linked")

_warned_salt = False


def _clean_text(value: str) -> str:
    """制御文字（改行・タブは除く）を落とす。保存・通知に紛れ込ませない。"""
    return "".join(ch for ch in value if ch in "\n\t" or ch >= " " and ch != "\x7f")


def validate_report(body) -> tuple[dict | None, str | None]:
    """リクエスト本文を検証して正規化する。(正規化済み, エラー文)。

    note は任意。上限を超えたら黙って切らずにエラーにする（利用者に書き直してもらう）。
    """
    if not isinstance(body, dict):
        return None, "入力が不正です"
    name = body.get("name")
    field = body.get("field")
    note = body.get("note", "")
    if not isinstance(name, str) or not name.strip():
        return None, "カード名が必要です"
    name = _clean_text(name).strip()
    if not name or len(name) > NAME_MAX:
        return None, "カード名が不正です"
    if not isinstance(field, str) or field not in FIELD_LABELS:
        return None, "誤りのある欄を選んでください"
    if note is None:
        note = ""
    if not isinstance(note, str):
        return None, "ひとことは文字列で指定してください"
    note = _clean_text(note).strip()
    if len(note) > NOTE_MAX:
        return None, f"ひとことは{NOTE_MAX}文字以内にしてください"
    return {"name": name, "field": field, "note": note}, None


def _ip_salt() -> str:
    """IPハッシュのソルト。専用の環境変数が理想。無ければ既存の秘密値から派生させる（生IPの復元を防ぐため）。"""
    global _warned_salt
    for key in ("TEXT_REPORT_IP_SALT", "ADMIN_KEY", "SUPABASE_KEY"):
        v = os.environ.get(key, "").strip()
        if v:
            return key + ":" + v
    if not _warned_salt:
        _warned_salt = True
        logger.warning("[text_report] TEXT_REPORT_IP_SALT も ADMIN_KEY も SUPABASE_KEY も未設定。"
                       "IPハッシュのソルトが固定値になる（推測されやすい）。環境変数 TEXT_REPORT_IP_SALT を設定すること")
    return "fixed:tcgym-text-report"


def hash_ip(ip: str) -> str:
    """生のIPを保存しないためのソルト付きハッシュ（SHA-256の16進）。"""
    return hashlib.sha256((_ip_salt() + "|" + (ip or "unknown")).encode("utf-8")).hexdigest()


def find_public_card(sb, name: str) -> dict | None:
    """公開中（approved / linked かつ hidden=false）の未発売カードを名前で探す。完全一致を優先し、無ければ fuzzy_key で照合。"""
    resp = (sb.table("unreleased_cards").select("id, name")
            .eq("name", name).in_("status", list(_PUBLIC_STATUSES)).eq("hidden", False).execute())
    if resp.data:
        return resp.data[0]
    key = fuzzy_key(name)
    if not key:
        return None
    # 記号・全半角のゆれを吸収した再照合。公開中の名前だけを読む（数百件規模）
    rows, offset = [], 0
    while True:
        batch = (sb.table("unreleased_cards").select("id, name")
                 .in_("status", list(_PUBLIC_STATUSES)).eq("hidden", False)
                 .order("id").range(offset, offset + 999).execute().data or [])
        rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    for r in rows:
        if fuzzy_key(r.get("name") or "") == key:
            return r
    return None


def build_discord_message(card_name: str, field: str, note: str, card_id, base_url: str = "") -> str:
    """Discord 通知の本文。ひとことは切り詰める（全文は管理画面）。"""
    label = FIELD_LABELS.get(field, field)
    shown = note if len(note) <= _DISCORD_NOTE_MAX else note[:_DISCORD_NOTE_MAX] + "…"
    lines = [f"[未発売テキスト誤り報告] {card_name}（card_id={card_id}）",
             f"欄: {label}",
             f"ひとこと: {shown or '（なし）'}"]
    if base_url:
        lines.append(f"管理画面: {base_url}/admin")
    return "\n".join(lines)


def _notify_async(message: str) -> None:
    """Discord 通知をバックグラウンドで送る（外部HTTPの遅延で応答を待たせない）。メンションは無効。"""
    def run():
        try:
            from discord_notify import send_discord_message
            send_discord_message(message, no_mentions=True)
        except Exception as e:
            logger.warning("[text_report] Discord通知失敗: %s", e)
    threading.Thread(target=run, daemon=True, name="text-report-notify").start()


def process_report(sb, body, ip: str, *, notify=None, base_url: str = "") -> tuple[dict, int]:
    """報告を検証して保存・通知する。(レスポンスJSON, HTTPステータス)。例外は呼び出し側で500にする。

    公開中の未発売カード以外（存在しない名前・非公開・発売済み）への報告は404。報告があってもカードの公開状態は変えない。
    """
    clean, err = validate_report(body)
    if err:
        return {"error": err}, 400
    card = find_public_card(sb, clean["name"])
    if not card:
        return {"error": "対象のカードが見つかりません"}, 404
    sb.table(TABLE).insert({
        "unreleased_card_id": card["id"],
        "card_name": card["name"],
        "field": clean["field"],
        "note": clean["note"],
        "ip_hash": hash_ip(ip),
        "status": "open",
    }).execute()
    (notify or _notify_async)(build_discord_message(card["name"], clean["field"], clean["note"], card["id"], base_url))
    return {"ok": True}, 200


# ──────────────────────────────────────────────
# 管理画面用
# ──────────────────────────────────────────────

def count_open_by_card(sb) -> dict:
    """未対応（open）の報告件数を card_id ごとに返す。テーブルが無い（SQL未適用）などで読めなければ空。"""
    counts: dict = {}
    try:
        offset = 0
        while True:
            rows = (sb.table(TABLE).select("unreleased_card_id").eq("status", "open")
                    .order("id").range(offset, offset + 999).execute().data or [])
            for r in rows:
                cid = r.get("unreleased_card_id")
                if cid is not None:
                    counts[cid] = counts.get(cid, 0) + 1
            if len(rows) < 1000:
                return counts
            offset += 1000
    except Exception as e:
        logger.warning("[text_report] 未対応の報告件数を読めない（unreleased_auto_approve_go.sql 未適用の可能性）: %s", e)
        return {}


def list_reports(sb, card_id: int) -> list[dict]:
    """カード1枚の報告一覧（新しい順）。ip_hash は返さない。"""
    rows = (sb.table(TABLE).select("id, unreleased_card_id, card_name, field, note, created_at, status, resolved_at")
            .eq("unreleased_card_id", card_id).order("id", desc=True).execute().data or [])
    for r in rows:
        r.pop("ip_hash", None)  # select に含めていないが、念のため管理画面には絶対に出さない
        r["field_label"] = FIELD_LABELS.get(r.get("field"), r.get("field"))
    return rows


def resolve_report(sb, report_id: int) -> dict | None:
    """報告を対応済みにする。対象が無ければ None。"""
    resp = (sb.table(TABLE).update({"status": "resolved", "resolved_at": datetime.now(timezone.utc).isoformat()})
            .eq("id", report_id).execute())
    if not resp.data:
        return None
    row = dict(resp.data[0])
    row.pop("ip_hash", None)  # 管理画面にも出さない
    return row
