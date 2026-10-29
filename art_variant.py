"""
art_variant.py — 店の商品が「どの絵か」「どの言語版か」を判別する（2026-10-28）
==========================================================================
同じ型番・同じレアリティの中に絵が2種類ある（例: 灰流うらら PAC1-JP016 シークレットの元の絵 500円／イラスト違い 980円）。
価格比較の画面でこれを混ぜて「最安」と言わないよう、検索の結果を画面に返す直前（app._build_done）で各行に印を付ける。

  art     : "元のイラスト" / "イラスト違い" / "漫画カラー" / "漫画モノクロ" / "GREEN Ver." / "アジア版" など / ""（判別できない）
  edition : "アジア版" / "英語版" など。日本版は ""
言語版は別の商品なので、絵より先に art を言語版の名前にする（画面の絵の切り替えで日本版と分ける）。
レアリティの名前は変えない（購入候補・買取・メルカリ・価格推移はレアリティ名で引いているため。10-28 レビュー指摘）。

店ごとの手がかり（2026-10-28 に人気カード30枚×5店で確認）:
  - 遊々亭・カードラボ・トレコロの一部: 決まった言葉（既存/通常/旧 ↔ イラスト違い/海外版/新）
  - トレコロ: 同じ型番の中で、違う絵の商品番号だけ末尾のレアリティ略号の前に S が付く（SE→SSE・QSE→SQSE・
    PSE→SPSE・PGR→SPGR・ウルトラの「なし」→S）。そのレアリティのいつもの略号（_TORECOLO_SUFFIX）と比べ、
    どちらでもない・番号つき（SSE1 など＝絵が3種類以上のカードの何番目か）・商品名の言葉と食い違うものは判別しない。
    検索の API は型番を推定値に置き換えるので、商品番号は商品のリンク（/shop/g/g{番号}/）から読む
  - カードラッシュ: 「立ち／座り」のような絵の描写しか書かない → カードごとの表（_CARDRUSH_WORDS）
  - まんぞく屋: 絵を書き分けない → 判別できない（""）
判別できない行は「すべての絵」にだけ出す（無理に振り分けない）。
型番ごと違う絵で刷られた再録（三戦の号 CH01-JP030 など）は、カーナベルだけが (イラスト違い) と書くので、
全店の行を見て型番ごとそろえる（_alt_only_codes。2026-10-29）。カーナベルが取れない日にも効くよう、
Yugipedia から週1回作る表（data/art_prints.json・tools/build_art_prints.py）の型番も同じ扱いにする。
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

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
# トレコロの商品番号の、レアリティごとのいつもの末尾（2026-10-28 に人気カード30枚の実データで確認したもの）。
# 違う絵は先頭に S が付く。ここに無いレアリティは判別しない
_TORECOLO_SUFFIX = {
    "シークレット": "SE", "25thシークレット": "QSE", "プリシク": "PSE", "プレミアムゴールド": "PGR",
    "ウルトラ": "", "スーパー": "SR", "レア": "R", "アルティメット": "AR", "コレクターズ": "CR",
    "20thシークレット": "SE", "EXシークレット": "EXS", "ホログラフィック": "HR",
}


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


def _torecolo_art(url: str, rarity: str) -> str:
    """トレコロの商品番号の S の決まりで元／違うを返す。決まりに当たらなければ ""（判別しない）"""
    base = _TORECOLO_SUFFIX.get(rarity)
    m = _TORECOLO_PID_RE.search(url or "")
    code = _TORECOLO_CODE_RE.match(m.group(1)) if m else None
    if base is None or not code:
        return ""
    if code.group(1) == base:
        return ART_BASE
    # S＋いつもの末尾が、ほかのレアリティのいつもの末尾と同じ形（レアの S＋R＝スーパーの SR）は、どちらとも言えない
    if code.group(1) == "S" + base and "S" + base not in _TORECOLO_SUFFIX.values():
        return ART_ALT
    return ""


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
        return edition, edition

    if re.search(r"GREEN\s*Ver", raw_name + raw_rarity, re.IGNORECASE):
        return ART_GREEN, edition
    if re.search(r"PINK\s*Ver", raw_name + raw_rarity, re.IGNORECASE):
        return ART_PINK, edition

    label = _label(card_name, raw_name)
    # カードラッシュは漫画風の絵を「(カラー)」「(モノクロ)」とだけ書く（10-28 青眼・ブラマジ・真紅眼などで確認）
    if shop == "カードラッシュ" and label in ("カラー", "モノクロ"):
        return (ART_MANGA_COLOR if label == "カラー" else ART_MANGA_MONO), edition
    if re.search(r"漫画|MANGA", label, re.IGNORECASE):
        if "カラー" in label:
            return ART_MANGA_COLOR, edition
        if re.search(r"モノクロ|白黒", label):
            return ART_MANGA_MONO, edition
        return "", edition

    if shop == "トレコロCB":
        art = _torecolo_art(row.get("url") or "", raw_rarity)
        # 商品名の決まった言葉と食い違うときは判別しない（[新イラスト] なのに S が無い等）
        if art and ((art == ART_BASE and label in _ALT_WORDS) or (art == ART_ALT and label in _BASE_WORDS - {""})):
            return "", edition
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


_ART_PRINTS_PATH = Path(__file__).resolve().parent / "data" / "art_prints.json"


@lru_cache(maxsize=1)
def _art_prints() -> dict[str, frozenset[str]]:
    """型番ごと違う絵の日本の型番の表（カード名の比べる形 → 型番）。無い・壊れているときは空（店の手がかりだけで判定する）"""
    try:
        raw = json.loads(_ART_PRINTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("art_prints.json を読めない（型番ごと違う絵は店の手がかりだけで判定する）: %s", e)
        return {}
    return {k: frozenset(v) for k, v in raw.items()}


def _alt_only_codes(rows: list[dict], card_name: str) -> set[str]:
    """型番ごと違う絵で刷られた再録の型番。例: 三戦の号 CH01-JP030 は QCAC-JP096 と違う絵だが、その型番の中に絵は1種類なので
    カーナベル以外の店は「三戦の号」とだけ書き、元のイラストに見えてしまう（2026-10-29 ユーザー指摘）。
    どこかの店がその型番を決まった言葉でイラスト違いと書き、その型番で元と違うを両方持つ店も、決まった言葉で元と書く店も無いとき、
    型番ごと違う絵とみなす（灰流うらら PAC1-JP016 のように1つの型番に絵が2種類ある型番は、同じ店に両方並ぶので当たらない）。
    Yugipedia の表（_art_prints）にある型番も、店の言葉が無くても同じ扱い（同じ店に両方並ぶ・元と書く店があれば外す）"""
    alt_by_explicit: set[str] = set()
    blocked: set[str] = set()
    arts_by_shop: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        code = row.get("code") or ""
        if not code or row.get("edition"):
            continue
        label = _label(card_name, row.get("name") or "")
        if row["art"] == ART_ALT and label in _ALT_WORDS:
            alt_by_explicit.add(code)
        if row["art"] == ART_BASE and label in _BASE_WORDS - {""}:
            blocked.add(code)
        arts_by_shop.setdefault((row.get("shop") or "", code), set()).add(row["art"])
    for (_, code), arts in arts_by_shop.items():
        if ART_BASE in arts and ART_ALT in arts:
            blocked.add(code)
    return (alt_by_explicit | _art_prints().get(_key(card_name), frozenset())) - blocked


def annotate(rows: list[dict], card_name: str) -> None:
    """各行に art・edition を付ける（rows の各 dict をその場で書き換える。レアリティは変えない）"""
    for row in rows:
        row["art"], row["edition"] = classify(row, card_name)
    # 型番ごと違う絵の再録は、無印で書く店の行も違う絵に直す（その店の中では「元」に見えるだけ）
    alt_codes = _alt_only_codes(rows, card_name)
    for row in rows:
        if row["art"] == ART_BASE and not row.get("edition") and (row.get("code") or "") in alt_codes:
            row["art"] = ART_ALT


def display_rarity(row: dict) -> str:
    """画面に出すレアリティ名。カードラッシュはレアリティ側に「クォーターセンチュリーシークレットGREEN Ver.」と書く
    （トレコロは 25thシークレット の名前側に [GREEN Ver]）ので、25thシークレット に寄せて絵の切り替えで GREEN Ver. を選べるようにする。
    保存（price_history・買取の履歴）には使わない。取得時の正準化で寄せると GREEN が 25thシークレット の系列に混ざる（10-28 レビュー指摘）"""
    rarity = row.get("rarity") or ""
    # アジア版の GREEN は art が言語版になるので、レアリティ側の表記で見る
    if rarity.startswith("クォーターセンチュリーシークレット") and re.search(r"GREEN\s*Ver|PINK\s*Ver", rarity, re.IGNORECASE):
        return "25thシークレット"
    return rarity


# 未開封・付属品つきの商品（(未開封)・(封筒付き未開封)・(当選通知書付き、ダンボール付き)・(台紙、封筒付き)・《未開封》 など）。
# 付属品は決まった言葉だけ（「キズ付き」「サイン付き」などを付属品と取り違えない）
_SEALED_RE = re.compile(r"未開封|[(（][^()（）]*(?:封筒|台紙|当選通知書|ダンボール|BD|DVD|書籍|スリーブ|ケース)[^()（）]*付き[^()（）]*[)）]")


def is_sealed(product_name: str) -> bool:
    """未開封・付属品つきの商品か。買取の最高額（記録・値動き・画面の最高額）は通常品で比べるため、これを外す（2026-10-28 ユーザー決定）。
    例: カードラッシュの 25TH-JP001 は通常品 25,000円・付属品つき未開封 45,000円で、最高額が 45,000円として記録されていた"""
    return bool(_SEALED_RE.search(product_name or ""))


def row_is_sealed(row: dict) -> bool:
    """行が未開封・付属品つきか。取得時に名前を加工する店（トレコロ・カードラボの買取）は取得時に sealed を付けている"""
    return bool(row.get("sealed")) or is_sealed(row.get("name", ""))

