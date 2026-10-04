"""所持枚数の後方互換と条件付き同期更新を検証する。"""
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import sync


def deck(**extra):
    return dict(id="d1", name="デッキ", text="3 A", main=[{"name": "A", "qty": 3}],
                ex=[], updated=10, **extra)


def test_validation_preserves_zero_and_absence():
    clean, error = sync.validate_decks([deck(owned={"A": 0, "__proto__": 2})])
    assert error is None
    assert clean[0]["owned"] == {"A": 0, "__proto__": 2}
    assert "owned" not in sync.validate_decks([deck()])[0][0]
    assert sync.validate_decks([deck(owned={})])[0][0]["owned"] == {}


@pytest.mark.parametrize("owned", [None, [], {"A": True}, {"A": -1}, {"A": 100},
                                     {"A": 1.5}, {"A": "0"}, {"": 0}, {"名" * 51: 0},
                                     {str(i): 0 for i in range(161)}])
def test_invalid_ownership_rejected(owned):
    assert sync.validate_decks([deck(owned=owned)])[1]


def test_merge_legacy_winner_retains_ownership_and_explicit_empty_clears():
    server = deck(owned={"A": 1})
    old_client = {**deck(), "updated": 20}
    assert sync.merge_decks([server], [old_client])[0]["owned"] == {"A": 1}
    assert sync.merge_decks([old_client], [server])[0]["owned"] == {"A": 1}
    assert sync.merge_decks([server], [{**old_client, "owned": {}}])[0]["owned"] == {}
    assert server["owned"] == {"A": 1}


class Store:
    def __init__(self, records, revision=1, collide=0):
        self.row = {"decks": records, "decks_rev": revision}
        self.collide = collide
        self.writes = 0

    def table(self, _):
        store = self

        class Query:
            values = None

            def __init__(self):
                self.conds = {}

            def select(self, _):
                return self

            def update(self, values):
                self.values = values
                return self

            def eq(self, key, value):
                self.conds[key] = value
                return self

            def execute(self):
                if self.values is None:
                    return SimpleNamespace(data=[dict(store.row)])
                store.writes += 1
                if store.collide:
                    store.collide -= 1
                    store.row["decks_rev"] += 1
                    store.row["decks"] = [deck(owned={"A": 2})]
                if self.conds["decks_rev"] != store.row["decks_rev"]:
                    return SimpleNamespace(data=[])
                store.row.update(self.values)
                return SimpleNamespace(data=[dict(store.row)])

        return Query()


def test_legacy_normal_push_keeps_owned_without_resurrecting_deleted_decks():
    server = Store([deck(owned={"A": 1}), {**deck(), "id": "deleted"}])
    result = sync.push_decks(server, "s", 1, [deck()])
    assert result["status"] == "merged"
    assert result["items"][0]["owned"] == {"A": 1}
    assert len(server.row["decks"]) == 1


def test_explicit_empty_push_clears():
    server = Store([deck(owned={"A": 1})])
    assert sync.push_decks(server, "s", 1, [deck(owned={})])["status"] == "applied"
    assert server.row["decks"][0]["owned"] == {}


def test_collision_reloads_latest_ownership():
    server = Store([deck(owned={"A": 1})], collide=1)
    result = sync.push_decks(server, "s", 1, [{**deck(), "updated": 20}])
    assert result["status"] == "merged"
    assert result["items"][0]["owned"] == {"A": 2}
    assert server.writes == 2


def test_collision_retry_is_bounded():
    server = Store([deck(owned={"A": 1})], collide=100)
    assert sync.push_decks(server, "s", 1, [deck()]) == {"ok": False, "reason": "conflict_retry_exceeded"}
    assert server.writes == sync.PUSH_MAX_RETRY


@pytest.mark.skipif(shutil.which("node") is None, reason="node が見つかりません")
@pytest.mark.parametrize("script", ["deck_ownership_check.js", "deck_ownership_sync_check.js"])
def test_client_ownership(script):
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(["node", f"tests/js/{script}"], cwd=root,
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
