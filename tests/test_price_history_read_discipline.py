# price_history / buyback_history 読み出し規律の不変条件テスト
#
# 背景: PostgRESTの返却上限1000行によるサイレント切り捨てを、このプロジェクトは
# 4回踏んだ（tracked_cards 2026-07 / estimate_cache 2026-08-03 / /api/price-history と
# featured_pack 2026-08-06 監査F1・F2）。原因は毎回「一括読みに range 分割が無い」か
# 「.limit(N) が上限で黙って切られる」こと。
#
# 本テストはソースコードを走査し、両テーブルへの読み出し（select）が
#   1) .range( による分割取得であること
#   2) .order( による順序安定化があること（order無しrangeはページ境界で行を落とす）
#   3) .limit( を使っていないこと
# を機械的に強制する。違反が増えたらこのクラスのバグの5例目なので、range分割＋
# order（複合キー推奨）に書き換えること。詳細: docs/audit-price-logic-2026-08-06.md

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 走査対象: リポジトリ直下と tools/ の .py（テスト自身は除外）
def _target_files():
    files = list(REPO_ROOT.glob("*.py")) + list((REPO_ROOT / "tools").glob("*.py"))
    return [f for f in files if f.name != Path(__file__).name]


TABLE_RE = re.compile(r"""\.table\(\s*['"](price_history|buyback_history)['"]\s*\)""")
WRITE_MARKERS = (".insert(", ".upsert(", ".update(", ".delete(")


def _call_windows():
    """各 .table("price_history"/"buyback_history") 呼び出しから
    直近の .execute( までのソース断片を (ファイル, 行番号, 断片) で返す"""
    windows = []
    for path in _target_files():
        src = path.read_text(encoding="utf-8")
        for m in TABLE_RE.finditer(src):
            start = m.start()
            end = src.find(".execute(", start)
            if end == -1:
                end = min(len(src), start + 1200)
            snippet = src[start:end]
            line_no = src.count("\n", 0, start) + 1
            windows.append((path.name, line_no, snippet))
    return windows


def test_reads_are_paginated_with_stable_order():
    violations = []
    for fname, line, snip in _call_windows():
        if any(w in snip for w in WRITE_MARKERS):
            continue  # 書き込み・削除は対象外
        if ".select(" not in snip:
            continue
        if ".limit(" in snip:
            violations.append(f"{fname}:{line} 読み出しに .limit() — 上限1000で黙って切られる。range分割にすること")
        if ".range(" not in snip:
            violations.append(f"{fname}:{line} 読み出しに .range() が無い — 既定1000行でサイレント切り捨て")
        if ".order(" not in snip:
            violations.append(f"{fname}:{line} range分割に .order() が無い — ページ境界で行を取りこぼす")
    assert not violations, "1000行上限クラスの違反（5例目）:\n" + "\n".join(violations)


def test_scan_actually_finds_known_read_sites():
    """走査自体が壊れて全件素通しになる退行を防ぐ（既知の読み出し箇所を最低数だけ確認）"""
    reads = [w for w in _call_windows()
             if ".select(" in w[2] and not any(m in w[2] for m in WRITE_MARKERS)]
    # 2026-08-06時点で既知の読み出しは app.py×4 / featured_pack.py×2 / notify.py×1 / x_poster.py×1 = 8箇所
    assert len(reads) >= 8, f"読み出し箇所が{len(reads)}件しか検出されない。走査ロジックの退行を疑うこと"
