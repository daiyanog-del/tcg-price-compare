"""メルカリ参考相場の画面側JavaScript回帰検証（配線・表示・XSS・古い応答の破棄）。"""
import shutil
import subprocess
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

import app as app_module

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which('node') is None, reason='node が見つかりません')
def test_mercari_ref_client():
    result = subprocess.run(['node', str(ROOT / 'tests/js/mercari_ref_check.js')],
                            cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which('node') is None, reason='node が見つかりません')
def test_mercari_parallel_wiring():
    """検索SSEと並行してメルカリを取得する配線（実物の doSearch を偽SSEで動かす）。"""
    result = subprocess.run(['node', str(ROOT / 'tests/js/mercari_parallel_check.js')],
                            cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_テンプレートの配線():
    html = app_module.app.test_client().get('/').get_data(as_text=True)
    soup = BeautifulSoup(html, 'html.parser')
    box = soup.select('#mercariRef')
    assert len(box) == 1 and 'hidden' in box[0].get('class', [])
    # 店舗の表(#sumCards)・買取(#buyInline)より後ろに置く＝店舗価格が先に見える
    assert html.index('id="sumCards"') < html.index('id="buyInline"') < html.index('id="mercariRef"')
    srcs = [s.get('src', '') for s in soup.find_all('script')]
    assert any('js/mercari-ref.js' in s for s in srcs)
    # インラインscriptは追加していない（CSP）
    assert 'MercariRef' not in ''.join(s.string or '' for s in soup.find_all('script') if not s.get('src'))


def test_パネルを開いたらGAイベントを送る配線():
    js = (ROOT / 'static/js/index-main.js').read_text(encoding='utf-8')
    i = js.index("document.getElementById('mercariRef')?.addEventListener('toggle'")
    block = js[i:i + 200]
    assert "if(!this.open) return;" in block and "_ga('mercari_inline_open'" in block
