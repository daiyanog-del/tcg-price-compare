"""却下した行の扱い（unreleased_rejected.py）と、4つの取込入口への組み込みを検証する。

再現ケース（2026-10-25）: 08-14 に旧URLから低確度で入って却下した「暗黒の太陽神－ラーの翼神竜」が、
10-15 の記事・10-22 の X 投稿の取込を「取込済み」として止めていた。
"""

import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

import unreleased_auto_approve as aa
import unreleased_rejected as rj
from fake_supabase import FakeSB, Query
from name_normalize import fuzzy_key

RA = "暗黒の太陽神－ラーの翼神竜"
OLD_URL = "https://yu-gi-oh.jp/news_detail.php?page=details&id=2609"
X_BASE = "https://x.com/YuGiOh_OCG_INFO/status/"


class _Query(Query):
    def execute(self):
        if self.op in ("upsert", "insert") and isinstance(self.payload, dict) and "id" not in self.payload \
                and self.name in ("unreleased_cards", "official_card_images"):
            self.payload = dict(self.payload, id=self.db.next_id())
        return super().execute()


class _SB(FakeSB):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._n = 1000

    def next_id(self):
        self._n += 1
        return self._n

    def table(self, name):
        return _Query(self, name)


def _rejected(cid=418, name=RA, source_url=OLD_URL, **kw):
    card = {"id": cid, "name": name, "product_name": "IMMORTAL PHOENIX", "status": "rejected",
            "source_url": source_url, "extracted_at": "2026-08-14T03:42:42+00:00", "hidden": False,
            "approved_at": None, "approved_via": None, "atk": None, "confidence": "low",
            "extraction_raw": {"model": "old"}}
    card.update(kw)
    return card


def _row(name=RA, source_url=X_BASE + "2113042647862399101", **kw):
    row = {"name": name, "product_name": "IMMORTAL PHOENIX", "card_type": "融合モンスター",
           "effect_text": "「ラーの翼神竜」＋「太陽神」モンスター×３", "level": 10, "def": "?",
           "confidence": "medium", "source_url": source_url, "source_domain": "x.com",
           "extraction_raw": {"source": "x_tweet", "card_image_url": "https://example.test/ra.jpg"}}
    row.update(kw)
    return row


@pytest.fixture
def no_context(monkeypatch):
    monkeypatch.setattr(aa, "load_active_names", lambda sb: [])
    monkeypatch.setattr(aa, "load_text_context", lambda sb: None)
    monkeypatch.setattr(aa, "compute_text_check", lambda *a, **k: {"status": "checked", "strong": 0})


@pytest.fixture
def notes(monkeypatch):
    sent = []
    monkeypatch.setattr(rj, "_default_notify", sent.append)
    return sent


# ──────────────────────────────────────────────
# 出どころの比較キー
# ──────────────────────────────────────────────

@pytest.mark.parametrize("a, b", [
    (X_BASE + "123", "https://twitter.com/YuGiOh_OCG_INFO/status/123?s=20"),
    (X_BASE + "123", "x.com/YuGiOh_OCG_INFO/status/123"),            # http なしで貼られた URL
    (X_BASE + "123", "https://fxtwitter.com/YuGiOh_OCG_INFO/status/123"),
    ("https://www.youtube.com/watch?v=abc&t=6448s", "https://youtube.com/watch?v=abc&t=10s"),
    ("https://youtu.be/abc", "https://www.youtube.com/watch?v=abc"),
    ("https://www.youtube.com/live/abc?si=x", "https://www.youtube.com/watch?v=abc"),
    ("https://www.youtube.com/shorts/abc", "https://youtu.be/abc?t=3"),
    (OLD_URL, "https://yu-gi-oh.jp/news/oid-2609/"),
    ("https://yu-gi-oh.jp/news/y1y2k8qfq9/", "https://yu-gi-oh.jp/news/y1y2k8qfq9"),
])
def test_source_key_same(a, b):
    assert rj.source_key(a) == rj.source_key(b) != ""


@pytest.mark.parametrize("a, b", [
    (X_BASE + "123", X_BASE + "124"),
    ("https://www.youtube.com/watch?v=abc", "https://www.youtube.com/watch?v=xyz"),
    (OLD_URL, "https://yu-gi-oh.jp/news/y1y2k8qfq9/"),  # 記事の移設はURLが変わる＝別の出どころ扱い
])
def test_source_key_different(a, b):
    assert rj.source_key(a) != rj.source_key(b)


@pytest.mark.parametrize("url", ["", "manual", None])
def test_source_key_empty(url):
    assert rj.source_key(url) == ""


# ──────────────────────────────────────────────
# 索引
# ──────────────────────────────────────────────

def _rev(idx, name=RA, url=X_BASE + "1", active=(), **kw):
    return idx.revivable(_row(name=name, source_url=url), url, active_keys=set(active), **kw)


def test_blocks_only_same_source():
    idx = rj.RejectedIndex([_rejected()])
    assert idx.blocks(RA, OLD_URL)
    assert idx.blocks("暗黒の太陽神-ラーの翼神竜", "https://yu-gi-oh.jp/news/oid-2609/")  # 表記ゆれ・旧新URL
    assert not idx.blocks(RA, X_BASE + "1")
    assert not idx.blocks("別のカード", OLD_URL)
    assert _rev(idx, url=OLD_URL) is None
    assert _rev(idx)["id"] == 418


def test_force_revives_even_same_source():
    idx = rj.RejectedIndex([_rejected()])
    assert _rev(idx, url=OLD_URL, force=True)["id"] == 418


def test_not_revived_when_same_name_is_active():
    """重複として却下した行（同名の公開中の行がある）は戻さない。force でも戻さない。"""
    idx = rj.RejectedIndex([_rejected()])
    assert _rev(idx, active={fuzzy_key(RA)}) is None
    assert _rev(idx, active={fuzzy_key(RA)}, force=True) is None


def test_prefers_exact_name_and_product():
    """同名の却下行が複数あるときは、名前と商品名が完全一致する行を戻す（一意制約に当たらない）。"""
    idx = rj.RejectedIndex([_rejected(cid=1, product_name="P1"), _rejected(cid=2, product_name="IMMORTAL PHOENIX")])
    assert _rev(idx)["id"] == 2
    assert _rev(rj.RejectedIndex([_rejected(cid=1, product_name="P1")]))["id"] == 1


def test_blocks_previous_sources_after_revival():
    # 一度復活してから再び却下された行は、最初の出どころでも止める（毎日の巡回で何度も戻さない）
    card = _rejected(source_url=X_BASE + "9",
                     extraction_raw={rj.REVIVED_KEY: {"previous_sources": [OLD_URL]}})
    idx = rj.RejectedIndex([card])
    assert idx.blocks(RA, OLD_URL) and idx.blocks(RA, X_BASE + "9")
    assert not idx.blocks(RA, X_BASE + "10")


def test_blocks_previous_sources_from_narrow_select():
    # load_rejected は extraction_raw を丸ごと読まず、復活の記録だけを revived として読む
    card = _rejected(source_url=X_BASE + "9", extraction_raw=None, revived={"previous_sources": [OLD_URL]})
    assert rj.RejectedIndex([card]).blocks(RA, OLD_URL)


def test_rejected_without_source_never_blocks():
    idx = rj.RejectedIndex([_rejected(source_url=None)])
    assert not idx.blocks(RA, X_BASE + "1")
    assert _rev(idx) is not None


def test_take_removes_name():
    idx = rj.RejectedIndex([_rejected()])
    idx.take(RA)
    assert not idx.has(RA) and _rev(idx) is None


def test_select_pages_in_id_order(monkeypatch):
    monkeypatch.setattr(rj, "_PAGE", 2)
    sb = _SB({"unreleased_cards": [_rejected(cid=i, name=f"カード{i}") for i in (5, 1, 4, 2, 3)]})
    idx = rj.load_rejected(sb)
    assert sorted(c["id"] for v in idx.cards.values() for c in v) == [1, 2, 3, 4, 5]


def test_load_rejected_and_active_keys_split_by_status():
    sb = _SB({"unreleased_cards": [_rejected(), {"id": 1, "name": "公開中", "status": "approved"},
                                   {"id": 2, "name": "承認待ち", "status": "pending"}]})
    assert rj.load_rejected(sb).has(RA)
    assert rj.load_active_keys(sb) == {fuzzy_key("公開中"), fuzzy_key("承認待ち")}


# ──────────────────────────────────────────────
# 復活
# ──────────────────────────────────────────────

def test_revive_rewrites_row_to_pending_and_hides_old_images(notes):
    card = _rejected(approved_at="2026-08-15", approved_via="single")
    sb = _SB({"unreleased_cards": [card],
              "official_card_images": [{"id": 5, "unreleased_card_id": 418, "deleted_at": None},
                                       {"id": 6, "unreleased_card_id": 999, "deleted_at": None}]})
    row = _row()

    got = rj.revive_rejected(sb, card, row)

    db = sb.tables["unreleased_cards"][0]
    assert got["id"] == 418 and db["status"] == "pending"
    assert db["confidence"] == "medium" and db["source_url"] == row["source_url"] and db["level"] == 10
    assert db["approved_at"] is None and db["approved_via"] is None and db["hidden"] is False
    raw = db["extraction_raw"]
    assert raw["card_image_url"] == "https://example.test/ra.jpg" and "model" not in raw
    assert raw[rj.REVIVED_KEY]["previous_sources"] == [OLD_URL]
    assert raw[rj.REVIVED_KEY]["previous_name"] == RA
    imgs = {i["id"]: i["deleted_at"] for i in sb.tables["official_card_images"]}
    assert imgs[5] is not None and imgs[6] is None  # このカードの古い画像だけ隠す
    assert len(notes) == 1 and "承認待ちに戻しました" in notes[0] and "id=418" in notes[0]


def test_revive_keeps_history_of_sources(notes):
    card = _rejected(source_url=X_BASE + "9", extraction_raw={rj.REVIVED_KEY: {"previous_sources": [OLD_URL]}})
    sb = _SB({"unreleased_cards": [card], "official_card_images": []})
    rj.revive_rejected(sb, card, _row(source_url=X_BASE + "10"))
    raw = sb.tables["unreleased_cards"][0]["extraction_raw"]
    assert raw[rj.REVIVED_KEY]["previous_sources"] == [OLD_URL, X_BASE + "9"]


def test_revive_returns_none_when_row_no_longer_rejected(notes):
    card = _rejected()
    sb = _SB({"unreleased_cards": [dict(card, status="pending")],
              "official_card_images": [{"id": 5, "unreleased_card_id": 418, "deleted_at": None}]})
    assert rj.revive_rejected(sb, card, _row()) is None
    assert notes == []
    assert sb.tables["official_card_images"][0]["deleted_at"] is None  # 生きている行の画像は隠さない


class _FailingSB(_SB):
    """指定の表への update で失敗する偽DB（一時的な DB 障害の代わり）。"""

    def __init__(self, *a, fail_table, **k):
        super().__init__(*a, **k)
        self.fail_table = fail_table

    def table(self, name):
        q = super().table(name)
        if name == self.fail_table:
            def update(payload):
                raise RuntimeError("DB 一時障害")
            q.update = update
        return q


def test_image_hide_failure_leaves_row_rejected_and_notifies(notes):
    card = _rejected()
    sb = _FailingSB({"unreleased_cards": [dict(card)],
                     "official_card_images": [{"id": 5, "unreleased_card_id": 418, "deleted_at": None}]},
                    fail_table="official_card_images")
    assert rj.revive_rejected(sb, card, _row()) is None
    # 行だけ戻って古い画像が見えたまま、にはならない
    assert sb.tables["unreleased_cards"][0]["status"] == "rejected"
    assert len(notes) == 1 and "戻せませんでした" in notes[0]


def test_db_error_raises_when_asked(notes):
    card = _rejected()
    sb = _FailingSB({"unreleased_cards": [dict(card)], "official_card_images": []}, fail_table="unreleased_cards")
    with pytest.raises(RuntimeError):
        rj.revive_rejected(sb, card, _row(), raise_errors=True)
    assert len(notes) == 1


def test_precheck_blocks_auto_approve_for_revived():
    import unreleased_approve
    card = {"id": 418, "source_url": X_BASE + "1", "extraction_raw": {rj.REVIVED_KEY: {"at": "x"}}}
    assert unreleased_approve.precheck_auto_approve(MagicMock(), card) == "revived_from_rejected"
    assert aa.reason_label("revived_from_rejected") != "revived_from_rejected"


# ──────────────────────────────────────────────
# X の定期取込
# ──────────────────────────────────────────────

@pytest.fixture
def xenv(monkeypatch, no_context, notes):
    import watch_x_unreleased as watcher
    monkeypatch.setattr(watcher, "_get_known_fuzzy_keys", lambda: set())
    ext = ModuleType("unreleased_extractor")
    ext.extract_cards_from_tweet = MagicMock()
    monkeypatch.setitem(sys.modules, "unreleased_extractor", ext)
    store = ModuleType("unreleased_image_store")
    store.ingest_x_card_image = MagicMock(return_value=(True, "ok"))
    monkeypatch.setitem(sys.modules, "unreleased_image_store", store)
    approve = MagicMock(return_value={})
    monkeypatch.setattr(watcher, "maybe_auto_approve", approve)
    return watcher, ext.extract_cards_from_tweet, store.ingest_x_card_image, approve


def _tweet(tid, name=RA):
    return {"id": str(tid), "text": f"◤{name}◢", "attachments": {"media_keys": ["m"]}}


def test_x_revives_rejected_from_other_source(xenv):
    """再現ケース: 却下行があっても、別の投稿から来たら承認待ちに戻す（新しい行は作らない）。"""
    watcher, extract, ingest, approve = xenv
    sb = _SB({"unreleased_cards": [_rejected()], "official_card_images": []})
    extract.return_value = [_row()]
    keys = rj.load_active_keys(sb)

    n = watcher.process_tweets([_tweet(2113042647862399101)], {"m": "https://example.test/x.jpg"}, sb, keys,
                               rejected=rj.load_rejected(sb))

    assert n == 1 and extract.call_count == 1
    assert len(sb.tables["unreleased_cards"]) == 1
    assert sb.tables["unreleased_cards"][0]["status"] == "pending"
    ingest.assert_called_once_with(sb, 418, "https://example.test/ra.jpg", X_BASE + "2113042647862399101")
    # 自動承認には渡すが（見送り理由を札に残すため）、precheck が止める
    assert approve.call_args.args[1][0]["extraction_raw"][rj.REVIVED_KEY]


def test_x_same_tweet_rejected_is_skipped_without_vision(xenv):
    watcher, extract, ingest, _ = xenv
    sb = _SB({"unreleased_cards": [_rejected(source_url=X_BASE + "5?s=20")], "official_card_images": []})
    n = watcher.process_tweets([_tweet(5)], {"m": "u"}, sb, rj.load_active_keys(sb),
                               rejected=rj.load_rejected(sb))
    assert n == 0
    extract.assert_not_called()
    assert sb.tables["unreleased_cards"][0]["status"] == "rejected"


def test_x_art_only_post_does_not_revive(xenv):
    watcher, extract, ingest, _ = xenv
    sb = _SB({"unreleased_cards": [_rejected()], "official_card_images": []})
    extract.return_value = [_row(effect_text="", card_type="", level=None, atk=None, **{"def": ""})]
    n = watcher.process_tweets([_tweet(7)], {"m": "u"}, sb, rj.load_active_keys(sb),
                               rejected=rj.load_rejected(sb))
    assert n == 0 and sb.tables["unreleased_cards"][0]["status"] == "rejected"
    ingest.assert_not_called()


def test_x_duplicate_rejected_not_revived_in_multi_card_tweet(xenv):
    """監査の再現ケース: 公開中のラーと重複として却下したラーがあり、新カードと一緒の投稿が来ても却下行は戻さない。"""
    watcher, extract, _, _ = xenv
    sb = _SB({"unreleased_cards": [{"id": 641, "name": RA, "status": "approved", "product_name": "IMMORTAL PHOENIX"},
                                   _rejected(cid=700, product_name="別の商品")],
              "official_card_images": []})
    extract.return_value = [_row(product_name="別の商品"), _row(name="新カード")]
    tweet = {"id": "9", "text": f"◤{RA}◢ ◤新カード◢", "attachments": {"media_keys": ["m"]}}

    n = watcher.process_tweets([tweet], {"m": "u"}, sb, rj.load_active_keys(sb), rejected=rj.load_rejected(sb))

    by_id = {r["id"]: r for r in sb.tables["unreleased_cards"]}
    assert by_id[700]["status"] == "rejected"
    assert n == 1 and [r["name"] for r in sb.tables["unreleased_cards"] if r["id"] > 1000] == ["新カード"]


def test_x_revive_db_error_raises_so_since_id_is_kept(xenv, monkeypatch):
    watcher, extract, _, _ = xenv
    sb = _FailingSB({"unreleased_cards": [_rejected()], "official_card_images": []}, fail_table="unreleased_cards")
    extract.return_value = [_row()]
    with pytest.raises(RuntimeError):
        watcher.process_tweets([_tweet(11)], {"m": "u"}, sb, set(), rejected=rj.load_rejected(sb))


def test_x_active_row_still_skips(xenv):
    watcher, extract, _, _ = xenv
    sb = _SB({"unreleased_cards": [_rejected(), {"id": 641, "name": RA, "status": "approved"}]})
    n = watcher.process_tweets([_tweet(8)], {"m": "u"}, sb, rj.load_active_keys(sb),
                               rejected=rj.load_rejected(sb))
    assert n == 0
    extract.assert_not_called()


# ──────────────────────────────────────────────
# 公式サイト巡回
# ──────────────────────────────────────────────

def test_watch_unreleased_blocks_same_page_and_revives_other(monkeypatch, no_context, notes):
    import unreleased_approve
    import unreleased_upload as up
    import watch_unreleased
    monkeypatch.setattr(up, "load_replaceable", lambda sb: up.ReplaceQueue())
    approve = MagicMock(return_value={})
    monkeypatch.setattr(unreleased_approve, "maybe_auto_approve", approve)
    page = "https://yu-gi-oh.jp/news/y1y2k8qfq9/"
    sb = _SB({"unreleased_cards": [_rejected(),
                                   _rejected(cid=420, name="同じページで却下", source_url=page)],
              "official_card_images": []})

    n = watch_unreleased._upsert_cards(sb, [_row(source_url=page), _row(name="同じページで却下", source_url=page),
                                            _row(name="新しいカード", source_url=page)])

    by_id = {r["id"]: r for r in sb.tables["unreleased_cards"]}
    assert n == 2
    assert by_id[418]["status"] == "pending" and by_id[418]["source_url"] == page
    assert by_id[420]["status"] == "rejected"
    assert [r["name"] for r in sb.tables["unreleased_cards"] if r["id"] > 1000] == ["新しいカード"]
    assert {r["id"] for r in approve.call_args.args[1]} == {418, 1001}


# ──────────────────────────────────────────────
# 画像アップロード
# ──────────────────────────────────────────────

def test_upload_revives_other_source_and_skips_same_source(no_context, notes):
    import unreleased_upload as up
    video = "https://www.youtube.com/watch?v=abc&t=10s"
    sb = _SB({"unreleased_cards": [_rejected(),
                                   _rejected(cid=420, name="同じ動画で却下",
                                             source_url="https://www.youtube.com/watch?v=abc&t=99s")],
              "official_card_images": []})
    store = MagicMock(return_value=(True, "取込成功"))
    rows = [_row(source_url=video, extraction_raw={"source": "image_upload", "source_image_url": "k"}),
            _row(name="同じ動画で却下", source_url=video,
                 extraction_raw={"source": "image_upload", "source_image_url": "k"})]

    result = up.register_rows(sb, rows, {"k": b"png"}, store_image=store, known_keys=set())

    assert [c["id"] for c in result["inserted"]] == [418]
    assert result["skipped"] == [{"name": "同じ動画で却下", "reason": "同じ出どころで却下済み"}]
    assert store.call_args.args[1] == 418
    assert len(sb.tables["unreleased_cards"]) == 2


def test_upload_art_only_does_not_revive(no_context, notes):
    import unreleased_upload as up
    sb = _SB({"unreleased_cards": [_rejected()], "official_card_images": []})
    row = _row(source_url="https://www.youtube.com/watch?v=abc", effect_text="", card_type="", level=None,
               atk=None, extraction_raw={"source": "image_upload", "source_image_url": "k"}, **{"def": ""})
    result = up.register_rows(sb, [row], {"k": b"png"}, store_image=MagicMock(), known_keys=set())
    assert result["inserted"] == [] and "カード本体が読めない" in result["skipped"][0]["reason"]
    assert sb.tables["unreleased_cards"][0]["status"] == "rejected"


def test_replace_with_official_keeps_revival_record(no_context):
    """画像アップロードで戻した行が公式版に差し替えられても、以前却下した出どころの記録は残る。"""
    import unreleased_upload as up
    card = {"id": 418, "name": RA, "status": "pending", "product_name": "IMMORTAL PHOENIX",
            "source_url": "https://www.youtube.com/watch?v=abc",
            "extraction_raw": {"source": "image_upload", "upload": {"filename": "a.png"},
                               rj.REVIVED_KEY: {"previous_sources": [OLD_URL]}}}
    sb = _SB({"unreleased_cards": [dict(card)], "official_card_images": []})

    def store(sb_, cid, row, page_url):
        sb_.table("official_card_images").insert({"unreleased_card_id": cid, "deleted_at": None,
                                                  "storage_path": f"unreleased/{cid}.jpg",
                                                  "public_url": f"https://cdn.example/{cid}.jpg"}).execute()
        return True, "ok"

    ok, _ = up.replace_with_official(sb, card, _row(), page_url=X_BASE + "1", store=store,
                                     notify=lambda m: None, crop=lambda *a: None)
    raw = sb.tables["unreleased_cards"][0]["extraction_raw"]
    assert ok and raw[rj.REVIVED_KEY]["previous_sources"] == [OLD_URL]


# ──────────────────────────────────────────────
# 管理画面の X 取込
# ──────────────────────────────────────────────

def _admin(monkeypatch, sb, tweet_id, extract_rows):
    import admin_unreleased as admin_module
    import app as app_module
    import requests
    import unreleased_upload as up
    monkeypatch.setattr(admin_module, "_supabase", sb, raising=False)
    monkeypatch.setattr(admin_module, "_ADMIN_KEY", "test-key", raising=False)
    monkeypatch.setenv("X_BEARER_TOKEN", "t")
    monkeypatch.setattr(up, "load_replaceable", lambda sb: up.ReplaceQueue())
    resp = MagicMock()
    resp.json.return_value = {"data": {"text": f"◤{RA}◢", "attachments": {"media_keys": ["m"]}},
                              "includes": {"media": [{"media_key": "m", "url": "https://example.test/x.jpg"}]}}
    monkeypatch.setattr(requests, "get", lambda *a, **k: resp)
    ext = ModuleType("unreleased_extractor")
    ext.extract_cards_from_tweet = MagicMock(return_value=extract_rows)
    monkeypatch.setitem(sys.modules, "unreleased_extractor", ext)
    store = ModuleType("unreleased_image_store")
    store.ingest_x_card_image = MagicMock(return_value=(True, "ok"))
    monkeypatch.setitem(sys.modules, "unreleased_image_store", store)
    import unreleased_approve
    monkeypatch.setattr(unreleased_approve, "maybe_auto_approve", lambda sb, rows: {})
    app_module.app.config.update(TESTING=True)
    client = app_module.app.test_client()
    return client.post("/api/admin/unreleased/fetch-x-post", headers={"X-Admin-Key": "test-key"},
                       json={"tweet_url": X_BASE + str(tweet_id)}), ext.extract_cards_from_tweet


def test_admin_fetch_x_post_revives_other_source(monkeypatch, no_context, notes):
    sb = _SB({"unreleased_cards": [_rejected()], "official_card_images": []})
    resp, extract = _admin(monkeypatch, sb, 2113042647862399101, [_row()])
    body = resp.get_json()
    assert resp.status_code == 201 and body["inserted"] == 1 and body["revived"] == 1
    assert sb.tables["unreleased_cards"][0]["status"] == "pending"


def test_admin_fetch_x_post_same_source_is_skipped(monkeypatch, no_context, notes):
    sb = _SB({"unreleased_cards": [_rejected(source_url=X_BASE + "77")], "official_card_images": []})
    resp, extract = _admin(monkeypatch, sb, 77, [_row(source_url=X_BASE + "77")])
    assert resp.get_json()["skipped"] is True
    extract.assert_not_called()
