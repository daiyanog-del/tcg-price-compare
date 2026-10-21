"""
tests/test_featured_history_rarity.py — featured_pack.get_card_history_since の
読み出し境界でのレアリティ正規化（2026-10-21）

背景:
  新弾値動きのグラフ（chart_renderer.render_price_chart）は ordered_canonicals() の
  正準名だけを描く。get_card_history_since だけ normalize_rarity を通しておらず、
  「UR」「ウルトラレア」など正準名でない表記の行はグラフから黙って消えていた
  （2026-09-06 rarity 統合作業のレビュー指摘）。

テスト方針: ネットワーク不使用。sb を最小限のフェイクで模倣する。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import featured_pack
from rarity import UNKNOWN_RARITY_LABEL, ordered_canonicals


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self._range = (0, len(rows))

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def gte(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def range(self, start, end):
        self._range = (start, end + 1)
        return self

    def execute(self):
        start, end = self._range
        return SimpleNamespace(data=[dict(r) for r in self._rows[start:end]])


class _FakeSb:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        assert name == "price_history"
        return _FakeQuery(self._rows)


def _row(rarity, price=1000, day="2026-10-20", shop="カーナベル"):
    return {"card_name": "灰流うらら", "shop": shop, "rarity": rarity,
            "min_price": price, "recorded_at": day}


def test_別表記は正準名に統合される():
    sb = _FakeSb([_row("UR"), _row("ウルトラレア", shop="まんぞく屋"), _row("ウルトラ")])
    rows = featured_pack.get_card_history_since(sb, "灰流うらら", "2026-10-01")
    assert [r["rarity"] for r in rows] == ["ウルトラ"] * 3
    # グラフが描く正準名に入っている（系列が消えない）
    assert "ウルトラ" in ordered_canonicals()


def test_空とダッシュは不明へ_未知表記はそのまま():
    sb = _FakeSb([_row(None), _row(""), _row("-"), _row("謎のレア")])
    rows = featured_pack.get_card_history_since(sb, "灰流うらら", "2026-10-01")
    assert [r["rarity"] for r in rows] == ["", "", UNKNOWN_RARITY_LABEL, "謎のレア"]


def test_複数ページでも全行を正規化する():
    data = [_row("UR", price=i) for i in range(1500)]
    rows = featured_pack.get_card_history_since(_FakeSb(data), "灰流うらら", "2026-10-01")
    assert len(rows) == 1500
    assert {r["rarity"] for r in rows} == {"ウルトラ"}
