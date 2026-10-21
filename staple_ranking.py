"""トップのランキング3列目「汎用カード」（2026-10-21〜）

汎用カード（staple_cards・staple_cards.py が週1回作り直す）の最安値と1週間の変化を
RPC get_staple_movers（supabase_rpc_staple_movers.sql）で集計し、2通りに並べる。
  drop   : 1週間で値下がりした金額の大きい順（既定のタブ。割合順だと 50円→20円 のような
           安いカードばかりが上に来るため金額順・2026-10-21 見本で確認）
  themes : 採用テーマが多い順

値段は常に全店の最安値（price_now）を出す。この列は買いたい人が見るため、販売ランキングのように
共通店舗どうしの値を出すと、新しく出品した店のもっと安い値段を隠してしまう
（2026-10-21 本番実測: 黒き竜のエクレシアは全店 ¥3,280・共通店舗では ¥3,980 のまま）。
変化（diff）は、共通店舗どうしで比べられ・定着していて・その最安値が全店の最安値と同じとき
だけ出す（値段と変化の数字が食い違わないように）。それ以外は diff=None（値下がり順に入らない）。
"""

import logging
import threading
import time

import top_page

logger = logging.getLogger(__name__)

CACHE_SEC = top_page.TOP_MOVERS_CACHE_SEC
SORTS = ("drop", "themes")

_cache: dict = {}
_cache_time = 0.0
_lock = threading.Lock()


def build_lists(rows):
    """RPC の行から2通りの並びを作る純関数（ネットワーク不要）。"""
    items = []
    date_new = date_old = None
    for r in rows:
        if date_new is None:
            date_new, date_old = r.get("date_new"), r.get("date_old")
        old, new, now = r.get("price_old"), r.get("price_new"), r.get("price_now")
        comparable = (old is not None and new is not None and old > 0 and bool(r.get("stable"))
                      and now == new)
        price = now
        if comparable:
            diff = new - old
            pct = round(diff / old * 100, 1)
        else:
            diff, pct = None, None
        if not price:
            continue
        items.append({"name": r.get("card_name", ""), "rarity": r.get("rarity") or "",
                      "themes": int(r.get("themes") or 0), "price": price, "diff": diff, "pct": pct})
    drop = sorted((i for i in items if i["diff"] is not None and i["diff"] < 0),
                  key=lambda i: (i["diff"], i["pct"], i["name"]))
    themes = sorted(items, key=lambda i: (-i["themes"], i["name"]))
    return {"drop": drop, "themes": themes, "date_new": date_new, "date_old": date_old,
            "total": len(items)}


def get_cached(sb, force=False):
    """CACHE_SEC 秒キャッシュ。取得に失敗したら前回の結果（無ければ error 付きの空）を返す。"""
    global _cache, _cache_time
    now = time.time()
    if not force and _cache and now - _cache_time < CACHE_SEC:
        return _cache
    if sb is None:
        return _cache or {"drop": [], "themes": [], "error": "価格データベースに接続できません"}
    if not _lock.acquire(blocking=False):
        return _cache or {"drop": [], "themes": [], "error": "計算中です。しばらくしてから再度お試しください"}
    try:
        rows = sb.rpc("get_staple_movers", {}).execute().data or []
        if not rows:
            return _cache or {"drop": [], "themes": [], "error": "データがまだありません"}
        result = {**build_lists(rows), "error": None}
        _cache, _cache_time = result, now
        return result
    except Exception as e:
        logger.warning(f"汎用カードの集計(RPC)取得失敗: {e}")
        return _cache or {"drop": [], "themes": [], "error": "取得に失敗しました"}
    finally:
        _lock.release()
