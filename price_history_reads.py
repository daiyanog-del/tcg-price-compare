"""price_history をカード名の集まりで直近N日ぶん読む（購入候補の価格・まとめ買い・レアリティ候補が使う）。

2026-10-22 購入候補に31枚入れると全部「価格が見つかりませんでした」になった件で切り出した。
本番ログで見えた失敗は3種類:
  (1) cloudflare の 400 Bad Request — 名前を1回の in_ に全部詰めて URL が長くなる（未検証の前提）
  (2) ConnectionTerminated — HTTP/2 の接続が途中で切れる（一時的）
  (3) statement timeout 57014 — 長い名前を50枚まとめて読むと重い
(1) は featured_matrix と同じバイト予算で名前を分ける、(2) は1回だけ再試行する。
(3) は再試行しない（重い問い合わせを2回投げると負荷を積むだけ）。失敗した名前は呼び出し元に返し、
「値段なし」と区別して扱わせる（CLAUDE.md「0件と取得失敗は区別」）。
(3) の根本は読み方の重さで、名前＋日付の範囲（gte）で読んでいたのをやめ、3か所とも
fetch_daily_rows_by_card_names（カード番号＋日付の等号）に移した（購入候補 PR #204、まとめ買い・レアリティ候補はその次）。
"""

import logging
import time
from collections import deque
from datetime import datetime, timedelta
from concurrent.futures import FIRST_COMPLETED, wait

import httpx

from featured_matrix import _chunk_card_names_by_bytes
from name_normalize import db_lookup_names, pick_card_ids
from featured_matrix import _FETCH_WORKERS
from featured_matrix import _executor as _db_executor

from constants import JST
from shop_status import drop_suspended_rows

# カード番号の in_ の1回あたりの件数。購入候補は最大50枚で、1日ぶん（2026-10-22 実測 長い名前50枚で約400〜600行）が
# 1ページに収まる。featured_matrix の _ID_CHUNK（25）は同時に読む束を増やすための値なので借りない
_ID_CHUNK = 200

# 1回の要求が実行枠に同時に積む本数。枠の本数（同時4本）までにして、1本終わるたびに次の日を積む。
# 全部の日を先に積むと、同じ枠を使う新商品の表がその後ろで待たされる（2026-10-22 reviewer 中1）
_MAX_IN_FLIGHT = _FETCH_WORKERS

# 1回だけ送り直す失敗。接続ごと切られた（HTTP/2 ConnectionTerminated）・つながらなかったときだけ。
# 読み取りの時間切れ（ReadTimeout）は含めない（既定120秒の待ちを2回すると、共有の枠の1本を4分ふさぐ。reviewer 低5）
_RETRY_ERRORS = (httpx.RemoteProtocolError, httpx.ConnectError)

# 1回の読み込み（fetch_daily_rows_by_card_names の呼び出し1回）の締め切り。過ぎたら、まだ読めていない束は failed にして返す
# （DB が重いときに要求を gunicorn の120秒まで引っぱらない。購入候補は8日窓と30日窓で2回呼ぶので合わせて60秒。reviewer 中1）。
# 2026-10-22 本番実測: 32枚 2.7秒・反映直後の冷えた状態で 17.8秒
_READ_DEADLINE_SEC = 30  # TODO: calibrate from data

logger = logging.getLogger(__name__)

_PAGE_SIZE = 1000


def jst_days_back(days: int) -> list[str]:
    """今日（JST）の days 日前から今日までの日付（古い順・days+1 個）。
    以前の `recorded_at >= 今日の days 日前` と同じ日付の集合"""
    today = datetime.now(JST).date()
    return [(today - timedelta(days=d)).isoformat() for d in range(days, -1, -1)]


def _read_day(sb, columns: str, id_chunk: list[int], day: str, log_tag: str) -> list[dict]:
    """カード番号の1束・1日ぶん。1000行を超えたら range で次のページを読む。接続切れ・接続失敗だけ1回再試行。"""
    rows: list[dict] = []
    offset = 0
    while True:
        for attempt in (1, 2):
            try:
                resp = (sb.table("price_history")
                        .select(columns)
                        .in_("card_id", id_chunk)
                        .eq("recorded_at", day)
                        .order("card_id", desc=False)
                        .order("shop_id", desc=False)
                        .order("rarity_id", desc=False)
                        .range(offset, offset + _PAGE_SIZE - 1)
                        .execute())
                break
            except _RETRY_ERRORS as e:
                if attempt == 2:
                    raise
                logger.info(f"[{log_tag}] 価格履歴の読み込みを再試行（{day}）: {type(e).__name__}: {e}")
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < _PAGE_SIZE:
            return rows
        offset += _PAGE_SIZE


def fetch_daily_rows_by_card_names(sb, names: list[str], columns: str, days: list[str],
                                   log_tag: str) -> tuple[list[dict], set[str]]:
    """names の price_history を、days の日付（JST の 'YYYY-MM-DD'）ごとに等号で読む。

    featured_matrix と同じ読み方: card_dim で名前をカード番号にしてから、price_history を
    「日付の等号＋カード番号の in_」で引く。索引 (recorded_at, card_id, shop_id, rarity_id) INCLUDE (min_price)
    の2列とも条件に使わせ、並びも索引と同じ card_id, shop_id, rarity_id にする（1日の中では一意なので
    range の境目で行が落ちない・重ならない）。2026-10-22 本番の実測（長い名前50枚）: 名前の in_＋日付の範囲は
    1ページ 4.8秒×約14ページ、こちらは1日 0.11秒（616行で1ページに収まる）×日数。
    日付どうしは featured_matrix と共有の実行枠で同時に読む（本番 Render オレゴン→DB 東京は1往復0.4〜1秒。
    30日窓を順に待つと31往復になる。同時の問い合わせ数はプロセス全体で featured_matrix._FETCH_WORKERS まで）。
    1回の要求が積むのは _MAX_IN_FLIGHT 本まで（1本終わるたびに次を積む）。
    実行枠のスレッドの中から呼ばないこと（枠の4本が互いの終わりを待って止まる）。

    戻り値: (rows, failed_names)
      rows: 読めた分の行
      failed_names: 読めなかった名前（呼び出し元はこれを「値段なし」ではなく失敗として扱う）
      - 接続切れ・接続失敗（_RETRY_ERRORS）だけ1回再試行。それ以外の失敗は再試行しない
      - カード番号の束のどこか1日でも読めなければ、その束の名前を全部 failed にし、
        読めていた他の日の行も捨てる（半端な日付で最安・値動きを出さない）。その束の残りの日は読みに行かない
      - card_dim に無い名前は行が0件になる（price_history ビューは card_dim との内部結合なので、名前で読んでも同じ）
      - 名前は収集と同じ normalize_card_name を通した形でも引き、行の card_name は呼び出し元の名前に戻す
        （name_normalize.db_lookup_names）。戻すのにカード番号を使うので columns に card_id を含めること
      - _READ_DEADLINE_SEC を過ぎたら、まだ読み終えていない束は failed にして返す（動いている読み込みは捨てるだけ）
    """
    lookup = db_lookup_names(names)  # 収集と同じそろえ方の名前でも引き、行の名前は呼び出し元の名前に戻す
    failed: set[str] = set()
    failed_keys: set[str] = set()
    dim_rows: list[dict] = []
    for chunk in _chunk_card_names_by_bytes(list(lookup)):
        try:
            for attempt in (1, 2):
                try:
                    resp = sb.table("card_dim").select("card_id, card_name").in_("card_name", chunk).execute()
                    break
                except _RETRY_ERRORS as e:
                    if attempt == 2:
                        raise
                    logger.info(f"[{log_tag}] カード番号の読み込みを再試行: {type(e).__name__}: {e}")
        except Exception as e:
            logger.warning(f"[{log_tag}] カード番号の取得失敗（{len(chunk)}枚）: {type(e).__name__}: {e}")
            failed_keys.update(chunk)
            continue
        dim_rows.extend(resp.data or [])
    id_to_name = pick_card_ids(dim_rows, lookup, failed_keys)
    # 読めなかった名前の束に関わる呼び出し元の名前は、カード番号が決まらなければ失敗にする
    # （完全一致で読めたカードは、そろえた名前の束が読めなくても失敗にしない。reviewer 低1）
    found = set(id_to_name.values())
    failed.update(lookup[k] for k in failed_keys if lookup[k] not in found)

    ids = sorted(id_to_name)
    id_chunks = [ids[i:i + _ID_CHUNK] for i in range(0, len(ids), _ID_CHUNK)]
    todo = deque((ci, day) for ci in range(len(id_chunks)) for day in days)
    in_flight = {}  # Future -> (束の番号, 日付)
    chunk_rows: dict[int, list[dict]] = {ci: [] for ci in range(len(id_chunks))}
    failed_chunks: set[int] = set()
    deadline = time.monotonic() + _READ_DEADLINE_SEC
    while todo or in_flight:
        left = deadline - time.monotonic()
        if left <= 0:
            late = ({ci for ci, _d in todo} | {ci for ci, _d in in_flight.values()}) - failed_chunks
            if late:
                logger.warning(f"[{log_tag}] 価格履歴の読み込みが締め切り（{_READ_DEADLINE_SEC}秒）を過ぎた: "
                               f"{sum(len(id_chunks[ci]) for ci in late)}枚を失敗として返す")
            failed_chunks |= late
            break
        while todo and len(in_flight) < _MAX_IN_FLIGHT:
            ci, day = todo.popleft()
            if ci in failed_chunks:
                continue  # 失敗した束の残りの日は読まない（重いときに負荷を積まない）
            in_flight[_db_executor.submit(_read_day, sb, columns, id_chunks[ci], day, log_tag)] = (ci, day)
        if not in_flight:
            break
        done, _ = wait(in_flight, timeout=left, return_when=FIRST_COMPLETED)
        for fut in done:
            ci, day = in_flight.pop(fut)
            try:
                rows = fut.result()
            except Exception as e:
                if ci not in failed_chunks:
                    logger.warning(f"[{log_tag}] 価格履歴取得失敗（{len(id_chunks[ci])}枚・{day}）: {type(e).__name__}: {e}")
                    failed_chunks.add(ci)
                continue
            chunk_rows[ci].extend(rows)

    out: list[dict] = []
    for ci, id_chunk in enumerate(id_chunks):
        if ci in failed_chunks:
            failed.update(id_to_name[c] for c in id_chunk)
        else:
            out.extend(chunk_rows[ci])
    for r in out:
        if r.get("card_id") is not None and int(r["card_id"]) in id_to_name:
            r["card_name"] = id_to_name[int(r["card_id"])]
    # 休止中の店の行は、休止前の観測でも「いま買える値段」として出さない（shop_status.py。
    # 店名で判定するので columns に shop を含めること）
    return drop_suspended_rows(out), failed
