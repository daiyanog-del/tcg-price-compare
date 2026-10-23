"""
tcg_translate.py — TCG先行カードの英語の効果文を日本のOCGの公式文の型で和訳する

仕組み（2026-10-23 設計。経緯は docs/decisions.md）:
  1. 対訳コーパス: YGOResources の公開ミラー（db-ygoresources-com/yugioh-card-history）の
     en/ と ja/ を同じ konami_id で突き合わせた日英の公式文の組（約1万4千枚）。
  2. 似たカードの例: 訳したい英文と言い回しが近いカードの日英の組を TF-IDF で数件選び、例として渡す。
     決まり文句（"also for the rest of this turn" →「このターン、自分は」など）を公式の型に揃えるため。
  3. 名前の対応表: 英文中の "…" が既存のカード名・テーマ名なら日本語の正式名を渡す。
     知らない名前（TCGにしかない新しい名前）は英語のまま「」に入れさせる（推測のカタカナは外れるため）。
  4. 訳した後の点検: 数値と名前が英文と合っているかを機械で照合し、合わない点を warnings に返す。

ミラーは自宅PC・定期実行とも sparse clone（en/ ja/ だけ）で手元に置き、DB には入れない
（YGOResources のキャッシュは DB 容量対策で日本語だけにしているため・ygores_repository._slim_raw）。
"""

import glob
import json
import logging
import math
import os
import re
import unicodedata
from collections import Counter, defaultdict

logger = logging.getLogger(__name__)

DEFAULT_MIRROR_DIR = os.path.join(os.path.expanduser("~"), ".cardsouba", "ygores-mirror")

# 例として渡す似たカードの枚数。TODO: calibrate from data（採点で 4/6/8 を比べて決める）
EXAMPLE_COUNT = 6
# 似たカードの選び方で新しさに足す重み（似ている度合いは0〜1）。TODO: calibrate from data
RECENCY_WEIGHT = 0.15

_EN_QUOTE = re.compile(r'"([^"]+)"')
_JA_QUOTE = re.compile(r"「([^」]+)」")
_WORD = re.compile(r"[a-z0-9']+")


def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


_KANA_KANJI = re.compile(r"[ぁ-んァ-ヶ一-龥]")


def is_japanese(s: str | None) -> bool:
    return bool(_KANA_KANJI.search(s or ""))


def _common_substring(names: list[str]) -> str:
    """全ての名前に共通して含まれる最長の部分文字列（両端の「・」「＝」などの記号は落とす）。"""
    base = min(names, key=len)
    for size in range(len(base), 0, -1):
        for i in range(len(base) - size + 1):
            sub = base[i:i + size]
            if all(sub in n for n in names):
                return sub.strip("・＝=－-　 ")
    return ""


# ──────────────────────────────────────────────
# 対訳コーパス
# ──────────────────────────────────────────────

def load_mirror(mirror_dir: str = DEFAULT_MIRROR_DIR) -> tuple[dict[int, dict], dict[int, dict]]:
    """ミラーの en/ と ja/ を読み、{konami_id: カードJSON} を2つ返す（英語, 日本語）。"""
    def read(lang):
        out, skipped = {}, 0
        for fp in glob.glob(os.path.join(mirror_dir, lang, "*.json")):
            try:
                with open(fp, encoding="utf-8") as f:
                    card = json.load(f)
                out[int(card["id"])] = card
            except (OSError, ValueError, KeyError, TypeError):
                skipped += 1
                continue
        if skipped:
            # 読み飛ばしが多いと「日本語に無い」候補が不当に増える（ミラーの欠け・壊れ）ので、件数を残す
            logger.warning(f"ミラー {lang}/ の読み飛ばし: {skipped}件（読めた{len(out)}件）")
        return out
    return read("en"), read("ja")


class Corpus:
    """日英の公式文の組と、名前の対応表・似たカード検索の索引。

    pool_ids を渡すとその konami_id だけを例・対応表の材料にする（採点で、訳す対象より後のカードを
    混ぜないため）。None なら日英が揃う全カード。
    """

    def __init__(self, en: dict[int, dict], ja: dict[int, dict], pool_ids=None):
        ids = set(en) & set(ja)
        if pool_ids is not None:
            ids &= set(pool_ids)
        # 日本語側の文に英文がそのまま入っているカードがある（例: 23414）。かな・漢字を含む組だけ使う
        self.pairs = {cid: (en[cid], ja[cid]) for cid in sorted(ids)
                      if (en[cid].get("effectText") or "").strip() and is_japanese(ja[cid].get("effectText"))}
        self.card_names = {e["name"]: j["name"] for e, j in self.pairs.values()}
        self.theme_names = self._mine_theme_names()
        # 英語名の単語 → その単語を含む英語のカード名（_theme_from_card_names の候補の絞り込み用）
        self._names_by_word = defaultdict(list)
        for en_name in self.card_names:
            for w in set(_WORD.findall(en_name.lower())):
                self._names_by_word[w].append(en_name)
        self._theme_cache: dict[str, str | None] = {}
        self._build_index()

    def _theme_from_card_names(self, q: str) -> str | None:
        """"…" がカード名の一部（例 "Aleister" ⊂ "Aleister the Invoker"）のとき、その語を含むカード
        2枚以上の日本語名に共通する部分（例「アレイスター」）を返す。見つからなければ None。

        効果文の共起（_mine_theme_names）は "…" が例の側に2回以上出ないと拾えないので、その補い。
        """
        if q in self._theme_cache:
            return self._theme_cache[q]
        words = _WORD.findall(q.lower())
        result = None
        if words:
            pat = re.compile(r"(?<![A-Za-z])" + re.escape(q) + r"(?![A-Za-z])")
            ja_names = [self.card_names[e] for e in self._names_by_word.get(words[0], ()) if pat.search(e)]
            if len(ja_names) >= 2:
                common = _common_substring(ja_names)
                # 2字以下（「・」や1語の助詞など）は偶然の一致が多いので採らない。TODO: calibrate from data
                if len(common) >= 3:
                    result = common
        self._theme_cache[q] = result
        return result

    def _mine_theme_names(self) -> dict[str, str]:
        """カード名以外の "…"（テーマ名など）を、同じカードの「…」との共起で日本語に対応付ける。"""
        co = defaultdict(Counter)
        seen = Counter()
        for e, j in self.pairs.values():
            en_q = set(_EN_QUOTE.findall(e["effectText"])) - set(self.card_names)
            if not en_q:
                continue
            ja_q = set(_JA_QUOTE.findall(j["effectText"]))
            # カード名として対応が分かっている「…」は候補から外す
            ja_q -= {self.card_names[q] for q in _EN_QUOTE.findall(e["effectText"]) if q in self.card_names}
            for q in en_q:
                seen[q] += 1
                for t in ja_q:
                    co[q][t] += 1
        out = {}
        for q, counts in co.items():
            best, n = counts.most_common(1)[0]
            # 2枚以上で、出てくるカードの6割以上に同じ「…」がある時だけ採る。TODO: calibrate from data
            if n >= 2 and n / seen[q] >= 0.6:
                out[q] = best
        return out

    # 似たカードの検索（語の2〜3連の TF-IDF・名前は伏せる）
    @staticmethod
    def _terms(text: str) -> Counter:
        words = _WORD.findall(_EN_QUOTE.sub(" name ", text.lower()))
        return Counter(" ".join(words[i:i + n]) for n in (2, 3) for i in range(len(words) - n + 1))

    def _build_index(self):
        self._ids = list(self.pairs)
        tfs = [self._terms(self.pairs[cid][0]["effectText"]) for cid in self._ids]
        df = Counter(t for tf in tfs for t in tf)
        n = len(tfs)
        self._idf = {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}
        self._vecs = []
        self._postings = defaultdict(list)
        for i, tf in enumerate(tfs):
            vec = {t: c * self._idf[t] for t, c in tf.items()}
            norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
            vec = {t: v / norm for t, v in vec.items()}
            self._vecs.append(vec)
            for t, v in vec.items():
                self._postings[t].append((i, v))

    def similar(self, en_text: str, k: int = EXAMPLE_COUNT, exclude=()) -> list[int]:
        tf = self._terms(en_text)
        q = {t: c * self._idf[t] for t, c in tf.items() if t in self._idf}
        norm = math.sqrt(sum(v * v for v in q.values())) or 1.0
        scores = defaultdict(float)
        for t, w in q.items():
            for i, v in self._postings[t]:
                scores[i] += w / norm * v
        # 新しいカードほど少し優先する（古いカードは「シンクロ召喚」「エクストラデッキ」など今の公式と違う表記のため）。
        # konami_id は作られた順に振られるので新しさの目安に使う。TODO: calibrate from data
        max_id = self._ids[-1] if self._ids else 1
        ex = set(exclude)
        ranked = sorted(scores.items(), key=lambda x: -(x[1] + RECENCY_WEIGHT * self._ids[x[0]] / max_id))
        return [self._ids[i] for i, _ in ranked if self._ids[i] not in ex][:k]

    def glossary_for(self, en_text: str) -> dict[str, str]:
        out = {}
        for q in _EN_QUOTE.findall(en_text):
            if q in self.card_names:
                out[q] = self.card_names[q]
            elif q in self.theme_names:
                out[q] = self.theme_names[q]
            elif self._theme_from_card_names(q):
                out[q] = self._theme_from_card_names(q)
        return out


# ──────────────────────────────────────────────
# 頼み方
# ──────────────────────────────────────────────

SYSTEM_PROMPT = """あなたは遊戯王OCGのカードテキストの翻訳者です。英語版（TCG）の効果文を、日本のOCGの公式カードテキストと同じ書き方の日本語にします。

守ること:
- 日本の公式文の決まった言い回し・語順・記号に揃える。例として渡す「似たカードの英語と日本語の公式文」の書き方を最優先で真似る。英語の文構造をなぞった直訳にしない。
- 効果が複数あるときは公式と同じく①②③…で番号を振り、効果ごとに改行する。「このカード名の①の効果は１ターンに１度しか使用できない。」のような制約文は公式と同じ位置（先頭）に置く。
- 数字・記号は公式と同じ表記（「１体」「１ターンに１度」は全角、攻撃力などの数値も例に合わせる）。
- 今の公式の略し方を使う: Extra Deck→EXデッキ、Synchro/Xyz/Link/Pendulum→S/X/L/P（例:「S召喚」「Lモンスター」「X素材」「Pゾーン」）、Tribute Summon→A召喚。ただし Fusion→融合、Ritual→儀式はそのまま。例に古いカードの長い書き方が混ざっていても、この略し方を優先する。
- 「A」はアドバンス召喚（Tribute Summon）の略だけに使う。召喚以外の Tribute（"Tributed"・"Tribute 1 monster" など）は「リリース」と訳す。
- カード自身の名前は、公式と同じく「このカード名」「このカード」と書く。
- 召喚の書き方は今の公式文に合わせ「〜召喚した場合」「〜召喚された場合」と書く（古いカードの「〜召喚に成功した場合」は使わない）。
- 英語の効果は最後まで全部訳す。途中で「……」などで省略しない。
- 英語の "…" で囲まれた名前は、名前の対応表にあるものはその日本語名を「」に入れる。対応表に無い名前は英語のまま「」に入れる（カタカナにしない）。
- 種族・属性・カードの種類の用語は公式の日本語（例: Thunder→雷族、LIGHT→光属性、Quick-Play Spell→速攻魔法）。
- 英語にある内容を足したり省いたりしない。ただし、日本の公式文では書かない決まった注記（例: "(Quick Effect)" や "(except during the Damage Step)" のうち、公式例で省かれている形）は例に合わせる。
- translation 欄には完成した訳文だけを入れる。説明・前置き・書き直しの断り書きは入れない。"""

# 出力は訳文だけの欄に固定する（10-23 の試しで「おっと、…正しい訳文を以下に示します」が混ざったため）
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"translation": {"type": "string"}},
    "required": ["translation"],
    "additionalProperties": False,
}


def build_user_message(corpus: Corpus, card: dict, *, exclude=(), k: int = EXAMPLE_COUNT,
                       text_key: str = "effectText") -> tuple[str, list[int]]:
    """訳す1枚分の依頼文を作る。戻り値は（依頼文, 例に使った konami_id）。"""
    en_text = card.get(text_key) or ""
    ex_ids = corpus.similar(en_text, k=k, exclude=exclude)
    parts = ["似たカードの英語と日本語の公式文:"]
    for i, cid in enumerate(ex_ids, 1):
        e, j = corpus.pairs[cid]
        parts.append(f"\n[例{i}]\n英語: {e['effectText']}\n日本語: {j['effectText']}")
    gl = corpus.glossary_for(en_text)
    parts.append("\n名前の対応表（英語 → 日本語の正式名）:")
    parts.append("\n".join(f"- {k_} → {v}" for k_, v in gl.items()) if gl else "（該当なし）")
    parts.append(f"\n訳すカード: {card.get('name', '')}（{_card_kind(card)}）\n英語: {en_text}\n日本語:")
    return "\n".join(parts), ex_ids


def _card_kind(card: dict) -> str:
    props = card.get("properties") or []
    if card.get("type") == "monster":
        return "モンスター: " + " / ".join(props)
    return f"{card.get('type')}: {card.get('englishProperty') or card.get('localizedProperty') or ''}"


def request_params(user_message: str, *, model: str, effort: str | None) -> dict:
    """Messages API の引数（Batch の params にもそのまま使う）。"""
    params = {
        "model": model,
        "max_tokens": 8000,
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user_message}],
    }
    params["output_config"] = {"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}}
    if effort:
        params["output_config"]["effort"] = effort
    return params


def parse_message(message) -> str:
    """応答から訳文を取り出す。未完了・形の崩れは例外にする（空の訳を成功扱いにしない）。"""
    if message.stop_reason != "end_turn":
        raise ValueError(f"翻訳の応答が未完了です: {message.stop_reason}")
    raw = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
    text = (json.loads(raw).get("translation") or "").strip()
    if not text:
        raise ValueError("訳文が空です")
    return text


def translate(client, corpus: Corpus, card: dict, *, model: str, effort: str | None = None,
              exclude=(), text_key: str = "effectText") -> dict:
    """1枚を同期で訳す。戻り値: {text, warnings, examples, usage}。"""
    msg, ex_ids = build_user_message(corpus, card, exclude=exclude, text_key=text_key)
    params = request_params(msg, model=model, effort=effort)
    # SDK 0.x は output_config を引数に持たないので extra_body で送る（unreleased_quality.structured_request と同じ）
    output_config = params.pop("output_config")
    message = client.messages.create(**params, extra_body={"output_config": output_config})
    text = parse_message(message)
    return {"text": text, "warnings": check(card.get(text_key) or "", text, corpus, own_name=card.get("name", "")),
            "examples": ex_ids, "usage": usage_dict(message.usage)}


def usage_dict(usage) -> dict:
    return {k: getattr(usage, k, 0) or 0 for k in
            ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}


# 料金（100万トークンあたり USD・2026-10 時点の公式単価）。Batch は半額。
PRICES = {"claude-sonnet-5-5": (2.0, 10.0), "claude-opus-5-5": (4.0, 20.0)}


def cost_usd(usage: dict, model: str, *, batch: bool = False) -> float:
    inp, out = PRICES[model]
    c = (usage["input_tokens"] * inp + usage["cache_creation_input_tokens"] * inp * 1.25
         + usage["cache_read_input_tokens"] * inp * 0.1 + usage["output_tokens"] * out) / 1e6
    return c / 2 if batch else c


# ──────────────────────────────────────────────
# 訳した後の点検
# ──────────────────────────────────────────────

def check(en_text: str, ja_text: str, corpus: Corpus, *, own_name: str = "") -> list[str]:
    """英文と訳文の数値・名前の食い違いを返す（空なら問題なし）。

    カード自身の名前は日本語では「このカード名」と書くので照合しない。
    """
    warnings = []
    if "…" in ja_text and "…" not in en_text and "..." not in en_text:
        warnings.append("訳文が「…」で省略されている")
    # 「1体」「1度」など英語の a/one が数字になるので、英語にある数字が訳に無い時だけ指摘する。
    # 回数は比べない（"ATK 800/DEF 800" は公式では「攻／守８００」と1回にまとめるため）
    ja_nums = set(re.findall(r"\d+", _nfkc(ja_text)))
    for n in sorted(set(re.findall(r"\d+", en_text)) - ja_nums):
        warnings.append(f"数値 {n} が訳文に無い")
    gl = corpus.glossary_for(en_text)
    ja_quotes = set(_JA_QUOTE.findall(ja_text))
    for q in set(_EN_QUOTE.findall(en_text)) - {own_name}:
        want = gl.get(q, q)
        if want not in ja_quotes:
            warnings.append(f"名前「{want}」が訳文に無い")
    return warnings
