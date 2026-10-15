"""効果テキストの拡大読み直し（2回目の読み取り）と、1回目との突き合わせ。

背景: 公式の未発売カード画像はカード全体で約407x593px、効果欄の文字は縦7〜8px＋ふりがなで、
1回目（全体画像を読む）は読めない箇所を定型文で補う作り話をすることがある
（実例 IMPH-JP031: 正「1ターンに1度、融合召喚及び以下の方法でのみ特殊召喚できる」→
抽出「の①の方法での特殊召喚は1ターンに1度しかできない」）。

そこで効果欄だけを切り出して3倍に拡大し、Opus 5.5 に読ませ直し（P2）、1回目（P1相当）と文字単位で
突き合わせる（P3）。食い違い・読めない字（〓）・読み直しの失敗は uncertain_fields に effect_text を入れ、
自動承認の対象から外して人の確認へ回す。評価（tools/eval_effect_text.py、n=30）では、
同じモデルの P1 と P2 の食い違いは誤りのあるカードを取りこぼさなかった（Opus 5.5: 再現率100%・誤警報率0〜14%）。

評価の限界（docs/decisions.md 参照）: n=30・YAC1（再録・特別版）に偏る・ペンデュラム未評価・
短文カードがほぼ無い。ペンデュラムは位置を校正していないため読み直さず、効果欄を常に不確かにする。
"""
import base64
import difflib
import logging
import os
import unicodedata
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# 読み直しに使うモデルと effort（評価と同じ medium）。Opus 5.5 は thinking を無効化できないので指定しない。
REREAD_MODEL = os.environ.get("EFFECT_REREAD_MODEL", "claude-opus-5-5")
REREAD_EFFORT = "medium"
_MAX_TOKENS = 8000
_WORKERS = 4  # TODO: calibrate from data（1カード1回の呼び出し。記事あたり最大30枚）

UNREADABLE = "〓"

REREAD_SYSTEM = """\
あなたは遊戯王OCGのカード画像から文字を書き起こす OCR 担当です。意味を理解して直すのではなく、
画像に見えている文字をそのまま写し取ることが仕事です。

ルール:
1. 効果欄の文字を1文字ずつ、画像に見えるとおりに書き起こす。文として自然になるよう直さない。
2. ふりがな（ルビ。漢字の上に付く小さな仮名）は無視し、本文の漢字だけを書く。
3. 読めない文字・自信が持てない文字は「〓」で置き換える（読めない文字数ぶん「〓」を並べる）。
   「このカード名の①②の効果は…」のような定型文や、あなたが知っているカードの文面を思い出して補うことは禁止。
   見えていない文字を推測で書くより「〓」と書くほうが正しい扱いになる。
4. 括弧・コロン・句読点は全角（「」『』（）：；。、）で書く。数字・丸数字（①②③）・記号は画像どおりに書く。
5. 改行は画像の段落の区切り（効果ごと）だけ \\n で表す。文の途中の折り返しは改行にしない。
6. 【】で囲まれた種別の行（モンスターの【種族／種類／…】、【魔法カード】【罠カード】など）は書かない。それ以外の効果欄の本文
   （素材指定の行、儀式・融合の指定、フレーバーテキストを含む）はすべて書く。
7. ペンデュラム効果欄が写っていても、書き起こすのはカード最下部のモンスター効果欄（effect_text）だけにする。
8. 効果欄の文字を1文字でも「〓」にした場合は unreadable を true にする。それ以外は false。
"""

REREAD_USER_TEXT = ("この画像はカード下部の効果欄を切り出して拡大したものです。"
                    "効果欄の本文を、ルールに従って書き起こしてください（カード名はここには写っていません）。")

REREAD_SCHEMA = {"type": "object", "properties": {
    "effect_text": {"type": "string"}, "unreadable": {"type": "boolean"}}}


def norm(text: str) -> str:
    """突き合わせ用の正規化: NFKC・空白改行除去。丸数字（①〜⑳）は NFKC で数字になるため除外して保持する。"""
    out = []
    for ch in text or "":
        out.append(ch if "①" <= ch <= "⑳" else unicodedata.normalize("NFKC", ch))
    t = "".join(out).replace("〜", "~").replace("～", "~")
    return "".join(t.split())


def diff_ops(a: str, b: str) -> list[list[str]]:
    """a（1回目）→ b（読み直し）の文字単位の差分。[[tag, a部分, b部分], ...]（tag: equal/replace/delete/insert）。"""
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return [[tag, a[i1:i2], b[j1:j2]] for tag, i1, i2, j1, j2 in sm.get_opcodes()]


def diff_summary(a: str, b: str, limit: int = 400) -> str:
    """食い違い箇所だけの短い要約。表記: 前後の一致部分(最大6字)＋[-1回目のみ-]{+読み直しのみ+}。"""
    ops = diff_ops(a, b)
    parts = []
    for i, (tag, x, y) in enumerate(ops):
        if tag == "equal":
            continue
        before = ops[i - 1][1][-6:] if i > 0 else ""
        after = ops[i + 1][1][:6] if i + 1 < len(ops) else ""
        parts.append(f"{before}[-{x}-]{{+{y}+}}{after}")
    text=" / ".join(parts)
    return text if len(text) <= limit else text[:limit] + "…"


def _mark_uncertain(raw: dict, *fields: str) -> None:
    current = raw.get("uncertain_fields")
    current = list(current) if isinstance(current, list) else []
    for f in fields:
        if f not in current:
            current.append(f)
    raw["uncertain_fields"] = current


def _read_one(client, model, crop_png: bytes):
    import unreleased_quality as quality
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": base64.standard_b64encode(crop_png).decode("ascii")}},
        {"type": "text", "text": REREAD_USER_TEXT},
    ]
    return quality.structured_request(client, model=model, system=REREAD_SYSTEM, content=content,
                                      schema=REREAD_SCHEMA, max_tokens=_MAX_TOKENS, effort=REREAD_EFFORT)


def reread_effect_texts(rows: list[dict], *, client, encoded_images: list[dict], model: str | None = None) -> None:
    """各カードの効果欄を拡大して読み直し、effect_text を採用して1回目と突き合わせる（rows を直接更新）。

    - 1回目の effect_text は extraction_raw.effect_text_first_pass に残す
    - 結果と比較情報は extraction_raw.effect_text_reread {text, model, agreed, diff_summary, usage, ...}
    - 食い違い・〓・失敗・ペンデュラムは uncertain_fields に effect_text（ペンデュラムは pendulum_effect も）を追加する
    - 失敗時は1回目の結果を残す（黙って通さない）
    """
    from card_crop import make_effect_crop, effect_box_kind
    from unreleased_extractor import normalize_effect_punct
    model = model or REREAD_MODEL
    images = {image["url"]: image for image in encoded_images}

    jobs = []  # (row, crop_png)
    for row in rows:
        raw = row["extraction_raw"]
        raw["effect_text_first_pass"] = row.get("effect_text", "")
        card_type = row.get("card_type", "")
        if "ペンデュラム" in card_type:
            # 切り出し位置を校正・評価していない種別。読み直さず人の確認へ回す。
            raw["effect_text_reread"] = {"skipped": "pendulum_not_evaluated", "agreed": False, "model": model}
            _mark_uncertain(raw, "effect_text", "pendulum_effect")
            continue
        try:
            image = images.get(raw.get("source_image_url"))
            if not image:
                raise ValueError("カード画像がありません")
            if effect_box_kind(card_type) is None:
                raise ValueError(f"カード種別から効果欄の位置を決められません: {card_type!r}")
            suggestion = raw.get("crop_suggestion")
            if suggestion is None and raw.get("source") != "official_html":
                # X画像などカード以外（パック宣材）を含む画像は、枠が無いと効果欄の位置を決められない
                raise ValueError("カード枠候補がありません")
            crop = make_effect_crop(base64.b64decode(image["data"], validate=True), card_type, suggestion)
        except Exception as exc:  # noqa: BLE001 — 失敗は記録して不確かにする
            _fail(row, model, f"{type(exc).__name__}: {exc}")
            continue
        jobs.append((row, crop))

    def work(job):
        row, crop = job
        try:
            return row, _read_one(client, model, crop), None
        except Exception as exc:  # noqa: BLE001 — API エラー・未完了応答・JSON 不正など
            return row, None, exc

    with ThreadPoolExecutor(max_workers=_WORKERS) as ex:
        results = list(ex.map(work, jobs))

    for row, result, exc in results:
        if exc is not None:
            _fail(row, model, f"{type(exc).__name__}: {str(exc)[:300]}")
            continue
        data, _, usage = result
        text = normalize_effect_punct((data.get("effect_text") or "").strip())
        first = row.get("effect_text", "")
        raw = row["extraction_raw"]
        if not text and first:
            # 空で返ったのに1回目には文がある: 読めたとは言えないので採用しない
            _fail(row, model, "読み直しの結果が空です", usage=usage)
            continue
        a, b = norm(first), norm(text)
        has_unreadable = UNREADABLE in text or bool(data.get("unreadable"))
        agreed = a == b and not has_unreadable
        raw["effect_text_reread"] = {
            "text": text, "model": model, "effort": REREAD_EFFORT, "agreed": agreed,
            "unreadable": has_unreadable, "diff_summary": "" if a == b else diff_summary(a, b), "usage": usage,
        }
        row["effect_text"] = text
        if not agreed:
            _mark_uncertain(raw, "effect_text")
            logger.warning("効果テキストが1回目と食い違い/〓あり: %s", row.get("name"))


def _fail(row: dict, model: str, reason: str, usage: dict | None = None) -> None:
    raw = row["extraction_raw"]
    logger.warning("効果テキストの読み直しに失敗（1回目の結果を残して不確かにする）: %s: %s", row.get("name"), reason)
    raw["effect_text_reread"] = {"error": reason, "model": model, "agreed": False, **({"usage": usage} if usage else {})}
    _mark_uncertain(raw, "effect_text")


def admin_view(raw: dict) -> dict | None:
    """管理画面に出す「1回目と読み直しの食い違い」の表示用データ。一致した・旧データ（読み直し前）は None。

    texts は比較のため norm() をかけた文字列（空白・改行を除き全角半角をそろえた形）。
    ops は [[tag, 1回目の部分, 読み直しの部分], ...]（tag: equal / replace / delete / insert）。
    """
    info = raw.get("effect_text_reread") if isinstance(raw, dict) else None
    if not isinstance(info, dict) or info.get("agreed") is not False:
        return None
    if info.get("skipped"):
        return {"status": "skipped", "reason": str(info["skipped"])}
    if info.get("error"):
        return {"status": "error", "reason": str(info["error"])[:300]}
    first, text = norm(raw.get("effect_text_first_pass") or ""), norm(info.get("text") or "")
    return {"status": "disagree", "first": first, "reread": text, "ops": diff_ops(first, text),
            "unreadable": bool(info.get("unreadable")), "model": info.get("model")}
