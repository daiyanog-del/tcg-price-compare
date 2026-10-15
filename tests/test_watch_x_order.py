"""X監視の処理順（古い順）と、カード本体が読めない投稿の保留を検証する。"""

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

import watch_x_unreleased as watcher
from name_normalize import fuzzy_key

BASE = f"https://x.com/{watcher.ACCOUNT_USERNAME}/status/"


def _body(name, img="body"):
    return {"name": name, "product_name": "商品", "card_type": "効果モンスター",
            "effect_text": "効果あり", "atk": 1500, "level": 4,
            "extraction_raw": {"card_image_url": f"https://example.test/{img}.jpg"}}


def _art(name, img="art"):
    return {"name": name, "product_name": "商品", "card_type": "", "effect_text": "",
            "pendulum_effect": "", "atk": None, "level": None, "rank": None, "link_val": None,
            "extraction_raw": {"card_image_url": f"https://example.test/{img}.jpg"}}


def _tweet(tid, text):
    return {"id": str(tid), "text": text, "attachments": {"media_keys": ["m"]}}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(watcher, "_get_known_fuzzy_keys", lambda: set())
    ext = ModuleType("unreleased_extractor")
    ext.extract_cards_from_tweet = MagicMock()
    monkeypatch.setitem(sys.modules, "unreleased_extractor", ext)
    store = ModuleType("unreleased_image_store")
    store.ingest_x_card_image = MagicMock(return_value=(True, "ok"))
    monkeypatch.setitem(sys.modules, "unreleased_image_store", store)
    sb = MagicMock()
    inserted = []
    counter = iter(range(1, 100))

    def execute():
        row = dict(sb.table.return_value.upsert.call_args.args[0])
        row["id"] = next(counter)
        inserted.append(row)
        return SimpleNamespace(data=[row])

    sb.table.return_value.upsert.return_value.execute.side_effect = execute
    # URL → 抽出結果 の対応で返す
    results = {}
    ext.extract_cards_from_tweet.side_effect = lambda text, urls, url: results[url]
    return SimpleNamespace(sb=sb, inserted=inserted, results=results, ingest=store.ingest_x_card_image,
                           extract=ext.extract_cards_from_tweet, media={"m": "https://example.test/x.jpg"})


def _run(env, tweets):
    keys = set()
    n = watcher.process_tweets(tweets, env.media, env.sb, keys)
    return n, keys


def test_processed_in_ascending_id_order(env):
    # 桁数が違うidでも数値順（文字列比較なら "9" > "10"）
    tweets = [_tweet(10, "◤B◢"), _tweet(9, "◤A◢")]
    env.results[BASE + "9"] = [_body("A")]
    env.results[BASE + "10"] = [_body("B")]
    _run(env, tweets)
    assert [c.args[2] for c in env.extract.call_args_list] == [BASE + "9", BASE + "10"]


def test_newer_art_only_old_body_inserts_body(env):
    """再現ケース: 新しい投稿=イラストのみ、古い投稿=本体あり。"""
    tweets = [_tweet(200, "◤竜◢"), _tweet(100, "◤竜◢")]
    env.results[BASE + "100"] = [_body("竜", "body")]
    n, keys = _run(env, tweets)
    assert n == 1
    assert env.inserted[0]["effect_text"] == "効果あり"
    assert env.extract.call_count == 1  # 新しい方は取込済みでスキップ
    env.ingest.assert_called_once_with(env.sb, 1, "https://example.test/body.jpg", BASE + "100")
    assert keys == {fuzzy_key("竜")}


def test_art_first_then_body_adopts_body(env):
    tweets = [_tweet(100, "◤竜◢"), _tweet(200, "◤竜◢")]
    env.results[BASE + "100"] = [_art("竜")]
    env.results[BASE + "200"] = [_body("竜", "body")]
    n, _ = _run(env, tweets)
    assert n == 1
    assert len(env.inserted) == 1 and env.inserted[0]["effect_text"] == "効果あり"
    env.ingest.assert_called_once_with(env.sb, 1, "https://example.test/body.jpg", BASE + "200")


def test_art_only_inserted_at_end_when_no_body(env):
    tweets = [_tweet(100, "◤竜◢"), _tweet(200, "◤竜◢")]
    env.results[BASE + "100"] = [_art("竜", "art1")]
    env.results[BASE + "200"] = [_art("竜", "art2")]
    n, keys = _run(env, tweets)
    assert n == 1
    assert len(env.inserted) == 1
    # 最初の1件（100）の行と画像出所が一致
    env.ingest.assert_called_once_with(env.sb, 1, "https://example.test/art1.jpg", BASE + "100")
    assert keys == {fuzzy_key("竜")}


def test_art_only_single_tweet_still_inserted(env):
    env.results[BASE + "100"] = [_art("竜")]
    n, _ = _run(env, [_tweet(100, "◤竜◢")])
    assert n == 1


@pytest.mark.parametrize("row", [
    {"name": "通常", "effect_text": "フレーバー", "atk": None},            # 通常モンスターのフレーバーのみ
    {"name": "通常", "effect_text": "", "atk": 0, "level": 4},             # ATK 0 は本体あり
    {"name": "魔法", "effect_text": "このカードは…", "card_type": "通常魔法"},
    {"name": "リンク", "effect_text": "", "link_val": 2},
    {"name": "ペン", "effect_text": "", "pendulum_effect": "P効果"},
    {"name": "エクシーズ", "effect_text": "", "rank": 4},
])
def test_body_rows_not_held(row):
    assert watcher._lacks_card_body(row) is False


def test_art_row_is_held():
    assert watcher._lacks_card_body(_art("x")) is True


def test_body_row_inserted_immediately_not_held(env):
    env.results[BASE + "100"] = [_body("竜")]
    _run(env, [_tweet(100, "◤竜◢")])
    assert len(env.inserted) == 1


def test_multi_card_tweet_judged_per_card(env):
    tweets = [_tweet(100, "◤A◢ ◤B◢"), _tweet(200, "◤A◢")]
    env.results[BASE + "100"] = [_art("A"), _body("B", "b")]
    env.results[BASE + "200"] = [_body("A", "a")]
    n, keys = _run(env, tweets)
    assert n == 2
    assert {r["name"] for r in env.inserted} == {"A", "B"}
    a = next(r for r in env.inserted if r["name"] == "A")
    assert a["effect_text"] == "効果あり"
    urls = {c.args[1]: c.args[3] for c in env.ingest.call_args_list}
    assert urls[a["id"]] == BASE + "200"
    assert keys == {fuzzy_key("A"), fuzzy_key("B")}


def test_extract_exception_still_raises(env):
    env.extract.side_effect = RuntimeError("失敗")
    with pytest.raises(RuntimeError):
        _run(env, [_tweet(100, "◤竜◢")])
