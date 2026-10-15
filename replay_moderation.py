"""
replay_moderation.py — 公開リプレイのタイトル・カード名の不適切語チェック

公開（みんなのリプレイ）のタイトルとカード名は誰でも見られる一覧に載るため、保存前に禁止語リストと照合する。
照合用の正規化（タイトルにも禁止語にも同じ処理を掛ける）:
  1. NFKC（全角半角・半角カナの統一）
  2. 見えない文字（unicodedata.category が Cf / Mn。ソフトハイフン・ゼロ幅・異体字セレクタ・結合記号など）を除く
  3. 小文字化
  4. カタカナをひらがなへそろえる
  5. 空白と、記号・区切り文字（P* / S* と長音「ー」。`_` `-` `.` `・` など）を除く
その上で、禁止語が含まれるか（部分一致）を見る。
リストは data/replay_ng_words.txt（1行1語。空行と # で始まる行は無視）。

リストを読めない（無い・空）ときは NgListUnavailable を投げる。呼び出し側は「公開だけを断る」（503）。
警告だけ出して全部通す、にはしない（禁止語チェックが無効のまま公開されるため）。

TODO: calibrate from data — 禁止語リストは仮置き（明らかな差別語・性的な語・誹謗中傷の基本語を少数）。
部分一致のため、短い語は正当なタイトルを誤って断る可能性がある（かな同士をそろえるので「しね」は「シネマ」にも当たる）。
公開の実データが溜まったら見直す。
"""

import logging
import os
import unicodedata

logger = logging.getLogger(__name__)

NG_WORDS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "replay_ng_words.txt")

_LONG_VOWEL = "\u30fc"   # 長音「ー」（カテゴリは Lm なので明示的に除く）

_ng_words_cache: tuple[str, ...] | None = None


class NgListUnavailable(Exception):
    """禁止語リストを使えない（読めない・空）。"""


def normalize_for_match(text: str) -> str:
    """照合用の正規化（モジュール冒頭の 1〜5）。"""
    out = []
    for c in unicodedata.normalize("NFKC", text or ""):
        cat = unicodedata.category(c)
        if cat in ("Cf", "Mn", "Cc") or c.isspace() or cat[0] in ("P", "S") or c == _LONG_VOWEL:
            continue
        c = c.lower()
        if "\u30a1" <= c <= "\u30f6":      # カタカナ → ひらがな
            c = chr(ord(c) - 0x60)
        out.append(c)
    return "".join(out)


def sanitize_title(text: str) -> str:
    """保存前のタイトル整形。改行・タブ等の制御文字は空白にし、書式制御文字（文字の向きを変える
    U+202A〜U+202E・U+2066〜U+2069、ゼロ幅など。Cf）は除く。前後の空白も除く。"""
    out = []
    for c in text or "":
        cat = unicodedata.category(c)
        if cat == "Cc":
            out.append(" ")
        elif cat == "Cf":
            continue
        else:
            out.append(c)
    return "".join(out).strip()


def load_ng_words(path: str = NG_WORDS_PATH) -> tuple[str, ...]:
    """禁止語リストを読み込む（正規化済み・重複なし）。読めないときは空で返し警告を出す。"""
    words: list[str] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                w = normalize_for_match(line)
                if w and w not in words:
                    words.append(w)
    except OSError as e:
        logger.warning(f"禁止語リストを読み込めません（{path}）: {e}")
    return tuple(words)


def _get_ng_words() -> tuple[str, ...]:
    """既定のリスト。読めない・空のときは NgListUnavailable（失敗は覚えず、次回また読みに行く）。"""
    global _ng_words_cache
    if _ng_words_cache is None:
        words = load_ng_words()
        if not words:
            raise NgListUnavailable("禁止語リストが使えません")
        _ng_words_cache = words
    return _ng_words_cache


def contains_ng_word(text: str, words: tuple[str, ...] | None = None) -> bool:
    """text に禁止語が含まれるか。words 省略時は data/replay_ng_words.txt を使う
    （使えないときは NgListUnavailable）。"""
    if words is None:
        words = _get_ng_words()
    norm = normalize_for_match(text)
    if not norm:
        return False
    return any(w in norm for w in words)
