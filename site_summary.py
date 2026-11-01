"""
site_summary.py — 重い集計の作り置き（site_summaries 表）を読む。無い・古い・条件違いならその場で RPC を呼ぶ。

背景と仕組みは site_summaries.sql の冒頭（2026-11-01）。要点:
  - 呼び出し側は今までどおり「rpc 名と引数」を渡す。ここで kind ごとに引数を表へ書いておき（依頼）、
    DB の定期実行（pg_cron・10分ごと）がその引数で作り直して payload に置く
  - 使うのは、作ったときの引数（params_used）が今の引数と完全に同じで、作ってから MAX_AGE_SEC 以内のものだけ。
    引数の正本は Python の定数のまま（休止中の店・レアリティの組・日付の区切りを DB に写さない）
  - 表が無い・読めない・古い・条件違いのときは、今までどおりその場で RPC を呼ぶ（例外もそのまま呼び出し側へ）
"""

import json
import logging
import threading
import time

from postgrest.types import ReturnMethod

logger = logging.getLogger(__name__)

TABLE = "site_summaries"
# 作り置きを使う上限の古さ。定期実行は10分ごとなので、数回続けて失敗するまでは使う
MAX_AGE_SEC = 40 * 60  # TODO: calibrate from data
# 同じ理由の警告を出し直す間隔（プロセスごと。読めない・古いが続いていることに気づけるように）
WARN_INTERVAL_SEC = 30 * 60

_lock = threading.Lock()
_registered: dict[str, str] = {}  # kind -> 表へ書いた引数（_key の形）。同じ引数なら書き直さない
_warned_at: dict[str, float] = {}  # 警告の理由 -> 最後に出した時刻


def _key(params: dict) -> str:
    """引数を比べる形（キーの順番・タプルとリストの違いを無くす）"""
    return json.dumps(params, sort_keys=True, ensure_ascii=False, default=sorted)


def _warn(reason: str, message: str) -> None:
    now = time.time()
    with _lock:
        if now - _warned_at.get(reason, 0) < WARN_INTERVAL_SEC:
            return
        _warned_at[reason] = now
    logger.warning(message)


def _register(sb, kind: str, rpc: str, key: str) -> None:
    """kind の引数を表へ書く（作り直しの依頼）。引数が前回書いたものと同じなら何もしない。失敗しても呼び出し側は止めない"""
    with _lock:
        if _registered.get(kind) == key:
            return
    try:
        (sb.table(TABLE).upsert({"kind": kind, "rpc": rpc, "params": json.loads(key)},
                                on_conflict="kind", returning=ReturnMethod.minimal).execute())
    except Exception as e:
        _warn(f"register:{type(e).__name__}",
              f"[site_summary] 作り置きの依頼を書けませんでした（その場で計算を続けます）: {kind}: {e}")
        return
    with _lock:
        _registered[kind] = key


def _cached_payload(sb, kind: str, rpc: str, key: str):
    """(使える作り置きの payload, 作った時刻, 使えない理由)。先に引数と時刻だけ読み、使えるときだけ payload（相場は約700KB）を読む"""
    try:
        rows = (sb.table(TABLE).select("params, params_used, computed_epoch")
                .eq("kind", kind).limit(1).execute().data or [])
    except Exception as e:
        _warn(f"read:{type(e).__name__}", f"[site_summary] 作り置きを読めませんでした（その場で計算します）: {kind}: {e}")
        return None, None, "読めない"
    if not rows:
        return None, None, "まだ無い"
    row = rows[0]
    if row.get("params") is not None and _key(row["params"]) != key:
        # 別のプロセス（デプロイで重なった旧版・違う設定の手元など）が違う引数を書いた。書き直して頼み直す
        _warn(f"conflict:{kind}", f"[site_summary] 作り置きの依頼が別の引数に書き換えられていました（書き直します）: {kind}")
        with _lock:
            _registered.pop(kind, None)
        _register(sb, kind, rpc, key)
    if row.get("params_used") is None or row.get("computed_epoch") is None:
        return None, None, "まだ作られていない"
    if _key(row["params_used"]) != key:
        return None, None, "条件が違う"
    computed = float(row["computed_epoch"])
    age = time.time() - computed
    if age > MAX_AGE_SEC:
        _warn(f"stale:{kind}", f"[site_summary] 作り置きが {age / 60:.0f}分前のままです（定期実行が止まっている疑い・"
                               f"その場で計算します）: {kind}")
        return None, None, "古い"
    try:
        data = sb.table(TABLE).select("payload").eq("kind", kind).limit(1).execute().data or []
    except Exception as e:
        _warn(f"read:{type(e).__name__}", f"[site_summary] 作り置きを読めませんでした（その場で計算します）: {kind}: {e}")
        return None, None, "読めない"
    if not data or data[0].get("payload") is None:
        return None, None, "まだ作られていない"
    return data[0]["payload"], computed, None


def fetch_with_time(sb, kind: str, rpc: str, params: dict):
    """(rpc を params で呼んだ結果, その結果を作った時刻 UNIX 秒)。結果は PostgREST の data と同じ形。
    作り置きが使えればそれと作った時刻、その場で計算したら今の時刻"""
    key = _key(params)
    _register(sb, kind, rpc, key)
    payload, computed, reason = _cached_payload(sb, kind, rpc, key)
    if reason is None:
        return payload, computed
    logger.info(f"[site_summary] {kind}: 作り置き{reason}のためその場で計算")
    return sb.rpc(rpc, params).execute().data, time.time()


def fetch(sb, kind: str, rpc: str, params: dict):
    """rpc を params で呼んだ結果（PostgREST の data と同じ形）。作り置きが使えればそれを返す"""
    return fetch_with_time(sb, kind, rpc, params)[0]
