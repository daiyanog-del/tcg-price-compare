"""
tests/test_overframe.py — overframe.py（OF のレアリティの店舗間そろえ）の単体テスト

本番で見た書き方を固定する（2026-10-30）:
  - 遊々亭「真紅眼の超越黒竜(オーバーフレーム)」プリシク／「古の秘儀(オーバーフレーム)」ウルトラ
  - カードラボ「古の秘儀〔オーバーフレーム〕」「古の秘儀〔通常フレーム〕」ウルトラ
  - カードラボの印の無いプリシク（BETB-JP036・OF しか無い型番）
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from overframe import apply_overframe, infer_overframe, mark_overframe, overframe_context


def _row(shop, rarity, code="BETB-JP036", name="真紅眼の超越黒竜", **extra):
    row = {"shop": shop, "name": name, "rarity": rarity, "code": code}
    row.update(extra)
    return row


# ── mark_overframe: 商品名の印 ──

def test_遊々亭の丸括弧の印でOFになる():
    assert mark_overframe("プリシク", "真紅眼の超越黒竜(オーバーフレーム)") == "OFプリシク"
    assert mark_overframe("ウルトラ", "古の秘儀(オーバーフレーム)") == "OFウルトラ"


def test_カードラボの亀甲括弧の印でOFになる():
    assert mark_overframe("ウルトラ", "古の秘儀〔オーバーフレーム〕") == "OFウルトラ"


def test_印が無い_通常フレーム_OF済みは変えない():
    assert mark_overframe("プリシク", "真紅眼の超越黒竜") == "プリシク"
    assert mark_overframe("ウルトラ", "古の秘儀〔通常フレーム〕") == "ウルトラ"
    assert mark_overframe("OFプリシク", "真紅眼の超越黒竜(オーバーフレーム)") == "OFプリシク"


def test_OFの無いレアリティは印があっても変えない():
    # OFレア・OFノーマルは rarity.py に無い。未知の名前を作らない
    assert mark_overframe("ノーマル", "テスト(オーバーフレーム)") == "ノーマル"


def test_括弧の外のオーバーフレームという語では変えない():
    # カード名の一部としての語（括弧の印でない）を OF の印と取り違えない
    assert mark_overframe("ウルトラ", "オーバーフレーム・ドラゴン") == "ウルトラ"


# ── infer_overframe: 店をまたいだ判定 ──

def test_BETB_JP036_カードラボの印の無いプリシクがOFにそろう():
    rows = [
        _row("遊々亭", "プリシク", name="真紅眼の超越黒竜(オーバーフレーム)"),
        _row("カードラボ", "プリシク"),
        _row("カードラッシュ", "OFプリシク"),
        _row("カードラボ", "シークレット"),
    ]
    out = infer_overframe(rows)
    assert [r["rarity"] for r in out] == ["OFプリシク", "OFプリシク", "OFプリシク", "シークレット"]


def test_ほかの店が普通の枠と書く型番は寄せない():
    # 同じ型番を普通のプリシクと書く店がある＝両方ある（か判定できない）。寄せない側に倒す
    rows = [
        _row("カードラボ", "プリシク"),
        _row("遊々亭", "プリシク", name="真紅眼の超越黒竜(オーバーフレーム)"),
        _row("トレコロCB", "プリシク"),
    ]
    assert infer_overframe(rows)[0]["rarity"] == "プリシク"


def test_遊々亭の印の無い行は普通の枠の根拠になる():
    # 遊々亭は OF に印を付けるので、印の無いプリシクは普通の枠
    rows = [
        _row("遊々亭", "プリシク"),
        _row("カードラボ", "プリシク"),
        _row("カーナベル", "OFプリシク"),
    ]
    assert infer_overframe(rows)[1]["rarity"] == "プリシク"


def test_ほかの店の行が無ければ寄せない():
    assert infer_overframe([_row("カードラボ", "プリシク")])[0]["rarity"] == "プリシク"


def test_遊々亭の行が無ければほかの店がOFと書いても寄せない():
    # 店を絞った検索・遊々亭の取得失敗の回。普通の枠の行が欠けているだけかもしれないので寄せない
    # （古の秘儀のウルトラのように両方の枠がある型番がある。10-30 reviewer 指摘）
    rows = [_row("カードラボ", "ウルトラ", code="UT01-JP001", name="古の秘儀"),
            _row("カーナベル", "OFウルトラ", code="UT01-JP001", name="古の秘儀")]
    assert infer_overframe(rows)[0]["rarity"] == "ウルトラ"


def test_型番が違えば寄せない():
    rows = [_row("カードラボ", "プリシク", code="BETB-JP001"), _row("遊々亭", "プリシク", name="真紅眼の超越黒竜(オーバーフレーム)")]
    assert infer_overframe(rows)[0]["rarity"] == "プリシク"


def test_カードラボの通常フレームの印は寄せない():
    rows = [
        _row("カードラボ", "ウルトラ", code="UT01-JP001", name="古の秘儀〔通常フレーム〕"),
        _row("遊々亭", "ウルトラ", code="UT01-JP001", name="古の秘儀(オーバーフレーム)"),
    ]
    assert infer_overframe(rows)[0]["rarity"] == "ウルトラ"


def test_カードラボ同士は根拠にしない():
    rows = [
        _row("カードラボ", "プリシク"),
        _row("カードラボ", "OFプリシク", name="真紅眼の超越黒竜〔オーバーフレーム〕"),
    ]
    assert infer_overframe(rows)[0]["rarity"] == "プリシク"


def test_キャッシュの古い遊々亭の行も商品名で直る():
    rows = [_row("遊々亭", "ウルトラ", code="UT01-JP001", name="古の秘儀(オーバーフレーム)")]
    assert infer_overframe(rows)[0]["rarity"] == "OFウルトラ"


def test_入力を書き換えず順序と件数を保つ():
    rows = [_row("カードラボ", "プリシク"), _row("遊々亭", "プリシク", name="真紅眼の超越黒竜(オーバーフレーム)")]
    out = infer_overframe(rows)
    assert rows[0]["rarity"] == "プリシク" and rows[1]["rarity"] == "プリシク"
    assert out[0]["rarity"] == "OFプリシク"
    assert len(out) == 2 and out[1]["shop"] == "遊々亭"


# ── 夜の収集: ループ側で残した context でカタログの行を寄せる ──

def test_contextでカタログのカードラボの行を寄せる():
    loop_rows = [_row("遊々亭", "プリシク", name="真紅眼の超越黒竜(オーバーフレーム)"), _row("遊々亭", "ウルトラ")]
    ctx = overframe_context(loop_rows)
    catalog = [_row("カードラボ", "プリシク"), _row("カードラボ", "ウルトラ")]
    out = apply_overframe(catalog, ctx)
    assert [r["rarity"] for r in out] == ["OFプリシク", "ウルトラ"]


def test_contextが無ければ印の判定だけ():
    catalog = [_row("カードラボ", "プリシク"), _row("カードラボ", "ウルトラ", name="X〔オーバーフレーム〕")]
    assert [r["rarity"] for r in apply_overframe(catalog, None)] == ["プリシク", "OFウルトラ"]


def test_中身の無い型番はcontextに入れない():
    # OF の無いレアリティ（ノーマル）とカードラッシュの OF だけの型番は判定に関係しない。夜の収集で全カードの文脈を持たない（10-30 reviewer 指摘）
    assert overframe_context([_row("遊々亭", "ノーマル"), _row("カードラッシュ", "OFプリシク")]) == {}
