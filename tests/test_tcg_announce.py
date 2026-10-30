"""海外先行の予告カード（2026-10-29）を検証する。

- tcg_announce: Yugipedia の一覧・弾・カードのページの読み取り（2026-10-29 Maze of Machines の実データの形）
- tools/tcg_announce_watch: 種別の日本語・要約の数字の点検・関連カード
- tcg_announce_post: 何をいつ出すか（初報 → 翌日からカードごと → 続報）と本文の長さ
- /tcg-preview/<slug> のページと OG 画像
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

import tcg_announce as ya
import tcg_announce_post as tp

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import tcg_announce_watch as tw  # noqa: E402

SET_LIST = """{{Set page header}}

{{Set list|region=EN|print=Unknown|
MZMC-EN000; Hamon, Lord of Striking Thunder; Starlight Rare; New artwork
MZMC-EN001; Pendulum Machine of Misery;; New
MZMC-EN004; ;
MZMC-EN005; Rampant Barrel Dragon;; New
MZMC-EN0??; Space Megatron;; New // description::''(unofficial name)''
MZMC-EN0??; Dominus Spark;; Reprint
}}
"""

SET_PAGE = """{{Infobox set
| en_name         = Maze of Machines
| prefix          = MZMC
| size            = 153
| cover_card      =
* Rampant Barrel Dragon
| na_release_date = March 19, 2027
}}
"""

RAMPANT = """{{Unofficial lore|English}}
{{CardTable2
| ja_name               =
| attribute             = DARK
| types                 = Machine / Fusion / Effect
| level                 = 8
| atk                   = 3000
| def                   = 2200
| text                  = {{quote|text=In the "[[Barrel Dragon]]" tradition, its Quick Effect flips 3 coins! [[Pendulum Machine of Misery|MZMC- 001]] synergy.|source=Blurb from [https://www.southernhobby.com/p84133/ southernhobby.com]}}
| en_sets               =
MZMC-EN005; Maze of Machines;
| database_id           =
}}
"""

MEGATRON = """{{Unofficial name|English}}
{{CardTable2
| ja_name             = メガトロン
| attribute           = ???
| types               = ??? / Effect
| level               = ???
| atk                 = ???
| def                 = ???
| text                = ''N/A''
| en_sets             =
MZMC-EN0??; Maze of Machines;
| database_id         =
}}
"""

CLAY = """{{CardTable2
| ja_name = E・HERO クレイ・ガードマン
| types = Warrior / Fusion / Effect
| text = Some official text.
| jp_sets =
DP30-JP001; 30th Anniversary Demon's Pack; Ultra Rare
| database_id = 23147
}}
"""

TRAP_UNKNOWN = """{{CardTable2
| card_type = Trap
| property  =
| text      = ''N/A''
}}
"""


# ── 読み取り ──────────────────────────────

def test_一覧は新規カードだけを拾い_名前の空の行と再録は除く():
    rows = ya.parse_set_list(SET_LIST)
    new = [r for r in rows if ya.is_new_card(r)]
    assert [(r["code"], r["name"]) for r in new] == [
        ("MZMC-EN001", "Pendulum Machine of Misery"), ("MZMC-EN005", "Rampant Barrel Dragon"),
        ("MZMC-EN0??", "Space Megatron")]
    assert all(r["name"] for r in rows)


def test_弾のページから接頭辞と収録枚数と発売日():
    assert ya.parse_set_page(SET_PAGE) == {"name_en": "Maze of Machines", "prefix": "MZMC", "size": 153,
                                           "release_date": "2027-03-19", "image": "",
                                           "cover_cards": ["Rampant Barrel Dragon"]}
    with_image = SET_PAGE.replace("{{Infobox set", "{{Infobox set\n| image = MZMC-BoosterEN.png")
    assert ya.parse_set_page(with_image)["image"] == "MZMC-BoosterEN.png"
    assert ya.slugify("Maze of Machines") == "maze-of-machines"


def test_宣伝文のカードを読む():
    c = ya.parse_card(RAMPANT)
    assert c["mirror_like"] == {"type": "monster", "properties": ["Machine", "Fusion", "Effect"],
                                "englishAttribute": "dark", "englishProperty": None}
    assert (c["level"], c["atk"], c["def"]) == (8, "3000", "2200")
    assert c["lore"]["kind"] == "blurb"
    assert c["lore"]["source_url"] == "https://www.southernhobby.com/p84133/"
    # 本文はリンクを外した文、raw はリンク先を拾えるよう元のまま
    assert "[[" not in c["lore"]["text_en"] and "Barrel Dragon" in c["lore"]["text_en"]
    assert ya.link_targets(c["lore"]["raw"]) == ["Barrel Dragon", "Pendulum Machine of Misery"]
    assert c["en_set_codes"] == ["MZMC-EN005"]
    assert not ya.exists_in_konami_db(c)


def test_漫画由来で日本名があっても公式DBに無ければ予告カード():
    c = ya.parse_card(MEGATRON)
    assert not ya.exists_in_konami_db(c)
    assert c["unofficial_name"] is True
    assert c["lore"]["kind"] == "none"
    assert (c["level"], c["atk"], c["def"]) == (None, "?", "?")
    assert c["mirror_like"]["properties"] == ["Effect"] and c["mirror_like"]["englishAttribute"] == ""


def test_公式DBの番号か日本の収録があれば既存カードとして除く():
    assert ya.exists_in_konami_db(ya.parse_card(CLAY))


def test_書き損じの閉じ括弧で読み取りが崩れない():
    p = ya.template_params("{{CardTable2\n| a = x]] y\n| b = [[L|T]]\n}}", "CardTable2")
    assert p == {"a": "x]] y", "b": "[[L|T]]"}


# ── 種別・点検・関連カード ─────────────────

class _Vocab:
    props = {"Machine": "機械族", "Fusion": "融合", "Effect": "効果"}
    order = {"融合": 0, "効果": 1}
    attributes = {"dark": "闇"}
    spell_trap = {("trap", "continuous"): "永続", ("spell", "continuous"): "永続"}


def test_種類が空欄の罠は通常罠と決めつけない():
    assert tw.card_type_ja(ya.parse_card(TRAP_UNKNOWN), _Vocab())[0] == "罠"
    normal = ya.parse_card(TRAP_UNKNOWN.replace("| property  =", "| property  = Normal"))
    assert tw.card_type_ja(normal, _Vocab())[0] == "通常罠"


def test_モンスターの種別と属性と種族():
    assert tw.card_type_ja(ya.parse_card(RAMPANT), _Vocab()) == ("融合・効果モンスター", "闇", "機械族")


def test_要約の点検は英文に無い2以上の数字だけを拾う():
    assert tw.invented_numbers("flips 3 coins, gain 700 ATK, MZMC-001", "コインを3回投げ、700アップ、相手モンスター1体") == []
    assert tw.invented_numbers("flips three coins", "３回投げる") == []
    assert tw.invented_numbers("gain 700 ATK", "攻撃力が800アップ") == ["数値 800 が英文に無い"]


def test_関連カードは対応表に完全一致する既存カードだけ():
    class C:
        card_names = {"Barrel Dragon": "リボルバー・ドラゴン", "Machine": "（無関係）"}
    raw = 'In the "[[Barrel Dragon]]" way, [[Machine]]s and [[Unknown New Card]]'
    assert tw.related_cards(C(), raw, "Rampant Barrel Dragon") == ["リボルバー・ドラゴン", "（無関係）"]
    assert tw.related_cards(C(), "[[Barrel Dragon]]", "Barrel Dragon") == []


# ── 投稿の判断 ────────────────────────────

SET = {"prefix": "MZMC", "slug": "maze-of-machines", "name_en": "Maze of Machines", "tcg_release_date": "2027-03-19",
       "size": 153}


def _card(pid, name, order, related=(), kind="blurb", warnings=(), status="done", seen="2026-10-29T01:00:00+00:00"):
    return {"page_id": pid, "name_en": name, "set_code": f"MZMC-EN{order:03d}", "list_order": order,
            "lore_kind": kind, "summary_status": status, "summary_short_ja": f"{name}の概要。",
            "summary_ja": ["一行目"], "summary_warnings": list(warnings), "related_ja": list(related),
            "attribute": "闇", "race": "機械族", "card_type": "効果モンスター", "level": 6, "atk": "1750", "def": "2000",
            "first_seen_at": seen, "lore_at": seen if kind != "none" else None}


CARDS = [_card(1, "Pendulum Machine of Misery", 1), _card(2, "Slot Machine 777", 2, related=["スロットマシーンAM－７"]),
         _card(3, "Bad Numbers", 3, warnings=["数値 800 が英文に無い"]), _card(4, "Name Only", 4, kind="none", status="skipped")]
NOW = datetime(2026, 10, 29, 11, 0, tzinfo=timezone.utc)  # JST 20:00


def _first(at="2026-10-29T02:00:00+00:00", count=2, phase="official", status="posted"):
    return {"kind": "set_first", "set_prefix": "MZMC", "page_id": None, "known_count": count, "created_at": at,
            "phase": phase, "status": status}


def _official(cards):
    """公式の段のカード（紹介文の出どころが KONAMI 公式の商品ページ）にする。"""
    return [dict(c, lore_source_url=OFFICIAL_URL) for c in cards]


def _leak(cards):
    """速報の段のカード（紹介文の出どころが販売店）にする。"""
    return [dict(c, lore_source_url=SHOP_URL) for c in cards]


def test_最初は初報_数えるのは点検を通った概要のあるカードだけ():
    item = tp.decide([SET], {"MZMC": CARDS}, [], NOW)
    assert item["kind"] == "set_first"
    assert [c["page_id"] for c in item["cards"]] == [1, 2]


def test_カードごとは初報の翌日から_時間帯の中で1日1枚_関連の分かるカードを先に():
    CARDS = _official(globals()["CARDS"])
    posts = [_first()]
    assert tp.decide([SET], {"MZMC": CARDS}, posts, NOW) is None  # 初報と同じ日
    nxt = NOW + timedelta(days=1)
    assert tp.decide([SET], {"MZMC": CARDS}, posts, nxt.replace(hour=5)) is None  # JST 14時は時間帯の外
    item = tp.decide([SET], {"MZMC": CARDS}, posts, nxt)
    assert (item["kind"], item["card"]["page_id"]) == ("card", 2)
    posts.append({"kind": "card", "set_prefix": "MZMC", "page_id": 2, "created_at": nxt.isoformat()})
    assert tp.decide([SET], {"MZMC": CARDS}, posts, nxt + timedelta(hours=1)) is None  # 同じ日の2枚目は出さない
    item = tp.decide([SET], {"MZMC": CARDS}, posts, nxt + timedelta(days=1))
    assert item["card"]["page_id"] == 1


def test_続報は判明が増えて時間がたったときだけ():
    many = CARDS + [_card(10 + i, f"New {i}", 10 + i, seen="2026-10-30T00:00:00+00:00") for i in range(3)]
    posts = [_first(at="2026-10-29T02:00:00+00:00", count=2)]
    assert tp.decide([SET], {"MZMC": many}, posts, datetime(2026, 10, 29, 12, tzinfo=timezone.utc)) is None or \
        tp.decide([SET], {"MZMC": many}, posts, datetime(2026, 10, 29, 12, tzinfo=timezone.utc))["kind"] != "set_update"
    item = tp.decide([SET], {"MZMC": many}, posts, datetime(2026, 10, 30, 3, tzinfo=timezone.utc))
    assert item["kind"] == "set_update"
    assert {c["name_en"] for c in item["new_cards"]} == {"New 0", "New 1", "New 2"}


def test_連載で出したカードに英語テキストが出たら全文判明():
    official = [dict(CARDS[0], lore_kind="official", official_at="2026-11-02T00:00:00+00:00")] + CARDS[1:]
    posts = [_first(), {"kind": "card", "set_prefix": "MZMC", "page_id": 1, "created_at": "2026-10-30T11:00:00+00:00"}]
    item = tp.decide([SET], {"MZMC": official}, posts, datetime(2026, 11, 2, 3, tzinfo=timezone.utc))
    assert (item["kind"], item["card"]["page_id"]) == ("card_official", 1)


def test_本文は上限に収まり_まとめへ誘う一文とリンクを残す(monkeypatch):
    import x_poster
    from unreleased_x_post import weighted_length
    monkeypatch.setattr(x_poster, "SITE_URL", "https://greedy-lab.com")
    long = dict(CARDS[1], summary_short_ja="長い概要。" * 40, related_ja=["スロットマシーンAM－７", "リボルバー・ドラゴン"])
    text = tp.text_card(SET, long, CARDS)
    body = text.rsplit("\n", 1)[0]
    assert weighted_length(body) + 1 + 23 <= tp.MAX_WEIGHT
    assert "同じパックで判明した残り3枚も、まとめて紹介しています。" in text
    assert text.endswith("https://greedy-lab.com/tcg-preview/maze-of-machines?c=mzmc-en002#mzmc-en002")
    first = tp.text_set_first(SET, CARDS[:2])
    assert "Maze of Machines" in first and first.endswith("https://greedy-lab.com/tcg-preview/maze-of-machines")


def test_公式の段の初報は絵が入っていればイラストも判明と書く():
    two = [dict(CARDS[0], art_url="https://example.com/a.png"), dict(CARDS[1], art_url="https://example.com/b.png")]
    assert "KONAMI 公式サイトでイラストも判明しました。" in tp.text_set_first(SET, two)
    part = [two[0], dict(CARDS[1], art_url="")]
    assert "KONAMI 公式サイトで1枚のイラストも判明しました。" in tp.text_set_first(SET, part)
    none = [dict(c, art_url="") for c in CARDS[:2]]
    assert "イラスト" not in tp.text_set_first(SET, none)


def test_Yugipediaの仮置きの断り書きは効果の文として扱わない():
    import tcg_announce as ta
    raw = ("''This card's existence is mentioned in the product description of \"[[Ticking Blast Sphere]]\". "
           "Further details, including official name and description, to be added once the card is revealed.''")
    lore = ta.parse_lore(raw, "{{Unofficial lore|English}}\n")
    assert lore["kind"] == "none" and lore["text_en"] == ""
    # 本物の宣伝文はそのまま
    real = ta.parse_lore("A Trap that destroys monsters.", "{{Unofficial lore|English}}\n")
    assert real["kind"] == "blurb"


def test_無効なら何もしない(monkeypatch):
    monkeypatch.delenv(tp.ENABLED_ENV, raising=False)
    assert tp.run(object()).startswith("無効")


# ── ページと OG 画像 ───────────────────────

@pytest.fixture
def client(monkeypatch):
    import card_display
    import tcg_announce_view as view
    monkeypatch.setattr(card_display, "_supabase_client", object())
    cards = [dict(c, hidden=False, status="announced", updated_at="2026-10-29T10:00:00+00:00", page_title=c["name_en"],
                  effect_text_ja="", unofficial_name=False, lore_source_url="https://www.southernhobby.com/p/")
             for c in CARDS]
    monkeypatch.setattr(view, "_fetch", lambda: {"maze-of-machines": {"set": dict(SET, set_title="Maze of Machines"),
                                                                        "cards": view.order_cards(cards)}})
    view._cache.update(at=0.0, data=None)
    import app as app_module
    yield app_module.app.test_client()
    view._cache.update(at=0.0, data=None)


def test_まとめページは中身をHTMLに書き_カード指定でOG画像を変える(client):
    r = client.get("/tcg-preview/maze-of-machines")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Pendulum Machine of Misery" in html and "非公式の要約" in html and "名前だけ判明" in html
    assert 'id="mzmc-en002"' in html
    assert "/tcg-preview/og/maze-of-machines.png" in html
    html = client.get("/tcg-preview/maze-of-machines?c=mzmc-en002").get_data(as_text=True)
    assert "/tcg-preview/og/maze-of-machines/mzmc-en002.png" in html
    assert client.get("/tcg-preview/no-such-set").status_code == 404


def test_OG画像はPNGで返る(client):
    for path in ("/tcg-preview/og/maze-of-machines.png", "/tcg-preview/og/maze-of-machines/mzmc-en001.png"):
        r = client.get(path)
        assert r.status_code == 200 and r.data[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get("/tcg-preview/og/maze-of-machines/nope.png").status_code == 404


# ── 取り込みの判断（レビュー 10-29 の指摘を固定する） ──────────

def _fake_pages(monkeypatch, pages_by_title: dict, alias: dict | None = None):
    """ya.fetch_pages の偽物。pages_by_title: 名前 → (page_id, revid, text)。alias: 頼んだ名前 → 移動先の名前。"""
    def fake(titles=None, page_ids=None, aliases=None):
        out = {}
        for t in titles or []:
            if alias and t in alias:
                if aliases is not None:
                    aliases[t] = alias[t]
                t = alias[t]
            if t in pages_by_title:
                pid, rev, text = pages_by_title[t]
                out[pid] = {"title": t, "revid": rev, "text": text}
        return out
    monkeypatch.setattr(ya, "fetch_pages", fake)


LIST_TITLE = "Set Card Lists:Maze of Machines (TCG-EN)"


def _pages(rampant_text=RAMPANT, list_text=SET_LIST):
    return {LIST_TITLE: (900, 1, list_text), "Maze of Machines": (901, 1, SET_PAGE),
            "Rampant Barrel Dragon": (100, 5, rampant_text), "Space Megatron": (101, 5, MEGATRON)}


def _known(pid, **kw):
    base = {"page_id": pid, "set_prefix": "MZMC", "status": "announced", "page_revid": 5, "list_order": 4,
            "summary_status": "done", "summary_attempts": 1}
    return dict(base, **kw)


KNOWN_SET = {"MZMC": {"prefix": "MZMC", "slug": "maze-of-machines", "list_title": LIST_TITLE}}


def test_要約が済んでいて版が同じなら作り直さない_失敗は上限まで取り直す(monkeypatch):
    _fake_pages(monkeypatch, _pages())
    known = {100: _known(100), 101: _known(101, list_order=5, summary_status="skipped")}
    got = tw.collect([LIST_TITLE], KNOWN_SET, known, set())
    assert got["changed"] == [] and got["skipped_known"] == 2
    known[100] = _known(100, summary_status="failed", summary_attempts=1)
    assert [c[0] for c in tw.collect([LIST_TITLE], KNOWN_SET, known, set())["changed"]] == [100]
    known[100] = _known(100, summary_status="failed", summary_attempts=tw.MAX_ATTEMPTS)
    assert tw.collect([LIST_TITLE], KNOWN_SET, known, set())["changed"] == []


def test_取り直しの回数は英文が変わると数え直す():
    row = {"lore_kind": "blurb", "lore_en": "new text", "pendulum_lore_en": ""}
    old = {"summary_status": "failed", "summary_attempts": 3, "summary_source_seen": "blurb\nold text\n"}
    assert tw.needs_summary(old, row) is True
    same = dict(old, summary_source_seen=tw.summary_source(row))
    assert tw.needs_summary(same, row) is False
    done = {"summary_status": "done", "summarized_source": tw.summary_source(row)}
    assert tw.needs_summary(done, row) is False


def test_公式DBに載った予告カードは引退し_一覧から外れたカードは外す(monkeypatch):
    in_db = RAMPANT.replace("| database_id           =", "| database_id           = 23999")
    _fake_pages(monkeypatch, _pages(rampant_text=in_db))
    known = {100: _known(100), 101: _known(101, list_order=5), 555: _known(555)}
    got = tw.collect([LIST_TITLE], KNOWN_SET, known, set())
    assert got["retire"] == [100]
    assert got["drop"] == [555]
    # 英語名が公式DB由来の一覧（tcg_preview_cards）にあるときも引退
    _fake_pages(monkeypatch, _pages())
    assert tw.collect([LIST_TITLE], KNOWN_SET, {100: _known(100)}, {"Rampant Barrel Dragon"})["retire"] == [100]


def test_一覧の名前がページの移動先に転送されても拾う(monkeypatch):
    lst = SET_LIST.replace("Rampant Barrel Dragon;", "Barrel Dragon Prototype;")
    _fake_pages(monkeypatch, _pages(list_text=lst), alias={"Barrel Dragon Prototype": "Rampant Barrel Dragon"})
    got = tw.collect([LIST_TITLE], KNOWN_SET, {}, set())
    assert 100 in got["seen_ids"]["MZMC"]
    assert 100 in [c[0] for c in got["changed"]]


def test_slugは最初の1回だけ決め_重なれば接頭辞を付ける():
    s = {"prefix": "MZMC", "slug": "maze-of-machines", "name_en": "Maze of Machines (renamed)"}
    assert "slug" not in tw.set_payload(s, KNOWN_SET)
    other = {"ABCD": {"prefix": "ABCD", "slug": "maze-of-machines"}}
    assert tw.set_payload(s, other)["slug"] == "maze-of-machines-mzmc"
    assert tw.set_payload(dict(s, slug="set"), {})["slug"] == "set-mzmc"


# ── 投稿の実行（偽の Supabase） ─────────────────

class _Q:
    def __init__(self, db, table):
        self.db, self.table, self.op, self.payload, self.filters = db, table, "select", None, []

    def select(self, *_):
        return self

    def eq(self, k, v):
        self.filters.append((k, v))
        return self

    def order(self, *_a, **_k):
        return self

    def range(self, a, b):
        self._range = (a, b)
        return self

    def insert(self, row):
        self.op, self.payload = "insert", row
        return self

    def update(self, row):
        self.op, self.payload = "update", row
        return self

    def delete(self):
        self.op = "delete"
        return self

    def execute(self):
        rows = self.db.rows.setdefault(self.table, [])
        match = [r for r in rows if all(r.get(k) == v for k, v in self.filters)]
        if self.op == "select":
            a, b = getattr(self, "_range", (0, 10 ** 9))
            return type("R", (), {"data": match[a:b + 1]})()
        if self.op == "insert":
            row = dict(self.payload, id=len(rows) + 1, created_at="2026-10-30T11:00:00+00:00")
            rows.append(row)
            return type("R", (), {"data": [row]})()
        if self.op == "update":
            for r in match:
                r.update(self.payload)
            return type("R", (), {"data": match})()
        self.db.rows[self.table] = [r for r in rows if r not in match]
        return type("R", (), {"data": match})()


class _DB:
    def __init__(self, rows):
        self.rows = rows

    def table(self, name):
        return _Q(self, name)


def _db_for_first_post():
    s = dict(SET, hidden=False, status="active")
    cards = [dict(c, hidden=False, status="announced", set_prefix="MZMC") for c in CARDS]
    return _DB({"tcg_announce_sets": [s], "tcg_announce_cards": cards, "tcg_announce_posts": []})


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv(tp.ENABLED_ENV, "1")
    monkeypatch.delenv("X_POST_DRY_RUN", raising=False)
    monkeypatch.setattr(tp, "_notify", lambda msg: None)
    import unreleased_x_post
    monkeypatch.setattr(unreleased_x_post, "in_quiet_hours", lambda now=None: False)


def test_拒否された投稿は残して出し直さない_回数制限は出し直す(enabled):
    from x_poster import ERR_RATE_LIMITED, ERR_REJECTED
    db = _db_for_first_post()
    assert tp.run(db, now=NOW, post_fn=lambda t: (None, ERR_RATE_LIMITED)).startswith("見送り")
    assert db.rows["tcg_announce_posts"] == []
    assert tp.run(db, now=NOW, post_fn=lambda t: (None, ERR_REJECTED)).startswith("失敗")
    assert [p["status"] for p in db.rows["tcg_announce_posts"]] == ["failed"]
    # 次の回は同じ初報を選び直さない（後ろの投稿を止めない）
    assert tp.decide([SET], {"MZMC": CARDS}, db.rows["tcg_announce_posts"], NOW) is None


def test_成功したら投稿済みにして閲覧数の記録に入れる(enabled):
    db = _db_for_first_post()
    assert "tweet_id=123" in tp.run(db, now=NOW, post_fn=lambda t: ("123", None))
    assert db.rows["tcg_announce_posts"][0]["status"] == "posted"
    assert db.rows["tweet_log"][0]["content_type"] == "tcg_announce_set_first"


def test_カードごとの投稿は弾が重なっても全体で1日1枚():
    CARDS = _official(globals()["CARDS"])
    other = dict(SET, prefix="ABCD", slug="other", name_en="Other Set")
    other_cards = [dict(c, page_id=c["page_id"] + 100) for c in CARDS]
    # 2つ目の弾だけなら出る（下の None が、公式待ちではなく1日1枚の決まりによることを確かめる）
    assert tp.decide([other], {"ABCD": other_cards}, [dict(_first(), set_prefix="ABCD")],
                     NOW + timedelta(days=1, hours=1))["kind"] == "card"
    nxt = NOW + timedelta(days=1)
    posts = [_first(), dict(_first(), set_prefix="ABCD"),
             {"kind": "card", "set_prefix": "MZMC", "page_id": 2, "name_en": "Slot Machine 777",
              "created_at": nxt.isoformat()}]
    assert tp.decide([SET, other], {"MZMC": CARDS, "ABCD": other_cards}, posts, nxt + timedelta(hours=1)) is None


def test_同じ名前のカードはページ番号が変わっても二度出さない():
    nxt = NOW + timedelta(days=2)
    posts = [_first(), {"kind": "card", "set_prefix": "MZMC", "page_id": 99, "name_en": "Slot Machine 777",
                        "created_at": (NOW + timedelta(days=1)).isoformat()}]
    item = tp.decide([SET], {"MZMC": _official(CARDS)}, posts, nxt)
    assert item["card"]["name_en"] != "Slot Machine 777"


# ── 要約の形・OG 画像（A+B の形・2026-10-29） ───────────

def test_要約は新しい形も古い文の配列も読める():
    import tcg_announce_og as og
    new = {"summary_ja": {"intro": "共通", "effects": [{"label": "ALPHA", "text": "a"}, {"label": "", "text": ""}]}}
    assert og.summary_of(new) == {"intro": "共通", "effects": [{"label": "ALPHA", "text": "a"}]}
    assert og.summary_of({"summary_ja": ["x", "y"]})["effects"] == [{"label": "", "text": "x"}, {"label": "", "text": "y"}]


def test_種類の言葉と日付の書き方():
    import tcg_announce_og as og
    c = {"card_type": "融合・効果モンスター", "attribute": "闇", "race": "機械族", "level": 8, "atk": "3000", "def": "2200"}
    assert og.kind_label(c) == "闇属性・機械族・融合"
    assert og.stats_line(c) == "闇属性・機械族・融合　★8　3000 / 2200"
    assert og.kind_label({"card_type": "永続罠"}) == "永続罠"
    assert og.format_release_ja("2027-03-19") == "2027年3月19日"


def test_書体に無い字を見分ける():
    import tcg_announce_og as og
    f = og.jp(20)
    assert og._has_glyph(f, "あ")
    if os.path.isfile(og._JP_PATH):
        assert not og._has_glyph(f, "★")  # M PLUS 2 には ★ が無い（予備の書体で描く）


def _img(w, h, color):
    from PIL import Image
    return Image.new("RGB", (w, h), color)


def test_OG画像は材料があってもなくても描ける():
    import tcg_announce_og as og
    card = dict(CARDS[1], summary_ja={"intro": "相手ターンでも使える。", "effects": [
        {"label": "ALPHA", "text": "カード1枚を手札に加える。"}, {"label": "", "text": "相手のカードを破壊する。"}]})
    box = _img(455, 426, (255, 255, 255))
    box.paste((40, 40, 40), (60, 60, 400, 380))
    rel = ("リボルバー・ドラゴン", _img(256, 372, (120, 60, 30)))
    for png in (og.render_set_png(SET, CARDS, box=box, related=[rel] * 4), og.render_set_png(SET, CARDS),
                og.render_card_png(SET, card, ["A", "B", "C", "D"], box=box, related=rel),
                og.render_card_png(SET, card, [])):
        assert png[:8] == bytes([0x89]) + b"PNG" + bytes([13, 10, 26, 10])


def test_商品写真の白い背景を透明にして余白を切る():
    import tcg_announce_og as og
    box = _img(200, 100, (255, 255, 255))
    box.paste((30, 30, 30), (50, 20, 150, 80))
    out = og.cutout(box)
    # 境目を1pxぼかすので、切り抜きは中身より数px広い
    assert 100 <= out.width <= 108 and 60 <= out.height <= 68
    assert out.getpixel((out.width // 2, out.height // 2))[3] == 255


# ── テーマによる見出しの出し分け（2026-10-29） ─────────

def test_見出しはゆかり_既存テーマ_新テーマ_弾の名前の順():
    import tcg_announce_og as og
    base = {"related_ja": [], "theme_kind": "", "theme_en": "", "theme_ja": ""}
    assert og.headline(dict(base, related_ja=["リボルバー・ドラゴン"], theme_kind="new_theme", theme_en="Heavy Metal"),
                       "Maze of Machines") == "《リボルバー・ドラゴン》ゆかりの新カード"
    assert og.headline(dict(base, theme_kind="member", theme_en="Elemental HERO", theme_ja="Ｅ・ＨＥＲＯ"), "X") \
        == "【Ｅ・ＨＥＲＯ】の新規カード"
    assert og.headline(dict(base, theme_kind="support", theme_en="Elemental HERO", theme_ja="Ｅ・ＨＥＲＯ"), "X") \
        == "【Ｅ・ＨＥＲＯ】をサポートする新カード"
    assert og.headline(dict(base, theme_kind="member", theme_en="Gun Dragon"), "X") == "「Gun Dragon」の新規カード"
    assert og.headline(dict(base, theme_kind="new_theme", theme_en="Heavy Metal"), "X") == "新テーマ「Heavy Metal」の新カード"
    assert og.headline(base, "Maze of Machines") == "「Maze of Machines」の新カード"


def test_画像に出すカードはゆかり_テーマの代表の順():
    import tcg_announce_og as og
    assert og.picture_card({"related_ja": ["リボルバー・ドラゴン"], "theme_kind": "member", "theme_card_ja": "X"}) == "リボルバー・ドラゴン"
    assert og.picture_card({"related_ja": [], "theme_kind": "member", "theme_card_ja": "Ｅ・ＨＥＲＯ ネオス"}) == "Ｅ・ＨＥＲＯ ネオス"
    assert og.picture_card({"related_ja": [], "theme_kind": "new_theme", "theme_card_ja": ""}) == ""
    assert og.set_headline([{"theme_kind": "member", "theme_ja": "Ｅ・ＨＥＲＯ"}]) == "【Ｅ・ＨＥＲＯ】の新規カードが判明"


class _Corpus:
    card_names = {"Elemental HERO Neos": "Ｅ・ＨＥＲＯ ネオス", "Elemental HERO Avian": "Ｅ・ＨＥＲＯ フェザーマン",
                  "Heavy Metal Raiders": "ヘビーメタル・レイダーズ", "Barrel Dragon": "リボルバー・ドラゴン"}

    def _theme_from_card_names(self, q):
        return "Ｅ・ＨＥＲＯ" if q == "Elemental HERO" else None


def test_テーマの判定は既存カードの有無で新テーマと見分ける():
    c = _Corpus()
    got = tw.decide_theme({"archseries": ["Elemental HERO"], "supports_archetypes": []}, "Elemental HERO Clay Guardsman",
                          {"Elemental HERO": "Ｅ・ＨＥＲＯ"}, c)
    assert (got["theme_kind"], got["theme_ja"]) == ("member", "Ｅ・ＨＥＲＯ")
    assert set(got["members"]) == {"Ｅ・ＨＥＲＯ ネオス", "Ｅ・ＨＥＲＯ フェザーマン"}
    # サポートだけ
    got = tw.decide_theme({"archseries": [], "supports_archetypes": ["Elemental HERO"]}, "Some Spell", {}, c)
    assert (got["theme_kind"], got["theme_ja"]) == ("support", "Ｅ・ＨＥＲＯ")
    # 既存カードはあるが日本語のテーマ名が無い → 見出しは新テーマ、絵には一員の既存カード（Heavy Metal → 鋼鉄の襲撃者）
    got = tw.decide_theme({"archseries": ["Heavy Metal"], "supports_archetypes": []}, "Heavy Metal Bandit", {}, c)
    assert got["theme_kind"] == "new_theme" and got["members"] == ["ヘビーメタル・レイダーズ"]
    # 既存カードが無く、カード自身の名前にテーマ名がある → 新テーマ
    got = tw.decide_theme({"archseries": ["Brand New"], "supports_archetypes": []}, "Brand New Dragon", {}, c)
    assert got["theme_kind"] == "new_theme"
    # 系列（Gun Dragon）は既存カードの英語名にも自分の名前にも無い → 無し
    got = tw.decide_theme({"archseries": ["Gun Dragon"], "supports_archetypes": []}, "Rampant Barrel Dragon", {}, c)
    assert got["theme_kind"] == ""


def test_代表カードは検索の多いカード_記録が無ければ名前の短いカード():
    assert tw.pick_theme_card(None, ["Ｅ・ＨＥＲＯ フェザーマン", "Ｅ・ＨＥＲＯ ネオス"]) == "Ｅ・ＨＥＲＯ ネオス"
    db = _DB({"deck_search_logs": [{"card_name": "Ｅ・ＨＥＲＯ フェザーマン", "searched_at": "2026-10-28T00:00:00+00:00"}]})
    _Q.in_ = lambda self, k, v: self
    _Q.gte = lambda self, k, v: self
    _Q.limit = lambda self, n: self
    assert tw.pick_theme_card(db, ["Ｅ・ＨＥＲＯ フェザーマン", "Ｅ・ＨＥＲＯ ネオス"]) == "Ｅ・ＨＥＲＯ フェザーマン"


def test_テーマの欄とふりがなの読み取り():
    assert ya.list_param("* Elemental HERO\n* HERO") == ["Elemental HERO", "HERO"]
    assert ya.list_param("Heavy Metal") == ["Heavy Metal"]
    page = "{{Infobox archseries\n| ja_name = {{Ruby|Ｅ・ＨＥＲＯ|エレメンタルヒーロー}}\n}}"
    assert ya.parse_archetype_ja(page) == "Ｅ・ＨＥＲＯ"


# ── 投稿を有効にする前の直し（2026-10-29 本番のデータで見つけたもの） ─────────

def test_本文が長くても見出しとまとめへの一文を残す(monkeypatch):
    import x_poster
    from unreleased_x_post import weighted_length
    monkeypatch.setattr(x_poster, "SITE_URL", "https://greedy-lab.com")
    long = dict(CARDS[1], summary_short_ja="長い概要。" * 40, related_ja=["スロットマシーンAM－７"])
    text = tp.text_card(SET, long, CARDS)
    assert "《スロットマシーンAM－７》ゆかりの新カード" in text
    assert "同じパックで判明した残り3枚も、まとめて紹介しています。" in text
    assert weighted_length(text.rsplit("\n", 1)[0]) + 1 + 23 <= tp.MAX_WEIGHT
    assert "\n\n" not in text


def test_要約が入りきらないときは文の切れ目で切る():
    first = "闇属性・機械族がカードを破壊すると手札から特殊召喚できる。"
    short = first + "破壊されてもデッキから闇属性・機械族を呼べる。"
    assert tp._cut_short(short, 70) == first
    assert tp._cut_short(short, 400) == short
    # 1文目も入らないときだけ「…」で切る
    assert tp._cut_short(short, 30).endswith("…")


def test_連載は表紙のカードを表紙の並びで先に出す():
    cards = [_card(1, "Pendulum Machine of Misery", 1), _card(2, "Slot Machine 777", 2, related=["スロットマシーンAM－７"]),
             _card(5, "Rampant Barrel Dragon", 5)]
    covers = ["Rampant Barrel Dragon", "Pendulum Machine of Misery"]
    got = sorted(cards, key=lambda c: tp.card_order_key(c, covers))
    assert [c["name_en"] for c in got] == ["Rampant Barrel Dragon", "Pendulum Machine of Misery", "Slot Machine 777"]
    s = dict(SET, cover_cards=covers)
    item = tp.decide([s], {"MZMC": _official(cards)}, [_first()], NOW + timedelta(days=1))
    assert item["card"]["name_en"] == "Rampant Barrel Dragon"


def test_弾のページから表紙のカード():
    page = SET_PAGE.replace("* Rampant Barrel Dragon", "* Rampant Barrel Dragon\n* Pendulum Machine of Misery")
    assert ya.parse_set_page(page)["cover_cards"] == ["Rampant Barrel Dragon", "Pendulum Machine of Misery"]


def test_商品写真が無い弾の画像はカードを右に並べる():
    import tcg_announce_og as og
    rel = [("A", _img(256, 372, (120, 60, 30)))] * 4
    for r in (rel, rel[:1]):
        png = og.render_set_png(SET, CARDS, related=r)
        assert png[:4] == bytes([0x89]) + b"PNG"


def test_既にある弾は書き換えで送り_slugを外したupsertにしない():
    calls = []

    class T:
        def __init__(self, name):
            self.name = name

        def upsert(self, payload, on_conflict=None):
            calls.append(("upsert", self.name, payload))
            return self

        def update(self, payload):
            calls.append(("update", self.name, payload))
            return self

        def eq(self, *a):
            return self

        def execute(self):
            return type("R", (), {"data": []})()

    class SB:
        def table(self, name):
            return T(name)

    s = {"prefix": "MZMC", "slug": "maze-of-machines", "name_en": "Maze of Machines", "list_title": "x",
         "tcg_release_date": "2027-03-19"}
    collected = {"sets": [s], "changed": [], "seen_ids": {}, "retire": [], "drop": [], "skipped_known": 0}
    from datetime import date
    tw.process(collected, {}, sb=SB(), client=None, model="m", mirror=None, summarize_on=False, dry_run=False,
               today=date(2026, 10, 29), known_sets={"MZMC": {"prefix": "MZMC", "slug": "maze-of-machines"}})
    assert [(k, n) for k, n, _ in calls] == [("update", "tcg_announce_sets")]
    assert "slug" not in calls[0][2]
    calls.clear()
    tw.process(collected, {}, sb=SB(), client=None, model="m", mirror=None, summarize_on=False, dry_run=False,
               today=date(2026, 10, 29), known_sets={})
    assert [(k, n) for k, n, _ in calls] == [("upsert", "tcg_announce_sets")]
    assert calls[0][2]["slug"] == "maze-of-machines"


def test_投稿用の要約も文末を句点にそろえる(monkeypatch):
    import json
    from types import SimpleNamespace as NS
    monkeypatch.setattr(tw.tcg_translate, "usage_dict", lambda u: {})
    out = {"intro": "", "effects": [{"label": "", "text": "蘇生できる"}], "short": "毎ターン蘇生できる"}
    msg = NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(out))], usage=None)
    client = NS(messages=NS(create=lambda **kw: msg))
    corpus = NS(glossary_for=lambda t: {})
    card = {"lore": {"kind": "blurb", "text_en": "revive every turn"}, "pendulum_lore": {"text_en": ""}}
    got = tw.summarize(client, corpus, card, "X", model="m")
    assert got["short"] == "毎ターン蘇生できる。"


# ── 商品写真を卸から取る・写真が無い画像の出どころの一行（2026-10-29 ユーザー指摘） ─────────

@pytest.fixture(autouse=True)
def _no_distributor(monkeypatch):
    """テストでは卸の検索を呼ばない（呼ぶテストは自分で差し替える）。"""
    monkeypatch.setattr(ya, "distributor_box_url", lambda name: "")


def test_卸の検索結果から弾の箱の写真だけを選ぶ():
    products = [
        {"title": "YGO Organized Play (OP) Pack (200ct) Box: Maze of Machines", "image": "https://cdn/x/op.png"},
        {"title": "YGO Maze of Machines Blister Pack", "image": "https://cdn/x/blister.png"},
        {"title": "YGO Maze of Machinesque Booster Box", "image": "https://cdn/x/other.png"},
        {"title": "YGO Maze of Machines Booster Box (PRE-ORDER)", "image": "//cdn/x/MZMC-Display01-US.png?v=1"},
    ]
    assert ya.pick_distributor_box(products, "Maze of Machines") == "https://cdn/x/MZMC-Display01-US.png?v=1"
    assert ya.pick_distributor_box(products[:3], "Maze of Machines") == ""
    assert ya.pick_distributor_box(products, "") == ""


def test_Yugipediaに写真が無ければ卸の写真_取れなければ前回の写真(monkeypatch):
    _fake_pages(monkeypatch, _pages())
    monkeypatch.setattr(ya, "distributor_box_url", lambda name: "https://cdn/MZMC-Display01-US.png" if name == "Maze of Machines" else "")
    got = tw.collect([LIST_TITLE], KNOWN_SET, {}, set())
    assert got["sets"][0]["box_image_url"] == "https://cdn/MZMC-Display01-US.png"

    def boom(name):
        raise OSError("down")
    monkeypatch.setattr(ya, "distributor_box_url", boom)
    prev = {"MZMC": dict(KNOWN_SET["MZMC"], box_image_url="https://cdn/old.png")}
    assert tw.collect([LIST_TITLE], prev, {}, set())["sets"][0]["box_image_url"] == "https://cdn/old.png"
    # 予約ページが消えても、一度取れた写真を使い続ける
    monkeypatch.setattr(ya, "distributor_box_url", lambda name: "")
    assert tw.collect([LIST_TITLE], prev, {}, set())["sets"][0]["box_image_url"] == "https://cdn/old.png"


def test_写真が無い画像には出どころの一行():
    import tcg_announce_og as og
    cards = [dict(CARDS[0], lore_kind="blurb", lore_source_url="https://www.southernhobby.com/x/p1/"),
             dict(CARDS[1], lore_kind="blurb", lore_source_url="https://www.southernhobby.com/x/p2/")]
    assert og.source_line(cards) == "出どころ：販売店の商品案内（southernhobby.com）"
    assert og.source_line([dict(CARDS[0], lore_kind="official", lore_source_url="")]) == "出どころ：公開された英語版のカードテキスト"
    assert og.source_line([dict(CARDS[0], lore_kind="none", lore_source_url="")]) == ""
    rel = [("A", _img(256, 372, (120, 60, 30)))] * 4
    for kw in ({"related": rel}, {"related": []}, {"box": _img(400, 300, (200, 200, 200)), "related": rel}):
        assert og.render_set_png(SET, cards, **kw)[:4] == bytes([0x89]) + b"PNG"
    assert og.render_card_png(SET, cards[0], ["B"])[:4] == bytes([0x89]) + b"PNG"


def test_宣伝文の効果の名前は札にせず点で並べる(client, monkeypatch):
    import tcg_announce_view as view
    entry = view._fetch()["maze-of-machines"]
    entry["cards"][0]["summary_ja"] = {"intro": "", "effects": [{"label": "ALPHA", "text": "カードを手札に加える。"}]}
    monkeypatch.setattr(view, "_fetch", lambda: {"maze-of-machines": entry})
    view._cache.update(at=0.0, data=None)
    html = client.get("/tcg-preview/maze-of-machines").get_data(as_text=True)
    assert "<li>カードを手札に加える。</li>" in html and "ALPHA" not in html


# ── 公式の商品ページのイラスト（2026-10-30。紹介文と絵は同じページに一緒に出る） ─────────

OFFICIAL_URL = "https://www.yugioh-card.com/en/products/mzmc/"
SHOP_URL = "https://www.southernhobby.com/yu-gi-oh-maze-of-machines-booster-box/p84133/"
OFFICIAL_HTML = """
<table class="listtable"><tbody><tr><td><strong>RAMPANT BARREL DRAGON</strong></p>
<p>Our latest top-of-the-line Fusion Monster…</p>
<p align="center"><img loading="lazy" src="/en/wp-content/uploads/2026/10/mzmc_art01.png" alt="" /></p></td></tr></tbody></table>
<table class="listtable"><tbody><tr><td><strong>SLOT MACHINE 777</strong></p>
<p>Put Lady Luck on your side…</p></td></tr></tbody></table>
<table class="listtable"><tbody><tr><td><strong>HEAVY METAL REVENGE</strong>
<img src="/en/a.png" /><img src="/en/b.png" /></td></tr></tbody></table>
<p><strong><u>New Cards for Cubic</u></strong></p><p><img src="/en/wp-content/uploads/2026/10/mzmc_art07.png" /></p>
"""


def test_公式の商品ページの紹介の枠ごとに名前と絵を対応づける():
    arts = ya.parse_official_arts(OFFICIAL_HTML, OFFICIAL_URL)
    assert ya.art_for("Rampant Barrel Dragon", arts) == "https://www.yugioh-card.com/en/wp-content/uploads/2026/10/mzmc_art01.png"
    assert ya.art_for("Slot Machine 777", arts) == ""      # 絵の無い枠
    assert ya.art_for("Heavy Metal Revenge", arts) == ""   # 絵が2枚あってどれか決められない枠
    assert len(arts) == 1                                    # 枠の外のまとめた絵は使わない
    assert ya.is_official_page(OFFICIAL_URL) and not ya.is_official_page("https://www.southernhobby.com/p/")


def test_絵の無い予告カードに公式ページの絵を入れる(monkeypatch):
    shop = "https://www.southernhobby.com/p/"
    rows = {"tcg_announce_cards": [
        {"page_id": 1, "name_en": "Rampant Barrel Dragon", "set_prefix": "MZMC", "lore_source_url": OFFICIAL_URL,
         "status": "announced", "art_url": ""},
        {"page_id": 2, "name_en": "Slot Machine 777", "set_prefix": "MZMC", "lore_source_url": OFFICIAL_URL,
         "status": "announced", "art_url": ""},
        {"page_id": 3, "name_en": "Other", "set_prefix": "MZMC", "lore_source_url": shop, "status": "announced", "art_url": ""},
        {"page_id": 4, "name_en": "Done", "set_prefix": "MZMC", "lore_source_url": OFFICIAL_URL,
         "status": "announced", "art_url": "https://x/old.png"},
        # 販売店の文が先に載り、Yugipedia の出どころが販売店のままのカードも、同じ弾の公式ページで探す
        {"page_id": 5, "name_en": "Ticking Blast Rider", "set_prefix": "MZMC", "lore_source_url": shop,
         "status": "announced", "art_url": ""},
        # 別の弾のカードは、この弾の公式ページで探さない
        {"page_id": 6, "name_en": "Ticking Blast Rider", "set_prefix": "XXXX", "lore_source_url": shop,
         "status": "announced", "art_url": ""},
    ]}
    calls = []

    rider = ('<table class="listtable"><tbody><tr><td><strong>TICKING BLAST RIDER</strong>'
             '<img src="/en/wp-content/uploads/2026/10/mzmc_art05.png" /></td></tr></tbody></table>')

    def fake(url):
        calls.append(url)
        return ya.parse_official_arts(OFFICIAL_HTML + rider, url)
    monkeypatch.setattr(ya, "official_arts", fake)
    db = _DB(rows)
    assert tw.fill_art(db) == 2
    got = {r["page_id"]: r["art_url"] for r in rows["tcg_announce_cards"]}
    assert got[1].endswith("mzmc_art01.png") and got[2] == "" and got[3] == "" and got[4] == "https://x/old.png"
    assert got[5].endswith("mzmc_art05.png") and got[6] == ""
    assert calls == [OFFICIAL_URL]   # 同じページは1回だけ読む・販売店のページは読まない

    calls.clear()
    for r in rows["tcg_announce_cards"]:
        if r["set_prefix"] == "MZMC":
            r["art_url"] = r["art_url"] or "https://x/filled.png"
    assert tw.fill_art(db) == 0 and calls == []   # 絵の無いカードが残っていない弾のページは読まない
    for r in rows["tcg_announce_cards"]:
        if r["page_id"] == 1:
            r["art_url"] = ""

    def boom(url):
        raise OSError("down")
    monkeypatch.setattr(ya, "official_arts", boom)
    assert tw.fill_art(db) == 0   # 読めなければ次の回に取り直す（例外にしない）


def test_出どころが公式の商品ページなら販売店と書かない():
    import tcg_announce_og as og
    cards = [dict(CARDS[0], lore_kind="blurb", lore_source_url=OFFICIAL_URL)]
    assert og.source_line(cards) == "出どころ：KONAMI 公式の商品紹介"
    assert og.source_sites(cards) == []
    assert og.from_official_page(cards[0])


def test_公式の絵が入った画像を描ける():
    import tcg_announce_og as og
    art = _img(225, 225, (200, 40, 40))
    card = dict(CARDS[1], lore_kind="blurb", lore_source_url=OFFICIAL_URL)
    old = _img(256, 372, (120, 60, 30))
    plain = og.render_card_png(SET, card, ["B"], related=("スロットマシーンAM－７", old))
    with_art = og.render_card_png(SET, card, ["B"], related=("スロットマシーンAM－７", old), art=art)
    assert with_art[:4] == bytes([0x89]) + b"PNG" and with_art != plain
    assert og.render_card_png(SET, card, ["B"], art=art)[:4] == bytes([0x89]) + b"PNG"
    box = _img(400, 300, (200, 200, 200))
    for kw in ({"pairs": [(old, art), (old, art)]}, {"pairs": [(old, art)], "arts": [art] * 4}, {"arts": [art]}):
        assert og.render_set_png(SET, [card], box=box, **kw)[:4] == bytes([0x89]) + b"PNG"


def test_テーマの代表カードには矢印を付けない():
    import tcg_announce_og as og
    old = _img(256, 372, (120, 60, 30))
    theme = dict(CARDS[0], related_ja=[], theme_kind="new_theme", theme_en="Heavy Metal", theme_card_ja="鋼鉄の襲撃者")
    yukari = dict(theme, related_ja=["鋼鉄の襲撃者"])
    # 同じ絵・同じ名前でも、ゆかり（宣伝文が名指し）のときだけ矢印と下の書き方が変わる
    assert og.render_card_png(SET, theme, [], related=("鋼鉄の襲撃者", old)) != \
        og.render_card_png(SET, yukari, [], related=("鋼鉄の襲撃者", old))


def test_まとめページに公式の絵を出す(client, monkeypatch):
    import tcg_announce_view as view
    entry = view._fetch()["maze-of-machines"]
    entry["cards"][0]["art_url"] = "https://www.yugioh-card.com/en/wp-content/uploads/2026/10/mzmc_art02.png"
    monkeypatch.setattr(view, "_fetch", lambda: {"maze-of-machines": entry})
    view._cache.update(at=0.0, data=None)
    html = client.get("/tcg-preview/maze-of-machines").get_data(as_text=True)
    assert 'class="ta-proxy-art" src="https://www.yugioh-card.com/en/wp-content/uploads/2026/10/mzmc_art02.png"' in html
    assert "カードのイラストは KONAMI 公式の商品ページのものです" in html


def test_同じ名前の枠が2つあればどちらの絵も使わない():
    page = OFFICIAL_HTML + """<table><tr><td><strong>Rampant Barrel-Dragon</strong><img src="/en/z.png" /></td></tr></table>"""
    assert ya.art_for("Rampant Barrel Dragon", ya.parse_official_arts(page, OFFICIAL_URL)) == ""


def test_公式と販売店が混ざった弾は両方を書く():
    import tcg_announce_og as og
    cards = [dict(CARDS[0], lore_kind="blurb", lore_source_url=OFFICIAL_URL),
             dict(CARDS[1], lore_kind="blurb", lore_source_url="https://www.southernhobby.com/p/")]
    assert og.source_line(cards) == "出どころ：KONAMI 公式の商品紹介・販売店の商品案内（southernhobby.com）"


def test_弾の画像の絵と組の選び方(monkeypatch):
    import tcg_announce_routes as routes
    loaded = []

    def fake_image(url):
        loaded.append(url)
        return None if url and "broken" in url else _img(10, 10, (1, 2, 3))
    olds = {"リボルバー・ドラゴン": "u/rev", "振り子刃の拷問機械": "u/furi"}   # 画像のあるゆかりのカード
    monkeypatch.setattr(routes, "_image", fake_image)
    monkeypatch.setattr(routes, "_related_images",
                        lambda names, limit: [(n, _img(7, 10, (0, 0, 0)), olds[n]) for n in names if n in olds][:limit])
    cards = [
        dict(CARDS[0], name_en="Pendulum", list_order=1, art_url="a/pend", related_ja=["名前だけ", "振り子刃の拷問機械"]),
        dict(CARDS[1], name_en="Rampant", list_order=2, art_url="a/ramp", related_ja=["リボルバー・ドラゴン"]),
        dict(CARDS[1], name_en="Slot", list_order=3, art_url="a/slot", related_ja=["リボルバー・ドラゴン"]),
        dict(CARDS[0], name_en="Broken", list_order=4, art_url="a/broken", related_ja=[]),
        dict(CARDS[0], name_en="NoArt", list_order=5, art_url="", related_ja=[]),
        dict(CARDS[0], name_en="Sphere", list_order=6, art_url="a/sphere", related_ja=[]),
        dict(CARDS[0], name_en="Bandit", list_order=7, art_url="a/bandit", related_ja=[]),
        dict(CARDS[0], name_en="Late", list_order=8, art_url="a/late", related_ja=[]),
    ]
    arts, pairs, keys = routes.set_arts(dict(SET, cover_cards=["Rampant"]), cards)
    assert len(arts) == 4 and len(pairs) == 2
    # 表紙のカードが先・1件目のゆかりに画像が無くても次を試す・同じゆかりは2組に使わない・読めない絵は飛ばす
    assert keys == ["a/ramp", "u/rev", "a/ramp", "a/pend", "u/furi", "a/pend", "a/slot", "a/sphere"]
    assert "a/late" not in loaded and "" not in loaded   # そろったら残りは読まない


def test_弾の画像は並べ方ごとに描き分ける():
    import tcg_announce_og as og
    art, old, box = _img(225, 225, (200, 40, 40)), _img(256, 372, (40, 160, 60)), _img(400, 300, (200, 200, 200))
    card = dict(CARDS[1], lore_kind="blurb", lore_source_url=OFFICIAL_URL)
    got = {og.render_set_png(SET, [card], box=box, pairs=[(old, art), (old, art)], arts=[art] * 4),
           og.render_set_png(SET, [card], box=box, pairs=[(old, art)], arts=[art] * 4),
           og.render_set_png(SET, [card], box=box, related=[("A", old)] * 4)}
    assert len(got) == 3


def test_絵の有無と差し替えでキャッシュの鍵が変わる(monkeypatch):
    import tcg_announce_og as og
    seen = []
    monkeypatch.setattr(og, "_cached", lambda key, render: seen.append(key) or b"")
    card = dict(CARDS[1], art_url="a/1")
    og.card_png(SET, card, [], art=None)
    og.card_png(SET, card, [], art=_img(5, 5, (0, 0, 0)))
    og.card_png(SET, dict(card, art_url="a/2"), [], art=_img(5, 5, (0, 0, 0)))
    og.set_png(SET, [card], art_keys=["a/1"])
    og.set_png(SET, [card], art_keys=["a/2"])
    assert len(set(seen)) == 5


def test_出どころの書かれていない非公式の文は販売店と書かない(monkeypatch):
    import tcg_announce_og as og
    drawn = []
    real = og.draw_text
    monkeypatch.setattr(og, "draw_text", lambda d, xy, text, *a, **k: drawn.append(text) or real(d, xy, text, *a, **k))
    card = dict(CARDS[0], lore_kind="blurb", lore_source_url="", summary_status="done")
    og.render_card_png(SET, card, [])
    assert "非公式の情報をもとにした要約です" in drawn and not any("販売店" in t for t in drawn)
    assert og.source_sites([card]) == [] and og.source_line([card]) == ""
    drawn.clear()
    og.render_card_png(SET, dict(card, lore_source_url="https://www.southernhobby.com/p/"), [])
    assert any("販売店" in t for t in drawn)


def test_連載は公式の絵のあるカードを先に出す():
    covers = ["Rampant Barrel Dragon"]
    cards = [_card(1, "Rampant Barrel Dragon", 1), dict(_card(2, "Slot Machine 777", 2), art_url="https://x/a.png")]
    got = sorted(cards, key=lambda c: tp.card_order_key(c, covers))
    assert [c["name_en"] for c in got] == ["Slot Machine 777", "Rampant Barrel Dragon"]


# ── 公式を基本に、販売店の速報は例外の分岐（2026-10-30 ユーザー合意） ───────────

def test_弾の段の見分け方():
    assert not tp.is_official_phase(_leak(CARDS))
    assert not tp.is_official_phase(CARDS)   # 出どころの書かれていない非公式の文は公式に数えない
    assert tp.is_official_phase(_leak(CARDS[:1]) + _official(CARDS[1:2]))
    assert tp.is_official_phase([dict(CARDS[0], art_url="https://x/a.png")])
    assert tp.is_official_phase([dict(CARDS[0], lore_kind="official")])   # 英語の正式なテキスト


def test_速報の初報は取り込みから12時間以内だけ_過ぎたら公式を待つ():
    leak = _leak(CARDS)   # 取り込みは 2026-10-29 01:00 UTC
    item = tp.decide([SET], {"MZMC": leak}, [], datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc))
    assert (item["kind"], item["phase"]) == ("set_first", "leak")
    text = tp.build_text(item)
    assert "【海外先行・速報】" in text and "販売店の商品案内" in text and "公式の発表待ち" in text
    late = datetime(2026, 10, 29, 14, 0, tzinfo=timezone.utc)   # 13時間後
    assert tp.decide([SET], {"MZMC": leak}, [], late) is None
    # 待っている間に公式が出れば、公式の初報として出る
    item = tp.decide([SET], {"MZMC": _official(CARDS)}, [], late)
    assert (item["kind"], item["phase"]) == ("set_first", "official")
    assert "速報" not in tp.build_text(item)


def test_速報の古さは効果の文が出た時刻から数える():
    noon = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    # カードの行は名前だけで何週間も前からあっても、効果の文が新しければ速報を出す
    fresh = [dict(c, first_seen_at="2026-10-01T00:00:00+00:00", lore_at="2026-10-29T10:00:00+00:00") for c in _leak(CARDS)]
    item = tp.decide([SET], {"MZMC": fresh}, [], noon)
    assert (item["kind"], item["phase"]) == ("set_first", "leak")
    # 効果の文が古ければ出さない（行ができた時刻が新しくても）
    stale = [dict(c, first_seen_at="2026-10-29T11:00:00+00:00", lore_at="2026-10-28T20:00:00+00:00")
             for c in _leak(CARDS)]
    assert tp.decide([SET], {"MZMC": stale}, [], noon) is None
    # 時刻の分からない速報は出さない
    assert tp.decide([SET], {"MZMC": [dict(c, lore_at=None) for c in _leak(CARDS)]}, [], noon) is None
    # ちょうど12時間は出す
    assert tp.decide([SET], {"MZMC": _leak(CARDS)}, [], datetime(2026, 10, 29, 13, 0, tzinfo=timezone.utc))
    # 前から1枚だけ古い文があっても、残りが新しければ速報を出す（真ん中の値で測る）
    burst = [dict(c, page_id=50 + i, name_en=f"Leak {i}", lore_at="2026-10-29T10:00:00+00:00")
             for i, c in enumerate(_leak([CARDS[0]] * 3))]
    one_old = [dict(_leak(CARDS)[0], lore_at="2026-10-15T00:00:00+00:00")] + burst
    item = tp.decide([SET], {"MZMC": one_old}, [], noon)
    assert (item["kind"], item["phase"]) == ("set_first", "leak")
    # 古い速報にあとから1枚だけ新しいカードが足されても、出さない
    one_new = [dict(c, lore_at="2026-10-28T00:00:00+00:00") for c in burst] + [
        dict(_leak(CARDS)[0], lore_at="2026-10-29T11:00:00+00:00")]
    assert tp.decide([SET], {"MZMC": one_new}, [], noon) is None


def test_効果の文が初めて出た回だけ時刻を入れる():
    now = "2026-10-30T12:00:00+00:00"
    assert tw.lore_at_for(None, {"lore_kind": "blurb"}, now) == now
    assert tw.lore_at_for({"lore_kind": "none"}, {"lore_kind": "blurb"}, now) == now
    assert tw.lore_at_for(None, {"lore_kind": "none"}, now) is None
    assert tw.lore_at_for({"lore_kind": "blurb", "lore_at": "x"}, {"lore_kind": "official"}, now) is None
    # 前から効果の文があるのに時刻が空のカード（SQL の後・新しいコードの前に取り込んだ）は、行ができた時刻にする
    # （今の時刻が入ると古い速報が新しく見える）
    old = {"lore_kind": "blurb", "lore_at": None, "first_seen_at": "2026-10-01T00:00:00+00:00"}
    assert tw.lore_at_for(old, {"lore_kind": "blurb"}, now) == "2026-10-01T00:00:00+00:00"


def test_公式の続報は速報で初報を出した弾に絵が入ったら1回だけ():
    now = NOW + timedelta(hours=20)
    with_art = [dict(c, art_url=f"https://x/{c['page_id']}.png") for c in _official(CARDS)]
    item = tp.decide([SET], {"MZMC": with_art}, [_first(phase="leak")], now)
    assert item["kind"] == "set_official"
    text = tp.build_text(item)
    assert "公式サイトが公開" in text and "イラストが判明" in text and "など2枚" in text
    # 枚数は絵のあるカードだけ数える
    one_art = [with_art[0]] + _official(CARDS)[1:]
    assert "など1枚" in tp.build_text(tp.decide([SET], {"MZMC": one_art}, [_first(phase="leak")], now))
    done = [_first(phase="leak"), {"kind": "set_official", "set_prefix": "MZMC", "known_count": 2,
                                   "created_at": now.isoformat()}]
    item = tp.decide([SET], {"MZMC": with_art}, done, now)
    assert item is None or item["kind"] != "set_official"
    # 公式の情報で初報を出した弾には出さない
    item = tp.decide([SET], {"MZMC": with_art}, [_first(phase="official")], now)
    assert item is None or item["kind"] != "set_official"
    # 速報の初報が失敗・結果不明の弾には前振りが無いので出さない
    for st in ("failed", "unknown"):
        item = tp.decide([SET], {"MZMC": with_art}, [_first(phase="leak", status=st)], now)
        assert item is None or item["kind"] != "set_official"
    # 絵がまだ無いうちは出さない（出どころが公式でも・英語のテキストだけでも）
    assert tp.decide([SET], {"MZMC": _official(CARDS)}, [_first(phase="leak")], now) is None
    text_only = [dict(CARDS[0], lore_kind="official")] + _leak(CARDS[1:])
    assert tp.decide([SET], {"MZMC": text_only}, [_first(phase="leak")], now) is None
    assert tp.decide([SET], {"MZMC": _leak(CARDS)}, [_first(phase="leak")], now) is None


def test_速報の段のカードごとは公式の絵を待つ_3日たったら絵なしで出す():
    leak = _leak(CARDS)
    posts = [_first(phase="leak")]   # 初報は 2026-10-29 02:00 UTC
    assert tp.decide([SET], {"MZMC": leak}, posts, NOW + timedelta(days=1)) is None
    assert tp.decide([SET], {"MZMC": leak}, posts, NOW + timedelta(days=2)) is None
    # ちょうど3日（初報から72時間）で出し始める。JST 20時台に合わせる
    at3 = datetime(2026, 11, 1, 11, 0, tzinfo=timezone.utc)
    posts3 = [_first(phase="leak", at="2026-10-29T11:00:00+00:00")]
    assert tp.decide([SET], {"MZMC": leak}, posts3, at3 - timedelta(minutes=1)) is None
    item = tp.decide([SET], {"MZMC": leak}, posts3, at3)
    assert item["kind"] == "card"
    assert "のイラストと効果が判明" not in tp.build_text(item)
    # 公式の絵が入って公式の続報を出したら、3日たつ前でもカードごとを始める
    with_art = [dict(c, art_url=f"https://x/{c['page_id']}.png") for c in _official(CARDS)]
    done = posts + [{"kind": "set_official", "set_prefix": "MZMC", "known_count": 2,
                     "created_at": (NOW + timedelta(hours=10)).isoformat()}]
    item = tp.decide([SET], {"MZMC": with_art}, done, NOW + timedelta(days=1))
    assert item["kind"] == "card" and "のイラストと効果が判明" in tp.build_text(item)


def test_続報の基準には公式の続報の枚数も使う():
    with_art = [dict(c, art_url=f"https://x/{c['page_id']}.png") for c in _official(CARDS)]
    many = with_art + [_official([_card(10 + i, f"New {i}", 10 + i)])[0] for i in range(3)]
    t = NOW + timedelta(days=2)
    posts = [_first(phase="leak", count=2), {"kind": "set_official", "set_prefix": "MZMC", "known_count": 4,
                                             "created_at": (t - timedelta(hours=21)).isoformat()}]
    # 初報の2枚からは3枚増えたが、公式の続報の4枚からは1枚しか増えていない → 続報は出さない
    item = tp.decide([SET], {"MZMC": many}, posts, t)
    assert item is None or item["kind"] != "set_update"
    posts[1]["known_count"] = 2
    assert tp.decide([SET], {"MZMC": many}, posts, t)["kind"] == "set_update"


def test_公式の続報も二重に予約しない(enabled, monkeypatch):
    db = _db_for_first_post()
    for c in db.rows["tcg_announce_cards"]:
        c["lore_source_url"], c["art_url"] = OFFICIAL_URL, "https://x/a.png"
    db.rows["tcg_announce_posts"] = [dict(_first(phase="leak"), id=1)]
    real = _Q.execute

    def dup(self):
        if self.op == "insert" and self.payload.get("kind") == "set_official":
            raise Exception('duplicate key value violates unique constraint "uq_tcg_announce_posts_set_official" (23505)')
        return real(self)
    monkeypatch.setattr(_Q, "execute", dup)
    assert tp.run(db, now=NOW + timedelta(hours=20), post_fn=lambda t: ("1", None)).startswith("予約できず")


def test_公式の絵のあるカードは本文もイラストと効果が判明():
    card = dict(_official(CARDS)[1], art_url="https://x/a.png")
    assert f"{card['name_en']} のイラストと効果が判明" in tp.text_card(SET, card, CARDS)
    assert f"{CARDS[1]['name_en']} の効果が判明" in tp.text_card(SET, CARDS[1], CARDS)


def test_初報の段を投稿の行に残す(enabled):
    db = _db_for_first_post()
    for c in db.rows["tcg_announce_cards"]:
        c["lore_source_url"] = SHOP_URL
    assert "tweet_id=1" in tp.run(db, now=NOW, post_fn=lambda t: ("1", None))
    row = db.rows["tcg_announce_posts"][0]
    assert (row["kind"], row["phase"]) == ("set_first", "leak") and "速報" in row["text"]


def test_出どころの分からない速報は販売店と書かない():
    noon = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    item = tp.decide([SET], {"MZMC": CARDS}, [], noon)   # 出どころの書かれていない非公式の文だけ
    assert (item["kind"], item["phase"]) == ("set_first", "leak")
    text = tp.build_text(item)
    assert "非公式の情報で" in text and "販売店" not in text
