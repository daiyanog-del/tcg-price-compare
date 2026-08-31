"""型番(code)の品質を統一する純粋関数群。

背景（docs/decisions.md 相当の要約）: 店ごとに `code` の実態がまるで違う。
  - カーナベル   : 「弾の略号」だけ（例 TTP1 / DT06）。scraper.py の
                   category3_abbr をそのまま入れており設計時から型番ではない
  - トレコロCB   : 型番＋レアリティ接尾辞（例 15AX-JPM04MR）、または商品IDの数値
  - カードラッシュ: アジア版など別商品を示す接頭辞が付くことがある（例 アジアTTP1-JP079）

完全な型番を返す店（まんぞく屋・カードラボ・遊々亭・カードラッシュの通常商品）の
データを使って、同じカードの結果セット内でカーナベル・トレコロCBの `code` を
補完する。補完は「同じカードを全店ぶん取得し終えた直後・保存前」に呼ぶこと
（infer_codes は1回の検索/収集で集まった同一カードの行のリストを渡す想定）。

判定基準は司令塔が本番データで実測した値に基づく（2026-08-31）:
  - カーナベル: (カード名, レアリティ) で候補を絞ったうえ、カーナベルの略号と
    「前方一致」するものだけを採用する。一意に決まらなければ補完しない。
    弾（前方一致）を見ずにレアリティだけで絞ると、実測199件で別の弾の型番を
    誤って採用してしまう（例: カーナベル DT06 なのに候補が DTC2-JP082 のみ、
    のようなケース）ため、前方一致は必須。
  - トレコロCB: 接尾辞つきの値・数値IDに対し、「既知の完全な型番」との前方一致で
    復元する。一意に決まらなければ復元しない。接尾辞の一覧を推測して正規表現で
    剥がすことはしない（自己検証にならないため）。
"""
from __future__ import annotations

import re
import unicodedata

# templates/index.html の _looksLikeCardCode() と同じ意味の判定。
# 2箇所に別々の定義を置かない方針のため、意味が食い違わないことをテストで固定する
# （tests/test_card_code.py）。
_CODE_RE = re.compile(r"^[A-Za-z0-9]{2,6}-[A-Z]{2}[A-Za-z]?\d{2,4}$")

CARNABELL_SHOP = "カーナベル"
TORECOLO_SHOP = "トレコロCB"

CODE_SOURCE_SHOP = "shop"
CODE_SOURCE_INFERRED = "inferred"


def _name_key(name: str) -> str:
    """候補集合のグルーピング用にカード名を正規化する（全角英数字を半角化）。

    店によって同じカードの表記が「増殖するG」/「増殖するＧ」のように全角/半角が
    食い違う（実測: トレコロCBは全角英数字を格納）。ここで揃えないと、正しい候補が
    (カード名, レアリティ) キーで一致せず補完が0件になる（2026-08-31 実機検証で発覚）。
    型番の値そのものには適用しない（型番の全角/半角表記は店側の実データのまま扱う）。
    """
    return unicodedata.normalize("NFKC", name or "").strip()


def looks_like_card_code(code: str) -> bool:
    """型番として扱ってよい形かどうかを判定する（templates/index.html の
    _looksLikeCardCode() と同じ意味）。"""
    if not code:
        return False
    return bool(_CODE_RE.match(str(code).strip()))


def _complete_kanabell_code(abbr: str, candidates: set) -> str | None:
    """カーナベルの弾略号(abbr)に対し、他店の完全な型番の集合(candidates)のうち
    abbr で前方一致するものだけを採用する。1つに絞れない場合はNoneを返す
    （弾が違う型番を誤って採用しないため）。"""
    if not abbr:
        return None
    matched = {c for c in candidates if c.startswith(abbr)}
    if len(matched) == 1:
        return next(iter(matched))
    return None


def _restore_torecolo_code(raw: str, candidates: set) -> str | None:
    """トレコロCBの接尾辞つき値・数値IDに対し、他店の完全な型番の集合(candidates)の
    うち raw の前方一致になるものを探す。1つに絞れる場合のみ採用する。"""
    if not raw:
        return None
    matched = {c for c in candidates if raw.startswith(c)}
    if len(matched) == 1:
        return next(iter(matched))
    return None


def infer_codes(rows: list[dict]) -> list[dict]:
    """同じカードの全店ぶんの結果(rows)に対し、codeを補完した新しいリストを返す。

    各行に `code_source`（"shop" | "inferred"）を必ず付ける。
      - "shop"     : その店が返した値がそのまま「型番らしい形」だった
      - "inferred" : このロジックで補完を試みた（補完できた／できなかった の両方を含む）
    補完できなかった行（カーナベル・トレコロCBで候補が絞れない、カードラッシュの
    「アジア」等の別商品接頭辞つきの値、その他の店で型番らしくない値等）は
    code を空文字にする。略号や接尾辞つきの値をそのまま残さない
    （表示側のガードを外したときに誤情報が復活するため）。

    引数の rows は変更しない（新しい dict のリストを返す）。
    """
    # (カード名, レアリティ) ごとに「型番らしい形」の code の集合を集める
    by_key: dict[tuple, set] = {}
    for r in rows:
        code = (r.get("code") or "").strip()
        if looks_like_card_code(code):
            key = (_name_key(r.get("name", "")), r.get("rarity", ""))
            by_key.setdefault(key, set()).add(code)

    out = []
    for r in rows:
        row = dict(r)
        code = (row.get("code") or "").strip()
        shop = row.get("shop")

        if looks_like_card_code(code):
            row["code"] = code
            row["code_source"] = CODE_SOURCE_SHOP
            out.append(row)
            continue

        key = (_name_key(row.get("name", "")), row.get("rarity", ""))
        candidates = by_key.get(key, set())
        inferred = None
        if shop == CARNABELL_SHOP:
            inferred = _complete_kanabell_code(code, candidates)
        elif shop == TORECOLO_SHOP:
            inferred = _restore_torecolo_code(code, candidates)

        row["code"] = inferred or ""
        row["code_source"] = CODE_SOURCE_INFERRED
        out.append(row)

    return out
