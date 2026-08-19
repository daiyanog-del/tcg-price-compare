"""
tests/test_wish_estimate.py — 2026-08-19「購入候補の見積もり」照合キー不一致バグの回帰テスト
（pytestラッパー）

背景:
  calcWishEstimate() が localStorage の生データ（list）をサーバの正規化済み entries と
  突合せずに使っていたため、保存値がサーバの補正・正規化形と食い違う場合（例:
  rarity:"ウルトラレア" の旧表記、カード名補正）にDBに価格があるのに「データなし」扱いで
  総額から丸ごと落ちていた。wishCheapestStores() で先に直した問題と同根の原因。

  実際の検証ロジックは Node で static/wish-estimate.js（照合ロジック本体）をそのまま
  ロードして動かす（tests/js/wish_estimate_check.js）。このファイルはそれを subprocess
  経由で pytest に接続するだけの薄いラッパー。
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK_SCRIPT = Path(__file__).resolve().parent / "js" / "wish_estimate_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node が見つからないためスキップ")
def test_wish_estimate_scenarios():
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
        "wish-estimateの回帰テストが失敗しました:\n"
        + result.stdout + "\n" + result.stderr
    )
