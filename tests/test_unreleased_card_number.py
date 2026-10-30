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
    assert rv.decide_card_number_from_reads([{"card_number": "IMPH-JP015"}, {"card_number": "imph-jp015"},
                                  {"card_number": "IMPH-JP016"}]) == "IMPH-JP015"
    assert rv.decide_card_number_from_reads([{"card_number": "IMPH-JP015"}, {"card_number": "IMPH-JP016"}]) == ""
    assert rv.decide_card_number_from_reads([{"card_number": ""}, {}]) == ""
    assert rv.decide_card_number_from_reads([{"card_number": "読めない"}, {"card_number": "IMPH-JP015"}]) == "IMPH-JP015"
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


# ── 抽出（_extract_cards）での型番の決め方・既存行の空欄埋め（reviewer 指摘 2026-10-30） ──

def _run_extract(monkeypatch, cards, images):
    import anthropic
    import unreleased_extractor as ex
    import unreleased_quality as quality
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: object())
    monkeypatch.setattr(quality, "structured_request", lambda client, **kw: ({"cards": cards}, "{}", {}))
    monkeypatch.setattr(quality, "enrich_rows", lambda rows, **kw: rows)
    return ex._extract_cards("記事", images, "https://yu-gi-oh.jp/news/abc/")


def _card(name, image_url, number):
    return {"name": name, "card_type": "通常魔法", "effect_text": "効果", "image_url": image_url,
            "card_number": number, "confidence": "high"}


def test_extract_uses_own_image_url_and_records_ai_reading(monkeypatch):
    a, b = _cms_url("/assets/a/b/IMPH-JP001.jpg"), _cms_url("/assets/a/b/IMPH-JP002.jpg")
    images = [{"url": a, "media_type": "image/jpeg", "data": ""}, {"url": b, "media_type": "image/jpeg", "data": ""}]
    rows = _run_extract(monkeypatch, [_card("一", a, "IMPH-JP001"), _card("二", b, "IMPH-JP009")], images)
    one, two = rows
    assert one["card_number"] == "IMPH-JP001" and one["extraction_raw"]["card_number_source"] == "image_url"
    # AI の読みが食い違っても URL を採り、読みは記録に残す（画像の取り違えの手がかり）
    assert two["card_number"] == "IMPH-JP002" and two["extraction_raw"]["card_number_ai"] == "IMPH-JP009"


def test_text_only_extract_does_not_trust_copied_url(monkeypatch):
    # 画像なしの抽出では AI が本文から写した URL を確かめられないので、AI の読みだけを使う
    url = _cms_url("/assets/a/b/IMPH-JP001.jpg")
    rows = _run_extract(monkeypatch, [_card("一", url, "IMPH-JP005"), _card("二", url, "")], [])
    assert rows[0]["card_number"] == "IMPH-JP005" and rows[0]["extraction_raw"]["card_number_source"] == "ai"
    assert rows[1]["card_number"] is None and rows[1]["extraction_raw"]["card_number_source"] == ""


class _UpdateRecorder:
    def __init__(self):
        self.calls = []

    def table(self, name):
        self.calls.append(["table", name])
        return self

    def update(self, values):
        self.calls.append(["update", values])
        return self

    def eq(self, field, value):
        self.calls.append(["eq", field, value])
        return self

    def is_(self, field, value):
        self.calls.append(["is_", field, value])
        return self

    def execute(self):
        return SimpleNamespace(data=[])


def test_fill_missing_card_number_only_with_official_url_value():
    from unreleased_card_number import fill_missing_card_number
    sb = _UpdateRecorder()
    row = {"name": "秋", "product_name": "IMMORTAL PHOENIX", "card_number": "IMPH-JP071",
           "extraction_raw": {"card_number_source": "image_url"}}
    assert fill_missing_card_number(sb, row) is True
    assert ["update", {"card_number": "IMPH-JP071"}] in sb.calls
    assert ["is_", "card_number", "null"] in sb.calls          # 値がある行は変えない
    assert ["eq", "product_name", "IMMORTAL PHOENIX"] in sb.calls
    # AI の読み・型番なしでは書かない
    sb = _UpdateRecorder()
    assert fill_missing_card_number(sb, dict(row, extraction_raw={"card_number_source": "ai"})) is False
    assert fill_missing_card_number(sb, dict(row, card_number=None)) is False
    assert sb.calls == []
