"""新弾の店舗×レア表の欠けの見張り（pack_matrix_watch・collect_featured.watch_pack_matrix）。"""
from datetime import datetime
from types import SimpleNamespace

import collect_featured
import pack_matrix_watch as pmw
from constants import JST

PACK = {"pack_name": "テストパック", "wiki_page": "テストパック", "tcg_name": "", "start_date": "2026-10-30"}


def _session(cards, rows, fail=False):
    calls = []

    def resp(data):
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: data)

    def get(url, params=None, timeout=None):
        calls.append(("get", url, params))
        if fail:
            raise ConnectionError("つながらない")
        return resp({"cards": cards})

    def post(url, json=None, timeout=None):
        calls.append(("post", url, json))
        return resp({"rows": rows})
    return SimpleNamespace(get=get, post=post, calls=calls)


def _rows(filled, empty):
    return ([{"name": n, "rarity": "ノーマル", "cells": {"遊々亭": {"price": 50}}} for n in filled]
            + [{"name": n, "rarity": "", "cells": {}} for n in empty])


def test_missing_counts_cards_without_any_cell():
    m = {"rows": [{"name": "A", "cells": {}}, {"name": "A", "cells": {"店": {}}}, {"name": "B", "cells": {}}]}
    assert pmw.missing_cards(["A", "B", "C"], m) == ["B", "C"]


def test_alerts_when_many_cards_missing(monkeypatch):
    sent = []
    monkeypatch.setattr(pmw, "send_discord_message", lambda msg, **k: sent.append((msg, k)))
    cards = [f"カード{i}" for i in range(10)]
    s = _session(cards, _rows(cards[:8], cards[8:]))
    out = pmw.check_pack_matrix(PACK, base_url="https://example.test", session=s)
    assert out["missing"] == ["カード8", "カード9"] and out["alerted"]
    assert "2/10" in sent[0][0] and sent[0][1] == {"no_mentions": True}
    assert s.calls[0][1] == "https://example.test/api/packs/cards"
    assert s.calls[1][2] == {"cards": cards}, "パックの一覧が返した名前のまま表を頼む（利用者の画面と同じ道）"


def test_no_alert_when_few_missing(monkeypatch):
    sent = []
    monkeypatch.setattr(pmw, "send_discord_message", lambda msg, **k: sent.append(msg))
    cards = [f"カード{i}" for i in range(10)]
    out = pmw.check_pack_matrix(PACK, base_url="https://example.test", session=_session(cards, _rows(cards[:9], cards[9:])))
    assert out["missing"] == ["カード9"] and not out["alerted"] and sent == []


def test_fetch_failure_is_reported(monkeypatch):
    sent = []
    monkeypatch.setattr(pmw, "send_discord_message", lambda msg, **k: sent.append(msg))
    out = pmw.check_pack_matrix(PACK, base_url="https://example.test", session=_session([], [], fail=True))
    assert "error" in out and "確かめられませんでした" in sent[0]


def test_watch_runs_only_at_10_and_after_release_day(monkeypatch):
    calls = []
    monkeypatch.setattr(pmw, "check_pack_matrix", lambda pack: calls.append(pack["pack_name"]))
    collect_featured.watch_pack_matrix(PACK, datetime(2026, 11, 1, 13, 0, tzinfo=JST))
    collect_featured.watch_pack_matrix(PACK, datetime(2026, 10, 30, 10, 0, tzinfo=JST))  # 発売日
    assert calls == []
    collect_featured.watch_pack_matrix(PACK, datetime(2026, 10, 31, 10, 1, tzinfo=JST))
    collect_featured.watch_pack_matrix({**PACK, "start_date": ""}, datetime(2026, 11, 1, 10, 1, tzinfo=JST))
    assert calls == ["テストパック", "テストパック"]


def test_watch_failure_does_not_raise(monkeypatch):
    """見張りが落ちても収集（終了コード）に響かせない"""
    def boom(pack):
        raise KeyError("pack_name")
    monkeypatch.setattr(pmw, "check_pack_matrix", boom)
    collect_featured.watch_pack_matrix(PACK, datetime(2026, 10, 31, 10, 1, tzinfo=JST))


def test_denominator_capped_like_matrix_api(monkeypatch):
    monkeypatch.setattr(pmw, "send_discord_message", lambda msg, **k: None)
    cards = [f"カード{i}" for i in range(200)]
    s = _session(cards, _rows(cards[:150], []))
    out = pmw.check_pack_matrix(PACK, base_url="https://example.test", session=s)
    assert out["cards"] == 150 and out["missing"] == [] and len(s.calls[1][2]["cards"]) == 150
