"""新弾の店舗×レア表の欠けの見張り（2026-11-01）。

パックの表がほぼ全カード「記録がありません」になる不具合が、11-01 にユーザーのスクショで2件見つかった
（PR #326 今日／前日の行の判定・PR #327 カード名の全角と半角）。どちらも収集は正しく動いていて、
表を作る側で外れていたので、収集の成否の通知では気づけなかった。

そこで、利用者が見るのと同じ道（公開サイトの /api/packs/cards → /api/packs/matrix）で新弾の表を取り、
記録の無いカードの割合が多ければ Discord に知らせる。名前のそろえ方・表の組み立て・キャッシュのどこで
外れても、同じ見え方で拾える。

呼ぶのは collect_featured の 10時台の回だけ（どの段階の弾でも 10時台は動く。collect_featured.should_run_now）。
発売日（0日目）は店の商品ページが順に開くので見ない。
"""

import requests

from discord_notify import send_discord_message
from site_urls import site_url

# 記録の無いカードがこの割合を超えたら知らせる。
# 11-01 の実測: 正常時 0/80 枚（IMMORTAL PHOENIX・発売3日目）、不具合時はほぼ全カード。
# 再録や店に出ない枚数を見込んで 10% にした
MISSING_RATIO_ALERT = 0.10  # TODO: calibrate from data
WATCH_HOUR_JST = 10
_TIMEOUT_SEC = 60


def missing_cards(cards: list[str], matrix: dict) -> list[str]:
    """表（build_matrix の形）で、どのレアリティ・どの店にも値が無いカード（cards の並びのまま）。"""
    has_cell = {r.get("name") for r in matrix.get("rows") or [] if r.get("cells")}
    return [c for c in cards if c not in has_cell]


def should_alert(cards: list[str], missing: list[str]) -> bool:
    return bool(cards) and len(missing) / len(cards) > MISSING_RATIO_ALERT


def check_pack_matrix(pack: dict, *, base_url: str | None = None, session=requests) -> dict:
    """公開サイトの道で新弾の表を取り、欠けが多ければ Discord に知らせる。

    戻り値: {"cards": 枚数, "missing": [カード名], "alerted": bool}。
    取れなかったときは {"error": str} を返し、それも知らせる（表が見えない状態なので）。
    """
    base = (base_url or site_url()).rstrip("/")
    name = pack["pack_name"]
    try:
        r = session.get(f"{base}/api/packs/cards",
                        params={"name": name, "wiki": pack.get("wiki_page") or name, "tcg": pack.get("tcg_name") or ""},
                        timeout=_TIMEOUT_SEC)
        r.raise_for_status()
        cards = [str(c) for c in (r.json().get("cards") or [])]
        if not cards:
            raise RuntimeError("収録カードが0枚")
        m = session.post(f"{base}/api/packs/matrix", json={"cards": cards}, timeout=_TIMEOUT_SEC)
        m.raise_for_status()
        matrix = m.json()
    except Exception as e:
        msg = f"⚠️ 新弾の店舗×レア表を確かめられませんでした（{name}）: {type(e).__name__}: {e}"
        print(f"[表の見張り] {msg}")
        send_discord_message(msg, no_mentions=True)
        return {"error": str(e)}
    missing = missing_cards(cards, matrix)
    alerted = should_alert(cards, missing)
    print(f"[表の見張り] {name}: 記録の無いカード {len(missing)}/{len(cards)}枚")
    if alerted:
        sample = "、".join(missing[:10]) + (f" ほか{len(missing) - 10}枚" if len(missing) > 10 else "")
        send_discord_message(
            f"⚠️ 新弾の店舗×レア表で記録の無いカードが多い: {name} {len(missing)}/{len(cards)}枚"
            f"（基準 {MISSING_RATIO_ALERT:.0%} 超）\n{sample}",
            no_mentions=True,
        )
    return {"cards": len(cards), "missing": missing, "alerted": alerted}
