"""
text_check_calibrate.py — 効果テキスト誤字検証（unreleased_text_check.py）の誤検知率の計測

  (a) 公式コーパス自身を leave-one-out（そのカードを辞書から除いた状態）で検査して、
      「正しい文章に疑義が付く率」＝誤検知率を出す。さらに、公式テキストに似た字の置換を1字入れて、
      強疑義で見つけられる率（検出率）と、置換前の字を候補に出せた率を出す。
  (b) unreleased_cards の status が approved/linked（人が確認済み）の全件を検査し、
      疑義件数の分布と具体例を出す。
  (c) 語の組み合わせ（kind="unknown_phrase"）の計測（2026-10-15）:
      (c-1) 公式コーパス leave-one-out で、規則ごとの誤検知率（カード単位）
      (c-2) 公式テキストの動詞を別の動詞に差し替えた擬似誤り（言い回しの誤り）の検出率と、
            文字3連鎖の強い疑義だけでは見つからなかった分を言い回しの照合がどれだけ追加で拾うか
      (c-3) id 529「墓地へ送って発動」を「墓地へ返って発動」に戻した版の検出

使い方（Supabase は読むだけ）:
  python tools/text_check_calibrate.py [--sample 3000] [--seed 0] [--examples 15]

認証: SUPABASE_URL / SUPABASE_KEY（未設定なら ~/.cardsouba/watcher.env から補完）
"""

import io
import os
import random
import sys
from collections import Counter

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import unreleased_text_check as tc  # noqa: E402
from tools.build_text_check_dict import _load_env_fallback, fetch_corpus, _QUOTE_RE  # noqa: E402

_PAGE = 200


class _LooNgrams:
    """文書頻度つきの n-gram 集合。注目中のカード分を差し引いて「知っているか」を答える。"""

    def __init__(self, df: Counter):
        self.df = df
        self.own: set = set()

    def __contains__(self, g):
        return self.df.get(g, 0) - (1 if g in self.own else 0) > 0


class LooPhrase(tc.PhraseTable):
    """leave-one-out 用の語の組み合わせ表。注目中のカード（own_triples）の寄与を引いて数える。"""

    def __init__(self, full: tc.PhraseTable):
        super().__init__(full.verbs, full.pairs, full.nps, full.triples)
        self.set_own(set())

    def set_own(self, triples: set):
        self.own_triples = triples
        self._own_verbs = {v for _, _, v in triples}
        self._own_pairs = {(p, v) for _, p, v in triples}
        self._own_nps = {(n, p) for n, p, _ in triples}

    def verb_count(self, v):
        return self.verbs.get(v, 0) - (1 if v in self._own_verbs else 0)

    def pair_count(self, p, v):
        return self.pairs.get((p, v), 0) - (1 if (p, v) in self._own_pairs else 0)

    def np_count(self, n, p):
        return self.nps.get((n, p), 0) - (1 if (n, p) in self._own_nps else 0)

    def triple_count(self, n, p, v):
        return self.triples.get((n, p, v), 0) - (1 if (n, p, v) in self.own_triples else 0)


class LooDict(tc.TextCheckDict):
    """leave-one-out 用。注目中のカードの寄与（n-gram・カード名・「」内語）を除いて照合する。"""

    def __init__(self, ngram_df: Counter, word_df: Counter, words_raw: dict, phrase=None):
        # 親の __init__ は使わず、必要な属性だけ用意する
        self.ngrams = _LooNgrams(ngram_df)
        self.phrase = phrase
        self.meta = {}
        self.word_df = word_df
        self.own_keys: set = set()
        self.words = words_raw
        self._by_len = {}
        for k in words_raw:
            self._by_len.setdefault(len(k), []).append(k)

    def _alive(self, key) -> bool:
        return self.word_df.get(key, 0) - (1 if key in self.own_keys else 0) > 0

    def knows_name(self, key):
        return self._alive(key)

    def near_words(self, key, max_dist, limit=3):
        res = super().near_words(key, max_dist, limit + 8)
        return [(w, d) for w, d in res if self._alive(tc.name_key(w))][:limit]


def build_loo(corpus):
    ngram_df: Counter = Counter()
    word_df: Counter = Counter()
    words_raw: dict = {}
    per_card = []
    phrase_sets = []
    for c in corpus:
        grams: set = set()
        keys: set = set()
        triples: set = set()
        if c["name"]:
            keys.add(tc.name_key(c["name"]))
            words_raw.setdefault(tc.name_key(c["name"]), c["name"])
        for t in c["texts"]:
            for m in _QUOTE_RE.finditer(t):
                w = m.group(1).strip()
                if w and len(w) <= 40:
                    keys.add(tc.name_key(w))
                    words_raw.setdefault(tc.name_key(w), w)
            if tc.is_effect_like(t):
                grams |= tc.ngram_set(tc.token_string(tc.tokenize(t, c["name"])))
                triples |= tc.phrase_triples(t, c["name"])
        ngram_df.update(grams)
        word_df.update(keys)
        per_card.append((grams, keys, triples))
        phrase_sets.append(triples)
    return ngram_df, word_df, words_raw, per_card, tc.PhraseTable.from_triple_sets(phrase_sets)


def _card_of(c) -> dict:
    return {"name": c["name"], "effect_text": c["texts"][0] if c["texts"] else "",
            "pendulum_effect": c["texts"][1] if len(c["texts"]) > 1 else ""}


def run_loo(corpus, sample: int, seed: int, n_examples: int):
    ngram_df, word_df, words_raw, per_card, phrase_full = build_loo(corpus)
    d = LooDict(ngram_df, word_df, words_raw, LooPhrase(phrase_full))
    rnd = random.Random(seed)
    idx = [i for i, c in enumerate(corpus) if any(tc.is_effect_like(t) for t in c["texts"])]
    idx = rnd.sample(idx, min(sample, len(idx)))
    strong_cards = weak_cards = 0
    cards_by_kind: Counter = Counter()   # (種別, 強弱) → その疑義が付いたカード数
    phrase_alone = Counter()             # 言い回しの照合を単体で実行したときの、強/弱が付いたカード数（重なり除去の前）
    by_kind: Counter = Counter()
    examples = []
    for i in idx:
        c = corpus[i]
        d.ngrams.own, d.own_keys, own_triples = per_card[i]
        d.phrase.set_own(own_triples)
        res = tc.check_card(_card_of(c), dictionary=d)
        for key in {(s["kind"], s["severity"]) for s in res["suspects"]}:
            cards_by_kind[key] += 1
        alone = [x for t in c["texts"] if tc.is_effect_like(t) for x in tc._check_phrases(t, d, c["name"])]
        for sev in {x["severity"] for x in alone}:
            phrase_alone[sev] += 1
        if res["strong"]:
            strong_cards += 1
            for s in res["suspects"]:
                if s["severity"] == tc.SEVERITY_STRONG:
                    by_kind[s["kind"]] += 1
                    if len(examples) < n_examples:
                        examples.append((c["name"], s["kind"], s["message"], s["context"]))
        if res["weak"]:
            weak_cards += 1
    n = len(idx)
    print(f"=== (a-1) 公式コーパス leave-one-out 誤検知率（{n}枚サンプル）===")
    print(f"強い疑義が付いた: {strong_cards}枚 ({strong_cards / n:.1%})   種別（件数）: {dict(by_kind)}")
    print(f"弱い疑義が付いた: {weak_cards}枚 ({weak_cards / n:.1%})")
    print("種別×強弱ごとに疑義が付いたカード数（誤検知率）:")
    for (kind, sev), cnt in sorted(cards_by_kind.items()):
        print(f"  {kind}/{sev}: {cnt}枚 ({cnt / n:.2%})")
    print(f"言い回しの照合を単体で実行（文字3連鎖の強と重なっても数える）: 強 {phrase_alone['strong']}枚 "
          f"({phrase_alone['strong'] / n:.2%}) / 弱 {phrase_alone['weak']}枚 ({phrase_alone['weak'] / n:.2%})")
    print("強い疑義の誤検知例（公式の正しい文章）:")
    for name, kind, msg, ctx in examples:
        print(f"  - {name} [{kind}] {msg} / …{ctx}…")
    return d, per_card, idx


def run_injection(corpus, d, per_card, idx, seed: int):
    """公式テキストに似た字の置換を1字入れ、強疑義で見つかるか・元の字を候補に出せるかを見る。"""
    rnd = random.Random(seed + 1)
    tried = found = suggested = 0
    for i in idx:
        c = corpus[i]
        t = next((x for x in c["texts"] if tc.is_effect_like(x)), None)
        if not t:
            continue
        masked = tc._mask_quotes(t)
        cand = [p for p, ch in enumerate(t) if ch in tc.CONFUSABLES and masked[p] != tc._Q]
        if not cand:
            continue
        p = rnd.choice(cand)
        alt = rnd.choice(tc.CONFUSABLES[t[p]])
        bad = t[:p] + alt + t[p + 1:]
        d.ngrams.own, d.own_keys, own_triples = per_card[i]
        d.phrase.set_own(own_triples)
        res = tc.check_card({"name": c["name"], "effect_text": bad}, dictionary=d)
        tried += 1
        hits = [s for s in res["suspects"] if s["kind"] == "unknown_ngram" and s["start"] <= p < s["end"]]
        if any(s["severity"] == tc.SEVERITY_STRONG for s in hits):
            found += 1
        if any(s["suggestion"] == t[p] for s in hits):
            suggested += 1
    print()
    print(f"=== (a-2) 似た字の置換を1字入れた擬似誤字の検出率（{tried}件）===")
    print(f"強い疑義で位置を捉えた: {found}件 ({found / max(tried, 1):.1%})")
    print(f"元の字を候補（suggestion）に出せた: {suggested}件 ({suggested / max(tried, 1):.1%})")
    print("※ 置換は表にある似た字のみ。実際の誤字傾向を網羅した数字ではありません。")


def run_phrase_injection(corpus, d, per_card, idx, seed: int):
    """公式テキストの動詞を、別の動詞（公式に実在する動詞語幹）に差し替えた擬似「言い回しの誤り」の検出率。

    差し替え後の文章は文字の並びとしては自然なことが多く、文字3連鎖の照合では見つからないものが出る。
    その分を言い回しの照合が追加で拾えるかを見る。差し替え先は語幹の実在語なので「別の正しい動詞が入った」
    ケース（言い回しの誤りの上限寄りの想定）。字の形の誤読（返→送 など）は (a-2) と (c-3) で見る。
    """
    rnd = random.Random(seed + 2)
    vocab = sorted(d.phrase.verbs)
    tried = ngram_strong = phrase_strong = phrase_any = either_strong = added = 0
    for i in idx:
        c = corpus[i]
        t = next((x for x in c["texts"] if tc.is_effect_like(x)), None)
        if not t:
            continue
        toks = tc.tokenize(t, c["name"])
        s = tc.token_string(toks)
        occ = tc.phrase_occurrences(s)
        if not occ:
            continue
        noun, p, v, a, b = rnd.choice(occ)
        alt = rnd.choice([x for x in vocab if x != v and d.phrase.verbs[x] >= 5])
        start, end = toks[a][1], toks[b - 1][2]
        bad = t[:start] + alt + t[end:]
        d.ngrams.own, d.own_keys, own_triples = per_card[i]
        d.phrase.set_own(own_triples)
        res = tc.check_card({"name": c["name"], "effect_text": bad}, dictionary=d)
        lo, hi = start, start + len(alt)
        near = [x for x in res["suspects"] if x["start"] < hi + 1 and lo - 1 < x["end"]]
        ng = any(x["kind"] == "unknown_ngram" and x["severity"] == "strong" for x in near)
        # 重なり除去で④の強が落ちるので、④は単体でも数える
        ph_only = tc._check_phrases(bad, d, c["name"])
        ph_near = [x for x in ph_only if x["start"] < hi + 1 and lo - 1 < x["end"]]
        ph_s = any(x["severity"] == "strong" for x in ph_near)
        ph_a = bool(ph_near)
        tried += 1
        ngram_strong += ng
        phrase_strong += ph_s
        phrase_any += ph_a
        either_strong += (ng or ph_s)
        added += (ph_s and not ng)
    print()
    print(f"=== (c-2) 動詞を別の動詞に差し替えた擬似誤りの検出率（{tried}件）===")
    print(f"文字3連鎖の強い疑義で捉えた:            {ngram_strong}件 ({ngram_strong / max(tried, 1):.1%})")
    print(f"言い回しの強い疑義で捉えた（単体）:        {phrase_strong}件 ({phrase_strong / max(tried, 1):.1%})")
    print(f"言い回しの強弱いずれかで捉えた（単体）:    {phrase_any}件 ({phrase_any / max(tried, 1):.1%})")
    print(f"どちらかの強で捉えた:                    {either_strong}件 ({either_strong / max(tried, 1):.1%})")
    print(f"言い回しの強が追加で拾った（3連鎖では未検出）: {added}件 ({added / max(tried, 1):.1%})")
    print("※ 差し替え先は公式に実在する動詞語幹。実際の誤りの傾向を網羅した数字ではありません。")


def run_case_529(rows):
    """id 529（ケペルの金字塔）の「墓地へ送って発動」を「墓地へ返って発動」に戻した版が検出できるか。"""
    d = tc.load_dict()
    r = next((x for x in rows if x["id"] == 529), None)
    if d is None or r is None:
        print("(c-3) id 529 が取得できないので実行できません")
        return
    bad = dict(r, effect_text=r["effect_text"].replace("墓地へ送って発動", "墓地へ返って発動"))
    print()
    print("=== (c-3) id 529「墓地へ返って発動」に戻した版 ===")
    for label, card in (("現行テキスト", r), ("戻した版", bad)):
        res = tc.check_card(card, dictionary=d)
        print(f"{label}: 強{res['strong']}件・弱{res['weak']}件")
        for sus in res["suspects"]:
            print(f"   [{sus['severity']}/{sus['kind']}] 「{sus['text']}」 候補={sus['suggestion']} / {sus['message']}")
    only = tc._check_phrases(bad["effect_text"], d, bad["name"])
    print("言い回しの照合だけを単体で実行した結果:")
    for sus in only:
        print(f"   [{sus['severity']}/{sus['kind']}] 「{sus['text']}」 候補={sus['suggestion']} / {sus['message']}")


def fetch_unreleased(sb):
    rows, off = [], 0
    while True:
        b = (sb.table("unreleased_cards").select("id, name, status, effect_text, pendulum_effect")
             .order("id").range(off, off + _PAGE - 1).execute().data or [])
        rows.extend(b)
        if len(b) < _PAGE:
            return rows
        off += _PAGE


def _summarize(label, approved, results, n_examples, show_examples):
    n = len(approved)
    dist_strong: Counter = Counter()
    dist_weak: Counter = Counter()
    kinds_strong: Counter = Counter()
    kinds_weak: Counter = Counter()
    unverified = 0
    lines = []
    for r, res in zip(approved, results):
        dist_strong[res["strong"]] += 1
        dist_weak[res["weak"]] += 1
        if res["unverified_fields"]:
            unverified += 1
        for s in res["suspects"]:
            (kinds_strong if s["severity"] == "strong" else kinds_weak)[s["kind"]] += 1
            lines.append((r["id"], r["name"], s))
    sc = sum(v for k, v in dist_strong.items() if k > 0)
    wc = sum(v for k, v in dist_weak.items() if k > 0)
    any_c = len({lid for lid, _, _ in lines})
    print()
    print(f"=== (b) 人が確認済み（approved/linked）{n}件の検査 [{label}] ===")
    print(f"強い疑義が付いた: {sc}件 ({sc / max(n, 1):.1%})  件数分布: {dict(sorted(dist_strong.items()))}")
    print(f"弱い疑義が付いた: {wc}件 ({wc / max(n, 1):.1%})  件数分布: {dict(sorted(dist_weak.items()))}")
    print(f"強弱いずれか:     {any_c}件 ({any_c / max(n, 1):.1%})")
    print(f"フレーバー等で n-gram 照合できなかった(未検証欄あり): {unverified}件")
    print(f"強の種別: {dict(kinds_strong)} / 弱の種別: {dict(kinds_weak)}")
    if show_examples:
        print("疑義の具体例（強→弱の順）:")
        lines.sort(key=lambda x: (x[2]["severity"] != "strong", x[0]))
        for cid, name, s in lines[:n_examples * 3]:
            print(f"  - id={cid} {name} [{s['severity']}/{s['kind']}/{s['field']}] 「{s['text']}」"
                  f" 候補={s['suggestion']} / …{s['context']}… / {s['message']}")


def run_approved(rows, n_examples: int):
    """承認済み/紐付け済みを検査する。3通りの条件で比べる:
      文脈なし / 他の全未発売カードを文脈にする(上限) / 自分より前に取り込まれたカードだけを文脈にする(取込時に近い)
    """
    approved = [r for r in rows if r["status"] in ("approved", "linked")]
    live = [r for r in rows if r["status"] != "rejected"]
    names_all = [r["name"] for r in live if r.get("name")]
    dictionary = tc.load_dict()
    if dictionary is None:
        print("辞書 data/text_check_dict.json.gz が無いので (b) は実行できません")
        return

    def names_except(r):
        return [x for x in names_all if x != r["name"]]

    plain = [tc.check_card(r, extra_names=names_except(r), dictionary=dictionary) for r in approved]
    _summarize("文脈なし(辞書のみ)", approved, plain, n_examples, False)

    ctx_all = tc.TextContext()
    for r in live:
        ctx_all.add_card(r)
    full = [tc.check_card(r, extra_names=names_except(r), dictionary=dictionary, context=ctx_all) for r in approved]
    _summarize("他の全未発売カードの「」内語を既知扱い(上限)", approved, full, n_examples, False)

    # 取込時の実態: id の小さい順に、自分より前のカードだけを文脈にする
    ctx_seq = tc.TextContext()
    seq_results = {}
    for r in sorted(live, key=lambda x: x["id"]):
        if r["status"] in ("approved", "linked"):
            prior = [x["name"] for x in live if x["id"] < r["id"] and x.get("name") and x["name"] != r["name"]]
            seq_results[r["id"]] = tc.check_card(r, extra_names=prior, dictionary=dictionary, context=ctx_seq,
                                                  context_has_self=False)
        ctx_seq.add_card(r)
    seq = [seq_results[r["id"]] for r in approved]
    _summarize("自分より前に取り込まれたカードの「」内語だけを既知扱い(取込時に近い)", approved, seq, n_examples, True)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    def opt(name, default):
        return type(default)(argv[argv.index(name) + 1]) if name in argv else default

    sample, seed, n_ex = opt("--sample", 3000), opt("--seed", 0), opt("--examples", 15)
    _load_env_fallback()
    if not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY")):
        print("SUPABASE_URL / SUPABASE_KEY が未設定です")
        return 1
    from supabase import create_client
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    corpus = fetch_corpus(sb)
    d, per_card, idx = run_loo(corpus, sample, seed, n_ex)
    run_injection(corpus, d, per_card, idx, seed)
    run_phrase_injection(corpus, d, per_card, idx, seed)
    rows = fetch_unreleased(sb)
    run_case_529(rows)
    run_approved(rows, n_ex)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
