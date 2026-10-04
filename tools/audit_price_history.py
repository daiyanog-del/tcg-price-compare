"""価格履歴の読取専用監査。実DBは明示した日付・店舗範囲のみSELECTする。

例: python tools/audit_price_history.py --from 2026-09-01 --to 2026-09-30
    --shop カーナベル --summary outputs/audit.json --csv outputs/audit.csv
--fixture JSON は {"sale": [...], "buyback": [...]} でDB接続なしに検証できる。
RESTは一貫したスナップショットではない。候補CSVは削除指示ではない。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


PAGE_SIZE = 500
SOURCES = {
    "sale": ("price_history", "card_id,shop_id,rarity_id,card_name,shop,rarity,recorded_at,min_price,min_price_any,code,url",
             ("card_id", "shop_id", "rarity_id")),
    "buyback": ("buyback_history", "card_name,shop,rarity,recorded_at,max_price",
                ("card_name", "shop", "rarity")),
}
CSV_FIELDS = ["source", "recorded_at", "card_id", "shop_id", "rarity_id", "card_name", "shop", "rarity",
              "min_price", "min_price_any", "max_price", "code", "url", "findings"]
RD_CODE = re.compile(r"(?:^|[\s{（(])RD[/\-]", re.I)
OTHER_CODE = re.compile(r"^(?:DM|DLC|PXR-|GBF[-/])", re.I)


class IncompleteRead(RuntimeError):
    """ページ欠落や読取中の変化により、全件確認を保証できない。"""


def classify_row(source: str, row: dict) -> list[dict]:
    """確定根拠と候補を分け、影響する価格項目を限定する。"""
    shop = row.get("shop", "")
    rarity = unicodedata.normalize("NFKC", row.get("rarity") or "").strip()
    findings = []

    def add(rule, level, axis):
        findings.append({"rule": rule, "level": level, "axis": axis})

    axis = "max_price" if source == "buyback" else "min_price,min_price_any"
    if shop == "カーナベル" and rarity in {"Oラッシュ", "ラッシュ"}:
        add("kanabell_rush_rarity", "confirmed", axis)
    elif shop == "カーナベル" and rarity == "ステンレス":
        # decisions.md 2026-08-03: 買取14行はユーザー判断で保持した。
        add("stainless_retained_2026_08_03" if source == "buyback" else "nonpaper_stainless",
            "retained_by_prior_decision" if source == "buyback" else "nonpaper_product", axis)
    elif "ラッシュ" in rarity or rarity in {"RR", "OR", "ステンレス"}:
        # RR/ORは正規化で原表記が失われるため、ラベルだけで別ゲームを確定しない。
        add("rarity_requires_source_check", "candidate", axis)

    if source == "sale":
        code = (row.get("code") or "").strip()
        if RD_CODE.search(code):
            add("explicit_rd_code", "confirmed", "min_price")
        elif (shop == "トレコロCB" and row.get("card_name") == "真実の名"
              and code in {"DMX12AS1-S3", "DMBD0613-19"}):
            add("historically_verified_dm_code", "confirmed", "min_price")
        elif shop in {"トレコロCB", "カードラボ"} and OTHER_CODE.search(code):
            add("code_requires_source_check", "candidate", "min_price")
        url = row.get("url") or ""
        if url:
            try:
                parsed = urlsplit(url)
                ids = parse_qs(parsed.query).get("id", [])
                if (shop == "カーナベル" and parsed.scheme in {"http", "https"}
                        and parsed.hostname in {"ka-nabell.com", "www.ka-nabell.com"}
                        and parsed.username is None and ids == ["100283847"]):
                    # URLは状態不問最安値側の出品。通常品最安値の誤りまで断定しない。
                    add("verified_kanabell_product_100283847", "confirmed", "min_price_any")
                elif "100283847" in url:
                    add("url_requires_source_check", "candidate", "min_price_any")
            except ValueError:
                add("malformed_url", "candidate", "min_price_any")
    return findings


def iter_database_day(client, source: str, day: str, shops: list[str]):
    """日付と複合キーで安定化した500件ページング。サーバー上限が小さくても継続する。"""
    table, columns, keys = SOURCES[source]
    offset, expected = 0, None
    while True:
        query = client.table(table).select(columns, count="exact").eq("recorded_at", day)
        if shops:
            query = query.in_("shop", shops)
        for key in keys:
            query = query.order(key)
        response = query.range(offset, offset + PAGE_SIZE - 1).execute()
        total, batch = response.count, response.data
        if type(total) is not int or total < 0 or not isinstance(batch, list):
            raise IncompleteRead("件数または応答形式が不明")
        if expected is not None and total != expected:
            raise IncompleteRead("読取中に対象件数が変化")
        expected = total
        if offset + len(batch) > expected or (not batch and offset < expected):
            raise IncompleteRead("ページに欠落または余剰")
        yield from batch
        offset += len(batch)
        if offset == expected:
            return


def fixture_reader(data: dict):
    def read(source, day, shops):
        rows = [row for row in data[source] if row.get("recorded_at") == day
                and (not shops or row.get("shop") in shops)]
        yield from sorted(rows, key=lambda row: tuple(row[key] for key in SOURCES[source][2]))
    return read


def safe_csv(value):
    """カード名などを表計算ソフトの式として実行させない。"""
    value = "" if value is None else str(value)
    if value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def audit(read, start: date, end: date, sources: list[str], shops: list[str], csv_path: Path,
          *, fixture: bool = False) -> dict:
    """候補を随時CSVへ保存。途中失敗でも完了済み範囲と未完了状態を返す。"""
    summary = {
        "complete": False, "mode": "fixture" if fixture else "database_read_only",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "scope": {"from": start.isoformat(), "to_inclusive": end.isoformat(), "timezone": "Asia/Tokyo",
                  "sources": sources, "shops": shops or "all"},
        "snapshot_consistent": fixture,
        "limitations": ["RESTでは一貫したスナップショットを保証しない。件数不変の更新もありうる。",
                        "該当根拠なしは混入なしを意味しない。買取には商品URL・型番がない。",
                        "販売codeは通常品最安、urlは状態不問最安の出品。削除可能件数ではない。"],
        "rows_scanned": 0, "flagged_rows": 0, "rows_without_findings": 0,
        "sale_rows_missing_code": 0, "sale_rows_missing_url": 0,
        "planned_partitions": (end - start).days * len(sources) + len(sources),
        "completed_partitions": [], "errors": [],
    }
    counts = Counter()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for source in sources:
            day = start
            while day <= end:
                day_text = day.isoformat()
                seen = set()
                try:
                    for row in read(source, day_text, shops):
                        key = tuple(row[field] for field in SOURCES[source][2])
                        if key in seen or row.get("recorded_at") != day_text or (shops and row.get("shop") not in shops):
                            raise IncompleteRead("重複キーまたは指定範囲外の応答")
                        seen.add(key)
                        findings = classify_row(source, row)
                        summary["rows_scanned"] += 1
                        if source == "sale":
                            summary["sale_rows_missing_code"] += not bool(row.get("code"))
                            summary["sale_rows_missing_url"] += not bool(row.get("url"))
                        if findings:
                            summary["flagged_rows"] += 1
                            output = {field: row.get(field) for field in CSV_FIELDS}
                            output.update(source=source, findings=json.dumps(findings, ensure_ascii=False))
                            writer.writerow({field: safe_csv(value) for field, value in output.items()})
                            for item in findings:
                                counts[(source, day_text, row.get("shop", ""), item["level"], item["rule"], item["axis"])] += 1
                        else:
                            summary["rows_without_findings"] += 1
                    summary["completed_partitions"].append({"source": source, "date": day_text, "rows": len(seen)})
                except Exception as exc:
                    # 例外本文には接続URLや認証情報が入りうるため記録しない。
                    summary["errors"].append({"source": source, "date": day_text,
                                              "error_type": type(exc).__name__, "code": "read_incomplete"})
                    break
                day += timedelta(days=1)
            if summary["errors"]:
                break
    summary["complete"] = not summary["errors"]
    summary["counts"] = [dict(zip(("source", "date", "shop", "level", "rule", "axis"), key), count=count)
                         for key, count in sorted(counts.items())]
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="start", required=True, type=date.fromisoformat)
    parser.add_argument("--to", dest="end", required=True, type=date.fromisoformat)
    parser.add_argument("--source", action="append", choices=SOURCES)
    parser.add_argument("--shop", action="append", default=[])
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--summary", type=Path, default=Path("outputs/price-history-audit.json"))
    parser.add_argument("--csv", type=Path, default=Path("outputs/price-history-audit.csv"))
    args = parser.parse_args(argv)
    if args.start > args.end:
        parser.error("--from は --to 以前の日付を指定してください")
    paths = [args.csv.resolve(), args.summary.resolve()]
    if len(set(paths)) < 2 or (args.fixture and args.fixture.resolve() in paths):
        parser.error("入力fixtureと出力JSON/CSVには異なるファイルを指定してください")
    if args.fixture:
        read = fixture_reader(json.loads(args.fixture.read_text(encoding="utf-8")))
    else:
        client = None
        def read(source, day, shops):
            nonlocal client
            if client is None:
                from supabase import create_client
                url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
                if not url or not key:
                    raise RuntimeError("Supabase接続設定なし")
                client = create_client(url, key)
            yield from iter_database_day(client, source, day, shops)
    summary = audit(read, args.start, args.end, list(dict.fromkeys(args.source or SOURCES)),
                    list(dict.fromkeys(args.shop)), args.csv, fixture=bool(args.fixture))
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"complete": summary["complete"], "rows_scanned": summary["rows_scanned"],
                      "flagged_rows": summary["flagged_rows"]}))
    return 0 if summary["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
