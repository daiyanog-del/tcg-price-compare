"""
unreleased_image_store.py — X投稿画像の Storage 保存・手動クロップ

クロップは管理画面での手動操作のみ。取り込み時は元画像をそのまま保存する。
admin_unreleased.py と watch_x_unreleased.py の両方から使用する。
Flask に依存しないため、Cron スクリプトからも安全にインポートできる。
"""

import io
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from PIL import Image
from supabase import Client

logger = logging.getLogger(__name__)

_STORAGE_BUCKET = "official-card-images"


def ingest_x_card_image(
    sb: Client,
    card_id: int,
    image_url: str,
    tweet_url: str,
) -> tuple[bool, str]:
    """
    X カード画像を取得して Storage に保存し、official_card_images に INSERT する。
    クロップは行わず元画像をそのまま保存する（管理画面で手動クロップする）。

    成功時は unreleased_cards.extraction_raw.card_image_url を
    Storage の public URL に更新する（管理画面プレビュー用）。

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
        image_bytes = resp.content
    except Exception as e:
        return False, f"画像取得失敗: {e}"

    content_type = resp.headers.get("Content-Type", "image/jpeg")
    mime = content_type.split(";")[0].strip().lower()
    if not mime.startswith("image/"):
        return False, f"Content-Type が image/* でない: {mime!r}"

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
    オリジナル画像（source_image_url）を指定割合でクロップして Storage を上書きする。
    常にオリジナルから切り出すため、何度でもやり直せる。
    left/top/right/bottom は画像全体を 1.0 とした割合（0.0〜1.0）。
    """
    # オリジナル画像URLと storage_path を取得
    try:
        rec = (
            sb.table("official_card_images")
            .select("id, storage_path, public_url, source_image_url")
            .eq("unreleased_card_id", card_id)
            .is_("deleted_at", "null")
            .execute()
        )
        if not rec.data:
            return False, "画像レコードが見つかりません"
        row = rec.data[0]
        storage_path = row.get("storage_path", "")
        public_url = row.get("public_url", "")
        source_url = row.get("source_image_url", "")
    except Exception as e:
        return False, f"DB参照失敗: {e}"

    # オリジナル画像を取得（Storage の上書き済み画像ではなく元の X 画像から切り出す）
    download_url = source_url or public_url
    if not download_url:
        return False, "画像URLが登録されていません"

    try:
        resp = requests.get(download_url, timeout=30)
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

    # キャッシュバスター付き URL を生成（Supabase CDN が古い画像をキャッシュしているため）
    bust = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    base_url = (public_url or "").split("?")[0]
    cache_busted_url = f"{base_url}?t={bust}"

    # official_card_images.public_url を更新（公開中カードリストが参照するURL）
    try:
        sb.table("official_card_images").update({"public_url": cache_busted_url}).eq("unreleased_card_id", card_id).is_("deleted_at", "null").execute()
    except Exception as e:
        logger.warning(f"[image_store] official_card_images.public_url 更新失敗（無視）: {e}")

    # extraction_raw.card_image_url も更新（管理画面プレビュー用）
    try:
        card_resp = sb.table("unreleased_cards").select("extraction_raw").eq("id", card_id).execute()
        if card_resp.data:
            raw = card_resp.data[0].get("extraction_raw") or {}
            raw["card_image_url"] = cache_busted_url
            sb.table("unreleased_cards").update({"extraction_raw": raw}).eq("id", card_id).execute()
    except Exception as e:
        logger.warning(f"[image_store] extraction_raw 更新失敗（無視）: {e}")

    logger.info(f"[image_store] クロップ保存完了: card_id={card_id}, path={storage_path!r}")
    return True, cache_busted_url
