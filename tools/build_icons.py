"""
アイコン・OGP画像の生成スクリプト（生成専用・本番未使用）

使い方:
    python tcg-web/tools/build_icons.py

依存ライブラリ:
    pip install cairosvg Pillow

入力:
    自作画像など/ベース（反転）縦80％.png  ← 黒背景+白線画の元素材
出力（tcg-web/static/ 配下に上書き）:
    favicon-16.png / favicon-32.png
    apple-touch-icon.png (180x180)
    icon-192.png / icon-512.png
    tcgym-icon-base.png (128x128, favicon.svg 用)
    og-image.png (1200x630)
"""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT   = Path(__file__).resolve().parent.parent.parent
SRC    = ROOT / '自作画像など' / 'ベース（反転）縦80％.png'
STATIC = ROOT / 'tcg-web' / 'static'

# ---- 元画像を紺地+白線に変換 ----
def load_navy_rgba():
    """黒背景+白線画を「白線・透明背景」RGBAに変換して返す"""
    img  = Image.open(SRC).convert('RGB')
    gray = img.convert('L')
    rgba = Image.new('RGBA', img.size, (255, 255, 255, 0))
    rgba.putalpha(gray)
    return rgba

def make_icon(rgba_img, size):
    navy = Image.new('RGBA', rgba_img.size, (10, 16, 36, 255))
    navy.alpha_composite(rgba_img)
    return navy.convert('RGB').resize((size, size), Image.LANCZOS)


def build_favicons():
    rgba = load_navy_rgba()
    sizes = {
        'favicon-16.png':        16,
        'favicon-32.png':        32,
        'apple-touch-icon.png': 180,
        'icon-192.png':         192,
        'icon-512.png':         512,
        'tcgym-icon-base.png':  128,
    }
    for name, size in sizes.items():
        make_icon(rgba, size).save(str(STATIC / name))
        print(f'  {name} ({size}px)')


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

    tx, ty = 470, 130
    tc_w = int(draw.textlength('TC', font=f_xl))
    g_w  = int(draw.textlength('G',  font=f_g))
    ym_w = int(draw.textlength('YM', font=f_xl))
    draw.text((tx,              ty + 25), 'TC', fill='white',   font=f_xl)
    draw.text((tx + tc_w,       ty),      'G',  fill='#facc15', font=f_g)
    draw.text((tx + tc_w + g_w, ty + 25), 'YM', fill='white',   font=f_xl)
    draw.line([(tx, ty + 205), (min(W - 30, tx + tc_w + g_w + ym_w + 20), ty + 205)],
              fill=(148, 163, 184, 100), width=1)
    draw.text((tx + 2, ty + 218), '遊戯王OCG 総合プレイヤーサイト', fill='#94a3b8', font=f_sub)
    draw.line([(0, 580), (W, 580)], fill=(250, 204, 21, 25), width=1)

    img.save(str(STATIC / 'og-image.png'))
    print('  og-image.png (1200x630)')


if __name__ == '__main__':
    print('=== ファビコン生成 ===')
    build_favicons()
    print('=== OGP画像生成 ===')
    build_og_image()
    print('完了')
