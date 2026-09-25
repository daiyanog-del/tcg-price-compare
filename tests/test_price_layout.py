"""価格画面のナビ接続と、表示状態の回帰検証。"""
import shutil
import subprocess
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

import app as app_module

ROOT = Path(__file__).resolve().parent.parent


def test_navigation_targets_and_unique_ids():
    html = app_module.app.test_client().get('/').get_data(as_text=True)
    soup = BeautifulSoup(html, 'html.parser')
    tabs = soup.select('.mode-tab')
    assert {b.get('data-mode') for b in tabs} == {'search', 'meta', 'mydeck', 'packs', 'wishlist'}
    for button in tabs:
        mode = button['data-mode']
        assert f"switchMode('{mode}'" in button['onclick']
        assert soup.find(id=f'mode-{mode}') is not None
    for element_id in ['wishTabBadge', 'q', 'btn', 'results', 'cardHero', 'heroInfo', 'buyInline',
                       'topMoversList', 'buybackMoversList', 'ofMoversList']:
        assert len(soup.select(f'#{element_id}')) == 1


@pytest.mark.skipif(shutil.which('node') is None, reason='node が見つかりません')
def test_price_layout_state_transitions():
    result = subprocess.run(['node', str(ROOT / 'tests/js/price_layout_check.js')],
                            cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
