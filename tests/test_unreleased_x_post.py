"""未発売カードのX自動投稿（unreleased_x_post.py）のテスト。"""
import re
from datetime import date

import pytest

import unreleased_x_post as xp
from tests.fake_supabase import FakeSB

TODAY = date(2026, 10, 15)


APPROVED_AT = "2026-10-15T00:00:00+00:00"  # 投稿条件は approved_at（approval_snapshot は計測用）
SNAP = {"approval_snapshot": {"at": "2026-10-15T00:00:00+00:00", "via": "single"}}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("UNRELEASED_X_POST_ENABLED", "1")
    monkeypatch.delenv("X_POST_DRY_RUN", raising=False)
    for k in xp._X_CRED_ENVS:
        monkeypatch.setenv(k, "x")
    monkeypatch.setattr(xp, "POST_INTERVAL_SEC", 0)           # テストで実際に待たない
    monkeypatch.setattr(xp, "_last_attempt_at", None)          # プロセス内の最終投稿時刻を初期化
    monkeypatch.setattr(xp, "_warned_not_configured", False)
    monkeypatch.setattr(xp, "_is_released_name", lambda name: False)  # 実カード名索引に依存させない
    monkeypatch.setattr(xp, "_notify", lambda msg: None)
    monkeypatch.setattr(xp, "_site_url", lambda: "https://example.test")
    monkeypatch.setattr(xp, "_verify_og_image", lambda card_id: (True, ""))   # OG 画像の生成確認（本番サイトへは行かない）


def _sb(card=None, image=True, posts=None):
    card = {"id": 1, "name": "テストカード", "product_name": "テストパック", "release_date": "2026-11-01",
            "status": "approved", "hidden": False, "approved_at": APPROVED_AT, "extraction_raw": dict(SNAP),
            **(card or {})}
    return FakeSB({
        "unreleased_cards": [card],
        "official_card_images": ([{"unreleased_card_id": card["id"], "public_url": "https://img.test/1.jpg",
                                   "hidden": False, "deleted_at": None}] if image else []),
        "unreleased_x_posts": posts or [],
    })


class Poster:
    def __init__(self, result="111"):
        self.result, self.calls = result, []

    def __call__(self, text, *args, **kwargs):
        self.calls.append((text, list(args)))
        self.kwargs = kwargs
        return self.result


# ── 本文 ──

def test_text_full():
    t = xp.build_tweet_text("テストカード", "テストパック", "2026-11-01", "https://x.com/YuGiOh_OCG_INFO/status/123",
                            site_url="https://example.test")
    assert t.split("\n") == [
        "「テストカード」を入れた展開、シミュレーターで回せます。",
        "テストパック（11月1日発売）",
        "情報元：https://x.com/YuGiOh_OCG_INFO/status/123",
        "https://example.test/solitaire?newcards=1&focus=%E3%83%86%E3%82%B9%E3%83%88%E3%82%AB%E3%83%BC%E3%83%89",
        "#遊戯王 #OCG"]
    assert "▼ 登録なし" not in t


def test_text_tcgym_link_is_last_url():
    # X のリンクカードは最後の URL で作られるとされるので、TCGYM のリンクを情報元より後ろに置く
    t = xp.build_tweet_text("A", "P", "2026-11-01", "https://yu-gi-oh.jp/news/abc/", site_url="https://example.test")
    urls = re.findall(r"https?://\S+", t)
    assert urls == ["https://yu-gi-oh.jp/news/abc/", "https://example.test/solitaire?newcards=1&focus=A"]


@pytest.mark.parametrize("src", ["", "manual", "  ", None, "ftp://x", "javascript:alert(1)"])
def test_text_source_line_omitted_unless_http_url(src):
    t = xp.build_tweet_text("A", "P", "2026-11-01", src, site_url="https://example.test")
    assert "情報元" not in t
    assert t.split("\n")[2].startswith("https://example.test/solitaire")


def test_text_without_date_and_product():
    t = xp.build_tweet_text("A", "", None, site_url="https://example.test")
    # 商品名・発売日が両方無いときは行ごと出さない
    assert t.split("\n") == [
        "「A」を入れた展開、シミュレーターで回せます。",
        "https://example.test/solitaire?newcards=1&focus=A", "#遊戯王 #OCG"]


def test_text_product_only_and_date_only():
    only_product = xp.build_tweet_text("A", "P", None, site_url="https://example.test").split("\n")
    assert only_product[1] == "P"
    only_date = xp.build_tweet_text("A", "", "2026-11-01", site_url="https://example.test").split("\n")
    assert only_date[1] == "11月1日発売"


def test_text_invalid_date_omitted():
    t = xp.build_tweet_text("A", "P", "bad", site_url="https://example.test")
    assert "発売" not in t
    assert t.split("\n")[1] == "P"


def test_text_url_encodes_reserved_characters():
    # & # ? / % スペース を含む名前でも focus が1つのクエリ値に収まる
    name = "A&B#C?D/E%F G"
    t = xp.build_tweet_text(name, "", None, site_url="https://example.test")
    url = t.split("\n")[-2]
    assert url == "https://example.test/solitaire?newcards=1&focus=A%26B%23C%3FD%2FE%25F%20G"
    from urllib.parse import urlsplit, parse_qs
    assert parse_qs(urlsplit(url).query) == {"newcards": ["1"], "focus": [name]}


def test_text_truncated_within_limit():
    name = "あ" * 60
    product = "商品名" * 80
    t = xp.build_tweet_text(name, product, "2026-11-01", "https://x.com/YuGiOh_OCG_INFO/status/123456789012345678",
                            site_url="https://example.test")
    assert xp.weighted_length(t) <= xp.TWEET_WEIGHT_LIMIT   # URL 2本（各23）を含めて収まる
    assert "…" in t
    # 商品名を先に詰めるので、カード名は残る
    assert name in t


def test_text_name_truncated_when_still_over():
    name = "あ" * 300
    t = xp.build_tweet_text(name, "P", None, site_url="https://example.test")
    assert xp.weighted_length(t) <= xp.TWEET_WEIGHT_LIMIT
    line = t.split("\n")[0]
    assert name not in line and "…" in line
    # URL は詰めず完全なカード名で作る
    from urllib.parse import quote
    assert t.split("\n")[-2].endswith("focus=" + quote(name, safe=""))


def test_weighted_length_url_and_cjk():
    assert xp.weighted_length("abc") == 3
    assert xp.weighted_length("あい") == 4
    assert xp.weighted_length("x https://example.test/very/long/path/" + "a" * 100) == 2 + xp.URL_WEIGHT


# ── 条件判定 ──

@pytest.mark.parametrize("card,img,ok,reason", [
    ({"status": "approved", "hidden": False, "release_date": None}, True, True, ""),
    ({"status": "approved", "hidden": False, "release_date": "2026-10-15"}, True, True, ""),
    ({"status": "approved", "hidden": False, "release_date": "2026-10-14"}, True, False, "already_released"),
    ({"status": "pending", "hidden": False, "release_date": None}, True, False, "status_not_approved"),
    ({"status": "linked", "hidden": False, "release_date": None}, True, False, "status_not_approved"),
    ({"status": "approved", "hidden": True, "release_date": None}, True, False, "hidden"),
    ({"status": "approved", "hidden": False, "release_date": None}, False, False, "no_visible_image"),
])
def test_is_postable(card, img, ok, reason):
    card = {"approved_at": APPROVED_AT, **card}
    assert xp.is_postable(card, img, TODAY) == (ok, reason)


# ── 投稿・予約 ──

def test_post_success_records_posted():
    sb, poster = _sb(), Poster("999")
    assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "posted"
    assert poster.kwargs == {}  # 画像は添付しない（require_media も付けない）
    row = sb.tables["unreleased_x_posts"][0]
    assert row["status"] == "posted" and row["tweet_id"] == "999"
    assert len(poster.calls) == 1 and poster.calls[0][1] == []  # 画像パスは渡さない


def test_og_check_failure_skips_post_and_marks_failed(monkeypatch):
    # OG 画像の生成が確認できなければ投稿せず failed（回収で再試行）
    monkeypatch.setattr(xp, "_verify_og_image", lambda card_id: (False, "OG画像が HTTP 500"))
    sb, poster = _sb(), Poster("999")
    assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "failed"
    row = sb.tables["unreleased_x_posts"][0]
    assert poster.calls == [] and row["status"] == "failed" and row["attempts"] == 1 and "HTTP 500" in row["last_error"]


def test_og_check_exception_marks_failed(monkeypatch):
    def boom(card_id):
        raise RuntimeError("net")
    monkeypatch.setattr(xp, "_verify_og_image", boom)
    sb, poster = _sb(), Poster("999")
    assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "failed"
    assert poster.calls == []


def _fake_resp(status=200, kind="card", ctype="image/png"):
    class R:
        status_code = status
        headers = {"Content-Type": ctype, **({"X-OG-Kind": kind} if kind else {})}
    return R()


@pytest.mark.parametrize("resp,ok", [
    (_fake_resp(), True),
    (_fake_resp(status=500), False),
    (_fake_resp(kind=None), False),          # 汎用画像への退避
    (_fake_resp(ctype="text/html"), False),
])
def test_verify_og_image_checks_response(monkeypatch, resp, ok):
    import requests
    monkeypatch.undo()   # autouse の差し替えを戻して本物を呼ぶ
    monkeypatch.setattr(xp, "_site_url", lambda: "https://example.test")
    seen = []
    monkeypatch.setattr(requests, "get", lambda url, **kw: (seen.append(url), resp)[1])
    assert xp._verify_og_image(7)[0] is ok
    assert seen == ["https://example.test/solitaire/og/card/7.png"]


def test_double_post_prevented():
    sb, poster = _sb(), Poster()
    assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "posted"
    assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "already"
    assert len(poster.calls) == 1


@pytest.mark.parametrize("status", ["posting", "posted", "skipped_legacy"])
def test_existing_row_blocks(status):
    sb, poster = _sb(posts=[{"card_id": 1, "status": status, "attempts": 0}]), Poster()
    assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "already"
    assert poster.calls == []


def test_none_from_post_tweet_is_failure():
    sb = _sb()
    assert xp.post_card(sb, 1, post_fn=Poster(None), today=TODAY) == "failed"
    row = sb.tables["unreleased_x_posts"][0]
    assert row["status"] == "failed" and row["attempts"] == 1 and "None を返した" in row["last_error"]


def test_failed_retried_until_limit():
    sb = _sb()
    for expected in (1, 2, 3):
        assert xp.post_card(sb, 1, post_fn=Poster(None), today=TODAY) == "failed"
        assert sb.tables["unreleased_x_posts"][0]["attempts"] == expected
    poster = Poster()
    assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "already"  # 上限到達で再試行しない
    assert poster.calls == []


def test_failed_then_success():
    sb = _sb()
    xp.post_card(sb, 1, post_fn=Poster(None), today=TODAY)
    assert xp.post_card(sb, 1, post_fn=Poster("5"), today=TODAY) == "posted"
    assert sb.tables["unreleased_x_posts"][0]["status"] == "posted"


def test_exception_in_post_fn_is_unknown():
    """post_fn が例外を投げた場合は送信済みかどうか分からない＝結果不明（自動再試行しない）"""
    def boom(text, *args, **kwargs):
        raise RuntimeError("API down")
    sb = _sb()
    assert xp.post_card(sb, 1, post_fn=boom, today=TODAY) == "unknown"
    row = sb.tables["unreleased_x_posts"][0]
    assert row["status"] == "unknown" and "API down" in row["last_error"]


def test_dry_run_leaves_no_row(monkeypatch):
    monkeypatch.setenv("X_POST_DRY_RUN", "1")
    sb, poster = _sb(), Poster()
    assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "dry_run"
    assert sb.tables["unreleased_x_posts"] == [] and poster.calls == []


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("UNRELEASED_X_POST_ENABLED")
    sb, poster = _sb(), Poster()
    assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "disabled"
    assert sb.tables["unreleased_x_posts"] == [] and poster.calls == []


def test_no_image_not_posted():
    sb, poster = _sb(image=False), Poster()
    assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "not_postable"
    assert sb.tables["unreleased_x_posts"] == []


def test_reserve_race_only_one_wins():
    sb = _sb()
    assert xp.reserve(sb, 1) == "reserved"
    assert xp.reserve(sb, 1) == "taken"


def test_reserve_takeover_only_failed_under_limit():
    sb = _sb(posts=[{"card_id": 1, "status": "failed", "attempts": 1}])
    assert xp.reserve(sb, 1) == "reserved"
    assert sb.tables["unreleased_x_posts"][0]["status"] == "posting"
    assert xp.reserve(sb, 1) == "taken"


# ── 連続投稿・回収 ──

def _add_card(sb, cid, *, image=True, release_date=None, approved=True):
    sb.tables["unreleased_cards"].append({"id": cid, "name": f"C{cid}", "product_name": "", "release_date": release_date,
                                          "status": "approved", "hidden": False,
                                          "approved_at": APPROVED_AT if approved else None,
                                          "extraction_raw": dict(SNAP)})
    if image:
        sb.tables["official_card_images"].append({"unreleased_card_id": cid, "public_url": "u", "hidden": False,
                                                  "deleted_at": None})


def test_sequential_waits_only_after_attempt(monkeypatch):
    monkeypatch.setattr(xp, "_clock", lambda: 1000.0)  # 時間が進まない時計 → 2枚目の待ちは interval ちょうど
    sb = _sb()
    _add_card(sb, 2)
    _add_card(sb, 3, image=False)  # 画像なし＝投稿を試みないので待たない
    sleeps = []
    xp.post_cards_sequential(sb, [1, 2, 3], interval=20, post_fn=Poster(), sleep=sleeps.append)
    assert sleeps == [20]


def test_backfill_candidates():
    sb = _sb()  # id=1: 画像あり・行なし → 候補
    _add_card(sb, 2, image=False)                    # 画像なし
    _add_card(sb, 3)                                 # legacy
    _add_card(sb, 4)                                 # failed attempts=1 → 候補
    _add_card(sb, 5)                                 # failed attempts=3
    _add_card(sb, 6, release_date="2026-10-01")      # 発売済み（日付絞りで除外）
    _add_card(sb, 7, approved=False)                 # approved_at なし（導入前の承認）→ 除外
    sb.tables["unreleased_x_posts"] += [
        {"card_id": 3, "status": "skipped_legacy", "attempts": 0},
        {"card_id": 4, "status": "failed", "attempts": 1},
        {"card_id": 5, "status": "failed", "attempts": 3},
    ]
    assert xp.find_backfill_candidates(sb, TODAY) == [1, 4]


def test_run_backfill_posts_candidates(monkeypatch):
    sb = _sb()
    poster = Poster()
    assert xp.run_backfill(sb, post_fn=poster, sleep=lambda s: None) == {"posted": 1}
    assert len(poster.calls) == 1
