"""言い回しの照合（unreleased_text_check の ④ 語の組み合わせ・kind="unknown_phrase"）のテスト。"""
import gzip
import json
import os

import pytest

import unreleased_auto_approve as aa
import unreleased_text_check as tc
from tools import auto_approve_report as report
from tools import build_text_check_dict as builder

# id 529「ケペルの金字塔」の現行テキスト（人が確認済み）。「墓地へ送って発動」が正しい
TEXT_529 = ("このカード名の①②の効果はそれぞれ1ターンに1度しか使用できない。"
            "①：デッキからアンデット族の儀式モンスター1体を墓地へ送って発動できる。"
            "自分のデッキ・墓地からフィールド魔法カード1枚を自分フィールドに表側表示で置く。"
            "このカードの発動後、ターン終了時まで自分はアンデット族以外のモンスターの効果を発動できない。"
            "②：相手フィールドに効果モンスターが存在する場合、墓地のこのカードを除外し、"
            "自分の墓地の儀式モンスター1体を対象として発動できる。そのモンスターを手札に加える。")
TEXT_529_BAD = TEXT_529.replace("墓地へ送って発動", "墓地へ返って発動")


def _triples(text, name=""):
    return sorted(tc.phrase_triples(text, name))


# ── 抽出（正規表現による名詞＋助詞＋動詞の切り出し）──

def test_phrase_extraction_basic():
    assert _triples("モンスター1体を墓地へ送って発動できる。") == [("墓地", "へ", "送")]
    # 助詞の後が名詞（ひらがなが助詞）のものは動詞として数えない（体を墓地…は対象外）
    assert ("体", "を", "墓地") not in _triples("モンスター1体を墓地へ送る。")


def test_phrase_extraction_sahen_and_particles():
    t = tc.phrase_triples("自分の手札から魔法カードを特殊召喚する。デッキに加える。効果で破壊できる。")
    assert ("手札", "から", "魔法") not in t   # 魔法カード（カタカナが続く）は動詞語幹の形ではない
    assert ("法カード", "を", "特殊召喚") in t  # 名詞は末尾4字で切る
    assert ("デッキ", "に", "加") in t
    assert ("効果", "で", "破壊") in t


def test_phrase_extraction_dekiru_is_verb_but_plain_de_is_particle():
    # 「使用できる」の「で」は活用（でき）なので動詞。「効果で」の「で」は助詞
    assert ("効果", "で", "使用") in tc.phrase_triples("効果で使用できる。")
    assert ("カード", "を", "使用") in tc.phrase_triples("このカードを使用できる。")
    # 助詞「で」が続くだけ（できる、ではない）なら名詞扱い。「破壊で」は動詞の語幹にならない
    assert ("カード", "を", "破壊") not in tc.phrase_triples("カードを破壊で十分。")


def test_phrase_extraction_kara_is_particle():
    # 「デッキから選び」: から の後の「選」は動詞。「手札から」自体は名詞＋助詞
    assert ("デッキ", "から", "選") in tc.phrase_triples("デッキから選び、")


def test_phrase_extraction_uses_quote_mask_and_position():
    s = tc.token_string(tc.tokenize("「青眼の白龍」を特殊召喚する。"))
    occ = tc.phrase_occurrences(s)
    assert [(n, p, v) for n, p, v, _, _ in occ] == [("」", "を", "特殊召喚")]
    assert s[occ[0][3]:occ[0][4]] == "特殊召喚"


# ── 表（PhraseTable）──

def _table():
    sets = [{("墓地", "へ", "送"), ("デッキ", "から", "選")}] * 30 + [{("手札", "に", "加")}] * 60 + [
        {("墓地", "を", "除外"), ("墓地", "へ", "送")}] * 25
    return tc.PhraseTable.from_triple_sets(sets)


def test_phrase_table_counts_per_card_and_roundtrip():
    t = _table()
    assert t.verb_count("送") == 55 and t.pair_count("へ", "送") == 55 and t.np_count("墓地", "へ") == 55
    assert t.triple_count("墓地", "へ", "送") == 55 and t.verb_count("返") == 0
    t2 = tc.PhraseTable.from_json(json.loads(json.dumps(t.to_json())))
    assert t2.verb_count("送") == 55 and t2.triple_count("手札", "に", "加") == 60
    assert t2.top_verbs("墓地", "へ") == ["送"]


def _dict_with(table):
    return tc.TextCheckDict([], [], [], {}, table)


# ── 判定（強・弱・なし）──

def test_unknown_verb_is_strong_with_suggestion():
    res = tc._check_phrases("①：モンスター1体を墓地へ返って発動できる。", _dict_with(_table()), "")
    assert len(res) == 1
    s = res[0]
    assert s["kind"] == "unknown_phrase" and s["severity"] == "strong"
    assert s["text"] == "返" and s["suggestion"] == "送" and "「送」" in s["message"]


def test_frequent_verb_with_unseen_particle_is_strong():
    # 除外は「を」とは出るが「へ」との組が0。動詞自体は25回以上出ている
    res = tc._check_phrases("モンスター1体を墓地へ除外して発動できる。", _dict_with(_table()), "")
    assert [(r["severity"], r["text"]) for r in res] == [("strong", "除外")]
    assert "へ除外" in res[0]["message"]


def test_rare_verb_with_unseen_particle_is_not_strong():
    t = tc.PhraseTable.from_triple_sets([{("墓地", "を", "除外")}] * 5 + [{("墓地", "へ", "送")}] * 5)
    # 除外は5回しか出ていない（下限20未満）。「組が0」だけでは強にしない
    assert tc._check_phrases("モンスター1体を墓地へ除外して発動できる。", _dict_with(t), "") == []


def test_known_phrase_is_clean():
    assert tc._check_phrases("モンスター1体を墓地へ送って発動できる。", _dict_with(_table()), "") == []


def test_unseen_triple_with_common_parts_is_weak_only():
    base = ([{("デッキ", "に", "加")}] * 60 + [{("墓地", "を", "除外")}] * 60 + [{("墓地", "へ", "送")}] * 60)
    # 墓地に（np=0）では弱の条件（np>=50）を満たさない → 疑義なし
    t = tc.PhraseTable.from_triple_sets(base)
    assert tc._check_phrases("モンスターを墓地に加えて発動できる。", _dict_with(t), "") == []
    # 墓地に が十分頻出（np>=50）で、加は頻出、に+加も頻出なのに、墓地に加 だけが無い → 弱
    t2 = tc.PhraseTable.from_triple_sets(base + [{("墓地", "に", "戻")}] * 60)
    res = tc._check_phrases("モンスターを墓地に加えて発動できる。", _dict_with(t2), "")
    assert [(r["severity"], r["kind"]) for r in res] == [("weak", "unknown_phrase")]
    # 弱は提案（suggestion）を出さない
    assert res[0]["suggestion"] is None


def test_no_phrase_table_skips_check():
    d = tc.TextCheckDict([], [], [], {}, None)
    assert tc._check_phrases("墓地へ返って発動できる。", d, "") == []


def test_positions_and_context_are_consistent():
    text = "①：モンスター1体を墓地へ返って発動できる。"
    res = tc._check_phrases(text, _dict_with(_table()), "")
    s = res[0]
    assert text[s["start"]:s["end"]] == "返"
    assert s["context"] == text[s["context_start"]:s["context_start"] + len(s["context"])]
    assert "返" in s["context"]


# ── check_text への統合（文字3連鎖の強との重なり除去）──

def test_check_text_drops_phrase_strong_overlapping_ngram_strong():
    # 文字3連鎖の辞書が空に近いと全位置が未知＝強になる。同じ位置の言い回しの強は二重計上しない
    d = tc.TextCheckDict(["①：モ"], [], [], {}, _table())
    text = "①：モンスター1体を墓地へ返って発動できる。"
    res = tc.check_text(text, dictionary=d)
    assert [x for x in res if x["kind"] == "unknown_ngram" and x["severity"] == "strong"]
    assert not [x for x in res if x["kind"] == "unknown_phrase" and x["severity"] == "strong"]


def test_check_text_keeps_phrase_strong_when_ngram_is_clean():
    # 3連鎖が全て既知（辞書に入れてある）の文章でも、言い回しの強は出る
    text = "①：モンスター1体を墓地へ返って発動できる。"
    s = tc.token_string(tc.tokenize(text))
    d = tc.TextCheckDict(sorted(tc.ngram_set(s)), [], [], {}, _table())
    res = tc.check_text(text, dictionary=d)
    assert [x["severity"] for x in res if x["kind"] == "unknown_phrase"] == ["strong"]


# ── 辞書づくり・読み込み ──

def test_build_dict_contains_phrase_table_and_loader_reads_it(tmp_path):
    corpus = [{"konami_id": 1, "name": "A", "texts": ["①：モンスター1体を墓地へ送って発動できる。"]},
              {"konami_id": 2, "name": "B", "texts": ["①：カード1枚を手札に加えて発動できる。"]}]
    data = builder.build_dict(corpus)
    assert data["phrase"]["verbs"] == {"加": 1, "送": 1} and data["meta"]["phrase_verbs"] == 2
    path = tmp_path / "d.json.gz"
    builder.write_dict(data, str(path))
    d = tc.load_dict(str(path))
    assert d.phrase is not None and d.phrase.verb_count("送") == 1


def test_old_dict_without_phrase_still_loads(tmp_path):
    path = tmp_path / "old.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump({"ngrams": "abc", "card_names": [], "quote_words": [], "meta": {"n": 3}}, f)
    d = tc.load_dict(str(path))
    assert d is not None and d.phrase is None


# ── 実辞書 ──

real = pytest.mark.skipif(not os.path.isfile(tc.DICT_PATH), reason="辞書ファイルが無い")


@real
def test_real_dict_has_phrase_table():
    d = tc.load_dict()
    assert d.phrase is not None and d.phrase.verb_count("送") > 1000 and len(d.phrase.verbs) > 100
    assert os.path.getsize(tc.DICT_PATH) < 2 * 1024 * 1024


@real
def test_real_dict_case_529_phrase_check_catches_misreading():
    """id 529 の現行テキストは疑義なし。「墓地へ返って発動」に戻した版は言い回しの照合が（単体で）強で捉え、「送」を候補に出す。"""
    d = tc.load_dict()
    assert tc._check_phrases(TEXT_529, d, "ケペルの金字塔") == []
    only = tc._check_phrases(TEXT_529_BAD, d, "ケペルの金字塔")
    assert [(s["severity"], s["text"], s["suggestion"]) for s in only] == [("strong", "返", "送")]
    # 統合した check_card でも強が付く（自動承認不可）。文字3連鎖の強と重なる場合は二重計上しない
    res = tc.check_card({"name": "ケペルの金字塔", "effect_text": TEXT_529_BAD}, dictionary=d)
    assert res["status"] == "checked" and res["strong"] >= 1
    assert tc.check_card({"name": "ケペルの金字塔", "effect_text": TEXT_529}, dictionary=d)["strong"] == 0


@real
def test_real_dict_phrase_only_error_is_caught_when_ngram_is_clean():
    """文字3連鎖では見つからず、言い回しの照合だけが見つける例（公式に実在する動詞への差し替え）。"""
    d = tc.load_dict()
    text = "①：自分フィールドのモンスター1体を墓地へ除外して発動できる。"
    assert [s for s in tc.check_text(text, dictionary=d) if s["kind"] == "unknown_ngram" and s["severity"] == "strong"] == []
    res = [s for s in tc.check_text(text, dictionary=d) if s["kind"] == "unknown_phrase"]
    assert res and res[0]["severity"] == "strong" and res[0]["text"] == "除外"


# ── 自動承認・集計への乗り方 ──

def test_auto_approve_version_bumped_and_phrase_blocks_approval():
    assert aa.VERSION >= 3
    chk = {"version": tc.VERSION, "status": "checked", "reason": None, "strong": 1, "weak": 0,
           "suspects": [{"kind": "unknown_phrase", "severity": "strong"}], "unverified_fields": []}
    assert aa._text_reason(chk) == "text_suspect"
    assert tc.VERSION >= 2


def test_approval_snapshot_counts_phrase_strong_by_kind(monkeypatch):
    chk = {"version": tc.VERSION, "status": "checked", "reason": None, "strong": 1, "weak": 0,
           "suspects": [{"kind": "unknown_phrase", "severity": "strong"}], "unverified_fields": []}
    monkeypatch.setattr(aa, "compute_text_check", lambda *a, **k: chk)
    snap = aa.build_approval_snapshot({"id": 1, "name": "A", "extraction_raw": {}}, "single")
    assert snap["text_check"]["strong_by_kind"] == {"unknown_phrase": 1}


def test_report_counts_phrase_as_reliable_strong():
    text = {"checked": 0, "flagged": [], "flagged_fixed": [], "flagged_unfixed": [], "weak_only": 0,
            "approval_flagged_unedited": []}
    raw = {"text_check": {"status": "checked", "strong": 1, "weak": 0, "suspects": [
        {"severity": "strong", "text": "返", "kind": "unknown_phrase"}]}}
    snap = {"text_check": {"strong_by_kind": {"unknown_phrase": 1}}}
    report._tally_text_check(text, {"id": 1, "name": "A"}, raw, snap, [])
    assert len(text["approval_flagged_unedited"]) == 1
