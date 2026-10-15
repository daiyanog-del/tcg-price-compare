"""
replay_moderation.py — 公開リプレイのタイトルの不適切語チェック

公開（みんなのリプレイ）のタイトルは誰でも見られる一覧に載るため、保存前に禁止語リストと照合する。
照合は「NFKC 正規化 → 小文字化 → 空白除去」をしたタイトルに、同じ正規化をした禁止語が含まれるか（部分一致）。
リストは data/replay_ng_words.txt（1行1語。空行と # で始まる行は無視）。

TODO: calibrate from data — 禁止語リストは仮置き（明らかな差別語・性的な語・誹謗中傷の基本語を少数）。
部分一致のため、短い語は正当なタイトルを誤って断る可能性がある。公開の実データが溜まったら見直す。
"""

import logging
import os
import re
import unicodedata

logger = logging.getLogger(__name__)

NG_WORDS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "replay_ng_words.txt")

# 空白（改行・全角スペース含む）とゼロ幅文字（見えない文字で語を割る回避を防ぐ）
_STRIP_RE = re.compile(r"[\s\u200b\u200c\u200d\u2060\ufeff]+")

_ng_words_cache: tuple[str, ...] | None = None


def normalize_for_match(text: str) -> str:
    """NFKC で正規化し、小文字にして、空白を除く。"""
    return _STRIP_RE.sub("", unicodedata.normalize("NFKC", text or "").lower())


def load_ng_words(path: str = NG_WORDS_PATH) -> tuple[str, ...]:
    """禁止語リストを読み込む（正規化済み・重複なし）。読めないときは空で続行し警告を出す。"""
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
        # リストが無いと全タイトルを通してしまうので、見逃さないよう警告する（根本対応は配置の修正）
        logger.warning(f"禁止語リストを読み込めません（{path}）: {e}")
    return tuple(words)


def _get_ng_words() -> tuple[str, ...]:
    global _ng_words_cache
    if _ng_words_cache is None:
        _ng_words_cache = load_ng_words()
    return _ng_words_cache


def contains_ng_word(text: str, words: tuple[str, ...] | None = None) -> bool:
    """text に禁止語が含まれるか。words 省略時は data/replay_ng_words.txt を使う。"""
    norm = normalize_for_match(text)
    if not norm:
        return False
    return any(w in norm for w in (words if words is not None else _get_ng_words()))
