"""日本に来たTCG先行カードの名前の書き換え（static/shared/tcg-rename.js・2026-10-23 段3）の画面側の検証。

- tests/js/tcg_rename_check.js を node で流す（実コードを読み込んで書き換え・合流・保存経路を確かめる）
- トップとシミュレーターの両方で、同期の部品（sync-client.js）の後に読み込まれていること
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which('node') is None, reason='node が見つかりません')
def test_書き換えの実コード():
    result = subprocess.run(['node', str(ROOT / 'tests/js/tcg_rename_check.js')],
                            capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == 'ok'  # 途中に書き換えのログ（console.info）が出る


@pytest.mark.parametrize('template', ['templates/index.html', 'templates/solitaire.html'])
def test_同期の部品の後に読み込む(template):
    html = (ROOT / template).read_text(encoding='utf-8')
    sync = html.find("shared/sync-client.js")
    rename = html.find("shared/tcg-rename.js")
    assert sync >= 0 and rename > sync
    # 読み込み方（defer の有無）は同じページの sync-client.js に揃える
    tag = lambda name: re.search(r'<script[^>]*' + re.escape(name) + r'[^>]*>', html).group(0)  # noqa: E731
    assert ('defer' in tag('shared/sync-client.js')) == ('defer' in tag('shared/tcg-rename.js'))
