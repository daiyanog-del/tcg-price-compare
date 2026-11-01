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

2026-11-01 購入候補を開くと価格とレアリティの2つの API が同じカードの同じ日を同時に読み、共有の実行枠を
取り合って締め切り（30秒）を過ぎ、レアリティの欄（58枚）・まとめ買い（19枚）が丸ごと失敗になっていた。
shared=True の呼び出しどうしは「カード番号×日付」の単位で読んだ行を短い間取っておき、同じものを読んでいる
途中ならその読み込みの終わりを待って使う（新しく読みに行かない）。レアリティ候補は partial_days=True で、
一部の日しか読めなくても読めた日の行を返す（まとめ買い・価格は従来どおり、全部の日が読めたカードだけ）。
"""

import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from concurrent.futures import FIRST_COMPLETED, wait
from functools import partial

import httpx

from featured_matrix import _chunk_card_names_by_bytes
from name_normalize import db_lookup_names, pick_card_ids
from featured_matrix import _FETCH_WORKERS
from featured_matrix import _executor as _db_executor

from constants import JST, PRICE_FLOOR
from shop_status import drop_suspended_rows, is_suspended

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
# （DB が重いときに要求を gunicorn の120秒まで引っぱらない。購入候補は8日窓と15日窓で2回呼ぶので合わせて60秒。予備の3段目の日ごとの最安は締め切りなし・DB 側の8秒で切られる。reviewer 中1）。
# 2026-10-22 本番実測: 32枚 2.7秒・反映直後の冷えた状態で 17.8秒
_READ_DEADLINE_SEC = 30  # TODO: calibrate from data

# shared=True の読み込みが取っておく行の期限。対象は過去の日だけ（今日は「価格を更新」・検索が書いた直後に画面が
# 読み直すので、取っておきも読み込みの使い回しもしない。2026-11-01 reviewer 高1）。過去の日は収集で書き足されない
# （0時をまたぐ収集の書き込みも30分あれば終わる。未検証の前提）
_SHARED_TTL_SEC = 30 * 60  # TODO: calibrate from data
# 取っておく行の数の上限（1行は約0.8KB。3万行で約24MB。2026-11-01 reviewer 中2）。購入候補200枚×8日で約1.6万行
_SHARED_MAX_ROWS = 30000  # TODO: calibrate from data
# shared=True は呼び出し元の columns によらずこの列で読む（取っておいた行を3つの API で使い回すため）。
# 呼び出し元の columns はこの中に収まっていること
_SHARED_COLUMNS = "card_id, shop_id, rarity_id, card_name, rarity, shop, min_price, recorded_at"

# (カード番号, 日付) -> (期限 monotonic, 行)。行は呼び出し元へ渡すたびに写す（呼び出し元が書き換えるため）
_shared_rows: dict[tuple[int, str], tuple[float, list[dict]]] = {}
_shared_row_total = 0
# (カード番号, 日付) -> (読んでいる途中の Future, 始めた時刻)（同じものを2回読みに行かない）
_shared_inflight: dict = {}
# 取っておきを捨てるたびに進める。捨てる前に始まった読み込みの行は取っておかない
_shared_gen = 0
# 登録中に終わった Future の後始末が同じスレッドで走るので再入可能にする
_shared_lock = threading.RLock()

logger = logging.getLogger(__name__)

_PAGE_SIZE = 1000


def _reset_shared_for_tests() -> None:
    global _shared_row_total, _shared_gen
    with _shared_lock:
        _shared_rows.clear()
        _shared_inflight.clear()
        _shared_row_total = 0
        _shared_gen += 1


def _cost(rows: list[dict]) -> int:
    """上限の数え方。行の無いカードも1行と数える（予備の窓で読む行の無いカードが上限の外に積もらないように）"""
    return max(1, len(rows))


def _store_shared(ids: list[int], day: str, gen: int, fut) -> None:
    """shared の読み込みが終わったら、途中の印を外し、読めていれば行をカード番号ごとに取っておく。"""
    global _shared_row_total
    with _shared_lock:
        for cid in ids:
            mark = _shared_inflight.get((cid, day))
            if mark is not None and mark[0] is fut:
                del _shared_inflight[(cid, day)]
        if fut.cancelled() or fut.exception() is not None or gen != _shared_gen:
            return  # 失敗は取っておかない（次の呼び出しが読み直す）
        now = time.monotonic()
        by_id: dict[int, list[dict]] = {cid: [] for cid in ids}
        for r in fut.result():
            cid = r.get("card_id")
            if cid is not None and int(cid) in by_id:
                by_id[int(cid)].append(r)
        # 期限切れは格納のたびに捨てる（上限を超えたときだけにすると、上限近くで張り付く）
        for k in [k for k, (exp, _r) in _shared_rows.items() if exp <= now]:
            _shared_row_total -= _cost(_shared_rows.pop(k)[1])
        for cid, rows in by_id.items():
            old = _shared_rows.get((cid, day))
            if old is not None:
                _shared_row_total -= _cost(old[1])
            _shared_rows[(cid, day)] = (now + _SHARED_TTL_SEC, rows)
            _shared_row_total += _cost(rows)
        # 上限を超えたら期限の近いものから捨てる
        if _shared_row_total > _SHARED_MAX_ROWS:
            for k in sorted(_shared_rows, key=lambda k: _shared_rows[k][0]):
                _shared_row_total -= _cost(_shared_rows.pop(k)[1])
                if _shared_row_total <= _SHARED_MAX_ROWS:
                    break


def _start_read(sb, columns: str, id_chunk: list[int], day: str, log_tag: str, shared: bool):
    """1束・1日の読み込みを始める。
    戻り値: (取っておきから取れた行, [(Future, 使うカード番号の集合 or None=全部)], 自分で読みに行ったか)。
    shared=False はいつも自分で読む。shared=True は 取っておき → 読んでいる途中の Future → 残りだけ新しく読む。"""
    if not shared:
        return [], [(_db_executor.submit(_read_day, sb, columns, id_chunk, day, log_tag), None)], True
    rows: list[dict] = []
    deps: dict = {}  # Future -> 使うカード番号の集合
    need: list[int] = []
    now = time.monotonic()
    with _shared_lock:
        for cid in id_chunk:
            hit = _shared_rows.get((cid, day))
            if hit and hit[0] > now:
                rows.extend(dict(r) for r in hit[1])
                continue
            mark = _shared_inflight.get((cid, day))
            # 締め切りより長く掛かっている読み込みは詰まっているとみて借りず、自分で読み直す（reviewer 中3）
            if mark is not None and now - mark[1] < _READ_DEADLINE_SEC:
                deps.setdefault(mark[0], set()).add(cid)
                continue
            need.append(cid)
        if need:
            # 登録を終えるまで鍵を持つ（終わった Future の後始末が登録より先に走り、印が取り残されないように）
            fut = _db_executor.submit(_read_day, sb, _SHARED_COLUMNS, need, day, log_tag)
            for cid in need:
                _shared_inflight[(cid, day)] = (fut, now)
            deps.setdefault(fut, set()).update(need)
            fut.add_done_callback(partial(_store_shared, need, day, _shared_gen))
    return rows, list(deps.items()), bool(need)


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


def _resolve_card_ids(sb, names: list[str], log_tag: str) -> tuple[dict[int, str], set[str]]:
    """card_dim で names をカード番号にする。戻り値: (カード番号 -> 呼び出し元の名前, 読めなかった名前)。
    収集と同じそろえ方の名前でも引き（name_normalize.db_lookup_names）、名前は呼び出し元の名前に戻す"""
    lookup = db_lookup_names(names)
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
    return id_to_name, failed


def fetch_daily_rows_by_card_names(sb, names: list[str], columns: str, days: list[str],
                                   log_tag: str, *, shared: bool = False,
                                   partial_days: bool = False) -> tuple[list[dict], set[str]]:
    """names の price_history を、days の日付（JST の 'YYYY-MM-DD'）ごとに等号で読む。

    featured_matrix と同じ読み方: card_dim で名前をカード番号にしてから、price_history を
    「日付の等号＋カード番号の in_」で引く。索引 (recorded_at, card_id, shop_id, rarity_id) INCLUDE (min_price)
    の2列とも条件に使わせ、並びも索引と同じ card_id, shop_id, rarity_id にする（1日の中では一意なので
    range の境目で行が落ちない・重ならない）。2026-10-22 本番の実測（長い名前50枚）: 名前の in_＋日付の範囲は
    1ページ 4.8秒×約14ページ、こちらは1日 0.11秒（616行で1ページに収まる）×日数。
    日付どうしは featured_matrix と共有の実行枠で同時に読む（本番 Render オレゴン→DB 東京は1往復0.4〜1秒。
    15日窓を順に待つと16往復になる。同時の問い合わせ数はプロセス全体で featured_matrix._FETCH_WORKERS まで）。
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
    shared=True: 過去の日の行を「カード番号×日付」で _SHARED_TTL_SEC 取っておき、shared=True の他の呼び出しと
      使い回す（読んでいる途中なら終わりを待つ）。今日（と先の日）は毎回読む。列は _SHARED_COLUMNS で読む。
      過去の日を書き換える処理の直後に読み直す呼び出しには使わないこと
    partial_days=True: 束の一部の日が読めなくても（締め切りを過ぎても）、読めた日の行を返す。
      1日も読めなかった束だけ failed にする。最安・値動きのように半端な日付で値が変わるものには使わない
    """
    if shared:
        extra = {c.strip() for c in columns.split(",")} - {c.strip() for c in _SHARED_COLUMNS.split(",")}
        if extra:
            raise ValueError(f"shared=True で読めない列: {sorted(extra)}")
    id_to_name, failed = _resolve_card_ids(sb, names, log_tag)

    ids = sorted(id_to_name)
    id_chunks = [ids[i:i + _ID_CHUNK] for i in range(0, len(ids), _ID_CHUNK)]
    todo = deque((ci, day) for ci in range(len(id_chunks)) for day in days)
    # (束の番号, 日付) -> (取っておきから取れた行, [(Future, 使うカード番号)], 自分で読みに行ったか)
    pending: dict[tuple[int, str], tuple[list[dict], list, bool]] = {}
    chunk_rows: dict[int, list[dict]] = {ci: [] for ci in range(len(id_chunks))}
    chunk_ok_days: dict[int, int] = {ci: 0 for ci in range(len(id_chunks))}
    failed_chunks: set[int] = set()
    deadline = time.monotonic() + _READ_DEADLINE_SEC
    today = datetime.now(JST).date().isoformat()  # 今日（と先の日）は shared でも毎回読む
    while todo or pending:
        left = deadline - time.monotonic()
        if left <= 0:
            late = ({ci for ci, _d in todo} | {ci for ci, _d in pending}) - failed_chunks
            if late and partial_days:
                logger.warning(f"[{log_tag}] 価格履歴の読み込みが締め切り（{_READ_DEADLINE_SEC}秒）を過ぎた")
            elif late:
                logger.warning(f"[{log_tag}] 価格履歴の読み込みが締め切り（{_READ_DEADLINE_SEC}秒）を過ぎた: "
                               f"{sum(len(id_chunks[ci]) for ci in late)}枚を失敗として返す")
                failed_chunks |= late
            break
        # 実行枠に積む本数は自分で読みに行く分だけ数える（他の呼び出しの読み込みを待つだけの分は枠を使わない）
        while todo and sum(1 for v in pending.values() if v[2]) < _MAX_IN_FLIGHT:
            ci, day = todo.popleft()
            if ci in failed_chunks:
                continue  # 失敗した束の残りの日は読まない（重いときに負荷を積まない）
            got, deps, own = _start_read(sb, columns, id_chunks[ci], day, log_tag, shared and day < today)
            if deps:
                pending[(ci, day)] = (got, deps, own)
            else:
                chunk_rows[ci].extend(got)  # 全部取っておきから取れた
                chunk_ok_days[ci] += 1
        if not pending:
            continue
        waiting = {f for _g, deps, _o in pending.values() for f, _ids in deps if not f.done()}
        if waiting:
            wait(waiting, timeout=left, return_when=FIRST_COMPLETED)
        for task in [t for t, (_g, deps, _o) in pending.items() if all(f.done() for f, _ids in deps)]:
            got, deps, _own = pending.pop(task)
            ci, day = task
            try:
                for f, want in deps:
                    res = f.result()
                    got.extend(res if want is None else
                               [dict(r) for r in res if r.get("card_id") is not None and int(r["card_id"]) in want])
            except Exception as e:
                if ci not in failed_chunks:
                    logger.warning(f"[{log_tag}] 価格履歴取得失敗（{len(id_chunks[ci])}枚・{day}）: {type(e).__name__}: {e}")
                    if not partial_days:
                        failed_chunks.add(ci)
                continue
            chunk_rows[ci].extend(got)
            chunk_ok_days[ci] += 1

    if partial_days and days:
        # 1日も読めなかった束だけ失敗にする（読めた日があれば、その日の行で返す）
        none = {ci for ci, n in chunk_ok_days.items() if n == 0}
        failed_chunks |= none
        short = [ci for ci, n in chunk_ok_days.items() if 0 < n < len(days)]
        if none or short:
            logger.warning(f"[{log_tag}] 一部の日が読めなかった: {sum(len(id_chunks[ci]) for ci in none)}枚は失敗・"
                           f"{sum(len(id_chunks[ci]) for ci in short)}枚は読めた日だけで返す")

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


def fetch_daily_min_by_card_names(sb, names: list[str], since: str, log_tag: str) -> tuple[list[dict], set[str]]:
    """names の日ごとの全店最安（price_daily_min・店舗別の行を畳んだ表）のうち since（JST の 'YYYY-MM-DD'）以降の日を返す。

    カード番号だけで主キー (card_id, rarity_id, recorded_at) を引き、日付は読んだ後に絞る。日付の下限を DB に渡すと、
    範囲内の行が少ない（と統計が見る）ときに DB が日付の BRIN 索引で範囲をまるごと読んでから絞る
    （2026-11-01 本番の実行計画で、名前でも番号でも3枚・30日で約9MB 読んだ。番号だけなら人気の5枚・100日ぶん
    約3,800行で 0.2秒）。この読み出しに来るのは最近の値段が無いカードなので行は少ない。
    表は RLS 有効・ポリシーなし（price_daily_rollup.sql）。service_role の鍵（本番の SUPABASE_KEY）でだけ読める
    （anon の鍵だとエラーにならず0件になる）。
    値は PRICE_FLOOR 未満を除いた最安（min_price_over10）。店舗別の読み出しが10円未満の店の行を捨てるのと同じ値になる。
    最安の店（min_shop_ids）は10円未満も含めた最安の店なので、全店最安が10円未満の日は店が分からない（shop を空にする）。
    最安の店がすべて休止中の日は除く（いま買える値段として出さない。次に安い店は畳んだ表に無い）。
    接続切れ・接続失敗（_RETRY_ERRORS）だけ1回再試行する。
    戻り値: (rows, failed_names)。rows は card_name・rarity・shop・min_price・recorded_at（店舗別の行と同じ列）
    """
    id_to_name, failed = _resolve_card_ids(sb, names, log_tag)
    if not id_to_name:
        return [], failed

    def _run(query):
        for attempt in (1, 2):
            try:
                return query().execute().data or []
            except _RETRY_ERRORS as e:
                if attempt == 2:
                    raise
                logger.info(f"[{log_tag}] 日ごとの最安の読み込みを再試行: {type(e).__name__}: {e}")

    try:
        # 名簿は小さい（2026-11-01 本番 rarity_dim 85行・shop_dim 6行）
        rarity_name = {r["rarity_id"]: r["rarity"] for r in
                       _run(lambda: sb.table("rarity_dim").select("rarity_id, rarity").order("rarity_id").range(0, 999))}
        shop_name = {r["shop_id"]: r["shop"] for r in
                     _run(lambda: sb.table("shop_dim").select("shop_id, shop").order("shop_id").range(0, 999))}
    except Exception as e:
        logger.warning(f"[{log_tag}] レアリティ・店の名簿の取得失敗（{len(id_to_name)}枚）: {type(e).__name__}: {e}")
        return [], failed | set(id_to_name.values())
    ids = sorted(id_to_name)
    out: list[dict] = []
    for i in range(0, len(ids), _ID_CHUNK):
        id_chunk = ids[i:i + _ID_CHUNK]
        got: list[dict] = []
        try:
            offset = 0
            while True:
                batch = _run(lambda: (sb.table("price_daily_min")
                                      .select("card_id, rarity_id, min_price, min_price_over10, min_shop_ids, recorded_at")
                                      .in_("card_id", id_chunk)
                                      .order("card_id", desc=False)
                                      .order("rarity_id", desc=False)
                                      .order("recorded_at", desc=False)
                                      .range(offset, offset + _PAGE_SIZE - 1)))
                got.extend(batch)
                if len(batch) < _PAGE_SIZE:
                    break
                offset += _PAGE_SIZE
        except Exception as e:
            logger.warning(f"[{log_tag}] 日ごとの最安の取得失敗（{len(id_chunk)}枚）: {type(e).__name__}: {e}")
            failed.update(id_to_name[c] for c in id_chunk)
            continue
        for r in got:
            day = (r.get("recorded_at") or "")[:10]
            price = r.get("min_price_over10")
            if day < since or price is None:
                continue
            shop = ""
            if (r.get("min_price") or 0) >= PRICE_FLOOR:
                open_shops = [n for n in (shop_name.get(s) for s in (r.get("min_shop_ids") or []))
                              if n and not is_suspended(n)]
                if not open_shops:
                    continue
                shop = open_shops[0]
            out.append({"card_name": id_to_name[int(r["card_id"])],
                        "rarity": rarity_name.get(r.get("rarity_id"), ""),
                        "shop": shop,
                        "min_price": price,
                        "recorded_at": day})
    return out, failed


def fetch_art_contexts_today(sb, names: list[str], today: str, log_tag: str) -> dict[str, dict]:
    """names の当日の絵ごとの内訳（price_history_art）から、カードごとの「絵が分かれるレアリティ」を作る
    （price_persist.art_price_context と同じ形。alt_codes は常に空）。新弾の収集は一部の店だけ取り直すので、その回に取らない店にしか
    違う絵が無いカードでも、取り直した店の内訳を夜の収集（全店で判定）と同じレアリティで入れ直すために使う（2026-11-01）。
    違う絵の型番は渡さない: 当日の内訳には店×レアリティ×絵の最安の型番しか残らず商品名も無いので、1つの型番に元と違う絵が
    並ぶ型番を見分けられず、取り直した元の絵の行まで違う絵にしてしまう（reviewer 指摘）。
    読めなかったカードは含めない（警告だけ。その回は取り直した店の行だけで判定する）"""
    id_to_name, failed = _resolve_card_ids(sb, names, log_tag)
    if failed:
        logger.warning(f"[{log_tag}] 絵ごとの内訳の文脈: カード番号が読めない {len(failed)}枚は取り直した店の行だけで判定する")
    if not id_to_name:
        return {}
    try:
        rows: list[dict] = []
        ids = sorted(id_to_name)
        for i in range(0, len(ids), _ID_CHUNK):
            id_chunk = ids[i:i + _ID_CHUNK]
            offset = 0
            while True:
                batch = (sb.table("price_history_art").select("card_id, rarity_id")
                         .in_("card_id", id_chunk).eq("recorded_at", today)
                         .order("card_id").order("rarity_id").order("art_id").order("shop_id")
                         .range(offset, offset + _PAGE_SIZE - 1).execute().data or [])
                rows.extend(batch)
                if len(batch) < _PAGE_SIZE:
                    break
                offset += _PAGE_SIZE
        if not rows:
            return {}
        rarity_name = {r["rarity_id"]: r["rarity"] for r in
                       sb.table("rarity_dim").select("rarity_id, rarity")
                       .in_("rarity_id", sorted({r["rarity_id"] for r in rows})).execute().data or []}
    except Exception as e:
        logger.warning(f"[{log_tag}] 絵ごとの内訳の文脈の取得失敗（{len(id_to_name)}枚・取り直した店の行だけで判定する）: "
                       f"{type(e).__name__}: {e}")
        return {}
    out: dict[str, dict] = {}
    for r in rows:
        rarity = rarity_name.get(r["rarity_id"])
        if rarity is None:
            logger.warning(f"[{log_tag}] 絵ごとの内訳の文脈: rarity_id={r['rarity_id']} の名前が読めない（その行は使わない）")
            continue
        out.setdefault(id_to_name[r["card_id"]], {"rarities": set(), "alt_codes": set()})["rarities"].add(rarity)
    return out
