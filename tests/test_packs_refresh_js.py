"""新商品タブ（packs.js）の判定部分（このパックの版に絞る・店舗×レアの表・価格の欄）の回帰検証。"""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which('node') is None, reason='node が見つかりません')
def test_packs_refresh_js():
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        ['node', str(root / 'tests/js/packs_refresh_check.js')],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
