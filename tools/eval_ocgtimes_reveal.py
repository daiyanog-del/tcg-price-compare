"""
eval_ocgtimes_reveal.py — OCGタイムズの紹介場面からの取込（ocgtimes_reveal）を、人が確かめた正解と比べる（DBは読むだけ）

正解: 本番 DB の unreleased_cards（既定は id 612〜627。10-16 配信の16枚。10-17 に人が承認し、字幕と照合して5枚を直した）。
比べるもの: ocgtimes_reveal.extract_reveal が作る行（本番の取込と同じ処理。登録・画像保存はしない）。
  - カードごと: 効果文（norm 後の完全一致と字数の差）・種別・属性・種族・レベル/ランク/リンク・攻守
  - 字幕とカード画像の突き合わせ（card_text_check）が一致したカード＝自動承認候補に、誤りが無いか
  - 網羅: 正解にあって取込に無いカード、取込にあって正解に無いカード、一覧画面にだけあったカード
  - 場面ごとの数値の読み違い（まとめる前）

使い方（自宅PC。~/.cardsouba/watcher.env の ANTHROPIC_API_KEY / SUPABASE_URL / SUPABASE_KEY を使う）:
  python -I tools/eval_ocgtimes_reveal.py <フレームのフォルダ> [--ids 612-627] [--out 結果.json] [--no-card-check]
  フォルダの画像の名前に「_t<秒>」を含める。名前に「list」を含む画像は一覧画面として扱う
  （例: reveal_t04754.png / ocgtimes_imph_list1_t6448.png）。
費用の目安: 24画面＋一覧4画面＋突き合わせ16枚で Opus 5.5 を44回（2026-10-17 実測は約2分）。
"""

import argparse
import difflib
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

FIELDS = ["card_type", "attribute", "race", "level", "rank", "link_val", "atk", "def"]
_T_RE = re.compile(r"_t(\d+)")


def _load_env() -> None:
    path = os.path.join(os.path.expanduser("~"), ".cardsouba", "watcher.env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def char_errors(a: str, b: str) -> int:
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal")


def same(field: str, a, b) -> bool:
    return str(a if a is not None else "").strip() == str(b if b is not None else "").strip()


def load_frames(folder: str):
    """フォルダの画像を（紹介場面, 一覧画面）に分け、normalize_uploaded_image の形で返す。"""
    from unreleased_extractor import normalize_uploaded_image
    from unreleased_upload import image_key_for
    reveal, lists = [], []
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            continue
        with open(os.path.join(folder, name), "rb") as f:
            data = f.read()
        m = _T_RE.search(name)
        item = {"image": normalize_uploaded_image(data, image_key_for(data)), "t": int(m.group(1)) if m else None,
                "file": name}
        (lists if "list" in name.lower() else reveal).append(item)
    return reveal, lists


def compare(rows: list[dict], truth: list[dict]) -> dict:
    from effect_text_reread import diff_summary, norm
    from name_normalize import fuzzy_key
    by_key = {fuzzy_key(c["name"]): c for c in truth}
    cards, extra = [], []
    for row in rows:
        card = by_key.get(fuzzy_key(row["name"]))
        raw = row["extraction_raw"]
        rv = raw.get("reveal") or {}
        if card is None:
            extra.append(row["name"])
            continue
        t_eff, r_eff = norm(card.get("effect_text") or ""), norm(row.get("effect_text") or "")
        wrong = [f for f in FIELDS if not same(f, row.get(f), card.get(f))]
        cards.append({
            "id": card["id"], "name": card["name"],
            "effect_ok": t_eff == r_eff, "effect_char_errors": char_errors(t_eff, r_eff), "effect_len": len(t_eff),
            "effect_diff": "" if t_eff == r_eff else diff_summary(t_eff, r_eff),
            "fields_wrong": {f: {"truth": card.get(f), "read": row.get(f)} for f in wrong},
            "uncertain_fields": raw.get("uncertain_fields"),
            "card_text_check": (rv.get("card_text_check") or {}).get("status"),
            "card_text_diff": (rv.get("card_text_check") or {}).get("diff_summary"),
            "auto_approve_candidate": rv.get("auto_approve_candidate"),
            "value_conflicts": rv.get("value_conflicts"),
            "list_check": (rv.get("list_check") or {}).get("status"),
            "frames": [f.get("t") for f in rv.get("frames") or []],
            "saved_frame_t": rv.get("saved_frame_t"),
            "crop": raw.get("crop_suggestion"),
        })
    got = {fuzzy_key(r["name"]) for r in rows}
    missing = [c["name"] for c in truth if fuzzy_key(c["name"]) not in got]
    return {"cards": cards, "extra": extra, "missing": missing}


def frame_errors(reads: list[dict], truth: list[dict]) -> list[dict]:
    """まとめる前の場面ごとの数値・種別の読み違い（名前が正解と一致した場面だけ）。"""
    from name_normalize import fuzzy_key
    by_key = {fuzzy_key(c["name"]): c for c in truth}
    out = []
    for r in reads:
        card = by_key.get(fuzzy_key(r.get("name") or ""))
        if not card:
            continue
        wrong = {f: {"truth": card.get(f), "read": r.get(f)} for f in FIELDS if not same(f, r.get(f), card.get(f))}
        if wrong:
            out.append({"t": r.get("t"), "name": card["name"], "wrong": wrong})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--ids", default="612-627", help="正解の id 範囲（例 612-627）")
    ap.add_argument("--video", default="https://www.youtube.com/watch?v=v-QIhn_zLoM")
    ap.add_argument("--product", default="IMMORTAL PHOENIX")
    ap.add_argument("--out", default="")
    ap.add_argument("--no-card-check", action="store_true", help="カード画像側の読み直しをしない（費用を抑える）")
    args = ap.parse_args(argv)
    _load_env()
    import logging
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")

    from supabase import create_client
    from ocgtimes_reveal import extract_reveal
    lo, hi = (int(x) for x in args.ids.split("-"))
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    truth = (sb.table("unreleased_cards").select("id, name, effect_text, " + ", ".join(FIELDS))
             .gte("id", lo).lte("id", hi).order("id").execute().data or [])
    reveal, lists = load_frames(args.folder)
    print(f"正解 {len(truth)}枚 / 紹介場面 {len(reveal)}画面 / 一覧 {len(lists)}画面")
    result = extract_reveal(reveal, lists, video_url=args.video, product_name=args.product,
                            card_check=not args.no_card_check)
    cmp = compare(result["rows"], truth)
    cmp["frame_errors"] = frame_errors(result["reads"], truth)
    cmp["list_only"] = [c["name"] for c in result["list_only"]]
    cmp["no_card_frames"] = result["no_card_frames"]

    cards = cmp["cards"]
    eff_ok = sum(c["effect_ok"] for c in cards)
    fld_ok = sum(not c["fields_wrong"] for c in cards)
    cand = [c for c in cards if c["auto_approve_candidate"]]
    cand_bad = [c for c in cand if not c["effect_ok"] or c["fields_wrong"]]
    wrong_unflagged = [c for c in cards if (not c["effect_ok"] and "effect_text" not in (c["uncertain_fields"] or []))
                       or any(f not in ("level", "rank", "link_val") and f not in (c["uncertain_fields"] or [])
                              for f in c["fields_wrong"])
                       or (any(f in ("level", "rank", "link_val") for f in c["fields_wrong"])
                           and "level_rank_link" not in (c["uncertain_fields"] or []))]
    for c in cards:
        mark = "OK " if c["effect_ok"] and not c["fields_wrong"] else "NG "
        print(f"{mark}{c['id']} {c['name']}: 効果{'一致' if c['effect_ok'] else '×' + str(c['effect_char_errors']) + '字'}"
              f" 数値{'一致' if not c['fields_wrong'] else json.dumps(c['fields_wrong'], ensure_ascii=False)}"
              f" 突き合わせ={c['card_text_check']} 候補={c['auto_approve_candidate']} 不確か={c['uncertain_fields']}"
              f" 一覧={c['list_check']} 場面={c['frames']}")
        if c["effect_diff"]:
            print(f"     効果の差（正解→取込）: {c['effect_diff']}")
        if c["card_text_diff"]:
            print(f"     字幕→カード画像: {c['card_text_diff']}")
    print(f"\n効果文の一致 {eff_ok}/{len(cards)}・種別と数値の一致 {fld_ok}/{len(cards)}"
          f"・自動承認候補 {len(cand)}枚（うち誤り {len(cand_bad)}）・誤りなのに不確かでない {len(wrong_unflagged)}枚")
    print(f"取込に無い正解: {cmp['missing']} / 正解に無い取込: {cmp['extra']} / 一覧のみ: {cmp['list_only']}"
          f" / カードの無い場面: {cmp['no_card_frames']}")
    print(f"場面ごとの読み違い（まとめる前）: {json.dumps(cmp['frame_errors'], ensure_ascii=False)}")
    usage_in = sum((r.get("usage") or {}).get("input_tokens", 0) for r in result["reads"])
    usage_out = sum((r.get("usage") or {}).get("output_tokens", 0) for r in result["reads"])
    print(f"紹介場面の読み取りのトークン in/out: {usage_in}/{usage_out}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"summary": {"effect_ok": eff_ok, "fields_ok": fld_ok, "cards": len(cards),
                                   "candidates": len(cand), "candidate_errors": len(cand_bad),
                                   "wrong_unflagged": len(wrong_unflagged)},
                       **cmp, "reads": result["reads"]}, f, ensure_ascii=False, indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
