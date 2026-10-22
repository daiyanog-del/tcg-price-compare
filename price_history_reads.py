"""price_history をカード名の集まりで直近N日ぶん読む（購入候補の価格・まとめ買い・レアリティ候補が使う）。

2026-10-22 購入候補に31枚入れると全部「価格が見つかりませんでした」になった件で切り出した。
本番ログで見えた失敗は3種類:
  (1) cloudflare の 400 Bad Request — 名前を1回の in_ に全部詰めて URL が長くなる（未検証の前提）
  (2) ConnectionTerminated — HTTP/2 の接続が途中で切れる（一時的）
  (3) statement timeout 57014 — 長い名前を50枚まとめて読むと重い
(1) は featured_matrix と同じバイト予算で名前を分ける、(2) は1回だけ再試行する。
(3) は再試行しない（重い問い合わせを2回投げると負荷を積むだけ）。失敗した名前は呼び出し元に返し、
「値段なし」と区別して扱わせる（CLAUDE.md「0件と取得失敗は区別」）。
"""

import logging

import httpx

from featured_matrix import _chunk_card_names_by_bytes

logger = logging.getLogger(__name__)

_PAGE_SIZE = 1000


def fetch_recent_rows_by_card_names(sb, names: list[str], columns: str, cutoff: str,
                                    order_by: tuple[str, ...], log_tag: str) -> tuple[list[dict], set[str]]:
    """names の price_history を recorded_at >= cutoff で読む。

    戻り値: (rows, failed_names)
      rows: 読めた分の行（失敗した分割の行は途中まで読めていても捨てる。半端な行で最安を出さない）
      failed_names: 読めなかった名前（呼び出し元はこれを「値段なし」ではなく失敗として扱う）
    """
    if not order_by:
        # 並びの無い range はページの境目で行を落とす（監査F8）。呼び出し側の付け忘れを止める
        raise ValueError("order_by を指定してください")
    rows: list[dict] = []
    failed: set[str] = set()
    for chunk in _chunk_card_names_by_bytes(list(dict.fromkeys(names))):
        chunk_rows: list[dict] = []
        offset = 0
        try:
            while True:
                for attempt in (1, 2):
                    try:
                        q = (sb.table("price_history")
                             .select(columns)
                             .in_("card_name", chunk)
                             .gte("recorded_at", cutoff))
                        for col in order_by:
                            q = q.order(col, desc=False)
                        resp = q.range(offset, offset + _PAGE_SIZE - 1).execute()
                        break
                    except httpx.TransportError as e:
                        # 接続切れ・接続失敗・読み取りの時間切れ（ConnectionTerminated はここに入る）。1回だけ再試行
                        if attempt == 2:
                            raise
                        logger.info(f"[{log_tag}] 価格履歴の読み込みを再試行: {type(e).__name__}: {e}")
                batch = resp.data or []
                chunk_rows.extend(batch)
                if len(batch) < _PAGE_SIZE:
                    break
                offset += _PAGE_SIZE
        except Exception as e:
            logger.warning(f"[{log_tag}] 価格履歴取得失敗（{len(chunk)}枚）: {type(e).__name__}: {e}")
            failed.update(chunk)
            continue
        rows.extend(chunk_rows)
    return rows, failed
