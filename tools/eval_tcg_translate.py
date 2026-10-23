"""
TCG先行カードの和訳（tcg_translate.py）の採点

日英の両方が公式に出ているカードのうち、モデルの知識より後のもの（konami_id >= CUTOFF_ID）を
英語だけから訳し、公式の日本語文と比べる。例・名前の対応表には CUTOFF_ID より前のカードだけを使う
（訳す対象と同じテーマの日本語名が例から漏れないように。本番の「日本にまだ無いテーマ」に近い条件）。

使い方:
    python tools/eval_tcg_translate.py --split dev --limit 10 --model claude-sonnet-5-5 --tag s55-try1
    python tools/eval_tcg_translate.py --split dev --model claude-sonnet-5-5 --batch --tag s55-dev1
    python tools/eval_tcg_translate.py --rescore <tag>      # 保存済みの訳を採点し直す（API を使わない）

ANTHROPIC_API_KEY は環境変数か ~/.cardsouba/watcher.env から読む。結果は ~/.cardsouba/tcg_eval/<tag>.jsonl。
"""

import argparse
import difflib
import json
import os
import random
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tcg_translate as tt  # noqa: E402

# モデルの知識の区切り（2026-06）より後に作られたカードの konami_id の下限。
# BLAZING DOMINION（TCG 2026-05）が 22510 台、CHAOS ORIGINS（TCG 2026-07）が 22960 台。
CUTOFF_ID = 22900
DEV_SIZE = 100
OUT_DIR = os.path.join(os.path.expanduser("~"), ".cardsouba", "tcg_eval")


def _api_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    path = os.path.join(os.path.expanduser("~"), ".cardsouba", "watcher.env")
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("ANTHROPIC_API_KEY が見つかりません")


def eval_cards(en, ja):
    ids = sorted(cid for cid in set(en) & set(ja) if cid >= CUTOFF_ID
                 and (en[cid].get("effectText") or "").strip()
                 and "Normal" not in (en[cid].get("properties") or []))  # 通常モンスターの説明文は除く
    random.Random(0).shuffle(ids)
    # 日本語欄に英文が入っているカードは採点できないので、分けた後で外す（分け方を変えず、版ごとの点数を比べられるように）
    ok = lambda cid: tt.is_japanese(ja[cid].get("effectText"))  # noqa: E731
    return {"dev": sorted(c for c in ids[:DEV_SIZE] if ok(c)), "test": sorted(c for c in ids[DEV_SIZE:] if ok(c))}


# ──────────────────────────────────────────────
# 採点
# ──────────────────────────────────────────────

def _norm(s):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or ""))


def _mask(s):
    return re.sub(r"「[^」]*」", "「X」", s)


def score_one(hyp, ref, en_text, corpus):
    h, r = _norm(hyp), _norm(ref)
    gl = corpus.glossary_for(en_text)
    ref_q = set(tt._JA_QUOTE.findall(ref))
    hyp_q = set(tt._JA_QUOTE.findall(hyp))
    known = [v for v in set(gl.values())]
    return {
        "exact": h == r,
        "sim": difflib.SequenceMatcher(None, h, r).ratio(),
        "masked_exact": _mask(h) == _mask(r),
        "masked_sim": difflib.SequenceMatcher(None, _mask(h), _mask(r)).ratio(),
        # 対応表で分かる名前が正しく入ったか（分からない新しい名前は英語のまま残す決まりなので数えない）
        "known_names": len(known),
        "known_names_ok": sum(1 for v in known if v in hyp_q and v in ref_q),
    }


def summarize(rows, model, batch):
    n = len(rows)
    if not n:
        return "0件"
    agg = lambda k: sum(r["score"][k] for r in rows) / n  # noqa: E731
    kn = sum(r["score"]["known_names"] for r in rows)
    ok = sum(r["score"]["known_names_ok"] for r in rows)
    usage = {k: sum(r["usage"][k] for r in rows) for k in rows[0]["usage"]}
    cost = tt.cost_usd(usage, model, batch=batch)
    return (f"{n}枚  完全一致 {agg('exact'):.0%}  類似度 {agg('sim'):.3f}  "
            f"名前を伏せた完全一致 {agg('masked_exact'):.0%}  名前を伏せた類似度 {agg('masked_sim'):.3f}  "
            f"対応表の名前 {ok}/{kn}  点検で指摘あり {sum(1 for r in rows if r['warnings'])}枚\n"
            f"トークン 入力{usage['input_tokens']:,} 出力{usage['output_tokens']:,} "
            f"(キャッシュ書込{usage['cache_creation_input_tokens']:,}/読込{usage['cache_read_input_tokens']:,})  "
            f"費用 ${cost:.3f}（1枚 ${cost / n:.4f}{'・Batch半額込み' if batch else ''}）")


# ──────────────────────────────────────────────
# 実行
# ──────────────────────────────────────────────

def run_sync(client, corpus, en, ids, model, effort):
    def one(cid):
        for attempt in range(3):
            try:
                return cid, tt.translate(client, corpus, en[cid], model=model, effort=effort, exclude={cid})
            except Exception as e:  # 一時失敗は2回まで取り直す。最後は失敗として残す
                if attempt == 2:
                    return cid, {"error": repr(e)}
                time.sleep(5)
    with ThreadPoolExecutor(max_workers=4) as ex:
        return dict(ex.map(one, ids))


def run_batch(client, corpus, en, ids, model, effort):
    reqs, examples = [], {}
    for cid in ids:
        msg, ex_ids = tt.build_user_message(corpus, en[cid], exclude={cid})
        examples[cid] = ex_ids
        reqs.append({"custom_id": str(cid), "params": tt.request_params(msg, model=model, effort=effort)})
    batch = client.beta.messages.batches.create(requests=reqs)
    print(f"Batch {batch.id} を送信（{len(reqs)}件）。終わるまで待ちます", flush=True)
    while True:
        b = client.beta.messages.batches.retrieve(batch.id)
        if b.processing_status == "ended":
            break
        time.sleep(30)
    out = {}
    for item in client.beta.messages.batches.results(batch.id):
        cid = int(item.custom_id)
        if item.result.type != "succeeded":
            out[cid] = {"error": item.result.type}
            continue
        m = item.result.message
        try:
            text = tt.parse_message(m)
        except ValueError as e:
            out[cid] = {"error": str(e)}
            continue
        out[cid] = {"text": text, "warnings": tt.check(en[cid]["effectText"], text, corpus, own_name=en[cid]["name"]),
                    "examples": examples[cid], "usage": tt.usage_dict(m.usage)}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test"], default="dev")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--effort", default=None)
    ap.add_argument("--batch", action="store_true")
    ap.add_argument("--tag")
    ap.add_argument("--rescore")
    ap.add_argument("--mirror", default=tt.DEFAULT_MIRROR_DIR)
    a = ap.parse_args()

    en, ja = tt.load_mirror(a.mirror)
    corpus = tt.Corpus(en, ja, pool_ids=[cid for cid in en if cid < CUTOFF_ID])
    os.makedirs(OUT_DIR, exist_ok=True)

    if a.rescore:
        path = os.path.join(OUT_DIR, f"{a.rescore}.jsonl")
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        meta = rows[0].get("meta", {})
        for r in rows:
            r["score"] = score_one(r["hyp"], ja[r["cid"]]["effectText"], en[r["cid"]]["effectText"], corpus)
        print(summarize(rows, meta.get("model", a.model), meta.get("batch", False)))
        return

    ids = eval_cards(en, ja)[a.split][: a.limit]
    import anthropic
    client = anthropic.Anthropic(api_key=_api_key(), timeout=300.0, max_retries=2)
    results = (run_batch if a.batch else run_sync)(client, corpus, en, ids, a.model, a.effort)

    rows, failed = [], []
    for cid in ids:
        res = results.get(cid) or {"error": "missing"}
        if "error" in res:
            failed.append((cid, res["error"]))
            continue
        rows.append({"cid": cid, "name": en[cid]["name"], "en": en[cid]["effectText"], "hyp": res["text"],
                     "ref": ja[cid]["effectText"], "warnings": res["warnings"], "examples": res["examples"],
                     "usage": res["usage"],
                     "score": score_one(res["text"], ja[cid]["effectText"], en[cid]["effectText"], corpus),
                     "meta": {"model": a.model, "effort": a.effort, "batch": a.batch, "split": a.split}})
    tag = a.tag or f"{a.model}-{a.split}-{int(time.time())}"
    with open(os.path.join(OUT_DIR, f"{tag}.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(summarize(rows, a.model, a.batch))
    if failed:
        print(f"失敗 {len(failed)}件: {failed[:5]}")
    print(f"保存: {os.path.join(OUT_DIR, tag + '.jsonl')}")


if __name__ == "__main__":
    main()
