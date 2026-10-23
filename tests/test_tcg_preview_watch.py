"""TCG先行カードの収集ツール（tools/tcg_preview_watch.py）を検証する。

公式データベース・Storage・翻訳APIは偽物に差し替える（実アクセスしない）。
"""

import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tcg_preview_watch as pw  # noqa: E402
from fake_supabase import FakeSB  # noqa: E402


def _w(root, lang, cid, **kw):
    d = root / lang
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{cid}.json").write_text(json.dumps({"id": cid, **kw}, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def mirror(tmp_path):
    """日英が揃うカード数枚（対応表の材料）＋英語だけのカード。"""
    _w(tmp_path, "en", 1, type="monster", name="A", englishAttribute="earth", localizedAttribute="EARTH",
       level=4, atk=1, effectText="x", properties=["Thunder", "Effect"])
    _w(tmp_path, "ja", 1, type="monster", name="あ", englishAttribute="earth", localizedAttribute="地属性",
       level=4, atk=1, effectText="x", properties=["雷族", "効果"])
    _w(tmp_path, "en", 2, type="monster", name="B", englishAttribute="light", localizedAttribute="LIGHT",
       level=7, properties=["Fairy", "Synchro", "Tuner", "Effect"])
    _w(tmp_path, "ja", 2, type="monster", name="い", englishAttribute="light", localizedAttribute="光属性",
       level=7, properties=["天使族", "シンクロ", "チューナー", "効果"])
    _w(tmp_path, "en", 3, type="monster", name="C", englishAttribute="dark", localizedAttribute="DARK",
       level=4, properties=["Spellcaster", "Pendulum", "Tuner", "Effect"])
    _w(tmp_path, "ja", 3, type="monster", name="う", englishAttribute="dark", localizedAttribute="闇属性",
       level=4, properties=["魔法使い族", "ペンデュラム", "チューナー", "効果"])
    _w(tmp_path, "en", 4, type="monster", name="D", englishAttribute="water", localizedAttribute="WATER",
       level=4, properties=["Aqua", "Normal"])
    _w(tmp_path, "ja", 4, type="monster", name="え", englishAttribute="water", localizedAttribute="水属性",
       level=4, properties=["水族", "通常"])
    _w(tmp_path, "en", 5, type="spell", name="E", englishAttribute="spell", localizedAttribute="SPELL",
       englishProperty="quickplay", localizedProperty="Quick-Play", effectText="x")
    _w(tmp_path, "ja", 5, type="spell", name="お", englishAttribute="spell", localizedAttribute="魔法",
       englishProperty="quickplay", localizedProperty="速攻", effectText="x")
    _w(tmp_path, "en", 6, type="trap", name="F", englishAttribute="trap", localizedAttribute="TRAP",
       englishProperty="continuous", localizedProperty="Continuous", effectText="x")
    _w(tmp_path, "ja", 6, type="trap", name="か", englishAttribute="trap", localizedAttribute="罠",
       englishProperty="continuous", localizedProperty="永続", effectText="x")
    _w(tmp_path, "en", 7, type="trap", name="G", englishAttribute="trap", localizedAttribute="TRAP", effectText="x")
    _w(tmp_path, "ja", 7, type="trap", name="き", englishAttribute="trap", localizedAttribute="罠", effectText="x")
    _w(tmp_path, "en", 8, type="spell", name="H", englishAttribute="spell", localizedAttribute="SPELL",
       englishProperty="field", localizedProperty="Field", effectText="x")
    _w(tmp_path, "ja", 8, type="spell", name="く", englishAttribute="spell", localizedAttribute="魔法",
       englishProperty="field", localizedProperty="フィールド", effectText="x")
    # 英語だけ（候補）。1〜8 は日本語にもある（対象外）
    _w(tmp_path, "en", 100, type="monster", name="New Synchro", englishAttribute="light", localizedAttribute="LIGHT",
       level=8, atk=2500, properties=["Fairy", "Synchro", "Tuner", "Effect"], effectText="e100")
    _w(tmp_path, "en", 101, type="spell", name="New Quick", englishAttribute="spell", localizedAttribute="SPELL",
       englishProperty="quickplay", effectText="e101")
    _w(tmp_path, "en", 102, type="trap", name="New Trap", englishAttribute="trap", localizedAttribute="TRAP",
       effectText="e102")
    return tmp_path


@pytest.fixture
def mirror_data(mirror):
    import tcg_translate
    return tcg_translate.load_mirror(str(mirror))


# ── 語彙の対応表 ──

def test_vocab_built_from_paired_cards(mirror_data):
    en, ja = mirror_data
    v = pw.build_vocab(en, ja)
    assert v.props["Thunder"] == "雷族"
    assert v.props["Effect"] == "効果"
    assert v.props["Synchro"] == "シンクロ"
    assert v.props["Tuner"] == "チューナー"
    assert v.attributes == {"earth": "地", "light": "光", "dark": "闇", "water": "水"}
    assert v.spell_trap[("spell", "quickplay")] == "速攻"
    assert v.spell_trap[("trap", "continuous")] == "永続"
    # 日本語側の並び（シンクロ→チューナー→効果、ペンデュラム→チューナー）に揃う
    assert v.order["シンクロ"] < v.order["チューナー"] < v.order["効果"]
    assert v.order["ペンデュラム"] < v.order["チューナー"]


def test_candidates_exclude_cards_in_japanese(mirror_data):
    en, ja = mirror_data
    assert pw.select_candidates(en, ja) == [100, 101, 102]


# ── card_type の組み立て ──

def test_card_type_monsters(mirror_data):
    en, ja = mirror_data
    v = pw.build_vocab(en, ja)
    assert pw.build_card_type(en[100], v) == "シンクロ・チューナー・効果モンスター"
    assert pw.build_card_type(en[1], v) == "効果モンスター"
    assert pw.build_card_type(en[4], v) == "通常モンスター"
    # 英語の並びが日本語と違っても日本語の並びにする
    card = {"id": 9, "type": "monster", "properties": ["Spellcaster", "Tuner", "Pendulum", "Effect"]}
    assert pw.build_card_type(card, v) == "ペンデュラム・チューナー・効果モンスター"
    assert pw.translate_monster_terms(card, v)[1] == "魔法使い族"


def test_card_type_spell_and_trap(mirror_data):
    en, ja = mirror_data
    v = pw.build_vocab(en, ja)
    assert pw.build_card_type(en[101], v) == "速攻魔法"
    assert pw.build_card_type(en[102], v) == "通常罠"
    assert pw.build_card_type(en[8], v) == "フィールド魔法"
    assert pw.build_card_type(en[6], v) == "永続罠"
    assert pw.build_card_type({"id": 9, "type": "spell"}, v) == "通常魔法"


def test_unknown_word_kept_in_english_with_warning(mirror_data, caplog):
    en, ja = mirror_data
    v = pw.build_vocab(en, ja)
    with caplog.at_level(logging.WARNING):
        t = pw.build_card_type({"id": 9, "type": "monster", "properties": ["Dragon", "Gizmo", "Effect"]}, v)
        s = pw.build_card_type({"id": 9, "type": "spell", "englishProperty": "mystery"}, v)
    assert "Dragon" in t and "Gizmo" in t and t.endswith("効果モンスター")
    assert s == "mystery魔法"
    assert "Gizmo" in caplog.text and "mystery" in caplog.text


def test_build_row_fields(mirror_data):
    en, ja = mirror_data
    v = pw.build_vocab(en, ja)
    row = pw.build_row(en[100], v)
    assert row["card_type"] == "シンクロ・チューナー・効果モンスター"
    assert row["attribute"] == "光" and row["race"] == "天使族"
    assert row["level"] == 8 and row["atk"] == 2500 and row["def"] == ""
    spell = pw.build_row(en[101], v)
    assert spell["attribute"] == "" and spell["race"] == ""


# ── 公式ページの解析 ──

PAGE = """
<meta property='og:image' content="https://www.db.yugioh-card.com/yugiohdb/get_image.action?type=1&osplang=1&cid=23513&ciid=1&enc=EXhIAuNBOJuPWlddTs_1Sg&app=tournament&request_locale=en" />
<script>$('#card_image_1').attr('src', '/yugiohdb/get_image.action?type=2&cid=23513&ciid=1&enc=EXhIAuNBOJuPWlddTs_1Sg').show();</script>
<div id="update_list" class="list">
 <div class="t_body">
  <div class="t_row ">
   <div class="inside">
    <div class="time">
        2026-10-09
    </div>
    <div class="flex_1 contents " >
     <div class="card_number">
        BETB-EN081
     </div>
     <div class="pack_name flex_1" >
        BEYOND THE BRAVE
     </div>
    </div>
   </div>
  </div>
  <div class="t_row ">
   <div class="inside">
    <div class="time">
        2026-08-01
    </div>
    <div class="flex_1 contents " >
     <div class="card_number">
        PROMO-EN001
     </div>
     <div class="pack_name flex_1" >
        Promo &amp; Co
     </div>
    </div>
   </div>
  </div>
 </div>
</div>
"""


def test_parse_pack_list_takes_oldest_row():
    assert pw.parse_pack_list(PAGE) == {
        "tcg_release_date": "2026-08-01", "set_code": "PROMO-EN001", "product_name_en": "Promo & Co"}


def test_parse_pack_list_empty_is_none():
    assert pw.parse_pack_list('<div id="update_list" class="list"></div>') is None
    assert pw.parse_pack_list("<html></html>") is None


def test_parse_image_url():
    url = pw.parse_image_url(PAGE, 23513)
    assert url == ("https://www.db.yugioh-card.com/yugiohdb/get_image.action?type=1&osplang=1&cid=23513&ciid=1"
                   "&enc=EXhIAuNBOJuPWlddTs_1Sg&app=tournament&request_locale=en")
    assert pw.parse_image_url(PAGE, 999) is None
    assert pw.parse_image_url(PAGE.replace("&", "&amp;"), 23513) == url  # &amp; を戻す


def test_fetch_official_distinguishes_zero_rows_from_failure(monkeypatch):
    monkeypatch.setattr(pw, "_http_get", lambda url, referer=None: (
        b'<div id="cardname" class="pc cardname"><h1>\n Foo \n</h1></div><div id="update_list"></div>', ""))
    r = pw.fetch_official(1, "Foo")
    assert r["ok"] is True and r["pack"] is None and r["status"] == "empty"

    def boom(url, referer=None):
        raise OSError("network down")
    monkeypatch.setattr(pw, "_http_get", boom)
    r = pw.fetch_official(1, "Foo")
    assert r["ok"] is False and r["status"] == "failed" and "network down" in r["error"]


# ── 訳し直しの判定 ──

def test_needs_translation():
    row = {"effect_text_en": "A", "pendulum_effect_en": ""}
    assert pw.needs_translation(None, row)
    done = {"translation_status": "done", "translated_source_en": "A", "translated_source_pend_en": ""}
    assert not pw.needs_translation(done, row)
    assert pw.needs_translation({**done, "translated_source_en": "old"}, row)
    assert pw.needs_translation({**done, "translation_status": "failed"}, row)
    assert pw.needs_translation({**done, "translation_status": "pending"}, row)
    assert pw.needs_translation({**done, "translated_source_pend_en": "P"}, row)
    # 通常モンスターの skipped は訳さない。基準は effect_text_en ではなく「訳したときの英文」
    assert not pw.needs_translation({"translation_status": "skipped"}, row)
    assert pw.needs_translation({**done, "effect_text_en": "A", "translated_source_en": ""}, row)


# ── process（upsert・翻訳・画像） ──

class _Storage:
    def __init__(self):
        self.uploaded = []

    def from_(self, bucket):
        return self

    def upload(self, path, file, file_options):
        self.uploaded.append((path, file_options))

    def get_public_url(self, path):
        return f"https://x/storage/{path}"


class _SB(FakeSB):
    def __init__(self, tables=None):
        super().__init__(tables, pks={pw.TABLE: "konami_id"})
        self.storage = _Storage()


@pytest.fixture
def env(monkeypatch):
    calls = []

    def fake_translate(client, corpus, card, *, model, effort=None, exclude=(), text_key="effectText"):
        calls.append((card["id"], text_key))
        return {"text": "訳:" + card[text_key], "warnings": [], "examples": [], "usage": {}}

    monkeypatch.setattr(pw.tcg_translate, "translate", fake_translate)
    monkeypatch.setattr(pw.tcg_translate, "Corpus", lambda en, ja: object())
    monkeypatch.setattr(pw, "fetch_official", lambda cid, name: {
        "page_url": f"p{cid}", "ok": True, "status": "ok", "image_url": f"img{cid}", "error": "",
        "pack": {"tcg_release_date": "2026-10-09", "set_code": f"S-{cid}", "product_name_en": "PACK"}})
    monkeypatch.setattr(pw, "download_image", lambda url, page: (b"bytes", "jpg"))
    return calls


def _run(sb, mirror_data, **kw):
    en, ja = mirror_data
    args = dict(sb=sb, client=object(), model="m", effort=None, translate=True, dry_run=False)
    args.update(kw)
    return pw.process(en, ja, [100, 101], **args)


def test_process_inserts_translates_and_stores_image(env, mirror_data):
    sb = _SB()
    stats = _run(sb, mirror_data)
    assert stats["new"] == 2 and stats["translated"] == 2
    rows = {r["konami_id"]: r for r in sb.tables[pw.TABLE]}
    r = rows[100]
    assert r["translation_status"] == "done" and r["effect_text_ja"] == "訳:e100"
    assert r["translation_model"] == "m" and r["translation_warnings"] == []
    assert r["image_storage_path"] == "tcg_preview/100.jpg"
    assert r["image_url"].startswith("https://x/storage/tcg_preview/100.jpg")
    assert r["image_source_url"] == "img100" and r["image_page_url"] == "p100"
    assert r["product_name_en"] == "PACK" and r["set_code"] == "S-100"
    assert "first_seen_at" not in r  # 初回の値は DB の DEFAULT に任せる
    assert sb.storage.uploaded[0][1]["upsert"] == "true"


def test_process_does_not_retranslate_same_text_and_keeps_first_seen(env, mirror_data):
    sb = _SB()
    _run(sb, mirror_data)
    for r in sb.tables[pw.TABLE]:
        r["first_seen_at"] = "2026-01-01T00:00:00+00:00"
    env.clear()
    stats = _run(sb, mirror_data)
    assert env == []  # 翻訳を呼ばない
    assert stats["new"] == 0 and stats["updated"] == 2 and stats["translated"] == 0
    assert all(r["first_seen_at"] == "2026-01-01T00:00:00+00:00" for r in sb.tables[pw.TABLE])
    assert all(r["effect_text_ja"].startswith("訳:") for r in sb.tables[pw.TABLE])


def test_process_retranslates_when_english_changed_and_failed(env, mirror_data):
    sb = _SB()
    _run(sb, mirror_data)
    rows = {r["konami_id"]: r for r in sb.tables[pw.TABLE]}
    rows[100]["translated_source_en"] = "old text"  # 訳したときの英文が今と違う
    rows[101]["translation_status"] = "failed"      # 前回失敗
    env.clear()
    stats = _run(sb, mirror_data)
    assert sorted(c[0] for c in env) == [100, 101]
    assert stats["retranslated"] == 1 and stats["translated"] == 2


def test_process_translation_failure_marks_failed(env, mirror_data, monkeypatch):
    def boom(*a, **k):
        raise ValueError("訳文が空です")
    monkeypatch.setattr(pw.tcg_translate, "translate", boom)
    sb = _SB()
    stats = _run(sb, mirror_data)
    assert stats["translate_failed"] == 2
    r = sb.tables[pw.TABLE][0]
    assert r["translation_status"] == "failed" and "訳文が空です" in r["translation_error"]
    assert r["name_en"]  # 文字情報は保存されている


def test_process_image_failure_keeps_text(env, mirror_data, monkeypatch):
    def boom(url, page):
        raise ValueError("画像として不正")
    monkeypatch.setattr(pw, "download_image", boom)
    sb = _SB()
    stats = _run(sb, mirror_data)
    assert stats["image_failed"] == 2
    r = sb.tables[pw.TABLE][0]
    assert "image_url" not in r and r["card_type"]


def test_process_page_failure_does_not_clobber_existing_product(env, mirror_data, monkeypatch):
    sb = _SB()
    _run(sb, mirror_data)
    for r in sb.tables[pw.TABLE]:
        r["image_url"] = None  # 画像が無いので公式ページを取り直す
    monkeypatch.setattr(pw, "fetch_official", lambda cid, name: {
        "page_url": "", "ok": False, "status": "failed", "image_url": None, "pack": None, "error": "timeout"})
    stats = _run(sb, mirror_data)
    assert stats["page_failed"] == 2 and stats["image_failed"] == 0  # 二重に数えない
    assert all(r["product_name_en"] == "PACK" for r in sb.tables[pw.TABLE])
    assert all(r["official_fetch_status"] == "failed" for r in sb.tables[pw.TABLE])


def test_process_dry_run_touches_nothing(env, mirror_data, capsys):
    stats = _run(None, mirror_data, dry_run=True, client=None, translate=False)
    assert stats["seen"] == 2
    assert env == []
    assert "シンクロ・チューナー・効果モンスター" in capsys.readouterr().out


# ── レビュー指摘の対応 ──

def test_no_translate_then_english_changed_is_retranslated(env, mirror_data):
    """OLD で訳す → 英文が NEW に → --no-translate の実行 → 通常実行で翻訳が呼ばれる。"""
    import copy
    en, ja = mirror_data
    en_old = copy.deepcopy(en)
    en_old[100]["effectText"] = "OLD"
    sb = _SB()
    pw.process(en_old, ja, [100], sb=sb, client=object(), model="m", effort=None, translate=True, dry_run=False)
    assert sb.tables[pw.TABLE][0]["effect_text_ja"] == "訳:OLD"
    env.clear()
    en_new = copy.deepcopy(en)
    en_new[100]["effectText"] = "NEW"
    pw.process(en_new, ja, [100], sb=sb, client=None, model="m", effort=None, translate=False, dry_run=False)
    r = sb.tables[pw.TABLE][0]
    assert r["effect_text_en"] == "NEW" and r["translated_source_en"] == "OLD" and r["translation_status"] == "done"
    assert env == []
    pw.process(en_new, ja, [100], sb=sb, client=object(), model="m", effort=None, translate=True, dry_run=False)
    assert env == [(100, "effectText")]
    r = sb.tables[pw.TABLE][0]
    assert r["effect_text_ja"] == "訳:NEW" and r["translated_source_en"] == "NEW"


def test_candidate_cap_stops_without_translating_or_fetching(mirror, monkeypatch):
    sent = []
    monkeypatch.setattr(pw, "MAX_CANDIDATES", 2)
    monkeypatch.setattr(pw, "_notify", sent.append)
    monkeypatch.setattr(pw, "fetch_official", lambda *a: pytest.fail("公式DBにアクセスしてはいけない"))
    monkeypatch.setattr(pw.tcg_translate, "translate", lambda *a, **k: pytest.fail("翻訳してはいけない"))
    assert pw.main(["--mirror", str(mirror)]) == 1
    assert len(sent) == 1 and "上限" in sent[0]


def test_load_mirror_logs_skipped_files(tmp_path, caplog):
    import tcg_translate
    _w(tmp_path, "en", 1, type="spell", name="A")
    (tmp_path / "en" / "bad.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "ja").mkdir()
    with caplog.at_level(logging.WARNING):
        en, ja = tcg_translate.load_mirror(str(tmp_path))
    assert set(en) == {1} and ja == {}
    assert "読み飛ばし" in caplog.text and "1件" in caplog.text


def test_max_translate_limits_api_calls(env, mirror_data):
    sb = _SB()
    stats = _run(sb, mirror_data, max_translate=1)
    assert len(env) == 1 and stats["translated"] == 1 and stats["translate_deferred"] == 1
    # 見送った分は次回に回る（pending のまま）
    assert sorted(r.get("translation_status", "pending") for r in sb.tables[pw.TABLE]) == ["done", "pending"]


def test_minus_one_means_unknown(mirror_data):
    en, ja = mirror_data
    v = pw.build_vocab(en, ja)
    base = {"id": 9, "type": "monster", "name": "Q", "englishAttribute": "dark",
            "properties": ["Fiend", "Effect"], "level": 4}
    row = pw.build_row({**base, "atk": -1, "def": -1}, v)
    assert row["atk"] is None and row["def"] == "?" and row["level"] == 4
    row = pw.build_row({**base, "atk": 0, "def": 0}, v)
    assert row["atk"] == 0 and row["def"] == "0"


def test_page_is_card_marker():
    ok = '<div id="cardname" class="pc cardname">\n<h1>\n Audhumla, Progenitor of the Frozen Expanse\n</h1></div>'
    assert pw.page_is_card(ok, "Audhumla, Progenitor of the Frozen Expanse")
    assert not pw.page_is_card(ok, "Another Card")
    assert not pw.page_is_card("<html>Under maintenance</html>", "Audhumla")


def test_fetch_official_maintenance_page_is_failed(monkeypatch):
    monkeypatch.setattr(pw, "_http_get", lambda url, referer=None: (b"<html>Under maintenance</html>", ""))
    r = pw.fetch_official(1, "Foo")
    assert r["ok"] is False and r["status"] == "failed" and "目印" in r["error"]


def test_official_status_recorded_and_failure_threshold(env, mirror_data):
    sb = _SB()
    _run(sb, mirror_data)
    assert all(r["official_fetch_status"] == "ok" and r["official_fetched_at"] for r in sb.tables[pw.TABLE])
    assert pw.official_failure_message({"fetched": 4, "page_failed": 1}) is None
    assert pw.official_failure_message({"fetched": 4, "page_failed": 2})
    assert pw.official_failure_message({"fetched": 0, "page_failed": 0}) is None


def test_main_exit_code_when_official_fails(mirror, monkeypatch):
    monkeypatch.setattr(pw, "fetch_official", lambda cid, name: {
        "page_url": "", "ok": False, "status": "failed", "image_url": None, "pack": None, "error": "x"})
    assert pw.main(["--mirror", str(mirror), "--dry-run"]) == 1


def test_complete_cards_are_not_refetched(env, mirror_data, monkeypatch):
    sb = _SB()
    _run(sb, mirror_data)
    monkeypatch.setattr(pw, "fetch_official", lambda *a: pytest.fail("取り直してはいけない"))
    stats = _run(sb, mirror_data)
    assert stats["fetched"] == 0 and stats["updated"] == 2


def test_normal_monster_is_skipped_not_translated(env, mirror_data):
    en, ja = mirror_data
    en = dict(en)
    en[103] = {"id": 103, "type": "monster", "name": "Flavor", "englishAttribute": "dark",
               "properties": ["Fiend", "Normal"], "effectText": "A tale.", "level": 8, "atk": 2500, "def": 2000}
    sb = _SB()
    stats = pw.process(en, ja, [103], sb=sb, client=object(), model="m", effort=None, translate=True, dry_run=False)
    r = sb.tables[pw.TABLE][0]
    assert env == [] and stats["skipped"] == 1
    assert r["translation_status"] == "skipped" and r.get("effect_text_ja", "") == ""
    # 2回目も訳さない
    pw.process(en, ja, [103], sb=sb, client=object(), model="m", effort=None, translate=True, dry_run=False)
    assert env == []


def test_empty_english_text_gets_done_record(env, mirror_data):
    en, ja = mirror_data
    en = dict(en)
    en[102] = {**en[102], "effectText": ""}
    sb = _SB()
    pw.process(en, ja, [102], sb=sb, client=object(), model="mm", effort=None, translate=True, dry_run=False)
    r = sb.tables[pw.TABLE][0]
    assert env == [] and r["translation_status"] == "done"
    assert r["translation_model"] == "mm" and r["translated_at"]


def test_cost_warns_for_unknown_model(caplog):
    pw._warned_models.clear()
    with caplog.at_level(logging.WARNING):
        assert pw._cost({}, "claude-unknown-9") == 0.0
    assert "単価" in caplog.text
    assert pw._cost({"input_tokens": 1000000}, "claude-opus-5-5") > 0


def test_limit_zero_means_no_cards(mirror, monkeypatch):
    monkeypatch.setattr(pw, "fetch_official", lambda *a: pytest.fail("0件なのでアクセスしない"))
    assert pw.main(["--mirror", str(mirror), "--dry-run", "--limit", "0"]) == 0


def test_default_model_is_opus():
    assert pw.DEFAULT_MODEL == "claude-opus-5-5"
    assert pw.DEFAULT_MODEL in pw.tcg_translate.PRICES


# ── 日本に来たカードの切り替え（status: active → ocg_linked） ──

def _link_sb(*rows):
    return _SB({pw.TABLE: [{"konami_id": cid, "name_en": en, "status": st, **extra}
                           for cid, en, st, extra in rows]})


def _status(sb):
    return {r["konami_id"]: r for r in sb.tables[pw.TABLE]}


def test_link_switches_when_in_ja_and_in_cardnames():
    sb = _link_sb((1, "Alpha", "active", {}))
    out = pw.link_ocg_cards({1: {"name": "アルファ"}}, ["アルファ", "他"], sb=sb, dry_run=False)
    r = _status(sb)[1]
    assert r["status"] == "ocg_linked" and r["ocg_name"] == "アルファ" and r["ocg_linked_at"] and r["updated_at"]
    assert out == {"linked": 1, "pairs": [("Alpha", "アルファ")]}


def test_link_waits_while_name_is_not_in_cardnames():
    # 日本語版は出ているが、価格検索の一覧に未掲載の間は切り替えない（表示できなくなるため）
    sb = _link_sb((1, "Alpha", "active", {}))
    out = pw.link_ocg_cards({1: {"name": "アルファ"}}, ["他のカード"], sb=sb, dry_run=False)
    assert _status(sb)[1]["status"] == "active" and "ocg_name" not in _status(sb)[1]
    assert out["linked"] == 0


def test_link_skips_cards_not_in_ja():
    sb = _link_sb((1, "Alpha", "active", {}))
    assert pw.link_ocg_cards({}, ["アルファ"], sb=sb, dry_run=False)["linked"] == 0
    assert _status(sb)[1]["status"] == "active"


def test_link_matches_with_fuzzy_key_and_uses_cardnames_spelling():
    # ミラーは半角ハイフン、一覧は全角ハイフン（NFKC で同じキーになる）。ocg_name には一覧側の表記を入れる
    sb = _link_sb((1, "Alpha", "active", {}))
    pw.link_ocg_cards({1: {"name": "ＥＭ-アルファ"}}, ["ＥＭ－アルファ"], sb=sb, dry_run=False)
    r = _status(sb)[1]
    assert r["status"] == "ocg_linked" and r["ocg_name"] == "ＥＭ－アルファ"


def test_link_skips_when_fuzzy_key_matches_two_cards():
    # 記号だけが違う別のカードが一覧に2つあるときは、どちらか分からないので切り替えない（先勝ちで別のカード名を入れない）
    sb = _link_sb((1, "Alpha", "active", {}))
    out = pw.link_ocg_cards({1: {"name": "ＥＭ-アルファ"}}, ["ＥＭ－アルファ", "ＥＭ―アルファ"], sb=sb, dry_run=False)
    assert out["linked"] == 0
    assert _status(sb)[1]["status"] == "active"


def test_link_does_not_touch_already_linked_rows():
    sb = _link_sb((1, "Alpha", "ocg_linked", {"ocg_name": "古い名前", "ocg_linked_at": "2026-01-01"}))
    out = pw.link_ocg_cards({1: {"name": "アルファ"}}, ["アルファ"], sb=sb, dry_run=False)
    r = _status(sb)[1]
    assert out["linked"] == 0
    assert r["ocg_name"] == "古い名前" and r["ocg_linked_at"] == "2026-01-01"


def test_link_dry_run_does_not_write(capsys):
    sb = _link_sb((1, "Alpha", "active", {}))
    out = pw.link_ocg_cards({1: {"name": "アルファ"}}, ["アルファ"], sb=sb, dry_run=True)
    assert out["linked"] == 1
    assert _status(sb)[1]["status"] == "active" and "ocg_name" not in _status(sb)[1]
    assert "Alpha → アルファ" in capsys.readouterr().out


def test_main_notifies_linked_cards_and_counts_in_summary(mirror, monkeypatch, caplog):
    # ミラーの日英が揃うカード「A→あ」が表で active のまま残っている場合
    sb = _link_sb((1, "A", "active", {}))
    sent = []
    monkeypatch.setattr(pw, "_notify", sent.append)
    monkeypatch.setattr(pw, "load_cardnames", lambda: ["あ"])
    monkeypatch.setattr(pw, "process", lambda *a, **k: pw.Counter())
    monkeypatch.setenv("SUPABASE_URL", "u")
    monkeypatch.setenv("SUPABASE_KEY", "k")
    monkeypatch.setattr("supabase.create_client", lambda url, key: sb)
    with caplog.at_level(logging.INFO):
        code = pw.main(["--mirror", str(mirror), "--no-translate"])
    assert code == 0
    assert _status(sb)[1]["status"] == "ocg_linked"
    assert sent == ["日本に来た1枚（A→あ）"]
    assert "日本に来た1" in caplog.text


def test_main_dry_run_does_not_touch_db_for_linking(mirror, monkeypatch):
    monkeypatch.setattr(pw, "fetch_official", lambda cid, name: {
        "page_url": "", "ok": True, "status": "empty", "image_url": None, "pack": None, "error": ""})
    monkeypatch.setattr(pw, "load_cardnames", lambda: pytest.fail("dry-run は一覧も DB も使わない"))
    assert pw.main(["--mirror", str(mirror), "--dry-run"]) == 0
