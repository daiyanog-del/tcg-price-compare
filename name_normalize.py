"""
name_normalize.py -- カード名正規化ロジック共通モジュール

app.py と reconcile_unreleased.py の両方で同一の正規化関数を使うため、
ここに切り出す。app.py はこのモジュールから import し、
Reconciler も同様にここから import することで挙動の一致を保証する。

このモジュールは Flask やその他のフレームワークに依存しない。
"""

import re
import unicodedata

# あいまい検索用: 中黒・ハイフン・スペース等を除去して比較するための正規化
# app.py の _FUZZY_STRIP と完全に同一のパターン
_FUZZY_STRIP = re.compile(r'[\s　・\-‒–—―−－ｰ・･.,()（）「」「」]')


def fuzzy_key(s: str) -> str:
    """
    あいまい検索用に記号・スペースを除去した比較キーを返す。
    全角英数・互換文字 -> 半角も正規化する。

    app.py の _fuzzy_key と完全に同一のロジック。
    """
    s = unicodedata.normalize('NFKC', s)  # ローマ数字(X->X等)・全角ハイフン等を一括変換
    # U+FF01-FF5E（全角英数・記号）-> 対応するASCII（半角）に変換
    s = ''.join(chr(ord(c) - 0xFEE0) if '！' <= c <= '～' else c for c in s)
    return _FUZZY_STRIP.sub('', s).lower()
