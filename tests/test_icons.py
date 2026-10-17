"""アイコンの透明度・安全領域・参照先とキャッシュ更新を検証する。"""

import base64
import hashlib
import io
import json
import math
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image, ImageChops

from tools import build_icons


STATIC = Path(__file__).resolve().parents[1] / 'static'


@pytest.mark.skipif(shutil.which('node') is None, reason='node が見つかりません')
def test_service_worker_preserves_card_cache():
    result = subprocess.run(['node', str(STATIC.parent / 'tests/js/icon_cache_check.js')],
                            cwd=STATIC.parent, capture_output=True, text=True,
                            encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('name,size,transparent', [
    ('favicon-16.png', 16, True), ('favicon-32.png', 32, True),
    ('icon-192.png', 192, True), ('icon-512.png', 512, True),
    ('apple-touch-icon.png', 180, False), ('icon-maskable-192.png', 192, False),
    ('icon-maskable-512.png', 512, False), ('tcgym-icon-base.png', 1024, True),
])
def test_icon_size_and_background(name, size, transparent):
    with Image.open(STATIC / name) as icon:
        assert icon.size == (size, size)
        if transparent:
            assert icon.mode == 'RGBA'
            alpha = icon.getchannel('A')
            assert alpha.getextrema() == (0, 255)
            assert all(alpha.getpixel(pos) == 0 for pos in
                       [(0, 0), (size - 1, 0), (0, size - 1), (size - 1, size - 1)])
        else:
            assert icon.mode == 'RGB'
            background = Image.new('RGB', (1, 1), build_icons.theme_background()).getpixel((0, 0))
            assert icon.getpixel((0, 0)) == background


@pytest.mark.parametrize('size', [192, 512])
def test_maskable_artwork_is_inside_safe_circle(size):
    with Image.open(STATIC / f'icon-maskable-{size}.png') as icon:
        background = Image.new('RGB', icon.size, build_icons.theme_background())
        difference = ImageChops.difference(icon, background).convert('L')
        for y in range(size):
            for x in range(size):
                if math.hypot(x + 0.5 - size / 2, y + 0.5 - size / 2) > size * 0.4:
                    assert difference.getpixel((x, y)) <= 1, (x, y)


def test_non_square_artwork_keeps_aspect_ratio():
    source = Image.new('RGBA', (200, 100), 'white')
    icon = build_icons.make_icon(source, 192)
    # アンチエイリアスの境界を中間輝度で測り、輪郭の濃色を図柄に含めない。
    white = icon.convert('RGB').convert('L').point(lambda value: 255 if value > 128 else 0)
    left, top, right, bottom = white.getbbox()
    assert abs((right - left) / (bottom - top) - 2) < 0.03


def test_svg_embeds_high_resolution_transparent_artwork():
    svg = (STATIC / 'favicon.svg').read_text(encoding='utf-8')
    data = base64.b64decode(re.search(r'base64,([^"\s]+)', svg).group(1))
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (128, 128)
        assert image.mode == 'RGBA'


def test_manifest_and_service_worker_reference_current_content():
    for name in ('favicon.svg', 'manifest.json', 'sw.js'):
        assert b'\r' not in (STATIC / name).read_bytes(), '本番Linuxと同じLF改行であること'
    manifest = json.loads((STATIC / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['theme_color'] == manifest['background_color'] == build_icons.theme_background()
    assert {(icon['purpose'], icon['sizes']) for icon in manifest['icons']} == {
        (purpose, f'{size}x{size}') for purpose in ('any', 'maskable') for size in (192, 512)
    }
    sw = (STATIC / 'sw.js').read_text(encoding='utf-8')
    assets = json.loads(re.search(r'const ICON_ASSETS = (\{.*?\});', sw, re.S).group(1))
    for url in [icon['src'] for icon in manifest['icons']] + list(assets.values()):
        parsed = urlsplit(url)
        path = STATIC / Path(parsed.path).name
        digest = hashlib.md5(path.read_bytes()).hexdigest()[:12]
        assert parse_qs(parsed.query) == {'v': [digest]}


def test_header_has_enough_pixels_for_high_density_displays():
    with Image.open(STATIC / 'tcgym-icon-small.png') as png, Image.open(STATIC / 'tcgym-icon-small.webp') as webp:
        assert png.height >= 72 * 3
        assert abs(png.width / png.height - 1) < 0.01
        assert png.size == webp.size
        for color in ('white', 'black'):
            background = Image.new('RGBA', png.size, color)
            png_view = Image.alpha_composite(background, png).convert('RGB')
            webp_view = Image.alpha_composite(background, webp.convert('RGBA')).convert('RGB')
            assert ImageChops.difference(png_view, webp_view).getbbox() is None


def test_error_and_disabled_pages_use_content_hash_urls():
    import app as app_module
    from flask import render_template

    with app_module.app.test_request_context('/'):
        expected = app_module.static_url('favicon.svg')
        for template in ('error.html', 'solo_disabled.html'):
            html = render_template(template, title='確認', message='確認')
            assert expected in html
            assert 'favicon.svg?v=3"' not in html


def test_new_icons_receive_existing_cache_policy():
    import app as app_module

    client = app_module.app.test_client()
    for name in ('icon-maskable-192.png', 'icon-maskable-512.png'):
        assert client.get('/static/' + name).headers['Cache-Control'] == 'public, max-age=86400'
        assert client.get(build_icons.asset_url(name)).headers['Cache-Control'] == 'public, max-age=31536000, immutable'
