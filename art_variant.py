"""
art_variant.py — 店の商品が「どの絵か」「どの言語版か」を判別する（2026-10-28）
==========================================================================
同じ型番・同じレアリティの中に絵が2種類ある（例: 灰流うらら PAC1-JP016 シークレットの元の絵 500円／イラスト違い 980円）。
価格比較の画面でこれを混ぜて「最安」と言わないよう、検索の結果を画面に返す直前（app._build_done）で各行に印を付ける。

  art     : "元のイラスト" / "イラスト違い" / "漫画カラー" / "漫画モノクロ" / "GREEN Ver." / ""（判別できない）
  edition : "アジア版" / "英語版" など。日本版は ""

店ごとの手がかり（2026-10-28 に人気カード30枚×5店で確認）:
  - 遊々亭・カードラボ・トレコロの一部: 決まった言葉（既存/通常/旧 ↔ イラスト違い/海外版/新）
  - トレコロ: 同じ型番の中で、違う絵の商品番号だけ末尾のレアリティ略号に S が付く（SE→SSE・QSE→SQSE・PSE→SPSE・PGR→SPGR）。
    検索の API は型番を推定値に置き換えるので、商品番号は商品のリンク（/shop/g/g{番号}/）から読む
  - カードラッシュ: 「立ち／座り」のような絵の描写しか書かない → カードごとの表（_CARDRUSH_WORDS）
  - まんぞく屋: 絵を書き分けない → 判別できない（""）
判別できない行は「すべての絵」にだけ出す（無理に振り分けない）。
"""

from __future__ import annotations

import re
import unicodedata

ART_BASE = "元のイラスト"
ART_ALT = "イラスト違い"
ART_MANGA_COLOR = "漫画カラー"
ART_MANGA_MONO = "漫画モノクロ"
ART_GREEN = "GREEN Ver."
ART_PINK = "PINK Ver."

# 決まった言葉（商品名からカード名を除いた残り。括弧は外して比べる）
_BASE_WORDS = frozenset({"", "既存イラスト", "通常イラスト", "旧イラスト"})
_ALT_WORDS = frozenset({"イラスト違い版", "海外版イラスト", "イラスト違い", "新イラスト"})

# カードラッシュの絵の描写 → 元／違う。カード名は _key() で比べる。
# 2026-10-28: 同じ型番のトレコロの元／違う（S の決まり）の商品画像と自動で見比べ、目でも確かめたものだけ載せる。
# 強欲で金満な壺は自動の見比べが外れた（両方「元」寄り）ので目で見た結果。アクセスコード・トーカーは見分けに
# 自信が持てず載せていない
_CARDRUSH_WORDS: dict[str, dict[str, str]] = {
    "灰流うらら": {"立ち": ART_BASE, "座り": ART_ALT},
    "屋敷わらし": {"正面": ART_BASE, "振り向き": ART_ALT},
    "幽鬼うさぎ": {"正面": ART_BASE, "右向/うずまき": ART_ALT},
    "ドロール＆ロックバード": {"鳥右上": ART_BASE, "鳥左上": ART_ALT},
    "I：Pマスカレーナ": {"バイク無": ART_BASE, "バイク有": ART_ALT},
    "召命の神弓－アポロウーサ": {"熊": ART_BASE, "月下": ART_ALT},
    "墓穴の指名者": {"老人": ART_BASE, "LiveTwin": ART_ALT},
    "原始生命態ニビル": {"隕石": ART_BASE, "悪魔": ART_ALT},
    "強欲で金満な壺": {"二面相": ART_BASE, "エルドリッチ": ART_ALT},
}

# ☆アジア☆ など商品名の先頭に付く言語版の印（カードラッシュ）。☆バンダイ☆ のような版でない印は拾わない
_EDITION_RE = re.compile(r"☆(旧?アジア|英語版|韓国版|中国版|スペイン版|フランス版|ドイツ版|イタリア版|ポルトガル版)☆")
# 状態・付属品の印（(未開封)・(封筒付き未開封)・(当選通知書付き、ダンボール付き) など）。絵とは別なので除いてから比べる
_PACKAGE_RE = re.compile(r"\((?:[^()]*未開封|[^()]*付き)[^()]*\)")
_TORECOLO_PID_RE = re.compile(r"/shop/g/g([^/]+?)(?:-[SK])?/?$")
_TORECOLO_CODE_RE = re.compile(r"^[A-Z0-9]+-[A-Z]*\d+([A-Z]*)$")
_TORECOLO_ALT_SUFFIX_RE = re.compile(r"^S(SE|QSE|PSE|PGR|UR|CR)$")


def _key(s: str) -> str:
    """比べるための形（全角半角・空白・中黒・コロン・ダッシュ・＆の揺れを消す）。カードラッシュは記号を落として登録する"""
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"[\s・･:\-‐−－&]", "", s)


_CARDRUSH_WORDS_BY_KEY = {_key(name): words for name, words in _CARDRUSH_WORDS.items()}


def _label(card_name: str, product_name: str) -> str:
    """商品名からカード名・言語版・状態の印を除いた残り（絵の言葉）。括弧は外す"""
    name = _key(product_name)
    name = re.sub(r"☆[^☆]*☆", "", name)
    name = _PACKAGE_RE.sub("", name)
    card = _key(card_name)
    if card and card in name:
        name = name.replace(card, "", 1)
    name = re.sub(r"GREENVer\.?|PINKVer\.?", "", name, flags=re.IGNORECASE)
    return re.sub(r"[\[\]〔〕()（）]", "", name)


def _torecolo_art(url: str) -> str | None:
    """トレコロの商品番号の S の決まりで元／違うを返す。番号が読めなければ None"""
    m = _TORECOLO_PID_RE.search(url or "")
    if not m:
        return None
    code = _TORECOLO_CODE_RE.match(m.group(1))
    if not code:
        return None
    return ART_ALT if _TORECOLO_ALT_SUFFIX_RE.match(code.group(1)) else ART_BASE


def classify(row: dict, card_name: str) -> tuple[str, str]:
    """1行の (art, edition) を返す。row は店の結果の1行（shop・name・rarity・url を見る）"""
    raw_name = row.get("name") or ""
    raw_rarity = row.get("rarity") or ""
    shop = row.get("shop") or ""

    m = _EDITION_RE.search(raw_name)
    edition = ""
    if m:
        edition = m.group(1).replace("旧", "")
        edition = edition if edition.endswith("版") else edition + "版"

    if re.search(r"GREEN\s*Ver", raw_name + raw_rarity, re.IGNORECASE):
        return ART_GREEN, edition
    if re.search(r"PINK\s*Ver", raw_name + raw_rarity, re.IGNORECASE):
        return ART_PINK, edition

    label = _label(card_name, raw_name)
    if re.search(r"漫画|MANGA", label, re.IGNORECASE):
        return (ART_MANGA_COLOR if "カラー" in label else ART_MANGA_MONO), edition

    if shop == "トレコロCB":
        art = _torecolo_art(row.get("url") or "")
        if art:
            return art, edition
    if shop == "まんぞく屋":
        return "", edition  # 絵を書き分けない店
    if label in _BASE_WORDS:
        return ART_BASE, edition
    if label in _ALT_WORDS:
        return ART_ALT, edition
    if shop == "カードラッシュ":
        art = _CARDRUSH_WORDS_BY_KEY.get(_key(card_name), {}).get(label)
        if art:
            return art, edition
    return "", edition


def annotate(rows: list[dict], card_name: str) -> None:
    """各行に art・edition を付ける（rows をその場で書き換える。レアリティは変えない）"""
    for row in rows:
        row["art"], row["edition"] = classify(row, card_name)


def with_edition(rarity: str, edition: str) -> str:
    """言語版の行のレアリティ表示名（例: シークレット（アジア版））。日本版はそのまま。二重に付けない"""
    if not edition or not rarity:
        return rarity
    suffix = f"（{edition}）"
    return rarity if rarity.endswith(suffix) else rarity + suffix
