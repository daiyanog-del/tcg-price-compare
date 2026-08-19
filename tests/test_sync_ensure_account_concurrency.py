"""
tests/test_sync_ensure_account_concurrency.py — 2026-08-17 二重init不具合の回帰テスト（pytestラッパー）

背景:
  static/shared/sync-client.js の _ensureAccount() が同時呼び出しに対して冪等でなかった。
  購入候補の追加とデッキの保存が2秒以内に起きると、種別ごとのデバウンスタイマー
  （onWishSave用・onDecksSave用）がほぼ同時に発火し、どちらも「sync_id無し」と判断して
  _ensureAccount() を独立に呼ぶため、/api/sync/init が2回叩かれていた（内容は同じだが、
  本番の sync_accounts に0.2秒差の孤児行が2回とも再現した）。

  実際の検証ロジックは Node で static/shared/sync-client.js のソースをそのまま
  ロードして動かす（tests/js/sync_ensure_account_concurrency_check.js）。別実装で
  再検証すると「テストは通るが実物は直っていない」状態を見逃すため、必ず本物のソースを使う。
  このファイルはそれを subprocess 経由で pytest に接続するだけの薄いラッパー。
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK_SCRIPT = Path(__file__).resolve().parent / "js" / "sync_ensure_account_concurrency_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node が見つからないためスキップ")
def test_sync_ensure_account_concurrency_scenarios():
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
        "sync-client.js の _ensureAccount() 同時呼び出し回帰テストが失敗しました:\n"
        + result.stdout + "\n" + result.stderr
    )
