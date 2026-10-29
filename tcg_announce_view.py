"""海外先行の予告カードのまとめページ（/tcg-preview/<slug>）と OG 画像の読み出し。

tools/tcg_announce_watch.py が書いた tcg_announce_sets / tcg_announce_cards を読む（書き込みはしない）。
Supabase の接続は card_display のものを使う。1時間ごとにしか増えないので 60 秒だけメモリに持つ。
"""
import logging
import threading
import time

import card_display

logger = logging.getLogger(__name__)

TTL = 60
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "data": None}


def available() -> bool:
    return card_display._supabase_client is not None


PAGE = 1000  # Supabase（PostgREST）が1回に返す行数の上限


def fetch_all(query) -> list[dict]:
    """1000行ずつ取って全部返す（上限で黙って途中から切れないように）。query は order 済みのもの。"""
    out, start = [], 0
    while True:
        rows = query.range(start, start + PAGE - 1).execute().data or []
        out += rows
        if len(rows) < PAGE:
            return out
        start += PAGE


def _fetch() -> dict:
    sb = card_display._supabase_client
    # 追いかけを終えた弾（closed）は出さない（入口・サイトマップにも残さない）
    sets = fetch_all(sb.table("tcg_announce_sets").select("*").eq("hidden", False).eq("status", "active").order("prefix"))
    cards = fetch_all(sb.table("tcg_announce_cards").select("*").eq("hidden", False).eq("status", "announced")
                      .order("page_id"))
    by_set: dict[str, list] = {}
    for c in cards:
        by_set.setdefault(c["set_prefix"], []).append(c)
    out = {}
    for s in sets:
        cs = by_set.get(s["prefix"], [])
        if not cs:
            continue  # 予告カードが全部公式DBへ移った弾は出さない（/tcg-preview の一覧が引き継ぐ）
        out[s["slug"]] = {"set": s, "cards": order_cards(cs)}
    return out


def order_cards(cards: list[dict]) -> list[dict]:
    """効果の分かるカードを先に、名前だけのカードを後に。同じ中では一覧の並び。"""
    return sorted(cards, key=lambda c: (c.get("lore_kind") == "none", c.get("list_order") or 0))


def _data() -> dict:
    now = time.monotonic()
    with _lock:
        if _cache["data"] is not None and now - _cache["at"] < TTL:
            return _cache["data"]
    try:
        data = _fetch()
    except Exception:
        # DB が少し不調なだけなら、前に読んだ中身で答える（毎回 DB を待たせない）。一度も読めていなければ例外
        if _cache["data"] is not None:
            logger.warning("予告カードの取得に失敗したため、前に読んだ中身を使います", exc_info=True)
            with _lock:
                _cache["at"] = time.monotonic()
            return _cache["data"]
        raise
    with _lock:
        _cache.update(at=time.monotonic(), data=data)
    return data


def list_sets() -> list[dict]:
    """予告カードのある弾（発売日の早い順）。取得失敗は例外（呼び出し側で 503 にする）。"""
    if not available():
        return []
    items = list(_data().values())
    return sorted(items, key=lambda x: (x["set"].get("tcg_release_date") or "9999", x["set"]["name_en"]))


def get_set(slug: str) -> dict | None:
    if not available():
        return None
    return _data().get(slug)


def anchor(card: dict) -> str:
    """まとめページの中のカードの位置（#mzmc-en005。番号が未定なら名前から）。"""
    code = card.get("set_code") or ""
    if code and "?" not in code:
        return code.lower()
    import tcg_announce
    return "c-" + tcg_announce.slugify(card.get("name_en") or "")
