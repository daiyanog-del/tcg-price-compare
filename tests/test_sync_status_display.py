"""
tests/test_sync_status_display.py — 2026-08-18 実機バグの回帰テスト（pytestラッパー）

背景:
  一度も他の端末と同期していないPCで「他の端末と同期中」と表示されるバグが実機で
  見つかった。templates/index.html の _refreshSyncStatusUI() が sync_id の有無だけで
  判定していたが、sync_id は購入候補・デッキを1件でも作れば自動発行される
  （設計文書 §8 遅延発行）ため、連携済みかどうかの判定材料にならない。

  実際の検証ロジックは Node で実際にレンダリングされた templates/index.html の
  該当ブロックをそのまま実行して動かす（tests/js/sync_status_display_check.js）。
  tests/test_sync_share_dialog_error_display.py と同じ方式。
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK_SCRIPT = Path(__file__).resolve().parent / "js" / "sync_status_display_check.js"

sys.path.insert(0, str(REPO_ROOT))
import app as app_module  # noqa: E402


@pytest.mark.skipif(shutil.which("node") is None, reason="node が見つからないためスキップ")
def test_sync_status_hidden_unless_actually_linked(tmp_path):
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
        "「他の端末と同期中」表示の回帰テストが失敗しました:\n"
        + result.stdout + "\n" + result.stderr
    )
