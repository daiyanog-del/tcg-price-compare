"""新着カード画面のJavaScript回帰検証。"""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which('node') is None, reason='node が見つかりません')
def test_new_cards_client():
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        ['node', str(root / 'tests/js/new_cards_check.js')],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
