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
                                           "release_date": "2027-03-19"}
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
            "first_seen_at": seen}


CARDS = [_card(1, "Pendulum Machine of Misery", 1), _card(2, "Slot Machine 777", 2, related=["スロットマシーンAM－７"]),
         _card(3, "Bad Numbers", 3, warnings=["数値 800 が英文に無い"]), _card(4, "Name Only", 4, kind="none", status="skipped")]
NOW = datetime(2026, 10, 29, 11, 0, tzinfo=timezone.utc)  # JST 20:00


def _first(at="2026-10-29T02:00:00+00:00", count=2):
    return {"kind": "set_first", "set_prefix": "MZMC", "page_id": None, "known_count": count, "created_at": at}


def test_最初は初報_数えるのは点検を通った概要のあるカードだけ():
    item = tp.decide([SET], {"MZMC": CARDS}, [], NOW)
    assert item["kind"] == "set_first"
    assert [c["page_id"] for c in item["cards"]] == [1, 2]


def test_カードごとは初報の翌日から_時間帯の中で1日1枚_関連の分かるカードを先に():
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
    assert "他の3枚もまとめています" in text
    assert text.endswith("https://greedy-lab.com/tcg-preview/maze-of-machines?c=mzmc-en002#mzmc-en002")
    first = tp.text_set_first(SET, CARDS[:2])
    assert "Maze of Machines" in first and first.endswith("https://greedy-lab.com/tcg-preview/maze-of-machines")


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
    assert "Pendulum Machine of Misery" in html and "概要・非公式" in html and "名前のみ" in html
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
    other = dict(SET, prefix="ABCD", slug="other", name_en="Other Set")
    other_cards = [dict(c, page_id=c["page_id"] + 100) for c in CARDS]
    nxt = NOW + timedelta(days=1)
    posts = [_first(), dict(_first(), set_prefix="ABCD"),
             {"kind": "card", "set_prefix": "MZMC", "page_id": 2, "name_en": "Slot Machine 777",
              "created_at": nxt.isoformat()}]
    assert tp.decide([SET, other], {"MZMC": CARDS, "ABCD": other_cards}, posts, nxt + timedelta(hours=1)) is None


def test_同じ名前のカードはページ番号が変わっても二度出さない():
    nxt = NOW + timedelta(days=2)
    posts = [_first(), {"kind": "card", "set_prefix": "MZMC", "page_id": 99, "name_en": "Slot Machine 777",
                        "created_at": (NOW + timedelta(days=1)).isoformat()}]
    item = tp.decide([SET], {"MZMC": CARDS}, posts, nxt)
    assert item["card"]["name_en"] != "Slot Machine 777"
