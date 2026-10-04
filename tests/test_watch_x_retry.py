"""抽出・挿入失敗で投稿カーソルを進めず、再試行できることを検証する。"""

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

import watch_x_unreleased as watcher
from name_normalize import fuzzy_key


@pytest.fixture
def batch(monkeypatch):
    for key in ("SUPABASE_URL", "SUPABASE_KEY", "X_BEARER_TOKEN"):
        monkeypatch.setattr(watcher, key, "test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    sb = MagicMock()
    monkeypatch.setattr(watcher, "create_client", lambda *_: sb)
    monkeypatch.setattr(watcher, "get_user_id", lambda _: "account")
    monkeypatch.setattr(watcher, "_get_known_fuzzy_keys", lambda: set())
    monkeypatch.setattr(watcher, "_load_since_id", lambda _: "99")
    existing = set()
    monkeypatch.setattr(watcher, "_load_existing_fuzzy_keys", lambda _: existing.copy())
    tweets = [{"id": str(100 + i), "text": f"◤カード{i}◢", "attachments": {"media_keys": ["media"]}}
              for i in range(2)]
    monkeypatch.setattr(watcher, "fetch_tweets", lambda *_: (tweets, {"media": "https://example.test/card.jpg"}))
    save = MagicMock()
    monkeypatch.setattr(watcher, "_save_since_id", save)
    extractor = ModuleType("unreleased_extractor")
    extractor.extract_cards_from_tweet = MagicMock()
    monkeypatch.setitem(sys.modules, "unreleased_extractor", extractor)
    image_store = ModuleType("unreleased_image_store")
    image_store.ingest_x_card_image = MagicMock(return_value=(True, "保存済み"))
    monkeypatch.setitem(sys.modules, "unreleased_image_store", image_store)
    return SimpleNamespace(sb=sb, existing=existing, save=save, tweets=tweets,
                           extract=extractor.extract_cards_from_tweet, ingest=image_store.ingest_x_card_image)


def test_extract_failure_keeps_cursor_and_retry_skips_successful_tweet(batch):
    first = {"name": "カード0", "product_name": "商品"}
    second = {"name": "カード1", "product_name": "商品"}
    batch.extract.side_effect = [[first], RuntimeError("星の再読に失敗")]

    def insert():
        row = batch.sb.table.return_value.upsert.call_args.args[0]
        batch.existing.add(fuzzy_key(row["name"]))
        return SimpleNamespace(data=[row])

    batch.sb.table.return_value.upsert.return_value.execute.side_effect = insert
    with pytest.raises(RuntimeError, match="星の再読に失敗"):
        watcher.main()
    batch.save.assert_not_called()
    assert batch.existing == {fuzzy_key("カード0")}

    batch.extract.reset_mock(side_effect=True)
    batch.extract.return_value = [second]
    watcher.main()
    batch.extract.assert_called_once()
    assert batch.extract.call_args.args[0] == "◤カード1◢"
    batch.save.assert_called_once_with(batch.sb, "101")


def test_insert_failure_keeps_cursor(batch):
    batch.extract.return_value = [{"name": "カード0", "product_name": "商品"}]
    batch.sb.table.return_value.upsert.return_value.execute.side_effect = RuntimeError("DB一時障害")
    with pytest.raises(RuntimeError, match="DB一時障害"):
        watcher.main()
    batch.save.assert_not_called()


def test_partial_insert_retries_remaining_card_in_same_tweet(batch):
    batch.tweets[0]["text"] = "◤カード0◢ と ◤カード1◢"
    batch.tweets[1]["text"] = "お知らせ"
    rows = [{"name": f"カード{i}", "product_name": "商品", "id": i + 1,
             "extraction_raw": {"card_image_url": f"https://example.test/card{i}.jpg"}}
            for i in range(2)]
    batch.extract.return_value = rows
    fail_second = True

    def insert():
        row = batch.sb.table.return_value.upsert.call_args.args[0]
        if row["name"] == "カード1" and fail_second:
            # 後続のDB障害より前に成功行の画像保存が済んでいること。
            batch.ingest.assert_called_once_with(batch.sb, 1, "https://example.test/card0.jpg",
                                                 "https://x.com/YuGiOh_OCG_INFO/status/100")
            raise RuntimeError("2枚目の挿入失敗")
        key = fuzzy_key(row["name"])
        if key in batch.existing:
            return SimpleNamespace(data=[])
        batch.existing.add(key)
        return SimpleNamespace(data=[row])

    batch.sb.table.return_value.upsert.return_value.execute.side_effect = insert
    with pytest.raises(RuntimeError, match="2枚目の挿入失敗"):
        watcher.main()
    batch.save.assert_not_called()
    assert batch.existing == {fuzzy_key("カード0")}

    fail_second = False
    watcher.main()
    assert batch.extract.call_count == 2
    assert batch.existing == {fuzzy_key("カード0"), fuzzy_key("カード1")}
    assert batch.ingest.call_count == 2
    assert [call.args[1] for call in batch.ingest.call_args_list] == [1, 2]
    batch.save.assert_called_once_with(batch.sb, "101")


def test_success_advances_cursor_to_maximum_id(batch):
    batch.extract.side_effect = [[{"name": "カード0"}], [{"name": "カード1"}]]
    batch.sb.table.return_value.upsert.return_value.execute.return_value = SimpleNamespace(data=[])
    watcher.main()
    batch.save.assert_called_once_with(batch.sb, "101")
