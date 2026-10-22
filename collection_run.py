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
    finished_at: datetime | None,
    attempted_shops: list[str],
    available_shops: list[str],
    skipped_shops: dict[str, str] | None = None,
    shop_stats: dict | None = None,
    success_count: int = 0,
    fail_count: int = 0,
    cutoff_count: int = 0,
    saved_rows: int = 0,
    aborted: dict | None = None,
) -> bool:
    """collection_runs に1行 insert する。

    途中で打ち切った（異常終了の）ランは finished_at=None・aborted={理由などの dict} で渡す。
    finished_at は NULL のまま（collection_runs.sql の注記「NULL=異常終了」に合わせる）、
    aborted は shop_stats JSON の "_aborted" キーに入れる（列を足さずに残すため）。

    テーブル未作成・書き込みエラー時も収集本体を止めない設計のため、
    例外は握りつぶして warning ログのみ出し False を返す。

    戻り値: 書き込み成功なら True、失敗なら False。
    """
    # row の組み立ても try 内に置く（引数が想定外でも収集本体へ例外を伝播させない）
    try:
        row = {
            "run_type": run_type,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat() if finished_at else None,
            "attempted_shops": list(attempted_shops or []),
            "available_shops": list(available_shops or []),
            "skipped_shops": skipped_shops or {},
            "shop_stats": {**(shop_stats or {}), **({"_aborted": aborted} if aborted else {})},
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


# ホスト名 -> shop_stats 上の店舗名（アクセス回数を既存の shop_stats JSON に載せる対象）
HOST_TO_SHOP = {"www.torecolo.jp": "トレコロCB"}


def attach_host_request_counts(shop_stats: dict, counts: dict) -> list[str]:
    """scraper.get_host_request_counts() の結果を shop_stats の該当店舗に
    "requests"（実HTTP送信数）・"skipped"（ブレーカでスキップ数）として足し、
    終了ログ用の行リストを返す。

    shop_stats は collection_runs.shop_stats（JSON列）にそのまま入るため、スキーマ変更は不要。
    既存の表示ループは s['ok'] 等しか読まないので追加キーがあっても影響しない。
    ログにはカウントされた全ホストを出す（店舗に対応しないホストも負担把握のため）。
    """
    lines = []
    for host, c in sorted(counts.items()):
        lines.append(f"  {host}: リクエスト {c['requests']}回 / ブレーカでスキップ {c['skipped']}回")
        shop = HOST_TO_SHOP.get(host)
        if shop:
            agg = shop_stats.setdefault(shop, {"ok": 0, "empty": 0, "error": 0})
            agg["requests"] = c["requests"]
            agg["skipped"] = c["skipped"]
    return lines
