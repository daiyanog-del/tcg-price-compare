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
    # 2026-10-30: QCAC-JP002 の元は全身・違う絵は両手を広げた上半身（トレコロの S 付きと見比べ）
    "エフェクト・ヴェーラー": {"オーブ無": ART_BASE, "オーブ有": ART_ALT},
    # 2026-10-30: 日本版＝アンク・海外版＝装飾の付いた像・浮世絵はアンクを描き直した別の絵（画像で確認）
    "死者蘇生": {"日本版": ART_BASE, "海外版": ART_ALT, "浮世絵/ロゴ": ART_ALT},
}

# カードラッシュが、絵はそのままで印や刷りの違いだけを書く言葉（どのカードでも元の絵）。
# 2026-10-30 に エフェクト・ヴェーラー（ロゴ・YCSJロゴ）・青眼の究極竜（ロゴ・復刻版）・大嵐（復刻版）・
# 烙印竜アルビオン（ロゴ）・死者蘇生（復刻版・初期）の商品画像を元の商品と見比べて確認。
# YOTロゴは画像を取れなかったが、YCSJロゴと同じ景品の英語版
_CARDRUSH_BASE_WORDS = frozenset({"ロゴ", "YCSJロゴ", "YOTロゴ", "復刻版", "初期"})

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


def _is_manga_row(row: dict) -> bool:
    """カーナベルの漫画風（MANGA-STYLE）の行か。取得時に付く manga 欄か、それより前のキャッシュの行の略号で見る"""
    return bool(row.get("manga")) or (row.get("rarity") or "").upper().startswith("MANGA")


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
    # カーナベルは漫画風をレアリティ側に「MANGAウル」「MANGAシク」と書き、商品名には「(カラー版)」「(モノクロ版)」とだけ書く
    # （2026-10-30 ベビードラゴン YAC1-JP075 で確認）。レアリティは取得時に「ウルトラ」などへ寄せ、印は manga 欄に残す
    # （scraper._kanabell_is_manga）。それより前に取った検索のキャッシュの行はレアリティが略号のまま
    if _is_manga_row(row):
        label = "MANGA" + label
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
        if label in _CARDRUSH_BASE_WORDS:
            return ART_BASE, edition
    return "", edition


_ART_PRINTS_PATH = Path(__file__).resolve().parent / "data" / "art_prints.json"


@lru_cache(maxsize=1)
def _art_table() -> dict[str, tuple[int, dict[str, int]]]:
    """art_prints.json（カード名の比べる形 → (絵の数, {型番: 何番目の絵か})）。無い・壊れているときは空。
    2026-10-30 から {"arts": 絵の数, "codes": {型番: 番号}} の形。それより前の形（型番の並び）は番号 0（不明）で読む"""
    try:
        raw = json.loads(_ART_PRINTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("art_prints.json を読めない（型番ごと違う絵は店の手がかりだけで判定する）: %s", e)
        return {}
    out: dict[str, tuple[int, dict[str, int]]] = {}
    for k, v in raw.items():
        # 1件の形が崩れていても、検索の画面（annotate）まで落とさない。そのカードだけ飛ばす
        try:
            if isinstance(v, dict):
                out[k] = (int(v.get("arts") or 0), {str(c): int(n) for c, n in (v.get("codes") or {}).items()})
            else:
                out[k] = (0, {str(c): 0 for c in v})
        except (TypeError, ValueError, AttributeError) as e:
            logger.warning("art_prints.json の %s を読めない（このカードは店の手がかりだけで判定する）: %s", k, e)
    return out


def _art_prints() -> dict[str, frozenset[str]]:
    """型番ごと違う絵の日本の型番の表（カード名の比べる形 → 型番）。無い・壊れているときは空（店の手がかりだけで判定する）"""
    return {k: frozenset(codes) for k, (_, codes) in _art_table().items()}


def _art_numbers(card_name: str) -> tuple[int, dict[str, int]]:
    """そのカードの (絵の数, {型番ごと違う絵の型番: 何番目の絵か})。表に無いカードは (0, {})"""
    return _art_table().get(_key(card_name), (0, {}))


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


# ── 絵の台帳（2026-11-01）──
# 青眼の白龍・ブラック・マジシャンのように絵が10種類以上あるカードは、「元のイラスト／イラスト違い」の2択では分けきれない
# （QCDB・QCCU の 25thシークが元のイラストに混ざっていた。ユーザー指摘）。台帳（data/art_ledger.json・tools/build_art_ledger.py）に
# 載せたカードだけ、型番と店の言葉から「石板」「原作絵」のような絵の名前を付ける。台帳に無いカードは今までどおり2択
_ART_LEDGER_PATH = Path(__file__).resolve().parent / "data" / "art_ledger.json"


@lru_cache(maxsize=1)
def _art_ledger() -> dict[str, dict]:
    """art_ledger.json（カード名の比べる形 → 台帳）。無い・壊れているときは空（台帳のカードも2択で判定する）"""
    try:
        raw = json.loads(_ART_LEDGER_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("art_ledger.json を読めない（絵の台帳を使わずに2択で判定する）: %s", e)
        return {}
    out: dict[str, dict] = {}
    for name, v in raw.items():
        if name.startswith("_"):
            continue
        try:
            arts = {int(n): str(a) for n, a in v["arts"].items()}
            codes = {}
            for code, e in v.get("codes", {}).items():
                base = e.get("base")
                codes[code.upper()] = (int(base) if base is not None else None, [int(n) for n in e.get("alts") or []])
            words = {code.upper(): [(w["shop"], _key(w["word"]), int(w["art"])) for w in ws]
                     for code, ws in (v.get("words") or {}).items()}
            colors = {str(c): int(n) for c, n in (v.get("colors") or {}).items()}
            out[_key(name)] = {"arts": arts, "default": int(v["default"]) if v.get("default") else None,
                               "codes": codes, "words": words, "colors": colors}
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            # 1枚の形が崩れていても検索の画面（annotate）まで落とさない。そのカードは2択で判定する
            logger.warning("art_ledger.json の %s を読めない（このカードは2択で判定する）: %s", name, e)
    return out


def ledger_info(card_name: str) -> dict | None:
    """画面に渡す台帳の情報（絵の名前の並び・既定の絵）。台帳に無いカードは None"""
    led = _art_ledger().get(_key(card_name))
    if not led:
        return None
    return {"order": [led["arts"][n] for n in sorted(led["arts"])],
            "default": led["arts"].get(led["default"] or 0, "")}


def _ledger_code(row: dict, led: dict) -> str:
    """台帳を引く型番。トレコロは検索の結果に型番が入らないことが多い（補完が一意に決まらない）ので、
    商品のリンクの商品番号（QCAC-JP018SQSE など＝型番＋レアリティの略号）の頭が台帳の型番と一致すればそれを使う"""
    code = (row.get("code") or "").upper()
    if code or row.get("shop") != "トレコロCB":
        return code
    m = _TORECOLO_PID_RE.search(row.get("url") or "")
    pid = m.group(1).upper() if m else ""
    # 型番の後ろが数字なら別の型番（LB-05 と LB-050 のような取り違えを避ける）
    hits = [c for c in led["codes"] if pid.startswith(c) and not pid[len(c):len(c) + 1].isdigit()]
    return max(hits, key=len) if hits else ""


def ledger_art(row: dict, card_name: str) -> int | None:
    """annotate 済み（2択の印が付いた）行の、台帳の絵の番号。決まらなければ None。
    型番の中に絵が1つ → その絵。絵が複数 → 店の言葉、なければ2択の印（元＝その型番のふつうの絵・違う＝違う絵が1つだけのとき）"""
    led = _art_ledger().get(_key(card_name))
    if not led:
        return None
    code = _ledger_code(row, led)
    label = _key(row.get("name") or "")
    if code not in led["codes"]:
        # 型番の無い行（カーナベルの古い版は型番も弾の略号も無い）は、台帳の「*」の言葉（「(初期イラスト版)」など）だけで決める
        if not code:
            for shop, word, n in led["words"].get("*", []):
                if shop == row.get("shop") and word in label:
                    return n
        return None
    base, alts = led["codes"][code]
    if not alts:
        return base
    for shop, word, n in led["words"].get(code, []):
        if shop == row.get("shop") and word in label:
            return n
    if row.get("art") == ART_BASE:
        return base
    if row.get("art") == ART_ALT and len(alts) == 1:
        return alts[0]
    return None


def _apply_ledger(rows: list[dict], card_name: str) -> None:
    """台帳のカードの行の art を絵の名前にする（art_no に番号）。言語版はそのまま。
    漫画風は台帳の「漫画カラー／漫画モノクロ」と同じ名前なのでそのまま。決まらない行は判別できない（""）。
    GREEN/PINK Ver. は台帳の colors の絵に寄せる（東京ドームの配布カード TD01・TD02 は、カーナベル・トレコロ・カードラッシュは
    「GREEN Ver.」と書き、遊々亭は無印で書く同じ商品。型番の無い行もあるので型番でなく色で寄せる。2026-11-01 本番の行で確認）。
    colors に無い色はそのまま"""
    led = _art_ledger().get(_key(card_name))
    if not led:
        return
    for row in rows:
        if row.get("edition") or row.get("art") in (ART_MANGA_COLOR, ART_MANGA_MONO):
            continue
        if row.get("art") in (ART_GREEN, ART_PINK):
            n = led["colors"].get(row["art"])
            if n is None:
                continue
        else:
            n = ledger_art(row, card_name)
        row["art"] = led["arts"].get(n, "") if n is not None else ""
        row["art_no"] = n


def annotate(rows: list[dict], card_name: str, extra_alt_codes: set[str] | None = None) -> None:
    """各行に art・edition を付ける（rows の各 dict をその場で書き換える。レアリティは変えない）。
    extra_alt_codes は、ほかの行（同じカードのほかの店）で型番ごと違う絵と分かった型番。1店だけの行を
    あとから判定するとき（夜の収集のカードラボのカタログ巡回）に渡す。
    絵の台帳に載せたカードは、2択の印を手がかりに最後に絵の名前へ置き換える（_apply_ledger）"""
    for row in rows:
        row["art"], row["edition"] = classify(row, card_name)
    # 型番ごと違う絵の再録は、無印で書く店の行も違う絵に直す（その店の中では「元」に見えるだけ）
    alt_codes = _alt_only_codes(rows, card_name) | (extra_alt_codes or set())
    # Yugipedia の表にある型番は絵が1種類だけなので、店の言葉で判別できなかった行（カーナベルの「(パワプロコラボ)」など。
    # 2026-10-29 増援 PPC2-JP001）も違う絵にする。絵を書き分けない店・漫画風（色が書いていない）は判別しないまま
    table_codes = _art_prints().get(_key(card_name), frozenset()) & alt_codes
    for row in rows:
        code = row.get("code") or ""
        if row.get("edition") or code not in alt_codes:
            continue
        if row["art"] == ART_BASE:
            row["art"] = ART_ALT
        elif (row["art"] == "" and code in table_codes and row.get("shop") != "まんぞく屋"
              and not _is_manga_row(row)
              and not re.search(r"漫画|MANGA", _label(card_name, row.get("name") or ""), re.IGNORECASE)):
            row["art"] = ART_ALT
    _apply_ledger(rows, card_name)


def display_rarity(row: dict) -> str:
    """画面に出すレアリティ名。カードラッシュはレアリティ側に「クォーターセンチュリーシークレットGREEN Ver.」と書く
    （トレコロは 25thシークレット の名前側に [GREEN Ver]）ので、25thシークレット に寄せて絵の切り替えで GREEN Ver. を選べるようにする。
    保存（price_history・買取の履歴）には使わない。取得時の正準化で寄せると GREEN が 25thシークレット の系列に混ざる（10-28 レビュー指摘）"""
    rarity = row.get("rarity") or ""
    # アジア版の GREEN は art が言語版になるので、レアリティ側の表記で見る
    if rarity.startswith("クォーターセンチュリーシークレット") and re.search(r"GREEN\s*Ver|PINK\s*Ver", rarity, re.IGNORECASE):
        return "25thシークレット"
    return rarity


# ── 記録（price_history_art・buyback_art）の絵の分け方（2026-10-30 イラスト違いの段2・案B） ──
# 判別できない行の記録上の名前（レアリティの "(不明)" と同じ書き方）
ART_UNKNOWN = "(判別できない)"
# 絵の内訳を別表に書かなくてよい絵（元のイラストと判別できない行だけなら、本表の値と同じ）
_PLAIN_RECORD_ARTS = frozenset({ART_BASE, ART_UNKNOWN})


def record_art(row: dict, card_name: str) -> str:
    """annotate 済みの行を、記録の表のどの絵に入れるか。言語版はその名前、判別できない行は ART_UNKNOWN。
    日本の型番から届く違う絵が2種類以上あるカードで、型番から何番目の絵か分かる違う絵は「イラスト違い#4」のように番号を付ける
    （青眼の白龍 KC01-JP004＝4番・YAP1-JP001＝5番を同じ「イラスト違い」にまとめないため）。Yugipedia の絵の数（arts）は
    英語版・ゲームだけの絵も数えるので条件に使わない（日本の違う絵が1種類なら番号を付けず、店の言葉だけの行と同じ名前にする。
    10-30 reviewer 指摘）。画面の呼び方は段2b で決める。
    絵の台帳のカードの行は「絵#4」のように台帳の番号で記録する（名前は後から直すことがあるので記録には使わない。2026-11-01）"""
    if row.get("edition"):
        return row["edition"]
    if row.get("art_no") is not None:
        return f"絵#{row['art_no']}"
    art = row.get("art") or ""
    if not art:
        return ART_UNKNOWN
    if art == ART_ALT:
        _, numbers = _art_numbers(card_name)
        n = numbers.get(row.get("code") or "", 0)
        if n >= 2 and len({m for m in numbers.values() if m >= 2}) >= 2:
            return f"{ART_ALT}#{n}"
    return art


def needs_art_rows(arts: set[str]) -> bool:
    """そのカード×レアリティの絵の内訳を別表に書くか（元のイラスト・判別できない行のほかに何かあるとき）"""
    return not arts <= _PLAIN_RECORD_ARTS


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

