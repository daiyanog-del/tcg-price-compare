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
import time
from datetime import datetime, timedelta, timezone

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

# 乱用対策（IP を変えても1ホストから大量に送られても、DB と Discord が埋まらないようにする）
# 同じ IP（ハッシュ）が同じカードに送れるのは1日1件まで
PER_IP_CARD_WINDOW = timedelta(days=1)
# 全体の受付は1時間あたりこの件数まで。超えたら保存せず 429
# TODO: calibrate from data
REPORT_HOURLY_LIMIT = 30
# Discord 通知は最短この間隔に1通。間の分は件数にまとめて、窓の終わりに1通送る
# TODO: calibrate from data
NOTIFY_WINDOW_SEC = 300
# 公開中カード名の読み込み結果を使い回す秒数（存在しない名前の照合のたびに全件を読まないため）
# TODO: calibrate from data
PUBLIC_NAMES_TTL_SEC = 60

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
    # 記号・全半角のゆれを吸収した再照合。公開中の名前は短時間キャッシュする
    for r in _public_names(sb):
        if fuzzy_key(r.get("name") or "") == key:
            return r
    return None


_names_cache: dict = {"at": 0.0, "sb": None, "rows": []}
_names_lock = threading.Lock()


def _public_names(sb, *, clock=time.monotonic) -> list[dict]:
    """公開中カードの [{id, name}]。PUBLIC_NAMES_TTL_SEC 秒キャッシュする（存在しない名前を連打されても全件読みは1回）。"""
    with _names_lock:
        now = clock()
        if _names_cache["sb"] is sb and now - _names_cache["at"] < PUBLIC_NAMES_TTL_SEC:
            return _names_cache["rows"]
        rows, offset = [], 0
        while True:
            batch = (sb.table("unreleased_cards").select("id, name")
                     .in_("status", list(_PUBLIC_STATUSES)).eq("hidden", False)
                     .order("id").range(offset, offset + 999).execute().data or [])
            rows.extend(batch)
            if len(batch) < 1000:
                break
            offset += 1000
        _names_cache.update(at=now, sb=sb, rows=rows)
        return rows


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


def _send_discord(message: str) -> None:
    try:
        from discord_notify import send_discord_message
        send_discord_message(message, no_mentions=True)
    except Exception as e:
        logger.warning("[text_report] Discord通知失敗: %s", e)


class NotifyBatcher:
    """Discord 通知を最短 window 秒に1通にまとめる。

    窓が開いていないときの報告は即送信して窓を開く。窓の間の報告は件数だけ数え、窓の終わりに
    「直近N分に M件」を1通送る（その後、また窓を開く）。同じ process の中だけの制御。
    send / timer_factory は差し替えられる（テスト用）。外部HTTPで応答を待たせないよう送信は別スレッド。
    """

    def __init__(self, send=_send_discord, window: float | None = None, timer_factory=threading.Timer):
        self.send = send
        self.window = NOTIFY_WINDOW_SEC if window is None else window
        self.timer_factory = timer_factory
        self.lock = threading.Lock()
        self.open = False
        self.suppressed: list[str] = []

    def _send_async(self, message: str) -> None:
        threading.Thread(target=self.send, args=(message,), daemon=True, name="text-report-notify").start()

    def _arm(self) -> None:
        """窓を開いて終わりのタイマーを起動する。起動に失敗したら窓を閉じる（閉じ忘れると以後ずっと通知されない）。"""
        self.open = True
        try:
            t = self.timer_factory(self.window, self._flush)
            t.daemon = True
            t.start()
        except Exception as e:
            self.open = False
            logger.warning("[text_report] 通知のまとめタイマーを起動できず窓を閉じた: %s", e)

    def submit(self, message: str, label: str = "") -> bool:
        """報告を1件通知する。即送信したら True、まとめに回したら False。"""
        with self.lock:
            if self.open:
                self.suppressed.append(label)
                return False
            self._arm()
        self._send_async(message)
        return True

    def _flush(self) -> None:
        with self.lock:
            labels, self.suppressed = self.suppressed, []
            if not labels:
                self.open = False
                return
            self._arm()   # まとめを送った後も次の窓を開く（連続しても1窓1通）
        shown = "、".join(dict.fromkeys(x for x in labels if x))[:300]
        self._send_async(f"[未発売テキスト誤り報告] 直近{int(self.window // 60)}分にさらに{len(labels)}件の報告がありました"
                         f"（{shown}）。詳細は管理画面で確認してください")


_batcher = NotifyBatcher()


def _notify_async(message: str, label: str = "") -> None:
    """Discord 通知（短時間に複数来たらまとめる）。メンションは無効。"""
    _batcher.submit(message, label)


def over_limits(sb, card_id, ip_hash: str, *, now: datetime | None = None) -> str | None:
    """乱用対策の上限に当たっていれば理由コード（"duplicate" / "hourly"）、なければ None。保存前に呼ぶ。"""
    now = now or datetime.now(timezone.utc)
    since_day = (now - PER_IP_CARD_WINDOW).isoformat()
    dup = (sb.table(TABLE).select("id").eq("ip_hash", ip_hash).eq("unreleased_card_id", card_id)
           .gte("created_at", since_day).limit(1).execute().data)
    if dup:
        return "duplicate"
    since_hour = (now - timedelta(hours=1)).isoformat()
    recent = (sb.table(TABLE).select("id").gte("created_at", since_hour).limit(REPORT_HOURLY_LIMIT).execute().data or [])
    if len(recent) >= REPORT_HOURLY_LIMIT:
        return "hourly"
    return None


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
    ip_hash = hash_ip(ip)
    limited = over_limits(sb, card["id"], ip_hash)
    if limited == "duplicate":
        return {"error": "このカードへの報告は受け付け済みです。明日以降にまたお知らせください"}, 429
    if limited == "hourly":
        return {"error": "現在報告が集中しています。しばらく待ってからもう一度お試しください"}, 429
    sb.table(TABLE).insert({
        "unreleased_card_id": card["id"],
        "card_name": card["name"],
        "field": clean["field"],
        "note": clean["note"],
        "ip_hash": ip_hash,
        "status": "open",
        "created_at": datetime.now(timezone.utc).isoformat(),   # 上限の集計と同じ時計で明示する
    }).execute()
    # 読み直し採用（食い違っても読み直し側を信じて通した）カードへの効果テキスト報告は、設定を自動で無効にする
    try:
        import reread_trust
        reread_trust.on_report(sb, card["id"], card["name"], clean["field"])
    except Exception as e:  # 報告の受付は成立させる
        logger.warning("[text_report] 読み直し採用の自動復帰処理に失敗: %s", e)
    message = build_discord_message(card["name"], clean["field"], clean["note"], card["id"], base_url)
    if notify is not None:
        notify(message)
    else:
        _notify_async(message, card["name"])
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
