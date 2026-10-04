"""公開画像のカード枠候補と、認識用の上部拡大画像を生成する。"""
import io
import logging
import math
import warnings

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)
# デコードと輪郭探索のメモリ使用量を制限する安全上限。
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def _open_image(image_bytes):
    if not isinstance(image_bytes, bytes) or not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError("画像データのサイズが不正です")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(image_bytes)) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("画像の画素数が上限を超えています")
            return image.convert("RGB")


def _valid_box(box, width, height):
    return (isinstance(box, (list, tuple)) and len(box) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                    and math.isfinite(v) for v in box)
            and 0 <= box[0] < box[2] <= width
            and 0 <= box[1] < box[3] <= height)


def _area(box):
    return (box[2] - box[0]) * (box[3] - box[1])


def _iou(a, b):
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0, min(a[3], b[3]) - max(a[1], b[1]))
    return intersection / (_area(a) + _area(b) - intersection)


def _contains(outer, inner):
    return (outer[0] <= inner[0] and outer[1] <= inner[1]
            and outer[2] >= inner[2] and outer[3] >= inner[3])


def refine_card_box(image_bytes: bytes, bbox: list[float]) -> dict | None:
    """元画像ピクセル座標を補正する。保存はせず、常に人による確認を要求する。"""
    try:
        image = _open_image(image_bytes)
        width, height = image.size
        if not _valid_box(bbox, width, height):
            raise ValueError("カード枠の座標が不正です")
        gray = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2GRAY)
        # 実験で使用した閾値。TODO: calibrate from data
        edges = cv2.Canny(gray, 50, 150)
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        candidates = []
        for contour in contours:
            x, y, bw, bh = cv2.boundingRect(contour)
            # TODO: calibrate from data
            if bh <= height * .25 or not .60 < bw / bh < .76:
                continue
            # TODO: calibrate from data
            if cv2.contourArea(contour) / (bw * bh) <= .8:
                continue
            candidates.append([x, y, x + bw, y + bh])
        # 面積順で包含された内側の輪郭（カード内の額縁など）を取り除く。
        outer_boxes = []
        for box in sorted(candidates, key=_area, reverse=True):
            if not any(_contains(outer, box) for outer in outer_boxes):
                outer_boxes.append(box)
        # 別カードへの飛び移りと、大きな背景枠への吸着を防ぐ。
        # 中心の相互包含に加えて、重なりが過半数の候補だけを採用する。
        # TODO: calibrate from data
        eligible = [box for box in outer_boxes
                    if _iou(bbox, box) >= .5
                    and box[0] <= (bbox[0] + bbox[2]) / 2 <= box[2]
                    and box[1] <= (bbox[1] + bbox[3]) / 2 <= box[3]
                    and bbox[0] <= (box[0] + box[2]) / 2 <= bbox[2]
                    and bbox[1] <= (box[1] + box[3]) / 2 <= bbox[3]]
        box = max(eligible, key=lambda b: _iou(bbox, b)) if eligible else bbox
        return {"left": box[0] / width, "top": box[1] / height,
                "right": box[2] / width, "bottom": box[3] / height,
                "width": width, "height": height,
                "method": "contour" if eligible else "vision", "needs_review": True}
    except (ValueError, OSError, Image.DecompressionBombError,
            Image.DecompressionBombWarning, cv2.error) as exc:
        logger.warning("カード枠候補を生成できません: %s", exc)
        return None


def make_level_zoom(image_bytes: bytes, suggestion: dict) -> bytes:
    """候補枠上部25%を3倍（長辺1568px以下）のPNGにする。不正入力は拒否する。"""
    try:
        image = _open_image(image_bytes)
        if not isinstance(suggestion, dict):
            raise ValueError("カード枠候補が不正です")
        box = [suggestion.get(k) for k in ("left", "top", "right", "bottom")]
        if not _valid_box(box, 1, 1):
            raise ValueError("カード枠候補の座標が不正です")
        left, top, right, bottom = box
        pixel_top = math.floor(top * image.height)
        pixel_bottom = math.ceil(bottom * image.height)
        region = (math.floor(left * image.width), pixel_top,
                  math.ceil(right * image.width),
                  pixel_top + math.ceil((pixel_bottom - pixel_top) / 4))
        zoom = image.crop(region)
        scale = min(3, 1568 / max(zoom.size))
        zoom = zoom.resize((max(1, round(zoom.width * scale)),
                            max(1, round(zoom.height * scale))), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        zoom.save(output, format="PNG")
        return output.getvalue()
    except (ValueError, OSError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        logger.warning("レベル確認用の拡大画像を生成できません: %s", exc)
        raise ValueError("レベル確認用の拡大画像を生成できません") from exc
