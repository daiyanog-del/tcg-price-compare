"""
unreleased_image_store.py — X投稿画像のクロップ・Storage保存

numpy で水平輝度勾配を計算してカードと背景の境界（右端）を検出し、
アスペクト比（59:86）からカード領域を決定して Pillow でクロップする。
Vision API は使用しない。

admin_unreleased.py と watch_x_unreleased.py の両方から使用する。
Flask に依存しないため、Cron スクリプトからも安全にインポートできる。
"""

import io
import logging
import os
from urllib.parse import urlparse

import requests
from PIL import Image
from supabase import Client

logger = logging.getLogger(__name__)

_STORAGE_BUCKET = "official-card-images"


def _detect_card_right_edge(arr, h: int, w: int) -> int:
    """
    画像の水平輝度勾配を計算し、カードと背景の境界列（右端 x2）を返す。

    カードは左側にあり、右側の販促背景との間に強い色変化がある。
    画像幅の 30〜70% の範囲でのみ境界を探す（カード右端はこの範囲に必ずある）。
    """
    import numpy as np

    # 上下 20〜80% の中央帯で計算（上下端のノイズを除く）
    mid = arr[int(h * 0.2):int(h * 0.8)]
    gray = mid.mean(axis=2).astype(float) if mid.ndim == 3 else mid.astype(float)

    # 隣接列の輝度差の絶対値 → 強い縦エッジが境界列
    col_grad = abs(gray[:, 1:] - gray[:, :-1]).mean(axis=0)

    search_start = int(w * 0.30)
    search_end   = int(w * 0.70)
    best_local = int(col_grad[search_start:search_end].argmax())
    x2 = search_start + best_local + 1  # diff で 1 列分ずれるため +1
    logger.info(f"[image_store] カード右端検出: x2={x2} (画像幅 {w}px)")
    return x2


def crop_x_promo_image(image_bytes: bytes, mime: str) -> bytes:
    """
    X 販促画像をカード領域にクロップする。

    手順:
      1. 水平輝度勾配でカード右端（x2）を検出
      2. アスペクト比（59:86）からカード幅を算出 → x1 を決定
      3. 上下は画像高さの 1% マージンのみ（y1=1%, y2=99%）
    """
    import numpy as np

    img = Image.open(io.BytesIO(image_bytes))
    w, h = img.size
    arr = _to_numpy(img)

    try:
        x2 = _detect_card_right_edge(arr, h, w)
    except Exception as e:
        logger.warning(f"[image_store] 右端検出失敗、左半分でフォールバック: {e}")
        x2 = w // 2

    y1 = int(h * 0.01)
    y2 = int(h * 0.99)
    card_height = y2 - y1
    card_width  = int(card_height * 59 / 86)
    x1 = max(0, x2 - card_width)
    x2 = min(w, x1 + card_width)

    logger.info(f"[image_store] クロップ領域: ({x1},{y1})-({x2},{y2}) / 元 {w}x{h}")
    cropped = img.crop((x1, y1, x2, y2))

    ext = mime.split("/")[-1].upper()
    pil_fmt = {"JPEG": "JPEG", "JPG": "JPEG", "PNG": "PNG", "WEBP": "WEBP"}.get(ext, "JPEG")
    buf = io.BytesIO()
    cropped.save(buf, format=pil_fmt)
    return buf.getvalue()


def _to_numpy(img: Image.Image):
    """PIL Image を numpy 配列に変換する（numpy 遅延インポート）"""
    import numpy as np
    return np.array(img)


def ingest_x_card_image(
    sb: Client,
    card_id: int,
    image_url: str,
    tweet_url: str,
) -> tuple[bool, str]:
    """
    X カード画像を取得・クロップして Storage に保存し、
    official_card_images に INSERT する。

    成功時は unreleased_cards.extraction_raw.card_image_url を
    Storage の public URL に更新する（管理画面で承認前にクロップ済み画像を表示するため）。

    Returns: (成功フラグ, 理由文字列)
    """
    # 既存の画像レコードがあればスキップ（二重取込防止）
    try:
        existing = (
            sb.table("official_card_images")
            .select("id")
            .eq("unreleased_card_id", card_id)
            .is_("deleted_at", "null")
            .execute()
        )
        if existing.data:
            logger.info(f"[image_store] 画像取込済みのためスキップ: card_id={card_id}")
            return True, "取込済み"
    except Exception as e:
        logger.warning(f"[image_store] 既存確認失敗（処理継続）: {e}")

    # 画像ダウンロード
    try:
        resp = requests.get(image_url, timeout=30)
        resp.raise_for_status()
        raw_bytes = resp.content
    except Exception as e:
        return False, f"画像取得失敗: {e}"

    content_type = resp.headers.get("Content-Type", "image/jpeg")
    mime = content_type.split(";")[0].strip().lower()
    if not mime.startswith("image/"):
        return False, f"Content-Type が image/* でない: {mime!r}"

    # クロップ（pbs.twimg.com の画像のみ）
    if urlparse(image_url).netloc == "pbs.twimg.com":
        try:
            image_bytes = crop_x_promo_image(raw_bytes, mime)
        except Exception as e:
            logger.warning(f"[image_store] クロップ失敗（元画像で保存）: {e}")
            image_bytes = raw_bytes
    else:
        image_bytes = raw_bytes

    MAX_SIZE = 5 * 1024 * 1024
    if len(image_bytes) > MAX_SIZE:
        return False, f"画像サイズ超過: {len(image_bytes):,}バイト（上限5MB）"

    ext_map = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
               "image/gif": "gif", "image/webp": "webp"}
    ext = ext_map.get(mime, "jpg")
    storage_path = f"unreleased/{card_id}.{ext}"

    # Storage アップロード
    try:
        sb.storage.from_(_STORAGE_BUCKET).upload(
            path=storage_path,
            file=image_bytes,
            file_options={"content-type": mime, "upsert": "true"},
        )
    except Exception as e:
        return False, f"Storage アップロード失敗: {e}"

    # public URL 取得
    try:
        url_resp = sb.storage.from_(_STORAGE_BUCKET).get_public_url(storage_path)
        public_url = url_resp if isinstance(url_resp, str) else url_resp.get("publicUrl", "")
    except Exception as e:
        logger.warning(f"[image_store] public URL 取得失敗: {e}")
        public_url = ""

    # official_card_images INSERT
    try:
        sb.table("official_card_images").insert({
            "unreleased_card_id": card_id,
            "storage_path": storage_path,
            "public_url": public_url,
            "source_image_url": image_url,
            "source_page_url": tweet_url,
            "source_domain": urlparse(image_url).netloc,
        }).execute()
    except Exception as e:
        return False, f"DB 登録失敗: {e}"

    # extraction_raw.card_image_url を Storage URL に更新（管理画面プレビュー用）
    if public_url:
        try:
            card_resp = sb.table("unreleased_cards").select("extraction_raw").eq("id", card_id).execute()
            if card_resp.data:
                raw = card_resp.data[0].get("extraction_raw") or {}
                raw["card_image_url"] = public_url
                sb.table("unreleased_cards").update({"extraction_raw": raw}).eq("id", card_id).execute()
        except Exception as e:
            logger.warning(f"[image_store] extraction_raw 更新失敗（無視）: {e}")

    logger.info(f"[image_store] 画像保存完了: card_id={card_id}, path={storage_path!r}")
    return True, f"取込成功: {storage_path}"
