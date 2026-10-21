"""
backfill_unreleased_thumbs.py — 保存済みの未発売カード画像に、新着欄用の縮小版を作り足す一回限りのスクリプト

背景:
  2026-10-21 から、画像の保存・切り抜き・復元のたびに縮小版（unreleased/thumbs/<id>.webp・幅300px）を作り、
  official_card_images.thumb_url に記録するようにした（unreleased_image_store.store_thumbnail）。
  それより前に保存した画像には縮小版が無いので、ここで作り足す。

対象:
  official_card_images で deleted_at IS NULL かつ thumb_url IS NULL の行（非表示の行も、表示に戻したときのため含める）。
  縮小版は今 Storage にある画像（切り抜き済みなら切り抜き後）から作る。

冪等性:
  thumb_url が空の行だけを対象にするため、再実行しても作り終えた行はスキップされる。

実行:
  python tools/backfill_unreleased_thumbs.py            # 本番 Supabase へ書き込み
  python tools/backfill_unreleased_thumbs.py --dry-run  # 対象一覧の確認のみ

認証:
  環境変数 SUPABASE_URL / SUPABASE_KEY を使う。未設定時は ~/.cardsouba/watcher.env から読み込む。
"""

import io
import logging
import os
import sys

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
except Exception:
    pass

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
logging.getLogger("httpx").setLevel(logging.WARNING)  # 1件ごとの HTTP ログを出さない
logger = logging.getLogger("backfill_thumbs")


def _load_env_fallback() -> None:
    """SUPABASE_URL/KEY が未設定なら ~/.cardsouba/watcher.env から補完する（refetch_missing_images.py と同じ）。"""
    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY"):
        return
    env_path = os.path.join(os.path.expanduser("~"), ".cardsouba", "watcher.env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def backfill(sb, dry_run: bool = False) -> dict:
    from unreleased_image_store import _STORAGE_BUCKET, store_thumbnail

    rows: list[dict] = []
    page = 1000  # Supabase の1回の取得上限に合わせてページ送りする
    while True:
        chunk = (
            sb.table("official_card_images")
            .select("id, unreleased_card_id, storage_path")
            .is_("deleted_at", "null")
            .is_("thumb_url", "null")
            .order("id")
            .range(len(rows), len(rows) + page - 1)
            .execute()
            .data
            or []
        )
        rows.extend(chunk)
        if len(chunk) < page:
            break
    logger.info(f"対象: {len(rows)}件")
    summary = {"target": len(rows), "done": 0, "failed": 0, "before_bytes": 0}
    for r in rows:
        card_id, path = r["unreleased_card_id"], r.get("storage_path") or ""
        if dry_run:
            logger.info(f"  id={r['id']} card_id={card_id} path={path}")
            continue
        try:
            data = bytes(sb.storage.from_(_STORAGE_BUCKET).download(path))
        except Exception as e:
            logger.warning(f"  失敗（画像を取得できない）: card_id={card_id} path={path}: {e}")
            summary["failed"] += 1
            continue
        url = store_thumbnail(sb, card_id, data)
        if not url:
            summary["failed"] += 1
            continue
        try:
            # 実行中に管理画面で切り抜かれた行（thumb_url が入った行）は上書きしない
            sb.table("official_card_images").update({"thumb_url": url}).eq("id", r["id"]).is_("thumb_url", "null").execute()
        except Exception as e:
            logger.warning(f"  失敗（記録できない）: card_id={card_id}: {e}")
            summary["failed"] += 1
            continue
        summary["done"] += 1
        summary["before_bytes"] += len(data)
        logger.info(f"  作成: card_id={card_id} 原寸 {len(data):,}バイト")
    logger.info(f"完了: {summary}")
    return summary


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    _load_env_fallback()
    if not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY")):
        logger.error("SUPABASE_URL / SUPABASE_KEY が未設定です")
        return 1
    from supabase import create_client
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    summary = backfill(sb, dry_run=dry_run)
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
