"""
tests/test_sync_client_data_loss.py — 2026-08-13 本番データ消失バグの回帰テスト（pytestラッパー）

背景:
  static/shared/sync-client.js の _ensureAccount(kind, items) が、きっかけになった種別
  （購入候補 or 保存デッキ）のリストしか /api/sync/init に送らず、もう一方をローカルで
  rev=0 のまま初期化していた。次の pull でローカルrev=0とサーバーrev（未送信のため空）が
  不一致となり、サーバーの空リストが「新しいデータ」として wishSave([])/savedDecksSet([])
  で上書き適用され、ローカルにしか無かったデータが消えるバグが本番で再現した。

  実際の検証ロジックは Node で static/shared/sync-client.js のソースをそのまま
  ロードして動かす（tests/js/sync_client_data_loss_check.js）。別実装で再検証すると
  「テストは通るが実物は直っていない」状態を見逃すため、必ず本物のソースを使う。
  このファイルはそれを subprocess 経由で pytest に接続するだけの薄いラッパー。
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK_SCRIPT = Path(__file__).resolve().parent / "js" / "sync_client_data_loss_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node が見つからないためスキップ")
def test_sync_client_data_loss_scenarios():
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
        "sync-client.js のデータ消失回帰テストが失敗しました:\n"
        + result.stdout + "\n" + result.stderr
    )
