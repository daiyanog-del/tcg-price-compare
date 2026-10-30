"""オーバーフレーム（OF）のレアリティを店のあいだでそろえる（2026-10-30）。

背景: 遊々亭とカードラボは OF の商品もレアリティ欄に「プリシク」「ウルトラ」などと書き、
OF かどうかを商品名にしか出さない。ほかの店（カードラッシュ・トレコロCB・カーナベル・
まんぞく屋）は「OFプリシク」と書くので、同じ商品が別の札に分かれていた
（本番の記録で約85型番。真紅眼の超越黒竜 BETB-JP036 でユーザー指摘）。

  - 遊々亭  : 商品名の末尾に「(オーバーフレーム)」。印の無い同じ型番は普通の枠
  - カードラボ: 同じ型番・同じレアリティに OF と普通の枠の両方があるときだけ
                「〔オーバーフレーム〕」「〔通常フレーム〕」と書く。OF しか無い型番
                （BETB-JP036 のプリシクなど）は印が無い

そこで2段で寄せる。
  1. mark_overframe: 商品名に OF の印があれば、取得時にレアリティへ OF を付ける（各店のスクレイパー）
  2. infer_overframe: 同じカードの全店の行がそろった直後（型番補完 infer_codes の後・保存前）に、
     カードラボの印の無い行を、ほかの店の行で判定する。遊々亭がその型番を OF の印つきで出し、
     遊々亭もほかの店も普通の枠と書いていないときだけ OF にする

根拠に遊々亭を必須にする理由（10-30 reviewer 指摘）: 同じ型番・同じレアリティに OF と普通の枠の
両方がある商品はある（古の秘儀 UT01-JP001 のウルトラ）。「普通の枠と書く店が無い」だけだと、
店を絞った検索・取得失敗・1ページ目だけの取得で普通の枠の行が無い回に、カードラボの普通の枠を
OF にして保存・キャッシュしてしまう。遊々亭は印の有る無しで両方の枠を書き分け、売り切れの商品も
出すので、遊々亭の行がそろっているときだけ判定する。遊々亭が取れなかった回は寄せない（今までどおり）。
"""
from __future__ import annotations

import re

from card_code import looks_like_card_code

# 普通の枠のレアリティ → OF のレアリティ（rarity.py の正規名）
OF_RARITY = {
    "プリシク": "OFプリシク",
    "シークレット": "OFシークレット",
    "ウルトラ": "OFウルトラ",
    "スーパー": "OFスーパー",
}

# 商品名の OF の印（遊々亭「(オーバーフレーム)」・カードラボ「〔オーバーフレーム〕」）
_OF_MARK_RE = re.compile(r"[(（〔\[［【]\s*オーバーフレーム\s*[)）〕\]］】]")
# カードラボが OF と並べるときに普通の枠に付ける印
_NORMAL_MARK_RE = re.compile(r"[(（〔\[［【]\s*通常フレーム\s*[)）〕\]］】]")

# レアリティ欄で OF を書き分けない店（商品名の印か、ほかの店の行で判定する）
UNMARKED_SHOP = "カードラボ"
# カードラボの印の無い行を OF と判定する根拠にする店（商品名の印で両方の枠を書き分ける）
WITNESS_SHOP = "遊々亭"


def mark_overframe(rarity: str, name: str) -> str:
    """商品名に OF の印があれば、普通の枠のレアリティを OF のレアリティにする（rarity は正規名）。"""
    if rarity in OF_RARITY and _OF_MARK_RE.search(name or ""):
        return OF_RARITY[rarity]
    return rarity


def overframe_context(rows: list[dict]) -> dict[str, dict[str, set]]:
    """カードラボ以外の店の行から、型番ごとに「遊々亭が OF の印つきで出しているレアリティ」
    「どこかの店が普通の枠と書いたレアリティ」を集める（中身の無い型番は入れない）。

    返り値: {型番: {"of": {普通の枠の名前, ...}, "plain": {普通の枠の名前, ...}}}
    "of" には OF の名前ではなく、それに当たる普通の枠の名前（"プリシク" など）を入れる。
    夜の収集はカードラボだけ後から別に保存するので、これを残してカードラボの行の判定に使う。
    """
    of_to_plain = {v: k for k, v in OF_RARITY.items()}
    ctx: dict[str, dict[str, set]] = {}
    for r in rows:
        if r.get("shop") == UNMARKED_SHOP:
            continue
        code = (r.get("code") or "").strip()
        if not looks_like_card_code(code):
            continue
        rarity = mark_overframe(r.get("rarity", ""), r.get("name", ""))
        if rarity in of_to_plain:
            if r.get("shop") == WITNESS_SHOP:
                ctx.setdefault(code, {"of": set(), "plain": set()})["of"].add(of_to_plain[rarity])
        elif rarity in OF_RARITY:
            ctx.setdefault(code, {"of": set(), "plain": set()})["plain"].add(rarity)
    return ctx


def apply_overframe(rows: list[dict], context: dict[str, dict[str, set]] | None) -> list[dict]:
    """カードラボの印の無い行を context（overframe_context の返り値）で OF に寄せた新しいリストを返す。

    全店の行に商品名の印の判定（mark_overframe）もかける（印を付ける前のキャッシュの行のため）。
    引数の rows は変更しない。順序と件数は変えない（呼び元が後ろの切り出しで店の内訳を作るため）。
    """
    out = []
    for r in rows:
        row = dict(r)
        name = row.get("name", "")
        rarity = mark_overframe(row.get("rarity", ""), name)
        if (context and row.get("shop") == UNMARKED_SHOP and rarity in OF_RARITY
                and not _NORMAL_MARK_RE.search(name or "")):
            entry = context.get((row.get("code") or "").strip())
            if entry and rarity in entry["of"] and rarity not in entry["plain"]:
                rarity = OF_RARITY[rarity]
        row["rarity"] = rarity
        out.append(row)
    return out


def infer_overframe(rows: list[dict]) -> list[dict]:
    """同じカードの全店ぶんの行（infer_codes の後）で OF のレアリティをそろえた新しいリストを返す。"""
    return apply_overframe(rows, overframe_context(rows))
