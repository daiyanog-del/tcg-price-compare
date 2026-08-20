"""
tests/test_xss_escaping.py — 2026-08-20 公開前修正 第2バッチ「escAttr() の導入と属性文脈の
置換」の回帰テスト（pytestラッパー）

背景:
  esc()（テキストノード用）と escJs()（JS文字列用）はどちらも " をエスケープしないため、
  HTML属性値（href=/src=/id=/data-*=等）にそのまま使うと属性の外へ脱出できる。escAttr()
  を新設し、属性文脈の呼び出しを置換した（onclick等の二重文脈は escAttr(escJs(x)) の
  重ね掛けに統一）。あわせて safeUrl()（http/httpsのみ許可）未適用のhref/src箇所も是正した。

  実際の検証ロジックは Node で templates/index.html の実際の <script> 中身をそのまま
  抽出して動かす（tests/js/xss_escaping_check.js）。このファイルはそれを subprocess
  経由で pytest に接続するだけの薄いラッパー。
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK_SCRIPT = Path(__file__).resolve().parent / "js" / "xss_escaping_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node が見つからないためスキップ")
def test_xss_escaping_scenarios():
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
        "XSS属性エスケープの回帰テストが失敗しました:\n"
        + result.stdout + "\n" + result.stderr
    )
