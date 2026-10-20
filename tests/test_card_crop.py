"""輪郭補正のカード取り違え・不正入力・認識用画像サイズの検証。"""
import io

import pytest
from PIL import Image, ImageDraw

import card_crop


def image_bytes(boxes=(), size=(800, 600)):
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    for box in boxes:
        draw.rectangle(box, outline="black", width=3)
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def test_refines_outer_border_and_does_not_select_neighbor():
    raw = image_bytes([(40, 40, 360, 510), (55, 60, 345, 490), (410, 40, 730, 510)])
    result = card_crop.refine_card_box(raw, [45, 45, 355, 505])
    assert result["method"] == "contour"
    assert result["needs_review"] is True
    assert abs(result["left"] * 800 - 40) <= 2
    assert abs(result["right"] * 800 - 360) <= 2
    assert abs(result["top"] * 600 - 40) <= 2


def test_distant_card_does_not_attract_vision_box():
    result = card_crop.refine_card_box(image_bytes([(410, 40, 730, 510)]), [40, 40, 360, 510])
    assert result["method"] == "vision"
    assert result["left"] == 40 / 800


@pytest.mark.parametrize("box", [None, [], [True, 0, 30, 50], [0, 0, float("nan"), 50],
                                 [0, 0, float("inf"), 50], [-1, 0, 50, 50],
                                 [0, 0, 900, 50], [50, 0, 50, 50]])
def test_invalid_boxes_are_rejected(box, caplog):
    assert card_crop.refine_card_box(image_bytes(), box) is None
    assert "カード枠候補を生成できません" in caplog.text


def test_invalid_and_oversized_images_are_rejected(monkeypatch):
    assert card_crop.refine_card_box(b"invalid", [0, 0, 1, 1]) is None
    monkeypatch.setattr(card_crop, "MAX_IMAGE_PIXELS", 100)
    assert card_crop.refine_card_box(image_bytes(), [0, 0, 50, 50]) is None
    with pytest.raises(ValueError):
        card_crop.make_level_zoom(image_bytes(), {})


def test_zoom_is_top_quarter_at_triple_size():
    raw = image_bytes(size=(800, 600))
    zoom = card_crop.make_level_zoom(raw, {"left": .1, "top": .1, "right": .5, "bottom": .9})
    with Image.open(io.BytesIO(zoom)) as image:
        assert image.format == "PNG"
        assert image.size == (960, 360)


def test_zoom_long_edge_is_capped_and_invalid_box_raises():
    raw = image_bytes(size=(2000, 2000))
    with Image.open(io.BytesIO(card_crop.make_level_zoom(
            raw, {"left": 0, "top": 0, "right": 1, "bottom": 1}))) as image:
        assert max(image.size) == 1568
    with pytest.raises(ValueError):
        card_crop.make_level_zoom(raw, {"left": False, "top": 0, "right": 1, "bottom": 1})


def broken_border_image(inner=True, omit_side=None, scale=1):
    """四隅が開いていて閉輪郭にならないカードと、隣のカードを作る。"""
    image = Image.new("RGB", (800, 600), "white")
    draw = ImageDraw.Draw(image)
    sides = {'left': (40, 70, 40, 480), 'top': (70, 40, 330, 40),
             'right': (360, 70, 360, 480), 'bottom': (70, 510, 330, 510)}
    for side, line in sides.items():
        if side != omit_side:
            draw.line(line, fill="black", width=2)
    if inner:
        draw.rectangle((50, 50, 350, 500), outline="black", width=2)
    draw.rectangle((420, 40, 740, 510), outline="black", width=2)
    image = image.resize((round(image.width * scale), round(image.height * scale)))
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


@pytest.mark.parametrize('scale', [1, .5])
@pytest.mark.parametrize('inner', [True, False])
def test_open_outer_sides_are_detected_without_closed_contour(inner, scale):
    raw = broken_border_image(inner=inner, scale=scale)
    result = card_crop.refine_card_box(raw, [v * scale for v in [45, 45, 355, 505]])
    actual = [result[key] * dimension / scale for key, dimension in
              [('left', 800 * scale), ('top', 600 * scale),
               ('right', 800 * scale), ('bottom', 600 * scale)]]
    assert result['refinement'] == 'edge_lines'
    assert max(abs(a - b) for a, b in zip(actual, [40, 40, 361, 511])) <= 2


def test_missing_side_is_not_invented_from_other_three():
    bbox = [45, 45, 355, 505]
    result = card_crop.refine_card_box(broken_border_image(inner=False, omit_side='bottom'), bbox)
    assert result['method'] == 'vision'
    assert result['bottom'] == bbox[3] / 600


def test_whole_image_box_keeps_outer_edges_even_with_closed_inner_frame():
    raw = image_bytes([(16, 16, 463, 682)], size=(480, 700))
    result = card_crop.refine_card_box(raw, [0, 0, 480, 700])
    assert [result[key] for key in ['left', 'top', 'right', 'bottom']] == [0, 0, 1, 1]
    assert result['refinement'] == 'image_boundary'


def test_outer_decoration_does_not_expand_correct_closed_card_box():
    image = Image.new('RGB', (800, 700), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 100, 420, 570), outline='black', width=2)
    for line in [(80, 125, 80, 545), (440, 125, 440, 545),
                 (125, 80, 395, 80), (125, 590, 395, 590)]:
        draw.line(line, fill='black', width=2)
    output = io.BytesIO()
    image.save(output, 'PNG')
    bbox = [100, 100, 421, 571]
    result = card_crop.refine_card_box(output.getvalue(), bbox)
    actual = [result[key] * dimension for key, dimension in
              [('left', 800), ('top', 700), ('right', 800), ('bottom', 700)]]
    assert max(abs(a - b) for a, b in zip(actual, bbox)) <= 2


def test_outer_decoration_does_not_expand_area_qualified_closed_contour():
    image = Image.new('RGB', (800, 700), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 100, 420, 570), outline='black', width=3)
    for line in [(80, 125, 80, 545), (440, 125, 440, 545),
                 (125, 80, 395, 80), (125, 590, 395, 590)]:
        draw.line(line, fill='black', width=2)
    output = io.BytesIO()
    image.save(output, 'PNG')
    bbox = [100, 100, 421, 571]
    result = card_crop.refine_card_box(output.getvalue(), bbox)
    actual = [result[key] * dimension for key, dimension in
              [('left', 800), ('top', 700), ('right', 800), ('bottom', 700)]]
    assert max(abs(a - b) for a, b in zip(actual, bbox)) <= 1
    assert 'refinement' not in result


def neighbor_card_image(inner_closed):
    """隣のカードとの隙間が辺の探索半径より狭い、並び画像の再現（2026-10-20 鬼神 夜魅・コダイダロス）。

    対象カードの外周は四隅が開いていて閉輪郭にならず、内側に額縁がある。
    左隣のカードは閉輪郭として検出される。外周は59:86に合わせる。
    """
    image = Image.new('RGB', (900, 600), 'white')
    draw = ImageDraw.Draw(image)
    # 実画像の隣カードは塗りの面として閉輪郭になる。
    draw.rectangle((40, 40, 383, 540), fill='gray', outline='black', width=2)
    for line in [(400, 70, 400, 510), (743, 70, 743, 510),
                 (430, 40, 713, 40), (430, 540, 713, 540)]:
        draw.line(line, fill='black', width=2)
    if inner_closed:
        draw.rectangle((413, 53, 730, 527), outline='black', width=3)
    else:
        for line in [(413, 83, 413, 497), (730, 83, 730, 497),
                     (443, 53, 700, 53), (443, 527, 700, 527)]:
            draw.line(line, fill='black', width=2)
    output = io.BytesIO()
    image.save(output, 'PNG')
    return output.getvalue()


def crop_pixels(result, size=(900, 600)):
    return [result['left'] * size[0], result['top'] * size[1],
            result['right'] * size[0], result['bottom'] * size[1]]


def test_closed_inner_frame_does_not_expand_into_neighbor_card_edge():
    # 鬼神 夜魅: 内側の額縁だけ閉輪郭になり、外へ広げる時に隣のカードの右端を左辺に採っていた。
    result = card_crop.refine_card_box(neighbor_card_image(inner_closed=True), [400, 43, 743, 555])
    assert result['refinement'] == 'edge_lines'
    assert max(abs(a - b) for a, b in zip(crop_pixels(result), [400, 40, 744, 541])) <= 2


def test_vision_box_on_inner_frame_bottom_keeps_outer_edge():
    # コダイダロス: AI枠の下辺が額縁に乗り、AI枠寄りの補正が外周を捨てていた。
    result = card_crop.refine_card_box(neighbor_card_image(inner_closed=False), [401, 41, 742, 528])
    assert result['refinement'] == 'edge_lines'
    assert max(abs(a - b) for a, b in zip(crop_pixels(result), [400, 40, 744, 541])) <= 2
