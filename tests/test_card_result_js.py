"""カードの検索結果（2026-10-21 案C）の JS 検証を pytest から走らせる。"""
import shutil
import subprocess
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

import app as app_module

ROOT = Path(__file__).resolve().parent.parent


def _node(script):
    result = subprocess.run(['node', str(ROOT / 'tests/js' / script)],
                            cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which('node') is None, reason='node が見つかりません')
def test_検索結果の一覧とレアリティ欄():
    _node('card_result_check.js')


@pytest.mark.skipif(shutil.which('node') is None, reason='node が見つかりません')
def test_アフィリエイトの印と関係():
    _node('affiliate_label_check.js')


def test_テンプレートの並びと部品():
    html = app_module.app.test_client().get('/').get_data(as_text=True)
    soup = BeautifulSoup(html, 'html.parser')
    # 名前（と右の相場の箱）→ レアリティ欄 → 出品の一覧
    assert html.index('id="cardHero"') < html.index('id="heroMarket"') < html.index('id="rarityFilter"') < html.index('id="crList"')
    seg = soup.select('#crList .cr-seg-btn')
    assert [b.get('data-v') for b in seg] == ['shop', 'all']
    assert 'active' in seg[0].get('class', []) and seg[0].get('aria-pressed') == 'true'  # 既定は店ごとの最安
    assert 'hidden' in soup.select_one('#soToggle').get('class', [])
    # 廃止した部品が残っていない
    for gone in ('sumCards', 'rfBanner', 'rgrid', 'p-all', 'p-rarity', 'tblBuyBtn'):
        assert soup.find(id=gone) is None, gone
    assert any('css/card-result.css' in (l.get('href') or '') for l in soup.find_all('link'))
    # 買取は相場の箱の外・名前の行の下（全幅・既定で閉じる。2026-10-22 案A）
    assert soup.select_one('#heroMarket #buyInline') is None
    buy = soup.select_one('#buyInline')
    assert buy is not None and not buy.has_attr('open')
    assert html.index('id="heroMarket"') < html.index('id="buyInline"') < html.index('id="rarityFilter"')


def test_PCの効果テキストの配線():
    """PC は効果テキストを最初から開き（計測には数えない）、届いたら高さの上限を当てる（2026-10-22 案3）。"""
    js = (ROOT / 'static/js/index-main.js').read_text(encoding='utf-8')
    html = (ROOT / 'templates/index.html').read_text(encoding='utf-8')
    assert "detailOpen?' open data-auto-open=\"1\"':''" in js
    i = js.index('function _fetchCardInfo(')
    assert '_showCardDetail();\n      _clampCardDetail();' in js[i:i + 4000]
    assert 'function _clampCardDetail()' in js and "btn.textContent='続きを読む'" in js
    assert "if(e.target.dataset.autoOpen){ delete e.target.dataset.autoOpen; return; }" in html
