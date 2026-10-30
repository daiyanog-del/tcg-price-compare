"""未発売カードの型番（例 IMPH-JP012）を決める・並べるための純粋関数群（2026-10-30）。

未発売カード一覧（/new-cards）の「型番順」に使う。型番の出どころは2つ:
  - 公式サイト（yu-gi-oh.jp）由来: 画像URLの /media/cms/v1/<base64url>.<署名>.webp の base64 部分を
    復号すると元のパス（…/IMPH-JP026.jpg 等）が出てくる。ファイル名が型番なので機械的に正確に取れる
  - X・YouTube・画像アップロード由来: URL に型番が無いので、抽出AIがカード画像の絵の右下の型番を読む
    （unreleased_extractor / ocgtimes_reveal）。小さい字の読み違いはあり得るので、形の合わない値は捨てる
画像URLから取れるときはそちらを優先する（AI の読みより確か）。
"""
from __future__ import annotations

import base64
import re
import unicodedata

# OCG の型番。弾の略号（英数字2〜5文字）-JP＋番号。番号の前に英字1文字が付くことがある
# （IMPH-JPS13 のシークレット枠・CH03-JPP01 のプロモ枠など）。実データ（2026-10-30 の一覧97枚のうち URL から取れた61枚）で確認
_CARD_NUMBER_RE = re.compile(r"^([A-Z0-9]{2,5})-JP([A-Z]?)(\d{2,3})$")
# 公式サイトの画像URLの、元のパスを base64url で包んだ部分
_CMS_SEGMENT_RE = re.compile(r"/media/cms/v1/([A-Za-z0-9_-]+)\.")
# 復号したパスの末尾のファイル名（IMPH-JP026.jpg / IMPH-JP026_OF.jpg / IMPH-JP026_500.jpg 等）
_FILENAME_RE = re.compile(r"/([A-Z0-9]{2,5}-JP[A-Z]?\d{2,3})(?:_[A-Za-z0-9]+)?\.(?:jpe?g|png|webp)", re.IGNORECASE)


def normalize_card_number(value) -> str:
    """型番の表記を揃える（全角→半角・大文字・前後の空白除去）。型番の形でなければ空文字。"""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    # ハイフンの揺れ（‐ − ― 等）は NFKC で揃わないものがあるので半角に寄せる
    text = re.sub(r"[‐‑‒–—―−ｰー]", "-", text)
    return text if _CARD_NUMBER_RE.match(text) else ""


def card_number_from_image_url(url: str) -> str:
    """公式サイトの画像URLから型番を取り出す。取れなければ空文字。"""
    match = _CMS_SEGMENT_RE.search(url or "")
    if not match:
        return ""
    segment = match.group(1)
    try:
        decoded = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)).decode("utf-8", "ignore")
    except (ValueError, TypeError):
        return ""
    found = _FILENAME_RE.search(decoded)
    return normalize_card_number(found.group(1)) if found else ""


def decide_card_number(image_url: str, ai_value) -> tuple[str, str]:
    """(型番, 出どころ) を返す。出どころは 'image_url'（公式画像のURL）・'ai'（抽出AIの読み）・''（不明）。"""
    from_url = card_number_from_image_url(image_url)
    if from_url:
        return from_url, "image_url"
    from_ai = normalize_card_number(ai_value)
    if from_ai:
        return from_ai, "ai"
    return "", ""
