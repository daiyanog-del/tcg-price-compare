"""
shop_cache_kv.py — 店舗検索キャッシュの Key Value（Redis 互換）置き場

背景（2026-10-22）:
  店舗検索のキャッシュはサーバー内の .cache / .cache_buy に置いていたが、Render は
  デプロイのたびにファイルを消すため、数十分おきのデプロイでキャッシュが空になり、
  店舗へ取りに行く回数が増えていた（本番の集計ログで確認）。DB（Supabase 無料 Nano）は
  10-22 朝に IO の余力切れで止まったので使わず、Render Key Value に置く。

方針:
  - 環境変数 SHOP_CACHE_REDIS_URL が無ければ使わない（呼び出し側はファイルに戻る）
  - Key Value に届かないとき（接続失敗・時間切れ）は例外を外へ出さず None/False を返し、
    DOWN_COOLDOWN_SEC の間は試さない。検索が Key Value の不調で待たされないため。
    期間が明けたら1スレッドだけが試し、ほかはファイルを使い続ける
  - 接続数は MAX_CONNECTIONS まで（無料枠は50接続で、他サービスと相乗りの場合がある）。
    空きを POOL_WAIT_SEC 待っても取れなければ、その呼び出しだけファイルに回す（停止扱いにしない）
  - 使う量に上限（SHOP_CACHE_KV_MAX_BYTES）を設け、超えたら書かない（呼び出し側がファイルへ）。
    allkeys-lru で相乗り先のキーを追い出さないため。使用量を確かめられない（INFO の失敗）ときも書かない
  - 形式: 1カード1ハッシュ。キー = f"{KEY_PREFIX}:{kind}:{md5}"、フィールド = 店舗名、
    値 = zlib 圧縮した JSON {"timestamp", "partial", "results"}。JSON のままより約15分の1
    （手元の実物11件で 85KB → 5KB）。店舗ごとのフィールドなので、別店舗の同時書き込みを
    読み直し・マージせずに済む
  - キーの期限（Redis の TTL）は「前回の結果」を残す期間（呼び出し側の stale 期間）。
    店舗ごとの通常の期限は値の timestamp で呼び出し側が判定する。
    Valkey 8 系にはハッシュのフィールド単位の期限（HEXPIRE）が無いため
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import zlib

logger = logging.getLogger(__name__)

KEY_PREFIX = "tcgprice:shopcache:v1"

# 不調を見つけたら、この秒数は Key Value を試さずファイルを使う
# TODO: calibrate from data — 本番で Key Value の瞬断がどれくらい続くかを見て決める
DOWN_COOLDOWN_SEC = 30.0

# 1回の通信の締め切り。同じリージョンの内部接続なら数ミリ秒で返る想定
# TODO: calibrate from data — 本番の応答時間（p99）を測って決める
SOCKET_CONNECT_TIMEOUT_SEC = 0.3
SOCKET_TIMEOUT_SEC = 0.3

# 同時に使う接続の上限と、空きを待つ秒数
# TODO: calibrate from data — 本番の INFO clients（相乗り先を含む connected_clients）を見て決める
MAX_CONNECTIONS = 8
POOL_WAIT_SEC = 0.2

# 使用量の確かめ直し間隔（INFO memory を毎回は呼ばない）
MEMORY_CHECK_INTERVAL_SEC = 60.0

# 使用量とデータベースごとのキーの数をログに出す間隔（最初の確かめは必ず出す）。
# 相乗り先（営業メールは /0、こちらは /1）のキーの数が減っていないかを見るため。キーの中身は読まない
STATS_LOG_INTERVAL_SEC = 600.0

# 無料枠 25MB の半分。相乗り先の数え上げなどを追い出さない余白を残す
# TODO: calibrate from data — 本番で used_memory の推移を見て決める
DEFAULT_MAX_BYTES = 12 * 1024 * 1024

_lock = threading.Lock()
_client = None
_client_url: str | None = None
_down_until = 0.0       # 0 = 正常。>0 の間は停止中（期限が過ぎたら1スレッドだけ試す）
_mem_checked_at = 0.0
_mem_over = False
_stats_logged_at = 0.0
_last_warn_at: dict[str, float] = {}


def _warn(kind: str, msg: str) -> None:
    """同じ種類の警告は60秒に1回まで（障害中にログを埋めない）"""
    now = time.monotonic()
    if now - _last_warn_at.get(kind, -1e9) >= 60.0:
        _last_warn_at[kind] = now
        logger.warning(msg)


def _url() -> str:
    return os.environ.get("SHOP_CACHE_REDIS_URL", "").strip()


def _max_bytes() -> int:
    try:
        return int(os.environ.get("SHOP_CACHE_KV_MAX_BYTES", "") or DEFAULT_MAX_BYTES)
    except ValueError:
        return DEFAULT_MAX_BYTES


def enabled() -> bool:
    return bool(_url())


def _new_client(url: str):
    import redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry
    pool = redis.BlockingConnectionPool.from_url(
        url,
        max_connections=MAX_CONNECTIONS,
        timeout=POOL_WAIT_SEC,
        socket_connect_timeout=SOCKET_CONNECT_TIMEOUT_SEC,
        socket_timeout=SOCKET_TIMEOUT_SEC,
        # 送り直さない（不調時の待ちを1回の締め切りに収める）
        retry=Retry(NoBackoff(), 0),
    )
    return redis.Redis(connection_pool=pool)


def _get_client():
    """接続プールを1つ作って使い回す（スレッド安全）。未設定・停止中は None。
    停止期間が明けたら、最初に来た1スレッドだけに返す（試し役）。試し役の結果が出るまで、
    ほかのスレッドには停止中として None を返す。"""
    global _client, _client_url, _down_until
    url = _url()
    if not url:
        return None
    if _down_until and time.monotonic() < _down_until:
        return None
    with _lock:
        if _down_until:
            if time.monotonic() < _down_until:
                return None
            # 試し役。結果が出るまでほかのスレッドは停止中のまま
            _down_until = time.monotonic() + DOWN_COOLDOWN_SEC
        if _client is None or _client_url != url:
            try:
                _client = _new_client(url)
                _client_url = url
            except Exception as e:
                _down_until = time.monotonic() + DOWN_COOLDOWN_SEC
                _warn("init", f"[shop-cache-kv] Key Value の設定を読めないためファイルを使います: "
                              f"{type(e).__name__}: {e}")
                return None
        return _client


def _mark_up() -> None:
    global _down_until
    if _down_until:
        _down_until = 0.0
        logger.info("[shop-cache-kv] Key Value に戻りました")


def _mark_down(where: str, err: Exception) -> None:
    global _down_until
    _down_until = time.monotonic() + DOWN_COOLDOWN_SEC
    _warn(f"down:{where}", f"[shop-cache-kv] Key Value に届かないためファイルに戻します "
                          f"({where}: {type(err).__name__}: {err})。{DOWN_COOLDOWN_SEC:.0f}秒後に再試行")


def _handle_error(where: str, err: Exception) -> None:
    """失敗の種類で扱いを分ける。
    - 接続の空き待ちの時間切れ → この呼び出しだけファイルへ（停止扱いにしない。混雑は不調ではない）
    - 接続失敗・時間切れ → 停止扱い（DOWN_COOLDOWN_SEC の間は試さない）
    - それ以外（命令の拒否・型違いなど）→ この呼び出しだけファイルへ・警告"""
    import redis
    if isinstance(err, redis.ConnectionError) and "No connection available" in str(err):
        _warn("pool", f"[shop-cache-kv] 接続の空きが無いため、この読み書きはファイルを使います"
                      f"（上限 {MAX_CONNECTIONS} 本）")
        _mark_up()  # 届いてはいる
        return
    if isinstance(err, (redis.ConnectionError, redis.TimeoutError, OSError)):
        _mark_down(where, err)
        return
    _warn(f"error:{where}", f"[shop-cache-kv] Key Value の{where}に失敗したため、この読み書きはファイルを使います: "
                           f"{type(err).__name__}: {err}")
    _mark_up()  # 届いてはいる


def _key(kind: str, key: str) -> str:
    return f"{KEY_PREFIX}:{kind}:{key}"


def _decode(raw: bytes):
    return json.loads(zlib.decompress(raw).decode("utf-8"))


def _encode(ent: dict) -> bytes:
    return zlib.compress(json.dumps(ent, ensure_ascii=False).encode("utf-8"), 6)


def load(kind: str, key: str, shops: list[str] | None = None) -> dict[str, dict] | None:
    """店舗ごとの生のエントリ {店舗名: {"timestamp","partial","results"}} を返す。
    shops=None なら全店。Key Value を使えないときは None（呼び出し側はファイルを読む）。
    期限の判定はしない（呼び出し側の仕事）。壊れたフィールドは読み飛ばす。"""
    r = _get_client()
    if r is None:
        return None
    try:
        if shops is None:
            raw_map = r.hgetall(_key(kind, key))
            pairs = [(k.decode("utf-8") if isinstance(k, bytes) else k, v) for k, v in raw_map.items()]
        else:
            if not shops:
                return {}
            vals = r.hmget(_key(kind, key), shops)
            pairs = [(s, v) for s, v in zip(shops, vals) if v is not None]
    except Exception as e:
        _handle_error("load", e)
        return None
    _mark_up()
    out: dict[str, dict] = {}
    for shop, raw in pairs:
        try:
            ent = _decode(raw)
        except Exception:
            continue
        if isinstance(ent, dict):
            out[shop] = ent
    return out


def _over_limit(r) -> bool:
    """used_memory が上限を超えていれば True。確かめ直しは MEMORY_CHECK_INTERVAL_SEC ごと。
    INFO が失敗したら、確かめられないので超えている扱い（書かない側）にする。
    接続の失敗は呼び出し側へ投げる（停止扱いにするため）。"""
    global _mem_checked_at, _mem_over
    import redis
    now = time.monotonic()
    if now - _mem_checked_at < MEMORY_CHECK_INTERVAL_SEC:
        return _mem_over
    try:
        used = int(r.info("memory").get("used_memory", 0))
    except (redis.ConnectionError, redis.TimeoutError, OSError):
        raise
    except Exception as e:
        _mem_checked_at, _mem_over = now, True
        _warn("info", f"[shop-cache-kv] Key Value の使用量を確かめられないため、書き込みはファイルに戻します: "
                      f"{type(e).__name__}: {e}")
        return True
    over = used >= _max_bytes()
    _log_stats(r, used, now)
    if over:
        _warn("full", f"[shop-cache-kv] Key Value の使用量 {used / 1048576:.1f}MB が上限 "
                      f"{_max_bytes() / 1048576:.1f}MB を超えたため、書き込みはファイルに戻します")
    _mem_checked_at, _mem_over = now, over
    return over


def _log_stats(r, used: int, now: float) -> None:
    """使用量とデータベースごとのキーの数（INFO keyspace）を STATS_LOG_INTERVAL_SEC ごとにログへ。
    失敗しても書き込みの判断には関わらせない。"""
    global _stats_logged_at
    if _stats_logged_at and now - _stats_logged_at < STATS_LOG_INTERVAL_SEC:
        return
    _stats_logged_at = now
    try:
        ks = r.info("keyspace") or {}
        counts = " ".join(f"{db}={v.get('keys', 0) if isinstance(v, dict) else v}"
                          for db, v in sorted(ks.items())) or "（キーなし）"
    except Exception as e:  # noqa: BLE001
        counts = f"（キーの数を読めず: {type(e).__name__}）"
    logger.info(f"[shop-cache-kv] 使用量 {used / 1048576:.2f}MB / 上限 {_max_bytes() / 1048576:.1f}MB・"
                f"キーの数 {counts}")


def store(kind: str, key: str, entries: dict[str, dict], keep_seconds: int) -> bool:
    """店舗ごとのエントリを書き、キーの期限を keep_seconds に延ばす。
    書けたら True。使えない・上限超え・失敗は False（呼び出し側がファイルに書く）。"""
    global _mem_over, _mem_checked_at
    if not entries:
        return True
    r = _get_client()
    if r is None:
        return False
    try:
        if _over_limit(r):
            _mark_up()  # 届いてはいる（読みは Key Value を使い続ける）
            return False
        k = _key(kind, key)
        # 書き込みと期限を1つの MULTI/EXEC に（途中で切れて期限なしのキーが残らないように）
        pipe = r.pipeline(transaction=True)
        pipe.hset(k, mapping={shop: _encode(ent) for shop, ent in entries.items()})
        pipe.expire(k, int(keep_seconds))
        pipe.execute()
    except Exception as e:
        # 満杯（noeviction の OOM）。次の確かめ直しまで書かない
        if "OOM" in str(e):
            _mem_over, _mem_checked_at = True, time.monotonic()
            _warn("oom", f"[shop-cache-kv] Key Value が満杯のためファイルに戻します: {e}")
            return False
        _handle_error("store", e)
        return False
    _mark_up()
    return True


def _reset_for_tests() -> None:
    global _client, _client_url, _down_until, _mem_checked_at, _mem_over, _stats_logged_at
    with _lock:
        _client = None
        _client_url = None
    _down_until = 0.0
    _stats_logged_at = 0.0
    _mem_checked_at = 0.0
    _mem_over = False
    _last_warn_at.clear()
