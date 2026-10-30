"""
ロゴの「強欲なラボ」（GREEDY LAB の下の日本語）を、指定したフォントの字形で描き直す（生成専用・本番未使用）

使い方:
    python tools/build_brand_subtitle.py --font <フォント.ttf>
    python tools/build_icons.py --og      # 続けて OGP 画像（og-logo / og-post-logo / og-image）を作り直す

入力:
    static/brand/wordmark-lockup.svg・logo-lockup.svg（同じ輪郭の path を使い回している）
    --font：字形を取るフォント。リポジトリには置かない（2026-10-30 は yugioh-shorts の試作フォント ProtoReisho-PR12.ttf。
            カード名の字形を写した試作で、フォントファイルは配布しない。ロゴには輪郭だけを書き出す）
出力（上書き）:
    static/brand/wordmark-lockup.svg / .png（幅2000）・logo-lockup.svg / .png（1500x500）
    PNG は SVG を cairosvg で描いたもの（入れ替え前の PNG もこの方法で描いたものと画素単位で一致することを確かめた）

日本語の置き方（既定値は 2026-10-30 のユーザー採用案 B。本番のヘッダーでモックを比べて決めた）:
    --scale 1.5      元の手描きのロゴの日本語の高さに対する倍率（細い隷書はヘッダーの小さな表示でかすれたため大きく）
    --gap-ratio 0.5  GREEDY LAB と日本語の間の空きを、元の何倍にするか（大きくした分ロゴが下に伸びすぎないよう詰める）
    --stroke 6       字の輪郭に足す同色の縁の太さ（元の座標。ヘッダーの幅170pxで約1px太くなる）
    左端は元の日本語と同じ。ロゴの高さは日本語の下端に合わせて伸ばし、下の余白は元と同じにする。

元のロゴからでも、すでに描き直したロゴからでも作れる:
    元のロゴ：1つの path に GREEDY LAB と日本語が入っている → 日本語の部品を外す
    描き直したロゴ：日本語は id="brand-subtitle" の別の path → それを外して描き直す
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
# 元の手描きのロゴ（2026-10-30 の入れ替え前・commit 53fb1f9）の日本語部分の外枠（元の座標）。描き直したロゴから作るときに使う
BASE_LEFT, BASE_TOP, BASE_BOTTOM = 611.0, 535.0, 595.0
BASE_HEIGHT = 218.0  # 元のロゴの viewBox の高さ（幅は1000）


def split_subpaths(d):
    """M で始まり Z で終わる部品に分ける（手描きのロゴは絶対座標の M・Q・Z だけで書かれている）。"""
    parts = re.findall(r'M[^M]*', d)
    if ''.join(parts) != d:
        raise ValueError('path の形が想定と違う（M で始まる部品の並びになっていない）')
    return parts


def bbox(part):
    v = list(map(float, NUM.findall(part)))
    xs, ys = v[0::2], v[1::2]
    return min(xs), min(ys), max(xs), max(ys)


def split_upper_lower(parts):
    """どの部品にもまたがれない高さで、上（GREEDY LAB）と下（日本語）に分ける。下が無ければ (全部, []) を返す。"""
    boxes = [bbox(p) for p in parts]
    for y in sorted({b[3] for b in boxes}):
        upper = [i for i, b in enumerate(boxes) if b[3] <= y]
        lower = [i for i, b in enumerate(boxes) if b[1] > y]
        if upper and lower and len(upper) + len(lower) == len(parts):
            uh = max(boxes[i][3] for i in upper) - min(boxes[i][1] for i in upper)
            lh = max(boxes[i][3] for i in lower) - min(boxes[i][1] for i in lower)
            if lh < uh:
                return upper, lower, boxes
    return list(range(len(parts))), [], boxes


def text_path(font_path, x0, top, height):
    """フォントの字を、左端 x0・上端 top・インクの高さ height に置いた SVG の path を返す。"""
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
    # フォント座標 → ロゴの座標（y を反転。フォントの上端 fy1 をロゴの上端 top に）
    pen = SVGPathPen(gs)
    cursor = 0
    for ch in TEXT:
        name = cmap[ord(ch)]
        t = (k, 0, 0, -k, x0 + (cursor - fx0) * k, top + fy1 * k)
        gs[name].draw(TransformPen(pen, t))
        cursor += hmtx[name][0]
    return pen.getCommands(), (fx1 - fx0) * k


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--font', required=True, help='字形を取るフォント（.ttf）')
    ap.add_argument('--scale', type=float, default=1.5, help='元の日本語の高さに対する倍率')
    ap.add_argument('--gap-ratio', type=float, default=0.5, help='GREEDY LAB との間の空きを元の何倍にするか')
    ap.add_argument('--stroke', type=float, default=6.0, help='字の輪郭に足す同色の縁の太さ（元の座標・0で無し）')
    args = ap.parse_args()

    word_svg = (STATIC / 'brand/wordmark-lockup.svg').read_text(encoding='utf-8')
    g = re.search(r'<g transform="translate\(([-\d.]+) ([-\d.]+)\) scale\(([-\d.]+)\)">(.*?)</g>', word_svg, re.S)
    tx, ty, k = map(float, g.group(1, 2, 3))
    inner = g.group(4)
    first = re.search(r'<path([^>]*)\sd="([^"]+)"([^>]*)/>', inner)
    attrs = (first.group(1) + first.group(3)).strip()
    fill = re.search(r'fill="([^"]+)"', attrs).group(1)
    parts = split_subpaths(first.group(2))
    upper, lower, boxes = split_upper_lower(parts)
    if lower:
        # 元の手描きのロゴ：日本語の部品を外す（外枠は定数と一致するはず）
        lx0 = min(boxes[i][0] for i in lower); ly0 = min(boxes[i][1] for i in lower); ly1 = max(boxes[i][3] for i in lower)
        if abs(lx0 - BASE_LEFT) > 1 or abs(ly0 - BASE_TOP) > 1 or abs(ly1 - BASE_BOTTOM) > 1:
            raise ValueError(f'元の日本語の外枠が想定と違う: {lx0}, {ly0}, {ly1}')
    upper_d = ''.join(parts[i] for i in upper)
    upper_bottom = max(boxes[i][3] for i in upper)

    base_gap = BASE_TOP - upper_bottom
    top = upper_bottom + base_gap * args.gap_ratio
    height = (BASE_BOTTOM - BASE_TOP) * args.scale
    new_d, new_w = text_path(args.font, BASE_LEFT, top, height)
    # ロゴの高さ：日本語の下端に合わせ、下の余白は元と同じに
    margin = BASE_HEIGHT - (BASE_BOTTOM * k + ty)
    vb_h = round(max(BASE_HEIGHT, (top + height) * k + ty + margin), 1)
    stroke = (f' stroke="{fill}" stroke-width="{args.stroke:g}" stroke-linejoin="round"' if args.stroke > 0 else '')
    new_inner = (f'<path {attrs} d="{upper_d}"/>'
                 f'<path id="brand-subtitle" fill="{fill}" fill-rule="nonzero"{stroke} d="{new_d}"/>')
    print(f'日本語：高さ {BASE_BOTTOM - BASE_TOP:.0f}→{height:.1f}・幅 {new_w:.1f}・GREEDY LAB との間 {base_gap:.1f}→{top - upper_bottom:.1f}'
          f'・縁 {args.stroke:g}。ロゴの高さ {BASE_HEIGHT:g}→{vb_h:g}（幅1000）')

    # ロゴ単体
    p = STATIC / 'brand/wordmark-lockup.svg'
    s = word_svg.replace(inner, new_inner, 1)
    s = re.sub(r'width="1000" height="[\d.]+" viewBox="0 0 1000 [\d.]+"',
               f'width="1000" height="{vb_h:g}" viewBox="0 0 1000 {vb_h:g}"', s, count=1)
    p.write_text(s, encoding='utf-8', newline='')
    cairosvg.svg2png(url=str(p), write_to=str(STATIC / 'brand/wordmark-lockup.png'),
                     output_width=2000, output_height=round(vb_h * 2))
    print(f'wordmark-lockup.svg / .png（2000x{round(vb_h * 2)}）を書き換えた')

    # フラスコ付き：中のロゴの箱を縦に伸ばし、上下の中心は保つ（フラスコとの釣り合いを変えない）
    p = STATIC / 'brand/logo-lockup.svg'
    s = p.read_text(encoding='utf-8')
    # 中には同じ形の入れ物が2つある（フラスコとロゴ）。ロゴ単体と同じ置き方（translate）のほうを選ぶ
    cands = [m for m in re.finditer(
        r'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 ([\d.]+)" x="([\d.]+)" y="([\d.]+)" '
        r'width="([\d.]+)" height="([\d.]+)"><g transform="translate\(([-\d.]+) ([-\d.]+)\) scale\(([-\d.]+)\)">(.*?)</g>', s, re.S)
        if abs(float(m.group(6)) - tx) < 1e-6 and abs(float(m.group(7)) - ty) < 1e-6]
    if len(cands) != 1:
        raise ValueError('logo-lockup.svg の中に、wordmark-lockup と同じ置き方のロゴが1つだけ入っている前提が崩れている')
    m = cands[0]
    old_vh, x, y, w, h = (float(m.group(i)) for i in range(1, 6))
    unit_h = h / old_vh                       # 中のロゴの1単位あたりの高さ
    new_h = vb_h * unit_h
    new_y = y + h / 2 - new_h / 2
    head = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 {vb_h:g}" x="{x:g}" y="{round(new_y, 1):g}" '
            f'width="{w:g}" height="{round(new_h, 1):g}"><g transform="translate({m.group(6)} {m.group(7)}) scale({m.group(8)})">')
    s = s[:m.start()] + head + new_inner + '</g>' + s[m.end():]
    p.write_text(s, encoding='utf-8', newline='')
    cairosvg.svg2png(url=str(p), write_to=str(STATIC / 'brand/logo-lockup.png'), output_width=1500, output_height=500)
    print('logo-lockup.svg / .png（1500x500）を書き換えた')


if __name__ == '__main__':
    main()
