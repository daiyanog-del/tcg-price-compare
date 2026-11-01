"""
build_art_ledger.py — 絵の台帳（data/art_ledger.json）の下書きを Yugipedia から作る（手で使う道具。定期実行はしない）

なぜ要るか: 青眼の白龍・ブラック・マジシャンのように絵が10種類以上あるカードは、店の「元のイラスト／イラスト違い」の2択では
分けきれない。QCDB・QCCU の 25thシークが「元のイラスト」に混ざり、絵の違う出品が同じ札に並んでいた（2026-11-01 ユーザー指摘）。
カードごとに「どの型番がどの絵か」を台帳に持ち、絵の名前で分ける（art_variant.ledger_art）。

Yugipedia の書き方（2026-11-01 ブラマジ・青眼・レッドアイズで確認）:
  - カードのページの {{CardTable2}} の image 欄に、絵の番号ごとの代表の画像が並ぶ（ブラマジ20・青眼19・レッドアイズ13）。
    番号は英語版・ゲームの絵も含む通し番号で、「OCG の何番目の絵」とは別。代表の画像が無い番号もある（ブラマジ 3 番＝パンドラの絵）
  - 日本の版が何番の絵かは、どこにも直接は書いていない。Set Card Lists の「(2nd artwork)」は OCG の番号で、image 欄の番号と合わない
  - そこで画像一覧（Card Gallery:<カード名>）の日本の版の画像を、番号ごとの代表の画像と絵の部分で見比べて振り分ける。
    新しい版は似ている度合いが 0.7 以上でほぼ当たる。古い印刷（LB・EX・DL2 などの jpg のスキャン）は写りが違って 0.6 未満になりやすく、
    目で確かめる（下書きの "unsure"）。同じポーズで背景だけ違う絵（レッドアイズ 3 番と 4 番）は 0.8 と近いので、名前を付けるときに目でも見る
  - 画像の名前は「<カード名>-<パック>-JP-<レアリティの略号>[-AA<n>].png」。型番はカードのページの jp_sets 欄で、パックとレアリティから引く
  - ⚠ imageinfo の問い合わせに iiurlwidth（縮小画像）を付けると 403 になる。元の大きさの画像を取る

出力（標準出力の JSON）: {"title", "ja_name", "reps": {番号: 代表の画像}, "printings": [{file, code, rarity, aa, art, score, second}]}
これを見て、名前・既定の絵・古い印刷の番号を人が決め、data/art_ledger.json に書く（台帳は手で直すデータ）。

実行:
  python tools/build_art_ledger.py "Red-Eyes Black Dragon" > draft.json
  python tools/build_art_ledger.py "Red-Eyes Black Dragon" --cache tmp/rebd   # 画像を置く場所（既定は一時フォルダ）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import tcg_announce as yp  # noqa: E402
from build_art_prints import _unruby  # noqa: E402

# 画像一覧の日本の段のうち、カードではないもの（アニメ・漫画・ゲームの画面・バンダイ版・ラッシュデュエル・韓国版など）
_NOT_CARD = re.compile(r"Anime|-JP-Manga|-VG|-RD|DDM|Commercial|-GC\.|BAN\d|Full-JP|-KR-|-OP[.-]")
# 画像の名前の「-<パック>-JP-<レアリティの略号>[-AA<n>]」
_FILE_RE = re.compile(r"-([A-Z0-9]+)-JP-([A-Za-z0-9]+?)(?:-(AA\d?))?(?:-[A-Za-z0-9]+)*\.(?:png|jpg)$")
# 画像の名前のレアリティの略号 → jp_sets 欄の書き方
RARITY_ABBR = {
    "C": "Common", "R": "Rare", "SR": "Super Rare", "UR": "Ultra Rare", "ScR": "Secret Rare", "UtR": "Ultimate Rare",
    "UPR": "Ultra Parallel Rare", "NPR": "Normal Parallel Rare", "PScR": "Prismatic Secret Rare",
    "QCScR": "Quarter Century Secret Rare", "20ScR": "20th Secret Rare", "MLR": "Millennium Rare",
    "MLUR": "Millennium Ultra Rare", "MLScR": "Millennium Secret Rare", "PGR": "Premium Gold Rare",
    "HGPR": "Holographic Parallel Rare", "HGR": "Holographic Rare", "EScR": "Extra Secret Rare",
    "KCC": "Kaiba Corporation Common", "KCUR": "Kaiba Corporation Ultra Rare",
    "DRPR": "Duel Terminal Rare Parallel Rare", "DSPR": "Duel Terminal Super Parallel Rare",
    "DNPR": "Duel Terminal Normal Parallel Rare",
}
# 見比べの点数がこれ未満なら目で確かめる（2026-11-01 の3枚で、外れはすべて 0.6 未満の古い印刷だった）
UNSURE_BELOW = 0.6
UA = {"User-Agent": "GreedyLabArtCheck/1.0 (+https://greedy-lab.com)"}


def representative_files(image_value: str) -> dict[int, str]:
    """image 欄 → 番号ごとの代表の画像（カードの画像を優先。ゲームの画面・イラストだけの画像は後回し）"""
    out: dict[int, str] = {}
    for line in (image_value or "").splitlines():
        m = re.match(r"^\s*(\d+)(?:\.\d+)?\s*;(.*)$", line)
        if not m:
            continue
        files = [f.strip() for f in m.group(2).split(";") if f.strip()]
        cards = [f for f in files if not re.search(r"-VG|-OW-|-CA-|-DAR\.", f)]
        if files:
            out.setdefault(int(m.group(1)), (cards or files)[0])
    return out


def jp_printings(jp_sets_value: str) -> list[tuple[str, list[str]]]:
    """jp_sets 欄 → [(型番, [レアリティ…])]。型番の無い行（20th Anniversary Duelist Box の UR など）は型番が空"""
    out = []
    for line in (jp_sets_value or "").splitlines():
        parts = [p.strip() for p in line.split(";")]
        if len(parts) >= 3:
            out.append((parts[0], [r.strip() for r in parts[2].split(",") if r.strip()]))
    return out


def resolve_code(pack: str, abbr: str, printings: list[tuple[str, list[str]]]) -> list[str]:
    """画像の名前のパック・レアリティ → 型番の候補（同じパックに型番が2つある 15AY・20TH はレアリティで分ける）"""
    rarity = RARITY_ABBR.get(abbr, "")
    cands = [c for c, rs in printings if c and c.split("-")[0] == pack and (not rarity or rarity in rs)]
    return sorted(set(cands))


def feature(im: Image.Image) -> np.ndarray:
    """絵の部分（枠と文字を除いた中ほど）を 20x20 の色に縮め、明るさの平均を引いて長さ1にしたもの"""
    w, h = im.size
    a = im.convert("RGB").crop((int(w * .13), int(h * .19), int(w * .87), int(h * .71))).resize((20, 20), Image.LANCZOS)
    v = np.asarray(a, dtype=float).reshape(-1)
    v = v - v.mean()
    return v / (np.linalg.norm(v) + 1e-9)


def _file_urls(files: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in range(0, len(files), 15):  # 名前を並べすぎると URL が長くなるので15ずつ
        d = yp._get({"action": "query", "titles": "|".join("File:" + f for f in files[i:i + 15]),
                     "prop": "imageinfo", "iiprop": "url"})
        norm = {n["to"]: n["from"] for n in d["query"].get("normalized", [])}
        for p in d["query"]["pages"]:
            info = p.get("imageinfo") or []
            if info:
                t = norm.get(p["title"], p["title"])
                out[t.removeprefix("File:")] = info[0]["url"]
    return out


def _load(url: str, path: Path) -> Image.Image:
    if not path.exists():
        req = urllib.request.Request(url, headers=UA)
        path.write_bytes(urllib.request.urlopen(req, timeout=30).read())
        time.sleep(0.4)
    return Image.open(path)


def draft(title: str, cache: Path) -> dict:
    page = list(yp.fetch_pages(titles=[title]).values())[0]["text"]
    p = yp.template_params(page, "CardTable2") or {}
    reps = representative_files(p.get("image", ""))
    printings = jp_printings(p.get("jp_sets", ""))
    imgs = yp._get({"action": "parse", "page": "Card Gallery:" + title, "prop": "images"})["parse"]["images"]
    jp = sorted(f for f in imgs if "-JP-" in f and not _NOT_CARD.search(f))
    urls = _file_urls(list(reps.values()) + jp)
    cache.mkdir(parents=True, exist_ok=True)

    def feat(f: str):
        try:
            return feature(_load(urls[f], cache / re.sub(r"[^\w.-]", "_", f)))
        except Exception as e:  # 消えた画像（404）など。その版は下書きに入れない
            print(f"画像を読めない {f}: {e}", file=sys.stderr)
            return None

    rep_feats = {n: v for n, f in reps.items() if f in urls and (v := feat(f)) is not None}
    if not rep_feats:
        raise SystemExit(f"{title}: 代表の画像を1枚も読めない（image 欄か Yugipedia の画像を確かめる）")
    rows = []
    for f in jp:
        m = _FILE_RE.search(f)
        if not m or f not in urls or (v := feat(f)) is None:
            continue
        scores = sorted(((float(v @ rv), n) for n, rv in rep_feats.items()), reverse=True)
        rows.append({"file": f, "code": resolve_code(m.group(1), m.group(2), printings), "rarity": m.group(2),
                     "aa": m.group(3) or "", "art": scores[0][1], "score": round(scores[0][0], 3),
                     "second": [scores[1][1], round(scores[1][0], 3)] if len(scores) > 1 else None,
                     "unsure": scores[0][0] < UNSURE_BELOW})
    ja = yp.plain_text(_unruby(p.get("ja_name", ""))).strip()
    return {"title": title, "ja_name": ja, "reps": reps, "printings": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("title", help="Yugipedia のカードのページの名前（英語）")
    ap.add_argument("--cache", help="画像を置く場所（既定は一時フォルダ）")
    args = ap.parse_args()
    cache = Path(args.cache) if args.cache else Path(tempfile.gettempdir()) / "art_ledger_cache"
    print(json.dumps(draft(args.title, cache), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
