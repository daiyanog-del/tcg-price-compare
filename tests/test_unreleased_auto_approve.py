"""自動承認の判定（シャドーモード）unreleased_auto_approve.py と集計スクリプトのテスト。"""
import copy

import pytest

import unreleased_auto_approve as aa
import unreleased_text_check as tc
from tests.fake_supabase import FakeSB
from tools import auto_approve_report as report


# 公式の効果文らしい（誤字検証で疑義が付かない）文章
CLEAN_TEXT = "①：このカードが特殊召喚に成功した場合に発動できる。自分のデッキからカード1枚を手札に加える。"


def _card(**over):
    """全条件を満たすX経由カード。"""
    card = {
        "name": "テスト竜", "card_type": "効果モンスター", "attribute": "闇", "race": "ドラゴン族",
        "level": 4, "rank": None, "link_val": None, "atk": 1800, "def": "1000",
        "effect_text": CLEAN_TEXT, "pendulum_scale": None, "pendulum_effect": "",
        "product_name": "パック", "release_date": "2026-11-01", "confidence": "high",
        "extraction_raw": {
            "source": "x_tweet",
            "recognition_review": {"status": "verified"},
            "release_date_evidence": {"status": "verified"},
            "crop_suggestion": {"left": .1, "top": .1, "right": .6, "bottom": .8, "width": 1000, "height": 1000,
                                "method": "contour"},
        },
    }
    raw_over = over.pop("raw", {})
    card.update(over)
    card["extraction_raw"] = {**card["extraction_raw"], **raw_over}
    return card


def _ev(card, others=()):
    return aa.evaluate(card, other_names=list(others))


# ── evaluate: 各条件 ──

def test_all_conditions_met():
    v = _ev(_card())
    assert v["eligible"] is True and v["reasons"] == [] and v["version"] == aa.VERSION and v["at"]


@pytest.mark.parametrize("conf", ["low", None, "unknown"])
def test_low_confidence_is_ineligible(conf):
    assert "confidence_low" in _ev(_card(confidence=conf))["reasons"]


def test_high_ignores_uncertain_fields():
    assert _ev(_card(raw={"uncertain_fields": ["effect_text"]}))["eligible"] is True


@pytest.mark.parametrize("fields", [None, [], "effect_text"])
def test_medium_without_uncertain_fields_is_ineligible(fields):
    raw = {} if fields is None else {"uncertain_fields": fields}
    v = _ev(_card(confidence="medium", raw=raw))
    assert v["eligible"] is False and v["reasons"] == ["medium_uncertain_unknown"]


@pytest.mark.parametrize("fields", [["attribute"], ["race"], ["product_name"], ["release_date"],
                                    ["attribute", "race", "release_date", "product_name"]])
def test_medium_with_unimportant_uncertain_fields_passes(fields):
    v = _ev(_card(confidence="medium", raw={"uncertain_fields": fields}))
    assert v["eligible"] is True and v["reasons"] == [] and "uncertain_fields" not in v


@pytest.mark.parametrize("field", sorted(["name", "effect_text", "pendulum_effect", "card_type",
                                          "level_rank_link", "atk", "def", "pendulum_scale"]))
def test_medium_with_important_uncertain_field_is_ineligible(field):
    v = _ev(_card(confidence="medium", raw={"uncertain_fields": ["attribute", field]}))
    assert v["eligible"] is False and v["reasons"] == ["medium_uncertain_fields"]
    assert v["uncertain_fields"] == [field]  # 重要欄だけを記録する


def test_medium_with_unknown_field_value_is_ineligible():
    v = _ev(_card(confidence="medium", raw={"uncertain_fields": ["something_new"]}))
    assert v["reasons"] == ["medium_uncertain_fields"]


def test_verdict_reason_labels_include_field_names():
    v = _ev(_card(confidence="medium", raw={"uncertain_fields": ["effect_text", "atk"]}))
    labels = aa.verdict_reason_labels(v)
    assert len(labels) == 1 and "効果テキスト" in labels[0] and "攻撃力" in labels[0]
    # 旧バージョンの判定記録（confidence_not_high）も表示できる
    assert aa.verdict_reason_labels({"reasons": ["confidence_not_high"]}) == ["抽出の信頼度が高ではない"]
    assert aa.verdict_reason_labels(None) == []


@pytest.mark.parametrize("review", [None, {"status": "needs_review"}, "verified"])
def test_recognition_must_be_verified(review):
    assert "recognition_not_verified" in _ev(_card(raw={"recognition_review": review}))["reasons"]


@pytest.mark.parametrize("ev", [None, {"status": "unverified"}, {"status": "conflict"}])
def test_release_date_must_be_verified(ev):
    assert "release_date_not_verified" in _ev(_card(raw={"release_date_evidence": ev}))["reasons"]


def test_article_adopted_date_is_not_verified():
    ev = {"status": "unverified", "adopted_from": "article"}
    assert "release_date_not_verified" in _ev(_card(raw={"release_date_evidence": ev}))["reasons"]


def test_no_crop_suggestion_ok():
    assert _ev(_card(raw={"crop_suggestion": None}))["eligible"] is True


def test_x_vision_crop_rejected_even_whole_image():
    whole = {"left": 0, "top": 0, "right": 1, "bottom": 1, "width": 700, "height": 1020, "method": "vision"}
    assert "crop_not_contour" in _ev(_card(raw={"crop_suggestion": whole}))["reasons"]


@pytest.mark.parametrize("box", [
    {"left": -.1, "top": 0, "right": .6, "bottom": .8},
    {"left": .5, "top": 0, "right": .4, "bottom": .8},
    {"left": 0, "top": 0, "right": 1.2, "bottom": .8},
    {"left": "0", "top": 0, "right": .5, "bottom": .8},
    {"left": float("nan"), "top": 0, "right": .5, "bottom": .8},
])
def test_invalid_crop_coordinates(box):
    sug = {**box, "method": "contour"}
    assert "crop_invalid" in _ev(_card(raw={"crop_suggestion": sug}))["reasons"]


def test_crop_too_small():
    sug = {"left": .1, "top": .1, "right": .2, "bottom": .3, "width": 1000, "height": 1000, "method": "contour"}
    assert "crop_too_small" in _ev(_card(raw={"crop_suggestion": sug}))["reasons"]


def test_crop_bad_aspect():
    sug = {"left": 0, "top": 0, "right": .9, "bottom": .3, "width": 1000, "height": 1000, "method": "contour"}
    assert "crop_invalid" in _ev(_card(raw={"crop_suggestion": sug}))["reasons"]


def test_official_whole_image_ok_and_partial_rejected():
    whole = {"left": 0, "top": 0, "right": 1, "bottom": 1, "method": "vision"}
    assert _ev(_card(raw={"source": "official_html", "crop_suggestion": whole}))["eligible"] is True
    part = {"left": .1, "top": .1, "right": .6, "bottom": .8, "method": "contour"}
    assert "crop_not_applied" in _ev(_card(raw={"source": "official_html", "crop_suggestion": part}))["reasons"]


def test_lacks_card_body():
    card = _card(effect_text="", atk=None, level=None)
    assert "lacks_card_body" in _ev(card)["reasons"]


def test_same_name_other_card_blocks_using_fuzzy_key():
    assert "same_name_exists" in _ev(_card(name="テスト竜"), ["別名", "テスト竜"])["reasons"]
    assert "same_name_exists" not in _ev(_card(), ["別のカード"])["reasons"]


def test_same_name_unchecked_when_names_unavailable():
    v = aa.evaluate(_card(), other_names=None)
    assert v["eligible"] is False and "same_name_unchecked" in v["reasons"]


def test_multiple_reasons_collected():
    v = _ev(_card(confidence="low", raw={"release_date_evidence": None}))
    assert {"confidence_low", "release_date_not_verified"} <= set(v["reasons"])


def test_all_reason_codes_have_labels():
    codes = {"confidence_not_high", "confidence_low", "medium_uncertain_fields", "medium_uncertain_unknown",
             "recognition_not_verified", "release_date_not_verified", "crop_not_contour",
             "crop_invalid", "crop_too_small", "crop_not_applied", "lacks_card_body", "same_name_exists",
             "same_name_unchecked", "evaluation_error"}
    assert codes <= set(aa.REASON_LABELS)
    assert aa.reason_label("unknown_x") == "unknown_x"


# ── 取込時の記録 ──

def test_annotate_row_adds_verdict_and_snapshot():
    row = _card()
    verdict = aa.annotate_row(row, [])
    raw = row["extraction_raw"]
    assert raw["auto_approve_verdict"] == verdict
    snap = raw["extracted_snapshot"]
    assert set(snap) == set(aa.SNAPSHOT_FIELDS)
    assert snap["name"] == "テスト竜" and snap["atk"] == 1800 and snap["release_date"] == "2026-11-01"


def test_annotate_row_survives_evaluation_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(aa, "evaluate", boom)
    row = _card()
    v = aa.annotate_row(row, [])
    assert v["eligible"] is False and v["reasons"] == ["evaluation_error"]


def test_load_active_names_excludes_rejected_and_paginates():
    rows = [{"id": i, "name": f"n{i}", "status": "pending"} for i in range(1, 1201)]
    rows.append({"id": 9999, "name": "却下", "status": "rejected"})
    sb = FakeSB({"unreleased_cards": rows})
    names = aa.load_active_names(sb)
    assert len(names) == 1200 and "却下" not in names


def test_load_active_names_failure_returns_none():
    class Broken:
        def table(self, _):
            raise RuntimeError("down")
    assert aa.load_active_names(Broken()) is None


# ── edited_fields ──

def test_edited_fields_diff():
    snap = aa.build_extracted_snapshot(_card())
    current = _card(atk=2000, effect_text=" " + CLEAN_TEXT + " ", release_date="2026-11-02")
    assert aa.compute_edited_fields(snap, current) == ["atk", "release_date"]


def test_edited_fields_none_vs_empty_and_number_types():
    snap = aa.build_extracted_snapshot(_card(pendulum_effect=None, level=4))
    current = _card(pendulum_effect="", level=4.0)
    assert aa.compute_edited_fields(snap, current) == []


def test_edited_fields_null_for_legacy():
    assert aa.compute_edited_fields(None, _card()) is None


def test_build_approval_snapshot():
    card = _card()
    aa.annotate_row(card, [])
    card["extraction_raw"]["crop_state"] = {"mode": "manual"}
    card["atk"] = 9999
    snap = aa.build_approval_snapshot(card, "bulk", "2026-10-15T00:00:00+00:00")
    assert snap == {"at": "2026-10-15T00:00:00+00:00", "via": "bulk", "edited_fields": ["atk"],
                    "crop_mode": "manual", "verdict_eligible": True,
                    "text_check": {"version": tc.VERSION, "status": "checked", "strong": 0, "weak": 0, "strong_by_kind": {}}}


def test_build_approval_snapshot_legacy_card():
    snap = aa.build_approval_snapshot({"name": "x", "extraction_raw": {}}, "single")
    assert snap["edited_fields"] is None and snap["crop_mode"] is None and snap["verdict_eligible"] is None


def test_save_approval_snapshots_merges_latest_raw():
    sb = FakeSB({"unreleased_cards": [
        {"id": 1, "extraction_raw": {"crop_state": {"mode": "auto"}}},
        {"id": 2, "extraction_raw": {"keep": 1}},
    ]})
    n = aa.save_approval_snapshots(sb, [(1, {"via": "single"}), (2, {"via": "bulk"}), (3, {"via": "x"})])
    assert n == 2
    rows = {r["id"]: r["extraction_raw"] for r in sb.tables["unreleased_cards"]}
    assert rows[1]["crop_state"] == {"mode": "auto"} and rows[1]["approval_snapshot"] == {"via": "single"}
    assert rows[2]["keep"] == 1 and rows[2]["approval_snapshot"]["via"] == "bulk"


# ── 集計スクリプト ──

def test_report_summarize():
    def c(i, status, eligible, edited=None, reasons=()):
        raw = {"auto_approve_verdict": {"eligible": eligible, "reasons": list(reasons)}}
        if edited is not None:
            raw["approval_snapshot"] = {"edited_fields": edited}
        return {"id": i, "name": f"c{i}", "status": status, "extraction_raw": raw}

    cards = [
        c(1, "approved", True, []),
        c(2, "approved", True, ["atk"]),
        c(3, "rejected", True),
        c(4, "approved", False, [], ["confidence_not_high"]),
        c(5, "approved", False, ["name"], ["confidence_not_high"]),
        c(6, "linked", False, None, ["crop_not_contour"]),       # 記録なしの旧データは数えない
        c(7, "pending", True),                                    # 対象外
        {"id": 8, "name": "判定なし", "status": "approved", "extraction_raw": {}},
    ]
    r = report.summarize(copy.deepcopy(cards))
    assert r["eligible_approved"] == 2
    assert [x["id"] for x in r["eligible_edited"]] == [2] and r["eligible_edited"][0]["edited_fields"] == ["atk"]
    assert [x["id"] for x in r["eligible_rejected"]] == [3]
    assert r["ineligible_clean"] == 1 and r["ineligible_clean_reasons"] == {"confidence_not_high": 1}
