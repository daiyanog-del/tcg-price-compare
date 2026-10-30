"""未発売カードの型番（card_number）の決め方のテスト（2026-10-30・未発売カード一覧の「型番順」）。

- 公式サイトの画像URL（/media/cms/v1/<base64url(JSON)>.<署名>.webp）から型番を取り出す
- URL に無ければ抽出AIの読みを使い、形の合わない読みは捨てる
- YouTube 取り込みは場面ごとの読みから1つに決め、割れたら空にする
- 管理画面の手入力は表記を揃え、形の合わない値は弾く
"""
import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import admin_unreleased as admin_module
import ocgtimes_reveal as rv
from unreleased_card_number import card_number_from_image_url, decide_card_number, normalize_card_number
from unreleased_extractor import _validate_and_fix


def _cms_url(asset_path: str) -> str:
    """新サイトの画像URLを、実際の生成方式（tests/test_unreleased_extractor_image_urls.py と同じ）で組み立てる。"""
    payload = json.dumps([asset_path, "fm=webp&q=80"])
    b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"https://yu-gi-oh.jp/media/cms/v1/{b64}.cPc2CpDSySoZ3q7X2sDUNzvlzeuLgDbHReeUBj_TDzo.webp"


# 本番の一覧にあった実際の画像URL（IMPH-JP026・2026-10-30 に unreleased_cards から取得）
_REAL_URL = ("https://yu-gi-oh.jp/media/cms/v1/WyIvYXNzZXRzLzZjYTFmZWFhM2YwNjQ3ZWI4OWVmNzRkYTRlYTUxN2U3L2E2ZDFmNjFj"
             "YWUzYzQ2MWQ4MGMxZTJlZDlhZjEwNzhmL0lNUEgtSlAwMjYuanBnIiwiZm09d2VicCZxPTgwIl0."
             "cPc2CpDSySoZ3q7X2sDUNzvlzeuLgDbHReeUBj_TDzo.webp")


def test_real_official_url_gives_card_number():
    assert card_number_from_image_url(_REAL_URL) == "IMPH-JP026"


def test_official_url_variants():
    base = "/assets/6ca1/45d2/"
    assert card_number_from_image_url(_cms_url(base + "IMPH-JP009_SR.jpg")) == "IMPH-JP009"   # レアリティの接尾辞
    assert card_number_from_image_url(_cms_url(base + "IMPH-JPS13.jpg")) == "IMPH-JPS13"      # シークレット枠
    assert card_number_from_image_url(_cms_url(base + "CH03-JPP01_OF.png")) == "CH03-JPP01"   # プロモ枠
    assert card_number_from_image_url(_cms_url(base + "pack_photo.jpg")) == ""                # 型番でないファイル
    assert card_number_from_image_url("https://pbs.twimg.com/media/abc.jpg") == ""            # X の画像
    assert card_number_from_image_url("https://yu-gi-oh.jp/media/cms/v1/!!!.x.webp") == ""
    assert card_number_from_image_url("") == ""


def test_normalize_card_number():
    assert normalize_card_number(" imph-jp012 ") == "IMPH-JP012"
    assert normalize_card_number("ＩＭＰＨ－ＪＰ０１２") == "IMPH-JP012"   # 全角
    assert normalize_card_number("IMPH−JP012") == "IMPH-JP012"            # 全角マイナス
    assert normalize_card_number("IMPH-JP0") == ""                        # 番号が短い
    assert normalize_card_number("IMPH-EN012") == ""                      # OCG でない
    assert normalize_card_number("IMPH JP012") == ""
    assert normalize_card_number(None) == ""


def test_url_wins_over_ai_and_bad_ai_reading_is_dropped():
    url = _cms_url("/assets/a/b/IMPH-JP026.jpg")
    assert decide_card_number(url, "IMPH-JP099") == ("IMPH-JP026", "image_url")
    assert decide_card_number("https://pbs.twimg.com/media/abc.jpg", "imph-jp071") == ("IMPH-JP071", "ai")
    assert decide_card_number("https://pbs.twimg.com/media/abc.jpg", "IMPH-JP0〓1") == ("", "")


def _extracted(**overrides):
    values = dict(name="秋", reading="", card_type="通常魔法", attribute="", race="", level=None, rank=None,
                  link_val=None, atk=None, def_="", pendulum_scale=None, pendulum_effect="", effect_text="効果",
                  product_name="IMMORTAL PHOENIX", release_date="", image_url="", card_number="", confidence="high")
    values.update(overrides)
    return SimpleNamespace(**values)


def test_extractor_row_has_card_number():
    row = _validate_and_fix(_extracted(image_url="https://pbs.twimg.com/media/a.jpg", card_number="IMPH-JP071"),
                            "https://x.com/YuGiOh_OCG_INFO/status/1")
    assert row["card_number"] == "IMPH-JP071"
    official = _validate_and_fix(_extracted(image_url=_cms_url("/assets/a/b/IMPH-JP026.jpg"), card_number=""),
                                 "https://yu-gi-oh.jp/news/abc/")
    assert official["card_number"] == "IMPH-JP026"
    # 読めなかったときは NULL（空文字を DB に入れない）
    assert _validate_and_fix(_extracted(), "https://x.com/a/status/1")["card_number"] is None


def test_extractor_schema_and_prompt_ask_for_card_number():
    import unreleased_extractor as ex
    card_model, _ = ex._get_pydantic_models()
    assert "card_number" in card_model.model_json_schema()["properties"]
    content = ex._build_vision_message("記事", [])
    assert '"card_number"' in content[-1]["text"]


def test_reveal_card_number_majority_and_tie():
    assert rv.decide_card_number([{"card_number": "IMPH-JP015"}, {"card_number": "imph-jp015"},
                                  {"card_number": "IMPH-JP016"}]) == "IMPH-JP015"
    assert rv.decide_card_number([{"card_number": "IMPH-JP015"}, {"card_number": "IMPH-JP016"}]) == ""
    assert rv.decide_card_number([{"card_number": ""}, {}]) == ""
    assert rv.decide_card_number([{"card_number": "読めない"}, {"card_number": "IMPH-JP015"}]) == "IMPH-JP015"
    assert "card_number" in rv.REVEAL_SCHEMA["properties"]
    # 型番は「不確か」の判定（値の多数決）には入れない
    assert "card_number" not in rv.VALUE_FIELDS


def test_admin_card_number_input():
    assert admin_module._clean_card_number({"card_number": " imph-jp012 "}) == ("IMPH-JP012", None)
    assert admin_module._clean_card_number({"card_number": ""}) == (None, None)
    assert admin_module._clean_card_number({}) == (None, None)
    value, error = admin_module._clean_card_number({"card_number": "IMPH012"})
    assert value is None and "型番の形" in error
    assert "card_number" in admin_module._ALLOWED_UPDATE_FIELDS
