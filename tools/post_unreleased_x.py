"""
post_unreleased_x.py — 承認済み・未投稿の未発売カードを X に投稿する回収スクリプト

背景:
  承認時の自動投稿（unreleased_x_post.start_post_thread / post_cards_sequential）は、
  画像取込に失敗した・Renderが再起動してスレッドが途切れた・X APIが一時的に失敗した、
  といった場合に取りこぼす。backfill-images.yml が画像を回収した後にこのスクリプトを実行し、
  「投稿条件を満たし、未投稿（unreleased_x_posts に行なし）か failed で attempts<3」のカードを投稿する。
  導入前から承認済みのカードは、マイグレーション（unreleased_x_posts.sql）で skipped_legacy 済みのため対象外。

実行:
  python tools/post_unreleased_x.py            # 実投稿（UNRELEASED_X_POST_ENABLED=1 のときだけ）
  python tools/post_unreleased_x.py --list     # 対象一覧の確認のみ（投稿しない）

環境変数:
  SUPABASE_URL / SUPABASE_KEY / X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN / X_ACCESS_TOKEN_SECRET
  UNRELEASED_X_POST_ENABLED=1（既定は無効）/ X_POST_DRY_RUN=1（本文をログに出すだけ）
"""

import io
import logging
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("post_unreleased_x")


def main() -> int:
    if not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY")):
        logger.error("SUPABASE_URL / SUPABASE_KEY が未設定です")
        return 1

    from supabase import create_client
    import unreleased_x_post as xp

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

    if "--list" in sys.argv:
        ids = xp.find_backfill_candidates(sb)
        logger.info("回収候補: %d件 %s", len(ids), ids)
        return 0

    result = xp.run_backfill(sb)
    logger.info("回収結果: %s", result)
    # 投稿失敗は xp 側で Discord 通知済み。ジョブ自体は成功扱いにして次回の再試行に任せる
    return 0


if __name__ == "__main__":
    # 日本語出力のため stdout を UTF-8 化（import 時には触らない: pytest の出力捕捉を壊すため）
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
