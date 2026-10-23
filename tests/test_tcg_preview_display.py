"""TCG先行カード（tcg_preview_cards）をシミュレーター・マイデッキで使うための表示解決（段2・2026-10-23）を検証する。

- card_display の3つ目の読み先（発売済み → 未発売 → TCG先行）。英語名で引く・active かつ非表示でないものだけ
- 機械訳は translation_status='done' のときだけ使う。英語の原文・「非公式訳」の印を足す
- 価格の流れ（未発売の発売日の判定 get_unreleased_proxy・/api/validate の発売済み扱い）には入れない
- 魔法・罠の判定（「通常魔法」「通常罠」「儀式魔法」をモンスター扱いしていた不具合の修正）
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

import card_display as display


class Query:
    def __init__(self, database, table):
        self.database, self.table = database, table
        self.filters = []
        self.bounds = None
        self.order_field = "id"

    def select(self, fields):
        return self

    def in_(self, field, values):
        self.filters.append(lambda row: row.get(field) in values)
        return self

    def eq(self, field, value):
        self.filters.append(lambda row: row.get(field) == value)
        return self

    def is_(self, field, value):
        self.filters.append(lambda row: row.get(field) is None)
        return self

    def order(self, field):
        self.order_field = field
        return self

    def range(self, start, end):
        self.bounds = start, end
        return self

    def execute(self):
        self.database.calls.append(self.table)
        if self.table in self.database.fail:
            raise RuntimeError("DB接続失敗")
        rows = [r for r in self.database.rows[self.table] if all(fn(r) for fn in self.filters)]
        rows.sort(key=lambda r: r.get(self.order_field, 0))
        if self.bounds:
            rows = rows[self.bounds[0]:self.bounds[1] + 1]
        return SimpleNamespace(data=deepcopy(rows))


def _tcg_row(konami_id, name_en, **kw):
    row = {
        "konami_id": konami_id, "name_en": name_en, "card_type": "シンクロ・効果モンスター",
        "attribute": "光", "race": "天使族", "level": 7, "rank": None, "link_val": None,
        "atk": 2500, "def": "2000", "pendulum_scale": None,
        "effect_text_en": "1 Tuner + 1+ non-Tuners\nYou can target 1 monster; banish it.",
        "effect_text_ja": "チューナー＋チューナー以外のモンスター１体以上\n①：モンスター１体を対象として発動できる。",
        "pendulum_effect_en": "", "pendulum_effect_ja": "",
        "translation_status": "done", "product_name_en": "CHAOS ORIGINS", "set_code": "CHOR-EN041",
        "tcg_release_date": "2026-07-03",
        "image_url": f"https://example.supabase.co/storage/v1/object/public/official-card-images/tcg_preview/{konami_id}.png",
        "image_page_url": "", "status": "active", "hidden": False,
    }
    row.update(kw)
    return row


class Database:
    def __init__(self):
        self.rows = {
            "unreleased_cards": [], "official_card_images": [],
            "app_settings": [{"key": "OFFICIAL_IMAGE_DISPLAY", "value": {"enabled": True}}],
            "tcg_preview_cards": [
                _tcg_row(23307, "Angelechy Destrier"),
                _tcg_row(19359, "Anotherverse Gluttonia", card_type="通常モンスター",
                         effect_text_en="A massive, malevolent monstrosity.", effect_text_ja="",
                         translation_status="skipped"),
                _tcg_row(23297, "Distrust Paranoia", card_type="通常罠", attribute="", race="",
                         level=None, atk=None, image_url=None, **{"def": ""}),
                _tcg_row(23900, "Pending Card", translation_status="pending", effect_text_ja="古い訳"),
                _tcg_row(23901, "Hidden Card", hidden=True),
                _tcg_row(23902, "Linked Card", status="ocg_linked"),
            ],
        }
        self.fail, self.calls = set(), []

    def table(self, table):
        return Query(self, table)


@pytest.fixture
def database(monkeypatch):
    db = Database()
    display.invalidate_cache()
    monkeypatch.setattr(display, "_supabase_client", db)
    monkeypatch.setattr(display, "_released_resolver", lambda name: None)
    yield db
    display.invalidate_cache()


# ── card_display ──

def test_英語名で画像を返し_TCG先行の印を付ける(database):
    r = display.resolve_card_display("Angelechy Destrier")
    assert r["kind"] == "image"
    assert r["source"] == "official_sample"  # 透かしなし・contain 表示は未発売の公式SAMPLEと同じ
    assert r["tcg_preview"] is True
    assert r["url"].endswith("/tcg_preview/23307.png")


def test_画像が無いと_proxy_で返す(database):
    r = display.resolve_card_display("Distrust Paranoia")
    assert r["kind"] == "proxy"
    p = r["proxy"]
    assert p["tcg_preview"] is True
    assert p["broad_type"] == "trap" and p["is_ex"] is False


def test_画像表示の設定が無効なら_proxy(database):
    database.rows["app_settings"] = [{"key": "OFFICIAL_IMAGE_DISPLAY", "value": {"enabled": False}}]
    r = display.resolve_card_display("Angelechy Destrier")
    assert r["kind"] == "proxy" and r["proxy"]["tcg_preview"] is True


def test_非表示_日本に来たカードは引かない(database):
    assert display.resolve_card_display("Hidden Card") == {"kind": "none"}
    assert display.resolve_card_display("Linked Card") == {"kind": "none"}
    assert sorted(display.get_tcg_preview_names()) == [
        "Angelechy Destrier", "Anotherverse Gluttonia", "Distrust Paranoia", "Pending Card"]


def test_大文字小文字と記号のゆれを吸収する(database):
    assert display.get_tcg_preview_proxy("angelechy destrier")["name"] == "Angelechy Destrier"
    assert display.resolve_card_display("ANGELECHY DESTRIER")["kind"] == "image"


def test_訳した_proxy_は未発売と同じ形に原文と非公式訳の印を足す(database):
    p = display.get_tcg_preview_proxy("Angelechy Destrier")
    base_keys = set(display._build_proxy_data({}).keys())
    assert base_keys <= set(p.keys())
    assert p["effect_text"].startswith("チューナー＋")
    assert p["unofficial_translation"] is True
    assert p["effect_text_en"].startswith("1 Tuner")
    assert p["product_name"] == "CHAOS ORIGINS"
    assert p["tcg_release_date"] == "2026-07-03"
    # 未発売の release_date は「発売日が来たら発売済み」の判定に使うので空にする（価格の流れに入れない）
    assert p["release_date"] == ""
    assert p["broad_type"] == "monster" and p["is_ex"] is True


def test_訳が無い_または_done_以外は訳を出さない(database):
    normal = display.get_tcg_preview_proxy("Anotherverse Gluttonia")
    assert normal["effect_text"] == "" and normal["unofficial_translation"] is False
    assert normal["effect_text_en"] == "A massive, malevolent monstrosity."
    pending = display.get_tcg_preview_proxy("Pending Card")
    assert pending["effect_text"] == "" and pending["unofficial_translation"] is False


def test_価格の関門は未発売カードだけを見る(database):
    # get_unreleased_proxy は /api/validate・検索の関門の「発売日到来＝発売済み」判定に使う
    assert display.get_unreleased_proxy("Angelechy Destrier") is None
    assert display.get_display_proxy("Angelechy Destrier")["tcg_preview"] is True


def test_同名なら未発売カードを優先する(database):
    database.rows["unreleased_cards"] = [{
        "id": 1, "name": "Angelechy Destrier", "reading": "", "card_type": "効果モンスター",
        "attribute": "光", "race": "天使族", "level": 4, "rank": None, "link_val": None, "atk": 1, "def": "1",
        "pendulum_scale": None, "pendulum_effect": "", "effect_text": "未発売の文", "product_name": "",
        "release_date": "2099-01-01", "extracted_at": None, "status": "approved", "hidden": False,
    }]
    r = display.resolve_card_display("Angelechy Destrier")
    assert r["kind"] == "proxy" and "tcg_preview" not in r["proxy"]
    assert display.get_display_proxy("Angelechy Destrier")["effect_text"] == "未発売の文"


def test_まとめて解決(database):
    got = display.resolve_card_displays(["Angelechy Destrier", "Distrust Paranoia", "存在しない"])
    assert got["Angelechy Destrier"]["tcg_preview"] is True
    assert got["Distrust Paranoia"]["kind"] == "proxy"
    assert got["存在しない"] == {"kind": "none"}


def test_キャッシュは無効化で取り直し_取得失敗は空(database):
    display.get_tcg_preview_names()
    n = database.calls.count("tcg_preview_cards")
    display.get_tcg_preview_names()
    assert database.calls.count("tcg_preview_cards") == n  # TTL 内は DB を読まない
    database.rows["tcg_preview_cards"].append(_tcg_row(23999, "New Card"))
    display.invalidate_cache()
    assert "New Card" in display.get_tcg_preview_names()
    display.invalidate_cache()
    database.fail.add("tcg_preview_cards")
    assert display.get_tcg_preview_names() == []
    assert display.resolve_card_display("Angelechy Destrier") == {"kind": "none"}


def test_refresh_caches_はTCG先行も取り直す(database):
    display.get_tcg_preview_names()
    database.rows["tcg_preview_cards"].append(_tcg_row(23999, "New Card"))
    display.refresh_caches()
    assert "New Card" in display.get_tcg_preview_names()


@pytest.mark.parametrize("card_type, expected", [
    ("通常魔法", "spell"), ("儀式魔法", "spell"), ("永続魔法", "spell"), ("魔法", "spell"),
    ("通常罠", "trap"), ("カウンター罠", "trap"),
    ("通常モンスター", "monster"), ("効果モンスター", "monster"), ("儀式モンスター", "monster"),
    ("シンクロ・チューナー・効果モンスター", "monster"), ("効果モンスター/チューナー", "monster"), ("", "monster"),
])
def test_魔法罠の判定(card_type, expected):
    assert display._build_proxy_data({"card_type": card_type})["broad_type"] == expected


# ── API（app.py の配線） ──

@pytest.fixture
def client(database):
    import app as app_module
    return app_module.app.test_client()


def test_候補に英語名の部分一致を印つきで足す(client):
    got = client.get("/api/suggest?include_unreleased=1&q=Angelechy").get_json()
    assert {"name": "Angelechy Destrier", "unreleased": True, "tcg_preview": True} in got
    got = client.get("/api/suggest?include_unreleased=1&q=paranoia").get_json()
    assert [g["name"] for g in got if g.get("tcg_preview")] == ["Distrust Paranoia"]
    # 価格検索の候補（include_unreleased なし）には出さない
    assert "Angelechy Destrier" not in client.get("/api/suggest?q=Angelechy").get_json()


def test_validate_は価格検索へ進ませない(client):
    got = client.get("/api/validate?q=angelechy%20destrier").get_json()
    assert got == {"valid": True, "name": "Angelechy Destrier", "unreleased": True, "tcg_preview": True}


def test_card_info_は訳と原文を返す(client):
    got = client.get("/api/card-info?name=Angelechy%20Destrier").get_json()
    assert got["kind"] == "proxy" and got["found"] is False and got["is_ex"] is True
    assert got["proxy"]["tcg_preview"] is True and got["proxy"]["effect_text_en"]


def test_card_infos_と_card_images(client):
    infos = client.post("/api/card-infos", json={"names": ["Distrust Paranoia", "Angelechy Destrier"]}).get_json()["infos"]
    assert infos["Distrust Paranoia"]["broad_type"] == "trap"
    assert infos["Angelechy Destrier"]["proxy"]["unofficial_translation"] is True
    resp = client.post("/api/card-images", json={"names": ["Angelechy Destrier"]})
    assert resp.get_json()["images"]["Angelechy Destrier"]["source"] == "official_sample"
    assert resp.headers["Cache-Control"] == "no-store"


def test_名前の一覧(client):
    resp = client.get("/api/tcg-preview-names")
    assert resp.get_json()["names"] == ["Angelechy Destrier", "Anotherverse Gluttonia", "Distrust Paranoia", "Pending Card"]
