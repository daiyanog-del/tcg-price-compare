"""効果テキストの「食い違っても読み直し側を信じる」設定と、外れたときの自動復帰。

背景（docs/decisions.md 2026-10-15）: 1回目と拡大読み直しが食い違ったカードは従来すべて人の確認に回していたが、
評価と本番の食い違い事例ではどれも読み直し側が正しかったため、条件つきで自動承認の対象にする。
ただし外れた（公開後に効果テキストを直した・利用者から誤り報告が来た）ときは、この設定を自動で無効にして
従来の厳しい条件に戻す。一度無効になったら自動では再有効化しない（人が app_settings を "1" に戻す）。

設定: app_settings のキー TRUST_REREAD_ON_DISAGREE（value が "1"=有効 / "0"=無効）。
- 行が無い（読み取りは成功）→ 既定の有効
- 読めない・取得に失敗・値が想定外 → 無効（安全側）。失敗はキャッシュしない
"""
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

SETTING_KEY = "TRUST_REREAD_ON_DISAGREE"
FLAG = "reread_trusted_on_disagree"   # extraction_raw に残す印

# 設定の読み取り結果を使い回す秒数（取込1バッチで何度も読まないため。復帰の反映が遅れても数十秒）
# TODO: calibrate from data
CACHE_TTL_SEC = 30

# 利用者の報告のうち、読み直し採用の根拠が外れた可能性を示す欄
REPORT_FIELDS = ("effect_text", "other")

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "value": None}
_forced_off = False       # DB に "0" を書けなかったとき、少なくともこのプロセスでは無効にする
_client_override = None   # テスト用
_client_cache = None


def _get_client():
    """app_settings を読み書きする Supabase クライアント。環境変数が無ければ None。"""
    global _client_cache
    if _client_override is not None:
        return _client_override
    if _client_cache is not None:
        return _client_cache
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
    if not (url and key):
        return None
    from supabase import create_client
    _client_cache = create_client(url, key)
    return _client_cache


def invalidate_cache() -> None:
    with _lock:
        _cache.update(at=0.0, value=None)


def parse_value(value) -> bool:
    """app_settings の value を有効/無効にする。"1"・1・True・{"enabled": true} だけが有効。それ以外は無効。"""
    if isinstance(value, dict):
        value = value.get("enabled")
    if isinstance(value, bool):
        return value
    return str(value).strip() == "1" if value is not None else False


def is_enabled(sb=None) -> bool:
    """食い違い時に読み直し側を信じてよいか。読めない・失敗時は False（安全側）。"""
    if _forced_off:
        return False
    now = time.monotonic()
    with _lock:
        if _cache["value"] is not None and now - _cache["at"] < CACHE_TTL_SEC:
            return _cache["value"]
    try:
        sb = sb or _get_client()
        if sb is None:
            logger.warning("[reread_trust] Supabase に接続できないため無効として扱う")
            return False
        rows = sb.table("app_settings").select("value").eq("key", SETTING_KEY).execute().data or []
        value = parse_value(rows[0].get("value")) if rows else True   # 行なし＝既定の有効
    except Exception as e:  # noqa: BLE001 — 読めないときは安全側。キャッシュしない
        logger.warning("[reread_trust] 設定を読めないため無効として扱う: %s", e)
        return False
    with _lock:
        _cache.update(at=time.monotonic(), value=value)
    return value


def is_trusted_card(raw) -> bool:
    return isinstance(raw, dict) and raw.get(FLAG) is True


def _notify_async(message: str) -> None:
    """Discord 通知を別スレッドで送る（外部 HTTP で管理画面・報告の応答を待たせない）。まとめ送信はしない（重要通知のため）。"""
    threading.Thread(target=_notify, args=(message,), daemon=True, name="reread-trust-notify").start()


def _notify(message: str) -> None:
    try:
        from discord_notify import send_discord_message
        send_discord_message(message, no_mentions=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("[reread_trust] Discord通知失敗: %s", e)


def disable(sb, card_name: str, reason: str) -> bool:
    """設定を "0" にして Discord に通知する。すでに無効なら何もしない（通知の重複を避ける）。

    DB に書けなかったときも通知は出す（人に知らせるのが最優先）。戻り値: 今回新たに無効にした/試みたら True。
    """
    global _forced_off
    invalidate_cache()
    try:
        sb = sb or _get_client()
        if sb is None:
            raise RuntimeError("Supabase に接続できません")
        rows = sb.table("app_settings").select("value").eq("key", SETTING_KEY).execute().data or []
        if rows and not parse_value(rows[0].get("value")):
            return False   # すでに無効
        sb.table("app_settings").upsert({"key": SETTING_KEY, "value": "0"}, on_conflict="key").execute()
        written = True
    except Exception as e:  # noqa: BLE001
        logger.error("[reread_trust] 設定の無効化に失敗（このプロセスでは無効にする）: %s", e)
        _forced_off = True
        written = False
    invalidate_cache()
    status = ("「食い違っても読み直しを信じる」設定を自動で無効にしました（従来の厳しい条件に戻ります）。"
              if written else "設定の無効化に失敗しました。app_settings の TRUST_REREAD_ON_DISAGREE を手動で \"0\" にしてください。")
    _notify_async(f"[読み直し採用] {status}\nカード: {card_name}\n理由: {reason}\n"
            f"再び有効にするには app_settings の {SETTING_KEY} を \"1\" にします（docs/decisions.md 参照）。")
    return True


def on_effect_text_edited(sb, card_id, card_name: str, saved_raw, new_text) -> bool:
    """管理画面の編集の保存後。読み直し採用のカードで、新しい effect_text が信じた読み直し文と（norm() 後に）違えば無効にする。

    saved_raw は保存後の行の extraction_raw（信じた読み直し文は effect_text_reread.text）。
    """
    if not is_trusted_card(saved_raw):
        return False
    from effect_text_reread import norm
    info = saved_raw.get("effect_text_reread")
    trusted_text = info.get("text") if isinstance(info, dict) else None
    if norm(trusted_text or "") == norm(new_text or ""):
        return False
    return disable(sb, card_name, f"読み直し採用で通したカード（id={card_id}）の効果テキストが、信じた読み直し文から変わる編集をされました")


def on_withdrawn(sb, card_id, card_name: str, status, raw, action: str) -> bool:
    """公開中（approved/linked）の読み直し採用カードを非公開・却下・削除したとき。外れたとみなして無効にする。"""
    if status not in ("approved", "linked") or not is_trusted_card(raw):
        return False
    return disable(sb, card_name, f"読み直し採用で通した公開中のカード（id={card_id}）が管理画面で{action}されました")


def on_report(sb, card_id, card_name: str, field: str) -> bool:
    """利用者の誤り報告が届いたとき。効果テキスト（または「その他」）の報告で、読み直し採用のカードなら無効にする。"""
    if field not in REPORT_FIELDS:
        return False
    rows = sb.table("unreleased_cards").select("extraction_raw").eq("id", card_id).execute().data or []
    if not rows or not is_trusted_card(rows[0].get("extraction_raw")):
        return False
    return disable(sb, card_name, f"読み直し採用で通したカード（id={card_id}）に利用者から誤り報告（欄: {field}）が届きました")
