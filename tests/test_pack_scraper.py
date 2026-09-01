"""
tests/test_pack_scraper.py — get_pack_list() の表示用/収集用分離テスト

背景:
    2026-07-10 の ea070ad で「Wikiにカードリスト未掲載パック（主に発売前パック）に
    _MAX_PACKS の枠を消費させない」挙動が入った。これは収集用（tracked_cards候補集め）
    には正しいが、副作用としてサイトの最新弾リスト（/api/packs）からも発売前パックが
    消えていた。この副作用を解消するため、get_pack_list(include_empty) で
    表示用（include_empty=True・既定）と収集用（include_empty=False）を分離した。

テスト方針:
    - _fetch_latest_packs_from_official / _resolve_wiki_page をモックし、
      ネットワークに一切出ない状態で挙動を検証する。
    - キャッシュは tmp_path にリダイレクトし、実キャッシュを汚染しない。
    - 表示用/収集用でキャッシュキーが分離され、片方の結果がもう片方に
      漏れ出さないこと（キャッシュ汚染がないこと）も検証する。
"""

from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pack_scraper


@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch, tmp_path):
    """テストごとにキャッシュディレクトリを隔離し、実キャッシュを汚染しない"""
    monkeypatch.setattr(pack_scraper, "_CACHE_DIR", tmp_path / "packs")


def _make_official_packs():
    """
    公式サイト取得結果を模したパック一覧（新しい順）。
    先頭2件は「Wikiにカードリスト未掲載」＝発売前パックを想定。
    """
    return [
        {"name": "未発売パックA", "wiki_page": "未発売パックA", "tcg_name": "", "date": "2026-08-01", "category": "basic"},
        {"name": "未発売パックB", "wiki_page": "未発売パックB", "tcg_name": "", "date": "2026-07-25", "category": "basic"},
        {"name": "既発売パック1", "wiki_page": "既発売パック1", "tcg_name": "", "date": "2026-07-10", "category": "basic"},
        {"name": "既発売パック2", "wiki_page": "既発売パック2", "tcg_name": "", "date": "2026-06-20", "category": "basic"},
        {"name": "既発売パック3", "wiki_page": "既発売パック3", "tcg_name": "", "date": "2026-05-15", "category": "basic"},
        {"name": "既発売パック4", "wiki_page": "既発売パック4", "tcg_name": "", "date": "2026-04-10", "category": "basic"},
    ]


_CARDLESS_NAMES = {"未発売パックA", "未発売パックB"}


def _fake_resolve_wiki_page(pack_name: str):
    """未発売パック（先頭2件）はカードリストなし、それ以外はありを返す"""
    has_cards = pack_name not in _CARDLESS_NAMES
    return pack_name, has_cards


@pytest.fixture(autouse=True)
def _mock_official_and_wiki(monkeypatch):
    monkeypatch.setattr(pack_scraper, "_fetch_latest_packs_from_official", _make_official_packs)
    monkeypatch.setattr(pack_scraper, "_resolve_wiki_page", _fake_resolve_wiki_page)


class TestCollectUse:
    """収集用（include_empty=False）: 0枚パックを除外し、枠も消費しない"""

    def test_cardless_packs_excluded(self):
        packs = pack_scraper.get_pack_list(include_empty=False)
        names = [p["name"] for p in packs]
        assert "未発売パックA" not in names
        assert "未発売パックB" not in names

    def test_slot_not_consumed_by_cardless(self):
        """
        _MAX_PACKS(4) 枠が発売前2件に消費されず、
        カードリストのある既発売パックが4件そのまま選ばれる
        """
        packs = pack_scraper.get_pack_list(include_empty=False)
        names = [p["name"] for p in packs]
        assert len(packs) == pack_scraper._MAX_PACKS
        assert names == ["既発売パック1", "既発売パック2", "既発売パック3", "既発売パック4"]


class TestDisplayUse:
    """表示用（include_empty=True・既定）: 0枚パックも含めて直近 _MAX_PACKS 件をそのまま返す"""

    def test_cardless_packs_included(self):
        packs = pack_scraper.get_pack_list(include_empty=True)
        names = [p["name"] for p in packs]
        assert "未発売パックA" in names
        assert "未発売パックB" in names

    def test_default_is_display_use(self):
        """引数省略時は表示用（include_empty=True）と同じ結果になる"""
        default_packs = pack_scraper.get_pack_list()
        explicit_packs = pack_scraper.get_pack_list(include_empty=True)
        assert [p["name"] for p in default_packs] == [p["name"] for p in explicit_packs]

    def test_returns_latest_max_packs_regardless_of_cards(self):
        packs = pack_scraper.get_pack_list(include_empty=True)
        names = [p["name"] for p in packs]
        assert len(packs) == pack_scraper._MAX_PACKS
        assert names == ["未発売パックA", "未発売パックB", "既発売パック1", "既発売パック2"]


class TestHasCardsField:
    """表示用（include_empty=True）の各エントリに has_cards が含まれること"""

    def test_has_cards_present_and_correct(self):
        packs = pack_scraper.get_pack_list(include_empty=True)
        by_name = {p["name"]: p for p in packs}
        assert by_name["未発売パックA"]["has_cards"] is False
        assert by_name["未発売パックB"]["has_cards"] is False
        assert by_name["既発売パック1"]["has_cards"] is True
        assert by_name["既発売パック2"]["has_cards"] is True


class TestCacheIsolation:
    """表示用/収集用のキャッシュが分離され、互いの結果を汚染しないこと"""

    def test_collect_then_display_not_polluted(self):
        # 先に収集用を呼んでキャッシュさせる
        collect_packs = pack_scraper.get_pack_list(include_empty=False)
        assert "未発売パックA" not in [p["name"] for p in collect_packs]

        # 続けて表示用を呼んでも、収集用キャッシュの影響を受けず発売前パックを含む
        display_packs = pack_scraper.get_pack_list(include_empty=True)
        assert "未発売パックA" in [p["name"] for p in display_packs]

    def test_display_then_collect_not_polluted(self):
        # 先に表示用を呼んでキャッシュさせる
        display_packs = pack_scraper.get_pack_list(include_empty=True)
        assert "未発売パックA" in [p["name"] for p in display_packs]

        # 続けて収集用を呼んでも、表示用キャッシュの影響を受けず発売前パックを除外する
        collect_packs = pack_scraper.get_pack_list(include_empty=False)
        assert "未発売パックA" not in [p["name"] for p in collect_packs]

    def test_cache_files_are_separate(self):
        pack_scraper.get_pack_list(include_empty=True)
        pack_scraper.get_pack_list(include_empty=False)
        cache_files = sorted(p.name for p in pack_scraper._CACHE_DIR.glob("*.json"))
        assert cache_files == ["pack_list_auto.json", "pack_list_collect.json"]
