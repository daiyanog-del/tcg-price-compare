"""検索中のバー（2026-10-28 空きの側の光・終盤の文言）の JS 検証を pytest から走らせる。"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which('node') is None, reason='node が見つかりません')
def test_検索中のバーの光と終盤の文言():
    result = subprocess.run(['node', str(ROOT / 'tests/js/search_progress_check.js')],
                            cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
