"""
ロゴの「強欲なラボ」（GREEDY LAB の下の日本語）を、指定したフォントの字形に入れ替える（生成専用・本番未使用）

使い方:
    python tools/build_brand_subtitle.py --font <フォント.ttf>
    python tools/build_icons.py --og      # 続けて OGP 画像（og-logo / og-post-logo / og-image）を作り直す

入力:
    static/brand/wordmark-lockup.svg・logo-lockup.svg（同じ輪郭の path を使い回している）
    --font：字形を取るフォント。リポジトリには置かない（2026-10-30 は yugioh-shorts の試作フォント ProtoReisho-PR12.ttf。
            カード名の字形を写した試作で、フォントファイルは配布しない。ロゴには輪郭だけを書き出す）
出力（上書き）:
    static/brand/wordmark-lockup.svg / .png（2000x436）・logo-lockup.svg / .png（1500x500）
    PNG は SVG を cairosvg で描いたもの（入れ替え前の PNG もこの方法で描いたものと画素単位で一致することを確かめた）

入れ替え方:
    元の輪郭を部品（M〜Z）に分け、「GREEDY LAB」の下端より下にある部品を日本語の部分とみなして外す。
    外した部分と同じ左端・同じ高さ・同じ上下の中心に、フォントの字の輪郭を置く（字送りはフォントの送り幅）。
"""
from pathlib import Path
import argparse
import re

import cairosvg
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.boundsPen import BoundsPen

STATIC = Path(__file__).resolve().parent.parent / 'static'
TEXT = '強欲なラボ'
NUM = re.compile(r'-?\d+(?:\.\d+)?')


def split_subpaths(d):
    """M で始まり Z で終わる部品に分ける（このロゴは絶対座標の M・Q・Z だけで書かれている）。"""
    parts = re.findall(r'M[^M]*', d)
    if ''.join(parts) != d:
        raise ValueError('path の形が想定と違う（M で始まる部品の並びになっていない）')
    return parts


def bbox(part):
    v = list(map(float, NUM.findall(part)))
    xs, ys = v[0::2], v[1::2]
    return min(xs), min(ys), max(xs), max(ys)


def split_upper_lower(parts):
    """どの部品にもまたがれない高さで、上（GREEDY LAB）と下（日本語）に分ける。下の部品群が上より低いことも確かめる。"""
    boxes = [bbox(p) for p in parts]
    for y in sorted({b[3] for b in boxes}):
        upper = [i for i, b in enumerate(boxes) if b[3] <= y]
        lower = [i for i, b in enumerate(boxes) if b[1] > y]
        if upper and lower and len(upper) + len(lower) == len(parts):
            uh = max(boxes[i][3] for i in upper) - min(boxes[i][1] for i in upper)
            lh = max(boxes[i][3] for i in lower) - min(boxes[i][1] for i in lower)
            if lh < uh:
                return upper, lower, boxes
    raise ValueError('GREEDY LAB と日本語の境目が見つからない')


def text_path(font_path, x0, y_center, height):
    """フォントの字を、左端 x0・上下の中心 y_center・インクの高さ height に置いた SVG の path を返す。"""
    font = TTFont(font_path)
    cmap = font.getBestCmap()
    gs = font.getGlyphSet()
    hmtx = font['hmtx']
    missing = [c for c in TEXT if ord(c) not in cmap]
    if missing:
        raise ValueError(f'フォントに字が無い: {"".join(missing)}')
    # まずフォント単位でインクの外枠を測る（y は上向き）
    bp = BoundsPen(gs)
    cursor = 0
    for ch in TEXT:
        name = cmap[ord(ch)]
        gs[name].draw(TransformPen(bp, (1, 0, 0, 1, cursor, 0)))
        cursor += hmtx[name][0]
    fx0, fy0, fx1, fy1 = bp.bounds
    k = height / (fy1 - fy0)
    # フォント座標 → ロゴの座標（y を反転）
    pen = SVGPathPen(gs)
    cursor = 0
    fcy = (fy0 + fy1) / 2
    for ch in TEXT:
        name = cmap[ord(ch)]
        t = (k, 0, 0, -k, x0 + (cursor - fx0) * k, y_center + fcy * k)
        gs[name].draw(TransformPen(pen, t))
        cursor += hmtx[name][0]
    return pen.getCommands(), (fx1 - fx0) * k


def replace_in_svg(svg_text, old_d, kept_d, new_d):
    m = re.search(r'<path([^>]*)\sd="' + re.escape(old_d) + r'"([^>]*)/>', svg_text)
    if not m:
        raise ValueError('入れ替える path が見つからない')
    attrs = (m.group(1) + m.group(2)).strip()
    fill = re.search(r'fill="([^"]+)"', attrs).group(1)
    repl = (f'<path {attrs} d="{kept_d}"/>'
            f'<path fill="{fill}" fill-rule="nonzero" d="{new_d}"/>')
    return svg_text[:m.start()] + repl + svg_text[m.end():]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--font', required=True, help='字形を取るフォント（.ttf）')
    args = ap.parse_args()

    word_svg = (STATIC / 'brand/wordmark-lockup.svg').read_text(encoding='utf-8')
    old_d = re.search(r'<path[^>]*\sd="([^"]+)"', word_svg).group(1)
    parts = split_subpaths(old_d)
    upper, lower, boxes = split_upper_lower(parts)
    lx0 = min(boxes[i][0] for i in lower); ly0 = min(boxes[i][1] for i in lower)
    lx1 = max(boxes[i][2] for i in lower); ly1 = max(boxes[i][3] for i in lower)
    new_d, new_w = text_path(args.font, lx0, (ly0 + ly1) / 2, ly1 - ly0)
    kept_d = ''.join(parts[i] for i in upper)
    print(f'GREEDY LAB の部品 {len(upper)}・日本語の部品 {len(lower)} を外した。'
          f'日本語の幅 {lx1 - lx0:.1f} → {new_w:.1f}（高さ {ly1 - ly0:.1f} は同じ）')

    for name, size in (('wordmark-lockup', (2000, 436)), ('logo-lockup', (1500, 500))):
        p = STATIC / f'brand/{name}.svg'
        s = p.read_text(encoding='utf-8')
        if s.count(f'd="{old_d}"') != 1:
            raise ValueError(f'{name}.svg に入れ替え前の輪郭が1つだけ入っている前提が崩れている')
        s = replace_in_svg(s, old_d, kept_d, new_d)
        p.write_text(s, encoding='utf-8', newline='')
        cairosvg.svg2png(url=str(p), write_to=str(STATIC / f'brand/{name}.png'),
                         output_width=size[0], output_height=size[1])
        print(f'{name}.svg / .png を書き換えた')


if __name__ == '__main__':
    main()
