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
