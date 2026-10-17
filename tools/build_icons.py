"""
アイコン・OGP画像の生成スクリプト（生成専用・本番未使用）

使い方:
    python tools/build_icons.py          # アイコンだけ再生成
    python tools/build_icons.py --og     # OGP画像だけ再生成

依存ライブラリ:
    pip install Pillow

入力:
    static/brand/symbol-source.png / wordmark-A.png / favicon-source.png（確定素材）
出力（tcg-web/static/ 配下に上書き）:
    favicon-16.png / favicon-32.png
    apple-touch-icon.png (180x180)
    icon-192.png / icon-512.png
    icon-maskable-192.png / icon-maskable-512.png
    tcgym-icon-base.png (1024x1024) / favicon.svg
    tcgym-icon-small.png / tcgym-icon-small.webp (256x256)
    manifest.json / sw.js のアイコンURL（内容ハッシュ付き）
    og-image.png (1200x630、--og 指定時のみ)
"""

from pathlib import Path
import argparse
import base64
import hashlib
import io
import json
import math
import re

from PIL import Image, ImageFilter, ImageOps

APP_ROOT = Path(__file__).resolve().parent.parent
# CIや別名のチェックアウトでも、生成スクリプトが属するリポジトリを参照する。
STATIC = APP_ROOT / 'static'

# ---- 確定ブランド素材の読込 ----
def load_navy_rgba(*, clean_background=False):
    """確定したCシンボルを透過RGBAで返す（引数は既存呼出し互換）。"""
    return Image.open(STATIC / 'brand/symbol-source.png').convert('RGBA')


def theme_background():
    """共通CSSの背景色を正とし、アイコンと起動画面の色ずれを防ぐ。"""
    css = (STATIC / 'css/site-theme.css').read_text(encoding='utf-8')
    match = re.search(r'--bg:\s*(#[0-9a-fA-F]{6})\s*;', css)
    if not match:
        raise ValueError('site-theme.css の --bg が見つかりません')
    return match.group(1)


def artwork_radius(rgba_img):
    """図柄の最外周から中心までの最大距離を求める。"""
    alpha = rgba_img.getchannel('A')
    cx, cy = rgba_img.width / 2, rgba_img.height / 2
    radius = 0.0
    for y in range(rgba_img.height):
        bounds = alpha.crop((0, y, rgba_img.width, y + 1)).getbbox()
        if bounds:
            radius = max(radius, *(math.hypot(x - cx, edge_y - cy)
                                   for x in (bounds[0], bounds[2]) for edge_y in (y, y + 1)))
    if not radius:
        raise ValueError('元素材に不透明な図柄がありません')
    return radius


def make_icon(rgba_img, size, *, background=None, maskable=False):
    """原画の比率を保ち、透過版または全面背景版を生成する。"""
    # 小サイズは4倍で合成。大サイズは原画のアンチエイリアスを使う。
    scale = 4 if size <= 192 else 1
    side = size * scale
    # 通常版は上下各1/16を余白にする。maskableは直径80%の円に収める。
    limit = side * 7 / 8
    if maskable:
        # 正方形の余白で過剰に縮小せず、実際の図柄を半径40%以内に配置する。
        # 再サンプリングの縁と丸め誤差には、完成画像の2pxを確保する。
        ratio = (side * 0.4 - 2 * scale) / artwork_radius(rgba_img)
        limit = min(side - 2 * scale, max(rgba_img.size) * ratio)
    fitted = ImageOps.contain(rgba_img, (int(limit), int(limit)), Image.Resampling.LANCZOS)
    layer = Image.new('RGBA', (side, side))
    layer.alpha_composite(fitted, ((side - fitted.width) // 2, (side - fitted.height) // 2))
    if background is None:
        # 白背景で縮小表示しても輪郭が残るよう、幅の1/64（最小0.5px）の濃色線を付ける。
        # 16/32pxも原画の全要素を維持し、簡略化はここでは行わない。
        radius = max(scale // 2, round(side / 64))
        outline = Image.new('RGBA', layer.size, theme_background())
        alpha = layer.getchannel('A')
        # 小窓の繰り返しは同じ最大値膨張になり、高解像度での大窓処理を避けられる。
        for _ in range(radius):
            alpha = alpha.filter(ImageFilter.MaxFilter(3))
        outline.putalpha(alpha)
        outline.alpha_composite(layer)
        return outline.resize((size, size), Image.Resampling.LANCZOS)
    canvas = Image.new('RGBA', layer.size, background)
    canvas.alpha_composite(layer)
    return canvas.convert('RGB').resize((size, size), Image.Resampling.LANCZOS)


def asset_url(name):
    """app.static_url と同じ内容ハッシュを、静的な参照先にも付ける。"""
    digest = hashlib.md5((STATIC / name).read_bytes()).hexdigest()[:12]
    return f'/static/{name}?v={digest}'


def update_icon_references(background):
    manifest_path = STATIC / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['background_color'] = manifest['theme_color'] = background
    manifest['icons'] = [
        {'src': asset_url(f'icon{suffix}-{size}.png'), 'sizes': f'{size}x{size}',
         'type': 'image/png', 'purpose': purpose}
        for suffix, purpose in [('', 'any'), ('-maskable', 'maskable')]
        for size in (192, 512)
    ]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
                             encoding='utf-8', newline='\n')

    names = ['favicon.svg', 'favicon-16.png', 'favicon-32.png', 'apple-touch-icon.png',
             'icon-192.png', 'icon-512.png', 'icon-maskable-192.png', 'icon-maskable-512.png']
    block = '// BEGIN GENERATED ICON URLS\nconst ICON_ASSETS = ' + json.dumps(
        {name: asset_url(name) for name in names}, indent=2
    ) + ';\n// END GENERATED ICON URLS'
    sw_path = STATIC / 'sw.js'
    sw, count = re.subn(r'// BEGIN GENERATED ICON URLS\n.*?// END GENERATED ICON URLS',
                        lambda _: block, sw_path.read_text(encoding='utf-8'), flags=re.S)
    if count != 1:
        raise ValueError('sw.js のアイコンURL生成範囲が見つかりません')
    sw_path.write_text(sw, encoding='utf-8', newline='\n')


def build_favicons():
    rgba = load_navy_rgba(clean_background=True)
    favicon = Image.open(STATIC / 'brand/favicon-source.png').convert('RGBA')
    background = theme_background()
    for name, size, opaque, maskable in [
        ('favicon-16.png', 16, False, False),
        ('favicon-32.png', 32, False, False),
        ('apple-touch-icon.png', 180, True, False),
        ('icon-192.png', 192, False, False),
        ('icon-512.png', 512, False, False),
        ('icon-maskable-192.png', 192, True, True),
        ('icon-maskable-512.png', 512, True, True),
        ('tcgym-icon-base.png', 1024, False, False),
    ]:
        if name.startswith('favicon-'):
            # 白いタブ背景でも白抜き図柄が消えないよう既存の濃色縁を付ける。
            icon = make_icon(favicon, size)
        else:
            icon = make_icon(rgba, size, background=background if opaque else None,
                             maskable=maskable)
        icon.save(STATIC / name, optimize=True)
        print(f'  {name} ({size}px)')

    # タブ向けSVGも簡略版に統一。128pxで高密度表示をカバーする。
    buffer = io.BytesIO()
    make_icon(favicon, 128).save(buffer, format='PNG', optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode('ascii')
    svg = ('<svg width="1024" height="1024" viewBox="0 0 1024 1024" '
           'xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
           'role="img" aria-label="強欲なラボ">\n  <title>強欲なラボ</title>\n'
           f'  <image width="1024" height="1024" xlink:href="data:image/png;base64,{encoded}"/>\n</svg>\n')
    (STATIC / 'favicon.svg').write_text(svg, encoding='utf-8', newline='\n')

    # 高密度画面でも承認サイズ64pxを鮮明に表示する。
    small = rgba.resize((256, 256), Image.Resampling.LANCZOS)
    small.save(STATIC / 'tcgym-icon-small.png', optimize=True)
    small.save(STATIC / 'tcgym-icon-small.webp', lossless=True, method=6)
    small.save(STATIC / 'brand/symbol-C.png', optimize=True)
    update_icon_references(background)


def build_og_image():
    # 字形は確定版の画像を使い、実行環境のフォントに依存させない。
    symbol = load_navy_rgba()
    word = Image.open(STATIC / 'brand/wordmark-A.png').convert('RGBA')
    logo = Image.new('RGBA', (1024, 200))
    logo.alpha_composite(symbol.resize((200, 200), Image.Resampling.LANCZOS), (0, 0))
    fitted = ImageOps.contain(word, (800, 200), Image.Resampling.LANCZOS)
    logo.alpha_composite(fitted, (224, (200 - fitted.height) // 2 + 30))
    logo.save(STATIC / 'og-logo.png', optimize=True)
    # 自動ポストではカード名を主役にするため、英語ロゴを小さくまとめる。
    english = Image.open(STATIC / 'brand/wordmark-en.png').convert('RGBA')
    post_logo = Image.new('RGBA', (880, 200))
    post_logo.alpha_composite(symbol.resize((200, 200), Image.Resampling.LANCZOS), (0, 0))
    fitted_en = ImageOps.contain(english, (640, 110), Image.Resampling.LANCZOS)
    post_logo.alpha_composite(fitted_en, (224, (200 - fitted_en.height) // 2 + 30))
    post_logo.save(STATIC / 'og-post-logo.png', optimize=True)
    canvas = Image.new('RGBA', (1200, 630), theme_background())
    canvas.alpha_composite(symbol.resize((400, 400), Image.Resampling.LANCZOS), (60, 115))
    fitted = ImageOps.contain(word, (650, 300), Image.Resampling.LANCZOS)
    canvas.alpha_composite(fitted, (490, (630 - fitted.height) // 2 + 60))
    canvas.convert('RGB').save(STATIC / 'og-image.png', optimize=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='強欲なラボの用途別アイコンを生成する')
    parser.add_argument('--og', action='store_true', help='アイコンは変更せずOGP画像だけ生成する')
    args = parser.parse_args()
    if args.og:
        print('=== OGP画像生成 ===')
        build_og_image()
    else:
        print('=== アイコン生成 ===')
        build_favicons()
    print('完了')
