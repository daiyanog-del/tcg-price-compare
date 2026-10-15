"""信頼度 medium の扱い（uncertain_fields）・半角記号の全角補正・書式と n-gram の二重計上の解消のテスト。

evaluate の条件そのものは tests/test_unreleased_auto_approve.py にある。ここは抽出・スキーマ・集計側。
"""
import unreleased_extractor as ex
import unreleased_quality as uq
import unreleased_text_check as tc
from tools import auto_approve_report as report


# ── 半角→全角の補正 ──

def test_normalize_effect_punct_converts_ascii_outside_quotes():
    assert ex.normalize_effect_punct("①:このカード(自身)を除外する!?") == "①：このカード（自身）を除外する！？"
    assert ex.normalize_effect_punct("A&B/C~D") == "A＆B／C～D"


def test_normalize_effect_punct_keeps_quoted_names():
    # 「」の中のカード名に半角が正しく含まれるケースを壊さない
    s = "「E-HERO (X)」を手札に加え(る)。"
    assert ex.normalize_effect_punct(s) == "「E-HERO (X)」を手札に加え（る）。"


def test_normalize_effect_punct_leaves_other_ascii_and_empty():
    assert ex.normalize_effect_punct("攻撃力+500、1,000") == "攻撃力+500、1,000"  # + , は半角の実在があり対象外
    assert ex.normalize_effect_punct("") == ""


def _card_model(**over):
    ExtractedCard, _ = ex._get_pydantic_models()
    base = {"name": "テスト", "card_type": "効果モンスター", "effect_text": "①:発動できる(任意)。",
            "pendulum_effect": "②:(P)", "confidence": "medium", "uncertain_fields": ["atk"]}
    base.update(over)
    return ExtractedCard(**base)


def test_validate_and_fix_normalizes_effect_and_pendulum_only():
    row = ex._validate_and_fix(_card_model(name="名(前)"), "https://example.com/")
    assert row["effect_text"] == "①：発動できる（任意）。"
    assert row["pendulum_effect"] == "②：（P）"
    assert row["name"] == "名(前)"  # カード名には触れない
    assert "uncertain_fields" not in row  # テーブル列ではない（extraction_raw に入れる）


# ── スキーマ ──

def test_schema_has_uncertain_fields_enum():
    _, Result = ex._get_pydantic_models()
    props = uq.strict_schema(Result.model_json_schema())
    schema_text = str(props)
    card = props["$defs"]["ExtractedCard"]
    assert "uncertain_fields" in card["required"]
    items = card["properties"]["uncertain_fields"]
    assert items["type"] == "array"
    assert "minItems" not in schema_text  # 過去に minItems で実API 400 になった
    enum = items["items"].get("enum") or props["$defs"].get("UncertainField", {}).get("enum")
    assert tuple(enum) == uq.UNCERTAIN_FIELDS


def test_prompts_mention_uncertain_fields():
    assert "uncertain_fields" in ex._SYSTEM_PROMPT
    assert "uncertain_fields" in ex._SYSTEM_PROMPT_X  # X 用も共通部分を引き継ぐ


# ── 書式と n-gram の二重計上 ──

def test_format_and_ngram_not_double_counted():
    text = "①：このカードが召喚に成功した場合に発動できる。デッキから(レベル4以下のモンスター1体)を手札に加える。"
    strong = [s for s in tc.check_card({"name": "テスト", "effect_text": text})["suspects"]
              if s["severity"] == "strong"]
    kinds = {s["kind"] for s in strong}
    assert "format" in kinds and "unknown_ngram" not in kinds


def test_real_ngram_error_elsewhere_still_flagged():
    text = "①：このカードが召喚に成功した場合に発動できる。デッキから(レベル4以下のモンスター1体)を基地に加える。"
    kinds = [s["kind"] for s in tc.check_card({"name": "テスト", "effect_text": text})["suspects"]
             if s["severity"] == "strong"]
    assert kinds.count("format") == 2  # 「(」「)」


# ── 集計 ──

def test_report_confidence_by_uncertain_fields():
    def c(i, status, conf, fields, edited=None):
        raw = {"auto_approve_verdict": {"eligible": False, "reasons": []}}
        if fields is not None:
            raw["uncertain_fields"] = fields
        if edited is not None:
            raw["approval_snapshot"] = {"edited_fields": edited}
        return {"id": i, "name": f"c{i}", "status": status, "confidence": conf, "extraction_raw": raw}

    r = report.summarize([
        c(1, "approved", "high", [], []),
        c(2, "approved", "medium", ["effect_text", "level_rank_link"], ["effect_text"]),
        c(3, "approved", "medium", ["level_rank_link"], []),
        c(4, "rejected", "medium", ["atk"]),
        c(5, "approved", "medium", None, []),
        c(6, "approved", "low", [], ["name"]),
    ])["confidence"]
    assert r["by_confidence"]["high"] == {"approved_clean": 1}
    assert r["by_confidence"]["medium"] == {"approved_edited": 1, "approved_clean": 2, "rejected": 1}
    assert r["by_confidence"]["low"] == {"approved_edited": 1}
    assert r["medium_without_fields"] == 1
    assert r["by_field"]["effect_text"] == {"n": 1, "judged": 1, "edited_that_field": 1}
    assert r["by_field"]["level_rank_link"] == {"n": 2, "judged": 2}
    assert r["by_field"]["atk"] == {"n": 1, "rejected": 1}
    report.print_report(report.summarize([]) | {"confidence": r})  # 表示で落ちない
