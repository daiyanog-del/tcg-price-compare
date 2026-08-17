"""
tests/test_sync_link_client.py — P3（ワンタイムリンク）の sync-client.js 回帰テスト（pytestラッパー）

実際の検証ロジックは Node で static/shared/sync-client.js のソースをそのまま
ロードして動かす（tests/js/sync_link_client_check.js）。P2の
tests/test_sync_client_data_loss.py と同じ構成（別実装で再検証すると
「テストは通るが実物は直っていない」状態を見逃すため、必ず本物のソースを使う）。
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK_SCRIPT = Path(__file__).resolve().parent / "js" / "sync_link_client_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node が見つからないためスキップ")
def test_sync_link_client_scenarios():
    # チェックスクリプトは日本語のOK/FAILメッセージを出力する。Windowsのコンソール既定
    # エンコーディング（cp932）では読めない文字が混じるため、node/pytest双方でUTF-8を明示する
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        ["node", str(CHECK_SCRIPT)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, (
        "sync-client.js のP3(ワンタイムリンク)回帰テストが失敗しました:\n"
        + result.stdout + "\n" + result.stderr
    )
