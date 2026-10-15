"""効果テキストの誤字検証 unreleased_text_check.py・辞書生成・自動承認への配線のテスト。

実辞書（data/text_check_dict.json.gz）に依存するテストは最後の節だけに分け、
それ以外は小さな合成コーパスから作った辞書で検証する。
"""
import copy
import gzip
import json
import os

import pytest

import unreleased_auto_approve as aa
import unreleased_text_check as tc
from tools import auto_approve_report as report
from tools import build_text_check_dict as builder

CORPUS_TEXTS = [
    ("青眼の白龍", "①：このカードが特殊召喚に成功した場合に発動できる。自分のデッキからカード1枚を手札に加える。"),
    ("黒き魔術師", "①：自分フィールドの「青眼の白龍」が破壊された場合に発動できる。このカードを墓地から特殊召喚する。\n"
                 "②：このカードが墓地に存在する場合、自分の墓地のモンスター1体を除外して発動できる。デッキからカード1枚を手札に加える。"),
    ("光の守護者", "このカードが召喚に成功した時、自分フィールドのモンスター1体を対象として発動できる。そのモンスターの攻撃力は500アップする。"),
]


@pytest.fixture(scope="module")
def d():
    corpus = [{"konami_id": i, "name": n, "texts": [t]} for i, (n, t) in enumerate(CORPUS_TEXTS)]
    data = builder.build_dict(corpus)
    return tc.TextCheckDict(data["ngrams"].split("\n"), data["card_names"], data["quote_words"], data["meta"])


def _kinds(res, kind=None, severity=None):
    return [s for s in res if (kind is None or s["kind"] == kind) and (severity is None or s["severity"] == severity)]


# ── トークン化 ──

def test_tokenize_folds_digits_quotes_and_keeps_offsets():
    text = "「青眼の白龍」を１５００以下の①"
    toks = tc.tokenize(text)
    s = tc.token_string(toks)
    assert s == "「" + tc._Q + "」を" + tc._DIG + "以下の①"
    # 数字の伏せ字は元の位置（全角2桁分）を指す
    dig = [t for t in toks if t[0] == tc._DIG][0]
    assert text[dig[1]:dig[2]] == "１５００"


def test_tokenize_width_agnostic_and_ignores_newlines():
    a = tc.token_string(tc.tokenize("1ターンに1度\nＸ素材"))
    b = tc.token_string(tc.tokenize("１ターンに１度X素材"))
    assert a == b


def test_tokenize_masks_own_name_without_quotes():
    s = tc.token_string(tc.tokenize("ブラックロッドを発動", "ブラックロッド"))
    assert s == tc._Q + "を発動"


# ── ① n-gram ──

def test_clean_official_like_text_has_no_suspect(d):
    res = tc.check_text("①：このカードが特殊召喚に成功した場合に発動できる。自分のデッキからカード1枚を手札に加える。", dictionary=d)
    assert res == []


def test_confusable_char_gets_strong_with_suggestion(d):
    text = "①：このカードが特殊召喚に成功した場合に発動できる。自分のデッキから力ード1枚を手札に加える。"
    res = _kinds(tc.check_text(text, dictionary=d), "unknown_ngram", "strong")
    assert len(res) == 1
    s = res[0]
    assert text[s["start"]:s["end"]] == s["text"] == "力"
    assert s["suggestion"] == "カ"


def test_unrelated_garbage_is_strong_without_suggestion(d):
    res = _kinds(tc.check_text("①：このカードが特殊召喚に成功した場合にzqx発動できる。", dictionary=d), "unknown_ngram")
    assert any(s["severity"] == "strong" for s in res)


def test_text_inside_quotes_is_not_ngram_checked(d):
    # 「」の中身は新テーマ名でありうるので ① の対象外（② で扱う）
    res = tc.check_text("①：自分フィールドの「ぬめぬめ怪獣」が破壊された場合に発動できる。", dictionary=d, card_name="ぬめぬめ怪獣")
    assert _kinds(res, "unknown_ngram", "strong") == []


def test_numbers_do_not_cause_suspect(d):
    res = tc.check_text("①：このカードが特殊召喚に成功した場合に発動できる。そのモンスターの攻撃力は２０００アップする。", dictionary=d)
    assert _kinds(res, "unknown_ngram", "strong") == []


def test_flavor_text_skips_ngram_and_is_reported_unverified(d):
    card = {"name": "怪獣", "effect_text": "ふわふわした優しい雲の精。いつも空を眺めている。"}
    res = tc.check_card(card, dictionary=d)
    assert res["status"] == "checked" and res["unverified_fields"] == ["effect_text"]
    assert res["strong"] == 0


# ── ② 「」内の名前 ──

def test_known_quote_word_is_ok(d):
    assert _kinds(tc.check_text("自分の墓地の「青眼の白龍」を特殊召喚できる。", dictionary=d), "unknown_name") == []


def test_unknown_name_with_close_existing_word_is_strong(d):
    res = _kinds(tc.check_text("自分の墓地の「青眼の白竜」を特殊召喚できる。", dictionary=d), "unknown_name")
    assert len(res) == 1 and res[0]["severity"] == "strong"
    assert res[0]["suggestion"] == "青眼の白龍"


def test_unknown_name_without_neighbor_is_weak(d):
    res = _kinds(tc.check_text("自分の墓地の「ぬめぬめ怪獣」を特殊召喚できる。", dictionary=d), "unknown_name")
    assert len(res) == 1 and res[0]["severity"] == "weak" and res[0]["suggestion"] is None


def test_new_name_is_allowed_when_it_is_the_cards_own_name_or_repeats(d):
    own = tc.check_text("自分の墓地の「ぬめぬめ怪獣」を特殊召喚できる。", dictionary=d, card_name="ぬめぬめ怪獣")
    assert _kinds(own, "unknown_name") == []
    twice = tc.check_text("「ぬめぬめ怪獣」を墓地へ送る。その後「ぬめぬめ怪獣」を特殊召喚できる。", dictionary=d)
    assert _kinds(twice, "unknown_name") == []


def test_name_known_from_other_unreleased_cards_and_context(d):
    assert _kinds(tc.check_text("「ぬめぬめ怪獣」を特殊召喚できる。", dictionary=d, extra_names=["ぬめぬめ怪獣"]),
                  "unknown_name") == []
    ctx = tc.TextContext()
    ctx.add_card({"effect_text": "「ぬめぬめ怪獣」を特殊召喚できる。"})
    res = tc.check_card({"name": "別カード", "effect_text": "「ぬめぬめ怪獣」を墓地へ送って発動できる。"},
                        dictionary=d, context=ctx, context_has_self=False)
    assert _kinds(res["suspects"], "unknown_name") == []


# ── ③ 書式 ──

@pytest.mark.parametrize("text,expect", [
    ("①：自分は「青眼の白龍」を特殊召喚できる。②：「このカードが発動できる。", "「」の開き側"),
    ("①：自分は特殊召喚できる。）", "（）の閉じ側"),
    ("①:自分は特殊召喚できる。", "半角の「:」"),
    ("①：自分は特殊召喚できる。。", "句読点が連続"),
    ("①：自分は特殊召喚できる。③：自分は破壊できる。", "番号が飛んで"),
    ("①：自分は特殊召喚できる。①：自分は破壊できる。", "重複"),
    ("このカード名の①②の効果は1ターンに1度しか使用できない。①：自分は特殊召喚できる。", "定義"),
    ("①：自分は特殊召喚できる。破壊でき", "文末が句点"),
    ("①：自分は特殊召喚できる。【サイバース族】", "使わない記号"),
])
def test_format_strong(text, expect):
    res = _kinds(tc._check_format(text), "format", "strong")
    assert any(expect in s["message"] for s in res), [s["message"] for s in res]


def test_format_clean_official_style_has_no_suspect():
    text = ("このカード名の①②の効果はそれぞれ1ターンに1度しか使用できない。\n"
            "①：自分は「青眼の白龍」を特殊召喚できる（同名カードは1枚まで）。\n②：墓地のこのカードを除外して発動できる。")
    assert tc._check_format(text) == []


def test_format_ascii_punct_inside_quotes_is_ignored():
    assert tc._check_format("①：自分は「D.D. Crow(2)」を特殊召喚できる。") == []


def test_format_colon_suggests_fullwidth():
    s = [x for x in tc._check_format("①:自分は特殊召喚できる。") if x["text"] == ":"][0]
    assert s["suggestion"] == "："


# ── 辞書 ──

def test_missing_dict_is_reported_as_unchecked_not_clean(monkeypatch, tmp_path):
    assert tc.load_dict(str(tmp_path / "none.json.gz")) is None
    monkeypatch.setattr(tc, "load_dict", lambda path=None: None)
    res = tc.check_card({"name": "A", "effect_text": "①：自分は特殊召喚できる。"})
    assert res["status"] == "unchecked" and res["reason"] == "dict_missing" and res["suspects"] == []


def test_corrupt_dict_is_unchecked(tmp_path):
    bad = tmp_path / "bad.json.gz"
    bad.write_bytes(b"not gzip")
    assert tc.load_dict(str(bad)) is None


def test_dict_roundtrip_and_deterministic(tmp_path):
    corpus = [{"konami_id": i, "name": n, "texts": [t]} for i, (n, t) in enumerate(CORPUS_TEXTS)]
    data = builder.build_dict(corpus)
    p1, p2 = tmp_path / "a.json.gz", tmp_path / "b.json.gz"
    builder.write_dict(data, str(p1))
    builder.write_dict(data, str(p2))
    assert p1.read_bytes() == p2.read_bytes()  # 同じ内容なら同じバイト列（git差分が出ない）
    loaded = tc.load_dict(str(p1))
    assert "カード" in "".join(loaded.ngrams) and loaded.knows_name(tc.name_key("青眼の白龍"))
    with gzip.open(p1, "rt", encoding="utf-8") as f:
        assert json.load(f)["meta"]["n"] == tc.NGRAM_N


def test_rows_to_corpus_handles_both_ygores_formats():
    rows = [
        {"konami_id": "1", "live_name": "A", "live_eff": "e1", "live_pend": "p1", "mir_name": None, "mir_eff": None, "mir_pend": None},
        {"konami_id": "2", "live_name": None, "live_eff": None, "live_pend": None, "mir_name": "B", "mir_eff": "e2", "mir_pend": None},
    ]
    out = builder.rows_to_corpus(rows)
    assert out[0] == {"konami_id": "1", "name": "A", "texts": ["e1", "p1"]}
    assert out[1] == {"konami_id": "2", "name": "B", "texts": ["e2"]}


# ── 自動承認への配線 ──

def _clean_check(**over):
    r = {"version": 1, "status": "checked", "reason": None, "strong": 0, "weak": 0, "suspects": [], "unverified_fields": []}
    r.update(over)
    return r


def _base_card():
    return {
        "name": "テスト竜", "card_type": "効果モンスター", "level": 4, "atk": 1800, "def": "1000",
        "effect_text": "効果", "pendulum_effect": "", "confidence": "high",
        "extraction_raw": {
            "source": "x_tweet", "recognition_review": {"status": "verified"},
            "release_date_evidence": {"status": "verified"}, "crop_suggestion": None,
        },
    }


def test_evaluate_text_conditions():
    card = _base_card()
    assert aa.evaluate(card, other_names=[], text_check=_clean_check())["eligible"] is True
    assert "text_suspect" in aa.evaluate(card, other_names=[], text_check=_clean_check(strong=1))["reasons"]
    # 弱い疑義は条件に使わない
    assert aa.evaluate(card, other_names=[], text_check=_clean_check(weak=5))["eligible"] is True
    # 未検証は不適格（疑義なし扱いにしない）
    assert "text_unchecked" in aa.evaluate(card, other_names=[], text_check=_clean_check(status="unchecked"))["reasons"]
    assert "text_unchecked" in aa.evaluate(card, other_names=[], text_check=_clean_check(unverified_fields=["effect_text"]))["reasons"]
    assert aa.reason_label("text_suspect") != "text_suspect" and aa.reason_label("text_unchecked") != "text_unchecked"
    assert aa.VERSION >= 2


def test_evaluate_without_dict_is_ineligible(monkeypatch):
    monkeypatch.setattr(tc, "load_dict", lambda path=None: None)
    v = aa.evaluate(_base_card(), other_names=[])
    assert "text_unchecked" in v["reasons"] and v["eligible"] is False


def test_compute_text_check_exception_becomes_unchecked(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(tc, "check_card", boom)
    r = aa.compute_text_check(_base_card(), [])
    assert r["status"] == "unchecked" and r["reason"] == "error"


def test_annotate_row_saves_versioned_text_check(monkeypatch):
    monkeypatch.setattr(aa, "compute_text_check", lambda *a, **k: _clean_check(strong=1, suspects=[{"kind": "format"}]))
    row = _base_card()
    ctx = tc.TextContext()
    row["effect_text"] = "「新しい語」を特殊召喚できる。"
    verdict = aa.annotate_row(row, [], ctx)
    raw = row["extraction_raw"]
    assert raw["text_check"]["version"] == 1 and raw["text_check"]["strong"] == 1
    assert "text_suspect" in verdict["reasons"]
    # 取り込んだ行の「」内語は後続カードの文脈になる
    assert ctx.word_df.get(tc.name_key("新しい語")) == 1


def test_approval_snapshot_records_text_check_counts(monkeypatch):
    monkeypatch.setattr(aa, "compute_text_check", lambda *a, **k: _clean_check(
        strong=2, weak=1, suspects=[{"severity": "strong", "kind": "unknown_ngram"}, {"severity": "strong", "kind": "format"}]))
    snap = aa.build_approval_snapshot(_base_card(), "single", "2026-10-15T00:00:00+00:00")
    assert snap["text_check"] == {"version": 1, "status": "checked", "strong": 2, "weak": 1,
                                  "strong_by_kind": {"unknown_ngram": 1, "format": 1}}


def test_report_counts_flagged_fixed_and_unfixed():
    def card(i, strong, edited, now_strong=0):
        return {"id": i, "name": f"c{i}", "status": "approved", "extraction_raw": {
            "auto_approve_verdict": {"eligible": False, "reasons": ["text_suspect"]},
            "text_check": _clean_check(strong=strong, suspects=[{"severity": "strong", "text": "基"}] if strong else []),
            "approval_snapshot": {"edited_fields": edited,
                                  "text_check": {"strong_by_kind": {"unknown_ngram": now_strong}}},
        }}
    res = report.summarize([card(1, 1, ["effect_text"]), card(2, 1, []), card(3, 0, []), card(4, 1, [], now_strong=1)])
    t = res["text_check"]
    assert t["checked"] == 4 and len(t["flagged"]) == 3
    assert [r["id"] for r in t["flagged_fixed"]] == [1]
    assert [r["id"] for r in t["flagged_unfixed"]] == [2, 4]
    assert [r["id"] for r in t["approval_flagged_unedited"]] == [4]


# ── 管理画面API ──

def test_admin_recheck_endpoint_uses_edited_values(monkeypatch):
    import admin_unreleased as admin_module
    from tests.test_admin_unreleased_release_date_guard import HEADERS, _client, _install_fake

    rows = [{"id": 5, "name": "カードA", "status": "pending", "effect_text": "①：自分は特殊召喚できる。",
             "pendulum_effect": ""}]
    _install_fake(monkeypatch, rows)
    seen = {}

    def fake_compute(card, others, ctx, **kw):
        seen["card"] = card
        return _clean_check()

    monkeypatch.setattr("unreleased_auto_approve.compute_text_check", fake_compute)
    resp = _client().post("/api/admin/unreleased/5/text-check", headers=HEADERS,
                          json={"effect_text": "①：直した文章。"})
    assert resp.status_code == 200 and resp.get_json()["text_check"]["status"] == "checked"
    assert seen["card"]["effect_text"] == "①：直した文章。" and seen["card"]["name"] == "カードA"
    assert rows[0]["effect_text"] == "①：自分は特殊召喚できる。"  # DBは書き換えない
    assert _client().post("/api/admin/unreleased/99/text-check", headers=HEADERS).status_code == 404
    assert admin_module  # import 確認


# ── 実辞書（リポジトリにコミットした data/text_check_dict.json.gz）──

real = pytest.mark.skipif(not os.path.isfile(tc.DICT_PATH), reason="辞書ファイルが無い")


@real
def test_real_dict_loads_and_is_small():
    d = tc.load_dict()
    assert d is not None and len(d.ngrams) > 10000 and len(d.words) > 10000
    assert os.path.getsize(tc.DICT_PATH) < 2 * 1024 * 1024


@real
def test_real_dict_clean_official_text_passes():
    text = ("このカード名の①②の効果はそれぞれ1ターンに1度しか使用できない。\n"
            "①：自分メインフェイズに発動できる。デッキから「青眼の白龍」1体を手札に加える。\n"
            "②：このカードが墓地に存在する場合、自分フィールドのモンスター1体をリリースして発動できる。このカードを特殊召喚する。")
    res = tc.check_card({"name": "テスト", "effect_text": text})
    assert res["status"] == "checked" and res["strong"] == 0


@real
def test_real_dict_flags_boti_misread_as_kiti():
    """承認済みカードで実際に見つかった読み違い（墓地→基地）を候補つきで捕まえる。"""
    text = "①：自分の墓地のモンスター1体を除外して発動できる。自分の基地のモンスター1体を特殊召喚する。"
    res = tc.check_card({"name": "テスト", "effect_text": text})
    strong = [s for s in res["suspects"] if s["severity"] == "strong"]
    assert strong and strong[0]["text"] == "基" and strong[0]["suggestion"] == "墓"


# ── 管理画面の表示（node で DOM スタブに描画して確認）──

def test_admin_js_text_check_panel():
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node が無い")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(["node", os.path.join(root, "tests", "js", "admin_text_check_check.js")],
                            cwd=root, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
