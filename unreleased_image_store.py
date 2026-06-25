"""
unreleased_image_store.py — X投稿画像のクロップ・Storage保存

Vision（claude-haiku-4-5）でカード領域座標を検出し、
Pillow でクロップして Supabase Storage に保存する。

admin_unreleased.py と watch_x_unreleased.py の両方から使用する。
Flask に依存しないため、Cron スクリプトからも安全にインポートできる。
"""

import base64
import io
import json
import logging
import os
from urllib.parse import urlparse

import requests
from PIL import Image
from supabase import Client

logger = logging.getLogger(__name__)

_STORAGE_BUCKET = "official-card-images"


def detect_card_bbox(image_bytes: bytes, mime: str) -> tuple[int, int, int, int] | None:
    """
    Vision（claude-haiku-4-5）で販促画像内のカード領域を検出する。
    Returns: (x1, y1, x2, y2) または None
    """
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("[image_store] ANTHROPIC_API_KEY 未設定")
        return None

    b64 = base64.standard_b64encode(image_bytes).decode()
    media_type = mime if mime.startswith("image/") else "image/jpeg"

    prompt = (
        "この画像は遊戯王OCGの公式Xアカウントが投稿した販促画像です。\n"
        "画像の左側に遊戯王カードが1枚写っています。\n"
        "カードの外枠（黒い枠線）の外側ギリギリを囲む矩形のピクセル座標を教えてください。\n"
        "座標は切り落とし方向ではなく、やや広めに取ってください（枠線が確実に入るように）。\n\n"
        "以下のJSON形式のみで回答してください。説明文は不要です。\n"
        '{"x1": <左端px>, "y1": <上端px>, "x2": <右端px>, "y2": <下端px>}'
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        text = resp.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1].lstrip("json").strip()
        coords = json.loads(text)
        x1, y1, x2, y2 = int(coords["x1"]), int(coords["y1"]), int(coords["x2"]), int(coords["y2"])
        if x2 > x1 and y2 > y1:
            logger.info(f"[image_store] カード座標検出: ({x1},{y1})-({x2},{y2})")
            return x1, y1, x2, y2
        return None
    except Exception as e:
        logger.warning(f"[image_store] detect_card_bbox 失敗: {e}")
        return None


def crop_x_promo_image(image_bytes: bytes, mime: str) -> bytes:
    """
    X 販促画像をカード領域にクロップする。
    Vision で座標を検出できた場合はその座標で、失敗時は左半分にフォールバック。
    Returns: クロップ済み画像バイト列
    """
    bbox = detect_card_bbox(image_bytes, mime)
    img = Image.open(io.BytesIO(image_bytes))
    w, h = img.size

    if bbox:
        x1, y1, x2, y2 = bbox
        # Vision の座標誤差を吸収するパディング
        # 上端は特に内側を指しやすいので画像高さの 3%（最低 20px）取る
        PAD_TOP = max(20, int(h * 0.03))
        PAD_OTHER = max(8, int(h * 0.01))
        x1 = max(0, x1 - PAD_OTHER)
        y1 = max(0, y1 - PAD_TOP)
        y2 = min(h, y2 + PAD_OTHER)
        # 右端（x2）はアスペクト比（59:86）から算出（Vision の x2 精度が低いため）
        card_height = y2 - y1
        x2 = min(w, x1 + int(card_height * 59 / 86))
        cropped = img.crop((x1, y1, x2, y2))
        logger.info(f"[image_store] Vision クロップ（上PAD={PAD_TOP}px）: ({x1},{y1})-({x2},{y2}) / 元 {w}x{h}")
    else:
        cropped = img.crop((0, 0, w // 2, h))
        logger.warning(f"[image_store] フォールバック左半分クロップ: {w}x{h}")

    ext = mime.split("/")[-1].upper()
    pil_fmt = {"JPEG": "JPEG", "JPG": "JPEG", "PNG": "PNG", "WEBP": "WEBP"}.get(ext, "JPEG")
    buf = io.BytesIO()
    cropped.save(buf, format=pil_fmt)
    return buf.getvalue()


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
    Storage の public URL に更新する（管理画面でクロップ済み画像を表示するため）。

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

    # Content-Type 確認
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

    # サイズ上限（5MB）
    MAX_SIZE = 5 * 1024 * 1024
    if len(image_bytes) > MAX_SIZE:
        return False, f"画像サイズ超過: {len(image_bytes):,}バイト（上限5MB）"

    # 拡張子
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
