"""
build_art_prints.py — 型番ごと違う絵で刷られた日本の版の表（data/art_prints.json）を Yugipedia から作る（週1回の定期実行用）

なぜ要るか: 三戦の号 CH01-JP030 のように型番ごと違う絵で再録された版は、その型番の中に絵が1種類しか無いので、
店の商品名の多くは無印になる。art_variant は今まで「(イラスト違い)」と書くカーナベルの行を手がかりに全店をそろえていたが、
カーナベルが取れない日は元のイラストに戻ってしまう（2026-10-29 ユーザー指摘・PR #255 の残り）。店に頼らない手がかりとして持つ。

Yugipedia の書き方（2026-10-29 三戦の号・I：Pマスカレーナ・灰流うららで確認）:
  - カードのページの {{CardTable2}} の image 欄は、絵ごとに「番号; 代表の版の画像; …」を並べる。
    「1; TripleTacticsThrust-LAVD-EN-C-1E.png」「2; TripleTacticsThrust-CH01-EN-UR-1E.png」のように、
    画像の名前の「-<パック>-<地域>-」で、その絵を代表する版のパックが分かる。「1.1」は1番の絵の小さな違い（同じ絵として扱う）
  - jp_sets 欄に日本の版が「CH01-JP030; パック名; レアリティ, …」と1行ずつ並ぶ
  - 画像一覧（Card Gallery:<カード名>）で「; AA」が付くのは、同じ型番の中に絵が2つある版だけ（灰流うらら PAC1-JP016）。
    型番ごと違う絵の版（三戦の号 CH01）には何も付かない
判定: 2番目以降の絵の代表と同じパックの日本の型番を「型番ごと違う絵」とする。ただし、1番の絵の代表にも同じパックがあるもの、
画像一覧でその型番に AA があるもの（型番の中に絵が2つ＝店の言葉で見分ける側）は入れない。
代表が英語版だけのパック（RA04 など）の絵は、日本の型番に届かない（拾えないだけで、誤って入れることはない）。

実行:
  python tools/build_art_prints.py              # data/art_prints.json を作り直す（変わらなければ書かない）
  python tools/build_art_prints.py --title "Triple Tactics Thrust"   # そのカードだけ読んで結果を表示（書かない）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tcg_announce as yp  # noqa: E402
from art_variant import _key  # noqa: E402

OUT_PATH = ROOT / "data" / "art_prints.json"
CATEGORY = "Category:OCG cards"
# 読めたカードがこれより少なければ、Yugipedia 側の不調とみなして表を書き換えない（2026-10-29 時点で約14,700枚）
MIN_PAGES = 10000

_IMAGE_LINE = re.compile(r"^\s*(\d+)(?:\.\d+)?\s*;(.*)$")
_FILE_SET = re.compile(r"-([A-Z0-9]+)-([A-Z]{2})-")
_JP_CODE = re.compile(r"^\s*(([A-Z0-9]+)-JP[A-Z0-9]+)\s*;", re.MULTILINE)


def _unruby(s: str) -> str:
    """ふりがなを外す。カードのページには {{Ruby|…}} と {{ruby|…}} の両方がある（光霊使いライナは小文字）"""
    return re.sub(r"\{\{\s*ruby\s*\|([^|}]*)\|[^}]*\}\}", r"\1", s or "", flags=re.IGNORECASE)


def artwork_sets(image_value: str) -> dict[int, set[str]]:
    """image 欄 → 絵の番号ごとの、代表の版のパック（MADU などゲームの画像も入るが、日本の型番と突き合わせるので害は無い）"""
    out: dict[int, set[str]] = {}
    for line in (image_value or "").splitlines():
        m = _IMAGE_LINE.match(line)
        if not m:
            continue
        sets = out.setdefault(int(m.group(1)), set())
        for name in m.group(2).split(";"):
            f = _FILE_SET.search(name)
            if f:
                sets.add(f.group(1))
    return out


def jp_codes(jp_sets_value: str) -> list[tuple[str, str]]:
    """jp_sets 欄 → [(型番, パック)]"""
    return [(m.group(1), m.group(2)) for m in _JP_CODE.finditer(jp_sets_value or "")]


def gallery_aa_codes(gallery_text: str) -> set[str]:
    """画像一覧の日本の段で「; AA」が付いた型番（同じ型番の中に絵が2つある版）"""
    m = re.search(r"\{\{\s*Card gallery\s*\|\s*region\s*=\s*JP\s*\|(.*?)\}\}", gallery_text or "", re.DOTALL)
    if not m:
        return set()
    out = set()
    for line in m.group(1).splitlines():
        parts = [p.strip() for p in line.split("//")[0].split(";")]
        if len(parts) >= 3 and "AA" in parts[3:]:
            out.add(parts[0])
    return out


def card_info(page_text: str) -> dict | None:
    """カードのページ → {ja_name, alt_candidates}。絵が1種類か、日本の版が無ければ None"""
    p = yp.template_params(page_text, "CardTable2")
    if not p:
        return None
    ja_name = yp.plain_text(_unruby(p.get("ja_name", ""))).strip()
    arts = artwork_sets(p.get("image", ""))
    if not ja_name or len(arts) < 2:
        return None
    base_sets = arts.get(1, set())
    alt_sets = set().union(*(s for n, s in arts.items() if n >= 2)) - base_sets
    cands = sorted(code for code, pack in jp_codes(p.get("jp_sets", "")) if pack in alt_sets)
    return {"ja_name": ja_name, "alt_candidates": cands}


def alt_codes(info: dict, aa_codes: set[str]) -> list[str]:
    """候補から、型番の中に絵が2つある版（画像一覧の AA）を除いたもの"""
    return [c for c in info["alt_candidates"] if c not in aa_codes]


def _iter_category_pages():
    """OCG のカードのページを50枚ずつ（wikitext つき）"""
    params = {"action": "query", "generator": "categorymembers", "gcmtitle": CATEGORY, "gcmnamespace": 0,
              "gcmlimit": yp.BATCH, "prop": "revisions", "rvprop": "content", "rvslots": "main"}
    cont: dict = {}
    while True:
        data = yp._get(dict(params, **cont))
        for p in data.get("query", {}).get("pages", []):
            if p.get("revisions"):
                yield p["title"], p["revisions"][0]["slots"]["main"]["content"]
        if "continue" not in data:
            return
        cont = data["continue"]


def _galleries(titles: list[str]) -> dict[str, str]:
    """カード名 → 画像一覧の wikitext（無いページは入らない）"""
    pages = yp.fetch_pages(titles=["Card Gallery:" + t for t in titles])
    return {p["title"].removeprefix("Card Gallery:"): p["text"] for p in pages.values()}


def build(pages) -> tuple[dict[str, list[str]], int]:
    """(カード名の比べる形 → 型番ごと違う絵の日本の型番, 読んだページ数)"""
    infos: dict[str, dict] = {}
    n = 0
    for title, text in pages:
        n += 1
        info = card_info(text)
        if info and info["alt_candidates"]:
            infos[title] = info
    galleries = _galleries(list(infos))
    table: dict[str, list[str]] = {}
    for title, info in infos.items():
        codes = alt_codes(info, gallery_aa_codes(galleries.get(title, "")))
        if codes:
            key = _key(info["ja_name"])
            table[key] = sorted(set(table.get(key, [])) | set(codes))
    return table, n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", help="このカードだけ読んで表示する（書かない）")
    args = ap.parse_args()
    if args.title:
        pages = yp.fetch_pages(titles=[args.title])
        table, _ = build((p["title"], p["text"]) for p in pages.values())
        print(json.dumps(table, ensure_ascii=False, indent=1))
        return 0
    table, n = build(_iter_category_pages())
    print(f"読んだカード {n} 枚 / 型番ごと違う絵のあるカード {len(table)} 枚")
    if n < MIN_PAGES:
        print(f"読めたカードが {MIN_PAGES} 枚未満なので表を書き換えない", file=sys.stderr)
        return 1
    body = json.dumps(dict(sorted(table.items())), ensure_ascii=False, indent=0) + "\n"
    if OUT_PATH.exists() and OUT_PATH.read_text(encoding="utf-8") == body:
        print("変わりなし")
        return 0
    OUT_PATH.write_text(body, encoding="utf-8", newline="\n")
    print(f"書き出し: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
