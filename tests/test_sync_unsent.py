"""
tests/test_sync_unsent.py — 端末同期の送信失敗表示（2026-10-21）の pytest ラッパー

検証の本体は tests/js/sync_unsent_check.js（実物の sync-client.js と index-sync.js を Node で動かす）。
"""

import shutil
import subprocess
from pathlib import Path

import pytest

CHECK_SCRIPT = Path(__file__).resolve().parent / "js" / "sync_unsent_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node が見つからないためスキップ")
def test_sync_unsent_notice():
    result = subprocess.run(["node", str(CHECK_SCRIPT)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_mydeck_tab_has_unsent_notice(monkeypatch):
    """マイデッキタブにも未送信の表示（data-sync-unsent）があり、タブを開くと出し直す（2026-10-21）"""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import app as app_module
    html = app_module.app.test_client().get("/").get_data(as_text=True)
    mydeck = html.split('id="mode-mydeck"', 1)[1].split('id="mode-meta"', 1)[0]
    assert "data-sync-unsent" in mydeck
    main_js = (Path(__file__).resolve().parent.parent / "static" / "js" / "index-main.js").read_text(encoding="utf-8")
    assert "if(mode==='mydeck' && typeof _refreshSyncStatusUI==='function') _refreshSyncStatusUI();" in main_js
