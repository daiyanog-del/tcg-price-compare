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


def detect_card_bbox_vision(image_bytes: bytes, mime: str) -> tuple[float, float, float, float] | None:
    """
    Vision（claude-sonnet-4-6）でカード領域を割合（0.0〜1.0）で取得する。
    Returns: (left, top, right, bottom) の割合、または None
    """
    import base64
    import json
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    b64 = base64.standard_b64encode(image_bytes).decode()
    media_type = mime if mime.startswith("image/") else "image/jpeg"

    prompt = (
        "この画像は遊戯王OCGの公式Xアカウントが投稿した販促画像です。\n"
        "画像の左側に遊戯王カード1枚が写っています。\n\n"
        "カードの外枠（一番外側の縁）の位置を、画像の幅・高さを 1.0 とした割合で答えてください。\n"
        "余白を入れず、カードの枠線の外側ギリギリを指定してください。\n"
        "特に left（左端）は背景とカード枠線の境界を正確に指定してください。\n\n"
        "例：\n"
        '{"left": 0.08, "top": 0.03, "right": 0.58, "bottom": 0.97}\n\n'
        "JSONのみ返してください。説明文は不要です。"
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=128,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        text = resp.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1].lstrip("json").strip()
        c = json.loads(text)
        left, top, right, bottom = float(c["left"]), float(c["top"]), float(c["right"]), float(c["bottom"])
        if 0 <= left < right <= 1 and 0 <= top < bottom <= 1:
            logger.info(f"[image_store] Vision割合検出: left={left}, top={top}, right={right}, bottom={bottom}")
            return left, top, right, bottom
        return None
    except Exception as e:
        logger.warning(f"[image_store] Vision割合検出失敗: {e}")
        return None


def crop_x_promo_image(image_bytes: bytes, mime: str) -> bytes:
    """
    X 販促画像をカード領域にクロップする。

    Vision（sonnet-4-6）でカード領域を割合で取得し、
    右端はアスペクト比（59:86）で補正する。
    Vision失敗時は左半分にフォールバック。
    """
    img = Image.open(io.BytesIO(image_bytes))
    w, h = img.size

    ratios = detect_card_bbox_vision(image_bytes, mime)
    if ratios:
        left, top, right, bottom = ratios
        # Vision は系統的に余白を多く取るため辺ごとに内側へ補正する。
        # 値は Vision の癖に合わせて個別調整する（0.0〜0.1 程度が目安）。
        TRIM_LEFT   = 0.0
        TRIM_TOP    = 0.02
        TRIM_BOTTOM = 0.02
        left   = min(left   + TRIM_LEFT,   0.5)
        top    = min(top    + TRIM_TOP,    0.5)
        bottom = max(bottom - TRIM_BOTTOM, 0.5)
        # left を起点に x1 を決定し、x2 はアスペクト比（59:86）で算出する。
        x1 = int(w * left)
        y1 = int(h * top)
        y2 = int(h * bottom)
        card_height = y2 - y1
        x2 = min(w, x1 + int(card_height * 59 / 86))
        logger.info(
            f"[image_store] Visionクロップ "
            f"(L+{TRIM_LEFT} T+{TRIM_TOP} B-{TRIM_BOTTOM}): "
            f"({x1},{y1})-({x2},{y2}) / 元 {w}x{h}"
        )
    else:
        logger.warning(f"[image_store] Vision失敗、左半分フォールバック")
        x1, y1, x2, y2 = 0, 0, w // 2, h

    cropped = img.crop((x1, y1, x2, y2))
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


def crop_and_save_image(
    sb: Client,
    card_id: int,
    left: float,
    top: float,
    right: float,
    bottom: float,
) -> tuple[bool, str]:
    """
    保存済み画像を指定割合でクロップし直してStorageを上書きする。
    left/top/right/bottom は画像全体を 1.0 とした割合（0.0〜1.0）。
    """
    # 現在のStorage URLと保存パスを取得
    try:
        rec = (
            sb.table("official_card_images")
            .select("id, storage_path, public_url")
            .eq("unreleased_card_id", card_id)
            .is_("deleted_at", "null")
            .execute()
        )
        if not rec.data:
            return False, "画像レコードが見つかりません"
        row = rec.data[0]
        storage_path = row.get("storage_path", "")
        public_url = row.get("public_url", "")
    except Exception as e:
        return False, f"DB参照失敗: {e}"

    if not public_url:
        return False, "Storage URLが登録されていません"

    # 現在の保存済み画像をダウンロード
    try:
        resp = requests.get(public_url, timeout=30)
        resp.raise_for_status()
        raw_bytes = resp.content
    except Exception as e:
        return False, f"画像取得失敗: {e}"

    content_type = resp.headers.get("Content-Type", "image/jpeg")
    mime = content_type.split(";")[0].strip().lower()
    if not mime.startswith("image/"):
        mime = "image/jpeg"

    # クロップ
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        w, h = img.size
        x1 = int(w * left)
        y1 = int(h * top)
        x2 = int(w * right)
        y2 = int(h * bottom)
        cropped = img.crop((x1, y1, x2, y2))
        ext = mime.split("/")[-1].upper()
        pil_fmt = {"JPEG": "JPEG", "JPG": "JPEG", "PNG": "PNG", "WEBP": "WEBP"}.get(ext, "JPEG")
        buf = io.BytesIO()
        cropped.save(buf, format=pil_fmt)
        image_bytes = buf.getvalue()
    except Exception as e:
        return False, f"クロップ失敗: {e}"

    # Storage に上書き保存
    try:
        sb.storage.from_(_STORAGE_BUCKET).upload(
            path=storage_path,
            file=image_bytes,
            file_options={"content-type": mime, "upsert": "true"},
        )
    except Exception as e:
        return False, f"Storage保存失敗: {e}"

    # extraction_raw.card_image_url を更新（キャッシュバスター付きURLで管理画面プレビューをリフレッシュ）
    from datetime import datetime, timezone
    bust = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    cache_busted_url = f"{public_url.split('?')[0]}?t={bust}"
    try:
        card_resp = sb.table("unreleased_cards").select("extraction_raw").eq("id", card_id).execute()
        if card_resp.data:
            raw = card_resp.data[0].get("extraction_raw") or {}
            raw["card_image_url"] = cache_busted_url
            sb.table("unreleased_cards").update({"extraction_raw": raw}).eq("id", card_id).execute()
    except Exception as e:
        logger.warning(f"[image_store] extraction_raw 更新失敗（無視）: {e}")

    logger.info(f"[image_store] クロップ再保存完了: card_id={card_id}, path={storage_path!r}")
    return True, cache_busted_url
