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


def _edge_line_box(edges, box, *, nearest=False):
    """途切れた外周を四辺別々の直線支持から検出する。出力を固定比率ではずらさない。"""
    x1, y1, x2, y2 = box
    width, height = x2 - x1, y2 - y1

    def locate(position, start, end, vertical, radius, first):
        length = edges.shape[0 if vertical else 1]
        start, end = max(0, int(start)), min(length, int(end))
        if end <= start:
            return None
        strip = edges[start:end, :] if vertical else edges[:, start:end].T
        support = (strip > 0).mean(axis=0)
        lower = max(0, round(position - radius))
        upper = min(len(support), round(position + radius) + 1)
        # 二重エッジ・発光枠は近接した列をまとめ、その和集合で連続性を確認する。
        # 12枚の元画像で観測した1〜3pxの二重エッジが根拠。縮小画像で再検証する。
        # TODO: calibrate from data
        groups = []
        for index in range(lower, upper):
            if support[index] < .5:
                continue
            if groups and index - groups[-1][-1] <= 3 and index - groups[-1][0] <= 4:
                groups[-1].append(index)
            else:
                groups.append([index])
        # 少数の文字線や別カードの一部ではなく、辺の大部分に根拠があることを要求。
        # TODO: calibrate from data
        groups = [group for group in groups if (strip[:, group] > 0).any(axis=1).mean() >= .8]
        if not groups:
            return None
        if nearest:
            group = min(groups, key=lambda values: abs(
                float(np.average(values, weights=support[values])) + (not first) - position))
        else:
            group = groups[0 if first else -1]
        boundary = round(float(np.average(group, weights=support[group])))
        # 右・下はPillowのクロップと同じ排他的座標へ変換する。
        return boundary if first else boundary + 1

    # 角の丸みを避けた中央80%で探索。近傍10%は12枚のAI枠誤差を含む探索域。
    # TODO: calibrate from data
    result = [locate(x1, y1 + height * .1, y2 - height * .1, True, width * .1, True),
              locate(y1, x1 + width * .1, x2 - width * .1, False, height * .1, True),
              locate(x2, y1 + height * .1, y2 - height * .1, True, width * .1, False),
              locate(y2, x1 + width * .1, x2 - width * .1, False, height * .1, False)]
    if any(value is None for value in result):
        return None
    return result


def refine_card_box(image_bytes: bytes, bbox: list[float]) -> dict | None:
    """元画像ピクセル座標を補正する。保存はせず、常に人による確認を要求する。"""
    try:
        image = _open_image(image_bytes)
        width, height = image.size
        if not _valid_box(bbox, width, height):
            raise ValueError("カード枠の座標が不正です")
        # 元のAI枠が画像全体と厳密に一致するカード単体画像は内枠へ縮めない。
        if list(bbox) == [0, 0, width, height]:
            return {"left": 0, "top": 0, "right": 1, "bottom": 1,
                    "width": width, "height": height, "method": "vision",
                    "refinement": "image_boundary", "needs_review": True}
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
        method = "contour" if eligible else "vision"
        refinement = None
        # 全画面がカードの場合は、その内側の額縁へ切り縮めない。
        if box != [0, 0, width, height]:
            line_box = _edge_line_box(edges, box)
            if not eligible:
                nearby = _edge_line_box(edges, bbox, nearest=True)
                # 細い線は閉輪郭の面積条件を満たさない場合がある。四辺すべてが
                # AI枠の二重エッジ許容2px以内で支持されるなら、外側の装飾線より優先。
                # TODO: calibrate from data
                if nearby is not None and max(abs(a - b) for a, b in zip(nearby, bbox)) <= 2:
                    line_box = nearby
            if (line_box is not None and _valid_box(line_box, width, height)
                    and .60 < (line_box[2] - line_box[0]) / (line_box[3] - line_box[1]) < .76
                    and _iou(bbox, line_box) >= .5
                    and line_box[0] <= (bbox[0] + bbox[2]) / 2 <= line_box[2]
                    and line_box[1] <= (bbox[1] + bbox[3]) / 2 <= line_box[3]
                    and bbox[0] <= (line_box[0] + line_box[2]) / 2 <= bbox[2]
                    and bbox[1] <= (line_box[1] + line_box[3]) / 2 <= bbox[3]):
                # 既存の閉輪郭は、直線がその外周を支持する場合だけ拡張する。
                # 二重エッジの2px許容以外は内向き変更せず、正しい閉輪郭を維持する。
                # TODO: calibrate from data
                outward = [box[0] - line_box[0], box[1] - line_box[1],
                           line_box[2] - box[2], line_box[3] - box[3]]
                # 外側の装飾線へ広げないため、既存閉輪郭がある時はAI枠との一致も改善する。
                if not eligible or (min(outward) >= -2 and max(outward) > 2
                                    and _iou(bbox, line_box) > _iou(bbox, box)):
                    box, method, refinement = line_box, "contour", "edge_lines"
        result = {"left": box[0] / width, "top": box[1] / height,
                "right": box[2] / width, "bottom": box[3] / height,
                "width": width, "height": height,
                "method": method, "needs_review": True}
        if refinement:
            result["refinement"] = refinement
        return result
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
