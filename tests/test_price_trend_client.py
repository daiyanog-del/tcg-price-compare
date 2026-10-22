"""価格推移の常設グラフの画面側JavaScript回帰検証（集計・表示切替・XSS・古い応答の破棄・配線）。"""
import shutil
import subprocess
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

import app as app_module

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which('node') is None, reason='node が見つかりません')
def test_price_trend_client():
    result = subprocess.run(['node', str(ROOT / 'tests/js/price_trend_check.js')],
                            cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_テンプレートの配線():
    html = app_module.app.test_client().get('/').get_data(as_text=True)
    soup = BeautifulSoup(html, 'html.parser')
    box = soup.select('#priceTrend')
    assert len(box) == 1 and 'hidden' in box[0].get('class', [])
    # 名前の右の相場の箱の先頭。レアリティ欄・出品の一覧より前（2026-10-21 案C）
    assert html.index('id="heroMarket"') < html.index('id="priceTrend"') < html.index('id="buyInline"') < html.index('id="rarityFilter"') < html.index('id="crList"')
    srcs = [s.get('src', '') for s in soup.find_all('script')]
    assert any('js/price-trend.js' in s for s in srcs)
    # インラインscriptは追加していない（CSP）
    assert 'PriceTrend' not in ''.join(s.string or '' for s in soup.find_all('script') if not s.get('src'))


def test_index_mainの連動配線():
    js = (ROOT / 'static/js/index-main.js').read_text(encoding='utf-8')
    assert "'heroMarket','buyInline','priceTrend','rarityFilter','crList'" in js  # 未発売カードで隠す対象
    assert 'PriceTrend.load(_currentCardName)' in js
    assert 'PriceTrend.clear()' in js
    assert js.count('_syncPriceTrendView()') >= 3  # 定義 + setRarityFilter + done


def test_グラフのCSSが新仕様():
    """目盛り・吹き出し用のCSSがあり、金色を使わず、Jinjaコメント('{#')を含まない。"""
    src = (ROOT / 'templates/index.html').read_text(encoding='utf-8')
    a = src.index('.price-trend{')
    css = src[a:src.index('.price-chart>summary', a)]
    for sel in ('.pt-plot{', '.pt-grid{', '.pt-axr{', '.pt-tip{', '.pt-cursor{', '.pt-dot{'):
        assert sel in css, sel
    assert 'height:96px' in css and 'var(--gold)' not in css and '{#' not in css
    assert '.pt-axis' not in css  # 下部の始点・終点日付は廃止
