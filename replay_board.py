"""
replay_board.py — 共有リプレイの「最後の盤面（board）」の検証

クライアント（static/solitaire/js/services/replay-board.js）が共有時に POST する board を
保存前に検証・正規化する。board はリンク画像（OGP）の描画専用で、不備があっても
リプレイ自体の保存は失敗させない設計のため、構造が不正なら None を返して呼び出し側で捨てる。
カード1枚だけが不正な場合は、そのカードだけを除外して board は残す。

board の形（カードは名前と種類だけ。画像 URL は受け取らない）:
  {v:1, moves:int,
   field:[{slot:'custom-slot-N', name, token?:true, pos:'atk'|'def', face:'up'|'down', n?:int}],
   hand:[{name, token?}], grave:{count, top:{name, token?, face?:'down'}|None}, banish:{count, top},
   ex:{count}, deckCount:int}

img など未知のキーは捨てる。画像はサーバーがカード名から解決するため、利用者が指定した URL を
サーバーが取りに行くことはない（任意URL取得＝SSRF と、名前で引く共通キャッシュの汚染を避ける）。
token は data: 画像を持つトークン（名前入りの枠で描く）。
"""

import re

# 上限値。TODO: calibrate from data（実リプレイの分布未収集のため仮置き）
BOARD_MAX_FIELD = 20      # 実盤面は14マス。余裕を見て20
BOARD_MAX_HAND = 60
BOARD_MAX_COUNT = 200     # 枚数系フィールドの上限（デッキ60+EX15 を十分に超える値）
BOARD_MAX_MOVES = 2000    # logs 上限（solitaire_routes.REPLAY_MAX_LOG_ENTRIES）と同じ
BOARD_MAX_NAME_LEN = 100

_SLOT_RE = re.compile(r"custom-slot-(\d{1,2})")  # 実盤面は 1〜14 の14マス


class BoardInvalid(ValueError):
    """board 全体の構造不正（内部用。外へは None で返す）"""


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _count(v, maxv=BOARD_MAX_COUNT) -> int:
    if not _is_int(v) or v < 0 or v > maxv:
        raise BoardInvalid("count")
    return v


def _card(d):
    """カード1枚を正規化する。不正なら None（呼び出し側がそのカードだけ除外する）。"""
    if not isinstance(d, dict):
        return None
    name = d.get("name", "")
    if not isinstance(name, str) or len(name) > BOARD_MAX_NAME_LEN:
        return None
    out = {"name": name}
    token = d.get("token")
    if token is not None and not isinstance(token, bool):
        return None
    if token:
        out["token"] = True
    return out


def _field_entry(e):
    """フィールドの1マス分。不正なら None。"""
    card = _card(e)
    if card is None:
        return None
    slot = e.get("slot")
    m = _SLOT_RE.fullmatch(slot) if isinstance(slot, str) else None
    if not m or not 1 <= int(m.group(1)) <= 14:
        return None
    if e.get("pos") not in ("atk", "def") or e.get("face") not in ("up", "down"):
        return None
    item = {"slot": slot, **card, "pos": e["pos"], "face": e["face"]}
    if "n" in e:
        n = e["n"]
        if not _is_int(n) or not 1 <= n <= BOARD_MAX_FIELD * 10:
            return None
        item["n"] = n
    return item


def _pile(d) -> dict:
    if not isinstance(d, dict):
        raise BoardInvalid("pile")
    top = _card(d.get("top"))   # 一番上のカードが不正ならカードだけ無しにする
    if top is not None and d["top"].get("face") == "down":
        top["face"] = "down"
    return {"count": _count(d.get("count")), "top": top}


def validate_board(board):
    """board を検証して正規化した dict を返す。None（未指定）・構造不正なら None。

    正規化で未知のキー（img を含む）は落とす。カード単位の不正は、そのカードだけ除外する。
    """
    if board is None:
        return None
    try:
        if not isinstance(board, dict) or board.get("v") != 1:
            raise BoardInvalid("v")
        field_in = board.get("field")
        hand_in = board.get("hand")
        if not isinstance(field_in, list) or not isinstance(hand_in, list):
            raise BoardInvalid("list")
        if len(field_in) > BOARD_MAX_FIELD or len(hand_in) > BOARD_MAX_HAND:
            raise BoardInvalid("limit")
        ex = board.get("ex")
        if not isinstance(ex, dict):
            raise BoardInvalid("ex")

        field, seen = [], set()
        for e in field_in:
            item = _field_entry(e)
            if item is None or item["slot"] in seen:
                continue
            seen.add(item["slot"])
            field.append(item)
        return {
            "v": 1,
            "moves": _count(board.get("moves"), BOARD_MAX_MOVES),
            "field": field,
            "hand": [c for c in map(_card, hand_in) if c is not None],
            "grave": _pile(board.get("grave")),
            "banish": _pile(board.get("banish")),
            "ex": {"count": _count(ex.get("count"))},
            "deckCount": _count(board.get("deckCount")),
        }
    except BoardInvalid:
        return None
