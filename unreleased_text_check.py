"""未発売カードの効果テキストの誤字検証（決定的・AI不使用）。

2026-10-15 追加。対象は effect_text と pendulum_effect。
お手本は ygores_cards の日本語公式テキスト（約14,000枚）。辞書ファイル data/text_check_dict.json.gz
（tools/build_text_check_dict.py で生成）を読み、次の3つの検査をする。

  ① n-gram 照合（kind="unknown_ngram"）
     公式コーパスに一度も現れない3文字連鎖を疑う。「」内・カード名・数字・丸数字は伏せ字にして比べる。
     見た目の似た字（カ/力 など）に置換すると既知になる場合は suggestion に入れる。
  ② 「」内の名前（kind="unknown_name"）
     既存カード名・既存の「」内語・他の未発売カード名に無い名前を疑い、近い既存語を候補にする。
  ③ 書式（kind="format"）
     連番の飛び、括弧の不対応、半角記号の混入、連続句読点、文末の欠け など。
  ④ 語の組み合わせ（kind="unknown_phrase"）
     「名詞＋助詞（を/へ/に/から/で/として）＋動詞」の組み合わせを公式コーパスと比べる。字の形の誤読ではなく
     言い回しそのものが違う誤り（例: 「墓地へ返って」は正しくは「墓地へ送って」）は、文字3連鎖では見つからないため。
     形態素解析器は使わず、正規表現（助詞の直後の漢字連続＋その直後のひらがなで動詞と判定）と、公式コーパスから
     数えた出現数の表（辞書の phrase）だけで判定する。

強弱（severity）:
  strong = 誤検知が少ないと実測で確認できた疑義。自動承認の条件に使う。
  weak   = 参考表示のみ。自動承認の条件には使わない。
  閾値の根拠と実測値は docs/decisions.md（2026-10-15 未発売テキスト検証）と tools/text_check_calibrate.py を参照。

辞書が無い・読めないときは「未検証」（status="unchecked"）を返す。黙って疑義なし扱いにはしない。
Flask / Supabase に依存しない（取込 Cron・自宅PC・管理画面のどこからでも import できる）。
"""

import gzip
import json
import logging
import os
import re
import threading

logger = logging.getLogger(__name__)

# 2: 語の組み合わせ（unknown_phrase）を追加（2026-10-15）
VERSION = 2

# 辞書の既定位置（リポジトリ直下の data/）
DICT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "text_check_dict.json.gz")

# n-gram の長さ。実データ（公式14,655テキスト）での leave-one-out 実測で決定:
#   n=2: 異常の検出力が低い / n=3: 強疑義の誤検知1.5%（採用） / n=4: 4.0〜2.5%と上がり辞書も2倍
# 根拠の数字は docs/decisions.md 参照。
NGRAM_N = 3

SEVERITY_STRONG = "strong"
SEVERITY_WEAK = "weak"

# 伏せ字（私用領域の文字。公式テキストには現れない）
_Q = ""      # 「」の中身・カード名自身
_DIG = ""    # 数字の連続

# 効果文らしさの判定。通常モンスターのフレーバーテキスト（散文）は公式効果文のコーパスで
# 照合できないため n-gram 照合の対象から外す（未検証として扱う）。
_EFFECT_WORDS = re.compile(r"発動|できる|できない|特殊召喚|破壊|墓地|効果|手札|フィールド|場合|ターン|ダメージ")

_FULLWIDTH_LATIN_DIGIT = {
    ord(c): chr(ord(c) - 0xFEE0)
    for c in "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ０１２３４５６７８９"
}
_DIGITS = set("0123456789０１２３４５６７８９")
_CIRCLED = set("①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳")
_SKIP_CHARS = set("\n\r 　\t")

# ──────────────────────────────────────────────
# 見た目・音の似た字（OCR の読み違い候補）
# ──────────────────────────────────────────────
# 同じ並びを「どちらにも置換しうる」対称表。実際の誤字傾向がわかれば足す。
_CONFUSABLE_PAIRS = [
    "カ力", "ト卜", "ロ口", "エ工", "ニ二", "タ夕", "ハ八", "へヘ", "ぺペ", "べベ", "ソン", "シツ",
    "ー一", "ー－", "一－", "ー―", "－―", "トㇳ", "マム", "リり", "ノ丿", "ミ三", "ホ木",
    "0O", "0〇", "O〇", "ケヶ",
    # 濁点・半濁点違い（ハ/バ/パ など）
    "ハバ", "ハパ", "バパ", "ヒビ", "ヒピ", "ビピ", "フブ", "フプ", "ブプ", "ヘベ", "ヘペ", "ホボ", "ホポ", "ボポ",
    "カガ", "キギ", "クグ", "ケゲ", "コゴ", "サザ", "シジ", "スズ", "セゼ", "ソゾ", "タダ", "チヂ", "ツヅ", "テデ", "トド",
    "ウヴ",
    # 漢字（効果文に頻出する字の読み違い）。2026-10-15 の実測で承認済みカードに「墓地」→「基地」が複数あった
    "墓基", "墓幕", "墓募", "己已", "己巳", "末未", "日目", "目白", "土士", "干千", "人入", "力刀", "刀刃",
    "攻政", "効功", "持待", "札礼", "除徐", "破波", "喚換", "戻戾", "属嘱",
]


def _build_confusables() -> dict:
    table: dict = {}
    for pair in _CONFUSABLE_PAIRS:
        chars = list(pair)
        for a in chars:
            for b in chars:
                if a != b:
                    table.setdefault(a, set()).add(b)
    return {k: sorted(v) for k, v in table.items()}


CONFUSABLES = _build_confusables()


# ──────────────────────────────────────────────
# トークン化（比較用の正規化）
# ──────────────────────────────────────────────

def is_effect_like(text: str) -> bool:
    """公式効果文らしい文章か。散文（フレーバー）・英文は False。"""
    if not text or not _EFFECT_WORDS.search(text):
        return False
    latin = len(re.findall(r"[A-Za-z]", text))
    return latin < len(text) * 0.3


def tokenize(text: str, card_name: str = ""):
    """テキストを比較用トークン列にする。戻り値: [(文字, 元の開始位置, 元の終了位置), ...]

    - 改行・空白は読み飛ばす（改行の有無で連鎖が変わらないようにする）
    - 「」の中身は伏せ字1つ（中身は②で別に検査する）
    - カード名自身（「」なしの出現）も伏せ字
    - 数字の連続は伏せ字1つ（全角/半角の違いも吸収）、丸数字は ① に統一
    - 全角英字は半角にする
    """
    tokens = []
    i, n = 0, len(text)
    name = (card_name or "").strip()
    while i < n:
        ch = text[i]
        if ch in _SKIP_CHARS:
            i += 1
            continue
        if ch == "「":
            close = text.find("」", i + 1)
            inner = text[i + 1:close] if close != -1 else None
            if close != -1 and "「" not in inner:
                tokens.append(("「", i, i + 1))
                if inner:
                    tokens.append((_Q, i + 1, close))
                tokens.append(("」", close, close + 1))
                i = close + 1
                continue
        if len(name) >= 2 and text.startswith(name, i):
            tokens.append((_Q, i, i + len(name)))
            i += len(name)
            continue
        if ch in _DIGITS:
            j = i
            while j < n and text[j] in _DIGITS:
                j += 1
            tokens.append((_DIG, i, j))
            i = j
            continue
        if ch in _CIRCLED:
            tokens.append(("①", i, i + 1))
        else:
            tokens.append((ch.translate(_FULLWIDTH_LATIN_DIGIT), i, i + 1))
        i += 1
    return tokens


def token_string(tokens) -> str:
    return "".join(t[0] for t in tokens)


def ngram_set(s: str, n: int = NGRAM_N) -> set:
    return {s[i:i + n] for i in range(len(s) - n + 1)}


# ──────────────────────────────────────────────
# 辞書
# ──────────────────────────────────────────────

def name_key(s: str) -> str:
    """カード名・「」内語の比較用キー。全角英数を半角にし、空白を除く。"""
    return re.sub(r"[\s　]+", "", (s or "").translate(_FULLWIDTH_LATIN_DIGIT)).lower()


class TextCheckDict:
    """読み込み済みの辞書。"""

    def __init__(self, ngrams, names, quote_words, meta=None, phrase=None):
        self.ngrams = set(ngrams)
        self.meta = meta or {}
        # 語の組み合わせの出現数表（PhraseTable）。None のときは④の検査をしない
        self.phrase = phrase
        # キー → 表示用の元の語。近い語の探索用に長さごとの索引も持つ
        self.words: dict = {}
        for w in list(names) + list(quote_words):
            k = name_key(w)
            if k and k not in self.words:
                self.words[k] = w
        self._by_len: dict = {}
        for k in self.words:
            self._by_len.setdefault(len(k), []).append(k)

    def knows_name(self, key: str) -> bool:
        return key in self.words

    def near_words(self, key: str, max_dist: int, limit: int = 3):
        """編集距離 max_dist 以内の既存語（近い順）。[(表示語, 距離)]"""
        found = []
        kset = set(key)
        for ln in range(max(1, len(key) - max_dist), len(key) + max_dist + 1):
            for cand in self._by_len.get(ln, ()):
                if cand == key:
                    continue
                # 共通する文字が少なすぎる語は距離が大きいので先に落とす
                if len(kset & set(cand)) < min(len(kset), len(set(cand))) - max_dist:
                    continue
                d = _bounded_edit_distance(key, cand, max_dist)
                if d is not None:
                    found.append((d, self.words[cand]))
        found.sort(key=lambda x: (x[0], x[1]))
        return [(w, d) for d, w in found[:limit]]


def _bounded_edit_distance(a: str, b: str, max_dist: int):
    """編集距離（挿入・削除・置換が各1）。max_dist を超えたら None。"""
    if abs(len(a) - len(b)) > max_dist:
        return None
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        row_min = i
        for j, cb in enumerate(b, 1):
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            cur.append(v)
            if v < row_min:
                row_min = v
        if row_min > max_dist:
            return None
        prev = cur
    return prev[-1] if prev[-1] <= max_dist else None


_cache_lock = threading.Lock()
_cache = {"path": None, "mtime": None, "dict": None}


def load_dict(path: str | None = None):
    """辞書を読む。無い・壊れているときは None（呼び出し側が「未検証」として扱う）。"""
    path = path or DICT_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    with _cache_lock:
        if _cache["path"] == path and _cache["mtime"] == mtime and _cache["dict"] is not None:
            return _cache["dict"]
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                data = json.load(f)
            d = TextCheckDict(
                data["ngrams"].split("\n") if isinstance(data["ngrams"], str) else data["ngrams"],
                data.get("card_names", []), data.get("quote_words", []), data.get("meta"),
                PhraseTable.from_json(data["phrase"]) if data.get("phrase") else None,
            )
            if d.phrase is None:
                logger.warning("[text_check] 辞書に語の組み合わせの表（phrase）が無い。④の検査をしない"
                               "（tools/build_text_check_dict.py で作り直すこと）")
            if data.get("meta", {}).get("n") not in (None, NGRAM_N):
                raise ValueError(f"辞書の n={data['meta'].get('n')} が NGRAM_N={NGRAM_N} と違う")
        except Exception as e:
            logger.warning("[text_check] 辞書を読めない（未検証として扱う）: %s: %s", path, e)
            return None
        _cache.update(path=path, mtime=mtime, dict=d)
        return d


# ──────────────────────────────────────────────
# ① n-gram 照合
# ──────────────────────────────────────────────

def _suspect(kind, severity, text, start, end, message, suggestion=None, context=None, context_start=None):
    """疑義1件。context は前後の文脈、context_start はその文脈が元テキストの何文字目から始まるか
    （表示側が start/end の位置で強調するため。同じ文字列が文脈に複数あっても正しい箇所を示せる）。"""
    if context is None:
        context_start = max(0, start - 4)
        context = text[context_start:end + 4]
    return {
        "kind": kind, "severity": severity, "start": start, "end": end, "text": text[start:end],
        "suggestion": suggestion, "message": message,
        "context": context, "context_start": context_start,
    }


def _check_ngrams(text: str, d: TextCheckDict, card_name: str) -> list:
    tokens = tokenize(text, card_name)
    s = token_string(tokens)
    n = NGRAM_N
    if len(s) < n:
        return []
    known = [s[k:k + n] in d.ngrams for k in range(len(s) - n + 1)]
    if all(known):
        return []

    def cover(j):
        return range(max(0, j - n + 1), min(j, len(s) - n) + 1)

    # 強: 周囲のどの3文字連鎖も未知の文字位置（誤字1字なら、その字だけがここに当たる）。
    #     連鎖が2つ以上かぶる位置に限る（文頭・文末の1連鎖だけの位置は弱に回す）
    dead = [j for j in range(len(s)) if len(cover(j)) >= 2 and not any(known[k] for k in cover(j))]
    dead_set = set(dead)

    regions = []   # (tok_start, tok_end_exclusive, severity)
    run = []
    for j in dead + [None]:
        if run and (j is None or j != run[-1] + 1):
            regions.append((run[0], run[-1] + 1, SEVERITY_STRONG))
            run = []
        if j is not None:
            run.append(j)
    # 弱: 強の位置に触れていない未知連鎖。かぶる連鎖はまとめる
    wk, cur = [], None
    for k, ok in enumerate(known):
        if ok:
            continue
        if any(p in dead_set for p in range(k, k + n)):
            continue
        if cur and k <= cur[1] - 1:   # 前の連鎖と文字がかぶる
            cur[1] = k + n
        else:
            if cur:
                wk.append(tuple(cur))
            cur = [k, k + n]
    if cur:
        wk.append(tuple(cur))
    regions.extend((a, b, SEVERITY_WEAK) for a, b in wk)
    regions.sort()

    out = []
    for a, b, sev in regions:
        suggestion = _suggest_confusable(s, known, a, b, d)
        start, end = tokens[a][1], tokens[b - 1][2]
        ctx_a, ctx_b = max(0, a - 3), min(len(tokens), b + 3)
        context_start = tokens[ctx_a][1]
        context = text[context_start:tokens[ctx_b - 1][2]]
        msg = ("公式の効果文に例のない文字の並びです" if sev == SEVERITY_STRONG
               else "公式の効果文にあまり例のない文字の並びです（参考）")
        if suggestion:
            msg += f"。「{suggestion['from']}」→「{suggestion['to']}」の読み違いの可能性"
        out.append(_suspect("unknown_ngram", sev, text, start, end, msg,
                            suggestion["replaced"] if suggestion else None, context, context_start))
    return out


def _suggest_confusable(s: str, known: list, a: int, b: int, d: TextCheckDict):
    """区間 [a,b) 内の1字を似た字に替えると、区間にかかる連鎖がすべて既知になるものを探す。"""
    n = NGRAM_N
    lo, hi = max(0, a - n + 1), min(len(s) - n, b - 1)
    for p in range(a, b):
        for alt in CONFUSABLES.get(s[p], ()):
            t = s[:p] + alt + s[p + 1:]
            if all(t[k:k + n] in d.ngrams for k in range(max(0, p - n + 1), min(p, len(s) - n) + 1)):
                # 区間内の他の未知連鎖も直るか（1字置換で直らない区間は提案しない）
                if all(t[k:k + n] in d.ngrams for k in range(lo, hi + 1)):
                    return {"from": s[p], "to": alt, "replaced": alt, "pos": p}
    return None


# ──────────────────────────────────────────────
# ② 「」内の名前
# ──────────────────────────────────────────────

_QUOTE_RE = re.compile(r"「([^「」]+)」")

# 近い既存語を探す編集距離の上限。短い語は1違いでも別の語になりやすいので1にする
def _max_edit_distance(key: str) -> int:
    return 1 if len(key) <= 5 else 2


def _check_names(text: str, d: TextCheckDict, card_name: str, extra_names, all_text_for_count: str,
                 recur_word=None) -> list:
    out = []
    own_key = name_key(card_name)
    extra_keys = {name_key(x) for x in (extra_names or ()) if x}
    seen = {}
    for m in _QUOTE_RE.finditer(text):
        seen.setdefault(name_key(m.group(1)), []).append(m)
    for key, ms in seen.items():
        if not key or len(key) > 40:
            continue
        if d.knows_name(key) or key in extra_keys or (recur_word is not None and recur_word(key)):
            continue
        # 新しい語の許容（根拠）: そのカード自身の名前、名前の一部、同じカードのテキストで2回以上
        # 出る語は、新テーマ名・新カード名の可能性が高く、誤字なら繰り返し同じ誤字になりにくいので
        # 新語として許容する。ただし既存語に近い場合だけ弱い疑義にする。
        is_local_new = (
            key == own_key or (own_key and key in own_key)
            or key.endswith("トークン")   # トークン名はカードごとに作られる（実測: 公式の誤検知の大半がこれ）
            or len(ms) >= 2
            or len(re.findall(re.escape(key), name_key(all_text_for_count))) >= 2
        )
        near = d.near_words(key, _max_edit_distance(key)) if len(key) >= 3 else []
        m = ms[0]
        start, end = m.start(1), m.end(1)
        if near:
            best = near[0][0]
            sev = SEVERITY_WEAK if is_local_new else SEVERITY_STRONG
            out.append(_suspect(
                "unknown_name", sev, text, start, end,
                f"「{m.group(1)}」は既存のカード名・語にありません。近い語: "
                + "、".join(f"「{w}」" for w, _ in near),
                best))
        elif not is_local_new:
            out.append(_suspect(
                "unknown_name", SEVERITY_WEAK, text, start, end,
                f"「{m.group(1)}」は既存のカード名・語になく、近い語も見つかりません（新カード名の可能性）"))
    out.sort(key=lambda x: x["start"])
    return out


# ──────────────────────────────────────────────
# ③ 書式
# ──────────────────────────────────────────────

_ASCII_PUNCT_FIX = {":": "：", ";": "；", ",": "、", "(": "（", ")": "）", "!": "！", "?": "？"}
_TERMINAL_OK = "。！？）」』…"


def _mask_quotes(text: str) -> str:
    """「」の中身を同じ長さの伏せ字にする（位置を保つ）。名前内の記号は書式検査の対象外。"""
    return _QUOTE_RE.sub(lambda m: "「" + _Q * len(m.group(1)) + "」", text)


def _check_format(text: str) -> list:
    out = []
    q = _mask_quotes(text)

    def add(sev, start, end, msg, suggestion=None):
        out.append(_suspect("format", sev, text, start, end, msg, suggestion))

    # 括弧の対応（「」『』（））
    for o, c, label in (("「", "」", "「」"), ("『", "』", "『』"), ("（", "）", "（）")):
        depth, first_open = 0, None
        for i, ch in enumerate(q):
            if ch == o:
                if depth == 0:
                    first_open = i
                depth += 1
            elif ch == c:
                if depth == 0:
                    add(SEVERITY_STRONG, i, i + 1, f"{label}の閉じ側に対応する開き側がありません")
                else:
                    depth -= 1
        if depth > 0:
            add(SEVERITY_STRONG, first_open, first_open + 1, f"{label}の開き側に対応する閉じ側がありません")

    # 半角記号の混入（「」の外）。「：」の半角混入もここ
    for m in re.finditer(r"[:;,()!?]", q):
        fix = _ASCII_PUNCT_FIX[m.group()]
        add(SEVERITY_STRONG, m.start(), m.end(), f"半角の「{m.group()}」が混じっています（公式表記は全角「{fix}」）", fix)
    for m in re.finditer(r"[ｦ-ﾟ]+", q):
        add(SEVERITY_STRONG, m.start(), m.end(), "半角カナが混じっています")
    for m in re.finditer(r"[【】〈〉《》\[\]<>“”\"']", q):
        add(SEVERITY_STRONG, m.start(), m.end(), f"公式の効果文では使わない記号「{m.group()}」があります")
    # 空白（公式の効果文にはほぼ無い。実測は decisions.md）
    for m in re.finditer(r"[ 　]+", q):
        add(SEVERITY_WEAK, m.start(), m.end(), "空白が入っています")
    # 連続句読点
    for m in re.finditer(r"[。、]{2,}", q):
        add(SEVERITY_STRONG, m.start(), m.end(), "句読点が連続しています")
    # 数字の全角/半角が混在（公式は全角、未発売の抽出結果は半角が多いので「混在」だけを疑う）
    if re.search(r"[0-9]", q) and re.search(r"[０-９]", q):
        m = re.search(r"[0-9]+", q)
        add(SEVERITY_WEAK, m.start(), m.end(), "半角数字と全角数字が混在しています")

    # 丸数字の連番
    defs = [(m.start(), ord(m.group(1)) - 0x2460 + 1) for m in re.finditer(r"([①-⑳])：", q)]
    nums = [v for _, v in defs]
    seen = set()
    for idx, (pos, v) in enumerate(defs):
        if v in seen:
            add(SEVERITY_STRONG, pos, pos + 1, f"番号「{q[pos]}」が重複しています")
        elif v != idx + 1 and idx + 1 not in seen:
            add(SEVERITY_STRONG, pos, pos + 1, f"番号が飛んでいます（{idx + 1}番目の効果が「{q[pos]}」）")
        seen.add(v)
    top = max(nums) if nums else 0
    for m in re.finditer(r"[①-⑳](?!：)", q):
        v = ord(m.group()) - 0x2460 + 1
        if v > top:
            add(SEVERITY_STRONG, m.start(), m.end(), f"「{m.group()}」の効果の定義（「{m.group()}：」）がありません")
    # 文頭の丸数字なのに「：」が続かない（読み落とし）
    for m in re.finditer(r"(?:^|(?<=[。\n]))[①-⑳](?!：)", q):
        add(SEVERITY_WEAK, m.start(), m.end(), "文頭の番号の後に「：」がありません")

    # 文末が欠けている（抽出が途中で切れた疑い）
    stripped = text.rstrip()
    if stripped and stripped[-1] not in _TERMINAL_OK:
        add(SEVERITY_STRONG, len(stripped) - 1, len(stripped), "文末が句点などで終わっていません（読み取りが途中で切れた可能性）")

    out.sort(key=lambda x: (x["start"], x["end"]))
    return out


# ──────────────────────────────────────────────
# ④ 語の組み合わせ（名詞＋助詞＋動詞）
# ──────────────────────────────────────────────

# 助詞（長いものを先に）。「と」「が」「は」「の」などは動詞との組み合わせが安定しないので対象にしない
_PHRASE_NOUN_RE = re.compile(r"([一-鿿々ァ-ヴー]+|」)(として|から|を|へ|に|で)")
# 助詞の直後の「漢字の連続（動詞の語幹）＋ひらがな」。ひらがなが助詞なら名詞（例: 墓地へ）で、動詞ではない
_PHRASE_VERB_RE = re.compile(r"([一-鿿]{1,6})([ぁ-ん])([ぁ-ん]?)")
# 名詞の末尾を何字で切るか（長い複合語・カタカナ語は末尾だけで比べて表を小さく保つ）
_PHRASE_NOUN_TAIL = 4

# 強い疑義にする条件。いずれも2026-10-15の公式コーパス leave-one-out 実測（tools/text_check_calibrate.py）で
# 誤検知率を確認して決めた。根拠の数字は docs/decisions.md（2026-10-15 言い回しの照合）
# TODO: calibrate from data
PHRASE_MIN_VERB_FOR_PAIR = 20     # 「動詞は頻出なのに、その助詞との組が0」を強にする動詞の出現数の下限
PHRASE_WEAK_MIN_NP = 50           # 弱: 名詞＋助詞の出現数の下限
PHRASE_WEAK_MIN_VERB = 20         # 弱: 動詞の出現数の下限
PHRASE_WEAK_MIN_PAIR = 5          # 弱: 助詞＋動詞の出現数の下限
# 言い回しの照合をする文章の長さの上限（字）。これを超える文章（異常に長い抽出結果・貼り付けの暴走など）は
# 照合せず、弱い注記だけを出す。公式の効果文の最長は約1,000字台なので十分余裕がある
# TODO: calibrate from data
PHRASE_MAX_TEXT_LEN = 3000


def _is_verb_follow(h1: str, h2: str) -> bool:
    """動詞語幹の直後のひらがな（h1, h2）が活用語尾なら True、助詞（名詞だった）なら False。"""
    if h1 in "をへにとがはのもや":
        return False
    if h1 == "で" and h2 != "き":      # 「で」は助詞。「できる」「でき」は動詞（使用できる）
        return False
    if h1 == "か" and h2 == "ら":      # 「から」は助詞。「置かれ」などの「か」は活用
        return False
    return True


def phrase_occurrences(s: str):
    """トークン文字列 s から (名詞末尾, 助詞, 動詞語幹, 動詞の開始位置, 動詞の終了位置) を全て返す。位置は s の添字。"""
    out = []
    for m in _PHRASE_NOUN_RE.finditer(s):
        v = _PHRASE_VERB_RE.match(s, m.end())
        if not v or not _is_verb_follow(v.group(2), v.group(3)):
            continue
        noun = m.group(1)
        if noun != "」":
            noun = noun[-_PHRASE_NOUN_TAIL:]
        out.append((noun, m.group(2), v.group(1), v.start(1), v.end(1)))
    return out


def phrase_triples(text: str, card_name: str = "") -> set:
    """テキスト1つの (名詞末尾, 助詞, 動詞語幹) の集合（辞書づくり用）。"""
    return {(n, p, v) for n, p, v, _, _ in phrase_occurrences(token_string(tokenize(text, card_name)))}


class PhraseTable:
    """公式コーパスでの語の組み合わせの出現数（カード単位。同じカードで何度出ても1）。

    verbs: 動詞語幹 → 数 / pairs: (助詞, 動詞) → 数 / nps: (名詞末尾, 助詞) → 数 / triples: (名詞末尾, 助詞, 動詞) → 数
    """

    def __init__(self, verbs=None, pairs=None, nps=None, triples=None):
        self.verbs = verbs or {}
        self.pairs = pairs or {}
        self.nps = nps or {}
        self.triples = triples or {}
        self._top: dict = {}
        for (n, p, v), c in self.triples.items():
            self._top.setdefault((n, p), []).append((c, v))
        for k in self._top:
            self._top[k].sort(key=lambda x: (-x[0], x[1]))

    # 件数の取得（校正ツールの leave-one-out 版がここを差し替える）
    def verb_count(self, v):
        return self.verbs.get(v, 0)

    def pair_count(self, p, v):
        return self.pairs.get((p, v), 0)

    def np_count(self, n, p):
        return self.nps.get((n, p), 0)

    def triple_count(self, n, p, v):
        return self.triples.get((n, p, v), 0)

    def top_verbs(self, n, p, limit=3):
        """その名詞＋助詞の後に公式でよく使われる動詞（多い順）。"""
        return [v for c, v in self._top.get((n, p), [])[:limit] if self.verb_count(v) > 0]

    @classmethod
    def from_triple_sets(cls, triple_sets):
        """カードごとの (名詞, 助詞, 動詞) の集合のリストから数える。"""
        verbs, pairs, nps, triples = {}, {}, {}, {}
        for ts in triple_sets:
            for t in ts:
                triples[t] = triples.get(t, 0) + 1
            for v in {v for _, _, v in ts}:
                verbs[v] = verbs.get(v, 0) + 1
            for pv in {(p, v) for _, p, v in ts}:
                pairs[pv] = pairs.get(pv, 0) + 1
            for np_ in {(n, p) for n, p, _ in ts}:
                nps[np_] = nps.get(np_, 0) + 1
        return cls(verbs, pairs, nps, triples)

    def to_json(self) -> dict:
        """辞書ファイル用。キーは区切り「|」で連結（名詞末尾・助詞・動詞語幹には「|」が現れない）。"""
        return {
            "verbs": dict(sorted(self.verbs.items())),
            "pairs": {"|".join(k): c for k, c in sorted(self.pairs.items())},
            "nps": {"|".join(k): c for k, c in sorted(self.nps.items())},
            "triples": {"|".join(k): c for k, c in sorted(self.triples.items())},
        }

    @classmethod
    def from_json(cls, data: dict):
        def split(d):
            return {tuple(k.split("|")): c for k, c in (d or {}).items()}
        return cls(dict(data.get("verbs") or {}), split(data.get("pairs")), split(data.get("nps")),
                   split(data.get("triples")))


def _check_phrases(text: str, d: TextCheckDict, card_name: str) -> list:
    """④: 公式に例のない動詞・助詞との組み合わせ・名詞との組み合わせを疑義にする。

    強: 動詞語幹が公式の「助詞の後の動詞」に1度も現れない（例: 「墓地へ返って」の「返」）、または
        動詞自体は頻出なのにその助詞と組み合わさった例が0（例: 「墓地を送って」の を＋送）。
    弱: 名詞＋助詞も助詞＋動詞も動詞も十分に頻出なのに、3つの組み合わせだけが公式に無い（参考表示）。
    """
    ph = d.phrase
    if ph is None:
        return []
    tokens = tokenize(text, card_name)
    s = token_string(tokens)
    out = []
    for noun, p, v, a, b in phrase_occurrences(s):
        vc = ph.verb_count(v)
        pc = ph.pair_count(p, v)
        sev = msg = None
        if vc == 0:
            sev = SEVERITY_STRONG
            msg = f"「{v}」は公式の効果文で「{p}」の後に使われる動詞の例がありません（言い回しの誤りの可能性）"
        elif pc == 0 and vc >= PHRASE_MIN_VERB_FOR_PAIR:
            sev = SEVERITY_STRONG
            msg = f"「{p}{v}」という組み合わせは公式の効果文に例がありません（言い回しの誤りの可能性）"
        elif (ph.triple_count(noun, p, v) == 0 and ph.np_count(noun, p) >= PHRASE_WEAK_MIN_NP
              and vc >= PHRASE_WEAK_MIN_VERB and pc >= PHRASE_WEAK_MIN_PAIR):
            sev = SEVERITY_WEAK
            msg = f"「{noun}{p}{v}…」という言い回しは公式の効果文にあまり例がありません（参考）"
        if sev is None:
            continue
        suggestion = None
        alts = ph.top_verbs(noun, p) if sev == SEVERITY_STRONG else []
        if alts:
            suggestion = alts[0]
            msg += "。この後に公式でよく使われる動詞: " + "、".join(f"「{x}」" for x in alts)
        start, end = tokens[a][1], tokens[b - 1][2]
        ctx_a, ctx_b = max(0, a - 8), min(len(tokens), b + 4)
        context_start = tokens[ctx_a][1]
        context = text[context_start:tokens[ctx_b - 1][2]]
        out.append(_suspect("unknown_phrase", sev, text, start, end, msg, suggestion, context, context_start))
    return out


# ──────────────────────────────────────────────
# 公開API
# ──────────────────────────────────────────────

def check_text(text: str, *, card_name: str = "", extra_names=(), dictionary=None, name_text: str = "",
               recur_word=None) -> list:
    """1つのテキストを検査して疑義のリストを返す。辞書が無いときは呼ばないこと（check_card を使う）。

    card_name: そのカード自身の名前（伏せ字・新語許容に使う）。
    extra_names: 辞書に無いが既知として扱う名前（同じ弾の他の未発売カード名など）。
    name_text: 「2回以上出る語」の数え上げに足す同じカードの別テキスト（例: 効果とP効果）。
    recur_word: 他の未発売カードの「」内にも出る語か（TextContext 由来）。あれば既知の語として扱う。
    """
    if not text or not text.strip():
        return []
    d = dictionary if dictionary is not None else load_dict()
    if d is None:
        raise RuntimeError("辞書が無いため検査できません（check_card を使うと未検証として返ります）")
    out = _check_format(text)
    if is_effect_like(text):
        out += _check_ngrams(text, d, card_name)
        # 同じ場所を文字3連鎖の強い疑義が既に指しているときは、同じ誤りの二重計上を避けて④の強は出さない
        covered = [(x["start"], x["end"]) for x in out if x["severity"] == SEVERITY_STRONG]
        if len(text) > PHRASE_MAX_TEXT_LEN:
            out.append(_suspect("text_too_long", SEVERITY_WEAK, text, 0, 1,
                                f"効果テキストが{len(text)}字と長いため、言い回しの照合をしていません（参考）"))
        for x in ([] if len(text) > PHRASE_MAX_TEXT_LEN else _check_phrases(text, d, card_name)):
            if x["severity"] == SEVERITY_STRONG and any(x["start"] < e and s0 < x["end"] for s0, e in covered):
                continue
            out.append(x)
    out += _check_names(text, d, card_name, extra_names, text + name_text, recur_word)
    out.sort(key=lambda x: (x["start"], x["end"]))
    return out


_FIELDS = ("effect_text", "pendulum_effect")


class TextContext:
    """他の未発売カードの「」内語の文書頻度。

    新弾の新しいカード名・テーマ名は同じ弾の複数のカードに出るので、②で既知の語として扱う材料にする。
    check_card には自分自身の寄与を除いて渡す（自分のテキストで自分を許さない）。

    n-gram（①）には使わない。実測（2026-10-15）で、承認済みカードに紛れていた読み違い
    「墓地」→「基地」は同じ弾の複数のカードに繰り返し出ており、「他のカードにも出る＝新語」と
    みなして弱に下げると本物の誤字を隠してしまうため。
    """

    def __init__(self):
        self.word_df: dict = {}

    @staticmethod
    def contrib(card: dict) -> set:
        """そのカード1枚の「」内語キーの集合。"""
        keys = set()
        for f in _FIELDS:
            t = card.get(f) or ""
            keys |= {name_key(m.group(1)) for m in _QUOTE_RE.finditer(t)}
        return keys

    def add_card(self, card: dict) -> None:
        for k in self.contrib(card):
            self.word_df[k] = self.word_df.get(k, 0) + 1

    def remove_card(self, card: dict) -> None:
        """add_card で足したカードを取り除く（編集前の登録内容を除いて再検査するときに使う）。"""
        for k in self.contrib(card):
            if self.word_df.get(k, 0) > 0:
                self.word_df[k] -= 1


def check_card(card: dict, *, extra_names=(), dictionary=None, context: "TextContext | None" = None,
               context_has_self: bool = True) -> dict:
    """カード行（unreleased_cards 形式）の効果テキストを検査する。

    戻り値:
      {"version", "status": "checked"|"unchecked", "reason": 未検証の理由（checked なら None）,
       "strong": 件数, "weak": 件数, "suspects": [{..., "field": "effect_text"|"pendulum_effect"}],
       "unverified_fields": n-gram 照合できなかった欄（フレーバー等）}
    status が "unchecked" のときは疑義なしの意味ではない。
    context: 他の未発売カードの TextContext（省略可）。
    context_has_self: context にこのカード自身が入っているか。入っている（DB 登録済みのカード）なら
        自分の寄与を引いて使う。取込前の新しい行は入っていないので False にする。
    """
    d = dictionary if dictionary is not None else load_dict()
    base = {"version": VERSION, "strong": 0, "weak": 0, "suspects": [], "unverified_fields": []}
    if d is None:
        return {**base, "status": "unchecked", "reason": "dict_missing"}
    name = card.get("name") or ""
    texts = {f: (card.get(f) or "") for f in _FIELDS}
    recur_word = None
    if context is not None:
        own_k = context.contrib(card) if context_has_self else set()
        recur_word = lambda k: context.word_df.get(k, 0) - (1 if k in own_k else 0) > 0  # noqa: E731
    suspects, unverified = [], []
    for f in _FIELDS:
        t = texts[f]
        if not t.strip():
            continue
        other = " ".join(texts[g] for g in _FIELDS if g != f)
        if not is_effect_like(t):
            unverified.append(f)
        for s in check_text(t, card_name=name, extra_names=extra_names, dictionary=d, name_text=other + " " + name, recur_word=recur_word):
            s["field"] = f
            suspects.append(s)
    if not any(t.strip() for t in texts.values()):
        # 調べる文章が1つも無い（魔法・罠・モンスターを問わず空）。疑義なしの意味ではないので未検証で返す
        return {**base, "status": "unchecked", "reason": "empty"}
    return {
        **base,
        "status": "checked",
        "reason": None,
        "strong": sum(1 for s in suspects if s["severity"] == SEVERITY_STRONG),
        "weak": sum(1 for s in suspects if s["severity"] == SEVERITY_WEAK),
        "suspects": suspects,
        "unverified_fields": unverified,
    }
