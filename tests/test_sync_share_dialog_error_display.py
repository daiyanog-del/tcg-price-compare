"""
tests/test_sync_share_dialog_error_display.py — 2026-08-17 本番不具合#2の回帰テスト（index.html側）

「他の端末でも見る」ダイアログ・「同期を解除」を押して失敗しても画面に一切表示されない
問題の回帰テスト。Flask test_client で実際にレンダリングした templates/index.html の
HTMLから、P3で追加した自己完結ブロック（sync-client.js 以外に依存しない部分）を抽出し、
Node上のフェイクDOMで実際に関数を呼んで検証する（tests/js/sync_share_dialog_error_display_check.js）。
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK_SCRIPT = Path(__file__).resolve().parent / "js" / "sync_share_dialog_error_display_check.js"

sys.path.insert(0, str(REPO_ROOT))
import app as app_module  # noqa: E402


@pytest.mark.skipif(shutil.which("node") is None, reason="node が見つからないためスキップ")
def test_index_page_shows_error_message_on_link_action_failure(tmp_path):
    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    html = client.get("/").get_data(as_text=True)
    html_path = tmp_path / "index_rendered.html"
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
        "index.html の端末間同期ダイアログの失敗時表示の回帰テストが失敗しました:\n"
        + result.stdout + "\n" + result.stderr
    )
