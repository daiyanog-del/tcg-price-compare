"""
name_normalize.py -- カード名正規化ロジック共通モジュール

app.py と reconcile_unreleased.py の両方で同一の正規化関数を使うため、
ここに切り出す。app.py はこのモジュールから import し、
Reconciler も同様にここから import することで挙動の一致を保証する。

このモジュールは Flask やその他のフレームワークに依存しない。

フェーズ3 P1（docs/design-phase3-generation-side.md 「P1: 書き込み時名寄せゼロ」）:
tracked_cards への登録経路が複数あり（ユーザー検索経路は app._correct_cardname で
補正済みだが、collect_prices.py の夜間同期経路は表記ゆれを素通ししていた）、
同一カードが表記違いで複数行に分裂する原因になっていた。
canonicalize_card_name / load_cardnames_index はその両経路で共通利用する
名寄せロジックを Flask 非依存で提供する。
"""

import json
import os
import re
import unicodedata
from typing import Callable, Optional

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


def load_cardnames_index(base_dir: Optional[str] = None) -> tuple[list[str], set[str], dict[str, list[str]]]:
    """data/cardnames_ja.json を読み込み、(名前一覧, 存在チェック用set, fuzzy_key→[正式名]) を返す。

    app.py の起動（Flask アプリ生成）に依存せず、このファイルを直接読む。
    collect_prices.py の夜間収集や tools/ の一回限りスクリプトから、
    Flask を経由せずに正式カード名インデックスを得るために使う。
    ファイル未検出時は空を返す（cardnames_set が空なら canonicalize_card_name は
    常に入力をそのまま返す＝現状と同じ「補正なし」挙動になる）。
    """
    base = base_dir or os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "data", "cardnames_ja.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            names = json.loads(f.read())
    except FileNotFoundError:
        return [], set(), {}
    fuzzy: dict[str, list[str]] = {}
    for name in names:
        fuzzy.setdefault(fuzzy_key(name), []).append(name)
    return names, set(names), fuzzy


def canonicalize_card_name(
    name: str,
    cardnames_set: set,
    cardnames_fuzzy: dict,
    warn: Optional[Callable[[str], None]] = None,
    context: str = "",
) -> tuple[str, bool]:
    """カード名を正式名へ解決する（tracked_cards 登録前の名寄せ用途を想定）。

    分岐:
      - cardnames_set が未ロード（空）: 補正材料がないのでそのまま返す
      - 完全一致: そのまま返す（既に正式名）
      - fuzzy_key が一意に1件の正式名へ一致: その正式名を返す
      - fuzzy_key が複数の正式名に衝突（例: E・HERO と E-HERO）:
        誤って別カードへ合流させないため補正せず入力のまま返す。warn があれば警告を通知する
      - fuzzy_key がインデックスに存在しない（未知のカード名）: そのまま返す

    戻り値: (resolved_name, matched)

    呼び出し側契約（matched の意味・厳守事項）:
      - matched=True （完全一致・fuzzy一意一致・fuzzy衝突の3系統いずれか）:
        canonicalize_card_name が既に最終判定を下している。呼び出し側は
        読み仮名照合など独自の追加フォールバックを**試みてはならない**
        （特にfuzzy衝突は「誤って別カードへ合流させない」ための安全側の
        補正見送りであり、ここで別ルートの補正を試みると本来の安全策を
        迂回してしまう）。
      - matched=False （fuzzy_key がインデックスに存在しない＝未知のカード名）:
        このときに限り、呼び出し側は読み仮名フォールバック等の追加処理を
        試みてよい（app._correct_cardname の読み仮名分岐はこの契約に基づく）。
    """
    if not cardnames_set or name in cardnames_set:
        return name, True
    q_fuzzy = fuzzy_key(name)
    if q_fuzzy in cardnames_fuzzy:
        candidates = cardnames_fuzzy[q_fuzzy]
        if len(candidates) > 1:
            # 同一fuzzy_keyに複数の正式名が衝突。[0]決め打ちは別カードへの誤合流という
            # 最悪種の汚染を招くため補正しない（P6a: docs/design-phase3-generation-side.md）。
            if warn:
                warn(
                    f"canonicalize_card_name: fuzzy_key衝突のため補正スキップ "
                    f"context={context!r} name={name!r} fuzzy_key={q_fuzzy!r} candidates={candidates}"
                )
            return name, True
        return candidates[0], True
    return name, False
