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
