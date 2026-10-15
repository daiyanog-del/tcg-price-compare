"""
discord_notify.py — Discord Webhook 通知の汎用ヘルパー

Watcher 等のジョブから運営向け通知を送る。DISCORD_WEBHOOK_URL 未設定なら何もしない。

このモジュールを分離している理由:
  watch_unreleased.py / unreleased_extractor.py は「公式サイトへのHTTP取得は
  fetch_guard.fetch_whitelisted() 経由のみ」という構造保証を tests/test_fetch_guard.py の
  AST検査（HTTPライブラリの直接import禁止）で担保している。
  Discord への通知（運営側で設定した信頼済みWebhook URLへの一方向POST）はこの制約の
  対象外だが、同一ファイル内にHTTPライブラリを import するとAST検査が成立しなくなる。
  そのため通知だけを本モジュールに隔離する。
"""

import os
import logging

import requests

logger = logging.getLogger(__name__)


def send_discord_message(message: str, *, no_mentions: bool = False) -> None:
    """Discord Webhook へテキスト通知を送る（DISCORD_WEBHOOK_URL 未設定なら何もしない）

    no_mentions=True のとき allowed_mentions を空にし、本文に @everyone や @ユーザー が含まれても
    通知（メンション）を飛ばさない。利用者の入力やカード名など、外部由来の文字列を載せる通知で使う。
    既定（False）は従来どおりで、既存の通知の挙動は変えない。
    """
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        return
    payload = {"content": message[:1900]}  # Discordの2000文字制限に余裕を持たせる
    if no_mentions:
        payload["allowed_mentions"] = {"parse": []}
    try:
        resp = requests.post(
            webhook_url,
            json=payload,
            timeout=10,
        )
        logger.info(f"[discord_notify] 通知送信: status={resp.status_code}")
    except Exception as e:
        logger.warning(f"[discord_notify] 通知失敗: {e}")
