"""日本語2表記の選択と、固定サイズ・素材の存在を検証する。"""
from pathlib import Path
import xml.etree.ElementTree as ET
import pytest
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('choice,filename,label', [
    (0, 'wordmark-kanji.svg', '強欲なラボ'),
    (1, 'wordmark-A.svg', 'ゴーヨクなラボ'),
])
def test_japanese_wordmark_variants(choice, filename, label):
    env = Environment(loader=FileSystemLoader(ROOT / 'templates'), autoescape=True)
    selected = []
    def choose(items):
        selected.append(items)
        return items[choice]
    env.filters['random'] = choose
    html = env.from_string(
        '{% from "_brand_wordmark.html" import japanese_wordmark with context %}'
        '{{ japanese_wordmark("brand-wordmark") }}'
    ).render(static_url=lambda path: '/static/' + path + '?v=test')
    image = ET.fromstring(html.strip()[:-1] + "/>" )
    assert image.attrib['src'] == '/static/brand/' + filename + '?v=test'
    assert image.attrib['alt'] == label
    assert (image.attrib['width'], image.attrib['height']) == ('1000', '190')
    assert len(selected) == 1 and len(selected[0]) == 2
    asset = ET.parse(ROOT / 'static/brand' / filename).getroot()
    assert asset.attrib['viewBox'] == '0 0 1000 190'
