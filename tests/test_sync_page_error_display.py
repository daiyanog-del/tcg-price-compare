"""
tests/test_sync_page_error_display.py — 2026-08-17 本番不具合#2の回帰テスト（/sync ページ側）

背景:
  「同期する」ボタンを押して429（レート制限）等で失敗しても、画面に一切表示されず
  「ボタンが壊れている」ようにしか見えないバグが本番で見つかった。
  関数単体（sync-client.js の戻り値）が正しいだけでは検出できない種類のバグだったため、
  Flask test_client で実際にレンダリングした templates/sync.html のHTMLをそのまま
  Node上のフェイクDOMに流し込み、「ボタンを押す→429が返る→画面に理由が表示され、
  ボタンが再度押せる状態に戻る」という実際の操作フローを検証する
  （tests/js/sync_page_error_display_check.js）。
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK_SCRIPT = Path(__file__).resolve().parent / "js" / "sync_page_error_display_check.js"

sys.path.insert(0, str(REPO_ROOT))
import app as app_module  # noqa: E402


@pytest.mark.skipif(shutil.which("node") is None, reason="node が見つからないためスキップ")
def test_sync_page_shows_error_message_on_redeem_failure(tmp_path):
    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    html = client.get("/sync?t=tok-error-display-test").get_data(as_text=True)
    html_path = tmp_path / "sync_rendered.html"
    html_path.write_text(html, encoding="utf-8")

    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        ["node", str(CHECK_SCRIPT), str(html_path)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, (
        "/sync ページの失敗時表示の回帰テストが失敗しました:\n"
        + result.stdout + "\n" + result.stderr
    )
