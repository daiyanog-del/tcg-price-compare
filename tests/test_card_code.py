"""
tests/test_card_code.py — card_code.infer_codes / looks_like_card_code の単体テスト

対象（司令塔が本番データで実測したケースを固定する）:
  - カーナベルの弾略号は、他店の完全な型番のうち「前方一致するもの」だけを採用する
    （弾が食い違う場合は補完しない。実測199件の誤補完防止）
  - トレコロCBの接尾辞つき値・数値IDは、既知の完全な型番との前方一致で復元する
  - カードラッシュの「アジア」等の別商品接頭辞つきの値は補完しない
  - 補完できなかった行の code は空文字にする
  - looks_like_card_code() は templates/index.html の _looksLikeCardCode() と同じ意味
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from card_code import infer_codes, looks_like_card_code, CODE_SOURCE_SHOP, CODE_SOURCE_INFERRED


def _row(shop, code, name="テストカード", rarity="ウルトラ", **extra):
    row = {"shop": shop, "name": name, "rarity": rarity, "code": code}
    row.update(extra)
    return row


# ── looks_like_card_code: templates/index.html の _looksLikeCardCode() と同じ意味 ──

def test_looks_like_card_code_通す例():
    for code in ["LPST-JP007", "SD47-JP016", "20TH-JPC82", "TT01-JPA10",
                 "EXP4-JP037", "QCDB-JP015", "RC04-JP005"]:
        assert looks_like_card_code(code), code


def test_looks_like_card_code_弾く例():
    for code in ["TTP1", "SDモ さ", "23434538", "LPST-JP007PSE", "20TH-JPC8220SE", ""]:
        assert not looks_like_card_code(code), code


# ── カーナベル: 弾の略号を他店の完全型番で補完 ──

def test_カーナベル_弾が食い違う候補は補完しない():
    """DT06 ＋ 候補 DTC2-JP082 のみ → 前方一致しないので補完しない"""
    rows = [
        _row("カーナベル", "DT06"),
        _row("遊々亭", "DTC2-JP082"),
    ]
    out = infer_codes(rows)
    kanabell_row = next(r for r in out if r["shop"] == "カーナベル")
    assert kanabell_row["code"] == ""
    assert kanabell_row["code_source"] == CODE_SOURCE_INFERRED


def test_カーナベル_前方一致する候補は補完する():
    """TTP1 ＋ 候補 TTP1-JP079 → 補完する"""
    rows = [
        _row("カーナベル", "TTP1"),
        _row("カードラッシュ", "TTP1-JP079"),
    ]
    out = infer_codes(rows)
    kanabell_row = next(r for r in out if r["shop"] == "カーナベル")
    assert kanabell_row["code"] == "TTP1-JP079"
    assert kanabell_row["code_source"] == CODE_SOURCE_INFERRED


def test_カーナベル_複数候補でも略号で一意に絞れれば補完する():
    rows = [
        _row("カーナベル", "TTP1"),
        _row("遊々亭", "TTP1-JP079"),
        _row("まんぞく屋", "DTC2-JP082"),  # 別の弾。前方一致せず候補から外れる
    ]
    out = infer_codes(rows)
    kanabell_row = next(r for r in out if r["shop"] == "カーナベル")
    assert kanabell_row["code"] == "TTP1-JP079"
    assert kanabell_row["code_source"] == CODE_SOURCE_INFERRED


def test_カーナベル_候補が複数あり略号でも絞れなければ補完しない():
    rows = [
        _row("カーナベル", "TT"),
        _row("遊々亭", "TTP1-JP079"),
        _row("カードラッシュ", "TTP2-JP080"),
    ]
    out = infer_codes(rows)
    kanabell_row = next(r for r in out if r["shop"] == "カーナベル")
    assert kanabell_row["code"] == ""
    assert kanabell_row["code_source"] == CODE_SOURCE_INFERRED


# ── トレコロCB: 接尾辞つき値・数値IDを既知の完全型番で復元 ──

def test_トレコロ_接尾辞つき値は既知の完全型番で復元する():
    rows = [
        _row("トレコロCB", "15AX-JPM04MR"),
        _row("遊々亭", "15AX-JPM04"),
    ]
    out = infer_codes(rows)
    torecolo_row = next(r for r in out if r["shop"] == "トレコロCB")
    assert torecolo_row["code"] == "15AX-JPM04"
    assert torecolo_row["code_source"] == CODE_SOURCE_INFERRED


def test_トレコロ_数値IDは復元できないので空にする():
    rows = [
        _row("トレコロCB", "23434538"),
        _row("遊々亭", "15AX-JPM04"),
    ]
    out = infer_codes(rows)
    torecolo_row = next(r for r in out if r["shop"] == "トレコロCB")
    assert torecolo_row["code"] == ""
    assert torecolo_row["code_source"] == CODE_SOURCE_INFERRED


# ── カードラッシュ: 「アジア」等の別商品接頭辞は補完しない ──

def test_カードラッシュ_アジア接頭辞は補完しない():
    rows = [
        _row("カードラッシュ", "アジアTTP1-JP079"),
        _row("遊々亭", "TTP1-JP079"),
    ]
    out = infer_codes(rows)
    cardrush_row = next(r for r in out if r["shop"] == "カードラッシュ")
    assert cardrush_row["code"] == ""
    assert cardrush_row["code_source"] == CODE_SOURCE_INFERRED


# ── 型番らしい値をそのまま返す店はshop扱いにする ──

def test_型番らしい値は補完せずそのまま_shopソースにする():
    rows = [_row("遊々亭", "TTP1-JP079")]
    out = infer_codes(rows)
    assert out[0]["code"] == "TTP1-JP079"
    assert out[0]["code_source"] == CODE_SOURCE_SHOP


def test_候補が無ければ補完できず空になる():
    rows = [_row("カーナベル", "DT06")]
    out = infer_codes(rows)
    assert out[0]["code"] == ""
    assert out[0]["code_source"] == CODE_SOURCE_INFERRED


def test_カード名の全角半角が店ごとに食い違っても候補を拾う():
    """実機検証で発覚: トレコロCBは全角英数字（増殖するＧ）で名前を返すが、
    他店は半角（増殖するG）。名前の正規化を怠ると候補が0件になる"""
    rows = [
        _row("トレコロCB", "15AX-JPM04MR", name="増殖するＧ"),
        _row("遊々亭", "15AX-JPM04", name="増殖するG"),
    ]
    out = infer_codes(rows)
    torecolo_row = next(r for r in out if r["shop"] == "トレコロCB")
    assert torecolo_row["code"] == "15AX-JPM04"
    assert torecolo_row["code_source"] == CODE_SOURCE_INFERRED


def test_元のリストは変更しない():
    rows = [_row("カーナベル", "TTP1"), _row("遊々亭", "TTP1-JP079")]
    infer_codes(rows)
    assert rows[0]["code"] == "TTP1"
    assert "code_source" not in rows[0]
