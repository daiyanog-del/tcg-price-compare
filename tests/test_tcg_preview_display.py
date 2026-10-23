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


# ── レビュー指摘への対応（2026-10-23） ──

def test_発売済みと同名なら発売済みを優先(database, monkeypatch):
    monkeypatch.setattr(display, "_released_resolver", lambda name: "https://released/x.png")
    assert display.resolve_card_display("Angelechy Destrier")["source"] == "released"
    assert display.resolve_card_displays(["Angelechy Destrier"])["Angelechy Destrier"]["source"] == "released"


def test_まとめて解決でもゆれを吸収する(database):
    got = display.resolve_card_displays(["angelechy destrier"])
    assert got["angelechy destrier"]["tcg_preview"] is True


def test_期限切れは古い値を返して裏で取り直す(database, monkeypatch):
    display.get_tcg_preview_names()
    started = []
    monkeypatch.setattr(display, "_start_background_refresh", lambda name, fn: started.append(name))
    monkeypatch.setattr(display, "_tcg_preview_cache_at", display._tcg_preview_cache_at - display._CACHE_TTL - 1)
    database.rows["tcg_preview_cards"].append(_tcg_row(23999, "New Card"))
    assert "New Card" not in display.get_tcg_preview_names()  # 古い値をすぐ返す
    assert started == ["tcg_preview"]


def test_取り直し中に無効化されたら古い応答を捨てる(database, monkeypatch):
    display.get_tcg_preview_names()
    real_fetch = display._fetch_tcg_preview_cards

    def fetch_then_invalidate(*, strict=False):
        rows = real_fetch(strict=strict)
        display.invalidate_cache()  # 取得中に管理操作で無効化された
        return rows

    monkeypatch.setattr(display, "_fetch_tcg_preview_cards", fetch_then_invalidate)
    display._refresh_tcg_preview()
    assert display._tcg_preview_cache is None


def test_TCG先行の辞書が読めるか(database):
    assert display.tcg_preview_data_available() is True
    display.invalidate_cache()
    database.fail.add("tcg_preview_cards")
    assert display.tcg_preview_data_available() is False


def test_名前の一覧は取得失敗をキャッシュさせない(client, database):
    display.invalidate_cache()
    database.fail.add("tcg_preview_cards")
    resp = client.get("/api/tcg-preview-names")
    assert resp.get_json()["names"] == [] and resp.headers["Cache-Control"] == "no-store"


def test_候補が10件埋まったらTCG先行は足さない(client, monkeypatch):
    import app as app_module
    app_module._load_cardnames()
    monkeypatch.setattr(app_module, "_cardnames", [f"Angelechy 和名{i}" for i in range(12)])
    got = client.get("/api/suggest?include_unreleased=1&q=Angelechy").get_json()
    assert len(got) == 10 and not any(g.get("tcg_preview") for g in got)


def test_収集対象の登録は英語名を弾く(client, monkeypatch):
    import app as app_module
    app_module._reset_rate_limits()
    app_module._load_cardnames()
    monkeypatch.setattr(app_module, "_supabase_client", None, raising=False)
    got = client.post("/api/track", json={"card": "Angelechy Destrier"}).get_json()
    assert got == {"ok": False, "reason": "unknown_card"}


class _SyncThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def _deck_events(client, path, cards):
    import json
    import app as app_module
    app_module._reset_rate_limits()
    data = client.get(path, query_string={"cards": cards, "shops": ["遊々亭"]}).get_data(as_text=True)
    return [json.loads(c[len("data: "):]) for c in data.split("\n\n") if c.startswith("data: ")]


@pytest.mark.parametrize("path", ["/api/deck", "/api/deck-buy"])
def test_デッキの一括検索はTCG先行を店舗へ取りに行かず記録もしない(client, monkeypatch, path):
    import app as app_module
    scraped, recorded = [], []

    def fake_scrape(card_name):
        scraped.append(card_name)
        return [{"shop": "遊々亭", "name": card_name, "rarity": "ノーマル", "code": "", "condition": "-",
                 "price": 100, "stock": 1, "sold_out": False, "url": "u", "image": ""}]

    for name in ("SHOPS", "BUYBACK_SHOPS"):
        monkeypatch.setattr(app_module, name, [("遊々亭", fake_scrape)], raising=False)
    monkeypatch.setattr(app_module, "cache_get_shops", lambda name, shops, include_partial=False: ({}, list(shops)))
    monkeypatch.setattr(app_module, "cache_store_shops", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "buyback_cache_get_shops", lambda name, shops: ({}, list(shops)))
    monkeypatch.setattr(app_module, "buyback_cache_store_shops", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "Thread", _SyncThread, raising=False)
    monkeypatch.setattr(app_module, "_supabase_client", None, raising=False)
    monkeypatch.setattr(app_module, "_record_deck_search", lambda names: recorded.extend(names))
    app_module._load_cardnames()
    events = _deck_events(client, path, "Angelechy Destrier|灰流うらら")
    done = {e["name"]: e for e in events if e["type"] == "card_done"}
    assert done["Angelechy Destrier"]["tcg_preview"] is True and done["Angelechy Destrier"]["best"] is None
    assert "Angelechy Destrier" not in scraped and "灰流うらら" in scraped
    assert recorded == ["灰流うらら"]
    assert events[-1]["type"] == "done"


# ── 日本に来たカードの読み替え（段3・ocg_linked の英語名 → 日本語の正式名） ──

def _linked(database, en="Renamed Card", ja="改名カード", konami_id=24000, **kw):
    """表に ocg_linked の行を足し、キャッシュを捨てる。"""
    database.rows["tcg_preview_cards"].append(
        _tcg_row(konami_id, en, status="ocg_linked", ocg_name=ja, **kw))
    display.invalidate_cache()


def _unreleased_row(name, release_date="2099-01-01"):
    return {
        "id": 7, "name": name, "reading": "", "card_type": "効果モンスター",
        "attribute": "光", "race": "天使族", "level": 4, "rank": None, "link_val": None, "atk": 1, "def": "1",
        "pendulum_scale": None, "pendulum_effect": "", "effect_text": "日本語の未発売の文", "product_name": "",
        "release_date": release_date, "extracted_at": None, "status": "approved", "hidden": False,
    }


def test_読み替え_発売済みの日本語名へ(database, monkeypatch):
    _linked(database)
    monkeypatch.setattr(display, "_released_resolver",
                        lambda name: "https://released/jp.png" if name == "改名カード" else None)
    r = display.resolve_card_display("Renamed Card")
    assert r == {"kind": "image", "url": "https://released/jp.png", "source": "released", "renamed_to": "改名カード"}
    got = display.resolve_card_displays(["Renamed Card", "renamed card"])  # ゆれも吸収する
    assert got["Renamed Card"] == r and got["renamed card"] == r


def test_読み替え_未発売の日本語名へ(database):
    _linked(database)
    database.rows["unreleased_cards"] = [_unreleased_row("改名カード")]
    database.rows["official_card_images"] = [{
        "unreleased_card_id": 7, "public_url": "https://example/unreleased.png", "thumb_url": None,
        "hidden": False, "deleted_at": None}]
    display.invalidate_cache()
    r = display.resolve_card_display("Renamed Card")
    assert r["kind"] == "image" and r["source"] == "official_sample" and r["renamed_to"] == "改名カード"
    assert r["url"] == "https://example/unreleased.png" and "tcg_preview" not in r
    assert display.resolve_card_displays(["Renamed Card"])["Renamed Card"] == r
    # 画像が無ければ未発売の proxy（日本語名）
    database.rows["official_card_images"] = []
    display.invalidate_cache()
    r = display.resolve_card_display("Renamed Card")
    assert r["kind"] == "proxy" and r["proxy"]["name"] == "改名カード" and r["renamed_to"] == "改名カード"
    assert display.get_display_proxy("Renamed Card")["effect_text"] == "日本語の未発売の文"


def test_読み替え_日本語名で解決できないうちはTCG先行の表示にとどまる(database):
    _linked(database)
    r = display.resolve_card_display("Renamed Card")
    assert r["kind"] == "image" and r["tcg_preview"] is True and r["renamed_to"] == "改名カード"
    assert r["url"].endswith("/tcg_preview/24000.png")
    assert display.resolve_card_displays(["Renamed Card"])["Renamed Card"] == r
    # 画像が無い行は TCG先行の proxy（英語名のまま）
    database.rows["tcg_preview_cards"][-1]["image_url"] = None
    display.invalidate_cache()
    r = display.resolve_card_display("Renamed Card")
    assert r["kind"] == "proxy" and r["proxy"]["tcg_preview"] is True and r["renamed_to"] == "改名カード"
    assert display.get_display_proxy("Renamed Card")["tcg_preview"] is True


def test_読み替え_価格の関門は変えない(database):
    _linked(database)
    database.rows["unreleased_cards"] = [_unreleased_row("改名カード")]
    display.invalidate_cache()
    assert display.get_unreleased_proxy("Renamed Card") is None  # 英語名のままでは未発売カードに当たらない
    assert display.get_unreleased_proxy("改名カード")["name"] == "改名カード"


def test_読み替えの公開関数(database):
    _linked(database)
    _linked(database, en="Another Renamed", ja="別の改名", konami_id=24001)
    assert display.get_tcg_preview_renames() == {"Renamed Card": "改名カード", "Another Renamed": "別の改名"}
    assert display.renamed_name("Renamed Card") == "改名カード"
    assert display.renamed_name("RENAMED CARD") == "改名カード"
    assert display.renamed_name("Angelechy Destrier") is None  # active は読み替えない
    assert display.renamed_name("") is None


def test_日本に来たカードは候補_一覧に出さない(database):
    _linked(database)
    _linked(database, en="Hidden Renamed", ja="隠し", konami_id=24002, hidden=True)
    _linked(database, en="No Name", ja=None, konami_id=24003)
    assert "Renamed Card" not in display.get_tcg_preview_names()
    assert "Renamed Card" not in [c["name"] for c in display.get_tcg_preview_cards()["cards"]]
    assert display.get_tcg_preview_proxy("Renamed Card") is None
    # hidden・ocg_name 無しは読み替えない
    assert display.get_tcg_preview_renames() == {"Renamed Card": "改名カード"}
    assert display.resolve_card_display("Hidden Renamed") == {"kind": "none"}


def test_読み替えの取得は1回のDB読みで済む(database):
    _linked(database)
    display.get_tcg_preview_names()
    display.get_tcg_preview_renames()
    display.resolve_card_display("Renamed Card")
    assert database.calls.count("tcg_preview_cards") == 1


def test_読み替えの取得失敗は例外(database):
    display.invalidate_cache()
    database.fail.add("tcg_preview_cards")
    with pytest.raises(display.DisplayDataUnavailable):
        display.get_tcg_preview_renames()
    assert display.renamed_name("Renamed Card") is None  # 画面表示側は縮退（読み替えなし）


def test_API_日本に来たカードの一覧(client, database):
    _linked(database)
    resp = client.get("/api/tcg-preview-renames")
    assert resp.status_code == 200
    assert resp.get_json() == {"renames": {"Renamed Card": "改名カード"}}
    assert resp.headers["Cache-Control"] == "public, max-age=300"
    # 候補・名前の一覧には出ない
    assert "Renamed Card" not in client.get("/api/tcg-preview-names").get_json()["names"]
    assert client.get("/api/suggest?include_unreleased=1&q=Renamed").get_json() == []


def test_API_日本に来たカードの一覧_取得失敗は503(client, database):
    display.invalidate_cache()
    database.fail.add("tcg_preview_cards")
    resp = client.get("/api/tcg-preview-renames")
    assert resp.status_code == 503 and "error" in resp.get_json()
    assert resp.headers["Cache-Control"] == "no-store"


def test_API_card_infos_と_card_images_に読み替えを返す(client, database, monkeypatch):
    import app as app_module
    _linked(database)
    monkeypatch.setattr(display, "_released_resolver",
                        lambda name: "https://released/jp.png" if name == "改名カード" else None)
    monkeypatch.setattr(app_module, "_get_ygores_name_index", lambda: {"改名カード": [4242]})
    looked_up = []

    def fake_fetch(card_id, name):
        looked_up.append(name)
        return {"found": True, "konami_id": card_id, "broad_type": "monster", "is_ex": False}

    monkeypatch.setattr(app_module, "_fetch_card_info_by_id", fake_fetch)

    infos = client.post("/api/card-infos", json={"names": ["Renamed Card", "Distrust Paranoia"]})
    got = infos.get_json()["infos"]
    assert got["Renamed Card"]["found"] is True and got["Renamed Card"]["renamed_to"] == "改名カード"
    assert looked_up == ["改名カード"]  # 名前索引は日本語名で引く
    assert "renamed_to" not in got["Distrust Paranoia"]
    assert infos.headers["Cache-Control"] == "no-store"

    imgs = client.post("/api/card-images", json={"names": ["Renamed Card", "Angelechy Destrier"]})
    body = imgs.get_json()
    assert body["images"]["Renamed Card"] == "https://released/jp.png"  # 文字列のまま（後方互換）
    assert body["renames"] == {"Renamed Card": "改名カード"}
    assert imgs.headers["Cache-Control"] == "no-store"
    # 読み替えが無い応答には renames を付けない
    assert "renames" not in client.post("/api/card-images", json={"names": ["Angelechy Destrier"]}).get_json()


def test_API_card_info_と_card_image_に読み替えを返す(client, database):
    _linked(database)  # 日本語名では解決できない → TCG先行の表示にとどまる
    info = client.get("/api/card-info?name=Renamed%20Card").get_json()
    assert info["kind"] == "proxy" and info["renamed_to"] == "改名カード" and info["proxy"]["tcg_preview"] is True
    img = client.get("/api/card-image?name=Renamed%20Card").get_json()
    assert img["kind"] == "image" and img["renamed_to"] == "改名カード"


def test_API_card_info_は日本語名で名前索引を引く(client, database, monkeypatch):
    import app as app_module
    _linked(database)
    monkeypatch.setattr(display, "_released_resolver",
                        lambda name: "https://released/jp.png" if name == "改名カード" else None)
    monkeypatch.setattr(app_module, "_get_ygores_name_index", lambda: {"改名カード": [4242]})
    monkeypatch.setattr(app_module, "_fetch_card_info_by_id",
                        lambda cid, name: {"found": True, "konami_id": cid, "name_used": name})
    got = client.get("/api/card-info?name=Renamed%20Card").get_json()
    assert got["found"] is True and got["name_used"] == "改名カード"
    assert got["kind"] == "released" and got["renamed_to"] == "改名カード"


def test_一括検索の入口で日本に来た英語名を日本語名に読み替える(database):
    # 編集中の画面・まだ開いていない端末に英語名が残っていても、英語名で全店へ問い合わせない（2026-10-23 段3）
    import app as app_module
    _linked(database)
    with app_module.app.test_request_context():
        entries, err = app_module._parse_deck_entries("2 Renamed Card|1 Angelechy Destrier")
    assert err is None
    assert [e["name"] for e in entries] == ["改名カード", "Angelechy Destrier"]  # 日本に来ていない TCG先行カードはそのまま
    assert [e["qty"] for e in entries] == [2, 1]
