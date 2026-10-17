"""
ocgtimes_reveal.py — 公式配信「遊戯王OCGタイムズ」の紹介場面から未発売カードを読む

背景（2026-10-17）:
  配信終盤の「新規カード一覧」（5枚並び・カード幅約300px）から読むと、効果文の1回目が空になりやすく、
  人が承認した16枚中6枚に誤りが残った（人の承認でも5枚見落とし）。
  一方、各カードの紹介場面（左に大きいカード＝幅約450px、右下の黒地に効果文の全文が白い字幕で出る）を
  Opus 5.5 で読むと、字幕からの効果文は24画面すべて正解と一致した（tools/eval_ocgtimes_reveal.py）。

読み方:
  - 効果文は右下の字幕から書き写す。種別・属性・種族・数値・名前は左のカード画像（と名前の札）から読む。
  - 同じカードが複数の場面に出るので、読み取った名前（fuzzy_key。1〜2字の揺れは近さでまとめる）でまとめる。
    場面ごとに数値・種別が食い違う欄は多数決（一覧画面の値も1票に数える）にしたうえで不確かにする
    （試験で ハーピィ・レディ三姉妹・TX のレベルを9と読んだ画面が1枚あった。正しくは8）。
  - 一覧画面（新規カード一覧①〜④）は、全カードの網羅確認と数値の突き合わせに使う（効果文は読まない）。
    一覧にあって紹介場面に無いカードは list_only として返す（呼び出し側が一覧画面の読み方で取り込む）。
  - カード画像側の効果欄を拡大して読み直し（effect_text_reread と同じ指示）、字幕と突き合わせる。
    試験では一致20/24、食い違い4件はすべてカード側の読み違い（読点の脱落・「鏡壁」→「鏡鷲」など）。
    一致は2つの独立した読み取りが揃った証拠なので、他に不確かな欄が無いカードを
    extraction_raw.reveal.auto_approve_candidate=True として記録する（今は自動承認しない。
    画像取込の行は unreleased_auto_approve の upload_source で必ず人の承認に回る）。
    食い違いは effect_text を不確かにして、管理画面に差分を出す（採用するのは字幕の文）。

Flask に依存しない（tools/ocgtimes_watch.py と管理画面の画像取込の両方から使う）。
"""

import difflib
import logging
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from name_normalize import fuzzy_key

logger = logging.getLogger(__name__)

REVEAL_MODEL = os.environ.get("REVEAL_MODEL", "claude-opus-5-5")
REVEAL_EFFORT = "medium"
_MAX_TOKENS = 8000
_WORKERS = 4  # TODO: calibrate from data（試験は24画面を4並列で約1分）

# 名前の近さでまとめる閾値（fuzzy_key どうしの difflib 比）。1字違いの読み揺れをまとめ、別カードは分ける。
# 例: 「アマゾネスの格闘戦士－蹴殺の舞い－」と「アマゾネスの鎖使い－死に際の鎖舞い－」は 0.6 前後で別扱い。
# TODO: calibrate from data（10-16 配信では揺れが出なかったので実例なし）
NAME_MERGE_RATIO = 0.88

# 場面ごと・一覧との突き合わせに使う欄と、不確かにするときの欄名（UNCERTAIN_FIELDS の値）
VALUE_FIELDS = ("card_type", "attribute", "race", "level", "rank", "link_val", "atk", "def", "pendulum_scale")
UNCERTAIN_NAME = {"level": "level_rank_link", "rank": "level_rank_link", "link_val": "level_rank_link"}

REVEAL_SYSTEM = """\
あなたは遊戯王OCGの公式配信の画面からカード情報を正確に書き写すアシスタントです。
画面の構成: 左にカード画像が1枚大きく映り、その下に名前の札がある。右下の黒い欄には、そのカードの効果文が大きな文字（字幕）で表示されている。
- name: 名前の札（またはカード名欄）の文字。ふりがなは含めない。
- reading: 名前の札のふりがな（ひらがな・カタカナ）。漢字の部分にだけふりがなが付くときは、ふりがなと仮名の部分をつないで名前全体の読みにする。読めなければ空文字。
- effect_text: 右下の字幕の文字を、改行を詰めてそのまま書き写す（①②・●・「」・数字も見たとおり）。融合・S・X・リンクの素材行（例:「植物族レベル１０モンスター×２」）も字幕にあれば含める。カードの中の小さい文字から補わない。字幕が途中で切れている・ページ送りの途中に見える場合は effect_complete=false。
- ペンデュラムモンスターで、字幕にペンデュラム効果とモンスター効果が分かれて出ているときは、ペンデュラム効果を pendulum_effect、モンスター効果を effect_text に分ける。分かれ目が分からないときは全文を effect_text に入れ、effect_complete=false。
- card_type などの数値・種別は左のカード画像から読む。モンスター以外は level/rank/link_val/atk/def/pendulum_scale を null、attribute/race を空文字。
  card_type は「効果モンスター」「融合モンスター」「エクシーズモンスター」「シンクロモンスター」「リンクモンスター」「儀式モンスター」「通常モンスター」「ペンデュラム・効果モンスター」「通常魔法」「速攻魔法」「永続魔法」「装備魔法」「フィールド魔法」「儀式魔法」「通常罠」「永続罠」「カウンター罠」など（魔法・罠はアイコンで種類を決める。アイコンが無い魔法・罠は通常）。
  レベル・ランクは星を1個ずつ数える。
- card_bbox: 左のカード画像の最外縁（元画像のピクセル座標 [left, top, right, bottom]）。名前の札や背景の枠は含めない。カードが映っていなければ null。
- 読めない字は推測せず〓にする。画面に字幕が無ければ has_panel=false。左にカードが映っていない画面は name を空文字にする。"""

REVEAL_SCHEMA = {"type": "object", "properties": {
    "has_panel": {"type": "boolean"}, "effect_complete": {"type": "boolean"},
    "name": {"type": "string"}, "reading": {"type": "string"}, "card_type": {"type": "string"},
    "attribute": {"type": "string"}, "race": {"type": "string"},
    "level": {"type": ["integer", "null"]}, "rank": {"type": ["integer", "null"]},
    "link_val": {"type": ["integer", "null"]}, "atk": {"type": ["integer", "null"]},
    "def": {"type": ["string", "null"]}, "pendulum_scale": {"type": ["integer", "null"]},
    "pendulum_effect": {"type": "string"}, "effect_text": {"type": "string"},
    "card_bbox": {"type": ["array", "null"], "items": {"type": "number"}}}}

LIST_SYSTEM = """\
あなたは遊戯王OCGの公式配信の「新規カード一覧」画面からカードの名前と数値を書き写すアシスタントです。
画面には複数のカードが並び、カードごとに名前の札が付いている。並んでいるカードを左上から順に1枚ずつ返す。
- name: 名前の札（またはカード名欄）の文字。ふりがなは含めない。
- card_type・attribute・race・level・rank・link_val・atk・def はカード画像から読む。モンスター以外は数値を null、attribute/race を空文字。
  card_type は「効果モンスター」「融合モンスター」「エクシーズモンスター」「通常魔法」「速攻魔法」「通常罠」「永続罠」などの書き方にする。
  レベル・ランクは星を1個ずつ数える。
- 効果文は書かない。読めない項目は推測せず null・空文字にする。"""

LIST_SCHEMA = {"type": "object", "properties": {"cards": {"type": "array", "items": {
    "type": "object", "properties": {
        "name": {"type": "string"}, "card_type": {"type": "string"}, "attribute": {"type": "string"},
        "race": {"type": "string"}, "level": {"type": ["integer", "null"]}, "rank": {"type": ["integer", "null"]},
        "link_val": {"type": ["integer", "null"]}, "atk": {"type": ["integer", "null"]},
        "def": {"type": ["string", "null"]}}}}}}


# ──────────────────────────────────────────────
# 1画面の読み取り
# ──────────────────────────────────────────────

def _image_block(image: dict) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": image["media_type"], "data": image["data"]}}


def read_reveal_frame(client, image: dict) -> dict:
    """紹介場面1画面を読む。image は unreleased_extractor.normalize_uploaded_image の戻り値。"""
    from unreleased_quality import structured_request
    content = [_image_block(image),
               {"type": "text", "text": f"この画面のカードを書き写してください。画像は {image['width']}x{image['height']} です。"}]
    t0 = time.time()
    data, _, usage = structured_request(client, model=REVEAL_MODEL, system=REVEAL_SYSTEM, content=content,
                                        schema=REVEAL_SCHEMA, max_tokens=_MAX_TOKENS, effort=REVEAL_EFFORT)
    data["usage"] = usage
    data["seconds"] = round(time.time() - t0, 1)
    return data


def read_list_frame(client, image: dict) -> list[dict]:
    """一覧画面1画面から、並んでいるカードの名前と数値を読む（効果文は読まない）。"""
    from unreleased_quality import structured_request
    content = [_image_block(image), {"type": "text", "text": "並んでいるカードの名前と数値を書き写してください。"}]
    data, _, _ = structured_request(client, model=REVEAL_MODEL, system=LIST_SYSTEM, content=content,
                                    schema=LIST_SCHEMA, max_tokens=_MAX_TOKENS, effort=REVEAL_EFFORT)
    return [c for c in data.get("cards") or [] if (c.get("name") or "").strip()]


# ──────────────────────────────────────────────
# 名前でまとめる・食い違いを見る
# ──────────────────────────────────────────────

def _clean(v):
    """比較用の値（文字列は前後空白を除き、空文字・None は None にそろえる）。"""
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def _close(a: str, b: str) -> bool:
    return bool(a and b) and (a == b or difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= NAME_MERGE_RATIO)


def near_name(a: str, b: str) -> bool:
    """fuzzy_key どうしが同じか、1字の読み違い程度（近い・長さの差1字以内）か。ふりがなが無い照合（一覧画面）用。"""
    return a == b or (_close(a, b) and abs(len(a) - len(b)) <= 1)


def _same_card(key: str, reading: str, group: dict) -> bool:
    """この読み取りを group（最初の場面の名前とふりがな）と同じカードとみなすか。

    名前が同じならまとめる。違うときは、近い（NAME_MERGE_RATIO 以上）・長さの差が1字以内・ふりがなが両方あって
    同じ、のすべてを満たすときだけまとめる（1字の読み違いだけを吸収する）。
    「真紅眼の黒竜」と「真紅眼の黒炎竜」のような末尾だけ違う別カードは比が0.9を超えるので、ふりがなで分ける。
    迷ったら分ける: 分けすぎた行は一覧との突き合わせで name が不確かになって人が見るが、まとめすぎるとカードが消える。
    """
    first = group["first_key"]
    if key == first:
        return True
    return (_close(key, first) and abs(len(key) - len(first)) <= 1
            and bool(reading) and reading == group["first_reading"])


def group_reads(reads: list[dict]) -> list[dict]:
    """名前のある読み取りを、名前の近さでまとめる。戻り値: [{"key", "name", "names", "reads": [...]}]（出た順）。

    reads の各要素は read_reveal_frame の結果に "t"（秒）と "image_key" を足したもの。
    """
    groups: list[dict] = []
    for r in reads:
        name = (r.get("name") or "").strip()
        key = fuzzy_key(name)
        if not key:
            continue
        reading = fuzzy_key((r.get("reading") or "").strip())
        g = next((g for g in groups if _same_card(key, reading, g)), None)
        if g is None:
            g = {"names": [], "reads": [], "first_key": key, "first_reading": reading}
            groups.append(g)
        g["names"].append(name)
        g["reads"].append(r)
    for g in groups:
        g["name"] = Counter(g["names"]).most_common(1)[0][0]
        g["key"] = fuzzy_key(g["name"])
    return groups


def match_list_card(key: str, list_cards: list[dict]) -> dict | None:
    """一覧画面の読み取りから、同じ名前（または近い名前）のカードを探す。"""
    exact = [c for c in list_cards if fuzzy_key(c["name"]) == key]
    if exact:
        return exact[0]
    near = [c for c in list_cards if near_name(key, fuzzy_key(c["name"]))]
    return near[0] if near else None


def decide_values(reads: list[dict], list_card: dict | None) -> tuple[dict, dict]:
    """場面ごとの値（と一覧の値1票）の多数決で各欄を決める。

    - 一覧画面の空値（小さくて読めなかった）は票に数えない。紹介場面の空値も、読めた場面があれば票に数えない
      （読めた値を空で消さない）。紹介場面がどれも空なら空のまま（紹介場面の読みを正とする。魔法・罠に一覧の
      誤読の数値を入れない）で、一覧にだけ値があれば食い違いとして残す。
    - 同数のときは、空でない一覧の値 → 空でない最初の場面の値。
    - 食い違い: 空でない値が2種類以上、または紹介場面に空と値が混ざるとき（魔法・罠を数値つきで読んだ可能性）。
    値は表記（str）で比べる（1200 と "1200" は同じ値）。
    戻り値: (値, 食い違い {欄: {"votes": {値: 票数}, "list": 一覧の値, "empty_frames": 空の場面数}})
    """
    values, conflicts = {}, {}
    for field in VALUE_FIELDS:
        frame_values = [_clean(r.get(field)) for r in reads]
        list_value = _clean(list_card.get(field)) if list_card and field in list_card else None
        if all(v is None for v in frame_values):
            values[field] = None
            if list_value is not None:
                conflicts[field] = {"votes": {}, "list": list_value, "empty_frames": len(frame_values)}
            continue
        votes = [v for v in frame_values if v is not None] + ([list_value] if list_value is not None else [])
        rep = {}
        for v in votes:
            rep.setdefault(str(v), v)
        counted = Counter(str(v) for v in votes)
        top = counted.most_common()
        best = top[0][0]
        if len(top) > 1 and top[0][1] == top[1][1]:
            best = str(list_value) if list_value is not None else next(str(v) for v in frame_values if v is not None)
        values[field] = rep[best]
        empty_frames = sum(v is None for v in frame_values)
        if len(counted) > 1 or (empty_frames and empty_frames < len(frame_values)):
            conflicts[field] = {"votes": dict(counted),
                                **({"list": list_value} if list_card and field in list_card else {}),
                                **({"empty_frames": empty_frames} if empty_frames else {})}
    return values, conflicts


def decide_effect(reads: list[dict]) -> tuple[dict, list]:
    """効果文を決める。字幕が全部見えている場面（effect_complete）の文の多数決。

    戻り値: ({"effect_text", "pendulum_effect", "status", "frames"}, 不確かにする欄)。
    status: agreed（全部見えた場面の文がすべて同じ）/ variants（場面ごとに違う）/ incomplete（全部見えた場面が無い）/ no_panel
    """
    from effect_text_reread import norm
    complete = [r for r in reads if r.get("has_panel") and r.get("effect_complete") and (r.get("effect_text") or "").strip()]
    if complete:
        key_of = lambda r: (norm(r.get("pendulum_effect") or ""), norm(r.get("effect_text") or ""))  # noqa: E731
        counted = Counter(key_of(r) for r in complete)
        best_key = counted.most_common(1)[0][0]
        best = next(r for r in complete if key_of(r) == best_key)
        status = "agreed" if len(counted) == 1 else "variants"
        frames = [r.get("t") for r in complete if key_of(r) == best_key]
    else:
        panel = [r for r in reads if r.get("has_panel") and (r.get("effect_text") or "").strip()]
        if not panel:
            return {"effect_text": "", "pendulum_effect": "", "status": "no_panel", "frames": []}, ["effect_text"]
        best = max(panel, key=lambda r: len(r.get("effect_text") or ""))
        status, frames = "incomplete", [best.get("t")]
    uncertain = [] if status == "agreed" else ["effect_text"]
    return {"effect_text": (best.get("effect_text") or "").strip(),
            "pendulum_effect": (best.get("pendulum_effect") or "").strip(),
            "status": status, "frames": frames,
            **({"variant_count": len(counted)} if complete else {})}, uncertain


# ──────────────────────────────────────────────
# カード画像側の効果欄との突き合わせ
# ──────────────────────────────────────────────

def pick_frame(group: dict, effect: dict, values: dict | None = None) -> dict | None:
    """保存・突き合わせに使う場面: 採用した効果文が出ていて、カード枠のある場面。

    多数決で決めた値（values）と読みが揃う場面を優先し、その中でカードが一番大きいもの
    （数値を読み違えた場面は、映りが悪い・切り替わりの途中のことがある）。
    """
    def width(r):
        b = r.get("card_bbox")
        return (b[2] - b[0]) if isinstance(b, list) and len(b) == 4 else -1

    def agrees(r):
        return values is None or all(_clean(r.get(f)) == values.get(f) for f in VALUE_FIELDS)
    cands = [r for r in group["reads"] if r.get("t") in effect["frames"] and width(r) > 0] or \
            [r for r in group["reads"] if width(r) > 0]
    return max(cands, key=lambda r: (agrees(r), width(r))) if cands else None


def card_text_check(client, frame_png: bytes, suggestion: dict, card_type: str, name: str, subtitle: str) -> dict:
    """カード画像の効果欄を拡大して読み直し（effect_text_reread と同じ指示）、字幕の文と突き合わせる。

    戻り値: {"status": agreed/disagree/error, "text", "diff_summary", "ops", "unreadable", "model", "usage"}
    """
    from card_crop import effect_box_kind, make_effect_crop
    from effect_text_reread import REREAD_MODEL, UNREADABLE, _read_one, diff_ops, diff_summary, norm
    from unreleased_extractor import normalize_effect_punct
    try:
        kind = effect_box_kind(card_type)
        if kind is None:
            raise ValueError(f"カード種別から効果欄の位置を決められません: {card_type!r}")
        crop = make_effect_crop(frame_png, card_type, suggestion, kind=kind)
        data, _, usage = _read_one(client, REREAD_MODEL, crop, name)
    except Exception as exc:  # noqa: BLE001 — 突き合わせられないだけ（字幕の文は残す）
        return {"status": "error", "reason": f"{type(exc).__name__}: {str(exc)[:300]}", "model": REREAD_MODEL}
    text = normalize_effect_punct((data.get("effect_text") or "").strip())
    a, b = norm(subtitle), norm(text)
    unreadable = UNREADABLE in text or bool(data.get("unreadable"))
    agreed = bool(a) and a == b and not unreadable
    return {"status": "agreed" if agreed else "disagree", "text": text, "unreadable": unreadable,
            "diff_summary": "" if a == b else diff_summary(a, b), "ops": [] if a == b else diff_ops(a, b),
            "model": REREAD_MODEL, "usage": usage}


# ──────────────────────────────────────────────
# まとめて抽出
# ──────────────────────────────────────────────

def _video_url(base: str, t) -> str:
    if t is None or t == "":
        return base
    return f"{base}{'&' if '?' in base else '?'}t={int(t)}s"


def extract_reveal(reveal_frames: list[dict], list_frames: list[dict] | None = None, *, video_url: str,
                   product_name: str = "", client=None, card_check: bool = True) -> dict:
    """紹介場面（と一覧画面）からカードの行を作る。DB には触らない。

    reveal_frames / list_frames: [{"image": normalize_uploaded_image の戻り値, "t": 秒 or None}]
    戻り値: {"rows": [行...], "list_only": [一覧にあって紹介場面に無いカード], "list_cards": [...],
            "reads": [各画面の読み取り], "no_card_frames": [カードの無い場面の t],
            "failed_frames": [{"kind", "t", "error"}]（読み取りに失敗した画面。紹介場面が全部失敗したら例外）}
    各行の extraction_raw.source_image_url は保存する場面の画像の識別子（image["url"]）。
    """
    from card_crop import refine_card_box
    from unreleased_quality import attach_release_date
    if client is None:
        from anthropic import Anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("環境変数 ANTHROPIC_API_KEY が設定されていません。")
        client = Anthropic(api_key=api_key, timeout=300.0, max_retries=1)
    list_frames = list_frames or []

    def read(job):
        # 1画面の失敗（API の一時的な過負荷など）で配信1本ぶんを止めない。失敗した画面は failed_frames で返す
        kind, f = job
        try:
            if kind == "reveal":
                return kind, f, read_reveal_frame(client, f["image"]), None
            return kind, f, read_list_frame(client, f["image"]), None
        except Exception as exc:  # noqa: BLE001
            logger.warning("[reveal] 画面の読み取りに失敗: %s t=%s: %s", kind, f.get("t"), exc)
            return kind, f, None, f"{type(exc).__name__}: {str(exc)[:200]}"

    jobs = [("reveal", f) for f in reveal_frames] + [("list", f) for f in list_frames]
    with ThreadPoolExecutor(max_workers=_WORKERS) as ex:
        results = list(ex.map(read, jobs))
    failed = [{"kind": kind, "t": f.get("t"), "error": err} for kind, f, _, err in results if err]
    if reveal_frames and all(err for kind, _, _, err in results if kind == "reveal"):
        raise RuntimeError(f"紹介場面をすべて読めませんでした: {failed[0]['error'] if failed else ''}")
    list_failed = any(x["kind"] == "list" for x in failed)
    reads, list_cards, by_key = [], [], {}
    for kind, f, data, err in results:
        if err:
            continue
        if kind == "reveal":
            reads.append({**data, "t": f.get("t"), "image_key": f["image"]["url"]})
            by_key[f["image"]["url"]] = f["image"]
        else:
            for c in data:
                list_cards.append({**c, "list_t": f.get("t")})
    no_card = [r.get("t") for r in reads if not (r.get("name") or "").strip()]

    groups = group_reads(reads)
    rows, matched_list = [], set()
    dates: dict = {}
    for g in groups:
        list_card = match_list_card(g["key"], list_cards)
        if list_card is not None:
            matched_list.add(id(list_card))
        values, conflicts = decide_values(g["reads"], list_card)
        effect, uncertain = decide_effect(g["reads"])
        uncertain = list(uncertain)
        for field in conflicts:
            u = UNCERTAIN_NAME.get(field, field)
            if u not in uncertain:
                uncertain.append(u)
        if len(set(g["names"])) > 1 and "name" not in uncertain:
            uncertain.append("name")
        pendulum = values.get("pendulum_scale") is not None or bool(effect["pendulum_effect"]) \
            or "ペンデュラム" in (values.get("card_type") or "")
        if pendulum:
            # ペンデュラムの字幕の出し方は未確認（10-16 配信に無かった）。2欄の分け方を人が確かめる
            for u in ("effect_text", "pendulum_effect"):
                if u not in uncertain:
                    uncertain.append(u)

        frame = pick_frame(g, effect, values)
        suggestion = None
        check = {"status": "skipped", "reason": "カード枠のある場面がありません"}
        if frame is not None:
            image = by_key[frame["image_key"]]
            suggestion = refine_card_box(image["png"], frame["card_bbox"])
            if suggestion:
                suggestion["source_image_url"] = image["url"]
            else:
                check = {"status": "skipped", "reason": "カード枠を確かめられません"}
            if suggestion and pendulum:
                check = {"status": "skipped", "reason": "ペンデュラムは未対応（字幕とカードの欄の対応が未確認）"}
            elif suggestion and not effect["effect_text"]:
                check = {"status": "skipped", "reason": "字幕の文がありません"}
            elif suggestion and card_check:
                check = card_text_check(client, image["png"], suggestion, values.get("card_type") or "",
                                        g["name"], effect["effect_text"])
            elif suggestion:
                check = {"status": "skipped", "reason": "突き合わせなし（指定）"}
        if check["status"] in ("disagree", "error") and "effect_text" not in uncertain:
            uncertain.append("effect_text")
        if list_card is not None:
            list_status = "matched"
            if fuzzy_key(list_card["name"]) != g["key"] and "name" not in uncertain:
                uncertain.append("name")   # 一覧とは近いだけで名前が違う
        elif not list_frames:
            list_status = "no_list"
        elif list_failed:
            list_status = "list_unavailable"   # 一覧画面の一部を読めなかった（name は不確かにしない）
        else:
            list_status = "not_in_list"
            if "name" not in uncertain:
                uncertain.append("name")   # 一覧があるのに見つからない＝名前の読み違いで割れた可能性
        candidate = (check["status"] == "agreed" and not uncertain and list_status == "matched")

        reading = Counter((r.get("reading") or "").strip() for r in g["reads"]
                          if fuzzy_key(r.get("name") or "") == g["key"]).most_common(1)
        t = frame.get("t") if frame else g["reads"][0].get("t")
        image_key = frame["image_key"] if frame else g["reads"][0]["image_key"]
        def_value = values.get("def")
        row = {
            "name": g["name"],
            "reading": reading[0][0] if reading else "",
            "card_type": values.get("card_type") or "",
            "attribute": values.get("attribute") or "",
            "race": values.get("race") or "",
            "level": values.get("level"), "rank": values.get("rank"), "link_val": values.get("link_val"),
            "atk": values.get("atk"), "def": str(def_value) if def_value is not None else "",
            "pendulum_scale": values.get("pendulum_scale"),
            "pendulum_effect": effect["pendulum_effect"],
            "effect_text": effect["effect_text"],
            "product_name": (product_name or "").strip(),
            "release_date": None,
            "confidence": "high" if not uncertain else "medium",
            "source_url": _video_url(video_url, t),
            "extraction_raw": {
                "model": REVEAL_MODEL, "vision_mode": True, "quality_version": 1,
                "layout": "reveal",
                "source_image_url": image_key, "card_image_url": image_key,
                "card_bbox": frame.get("card_bbox") if frame else None,
                **({"crop_suggestion": suggestion} if suggestion else {}),
                "uncertain_fields": uncertain,
                "reveal": {
                    "frames": [{"t": r.get("t"), "name": r.get("name"), "has_panel": r.get("has_panel"),
                                "effect_complete": r.get("effect_complete"),
                                **{k: r.get(k) for k in VALUE_FIELDS}} for r in g["reads"]],
                    "saved_frame_t": t,
                    "effect": effect,
                    "value_conflicts": conflicts,
                    "list_check": {"status": list_status,
                                   **({"t": list_card.get("list_t"), "name": list_card.get("name")}
                                      if list_card else {})},
                    "card_text_check": check,
                    "auto_approve_candidate": candidate,
                },
            },
        }
        from unreleased_extractor import normalize_effect_punct
        row["effect_text"] = normalize_effect_punct(row["effect_text"])
        row["pendulum_effect"] = normalize_effect_punct(row["pendulum_effect"])
        attach_release_date(row, dates)
        rows.append(row)
        logger.info("[reveal] %s 場面=%d 効果=%s 突き合わせ=%s 一覧=%s 不確か=%s", g["name"], len(g["reads"]),
                    effect["status"], check["status"], list_status, uncertain)

    list_only = [c for c in list_cards if id(c) not in matched_list]
    return {"rows": rows, "list_only": list_only, "list_cards": list_cards, "reads": reads,
            "no_card_frames": no_card, "failed_frames": failed}


def admin_view(raw: dict) -> dict | None:
    """管理画面の効果テキスト欄の下に出す「字幕とカード画像の読み直しの食い違い」。一致・紹介場面以外は None。

    形は effect_text_reread.admin_view の disagree / error と同じにして、表示の言葉だけ labels で変える。
    """
    info = (raw.get("reveal") or {}).get("card_text_check") if isinstance(raw, dict) else None
    if not isinstance(info, dict) or info.get("status") not in ("disagree", "error"):
        return None
    labels = {"first": "字幕（採用中）", "reread": "カード画像の効果欄の読み直し",
              "note": "紹介場面の字幕と、カード画像の効果欄を拡大して読み直した結果が食い違いました。"
                      "効果テキスト欄は字幕の文です。食い違った箇所を画像で確かめてください。"}
    if info.get("status") == "error":
        return {"status": "error", "reason": str(info.get("reason") or "")[:300], "labels": labels}
    from effect_text_reread import diff_ops, norm
    effect = (raw.get("reveal") or {}).get("effect") or {}
    first, text = norm(effect.get("effect_text") or ""), norm(info.get("text") or "")
    return {"status": "disagree", "first": first, "reread": text, "ops": diff_ops(first, text),
            "unreadable": bool(info.get("unreadable")), "model": info.get("model"), "pendulum": False,
            "labels": labels}

