"""食い違っても読み直し側を信じる設定（reread_trust）・緩める条件・自動復帰・集計のテスト。API・DB はモック。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import admin_unreleased as admin_module
import effect_text_reread as rr
import reread_trust
import unreleased_auto_approve as aa
import unreleased_text_report as tr
from tests.fake_supabase import FakeSB
from tests.test_effect_text_reread import _fake_read, _images, _row

# 本番の食い違いと同じ型（1〜2文字の置換）: され→し
FIRST = "①：このカードが墓地へ送られた場合に発動できる。自分フィールドのモンスター1体を選んで破壊され、デッキから1枚ドローする。"
REREAD = "①：このカードが墓地へ送られた場合に発動できる。自分フィールドのモンスター1体を選んで破壊し、デッキから1枚ドローする。"


@pytest.fixture
def notices(monkeypatch):
    sent = []
    monkeypatch.setattr(reread_trust, "_notify_async", sent.append)
    return sent


@pytest.fixture
def clean_check(monkeypatch):
    """誤字検査は強い疑いなし（辞書の有無に左右されない）。"""
    monkeypatch.setattr(aa, "compute_text_check", lambda card, *a, **k: {"status": "checked", "strong": 0, "weak": 0})


def _disagree_row(model="claude-opus-5-5", unc=()):
    row = _row(effect=FIRST)
    row["extraction_raw"]["model"] = model
    row["extraction_raw"]["uncertain_fields"] = list(unc)
    return row


def _run(monkeypatch, row, text=REREAD, unreadable=False, trust=True):
    _fake_read(monkeypatch, text=text, unreadable=unreadable)
    rr.reread_effect_texts([row], client=object(), encoded_images=_images(), trust_on_disagree=trust)
    return row["extraction_raw"]


# ---- 緩める条件 ----

def test_trusted_when_all_conditions_met(monkeypatch, clean_check):
    raw = _run(monkeypatch, _disagree_row())
    assert "effect_text" not in raw["uncertain_fields"]
    assert raw["reread_trusted_on_disagree"] is True
    assert raw["reread_trust_diff_summary"] and raw["reread_trust_diff_summary"] == raw["effect_text_reread"]["diff_summary"]
    assert raw["effect_text_reread"]["agreed"] is False   # 一致扱いにはしない


def test_trusted_clears_self_reported_uncertain(monkeypatch, clean_check):
    import unreleased_auto_approve as aa2
    raw = _run(monkeypatch, _disagree_row(unc=("effect_text", "atk")))
    assert raw["uncertain_fields"] == ["atk"]
    assert raw["reread_trusted_on_disagree"] is True and raw["uncertain_cleared_by_reread"] is True
    # effect_text だけだった場合、fields が空でも medium_uncertain_unknown にならない
    raw2 = _run(monkeypatch, _disagree_row(unc=("effect_text",)))
    assert raw2["uncertain_fields"] == []
    reasons, _ = aa2._confidence_reasons({"confidence": "medium"}, raw2)
    assert reasons == []


def test_not_trusted_keeps_self_reported_uncertain(monkeypatch, clean_check):
    raw = _run(monkeypatch, _disagree_row(unc=("effect_text",)), trust=False)
    assert raw["uncertain_fields"] == ["effect_text"] and "uncertain_cleared_by_reread" not in raw
    raw = _run(monkeypatch, _disagree_row(unc=("effect_text", "name")))
    assert "effect_text" in raw["uncertain_fields"] and "uncertain_cleared_by_reread" not in raw


def test_not_trusted_when_setting_off(monkeypatch, clean_check):
    raw = _run(monkeypatch, _disagree_row(), trust=False)
    assert "effect_text" in raw["uncertain_fields"] and "reread_trusted_on_disagree" not in raw


def test_not_trusted_when_unreadable(monkeypatch, clean_check):
    raw = _run(monkeypatch, _disagree_row(), text="1ターンに1度、〓〓召喚できる。", unreadable=True)
    assert "effect_text" in raw["uncertain_fields"] and "reread_trusted_on_disagree" not in raw


def test_not_trusted_when_unreadable_flag_without_mark(monkeypatch, clean_check):
    raw = _run(monkeypatch, _disagree_row(), unreadable=True)
    assert "effect_text" in raw["uncertain_fields"]


def test_not_trusted_when_text_check_strong(monkeypatch):
    monkeypatch.setattr(aa, "compute_text_check", lambda card, *a, **k: {"status": "checked", "strong": 1})
    raw = _run(monkeypatch, _disagree_row())
    assert "effect_text" in raw["uncertain_fields"] and "reread_trusted_on_disagree" not in raw


def test_not_trusted_when_text_check_unchecked(monkeypatch):
    monkeypatch.setattr(aa, "compute_text_check", lambda card, *a, **k: {"status": "unchecked", "strong": 0})
    raw = _run(monkeypatch, _disagree_row())
    assert "effect_text" in raw["uncertain_fields"]


def test_text_check_runs_on_reread_text(monkeypatch):
    seen = []

    def fake(card, *a, **k):
        seen.append(card["effect_text"])
        return {"status": "checked", "strong": 0}
    monkeypatch.setattr(aa, "compute_text_check", fake)
    _run(monkeypatch, _disagree_row())
    assert seen == [REREAD]


def test_not_trusted_when_models_differ(monkeypatch, clean_check):
    raw = _run(monkeypatch, _disagree_row(model="claude-sonnet-5-5"))
    assert "effect_text" in raw["uncertain_fields"]


def test_not_trusted_when_name_uncertain(monkeypatch, clean_check):
    raw = _run(monkeypatch, _disagree_row(unc=("name",)))
    assert "effect_text" in raw["uncertain_fields"] and "name" in raw["uncertain_fields"]


def test_not_trusted_for_pendulum(monkeypatch, clean_check):
    row = _disagree_row()
    row["pendulum_scale"] = 5
    raw = _run(monkeypatch, row)
    assert "effect_text" in raw["uncertain_fields"] and "reread_trusted_on_disagree" not in raw


def test_not_trusted_when_first_pass_empty(monkeypatch, clean_check):
    row = _disagree_row()
    row["effect_text"] = ""
    raw = _run(monkeypatch, row)
    assert "effect_text" in raw["uncertain_fields"]


def test_agreement_path_unchanged(monkeypatch, clean_check):
    row = _row(effect=REREAD)
    row["extraction_raw"]["model"] = "claude-opus-5-5"
    raw = _run(monkeypatch, row)
    assert raw["effect_text_reread"]["agreed"] is True and "reread_trusted_on_disagree" not in raw


def test_setting_consulted_by_default(monkeypatch, clean_check):
    monkeypatch.setattr(reread_trust, "is_enabled", lambda sb=None: True)
    _fake_read(monkeypatch, text=REREAD)
    row = _disagree_row()
    rr.reread_effect_texts([row], client=object(), encoded_images=_images())
    assert row["extraction_raw"]["reread_trusted_on_disagree"] is True


def test_default_without_db_is_safe_side(monkeypatch, clean_check):
    # conftest が DB を読めない状態にしている＝設定が読めない → 従来どおり不確か
    _fake_read(monkeypatch, text=REREAD)
    row = _disagree_row()
    rr.reread_effect_texts([row], client=object(), encoded_images=_images())
    assert "effect_text" in row["extraction_raw"]["uncertain_fields"]


# ---- 差分の大きさの上限 ----

BASE = "①：このカードが墓地へ送られた場合に発動できる。自分フィールドのモンスター1体を選んで破壊し、デッキから1枚ドローする。②：このカードが除外された場合、自分は500LP回復する。"


@pytest.mark.parametrize("first,reread", [
    ("相手は自分のカードを1枚選び、それを破壊されて、その後デッキから1枚引くことができる。", "相手は自分のカードを1枚選び、それを破壊して、その後デッキから1枚引くことができる。"),   # され→し
    ("「サンプル」・モンスター1体を選んで特殊召喚する。この効果は1ターンに1度しか使用できない。", "「サンプル」か モンスター1体を選んで特殊召喚する。この効果は1ターンに1度しか使用できない。"),   # ・→か
    ("このカードは召喚したは、自分フィールドにモンスターが存在しない場合に特殊召喚できる。", "このカードは召喚した後、自分フィールドにモンスターが存在しない場合に特殊召喚できる。"),   # は→後
])
def test_production_style_disagreements_are_within_limits(first, reread):
    assert rr.diff_within_trust_limits(rr.norm(first), rr.norm(reread)) is True


def test_limits_reject_missing_line():
    # 行（②の効果）がまるごと欠けた読み取り
    first = BASE
    reread = BASE.split("②")[0]
    assert rr.diff_within_trust_limits(rr.norm(first), rr.norm(reread)) is False


def test_limits_reject_other_card_text():
    other = "①：自分メインフェイズに発動できる。手札からレベル4以下のモンスター1体を特殊召喚する。②：自分の墓地のこのカードを除外して発動できる。"
    assert rr.diff_within_trust_limits(rr.norm(BASE), rr.norm(other)) is False


def test_limits_reject_large_fabrication_same_length():
    # 文字数はほぼ同じだが中身が大きく違う（作り話）
    fake = BASE[:20] + "相手フィールドの全てのモンスターを破壊し、自分はデッキから2枚ドローする。" + BASE[-30:]
    fake = fake[:len(BASE)]
    assert rr.diff_within_trust_limits(rr.norm(BASE), rr.norm(fake)) is False


def test_limits_reject_edge_insert_and_too_many_chars():
    a = rr.norm(BASE)
    assert rr.diff_within_trust_limits(a, a + "。") is True   # 1文字の端の追加は許容
    assert rr.diff_within_trust_limits(a, a + "その後ドローする。") is False   # 端にまとまった追加
    assert rr.diff_within_trust_limits(a, a.replace("自分は500LP回復する", "相手は800LP失う", 1)) is False   # 非一致が5文字以上（長さの比は範囲内）


def test_limits_reject_length_ratio():
    a = rr.norm(BASE)
    assert rr.diff_within_trust_limits(a, a[:-8]) is False
    assert rr.diff_within_trust_limits("", a) is False


def test_big_disagreement_not_trusted_in_pipeline(monkeypatch, clean_check):
    raw = _run(monkeypatch, _disagree_row(), text="1ターンに1度、融合召喚及び以下の方法でのみ特殊召喚できる。")
    assert "effect_text" in raw["uncertain_fields"] and "reread_trusted_on_disagree" not in raw


# ---- 設定の読み取り ----

def _settings_sb(value=None, present=True):
    rows = [{"key": reread_trust.SETTING_KEY, "value": value}] if present else []
    return FakeSB({"app_settings": rows}, pks={"app_settings": "key"})


@pytest.mark.parametrize("value,expected", [("1", True), (1, True), (True, True), ({"enabled": True}, True),
                                            ("0", False), (0, False), (False, False), ("yes", False), (None, False), ("", False)])
def test_parse_value(value, expected):
    assert reread_trust.parse_value(value) is expected


def test_is_enabled_default_on_when_no_row():
    assert reread_trust.is_enabled(_settings_sb(present=False)) is True


def test_is_enabled_reads_value_and_caches():
    sb = _settings_sb("1")
    assert reread_trust.is_enabled(sb) is True
    sb.tables["app_settings"][0]["value"] = "0"
    assert reread_trust.is_enabled(sb) is True   # キャッシュ内
    reread_trust.invalidate_cache()
    assert reread_trust.is_enabled(sb) is False


def test_is_enabled_unreadable_is_false_and_not_cached():
    assert reread_trust.is_enabled() is False   # conftest の DB はエラーを投げる
    assert reread_trust.is_enabled(_settings_sb("1")) is True   # 失敗はキャッシュしていない


def test_is_enabled_without_client_is_false(monkeypatch):
    monkeypatch.setattr(reread_trust, "_client_override", None)
    monkeypatch.setattr(reread_trust, "_client_cache", None)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    assert reread_trust.is_enabled() is False


# ---- 自動復帰 ----

def _trusted_raw():
    return {"reread_trusted_on_disagree": True, "effect_text_reread": {"text": "信じた読み直し文", "agreed": False}}


def test_disable_writes_zero_and_notifies_once(notices):
    sb = _settings_sb("1")
    assert reread_trust.disable(sb, "カードA", "理由") is True
    assert sb.tables["app_settings"][0]["value"] == "0"
    assert len(notices) == 1 and "カードA" in notices[0] and "理由" in notices[0]
    assert reread_trust.is_enabled(sb) is False
    assert reread_trust.disable(sb, "カードB", "理由2") is False   # すでに無効: 通知を重ねない
    assert len(notices) == 1


def test_disable_creates_row_when_absent(notices):
    sb = _settings_sb(present=False)
    reread_trust.disable(sb, "カードA", "理由")
    assert sb.tables["app_settings"] == [{"key": reread_trust.SETTING_KEY, "value": "0"}]


def test_disable_notifies_even_if_write_fails(notices):
    reread_trust.disable(None, "カードA", "理由")   # conftest の DB は失敗する
    assert len(notices) == 1 and "手動" in notices[0]


def test_write_failure_still_disables_in_process(notices):
    reread_trust.disable(None, "カードA", "理由")
    assert reread_trust._forced_off is True
    # DB 側が "1" のままでも、このプロセスでは無効
    assert reread_trust.is_enabled(_settings_sb("1")) is False


def test_notify_async_sends_in_thread(monkeypatch):
    import threading
    done = threading.Event()
    monkeypatch.setattr(reread_trust, "_notify", lambda m: done.set())
    reread_trust._notify_async("x")
    assert done.wait(2)


def test_notify_disables_mentions(monkeypatch):
    import discord_notify
    calls = []
    monkeypatch.setattr(discord_notify, "send_discord_message", lambda m, **k: calls.append(k))
    reread_trust._notify("x")
    assert calls == [{"no_mentions": True}]


def _admin_setup(monkeypatch, raw, effect="信じた読み直し文"):
    sb = FakeSB({"unreleased_cards": [{"id": 1, "name": "カードA", "effect_text": effect, "status": "approved",
                                       "extraction_raw": raw}],
                 "app_settings": [{"key": reread_trust.SETTING_KEY, "value": "1"}]},
                pks={"unreleased_cards": "id", "app_settings": "key"})
    monkeypatch.setattr(admin_module, "_supabase", sb, raising=False)
    monkeypatch.setattr(admin_module, "_ADMIN_KEY", "k", raising=False)
    import app as app_module
    app_module.app.config.update(TESTING=True)
    return sb, app_module.app.test_client()


def _put(client, body):
    return client.put("/api/admin/unreleased/1", json=body, headers={"X-Admin-Key": "k"})


def _setting(sb):
    return sb.tables["app_settings"][0]["value"]


def test_admin_edit_of_trusted_card_disables(monkeypatch, notices):
    sb, client = _admin_setup(monkeypatch, _trusted_raw())
    assert _put(client, {"effect_text": "新"}).status_code == 200
    assert _setting(sb) == "0" and len(notices) == 1 and "カードA" in notices[0]


def test_admin_edit_compares_normalized_with_trusted_reread_text(monkeypatch, notices):
    # 信じた読み直し文と norm() 後に同じ（空白・全角半角の違いだけ）なら外れではない。他の欄の編集も同様
    sb, client = _admin_setup(monkeypatch, _trusted_raw(), effect="旧")
    assert _put(client, {"effect_text": "信じた 読み直し文"}).status_code == 200
    assert _put(client, {"atk": 1000}).status_code == 200
    assert _setting(sb) == "1" and notices == []


def test_admin_edit_of_untrusted_card_keeps(monkeypatch, notices):
    sb, client = _admin_setup(monkeypatch, {})
    assert _put(client, {"effect_text": "新"}).status_code == 200
    assert _setting(sb) == "1" and notices == []


def test_on_effect_text_edited_without_reread_text_disables(notices):
    sb = _settings_sb("1")
    assert reread_trust.on_effect_text_edited(sb, 1, "A", {"reread_trusted_on_disagree": True}, "何か") is True


@pytest.mark.parametrize("path,method,body,status,action", [
    ("/api/admin/unreleased/1/unpublish", "post", None, "approved", "非公開"),
    ("/api/admin/unreleased/1/reject", "post", None, "approved", "却下"),
    ("/api/admin/unreleased/1/reject", "post", None, "linked", "却下"),
    ("/api/admin/unreleased/1", "delete", None, "approved", "削除"),
])
def test_withdrawing_published_trusted_card_disables(monkeypatch, notices, path, method, body, status, action):
    sb, client = _admin_setup(monkeypatch, _trusted_raw())
    sb.tables["unreleased_cards"][0]["status"] = status
    monkeypatch.setattr(admin_module._x_post, "collect_posted_safe", lambda *a, **k: [])
    monkeypatch.setattr(admin_module._x_post, "start_delete_thread", lambda *a, **k: None)
    resp = getattr(client, method)(path, headers={"X-Admin-Key": "k"})
    assert resp.status_code == 200
    assert _setting(sb) == "0" and len(notices) == 1


@pytest.mark.parametrize("path,method,status", [
    ("/api/admin/unreleased/1/reject", "post", "pending"),     # 未公開の却下は対象外
    ("/api/admin/unreleased/1/reject", "post", "needs_review"),
])
def test_rejecting_unpublished_trusted_card_keeps(monkeypatch, notices, path, method, status):
    sb, client = _admin_setup(monkeypatch, _trusted_raw())
    sb.tables["unreleased_cards"][0]["status"] = status
    monkeypatch.setattr(admin_module._x_post, "collect_posted_safe", lambda *a, **k: [])
    monkeypatch.setattr(admin_module._x_post, "start_delete_thread", lambda *a, **k: None)
    assert getattr(client, method)(path, headers={"X-Admin-Key": "k"}).status_code == 200
    assert _setting(sb) == "1" and notices == []


def test_withdrawing_untrusted_published_card_keeps(monkeypatch, notices):
    sb, client = _admin_setup(monkeypatch, {})
    monkeypatch.setattr(admin_module._x_post, "collect_posted_safe", lambda *a, **k: [])
    monkeypatch.setattr(admin_module._x_post, "start_delete_thread", lambda *a, **k: None)
    assert client.post("/api/admin/unreleased/1/unpublish", headers={"X-Admin-Key": "k"}).status_code == 200
    assert _setting(sb) == "1" and notices == []


def _report(sb, field):
    return tr.process_report(sb, {"name": "カードA", "field": field, "note": "誤り"}, "1.1.1.1", notify=lambda m: None)


def _report_sb(raw):
    return FakeSB({"unreleased_cards": [{"id": 1, "name": "カードA", "status": "approved", "hidden": False,
                                         "extraction_raw": raw}],
                   "unreleased_text_reports": [],
                   "app_settings": [{"key": reread_trust.SETTING_KEY, "value": "1"}]},
                  pks={"unreleased_cards": "id", "app_settings": "key"})


@pytest.mark.parametrize("field", ["effect_text", "other"])
def test_report_on_trusted_card_disables(notices, field):
    sb = _report_sb(_trusted_raw())
    _, status = _report(sb, field)
    assert status == 200 and _setting(sb) == "0" and len(notices) == 1


@pytest.mark.parametrize("field", ["card_name", "stats", "pendulum_effect"])
def test_report_on_other_fields_keeps(notices, field):
    sb = _report_sb(_trusted_raw())
    _, status = _report(sb, field)
    assert status == 200 and _setting(sb) == "1" and notices == []


def test_report_on_untrusted_card_keeps(notices):
    sb = _report_sb({})
    _report(sb, "effect_text")
    assert _setting(sb) == "1" and notices == []


def test_report_still_saved_when_revert_fails(monkeypatch):
    sb = _report_sb(_trusted_raw())

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(reread_trust, "on_report", boom)
    _, status = _report(sb, "effect_text")
    assert status == 200 and len(sb.tables["unreleased_text_reports"]) == 1


# ---- 集計・一覧 ----

def test_report_summary_counts_trusted_fixed_reported():
    import tools.auto_approve_report as rep
    v = {"eligible": True}

    def card(i, text, trusted=True):
        raw = {"auto_approve_verdict": v, "effect_text_reread": {"text": "読み直し文"}}
        if trusted:
            raw["reread_trusted_on_disagree"] = True
        return {"id": i, "name": f"c{i}", "status": "approved", "effect_text": text, "extraction_raw": raw}
    cards = [card(1, "読み直し文"), card(2, "直した文"), card(3, "読み直し 文"), card(4, "他", trusted=False)]
    out = rep.summarize(cards, reported_ids={3, 4})
    t = out["reread_trust"]
    assert [x["id"] for x in t["trusted"]] == [1, 2, 3]
    assert [x["id"] for x in t["fixed_later"]] == [2]
    assert [x["id"] for x in t["reported"]] == [3]
    rep.print_report(out)


def test_admin_list_exposes_flag_for_pending_too(monkeypatch):
    sb = FakeSB({"unreleased_cards": [{"id": 1, "name": "A", "status": "pending", "extracted_at": "2026-10-15",
                                       "extraction_raw": _trusted_raw()}],
                 "official_card_images": [], "unreleased_text_reports": []}, pks={"unreleased_cards": "id"})
    monkeypatch.setattr(admin_module, "_supabase", sb, raising=False)
    monkeypatch.setattr(admin_module, "_ADMIN_KEY", "k", raising=False)
    import app as app_module
    app_module.app.config.update(TESTING=True)
    resp = app_module.app.test_client().get("/api/admin/unreleased?status=pending", headers={"X-Admin-Key": "k"})
    assert resp.get_json()["cards"][0]["reread_trusted_on_disagree"] is True


def test_admin_list_exposes_flag(monkeypatch):
    sb = FakeSB({"unreleased_cards": [{"id": 1, "name": "A", "status": "approved", "extracted_at": "2026-10-15",
                                       "extraction_raw": _trusted_raw()}],
                 "official_card_images": [], "unreleased_text_reports": []}, pks={"unreleased_cards": "id"})
    monkeypatch.setattr(admin_module, "_supabase", sb, raising=False)
    monkeypatch.setattr(admin_module, "_ADMIN_KEY", "k", raising=False)
    import app as app_module
    app_module.app.config.update(TESTING=True)
    resp = app_module.app.test_client().get("/api/admin/unreleased?status=approved", headers={"X-Admin-Key": "k"})
    assert resp.status_code == 200
    assert resp.get_json()["cards"][0]["reread_trusted_on_disagree"] is True
