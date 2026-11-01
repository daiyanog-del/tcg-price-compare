"""カード個別ページ（/card/<name>）の HTML に最初から書いておく価格（2026-11-01）。

検索エンジンは最初に返した HTML を読む。カードのページは価格を画面が後から取りに行くので、
HTML にはカード名しか無く、Search Console でカードのページがほぼ登録されていなかった
（登録済み7ページ・カードの表示回数ほぼ0）。毎日集めている price_history の直近の日から
「店ごとの最安」を読み、HTML の先出しの欄（#searchPreview）とページの説明文・構造化データに入れる。

読み方:
  - price_history（card_dim と結合したビュー）をカード名で1回だけ読む（直近 _LOOKBACK_DAYS 日・新しい日から）。
    本番は Render（オレゴン）→ DB（東京）で1往復 0.4〜1秒かかるので、ページの応答は _WAIT_SEC までしか待たない。
    間に合わなければ価格なしで返し、読み込みはそのまま続けて取っておく（次にそのページを開いた人・検索エンジンから出る）。
  - 読んだ結果は _CACHE_TTL_SEC だけ、最大 _CACHE_MAX 枚まで取っておく（上限を超えたら古い順に捨てる）。
    価格が無かった（記録なし）結果も取っておく。読めなかった（失敗）結果は取っておかない。
"""
import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timedelta

from constants import JST, PRICE_FLOOR
from name_normalize import db_lookup_names
from shop_status import drop_suspended_rows

logger = logging.getLogger(__name__)

# 何日前までの記録を見るか。相場キャッシュ（_load_estimate_cache）と同じ7日
_LOOKBACK_DAYS = 7
# ページの応答が DB を待つ上限。1往復 0.4〜1秒（2026-10-22 本番実測・price_history_reads）の大半が収まる値
_WAIT_SEC = 0.8  # TODO: calibrate from data
# 1枚ぶんは店×レアリティ×日で、灰流うらら（レアリティの多いカード）でも3日で162行（2026-11-01 実測）。
# 1ページはこのプロジェクトの PostgREST の返却上限（1000行）。_MAX_PAGES は暴走ガード（8日で3000行を超えるカードは無い）
_PAGE = 1000
_MAX_PAGES = 3
_CACHE_TTL_SEC = 60 * 60  # TODO: calibrate from data
# 1枚ぶんは店の数（6）までの小さな dict なので、1000枚でも数 MB に収まる
_CACHE_MAX = 1000  # TODO: calibrate from data

# 読み込み専用の枠。ページの要求のスレッドは待つのをやめても、読み込みはここで最後まで走らせて取っておく。
# 本数を絞るのは、検索エンジンがまとめて読みに来ても DB への同時の問い合わせを増やしすぎないため
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="card-ssr")

_lock = threading.Lock()
_cache: "OrderedDict[str, tuple[float, dict | None]]" = OrderedDict()
_inflight: dict[str, object] = {}


def _reset_for_tests() -> None:
    with _lock:
        _cache.clear()
        _inflight.clear()


def _cache_get(name: str):
    """(あるか, 値)。期限切れは捨てる"""
    with _lock:
        hit = _cache.get(name)
        if not hit:
            return False, None
        at, value = hit
        if time.time() - at > _CACHE_TTL_SEC:
            del _cache[name]
            return False, None
        _cache.move_to_end(name)
        return True, value


def _cache_put(name: str, value: dict | None) -> None:
    with _lock:
        _cache[name] = (time.time(), value)
        _cache.move_to_end(name)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)


def _md(day: str) -> str:
    d = datetime.strptime(day, "%Y-%m-%d")
    return f"{d.month}/{d.day}"


def summarize(rows: list[dict], name: str) -> dict | None:
    """price_history の行 → 店ごとの最安（その店の一番新しい日の行から）。記録が無ければ None。

    店ごとに最新の日を取るのは、今日の行は収集の途中・検索のついでに書かれ、店によって揃っていないため
    （2026-11-01 灰流うらら: 今日の行に遊々亭のノーマルが無く 10/31 にだけある）。全体の一番新しい日で切ると店が抜ける。
    日付が店で違うときは date_label を「10/31〜11/1」と範囲で書く（いつの価格か分かるように）。
    名前は呼び出し元の名前で完全一致した行を優先し、無いときだけ表記をそろえた名前の行を使う
    （name_normalize.pick_card_ids と同じ考え方。全角と半角で2行に分かれたカードの記録を混ぜない）。
    休止中の店・PRICE_FLOOR 未満は除く。
    """
    exact = [r for r in rows if r.get("card_name") == name]
    use = exact or rows
    use = [r for r in drop_suspended_rows(use)
           if r.get("shop") and isinstance(r.get("min_price"), int) and r["min_price"] >= PRICE_FLOOR
           and r.get("recorded_at")]
    if not use:
        return None
    shop_day: dict[str, str] = {}
    for r in use:
        if r["recorded_at"] > shop_day.get(r["shop"], ""):
            shop_day[r["shop"]] = r["recorded_at"]
    best_by_shop: dict[str, dict] = {}
    for r in use:
        if r["recorded_at"] != shop_day[r["shop"]]:
            continue
        cur = best_by_shop.get(r["shop"])
        if cur is None or r["min_price"] < cur["price"]:
            best_by_shop[r["shop"]] = {"shop": r["shop"], "rarity": r.get("rarity") or "",
                                       "price": r["min_price"], "date": r["recorded_at"]}
    shops = sorted(best_by_shop.values(), key=lambda s: (s["price"], s["shop"]))
    newest, oldest = max(shop_day.values()), min(shop_day.values())
    return {
        "date": newest,
        "date_label": _md(newest) if newest == oldest else f"{_md(oldest)}〜{_md(newest)}",
        "shops": shops,
        "low": shops[0]["price"],
        "high": shops[-1]["price"],
        "best": shops[0],
    }


def _read(sb, name: str) -> dict | None:
    """DB から読んで要約する。読めなければ例外（呼び出し元で取っておかない）"""
    since = (datetime.now(JST).date() - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    names = list(db_lookup_names([name]))
    # 1枚の8日ぶんは通常1ページ（数百行）に収まる。超えたときも range で続きを読む
    # （tests/test_price_history_read_discipline.py: .limit() は返却上限で黙って切れるので使わない・並びは一意な複合キー）
    rows: list[dict] = []
    for page in range(_MAX_PAGES):
        resp = (sb.table("price_history")
                .select("card_name,shop,rarity,min_price,recorded_at")
                .in_("card_name", names)
                .gte("recorded_at", since)
                .order("recorded_at", desc=True).order("card_name").order("shop").order("rarity")
                .range(page * _PAGE, page * _PAGE + _PAGE - 1)
                .execute())
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < _PAGE:
            break
    return summarize(rows, name)


def _read_and_store(sb, name: str) -> dict | None:
    # 取っておいてから「読み込み中」を外す（逆だと、その間に来た要求がもう一度読みに行く）
    try:
        value = _read(sb, name)
        _cache_put(name, value)
        return value
    except Exception:
        logger.exception("カードのページの価格の読み込み失敗: %s", name)
        raise
    finally:
        with _lock:
            _inflight.pop(name, None)


def get_snapshot(sb, name: str) -> dict | None:
    """カードの直近の店ごとの最安（summarize の形）。記録なし・読めない・待ち切れないときは None。

    待ち切れなかった読み込みは裏で続け、終わったら取っておく（次の表示から出る）。
    同じカードの読み込みが動いていれば、新しく読まずにそれを待つ。
    """
    if not sb or not name:
        return None
    found, value = _cache_get(name)
    if found:
        return value
    with _lock:
        fut = _inflight.get(name)
        if fut is None:
            fut = _executor.submit(_read_and_store, sb, name)
            _inflight[name] = fut
    try:
        return fut.result(timeout=_WAIT_SEC)
    except FutureTimeout:
        logger.info("カードのページの価格: %.1f秒で読み終わらず価格なしで返す（裏で続ける）: %s", _WAIT_SEC, name)
        return None
    except Exception:
        return None
