"""店舗検索の混雑応答（2026-10-22）の画面側の JS 検証を pytest から走らせる。"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which('node') is None, reason='node が見つかりません')
def test_混雑応答の自動再試行と表示():
    result = subprocess.run(['node', str(ROOT / 'tests/js/shop_busy_check.js')],
                            cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
