"""
tests/test_unreleased_extractor_image_urls.py — unreleased_extractor._extract_card_image_urls()
の新旧サイト形式対応の単体テスト。

背景: 2026-09-01のyu-gi-oh.jpリニューアルで記事内カード画像のURL形式が
`/images/news/...` から `/media/cms/v1/<base64url(JSON)>.<署名>.webp` という
CMS画像変換プロキシURLへ変わり、旧形式の判定ロジックが新カードを一切拾えなく
なっていた。新形式（base64urlデコード→JSON配列0番目の元アセットパスのファイル名が
型番形式かどうか）と旧形式（両方とも）が正しく判定できることをここで固定する。
"""

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from unreleased_extractor import _extract_card_image_urls


def _make_cms_url(asset_path: str, sig: str = "fLqpU_Sqp5RytTTGlZCdPvTiqdpYZwZwZSw0kI4mUtw") -> str:
    """新サイトのCMS画像変換プロキシURLを、実際の生成方式に合わせて組み立てる。"""
    payload = json.dumps([asset_path, "fm=webp&q=80"])
    b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"https://yu-gi-oh.jp/media/cms/v1/{b64}.{sig}.webp"


# 実機確認した記事（https://yu-gi-oh.jp/news/khetf4yog_k/）から取得した
# 実際の元アセットファイル名一覧（カード11枚＋パック写真1枚）
_REAL_ASSET_DIR = "/assets/6ca1feaa3f0647eb89ef74da4ea517e7/45d29b2dcc534b4ca507de5a451da7e1"
_CARD_FILENAMES = [
    "IMPH-JP009_SR.jpg",
    "IMPH-JP010_R.jpg",
    "IMPH-JP011.jpg",
    "IMPH-JP034_UL.jpg",
    "IMPH-JP034_PSE.jpg",
    "IMPH-JP037.jpg",
    "IMPH-JP038.jpg",
    "IMPH-JP059_R.jpg",
    "IMPH-JP077.jpg",
    "IMPH-JP078.jpg",
    "IMPH-JPS13_UR.jpg",
]
_NON_CARD_FILENAME = "pack_IMPH.jpg"


def test_new_site_extracts_card_images_and_excludes_pack_photo():
    card_urls = [_make_cms_url(f"{_REAL_ASSET_DIR}/{name}") for name in _CARD_FILENAMES]
    pack_url = _make_cms_url(f"{_REAL_ASSET_DIR}/{_NON_CARD_FILENAME}")

    img_tags = "\n".join(f'<img src="{u}">' for u in card_urls + [pack_url])
    html = f"<html><body>{img_tags}</body></html>"

    result = _extract_card_image_urls(html, "https://yu-gi-oh.jp/news/khetf4yog_k/")

    assert len(result) == 11
    assert set(result) == set(card_urls)
    assert pack_url not in result


def test_new_site_real_sample_url_from_spec():
    """タスク仕様に記載された実データそのままのURL（IMPH-JP009_SR）が抽出されること。"""
    real_url = (
        "https://yu-gi-oh.jp/media/cms/v1/"
        "WyIvYXNzZXRzLzZjYTFmZWFhM2YwNjQ3ZWI4OWVmNzRkYTRlYTUxN2U3LzQ1ZDI5YjJkY2M1MzRi"
        "NGNhNTA3ZGU1YTQ1MWRhN2UxL0lNUEgtSlAwMDlfU1IuanBnIiwiZm09d2VicCZxPTgwIl0."
        "fLqpU_Sqp5RytTTGlZCdPvTiqdpYZwZwZSw0kI4mUtw.webp"
    )
    html = f'<html><body><img src="{real_url}"></body></html>'

    result = _extract_card_image_urls(html, "https://yu-gi-oh.jp/news/khetf4yog_k/")

    assert result == [real_url]


def test_new_site_skips_undecodable_url():
    """base64/JSONデコードに失敗するURLは例外を投げずスキップする。"""
    bad_url = "https://yu-gi-oh.jp/media/cms/v1/not-valid-base64!!!.sig.webp"
    html = f'<html><body><img src="{bad_url}"></body></html>'

    result = _extract_card_image_urls(html, "https://yu-gi-oh.jp/news/khetf4yog_k/")

    assert result == []


# ──────────────────────────────────────────────
# 旧サイト形式（回帰確認）
# ──────────────────────────────────────────────

def test_old_site_extracts_card_image_by_naming_convention():
    html = (
        "<html><body>"
        '<img src="/images/news/2543_20260602093341_img_1_abcdef.jpg">'
        '<img src="/images/news/news_1.jpg">'
        "</body></html>"
    )

    result = _extract_card_image_urls(html, "https://yu-gi-oh.jp/news_detail.php?page=details&id=2543")

    assert result == ["https://yu-gi-oh.jp/images/news/2543_20260602093341_img_1_abcdef.jpg"]


def test_old_site_excludes_non_matching_paths():
    html = '<html><body><img src="/images/common/header_logo.png"></body></html>'

    result = _extract_card_image_urls(html, "https://yu-gi-oh.jp/news_detail.php?page=details&id=2543")

    assert result == []
