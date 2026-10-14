"""
アイコン・OGP画像の生成スクリプト（生成専用・本番未使用）

使い方:
    python tools/build_icons.py          # アイコンだけ再生成
    python tools/build_icons.py --og     # OGP画像だけ再生成

依存ライブラリ:
    pip install Pillow

入力:
    自作画像など/ベース（反転）縦80％.png  ← 黒背景+白線画の元素材
出力（tcg-web/static/ 配下に上書き）:
    favicon-16.png / favicon-32.png
    apple-touch-icon.png (180x180)
    icon-192.png / icon-512.png
    icon-maskable-192.png / icon-maskable-512.png
    tcgym-icon-base.png (1024x1024) / favicon.svg
    tcgym-icon-small.png / tcgym-icon-small.webp (288x352)
    manifest.json / sw.js のアイコンURL（内容ハッシュ付き）
    og-image.png (1200x630、--og 指定時のみ)
"""

from pathlib import Path
import argparse
import base64
import hashlib
import json
import math
import re

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

ROOT   = Path(__file__).resolve().parent.parent.parent
SRC    = ROOT / '自作画像など' / 'ベース（反転）縦80％.png'
STATIC = ROOT / 'tcg-web' / 'static'
HEADER_SRC = ROOT / '自作画像など' / 'ベース（反転）_w_trans_よさそう.png'

# ---- 元画像を紺地+白線に変換 ----
def load_navy_rgba(*, clean_background=False):
    """黒背景+白線画を「白線・透明背景」RGBAに変換して返す"""
    img  = Image.open(SRC).convert('RGB')
    gray = img.convert('L')
    if clean_background:
        # 図柄の無い四隅（短辺の1/20四方）から背景のノイズ上限を実測する。
        w, h = gray.size
        edge = min(w, h) // 20
        corners = [(0, 0, edge, edge), (w - edge, 0, w, edge),
                   (0, h - edge, edge, h), (w - edge, h - edge, w, h)]
        black = max(gray.crop(box).getextrema()[1] for box in corners)
        if black >= 128:
            raise ValueError('元素材の四隅に図柄があります。背景サンプル領域を確認してください')
        gray = gray.point(lambda value: max(0, round((value - black) * 255 / (255 - black))))
    rgba = Image.new('RGBA', img.size, (255, 255, 255, 0))
    rgba.putalpha(gray)
    return rgba

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
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

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
    sw_path.write_text(sw, encoding='utf-8')


def build_favicons():
    rgba = load_navy_rgba(clean_background=True)
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
        make_icon(rgba, size, background=background if opaque else None,
                  maskable=maskable).save(STATIC / name, optimize=True)
        print(f'  {name} ({size}px)')

    encoded = base64.b64encode((STATIC / 'tcgym-icon-base.png').read_bytes()).decode('ascii')
    svg = ('<svg width="1024" height="1024" viewBox="0 0 1024 1024" '
           'xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
           'role="img" aria-label="TCGYM">\n  <title>TCGYM</title>\n'
           f'  <image width="1024" height="1024" xlink:href="data:image/png;base64,{encoded}"/>\n</svg>\n')
    (STATIC / 'favicon.svg').write_text(svg, encoding='utf-8')

    # ヘッダーは従来と同一の透過原画。72 CSS pxでの3倍密度まで原寸未満で表示できる。
    with Image.open(HEADER_SRC) as header:
        small = ImageOps.contain(header.convert('RGBA'), (288, 352), Image.Resampling.LANCZOS)
    small.save(STATIC / 'tcgym-icon-small.png', optimize=True)
    small.save(STATIC / 'tcgym-icon-small.webp', lossless=True, method=6)
    update_icon_references(background)


def build_og_image():
    rgba = load_navy_rgba()
    W, H = 1200, 630
    canvas = Image.new('RGBA', (W, H), (10, 16, 36, 255))
    iw, ih = rgba.size
    TW, TH = 420, 690
    scale = min(TW / iw, TH / ih)
    nw, nh = int(iw * scale), int(ih * scale)
    icon_r = rgba.resize((nw, nh), Image.LANCZOS)
    px = 30 + (TW - nw) // 2
    py = -30 + (TH - nh) // 2
    canvas.alpha_composite(icon_r, (max(0, px), max(0, py)))
    img = canvas.convert('RGB')
    draw = ImageDraw.Draw(img)

    def load_font(names, size):
        for d in [r'C:\Windows\Fonts', '/usr/share/fonts/truetype']:
            for n in names:
                p = Path(d) / n
                if p.exists():
                    try:
                        return ImageFont.truetype(str(p), size)
                    except Exception:
                        pass
        return ImageFont.load_default()

    f_xl  = load_font(['ariblk.ttf', 'arialbd.ttf'], 165)
    f_g   = load_font(['ariblk.ttf', 'arialbd.ttf'], 205)
    f_sub = load_font(['YuGothB.ttc', 'msgothic.ttc', 'meiryo.ttc'], 26)

    tx = 470
    tc_w = int(draw.textlength('TC', font=f_xl))
    g_w  = int(draw.textlength('G',  font=f_g))
    ym_w = int(draw.textlength('YM', font=f_xl))

    # 実際の描画範囲（textbbox）から縦配置を計算する。基準はベースライン（anchor='ls'）。
    # 大きな「G」(205px) は下端がベースライン付近まで伸びるため、固定オフセットで下線・小見出しを
    # 置くと重なる（以前の不具合）。G の bbox 下端・小見出しの bbox 上端から間隔を取る。
    SUB_TEXT = '遊戯王OCG 総合プレイヤーサイト'
    g_box   = draw.textbbox((0, 0), 'G', font=f_g, anchor='ls')   # (l, t, r, b) ベースライン原点
    sub_box = draw.textbbox((0, 0), SUB_TEXT, font=f_sub, anchor='ls')
    GAP_LINE = 30   # G下端 → 下線
    GAP_SUB  = 26   # 下線 → 小見出し上端
    block_h = (g_box[3] - g_box[1]) + GAP_LINE + 1 + GAP_SUB + (sub_box[3] - sub_box[1])
    top = (H - block_h) // 2 - 10          # アイコン(縦中心 約315)に合わせ、ブロックを上下中央寄せ
    base = top - g_box[1]                  # G 上端が top に来るベースライン
    draw.text((tx,              base), 'TC', fill='white',   font=f_xl, anchor='ls')
    draw.text((tx + tc_w,       base), 'G',  fill='#facc15', font=f_g,  anchor='ls')
    draw.text((tx + tc_w + g_w, base), 'YM', fill='white',   font=f_xl, anchor='ls')
    line_y = base + g_box[3] + GAP_LINE
    draw.line([(tx, line_y), (min(W - 30, tx + tc_w + g_w + ym_w + 20), line_y)],
              fill=(148, 163, 184, 100), width=1)
    sub_base = line_y + 1 + GAP_SUB - sub_box[1]
    draw.text((tx + 2, sub_base), SUB_TEXT, fill='#94a3b8', font=f_sub, anchor='ls')
    draw.line([(0, 580), (W, 580)], fill=(250, 204, 21, 25), width=1)

    img.save(str(STATIC / 'og-image.png'))
    print('  og-image.png (1200x630)')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='TCGYMの用途別アイコンを生成する')
    parser.add_argument('--og', action='store_true', help='アイコンは変更せずOGP画像だけ生成する')
    args = parser.parse_args()
    if args.og:
        print('=== OGP画像生成 ===')
        build_og_image()
    else:
        print('=== アイコン生成 ===')
        build_favicons()
    print('完了')
