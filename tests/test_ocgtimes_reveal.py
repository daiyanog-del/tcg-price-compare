"""OCGタイムズの紹介場面からの取込（ocgtimes_reveal.py と unreleased_upload.import_reveal_frames）を検証する。"""

import io
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image

import ocgtimes_reveal as rv
import unreleased_auto_approve as aa
import unreleased_quality as uq
import unreleased_upload as up
from name_normalize import fuzzy_key
from test_unreleased_upload import _SB

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import ocgtimes_watch as ow  # noqa: E402

VIDEO = "https://www.youtube.com/watch?v=v-QIhn_zLoM"
TX = "ハーピィ・レディ三姉妹・TX"
TX_EFFECT = "「ハーピィ・レディ」モンスター×３このカードは融合召喚でのみEXデッキから特殊召喚できる。"


def _png(color=(10, 20, 30), w=192, h=108) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _read(name, t, **kw):
    r = {"has_panel": True, "effect_complete": True, "name": name, "reading": "", "card_type": "融合モンスター",
         "attribute": "風", "race": "鳥獣族", "level": 8, "rank": None, "link_val": None, "atk": 3000, "def": "2100",
         "pendulum_scale": None, "pendulum_effect": "", "effect_text": TX_EFFECT, "card_bbox": [4, 14, 50, 81],
         "t": t, "image_key": f"k{t}"}
    r.update(kw)
    return r


# ──────────────────────────────────────────────
# まとめ方・多数決
# ──────────────────────────────────────────────

def test_group_reads_merges_near_names_and_skips_empty():
    yomi = "ハーピィ・レディさんしまいトライアングル・エクスタシー"
    reads = [_read(TX, 1, reading=yomi), _read("ハーピィ・レディ三姉妺・TX", 2, reading=yomi), _read(TX, 3), _read("", 4),
             _read("誘惑の鏡壁", 5)]
    groups = rv.group_reads(reads)
    assert [g["name"] for g in groups] == [TX, "誘惑の鏡壁"]
    assert [r["t"] for r in groups[0]["reads"]] == [1, 2, 3]


def test_group_reads_keeps_similar_series_names_apart():
    groups = rv.group_reads([_read("アマゾネスの格闘戦士－蹴殺の舞い－", 1), _read("アマゾネスの鎖使い－死に際の鎖舞い－", 2),
                             _read("アマゾネスの呪詛師－呪印の舞い－", 3)])
    assert len(groups) == 3


def test_decide_values_tie_broken_by_list_and_marked_conflict():
    # 試験の実例: TX のレベルを 8 と 9 に読んだ場面が1枚ずつ。一覧画面の 8 で決まり、食い違いとして残る
    values, conflicts = rv.decide_values([_read(TX, 1), _read(TX, 2, level=9)], {"name": TX, "level": 8})
    assert values["level"] == 8 and values["atk"] == 3000
    assert conflicts == {"level": {"votes": {"8": 2, "9": 1}, "list": 8}}


def test_decide_values_majority_without_list():
    values, conflicts = rv.decide_values([_read(TX, 1, atk=3000), _read(TX, 2, atk=3000), _read(TX, 3, atk=2000)], None)
    assert values["atk"] == 3000 and set(conflicts) == {"atk"}
    # 同数で一覧が無ければ最初の場面
    values, _ = rv.decide_values([_read(TX, 1, level=9), _read(TX, 2, level=8)], None)
    assert values["level"] == 9


def test_decide_effect_statuses():
    eff, unc = rv.decide_effect([_read(TX, 1), _read(TX, 2, effect_text=TX_EFFECT + " ")])
    assert eff["status"] == "agreed" and unc == [] and eff["frames"] == [1, 2]
    eff, unc = rv.decide_effect([_read(TX, 1), _read(TX, 2), _read(TX, 3, effect_text="違う文")])
    assert eff["status"] == "variants" and eff["effect_text"] == TX_EFFECT and unc == ["effect_text"]
    eff, unc = rv.decide_effect([_read(TX, 1, effect_complete=False, effect_text="途中")])
    assert eff["status"] == "incomplete" and eff["effect_text"] == "途中" and unc == ["effect_text"]
    eff, unc = rv.decide_effect([_read(TX, 1, has_panel=False, effect_text="")])
    assert eff["status"] == "no_panel" and unc == ["effect_text"]


def test_pick_frame_prefers_frame_agreeing_with_decided_values():
    g = {"reads": [_read(TX, 1, level=9, card_bbox=[0, 0, 60, 80]), _read(TX, 2, card_bbox=[0, 0, 50, 80])]}
    eff = {"frames": [1, 2]}
    assert rv.pick_frame(g, eff, {**{f: None for f in rv.VALUE_FIELDS}, "card_type": "融合モンスター", "attribute": "風",
                                  "race": "鳥獣族", "level": 8, "atk": 3000, "def": "2100"})["t"] == 2
    assert rv.pick_frame(g, eff)["t"] == 1  # 値を渡さなければ一番大きい場面


# ──────────────────────────────────────────────
# extract_reveal（API は偽物）
# ──────────────────────────────────────────────

@pytest.fixture
def fake_api(monkeypatch):
    """場面の読み取り・一覧の読み取り・カード側の読み直しを、画像の色で決まる偽の応答にする。"""
    state = {"reveal": {}, "list": [], "card_text": {}}

    def fake_reveal(client, image):
        return dict(state["reveal"][image["url"]])

    def fake_list(client, image):
        return [dict(c) for c in state["list"]]

    monkeypatch.setattr(rv, "read_reveal_frame", fake_reveal)
    monkeypatch.setattr(rv, "read_list_frame", fake_list)
    monkeypatch.setattr("card_crop.refine_card_box", lambda png, bbox: {"left": 0.02, "top": 0.13, "right": 0.26,
                                                                        "bottom": 0.75, "method": "contour"})
    monkeypatch.setattr("card_crop.make_effect_crop", lambda *a, **k: b"crop")
    import effect_text_reread as er
    monkeypatch.setattr(er, "_read_one", lambda client, model, crop, name: (
        {"effect_text": state["card_text"].get(name, ""), "unreadable": False}, "", {"input_tokens": 1}))
    import release_date_resolver
    monkeypatch.setattr(release_date_resolver, "resolve_release_date", lambda p: {
        "release_date": "2026-10-31", "status": "verified", "reason": "", "source_url": None})
    return state


def _frame(key, t):
    return {"image": {"url": key, "media_type": "image/png", "data": "", "width": 1920, "height": 1080,
                      "png": _png()}, "t": t}


def _set(state, key, t, name, **kw):
    r = _read(name, t, **kw)
    r.pop("t"), r.pop("image_key")
    state["reveal"][key] = r


def test_extract_reveal_builds_rows_with_checks(fake_api):
    _set(fake_api, "a", 5089, TX)
    _set(fake_api, "b", 5309, TX, level=9)
    _set(fake_api, "c", 5236, "誘惑の鏡壁", card_type="永続罠", attribute="", race="", level=None, atk=None,
         **{"def": None, "effect_text": "①：相手は「誘惑の鏡壁」を対象にできない。"})
    _set(fake_api, "d", 5000, "", has_panel=False, effect_text="")
    fake_api["list"] = [{"name": TX, "card_type": "融合モンスター", "attribute": "風", "race": "鳥獣族", "level": 8,
                         "rank": None, "link_val": None, "atk": 3000, "def": "2100"},
                        {"name": "誘惑の鏡壁", "card_type": "永続罠", "attribute": "", "race": "", "level": None,
                         "rank": None, "link_val": None, "atk": None, "def": None},
                        {"name": "パワー・オブ・ザ・デュエリスト", "card_type": "速攻魔法"}]
    fake_api["card_text"] = {TX: TX_EFFECT, "誘惑の鏡壁": "①：相手は「誘惑の鏡鷲」を対象にできない。"}

    out = rv.extract_reveal([_frame("a", 5089), _frame("b", 5309), _frame("c", 5236), _frame("d", 5000)],
                            [_frame("L", 6468)], video_url=VIDEO, product_name="IMMORTAL PHOENIX", client=object())

    tx, mirror = out["rows"]
    assert tx["name"] == TX and tx["level"] == 8 and tx["release_date"] == "2026-10-31"
    assert tx["source_url"] == VIDEO + "&t=5089s"   # 多数決と揃う場面を保存する
    raw = tx["extraction_raw"]
    assert raw["uncertain_fields"] == ["level_rank_link"] and tx["confidence"] == "medium"
    assert raw["source_image_url"] == "a" and raw["crop_suggestion"]["source_image_url"] == "a"
    assert raw["reveal"]["card_text_check"]["status"] == "agreed"
    assert raw["reveal"]["auto_approve_candidate"] is False     # 数値の食い違いがあるので候補にしない
    assert raw["reveal"]["list_check"] == {"status": "matched", "t": 6468, "name": TX}

    mraw = mirror["extraction_raw"]
    assert mirror["atk"] is None and mirror["def"] == "" and mirror["level"] is None
    assert mraw["reveal"]["card_text_check"]["status"] == "disagree"
    assert mraw["uncertain_fields"] == ["effect_text"]           # 食い違いは人に確かめさせる（文は字幕のまま）
    assert mirror["effect_text"].startswith("①：相手は「誘惑の鏡壁」")
    view = rv.admin_view(mraw)
    assert view["status"] == "disagree" and view["labels"]["first"] == "字幕（採用中）"
    assert any(op[0] == "replace" and op[1] == "壁" and op[2] == "鷲" for op in view["ops"])

    assert [c["name"] for c in out["list_only"]] == ["パワー・オブ・ザ・デュエリスト"]
    assert out["no_card_frames"] == [5000]


def test_extract_reveal_candidate_when_everything_agrees(fake_api):
    _set(fake_api, "a", 1, TX)
    fake_api["list"] = [{"name": TX, "card_type": "融合モンスター", "attribute": "風", "race": "鳥獣族", "level": 8,
                         "rank": None, "link_val": None, "atk": 3000, "def": "2100"}]
    fake_api["card_text"] = {TX: TX_EFFECT}
    row = rv.extract_reveal([_frame("a", 1)], [_frame("L", 9)], video_url=VIDEO, client=object())["rows"][0]
    assert row["confidence"] == "high" and row["extraction_raw"]["uncertain_fields"] == []
    assert row["extraction_raw"]["reveal"]["auto_approve_candidate"] is True
    assert rv.admin_view(row["extraction_raw"]) is None


def test_extract_reveal_without_list_is_not_candidate(fake_api):
    _set(fake_api, "a", 1, TX)
    fake_api["card_text"] = {TX: TX_EFFECT}
    row = rv.extract_reveal([_frame("a", 1)], [], video_url=VIDEO, client=object())["rows"][0]
    assert row["extraction_raw"]["reveal"]["list_check"]["status"] == "no_list"
    assert row["extraction_raw"]["reveal"]["auto_approve_candidate"] is False


def test_extract_reveal_pendulum_skips_check_and_marks_uncertain(fake_api):
    _set(fake_api, "a", 1, "Pのカード", card_type="ペンデュラム・効果モンスター", pendulum_scale=4,
         pendulum_effect="P効果", effect_text="モンスター効果")
    row = rv.extract_reveal([_frame("a", 1)], [], video_url=VIDEO, client=object())["rows"][0]
    raw = row["extraction_raw"]
    assert raw["reveal"]["card_text_check"]["status"] == "skipped"
    assert {"effect_text", "pendulum_effect"} <= set(raw["uncertain_fields"])
    assert row["pendulum_effect"] == "P効果" and row["pendulum_scale"] == 4


def test_card_text_check_error_keeps_subtitle(monkeypatch):
    monkeypatch.setattr("card_crop.make_effect_crop", MagicMock(side_effect=ValueError("枠が不正")))
    out = rv.card_text_check(object(), b"png", {"left": 0, "top": 0, "right": 1, "bottom": 1}, "効果モンスター", "X", "文")
    assert out["status"] == "error" and "枠が不正" in out["reason"]
    assert rv.admin_view({"reveal": {"card_text_check": out}})["status"] == "error"


# ──────────────────────────────────────────────
# 登録（import_reveal_frames）
# ──────────────────────────────────────────────

@pytest.fixture
def no_context(monkeypatch):
    monkeypatch.setattr(aa, "load_active_names", lambda sb: [])
    monkeypatch.setattr(aa, "load_text_context", lambda sb: None)
    monkeypatch.setattr(aa, "compute_text_check", lambda *a, **k: {"status": "checked", "strong": 0})


def _reveal_row(name, key, t):
    return {"name": name, "product_name": "IMMORTAL PHOENIX", "card_type": "通常罠", "effect_text": "効果",
            "confidence": "high", "source_url": f"{VIDEO}&t={t}s",
            "extraction_raw": {"source_image_url": key, "reveal": {"saved_frame_t": t, "auto_approve_candidate": True}}}


def test_import_reveal_frames_registers_and_stores_each_cards_frame(no_context):
    sb = _SB({"unreleased_cards": []})
    a, b, lst = _png((1, 0, 0)), _png((2, 0, 0)), _png((3, 0, 0))
    ka, kb = up.image_key_for(a), up.image_key_for(b)
    extract = MagicMock(return_value={"rows": [_reveal_row("カードA", ka, 10), _reveal_row("カードB", kb, 20)],
                                      "list_only": [{"name": "カードC", "list_t": 99}], "no_card_frames": [30]})
    list_extract = MagicMock(return_value=[
        {"name": "カードA", "product_name": "P", "card_type": "通常罠", "effect_text": "x", "confidence": "medium",
         "extraction_raw": {}},
        {"name": "カードC", "product_name": "P", "card_type": "通常罠", "effect_text": "y", "confidence": "medium",
         "extraction_raw": {}}])
    store = MagicMock(return_value=(True, "取込成功"))

    out = up.import_reveal_frames(sb, reveal_images=[(a, 10), (b, 20)], list_images=[(lst, 99)], video_url=VIDEO,
                                  note="メモ", product_name="IMMORTAL PHOENIX", extract=extract,
                                  list_extract=list_extract, store_image=store, known_keys=set())

    assert [c["name"] for c in out["inserted"]] == ["カードA", "カードB", "カードC"]
    # カードごとに、そのカードの場面の画像を保存する（一覧から補ったカードは一覧画面）
    saved = [(c.args[4], c.args[5]) for c in store.call_args_list]
    assert saved[:2] == [(ka, f"{VIDEO}&t=10s"), (kb, f"{VIDEO}&t=20s")]
    assert saved[2][1] == f"{VIDEO}&t=99s"
    rows = sb.tables["unreleased_cards"]
    a_row = next(r for r in rows if r["name"] == "カードA")
    assert a_row["extraction_raw"]["source"] == "image_upload"
    assert a_row["extraction_raw"]["upload"]["filename"] == "reveal_t10"
    assert "upload_source" in a_row["extraction_raw"]["auto_approve_verdict"]["reasons"]   # 自動承認しない
    # 一覧画面からは、紹介場面に無かったカードだけを補う
    assert {"name": "カードA", "reason": "紹介場面から登録済み（一覧からは補わない）"} in out["skipped"]
    assert out["list_only"] == ["カードC"] and out["list_fallback"][0]["inserted"] == ["カードC"]
    assert out["no_card_frames"] == [30]
    assert extract.call_args.kwargs["product_name"] == "IMMORTAL PHOENIX"


def test_group_reads_does_not_merge_near_names_without_same_reading():
    # 末尾だけ違う別カード（比 0.92）。ふりがなが違えばまとめない／ふりがなが無ければ迷ったら分ける
    groups = rv.group_reads([_read("真紅眼の黒竜", 1, reading="レッドアイズ・ブラックドラゴン"),
                             _read("真紅眼の黒炎竜", 2, reading="レッドアイズ・ブラックフレアドラゴン")])
    assert len(groups) == 2
    assert len(rv.group_reads([_read(TX, 1), _read("ハーピィ・レディ三姉妺・TX", 2)])) == 2
    # 長さが2字以上違えば、ふりがなが同じでもまとめない
    assert len(rv.group_reads([_read("六花精シクラン", 1, reading="x"), _read("六花精シクラメンス", 2, reading="x")])) == 2


def test_decide_values_ignores_empty_list_and_frame_values():
    # 一覧画面が小さくて読めなかった（空）ときに、紹介場面で読めた数値を消さない
    empty_list = {"name": TX, "level": None, "atk": None, "def": None}
    values, conflicts = rv.decide_values([_read(TX, 1, level=4, atk=1800, **{"def": "1200"})], empty_list)
    assert (values["level"], values["atk"], values["def"]) == (4, 1800, "1200") and conflicts == {}
    values, conflicts = rv.decide_values([_read(TX, 1, level=8), _read(TX, 2, level=9)], empty_list)
    assert values["level"] == 8 and conflicts["level"]["votes"] == {"8": 1, "9": 1}
    # 紹介場面の空と値が混ざる → 値を採って食い違いに残す
    values, conflicts = rv.decide_values([_read(TX, 1, atk=None), _read(TX, 2, atk=1800)], None)
    assert values["atk"] == 1800 and conflicts["atk"]["empty_frames"] == 1
    # 魔法・罠は全部空のまま
    values, conflicts = rv.decide_values([_read(TX, 1, level=None), _read(TX, 2, level=None)], empty_list)
    assert values["level"] is None and "level" not in conflicts
    # 紹介場面がどれも空（魔法・罠）なら、一覧の誤読の数値を入れずに食い違いとして残す
    values, conflicts = rv.decide_values([_read(TX, 1, level=None, atk=None), _read(TX, 2, level=None, atk=None)],
                                         {"name": TX, "level": 4, "atk": 1800})
    assert values["level"] is None and values["atk"] is None
    assert conflicts["level"] == {"votes": {}, "list": 4, "empty_frames": 2} and "atk" in conflicts
    # 1200 と "1200" は同じ値
    values, conflicts = rv.decide_values([_read(TX, 1, **{"def": "2100"})], {"name": TX, "def": 2100})
    assert "def" not in conflicts


def test_extract_reveal_name_uncertain_when_list_name_differs_or_missing(fake_api):
    _set(fake_api, "a", 1, TX)
    _set(fake_api, "b", 2, "誘惑の鏡壁", card_type="永続罠", attribute="", race="", level=None, atk=None, **{"def": None})
    fake_api["list"] = [{"name": "ハーピィ・レディ三姉妺・TX", "card_type": "融合モンスター", "level": 8}]
    fake_api["card_text"] = {TX: TX_EFFECT, "誘惑の鏡壁": TX_EFFECT}
    rows = rv.extract_reveal([_frame("a", 1), _frame("b", 2)], [_frame("L", 9)], video_url=VIDEO,
                             client=object())["rows"]
    tx, mirror = rows
    assert tx["extraction_raw"]["reveal"]["list_check"]["status"] == "matched"
    assert "name" in tx["extraction_raw"]["uncertain_fields"]
    assert tx["extraction_raw"]["reveal"]["auto_approve_candidate"] is False
    assert mirror["extraction_raw"]["reveal"]["list_check"]["status"] == "not_in_list"
    assert "name" in mirror["extraction_raw"]["uncertain_fields"]


def test_extract_reveal_continues_when_a_frame_fails(fake_api, monkeypatch):
    _set(fake_api, "a", 1, TX)
    fake_api["card_text"] = {TX: TX_EFFECT}
    real = rv.read_reveal_frame

    def flaky(client, image):
        if image["url"] == "bad":
            raise RuntimeError("overloaded")
        return real(client, image)
    monkeypatch.setattr(rv, "read_reveal_frame", flaky)
    monkeypatch.setattr(rv, "read_list_frame", MagicMock(side_effect=RuntimeError("overloaded")))
    out = rv.extract_reveal([_frame("a", 1), _frame("bad", 2)], [_frame("L", 9)], video_url=VIDEO, client=object())
    assert [r["name"] for r in out["rows"]] == [TX]
    assert {(x["kind"], x["t"]) for x in out["failed_frames"]} == {("reveal", 2), ("list", 9)}
    assert out["rows"][0]["extraction_raw"]["reveal"]["list_check"]["status"] == "list_unavailable"
    with pytest.raises(RuntimeError, match="すべて読めません"):
        rv.extract_reveal([_frame("bad", 2)], [], video_url=VIDEO, client=object())


def test_import_reveal_frames_fallback_tolerates_name_wobble_and_reports_missing(no_context, monkeypatch):
    import release_date_resolver
    monkeypatch.setattr(release_date_resolver, "resolve_release_date", lambda p: {
        "release_date": "2026-10-31", "status": "verified", "reason": "", "source_url": None})
    sb = _SB({"unreleased_cards": []})
    a, lst = _png((1, 0, 0)), _png((3, 0, 0))
    extract = MagicMock(return_value={"rows": [_reveal_row("カードA", up.image_key_for(a), 10)],
                                      "list_only": [{"name": "アマゾネスの格闘戦士－蹴殺の舞い－", "list_t": 99},
                                                    {"name": "見つからないカード", "list_t": 99}],
                                      "no_card_frames": []})
    # 一覧画面の読み直しで名前が1字揺れても補う。収録商品は --product に揃える
    list_extract = MagicMock(return_value=[
        {"name": "アマゾネスの格闘戦士－蹴殺の舞ぃ－", "product_name": "", "card_type": "効果モンスター",
         "effect_text": "x", "confidence": "medium", "extraction_raw": {}},
        {"name": "カードA", "product_name": "", "card_type": "通常罠", "effect_text": "y", "confidence": "medium",
         "extraction_raw": {}}])
    out = up.import_reveal_frames(sb, reveal_images=[(a, 10)], list_images=[(lst, 99)], video_url=VIDEO,
                                  product_name="IMMORTAL PHOENIX", extract=extract, list_extract=list_extract,
                                  store_image=MagicMock(return_value=(True, "ok")), known_keys=set())
    added = next(r for r in sb.tables["unreleased_cards"] if r["name"] == "アマゾネスの格闘戦士－蹴殺の舞ぃ－")
    assert added["product_name"] == "IMMORTAL PHOENIX" and added["release_date"] == "2026-10-31"
    assert out["list_fallback"][0]["missing"] == [fuzzy_key("見つからないカード")]


def test_near_name_rejects_similar_but_longer_names():
    assert rv.near_name(fuzzy_key("アマゾネスの剣聖"), fuzzy_key("アマゾネスの剣聖"))
    assert rv.near_name(fuzzy_key("アマゾネスの格闘戦士－蹴殺の舞い－"), fuzzy_key("アマゾネスの格闘戦士－蹴殺の舞ぃ－"))
    assert not rv.near_name(fuzzy_key("六花精シクラン"), fuzzy_key("六花精シクラメンス"))


def test_import_uploaded_image_product_override_drops_previous_verified_date(no_context, monkeypatch):
    # 抽出時に別商品で照合した日付を、上書き後の商品で照合できないときに「記事の日付」として採らない
    import release_date_resolver
    monkeypatch.setattr(release_date_resolver, "resolve_release_date", lambda p: {
        "release_date": None, "status": "unverified", "reason": "x", "source_url": None})
    sb = _SB({"unreleased_cards": []})
    extract = MagicMock(return_value=[
        {"name": "カードX", "product_name": "別の商品", "release_date": "2026-11-20", "card_type": "通常罠",
         "effect_text": "x", "confidence": "medium",
         "extraction_raw": {"release_date_evidence": {"status": "verified", "candidate_date": None}}}])
    up.import_uploaded_image(sb, data_url=up._data_url(_png()), source_url=VIDEO, extract=extract,
                             store_image=MagicMock(return_value=(True, "ok")), known_keys=set(),
                             product_name="IMMORTAL PHOENIX")
    row = sb.tables["unreleased_cards"][0]
    assert row["product_name"] == "IMMORTAL PHOENIX" and row["release_date"] is None


def test_admin_import_image_reveal_layout(monkeypatch):
    import base64
    from flask import Flask
    import admin_unreleased as au
    app = Flask(__name__)
    app.register_blueprint(au.admin_bp)
    monkeypatch.setattr(au, "_require_admin_key", lambda: None)
    monkeypatch.setattr(au, "_supabase", object())
    calls = []

    def fake(sb, **kw):
        calls.append(kw)
        return {"inserted": [{"id": 5, "name": TX}], "skipped": [], "images": {5: "取込成功"}, "extracted": 1}
    monkeypatch.setattr(up, "import_reveal_frames", fake)
    client = app.test_client()
    img = "data:image/png;base64," + base64.b64encode(_png()).decode()
    resp = client.post("/api/admin/unreleased/import-image", json={
        "image": img, "source_url": VIDEO, "layout": "reveal", "product_name": "IMMORTAL PHOENIX"})
    assert resp.status_code == 201 and resp.get_json()["inserted"] == 1
    assert calls[0]["reveal_images"][0][1] is None and calls[0]["product_name"] == "IMMORTAL PHOENIX"
    assert client.post("/api/admin/unreleased/import-image",
                       json={"image": img, "source_url": VIDEO, "layout": "x"}).status_code == 400
    bad = client.post("/api/admin/unreleased/import-image",
                      json={"image": "data:image/png;base64,@@", "source_url": VIDEO, "layout": "reveal"})
    assert bad.status_code == 400


def test_import_reveal_frames_requires_reveal_frames():
    with pytest.raises(up.UploadError, match="紹介場面"):
        up.import_reveal_frames(_SB({}), reveal_images=[], video_url=VIDEO)


def test_import_reveal_frames_rejects_bad_url():
    with pytest.raises(up.UploadError):
        up.import_reveal_frames(_SB({}), reveal_images=[(_png(), 1)], video_url="javascript:alert(1)")


def test_attach_release_date_helper(monkeypatch):
    import release_date_resolver
    monkeypatch.setattr(release_date_resolver, "resolve_release_date", lambda p: {
        "release_date": None, "status": "unverified", "reason": "商品名が未指定", "source_url": None})
    row = {"name": "X", "product_name": "", "release_date": None, "extraction_raw": {}}
    uq.attach_release_date(row, {})
    assert row["release_date"] is None and row["extraction_raw"]["release_date_evidence"]["status"] == "unverified"


# ──────────────────────────────────────────────
# ツール（tools/ocgtimes_watch.py）
# ──────────────────────────────────────────────

def test_parse_times_splits_reveal_and_list():
    assert ow.parse_times("816, 885,L6448,l6458") == [("reveal", 816), ("reveal", 885), ("list", 6448), ("list", 6458)]
    with pytest.raises(SystemExit):
        ow.parse_times("81x")


def test_parse_watch_reveal_sections():
    from test_ocgtimes_watch import DESC, _watch_html
    info = ow.parse_watch(_watch_html({"isLiveNow": False, "endTimestamp": "y"}, DESC))
    assert info["reveal_sections"] == [
        {"start": 772, "end": 5922, "label": "新規「六花」カード公開"},
        {"start": 5922, "end": 6200, "label": "新規カード「パワー・オブ・ザ・デュエリスト」公開"}]
    # 商品CMの「初公開」や「新規カードまとめ」は紹介場面ではない
    assert not ow._REVEAL_RE.search("「IMMORTAL PHOENIX」商品CM初公開")
    assert not ow._REVEAL_RE.search("本日の新規カードまとめ")


def test_cmd_import_reveal_mode(tmp_path, monkeypatch):
    import os
    import time
    import types
    from datetime import datetime, timedelta, timezone
    since = datetime.now(timezone.utc) - timedelta(seconds=5)
    for i, color in enumerate([(1, 0, 0), (2, 0, 0), (3, 0, 0)]):
        f = tmp_path / f"{i}.tmp"
        f.write_bytes(_png(color))
        os.utime(f, (time.time() + i, time.time() + i))
    calls = []

    def fake_import(sb, **kw):
        calls.append(kw)
        return {"inserted": [{"id": 7, "name": TX, "extraction_raw": {
            "uncertain_fields": [], "reveal": {"card_text_check": {"status": "agreed"}, "auto_approve_candidate": True,
                                               "frames": [{"t": 5089}]}}}],
                "skipped": [], "images": {7: "取込成功"}, "extracted": 1, "list_only": [], "list_fallback": [],
                "no_card_frames": []}

    monkeypatch.setattr(up, "import_reveal_frames", fake_import)
    monkeypatch.setitem(sys.modules, "supabase", types.SimpleNamespace(create_client=lambda *a: object()))
    monkeypatch.setenv("SUPABASE_URL", "https://x.example")
    monkeypatch.setenv("SUPABASE_KEY", "k")
    monkeypatch.setattr(ow, "FRAMES_ROOT", str(tmp_path / "keep"))
    monkeypatch.setattr(ow, "_notify", lambda m: None)
    args = types.SimpleNamespace(video_id="v-QIhn_zLoM", since=since.isoformat(), times="5089,5309,L6468",
                                 note="メモ", dir=str(tmp_path), no_sheet=True, no_notify=True,
                                 layout="reveal", product="IMMORTAL PHOENIX")

    out = ow.cmd_import(args)

    kw = calls[0]
    assert [t for _, t in kw["reveal_images"]] == [5089, 5309] and [t for _, t in kw["list_images"]] == [6468]
    assert kw["video_url"] == VIDEO and kw["product_name"] == "IMMORTAL PHOENIX" and kw["note"] == "メモ"
    assert sorted(os.listdir(tmp_path / "keep" / "v-QIhn_zLoM")) == ["list_t6468.png", "reveal_t5089.png",
                                                                     "reveal_t5309.png"]
    assert out["summary"] == "紹介場面2画面・一覧1画面・追加1枚"
    assert out["results"][0]["inserted"][0]["auto_approve_candidate"] is True


def test_cmd_import_reveal_mode_needs_reveal_times(tmp_path, monkeypatch):
    import types
    from datetime import datetime, timedelta, timezone
    (tmp_path / "a.png").write_bytes(_png())
    monkeypatch.setattr(ow, "_load_env", lambda: None)
    args = types.SimpleNamespace(video_id="x", since=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                                 times="L1", note="", dir=str(tmp_path), no_sheet=True, no_notify=True,
                                 layout="reveal", product="")
    assert "紹介場面の時刻がありません" in ow.cmd_import(args)["error"]


def test_fuzzy_key_sanity():
    assert fuzzy_key(TX) == fuzzy_key("ハーピィ・レディ三姉妹・ＴＸ")
