"""
監視・通知モジュール
====================
- 各店舗スクレイパーのヘルスチェック
- エラー追跡（連続失敗カウント）
- Discord Webhook 通知
"""

import json
import time
import os
import logging
from datetime import datetime, timedelta
from threading import Lock

import requests

logger = logging.getLogger(__name__)

# ── 設定 ──

# Discord Webhook URL（環境変数で設定）
# Render の Environment Variables に DISCORD_WEBHOOK_URL を追加してください
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# 通知条件
ALERT_AFTER_CONSECUTIVE_FAILURES = 2   # 連続N回失敗で通知
ALERT_COOLDOWN_MINUTES = 60            # 同じ店舗のアラートは60分に1回まで

# ヘルスチェック用テストカード
HEALTH_CHECK_CARD = "ブラック・マジシャン"
HEALTH_CHECK_MIN_RESULTS = 1           # 最低1件取得できればOK


# ── エラー追跡 ──

class ErrorTracker:
    """各店舗のエラー状態を追跡"""

    def __init__(self):
        self._lock = Lock()
        self._state: dict[str, dict] = {}
        # state[shop] = {
        #   "consecutive_failures": int,
        #   "last_error": str,
        #   "last_error_time": datetime,
        #   "last_success_time": datetime,
        #   "last_alert_time": datetime | None,
        #   "total_errors": int,
        #   "total_successes": int,
        # }

    def record_success(self, shop: str, result_count: int):
        with self._lock:
            s = self._get_or_create(shop)
            was_failing = s["consecutive_failures"] >= ALERT_AFTER_CONSECUTIVE_FAILURES
            s["consecutive_failures"] = 0
            s["last_success_time"] = datetime.now()
            s["total_successes"] += 1

            # 復旧通知
            if was_failing:
                _send_recovery_alert(shop, result_count)

    def record_failure(self, shop: str, error: str):
        with self._lock:
            s = self._get_or_create(shop)
            s["consecutive_failures"] += 1
            s["last_error"] = error
            s["last_error_time"] = datetime.now()
            s["total_errors"] += 1

            # アラート判定
            if s["consecutive_failures"] >= ALERT_AFTER_CONSECUTIVE_FAILURES:
                if self._should_alert(s):
                    s["last_alert_time"] = datetime.now()
                    _send_error_alert(shop, error, s["consecutive_failures"])

    def get_status(self) -> dict:
        """全店舗の状態を返す（ヘルスチェックAPI用）"""
        with self._lock:
            result = {}
            for shop, s in self._state.items():
                result[shop] = {
                    "status": "ok" if s["consecutive_failures"] == 0 else "error",
                    "consecutive_failures": s["consecutive_failures"],
                    "last_error": s["last_error"],
                    "last_error_time": s["last_error_time"].isoformat() if s["last_error_time"] else None,
                    "last_success_time": s["last_success_time"].isoformat() if s["last_success_time"] else None,
                    "total_errors": s["total_errors"],
                    "total_successes": s["total_successes"],
                }
            return result

    def _get_or_create(self, shop: str) -> dict:
        if shop not in self._state:
            self._state[shop] = {
                "consecutive_failures": 0,
                "last_error": "",
                "last_error_time": None,
                "last_success_time": None,
                "last_alert_time": None,
                "total_errors": 0,
                "total_successes": 0,
            }
        return self._state[shop]

    def _should_alert(self, s: dict) -> bool:
        if not s["last_alert_time"]:
            return True
        return datetime.now() - s["last_alert_time"] > timedelta(minutes=ALERT_COOLDOWN_MINUTES)


# グローバルインスタンス
tracker = ErrorTracker()


# ── Discord 通知 ──

def _send_discord(content: str, color: int = 0xFF5252):
    """Discord Webhook にメッセージを送信"""
    if not DISCORD_WEBHOOK_URL:
        logger.warning("DISCORD_WEBHOOK_URL が未設定のため通知をスキップ")
        return

    payload = {
        "embeds": [{
            "title": "🎴 TCG価格比較 — 監視アラート",
            "description": content,
            "color": color,
            "timestamp": datetime.utcnow().isoformat(),
        }]
    }

    try:
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            json=payload,
            timeout=10,
        )
        if resp.status_code != 204:
            logger.error(f"Discord通知失敗: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"Discord通知エラー: {e}")


def _send_error_alert(shop: str, error: str, count: int):
    """エラーアラートを送信"""
    content = (
        f"🔴 **{shop}** でエラーが **{count}回連続** で発生しています\n\n"
        f"```\n{error[:500]}\n```\n\n"
        f"サイトの構造が変更された可能性があります。\n"
        f"`scraper.py` のセレクタ確認が必要です。"
    )
    _send_discord(content, color=0xFF5252)
    logger.error(f"ALERT: {shop} - {count}回連続失敗 - {error[:200]}")


def _send_recovery_alert(shop: str, result_count: int):
    """復旧通知を送信"""
    content = (
        f"🟢 **{shop}** が復旧しました（{result_count}件取得）"
    )
    _send_discord(content, color=0x00E676)
    logger.info(f"RECOVERY: {shop} - {result_count}件取得で復旧")


# ── ヘルスチェック ──

def run_health_check() -> dict:
    """全店舗のスクレイパーをテスト実行してステータスを返す"""
    from scraper import SHOPS, WAIT_SEC

    results = {}
    overall_ok = True

    for shop_name, scraper in SHOPS:
        start = time.time()
        try:
            items = scraper(HEALTH_CHECK_CARD)
            elapsed = round(time.time() - start, 2)

            if len(items) >= HEALTH_CHECK_MIN_RESULTS:
                results[shop_name] = {
                    "status": "ok",
                    "count": len(items),
                    "time_sec": elapsed,
                    "sample": items[0]["name"] if items else "",
                }
                tracker.record_success(shop_name, len(items))
            else:
                msg = f"結果が{len(items)}件（最低{HEALTH_CHECK_MIN_RESULTS}件必要）"
                results[shop_name] = {
                    "status": "warning",
                    "count": len(items),
                    "time_sec": elapsed,
                    "message": msg,
                }
                tracker.record_failure(shop_name, msg)
                overall_ok = False

        except Exception as e:
            elapsed = round(time.time() - start, 2)
            error_msg = str(e)[:300]
            results[shop_name] = {
                "status": "error",
                "time_sec": elapsed,
                "error": error_msg,
            }
            tracker.record_failure(shop_name, error_msg)
            overall_ok = False

        time.sleep(WAIT_SEC)

    return {
        "status": "ok" if overall_ok else "degraded",
        "timestamp": datetime.now().isoformat(),
        "checks": results,
        "tracker": tracker.get_status(),
    }
