"""
refetch_missing_images.py — 承認済みなのに画像が未登録の未発売カードを一括再取得する一回限りの復旧スクリプト

背景:
  承認処理（admin_unreleased.approve / bulk-approve）は「画像取込失敗は承認自体を
  失敗させない」仕様のため、承認時の画像取得が（バックグラウンドスレッドの中断・
  一時的なネットワーク失敗等で）こぼれると「承認済み・画像なし」のカードが残り、
  マイデッキ/一人回しでプロキシ表示になる。

  これらのカードは extraction_raw に元画像URL（card_image_url）が残っているため、
  本スクリプトで承認時と同じ _fetch_and_store_image を呼び直せば復旧できる。

対象:
  status IN ('approved','linked') AND hidden=false
  かつ official_card_images に可視レコード（hidden=false, deleted_at IS NULL）が無い
  かつ extraction_raw.card_image_url が非空

冪等性:
  可視画像が無いカードだけを対象にするため、再実行しても既に復旧済みのカードは
  スキップされる（official_card_images への重複INSERTを避ける）。

実行:
  python tools/refetch_missing_images.py            # 実取得（本番Supabaseへ書き込み）
  python tools/refetch_missing_images.py --dry-run  # 対象一覧の確認のみ

認証:
  環境変数 SUPABASE_URL / SUPABASE_KEY を使う。未設定時は
  ~/.cardsouba/watcher.env から読み込む（ローカル実行の利便のため）。
"""

import io
import os
import sys
import logging

# Windows コンソールで日本語を出すため stdout を UTF-8 化する
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
except Exception:
    pass

# tcg-web 直下のモジュール（admin_unreleased 等）を import できるようにする
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _load_env_fallback() -> None:
    """SUPABASE_URL/KEY が未設定なら ~/.cardsouba/watcher.env から補完する。"""
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
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            # 既に環境変数があればそちらを優先（上書きしない）
            if key and key not in os.environ:
                os.environ[key] = val


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("refetch_missing_images")


def _find_missing_image_cards(sb) -> list[dict]:
    """
    承認済み・hidden=false かつ可視画像が無く、元画像URLを持つカードを返す。
    返り値: [{"id", "name", "source_url", "image_url"}, ...]
    """
    # 1. 承認済み（approved/linked）かつ hidden=false のカード
    cards_resp = (
        sb.table("unreleased_cards")
        .select("id, name, source_url, extraction_raw, status")
        .in_("status", ["approved", "linked"])
        .eq("hidden", False)
        .execute()
    )
    cards = cards_resp.data or []
    if not cards:
        return []

    # 2. 可視画像（hidden=false, deleted_at IS NULL）を持つカードIDの集合
    ids = [c["id"] for c in cards]
    imgs_resp = (
        sb.table("official_card_images")
        .select("unreleased_card_id")
        .in_("unreleased_card_id", ids)
        .eq("hidden", False)
        .is_("deleted_at", "null")
        .execute()
    )
    have_image = {row["unreleased_card_id"] for row in (imgs_resp.data or [])}

    # 3. 画像が無く、extraction_raw に card_image_url を持つカードを抽出
    missing = []
    for c in cards:
        if c["id"] in have_image:
            continue
        raw = c.get("extraction_raw") or {}
        url = (raw.get("card_image_url") or "").strip()
        if not url:
            # 個別URLが無ければ記事全体の画像一覧の先頭をフォールバックに使う
            # （admin_unreleased._try_fetch_image_from_extraction と同じ優先順）
            urls = raw.get("card_image_urls") or []
            if isinstance(urls, list) and urls:
                url = (urls[0] or "").strip()
        if not url:
            logger.warning(f"  スキップ（画像URLなし）: id={c['id']} {c['name']}")
            continue
        missing.append(
            {
                "id": c["id"],
                "name": c["name"],
                "source_url": c.get("source_url", "") or "",
                "image_url": url,
            }
        )
    return missing


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    _load_env_fallback()
    if not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY")):
        logger.error("SUPABASE_URL / SUPABASE_KEY が未設定です")
        return 1

    from supabase import create_client

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

    targets = _find_missing_image_cards(sb)
    logger.info(f"画像未登録カード: {len(targets)} 件")
    for t in targets:
        logger.info(f"  - id={t['id']:>4} {t['name']}  <- {t['image_url']}")

    if dry_run:
        logger.info("--dry-run のため取得は行いません")
        return 0

    if not targets:
        logger.info("対象なし。終了します")
        return 0

    # 承認時と同一の取得・保存ロジックを再利用する（env 設定後に import すること）
    from admin_unreleased import _fetch_and_store_image

    ok, ng = 0, 0
    for t in targets:
        success, reason = _fetch_and_store_image(
            card_id=t["id"],
            image_url=t["image_url"],
            source_page_url=t["source_url"],
        )
        if success:
            ok += 1
            logger.info(f"  ✅ id={t['id']} {t['name']}: {reason}")
        else:
            ng += 1
            logger.error(f"  ❌ id={t['id']} {t['name']}: {reason}")

    logger.info(f"完了: 成功 {ok} 件 / 失敗 {ng} 件")
    return 0 if ng == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
