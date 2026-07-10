"""収集ラン記録 (collection_runs) への書き込みを集約するモジュール。

フェーズ3 P5（docs/design-phase3-generation-side.md）。
collect_prices.py / collect_buyback.py の実行1回につき1行を collection_runs に記録し、
「その日その店は観測されていたのか」を今後の検証で復元できるようにする。

collection_runs テーブルが未作成でも収集本体を止めないよう、書き込み失敗は
例外を送出せず warning ログのみ出す（price_persist.upsert_price_rows と同じ方針）。
"""
from __future__ import annotations
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def record_collection_run(
    sb,
    run_type: str,
    started_at: datetime,
    finished_at: datetime,
    attempted_shops: list[str],
    available_shops: list[str],
    skipped_shops: dict[str, str] | None = None,
    shop_stats: dict | None = None,
    success_count: int = 0,
    fail_count: int = 0,
    cutoff_count: int = 0,
    saved_rows: int = 0,
) -> bool:
    """collection_runs に1行 insert する。

    テーブル未作成・書き込みエラー時も収集本体を止めない設計のため、
    例外は握りつぶして warning ログのみ出し False を返す。

    戻り値: 書き込み成功なら True、失敗なら False。
    """
    # row の組み立ても try 内に置く（引数が想定外でも収集本体へ例外を伝播させない）
    try:
        row = {
            "run_type": run_type,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "attempted_shops": list(attempted_shops or []),
            "available_shops": list(available_shops or []),
            "skipped_shops": skipped_shops or {},
            "shop_stats": shop_stats or {},
            "success_count": success_count,
            "fail_count": fail_count,
            "cutoff_count": cutoff_count,
            "saved_rows": saved_rows,
        }
        sb.table("collection_runs").insert(row).execute()
        return True
    except Exception as e:
        logger.warning(f"[collection_run] {run_type} ラン記録失敗（テーブル未作成の可能性）: {e}")
        return False
