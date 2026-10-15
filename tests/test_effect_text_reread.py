"""効果テキストの拡大読み直し（effect_text_reread）と効果欄の切り出し（card_crop.make_effect_crop）の検証。API はモック。"""
import base64
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import card_crop
import effect_text_reread as rr
import unreleased_extractor as extractor
import unreleased_quality as quality


def _png(w=407, h=593):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 180, 120)).save(buf, format="PNG")
    return buf.getvalue()


URL = "https://yu-gi-oh.jp/media/cms/v1/x.webp"


def _images(w=407, h=593):
    return [{"url": URL, "media_type": "image/png", "data": base64.b64encode(_png(w, h)).decode()}]


def _row(card_type="効果モンスター", effect="①：このカードを特殊召喚できる。", source="official_html", suggestion=None):
    raw = {"source_image_url": URL, "source": source, "uncertain_fields": []}
    if suggestion is not None:
        raw["crop_suggestion"] = suggestion
    return {"name": "テスト", "card_type": card_type, "effect_text": effect, "extraction_raw": raw}


def _fake_read(monkeypatch, text="", unreadable=False, error=None, calls=None):
    def fake(client, *, model, system, content, schema, max_tokens=16000, effort=None):
        if calls is not None:
            calls.append({"model": model, "effort": effort, "content": content, "system": system})
        if error:
            raise error
        return {"effect_text": text, "unreadable": unreadable}, "{}", {"input_tokens": 10, "output_tokens": 5}
    monkeypatch.setattr(quality, "structured_request", fake)


# ---- 切り出し ----

def test_monster_crop_uses_calibrated_ratio_and_3x():
    data = card_crop.make_effect_crop(_png(407, 593), "効果モンスター")
    w, h = Image.open(io.BytesIO(data)).size
    # 0.035〜0.965 / 0.69〜0.915 を3倍（丸めの誤差を許容）
    assert abs(w - 3 * round(407 * 0.93)) <= 6
    assert abs(h - 3 * round(593 * 0.225)) <= 6


def test_spell_crop_ratio():
    w, h = Image.open(io.BytesIO(card_crop.make_effect_crop(_png(480, 700), "通常魔法"))).size
    assert abs(h - 3 * round(700 * 0.22)) <= 6


def test_crop_inside_suggested_card_box():
    # カード枠が画像の右半分（X画像のように左右に別物が写る場合）: 枠内の比率で切る
    img = _png(814, 593)
    s = {"left": 0.5, "top": 0, "right": 1, "bottom": 1}
    w, h = Image.open(io.BytesIO(card_crop.make_effect_crop(img, "効果モンスター", s))).size
    # 枠候補のときは左右を3.5%ずつ広げる: 幅は枠(407px)の 0.93+0.07 倍を3倍
    assert abs(w - 3 * round(407 * (0.93 + 2 * card_crop.EFFECT_CROP_PAD_X))) <= 6


def test_crop_pads_inward_shifted_suggestion_but_not_image_boundary():
    # 枠候補が実際の外縁より内側に寄る場合のみ、切り出し範囲を外へ広げる（最後の行が欠けた実例の対策）
    base = Image.open(io.BytesIO(card_crop.make_effect_crop(_png(407, 593), "効果モンスター"))).size
    boundary = {"left": 0, "top": 0, "right": 1, "bottom": 1}
    assert Image.open(io.BytesIO(card_crop.make_effect_crop(_png(407, 593), "効果モンスター", boundary))).size == base
    inner = {"left": 0.037, "top": 0.02, "right": 0.966, "bottom": 0.978}
    padded = Image.open(io.BytesIO(card_crop.make_effect_crop(_png(407, 593), "効果モンスター", inner))).size
    assert padded[1] > base[1] - 12 and padded[0] > base[0] - 12  # 枠が小さくても、広げた分で元の大きさ近くになる


@pytest.mark.parametrize("card_type", ["ペンデュラム効果モンスター", "", "不明"])
def test_crop_rejects_unsupported_type(card_type):
    with pytest.raises(ValueError):
        card_crop.make_effect_crop(_png(), card_type)


def test_crop_rejects_bad_suggestion():
    with pytest.raises(ValueError):
        card_crop.make_effect_crop(_png(), "効果モンスター", {"left": 0.9, "top": 0, "right": 0.1, "bottom": 1})


# ---- 突き合わせ ----

def test_agreed_keeps_text_and_not_uncertain(monkeypatch):
    calls = []
    _fake_read(monkeypatch, text="①：このカードを特殊召喚できる。", calls=calls)
    row = _row()
    rr.reread_effect_texts([row], client=object(), encoded_images=_images())
    raw = row["extraction_raw"]
    assert raw["effect_text_reread"]["agreed"] is True
    assert raw["effect_text_first_pass"] == "①：このカードを特殊召喚できる。"
    assert "effect_text" not in raw["uncertain_fields"]
    assert calls[0]["model"] == "claude-opus-5-5" and calls[0]["effort"] == "medium"
    # テキスト欄のみ（全体画像は添えない）: 画像1枚＋指示文
    assert [c["type"] for c in calls[0]["content"]] == ["image", "text"]


def test_normalization_ignores_width_whitespace_but_keeps_circled_digits(monkeypatch):
    # 半角コロン・改行の違いは一致扱い（正規化後に比べる）
    _fake_read(monkeypatch, text="①:このカードを\n特殊召喚できる。")
    row = _row(effect="①：このカードを特殊召喚できる。")
    rr.reread_effect_texts([row], client=object(), encoded_images=_images())
    assert row["extraction_raw"]["effect_text_reread"]["agreed"] is True
    # 丸数字は数字に潰さない: ①と1は別
    assert rr.norm("①") != rr.norm("1")


def test_disagreement_adopts_reread_and_marks_uncertain(monkeypatch):
    _fake_read(monkeypatch, text="1ターンに1度、融合召喚及び以下の方法でのみ特殊召喚できる。")
    row = _row(effect="の①の方法での特殊召喚は1ターンに1度しかできない。")
    rr.reread_effect_texts([row], client=object(), encoded_images=_images())
    raw = row["extraction_raw"]
    assert row["effect_text"] == "1ターンに1度、融合召喚及び以下の方法でのみ特殊召喚できる。"
    assert raw["effect_text_first_pass"] == "の①の方法での特殊召喚は1ターンに1度しかできない。"
    info = raw["effect_text_reread"]
    assert info["agreed"] is False and info["diff_summary"] and info["usage"]["input_tokens"] == 10
    assert "effect_text" in raw["uncertain_fields"]


def test_unreadable_mark_is_uncertain(monkeypatch):
    _fake_read(monkeypatch, text="①：このカードを〓〓召喚できる。", unreadable=True)
    row = _row()
    rr.reread_effect_texts([row], client=object(), encoded_images=_images())
    assert row["extraction_raw"]["effect_text_reread"]["unreadable"] is True
    assert "effect_text" in row["extraction_raw"]["uncertain_fields"]


def test_api_error_keeps_first_pass_and_marks_uncertain(monkeypatch):
    _fake_read(monkeypatch, error=RuntimeError("boom"))
    row = _row()
    rr.reread_effect_texts([row], client=object(), encoded_images=_images())
    raw = row["extraction_raw"]
    assert row["effect_text"] == "①：このカードを特殊召喚できる。"
    assert "boom" in raw["effect_text_reread"]["error"] and raw["effect_text_reread"]["agreed"] is False
    assert "effect_text" in raw["uncertain_fields"]


def test_empty_reread_is_failure_not_adopted(monkeypatch):
    _fake_read(monkeypatch, text="")
    row = _row()
    rr.reread_effect_texts([row], client=object(), encoded_images=_images())
    assert row["effect_text"] == "①：このカードを特殊召喚できる。"
    assert "error" in row["extraction_raw"]["effect_text_reread"]
    assert "effect_text" in row["extraction_raw"]["uncertain_fields"]


def test_pendulum_is_not_read_and_both_fields_uncertain(monkeypatch):
    calls = []
    _fake_read(monkeypatch, text="x", calls=calls)
    row = _row(card_type="ペンデュラム効果モンスター")
    rr.reread_effect_texts([row], client=object(), encoded_images=_images())
    raw = row["extraction_raw"]
    assert calls == []
    assert raw["effect_text_reread"]["skipped"] == "pendulum_not_evaluated"
    assert set(raw["uncertain_fields"]) >= {"effect_text", "pendulum_effect"}
    assert raw["effect_text_first_pass"] == row["effect_text"]


def test_missing_image_is_uncertain(monkeypatch):
    calls = []
    _fake_read(monkeypatch, text="x", calls=calls)
    row = _row()
    rr.reread_effect_texts([row], client=object(), encoded_images=[])
    assert calls == [] and "effect_text" in row["extraction_raw"]["uncertain_fields"]


def test_x_without_crop_suggestion_is_uncertain_but_official_reads_whole_image(monkeypatch):
    calls = []
    _fake_read(monkeypatch, text="①：このカードを特殊召喚できる。", calls=calls)
    x_row, official = _row(source="x_tweet"), _row(source="official_html")
    rr.reread_effect_texts([x_row, official], client=object(), encoded_images=_images())
    assert len(calls) == 1
    assert "effect_text" in x_row["extraction_raw"]["uncertain_fields"]
    assert "effect_text" not in official["extraction_raw"]["uncertain_fields"]


def test_existing_uncertain_fields_are_kept_without_duplicates(monkeypatch):
    _fake_read(monkeypatch, text="違う文。")
    row = _row()
    row["extraction_raw"]["uncertain_fields"] = ["atk", "effect_text"]
    rr.reread_effect_texts([row], client=object(), encoded_images=_images())
    assert row["extraction_raw"]["uncertain_fields"] == ["atk", "effect_text"]


def test_punct_normalization_applied_to_reread(monkeypatch):
    _fake_read(monkeypatch, text="①:このカードを特殊召喚できる。")
    row = _row()
    rr.reread_effect_texts([row], client=object(), encoded_images=_images())
    assert row["effect_text"] == "①：このカードを特殊召喚できる。"


# ---- 呼び出しの形 ----

def test_structured_request_sends_effort_and_no_sampling_params():
    seen = {}

    def create(**kw):
        seen.update(kw)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="{}")],
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    schema = {"type": "object", "properties": {}}
    quality.structured_request(client, model="m", system="", content=[], schema=schema, effort="medium")
    assert seen["extra_body"]["output_config"]["effort"] == "medium"
    assert "thinking" not in seen and "temperature" not in seen
    seen.clear()
    quality.structured_request(client, model="m", system="", content=[], schema=schema)
    assert "effort" not in seen["extra_body"]["output_config"]


def test_default_models_are_opus():
    assert extractor._DEFAULT_MODEL == "claude-opus-5-5"
    assert extractor.EXTRACTOR_EFFORT == "medium"
    assert rr.REREAD_MODEL == "claude-opus-5-5" and rr.REREAD_EFFORT == "medium"


def test_enrich_rows_runs_reread_for_every_row(monkeypatch):
    import release_date_resolver
    monkeypatch.setattr(release_date_resolver, "resolve_release_date", lambda p: {
        "status": "unverified", "release_date": None, "reason": "x", "source_url": ""})
    _fake_read(monkeypatch, text="別の文。")
    row = _row()
    row["confidence"] = "high"
    row["extraction_raw"]["card_bbox"] = None  # 数値再読は対象外（枠なし）でも効果欄の読み直しは走る
    out = quality.enrich_rows([row], client=None, model="m", encoded_images=_images())
    assert out[0]["extraction_raw"]["effect_text_reread"]["agreed"] is False
    assert out[0]["effect_text"] == "別の文。"
