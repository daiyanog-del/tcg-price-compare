"""
tests/test_wish_split_baseline.py — 2026-08-19「まとめ買い最安店舗」baselineバグの回帰テスト
（pytestラッパー）

背景:
  templates/index.html の wishCheapestStores() が、分割プランと比較する「1店舗
  まとめ買いの最安値」（baseline）を covered_qty（枚数）の一致だけで選んでいたため、
  中身のカード集合が違う店を比較対象に選んでしまい、誤った金額差を表示していた。
  また比較対象の注文可否も店の全カード合計を流用しており、分割プランが対象にした
  部分集合だけでは最低注文金額に届かない店を見逃していた。

  実際の検証ロジックは Node で static/wish-split.js（分割アルゴリズム本体）をそのまま
  ロードし、index.html のbaseline選定ロジックを複製して動かす
  （tests/js/wish_split_baseline_check.js）。このファイルはそれを subprocess 経由で
  pytest に接続するだけの薄いラッパー。
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK_SCRIPT = Path(__file__).resolve().parent / "js" / "wish_split_baseline_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node が見つからないためスキップ")
def test_wish_split_baseline_scenarios():
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
        "wish-split baselineの回帰テストが失敗しました:\n"
        + result.stdout + "\n" + result.stderr
    )
