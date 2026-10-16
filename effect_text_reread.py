"""効果テキストの拡大読み直し（2回目の読み取り）と、1回目との突き合わせ。

背景: 公式の未発売カード画像はカード全体で約407x593px、効果欄の文字は縦7〜8px＋ふりがなで、
1回目（全体画像を読む）は読めない箇所を定型文で補う作り話をすることがある
（実例 IMPH-JP031: 正「1ターンに1度、融合召喚及び以下の方法でのみ特殊召喚できる」→
抽出「の①の方法での特殊召喚は1ターンに1度しかできない」）。

そこで効果欄だけを切り出して3倍に拡大し、Opus 5.5 に読ませ直し（P2）、1回目（P1相当）と文字単位で
突き合わせる（P3）。食い違い・読めない字（〓）・読み直しの失敗は uncertain_fields に effect_text を入れ、
自動承認の対象から外して人の確認へ回す。評価（tools/eval_effect_text.py、n=30）では、
同じモデルの P1 と P2 の食い違いは誤りのあるカードを取りこぼさなかった（Opus 5.5: 再現率100%・誤警報率0〜14%）。

評価の限界（docs/decisions.md 参照）: n=30・YAC1（再録・特別版）に偏る・短文カードがほぼ無い。

ペンデュラム（2026-10-16〜）: ペンデュラム効果欄とモンスター効果欄をまとめて切り出し、両方を読み直して2欄とも突き合わせる。
評価（発売済み28枚・公式記事画像。docs/decisions.md）: 読み直しの両欄完全一致 27/28、1回目と一致した14枚に誤り0。
比較は「ペンデュラム効果＋区切り＋モンスター効果」をつないだ文字列（combined_text）で行い、通常カードと同じ条件で判定する。
"""
import base64
import difflib
import logging
import os
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# 読み直しに使うモデルと effort（評価と同じ medium）。Opus 5.5 は thinking を無効化できないので指定しない。
REREAD_MODEL = os.environ.get("EFFECT_REREAD_MODEL", "claude-opus-5-5")
REREAD_EFFORT = "medium"
_MAX_TOKENS = 8000
_WORKERS = 4  # TODO: calibrate from data（1カード1回の呼び出し。記事あたり最大30枚）

# 一致したとき、1回目のAI自己申告で入っていた effect_text の不確かを外すか。
# 計測（tools/eval_effect_text.py の firstpass、n=30。docs/decisions.md）: 1回目が自己申告した22枚のうち一致12枚、
# 一致した12枚に正解と違うものは0（一致した17枚全体でも0）だったので有効。1枚でも誤りが出たら False にして無効化する。
# 外した場合は
# extraction_raw.uncertain_cleared_by_reread=True を残す（自動承認の判定が「不確かを出さない medium」と区別するため）。
CLEAR_UNCERTAIN_ON_AGREE = True

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

PENDULUM_REREAD_USER_TEXT = ("この画像はペンデュラムモンスターのカード下部（ペンデュラム効果欄とモンスター効果欄）を切り出して"
                             "拡大したものです。両方の欄の本文を、ルールに従って書き起こしてください（カード名はここには写っていません）。")


def user_text_with_name(name: str, base: str | None = None) -> str:
    """カード名のヒントを添えた指示文（効果文の中の自分のカード名の誤読対策。評価: tools/eval_effect_text.py の P2H）。"""
    return ((base or REREAD_USER_TEXT) + f"\nこのカードの名前は「{name}」です。効果文の中の自分のカード名は、この表記と同じです。"
            "ただし画像の文字がこれと違って見えるときは、画像に見える文字を書いてください。")


REREAD_SCHEMA = {"type": "object", "properties": {
    "effect_text": {"type": "string"}, "unreadable": {"type": "boolean"}}}

# ペンデュラム用（ルール7・8・9と出力欄だけが通常と違う）。スケールも切り出しに写っているので読ませて1回目と突き合わせる
PENDULUM_REREAD_SYSTEM = REREAD_SYSTEM.replace(
    "7. ペンデュラム効果欄が写っていても、書き起こすのはカード最下部のモンスター効果欄（effect_text）だけにする。\n",
    "7. この画像には2つの欄がある。上の欄（左右に青と赤のスケールの数字がある欄）の本文を pendulum_effect に、\n"
    "   下の欄（【】の種別の行で始まる欄）の本文を effect_text に入れる。スケールの数字は書かない。\n"
    "   上の欄が空なら pendulum_effect は空文字。\n").replace(
    "8. 効果欄の文字を1文字でも", "8. どちらかの欄の文字を1文字でも") + (
    "9. 上の欄の左右にある青と赤のスケールの数字を pendulum_scale に入れる（左右は同じ値。読めなければ null）。\n")

PENDULUM_REREAD_SCHEMA = {"type": "object", "properties": {
    "pendulum_effect": {"type": "string"}, "effect_text": {"type": "string"},
    "pendulum_scale": {"type": ["integer", "null"]}, "unreadable": {"type": "boolean"}}}

# combined_text の区切り（効果文に出てこない記号。管理画面の差分表示でも欄の境目として見える）
PENDULUM_SEPARATOR = "‖"


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


def combined_text(effect_text: str, pendulum_effect: str = "", pendulum: bool = False) -> str:
    """突き合わせ用の文字列。通常は norm(効果)。ペンデュラムは norm(P効果)＋区切り＋norm(モンスター効果)。"""
    if not pendulum:
        return norm(effect_text)
    return norm(pendulum_effect) + PENDULUM_SEPARATOR + norm(effect_text)


def reread_combined(raw) -> str | None:
    """取込時に採用した読み直し文の combined_text。読み直し結果（text）が無ければ None。"""
    info = raw.get("effect_text_reread") if isinstance(raw, dict) else None
    if not isinstance(info, dict) or not isinstance(info.get("text"), str):
        return None
    return combined_text(info["text"], info.get("pendulum_text") or "", info.get("pendulum") is True)


def current_combined(raw, effect_text: str, pendulum_effect: str) -> str:
    """現在の値を、取込時の読み直しと同じ形（ペンデュラムなら2欄）で突き合わせ用にする。"""
    info = raw.get("effect_text_reread") if isinstance(raw, dict) else None
    pendulum = isinstance(info, dict) and info.get("pendulum") is True
    return combined_text(effect_text or "", pendulum_effect or "", pendulum)


def _is_pendulum(row: dict) -> bool:
    """種別名だけでなく、スケール・ペンデュラム効果が読めている場合もペンデュラムとして扱う。
    1回目の種別から「ペンデュラム」が抜けることがある（評価で 5/28。エクシーズ・儀式・融合のペンデュラム）。"""
    return ("ペンデュラム" in (row.get("card_type") or "")
            or row.get("pendulum_scale") is not None or bool((row.get("pendulum_effect") or "").strip()))


def _uncertain_list(raw: dict) -> list:
    value = raw.get("uncertain_fields")
    return list(value) if isinstance(value, list) else []


def _mark_uncertain(raw: dict, *fields: str) -> None:
    current = raw.get("uncertain_fields")
    current = list(current) if isinstance(current, list) else []
    for f in fields:
        if f not in current:
            current.append(f)
    raw["uncertain_fields"] = current


def _read_one(client, model, crop_png: bytes, name: str = "", pendulum: bool = False):
    import unreleased_quality as quality
    base = PENDULUM_REREAD_USER_TEXT if pendulum else REREAD_USER_TEXT
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": base64.standard_b64encode(crop_png).decode("ascii")}},
        {"type": "text", "text": user_text_with_name(name, base) if name else base},
    ]
    return quality.structured_request(client, model=model,
                                      system=PENDULUM_REREAD_SYSTEM if pendulum else REREAD_SYSTEM, content=content,
                                      schema=PENDULUM_REREAD_SCHEMA if pendulum else REREAD_SCHEMA,
                                      max_tokens=_MAX_TOKENS, effort=REREAD_EFFORT)


# 食い違いを信じてよい大きさの上限（norm() 後の1回目 a と読み直し b の比較）。
# 根拠: 本番の食い違い4件はいずれも1〜2文字の置換（され→し、・→か、は→後）。行の欠け・別カードの文・作り話は
# 差分が大きく、先頭や末尾にまとまる。これらを「読み直しが正しい」とは言えないので人に回す。
# TODO: calibrate from data（食い違い事例が溜まったら、信じた後の訂正率から見直す）
TRUST_MAX_DIFF_CHARS = 4          # 非一致文字数（置換は長い側、削除・挿入はその長さの合計）の上限
TRUST_LEN_RATIO = (0.95, 1.05)    # len(読み直し)/len(1回目) の許容範囲
TRUST_MAX_EDGE_INDEL = 1          # 先頭・末尾にある削除/挿入がこの文字数を超えたら（行の欠け・追加とみなして）信じない


def diff_within_trust_limits(a: str, b: str) -> bool:
    """norm() 後の a（1回目）と b（読み直し）の食い違いが、信じてよい小ささか。"""
    if not a or not b:
        return False
    if not (TRUST_LEN_RATIO[0] <= len(b) / len(a) <= TRUST_LEN_RATIO[1]):
        return False
    ops = diff_ops(a, b)
    # ペンデュラムの区切りが食い違いに含まれる＝文が欄をまたいで移った。差の字数に表れないので信じない
    if any(PENDULUM_SEPARATOR in x + y for tag, x, y in ops if tag != "equal"):
        return False
    mismatch = sum(max(len(x), len(y)) for tag, x, y in ops if tag != "equal")
    if mismatch > TRUST_MAX_DIFF_CHARS:
        return False
    for idx in (0, len(ops) - 1):   # 端にまとまった削除・挿入
        tag, x, y = ops[idx]
        if tag in ("delete", "insert") and max(len(x), len(y)) > TRUST_MAX_EDGE_INDEL:
            return False
    return True


def _can_trust_on_disagree(row: dict, raw: dict, model: str, a: str, b: str, text: str,
                           has_unreadable: bool, trust_on_disagree: bool | None, pendulum_text: str | None = None) -> bool:
    """食い違っても読み直し側を信じて効果テキストを不確かにしないか（reread_trust.py 参照）。

    すべて満たすときだけ True: 食い違いが小さい（diff_within_trust_limits）／読み直しに〓が無い／両方に文がある／1回目と読み直しが同じモデル／
    1回目が name を不確かとしていない／読み直し文に誤字検査の強い疑いが無い（検査できないときも不可）／
    設定が有効（読めなければ無効）。安い条件から順に見て、設定の読み取りは最後にする。
    a, b は combined_text（ペンデュラムは2欄をつないだ文字列なので、差の大きさは2欄の合計で見る）。
    pendulum_text はペンデュラムの読み直したペンデュラム効果（通常カードは None で、行の pendulum_effect を使う）。
    誤字検査は取込前なので他カードの文脈（名前・「」内語）なしで行う。文脈なしのほうが強い疑いが出やすい（安全側）。
    最終的な自動承認の判定（annotate_row）は文脈つきで text_check をやり直し、強い疑いがあれば別途不適格にする。
    """
    # ペンデュラムは区切りだけの文字列にならないよう、区切りを除いて中身があるかを見る
    if has_unreadable or not a.replace(PENDULUM_SEPARATOR, "") or not b.replace(PENDULUM_SEPARATOR, ""):
        return False
    if not diff_within_trust_limits(a, b):
        return False
    if raw.get("model") != model or "name" in _uncertain_list(raw):
        return False
    from unreleased_auto_approve import compute_text_check
    check = compute_text_check({"name": row.get("name") or "", "effect_text": text,
                                "pendulum_effect": pendulum_text if pendulum_text is not None
                                else (row.get("pendulum_effect") or "")})
    if check.get("status") != "checked" or check.get("strong"):
        return False
    if trust_on_disagree is not None:
        return trust_on_disagree
    import reread_trust
    return reread_trust.is_enabled()


def _effect_fields(pendulum: bool) -> tuple:
    """不確かの印を付け外しする欄（ペンデュラムは2欄）。"""
    return ("effect_text", "pendulum_effect") if pendulum else ("effect_text",)


def reread_effect_texts(rows: list[dict], *, client, encoded_images: list[dict], model: str | None = None,
                        trust_on_disagree: bool | None = None) -> None:
    """各カードの効果欄を拡大して読み直し、読み直し結果を採用して1回目と突き合わせる（rows を直接更新）。

    - 1回目の effect_text は extraction_raw.effect_text_first_pass（ペンデュラムは pendulum_effect_first_pass も）に残す
    - 結果と比較情報は extraction_raw.effect_text_reread {text, model, agreed, diff_summary, usage, ...}
      ペンデュラムは pendulum: True と pendulum_text（読み直したペンデュラム効果）も入る
    - 食い違い・〓・失敗は uncertain_fields に effect_text（ペンデュラムは pendulum_effect も）を追加する
    - 失敗時は1回目の結果を残す（黙って通さない）
    - trust_on_disagree: None なら設定（reread_trust.is_enabled）に従う。True/False はテスト用の固定
    """
    from card_crop import make_effect_crop, effect_box_kind
    from unreleased_extractor import normalize_effect_punct
    model = model or REREAD_MODEL
    images = {image["url"]: image for image in encoded_images}

    jobs = []  # (row, crop_png, pendulum)
    for row in rows:
        raw = row["extraction_raw"]
        raw["effect_text_first_pass"] = row.get("effect_text", "")
        card_type = row.get("card_type", "")
        pendulum = _is_pendulum(row)
        if pendulum:
            raw["pendulum_effect_first_pass"] = row.get("pendulum_effect") or ""
        try:
            image = images.get(raw.get("source_image_url"))
            if not image:
                raise ValueError("カード画像がありません")
            # ペンデュラムは種別名に「ペンデュラム」が無くてもペンデュラム用の位置で切り出す
            kind = "pendulum" if pendulum else effect_box_kind(card_type)
            if kind is None:
                raise ValueError(f"カード種別から効果欄の位置を決められません: {card_type!r}")
            suggestion = raw.get("crop_suggestion")
            if suggestion is None and raw.get("source") != "official_html":
                # X画像などカード以外（パック宣材）を含む画像は、枠が無いと効果欄の位置を決められない
                raise ValueError("カード枠候補がありません")
            crop = make_effect_crop(base64.b64decode(image["data"], validate=True), card_type, suggestion, kind=kind)
        except Exception as exc:  # noqa: BLE001 — 失敗は記録して不確かにする
            _fail(row, model, f"{type(exc).__name__}: {exc}")
            continue
        jobs.append((row, crop, pendulum))

    def work(job):
        row, crop, pendulum = job
        try:
            # 1回目で読んだカード名をヒントに添える（効果文中の自分のカード名の誤読対策。評価 P2H）。
            # 1回目が name を不確かとしたときは、誤った名前に引きずられないようヒントを付けない。
            name = "" if "name" in _uncertain_list(row["extraction_raw"]) else (row.get("name") or "").strip()
            t0 = time.time()
            result = _read_one(client, model, crop, name, pendulum=pendulum)
            row["extraction_raw"]["_reread_seconds"] = time.time() - t0
            return row, pendulum, result, None
        except Exception as exc:  # noqa: BLE001 — API エラー・未完了応答・JSON 不正など
            return row, pendulum, None, exc

    with ThreadPoolExecutor(max_workers=_WORKERS) as ex:
        results = list(ex.map(work, jobs))

    for row, pendulum, result, exc in results:
        if exc is not None:
            _fail(row, model, f"{type(exc).__name__}: {str(exc)[:300]}")
            continue
        data, _, usage = result
        text = normalize_effect_punct((data.get("effect_text") or "").strip())
        first = row.get("effect_text", "")
        ptext = normalize_effect_punct((data.get("pendulum_effect") or "").strip()) if pendulum else ""
        pfirst = (row.get("pendulum_effect") or "") if pendulum else ""
        raw = row["extraction_raw"]
        if (first and not text) or (pfirst and not ptext):
            # 空で返ったのに1回目には文がある: 読めたとは言えないので採用しない
            _fail(row, model, "読み直しの結果が空です", usage=usage)
            continue
        a, b = combined_text(first, pfirst, pendulum), combined_text(text, ptext, pendulum)
        has_unreadable = UNREADABLE in text + ptext or bool(data.get("unreadable"))
        # 両方空は「読めた」の証拠にならない（空同士の一致を一致扱いしない）
        agreed = bool(norm(first) + norm(pfirst)) and a == b and not has_unreadable
        seconds = raw.pop("_reread_seconds", None)
        raw["effect_text_reread"] = {
            "text": text, "model": model, "effort": REREAD_EFFORT, "agreed": agreed,
            "unreadable": has_unreadable, "diff_summary": "" if a == b else diff_summary(a, b), "usage": usage,
            **({"pendulum": True, "pendulum_text": ptext} if pendulum else {}),
            **({"seconds": round(seconds, 1)} if seconds is not None else {}),
        }
        row["effect_text"] = text
        if pendulum:
            row["pendulum_effect"] = ptext
            # スケールも読み直しで確かめる（切り出しに写っている）。食い違い・読めないときは 1回目を残して不確かにする
            rescale = data.get("pendulum_scale")
            rescale = rescale if isinstance(rescale, int) and not isinstance(rescale, bool) else None
            scale_agreed = rescale is not None and rescale == row.get("pendulum_scale")
            raw["effect_text_reread"].update({"pendulum_scale": rescale, "scale_agreed": scale_agreed})
            if not scale_agreed:
                _mark_uncertain(raw, "pendulum_scale")
                logger.warning("ペンデュラムスケールが読み直しと食い違い: %s 1回目=%s 読み直し=%s",
                               row.get("name"), row.get("pendulum_scale"), rescale)
        fields = _effect_fields(pendulum)
        uncertain = _uncertain_list(raw)
        # 一致で自己申告の不確かを外してよいのは、(1) 設定で有効 (2) 1回目と読み直しが同じモデル
        # （別モデル同士の一致は独立性の意味が違うので評価の外） (3) 1回目が name を不確かとしていない場合だけ
        can_clear = (CLEAR_UNCERTAIN_ON_AGREE and raw.get("model") == model and "name" not in uncertain)
        if agreed and can_clear:
            if any(f in uncertain for f in fields):
                raw["uncertain_fields"] = [f for f in uncertain if f not in fields]
                raw["uncertain_cleared_by_reread"] = True
        elif not agreed and _can_trust_on_disagree(row, raw, model, a, b, text, has_unreadable, trust_on_disagree,
                                                   pendulum_text=ptext if pendulum else None):
            # 食い違ったが、読み直し側に問題が見当たらないので不確かにしない（外れたら設定が自動で無効になる）
            raw["reread_trusted_on_disagree"] = True
            # 採用するのは読み直し文。1回目の自己申告による効果欄の不確かは1回目の読み取りへの自信の話なので外す
            # （name の自己申告があるときは条件側で信じない）。fields が空でも medium_uncertain_unknown にしない印を残す
            if any(f in uncertain for f in fields):
                raw["uncertain_fields"] = [f for f in uncertain if f not in fields]
                raw["uncertain_cleared_by_reread"] = True
            raw["reread_trust_diff_summary"] = raw["effect_text_reread"]["diff_summary"]
            logger.info("効果テキストが食い違ったが読み直し側を採用: %s %s", row.get("name"), raw["reread_trust_diff_summary"])
        elif not agreed:
            _mark_uncertain(raw, *fields)
            logger.warning("効果テキストが1回目と食い違い/〓あり/空: %s", row.get("name"))
        logger.info("[Reread] %s agreed=%s pendulum=%s model=%s %.1f秒", row.get("name"), agreed, pendulum, model,
                    seconds or 0.0)


def _fail(row: dict, model: str, reason: str, usage: dict | None = None) -> None:
    raw = row["extraction_raw"]
    logger.warning("効果テキストの読み直しに失敗（1回目の結果を残して不確かにする）: %s: %s", row.get("name"), reason)
    raw["effect_text_reread"] = {"error": reason, "model": model, "agreed": False, **({"usage": usage} if usage else {})}
    _mark_uncertain(raw, *_effect_fields(_is_pendulum(row)))


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
    # ペンデュラムは「ペンデュラム効果‖モンスター効果」の形で並べる（pendulum: True で画面に注記を出す）
    pendulum = info.get("pendulum") is True
    first = combined_text(raw.get("effect_text_first_pass") or "", raw.get("pendulum_effect_first_pass") or "", pendulum)
    text = combined_text(info.get("text") or "", info.get("pendulum_text") or "", pendulum)
    return {"status": "disagree", "first": first, "reread": text, "ops": diff_ops(first, text),
            "unreadable": bool(info.get("unreadable")), "model": info.get("model"), "pendulum": pendulum}
