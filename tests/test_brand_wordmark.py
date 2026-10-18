"""英語と日本語を組み合わせた固定ロゴの表示と素材の寸法を検証する。"""
from pathlib import Path
import xml.etree.ElementTree as ET
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]


def test_brand_wordmark_is_fixed():
    env = Environment(loader=FileSystemLoader(ROOT / 'templates'), autoescape=True)
    # 旧実装のランダム選択が残っていたら、このテストを失敗させる。
    def reject_random(items):
        raise AssertionError('ブランドロゴは固定表示する')
    env.filters['random'] = reject_random
    html = env.from_string(
        '{% from "_brand_wordmark.html" import brand_wordmark with context %}'
        '{{ brand_wordmark("brand-wordmark") }}'
    ).render(static_url=lambda path: '/static/' + path + '?v=test')
    image = ET.fromstring(html.strip()[:-1] + "/>")
    assert image.attrib['src'] == '/static/brand/wordmark-lockup.svg?v=test'
    assert image.attrib['alt'] == 'GREEDY LAB — 強欲なラボ'
    assert image.attrib['class'] == 'brand-wordmark'
    assert (image.attrib['width'], image.attrib['height']) == ('1000', '218')
    asset = ET.parse(ROOT / 'static/brand/wordmark-lockup.svg').getroot()
    assert asset.attrib['viewBox'] == '0 0 1000 218'
