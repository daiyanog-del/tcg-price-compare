"""読取監査の誤検出・ページ境界・未完了出力をDB通信なしで検証する。"""
import csv
import json
import sys
from datetime import date
from types import SimpleNamespace

import pytest
from tools import audit_price_history as audit


DAY = "2026-10-04"


def row(index=1, **kwargs):
    return {"card_id": index, "shop_id": 1, "rarity_id": 1, "card_name": f"カード{index}",
            "shop": "カーナベル", "rarity": "ウルトラ", "recorded_at": DAY,
            "min_price": 800, "min_price_any": 500, "code": "OCG-JP001", "url": None, **kwargs}


@pytest.mark.parametrize("rarity", ["Oラッシュ", "Ｏラッシュ", " ラッシュ "])
@pytest.mark.parametrize("source", ["sale", "buyback"])
def test_verified_rush_rarities(source, rarity):
    findings = audit.classify_row(source, row(rarity=rarity))
    assert findings[0]["level"] == "confirmed"
    assert findings[0]["axis"] == ("max_price" if source == "buyback" else "min_price,min_price_any")


def test_known_url_only_proves_any_price_side():
    findings = audit.classify_row("sale", row(url="https://www.ka-nabell.com/?genre=1&id=100283847"))
    assert findings == [{"rule": "verified_kanabell_product_100283847", "level": "confirmed", "axis": "min_price_any"}]


@pytest.mark.parametrize("url", [
    "https://www.ka-nabell.com.evil.test/?id=100283847",
    "https://evil.test/?id=100283847",
    "https://www.ka-nabell.com/?id=1002838470",
    "https://www.ka-nabell.com/?id=100283847&id=42",
    "https://user@www.ka-nabell.com/?id=100283847",
])
def test_url_substrings_are_not_confirmed(url):
    assert all(f["level"] == "candidate" for f in audit.classify_row("sale", row(url=url)))


def test_stainless_prior_decision_is_preserved():
    finding = audit.classify_row("buyback", row(rarity="ステンレス"))[0]
    assert finding["level"] == "retained_by_prior_decision"
    assert audit.classify_row("sale", row(rarity="ステンレス"))[0]["level"] == "nonpaper_product"


@pytest.mark.parametrize("rarity", ["RR", "OR", "ラッシュレア", "オーバーラッシュ"])
def test_normalized_rarity_is_only_a_candidate(rarity):
    assert audit.classify_row("sale", row(rarity=rarity))[0]["level"] == "candidate"


@pytest.mark.parametrize("code", ["39913299", "G6-B1", "MA-51SR", "DL5-090R", "P4-06", ""])
def test_ocg_non_jp_and_missing_codes_are_not_flagged(code):
    assert audit.classify_row("sale", row(shop="トレコロCB", card_name="ラッシュ・ウォリアー", code=code)) == []


def test_code_evidence_distinguishes_exact_from_prefix():
    exact = row(shop="トレコロCB", card_name="真実の名", code="DMX12AS1-S3")
    assert audit.classify_row("sale", exact)[0]["level"] == "confirmed"
    assert audit.classify_row("sale", row(shop="トレコロCB", code="DM-UNKNOWN"))[0]["level"] == "candidate"
    assert audit.classify_row("sale", row(code="RD/KP01-JP001"))[0]["axis"] == "min_price"


class Query:
    def __init__(self, client, table):
        self.client, self.table_name = client, table
        self.orders, self.shops = [], []

    def select(self, columns, count):
        assert count == "exact"
        return self

    def eq(self, field, value):
        assert field == "recorded_at"
        self.day = value
        return self

    def in_(self, field, values):
        assert field == "shop"
        self.shops = values
        return self

    def order(self, field):
        self.orders.append(field)
        return self

    def range(self, start, end):
        assert end - start + 1 == 500
        self.start, self.end = start, end
        return self

    def execute(self):
        self.client.calls.append((self.table_name, self.day, self.start, self.end, self.orders))
        if self.client.fail_after is not None and len(self.client.calls) > self.client.fail_after:
            raise RuntimeError("PRIVATE_SUPABASE_KEY_SHOULD_NOT_APPEAR")
        rows = [r for r in self.client.rows if r["recorded_at"] == self.day
                and (not self.shops or r["shop"] in self.shops)]
        rows.sort(key=lambda r: tuple(r[k] for k in self.orders))
        total = len(rows) + (1 if self.client.changing_count and self.start else 0)
        batch = rows[self.start:self.start + min(500, self.client.server_cap)]
        if self.client.empty_later and self.start:
            batch = []
        return SimpleNamespace(data=batch, count=total)


class ReadOnlyClient:
    def __init__(self, rows, *, server_cap=500, fail_after=None, changing_count=False, empty_later=False):
        self.rows, self.server_cap, self.fail_after = rows, server_cap, fail_after
        self.changing_count, self.empty_later = changing_count, empty_later
        self.calls = []

    def table(self, name):
        assert name in {"price_history", "buyback_history"}
        return Query(self, name)


@pytest.mark.parametrize("size, cap", [(0, 500), (500, 500), (1001, 500), (701, 200)])
@pytest.mark.parametrize("source", ["sale", "buyback"])
def test_paging_reads_all_rows_even_when_server_cap_is_smaller(size, cap, source):
    client = ReadOnlyClient([row(i) for i in range(size)], server_cap=cap)
    rows = list(audit.iter_database_day(client, source, DAY, ["カーナベル"]))
    assert len(rows) == size
    assert all(call[4] == list(audit.SOURCES[source][2]) for call in client.calls)


@pytest.mark.parametrize("option", [{"changing_count": True}, {"empty_later": True}])
def test_inconsistent_paging_is_an_error(option):
    client = ReadOnlyClient([row(i) for i in range(501)], **option)
    with pytest.raises(audit.IncompleteRead):
        list(audit.iter_database_day(client, "sale", DAY, []))


def test_midpage_failure_is_incomplete_and_redacts_exception(tmp_path):
    client = ReadOnlyClient([row(i, rarity="Oラッシュ") for i in range(501)], fail_after=1)
    read = lambda *args: audit.iter_database_day(client, *args)
    result = audit.audit(read, date.fromisoformat(DAY), date.fromisoformat(DAY), ["sale", "buyback"], [], tmp_path / "audit.csv")
    assert result["complete"] is False
    assert result["rows_scanned"] == result["flagged_rows"] == 500
    assert result["completed_partitions"] == []
    assert result["errors"] == [{"source": "sale", "date": DAY, "error_type": "RuntimeError", "code": "read_incomplete"}]
    assert "PRIVATE_SUPABASE_KEY" not in json.dumps(result)
    assert result["snapshot_consistent"] is False


def test_fixture_cli_outputs_safe_csv_and_anonymous_summary(tmp_path, capsys):
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"sale": [row(card_name="=HYPERLINK(1)", rarity="Oラッシュ"),
                                             row(2, recorded_at="2026-10-05", rarity="Oラッシュ")],
                                   "buyback": [row(rarity="ステンレス", max_price=100)]}), encoding="utf-8")
    summary_path, csv_path = tmp_path / "summary.json", tmp_path / "rows.csv"
    code = audit.main(["--from", DAY, "--to", DAY, "--fixture", str(fixture),
                       "--summary", str(summary_path), "--csv", str(csv_path)])
    assert code == 0
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["rows_scanned"] == 2
    assert "HYPERLINK" not in json.dumps(summary)
    assert "HYPERLINK" not in capsys.readouterr().out
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["card_name"].startswith("'=HYPERLINK")
    assert "retained_by_prior_decision" in rows[1]["findings"]


def test_fixture_duplicate_key_and_missing_source_are_not_complete(tmp_path):
    for data in ({"sale": [row(), row()]}, {}):
        result = audit.audit(audit.fixture_reader(data), date.fromisoformat(DAY), date.fromisoformat(DAY),
                             ["sale"], [], tmp_path / "rows.csv", fixture=True)
        assert result["complete"] is False


@pytest.mark.parametrize("text", ["=1", "+1", "-1", "@sum(A1)", "  =1", "\t=1", "\r=1"])
def test_spreadsheet_formula_injection_is_escaped(text):
    assert audit.safe_csv(text) == "'" + text


def test_dates_are_required_and_reversed_range_rejected():
    with pytest.raises(SystemExit):
        audit.main([])
    with pytest.raises(SystemExit):
        audit.main(["--from", "2026-10-05", "--to", DAY])


def test_connection_failure_writes_incomplete_summary_without_key(tmp_path, monkeypatch, capsys):
    secret = "DO_NOT_PRINT_CREDENTIAL"
    monkeypatch.setenv("SUPABASE_URL", "https://example.invalid")
    monkeypatch.setenv("SUPABASE_KEY", secret)
    def create_client(*_):
        raise RuntimeError(secret)
    monkeypatch.setitem(sys.modules, "supabase", SimpleNamespace(create_client=create_client))
    output = tmp_path / "summary.json"
    code = audit.main(["--from", DAY, "--to", DAY, "--summary", str(output), "--csv", str(tmp_path / "rows.csv")])
    assert code == 1
    assert json.loads(output.read_text(encoding="utf-8"))["complete"] is False
    assert secret not in output.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
