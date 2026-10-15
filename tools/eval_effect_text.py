"""
eval_effect_text.py — 未発売カードの効果テキスト読み取り方式 × モデルの精度・費用比較ツール（評価専用）

本番コード（unreleased_extractor.py 等）は変更せず、読むだけで再利用する。DB は読むだけ（書き込まない）。

正解データ:
  発売済み（収録商品の発売日 <= 2026-10-03）のカードについて、yu-gi-oh.jp 公式記事の画像（watched_pages の
  /news/ 記事。fetch_guard 経由で取得）を、ygores_cards の公式テキスト（prints[].code で突き合わせ）と比べる。

方式（effort はすべて "medium"。thinking は指定しない＝モデル既定）:
  P0  現行再現: 全体画像を最大4枚ずつ1回で、現行プロンプト・現行スキーマ（_extract_cards と同じ呼び方）。sonnet のみ
  P1  全体画像を1枚ずつ＋改善プロンプト（C）。3モデル
  P2  効果欄を切り出して3倍拡大（LANCZOS）＋改善プロンプト。3モデル。
      ★テキスト欄のみを渡す（全体画像は添えない）。理由は README 相当の下記 P2_REASON を参照。
  P3  同じモデルの P1 と P2 を文字単位で突き合わせ、食い違い＝「不確か」。追加 API 呼び出しなし

サブコマンド（--out は結果の保存先。既定は scratchpad の eval_text）:
  select   評価セットを選んで JSON と画像を保存
  crops    効果欄の切り出し画像を保存（目視確認用）
  schema   構造化出力スキーマが実 API で通るか 1 件ずつ確認
  run      API 実行（--tag trial/full、--limit N、--models、--methods、--budget USD）。結果は逐次保存・再開可
  report   指標の集計（JSON + Markdown）と差分の保存

使い方の例:
  python tools/eval_effect_text.py select
  python tools/eval_effect_text.py crops
  python tools/eval_effect_text.py schema
  python tools/eval_effect_text.py run --tag trial --limit 2
  python tools/eval_effect_text.py run --tag full
  python tools/eval_effect_text.py report --tag full

認証: ~/.cardsouba/watcher.env（SUPABASE_URL / SUPABASE_KEY / ANTHROPIC_API_KEY）。値は出力しない。
"""

import argparse
import base64
import difflib
import hashlib
import json
import os
import statistics
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_OUT = (r"C:\Users\daiya\AppData\Local\Temp\claude\C--Users-daiya"
               r"\89aa63b3-374e-4d92-9b79-c44198ce4b89\scratchpad\eval_text")

RELEASED_BEFORE = "2026-10-03"  # この日付以前に発売済みの収録のみ評価対象
# 再録カードは、過去の印刷が古い文面（エラッタ前の旧テキスト）のまま印刷されることがあり、ygores（現行テキスト）が
# 正解にならない（実例: YAC1-JP088 リバイバルスライム。画像は旧文面、ygores は①：形式の現行文面）。
# そのため、初収録のカード、または過去の印刷がすべてこの日付以降（現行の書式で印刷された世代）の再録だけを使う。
REPRINT_OK_FROM = "2019-01-01"
EFFORT = "medium"
MODELS = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]
# 100万トークンあたり (入力, 出力) USD
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
}
MAX_TOKENS = 16000

P2_REASON = (
    "P2 はテキスト欄のみを渡す。理由: (1) 全体画像（約407x593px で文字が潰れている）を添えると、"
    "モデルが拡大画像ではなくぼやけた全体像や記憶から文を補う経路が残り、作り話の原因を切り分けられない。"
    "(2) P3 は P1（全体画像）と P2 の入力を独立にして食い違いを不確かさの信号にする設計で、"
    "P2 に全体画像を足すと両者の誤りが相関して食い違いが減る。(3) 入力トークンも減る。"
    "欠点: カード名は読ませられない（P2 のカード名一致率は対象外）。"
)

# ---------------------------------------------------------------------------
# 環境・共通
# ---------------------------------------------------------------------------


def load_env() -> None:
    from tools.build_text_check_dict import _load_env_fallback
    _load_env_fallback()
    path = os.path.join(os.path.expanduser("~"), ".cardsouba", "watcher.env")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if k and k not in os.environ:
                    os.environ[k] = v


def supabase_client():
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def jload(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def jsave(path, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def encode_image(content: bytes, url: str) -> dict:
    """unreleased_extractor._download_and_encode_images と同じ正規化（EXIF補正・RGB・PNG化）。"""
    from PIL import Image, ImageOps
    with Image.open(BytesIO(content)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        w, h = image.size
        buf = BytesIO()
        image.save(buf, format="PNG")
    return {"url": url, "media_type": "image/png",
            "data": base64.standard_b64encode(buf.getvalue()).decode("ascii"),
            "width": w, "height": h}


# ---------------------------------------------------------------------------
# select: 評価セット
# ---------------------------------------------------------------------------


def classify(ja: dict) -> str:
    """ygores の日本語データから大まかな種別タグを付ける（評価セットの層別用の近似）。"""
    ct = ja.get("cardType")
    if ct in ("spell", "trap"):
        return ct
    if ja.get("linkRating"):
        return "link"
    if ja.get("rank"):
        return "xyz"
    first = (ja.get("effectText") or "").split("\n")[0]
    if first.startswith("チューナー") and "＋" in first:
        return "synchro"
    if "＋" in first and "：" not in first and "①" not in first:
        return "fusion"
    if "により降臨" in first:
        return "ritual"
    return "monster"


def cmd_select(a) -> None:
    from fetch_guard import fetch_whitelisted
    from unreleased_extractor import _extract_card_image_urls

    out = a.out
    sb = supabase_client()
    pages = sb.table("watched_pages").select("url,last_extracted_at").execute().data
    news = [p for p in pages if "yu-gi-oh.jp" in p["url"] and "/news/" in p["url"]
            and "/search" not in p["url"] and not p["url"].rstrip("/").endswith("/news")]
    print(f"記事URL: {len(news)}件（一覧・検索ページは除外）")
    cache_dir = os.path.join(out, "html")
    os.makedirs(cache_dir, exist_ok=True)

    cands: dict[str, dict] = {}  # 基本カード番号 -> {article, code, url}
    for p in sorted(news, key=lambda r: r["last_extracted_at"] or ""):
        u = p["url"]
        cp = os.path.join(cache_dir, hashlib.md5(u.encode()).hexdigest() + ".html")
        if os.path.exists(cp):
            html = open(cp, encoding="utf-8").read()
        else:
            resp = fetch_whitelisted(u, timeout=30, min_interval=1.0)
            if resp.status_code != 200:
                print("取得失敗", u, resp.status_code)
                continue
            html = resp.text
            open(cp, "w", encoding="utf-8").write(html)
        for url in _extract_card_image_urls(html, u):
            seg = url.rsplit("/", 1)[-1].split(".", 1)[0]
            asset = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))[0]
            code = asset.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            base = code.split("_")[0]
            cands.setdefault(base, {"article": u, "code": code, "url": url, "html_cache": cp})
    print(f"画像から得たカード番号: {len(cands)}種")

    gt_cache = jload(os.path.join(out, "ygores_lookup.json"), {})
    pool = []
    for base, c in cands.items():
        if base not in gt_cache:
            rows = (sb.table("ygores_cards").select("konami_id,ja:raw->cardData->ja")
                    .filter("raw->cardData->ja->prints", "cs", json.dumps([{"code": base}]))
                    .execute().data)
            gt_cache[base] = rows[0]["ja"] if rows else None
        ja = gt_cache[base]
        if not ja or not (ja.get("effectText") or "").strip():
            continue
        dates = [p["date"] for p in ja.get("prints", []) if p.get("code") == base]
        if not dates or min(dates) > RELEASED_BEFORE:
            continue
        others = [p["date"] for p in ja.get("prints", []) if p.get("code") != base and p["date"] < min(dates)]
        if others and min(others) < REPRINT_OK_FROM:
            continue  # 古い世代の再録は印刷が旧文面の恐れがある
        pool.append({
            "code": base, "image_code": c["code"], "html_cache": c["html_cache"],
            "first_print": not others, "article": c["article"], "image_url": c["url"],
            "name": ja["name"], "kind": classify(ja), "release_date": min(dates),
            "effect_text": ja.get("effectText") or "",
            "pendulum_effect_text": ja.get("pendulumEffectText") or "",
            "has_pendulum_text": bool(ja.get("pendulumEffectText")),
        })
    jsave(os.path.join(out, "ygores_lookup.json"), gt_cache)
    print(f"発売済み・正解あり: {len(pool)}枚")

    by_kind: dict[str, list] = {}
    for c in pool:
        by_kind.setdefault(c["kind"], []).append(c)
    print("層別プール:", {k: len(v) for k, v in by_kind.items()},
          "ペンデュラム:", sum(c["has_pendulum_text"] for c in pool))

    # 層別の枠（合計 a.n 枚）。足りない層は効果モンスターで埋める。長さ順に等間隔で選ぶ（再現可能）。
    quota = {"spell": 6, "trap": 3, "link": 3, "xyz": 3, "fusion": 2, "synchro": 2, "ritual": 1}
    chosen: list[dict] = []

    def spread(items: list, k: int) -> list:
        items = sorted(items, key=lambda c: (len(c["effect_text"]), c["code"]))
        if k >= len(items):
            return items
        step = (len(items) - 1) / max(k - 1, 1)
        return [items[round(i * step)] for i in range(k)]

    for kind, k in quota.items():
        chosen += spread(by_kind.get(kind, []), k)
    rest = a.n - len(chosen)
    chosen_codes = {c["code"] for c in chosen}
    mons = [c for c in by_kind.get("monster", []) if c["code"] not in chosen_codes]
    chosen += spread(mons, rest)
    # 同じ記事の画像をまとめる（P0 のバッチ分けのため）
    chosen.sort(key=lambda c: (c["article"], c["code"]))

    # 画像を保存
    img_dir = os.path.join(out, "images")
    os.makedirs(img_dir, exist_ok=True)
    for c in chosen:
        ip = os.path.join(img_dir, c["code"] + ".bin")
        if not os.path.exists(ip):
            r = fetch_whitelisted(c["image_url"], timeout=30, min_interval=0.5)
            if r.status_code != 200:
                raise RuntimeError(f"画像取得失敗 {c['code']} {r.status_code}")
            open(ip, "wb").write(r.content)
        from PIL import Image
        with Image.open(ip) as im:
            c["image_size"] = list(im.size)
        c["image_file"] = ip
    jsave(os.path.join(out, "selection.json"),
          {"released_before": RELEASED_BEFORE, "n": len(chosen), "cards": chosen})
    print(f"選定 {len(chosen)}枚:", {k: sum(c['kind'] == k for c in chosen) for k in sorted({c['kind'] for c in chosen})})
    for c in chosen:
        print(f"  {c['code']:12s} {c['kind']:8s} {len(c['effect_text']):4d}字 {c['image_size']} {c['name']}")


# ---------------------------------------------------------------------------
# crops: 効果欄の切り出し
# ---------------------------------------------------------------------------

# 切り出しは本番と同じ関数（card_crop.make_effect_crop。比率の校正値と根拠はそちらを参照）。
# ペンデュラムは評価セットに入らなかったため未評価。
_KIND_TO_CARD_TYPE = {"spell": "通常魔法", "trap": "通常罠"}


def make_crop(content: bytes, kind: str) -> bytes:
    from card_crop import make_effect_crop
    return make_effect_crop(content, _KIND_TO_CARD_TYPE.get(kind, "効果モンスター"))


def cmd_crops(a) -> None:
    sel = jload(os.path.join(a.out, "selection.json"))
    d = os.path.join(a.out, "crops")
    os.makedirs(d, exist_ok=True)
    for c in sel["cards"]:
        data = make_crop(open(c["image_file"], "rb").read(), c["kind"])
        open(os.path.join(d, f"{c['code']}_{c['kind']}.png"), "wb").write(data)
    print(f"切り出し {len(sel['cards'])}枚を {d} に保存")


# ---------------------------------------------------------------------------
# プロンプトとスキーマ（改善プロンプト C）
# ---------------------------------------------------------------------------

IMPROVED_SYSTEM = """\
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
7. ペンデュラムモンスターは、ペンデュラム効果欄の文を pendulum_effect に、
   カード最下部のモンスター効果欄の文を effect_text に入れる。該当しなければ空文字。
8. 効果欄の文字を1文字でも「〓」にした場合は unreadable を true にする。それ以外は false。
"""

P1_USER_TEXT = ("この画像は遊戯王OCGのカード1枚です。カード名（name）と効果欄の本文を、"
                "ルールに従って書き起こしてください。カード名も読めない文字は「〓」にしてください。")

SCHEMA_P1 = {"type": "object", "properties": {
    "name": {"type": "string"}, "effect_text": {"type": "string"},
    "pendulum_effect": {"type": "string"}, "unreadable": {"type": "boolean"}}}


class EffortClient:
    """messages.create の extra_body.output_config に effort を足すだけのラッパー。

    本番の structured_request は effort を指定できないため、本番関数を書き換えずに
    全方式の effort を "medium" に揃えるために使う。
    """

    def __init__(self, client, effort: str):
        self._client, self._effort = client, effort
        self.messages = self

    def create(self, **kw):
        eb = dict(kw.pop("extra_body", None) or {})
        oc = dict(eb.get("output_config") or {})
        oc["effort"] = self._effort
        eb["output_config"] = oc
        return self._client.messages.create(extra_body=eb, **kw)


def make_client():
    from anthropic import Anthropic
    return EffortClient(Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], timeout=180.0, max_retries=2), EFFORT)


def cost_of(model: str, usage: dict) -> float:
    pi, po = PRICES[model]
    return (usage["input_tokens"] * pi + usage["output_tokens"] * po) / 1_000_000


def image_block(img: dict) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": img["media_type"], "data": img["data"]}}


def call_structured(client, model, system, content, schema):
    """unreleased_quality.structured_request をそのまま使う（失敗は例外）。"""
    from unreleased_quality import structured_request
    t0 = time.time()
    data, text, usage = structured_request(client, model=model, system=system, content=content,
                                           schema=schema, max_tokens=MAX_TOKENS)
    return data, usage, time.time() - t0


# ---------------------------------------------------------------------------
# run: API 実行
# ---------------------------------------------------------------------------


class Budget:
    def __init__(self, cap: float, spent: float = 0.0):
        self.cap, self.spent, self.lock = cap, spent, threading.Lock()

    def add(self, x: float) -> None:
        with self.lock:
            self.spent += x

    def exceeded(self) -> bool:
        with self.lock:
            return self.spent >= self.cap


def run_p1(client, model, card, img):
    content = [image_block(img), {"type": "text", "text": P1_USER_TEXT}]
    return call_structured(client, model, IMPROVED_SYSTEM, content, SCHEMA_P1)


def run_p2(client, model, card, crop_png):
    # P2 は本番（effect_text_reread）と同じプロンプト・スキーマ・effort で測る
    import effect_text_reread as rr
    img = {"media_type": "image/png", "data": base64.standard_b64encode(crop_png).decode("ascii")}
    content = [image_block(img), {"type": "text", "text": rr.REREAD_USER_TEXT}]
    return call_structured(client, model, rr.REREAD_SYSTEM, content, rr.REREAD_SCHEMA)


def run_p0(client, batch_cards, imgs, html_cache):
    """現行 _extract_cards の1バッチ分と同じ呼び出し（拡大再読 enrich_rows は含めない）。"""
    import unreleased_extractor as ue
    from unreleased_quality import structured_request
    from datetime import datetime
    from zoneinfo import ZoneInfo
    article = batch_cards[0]["article"]
    html = open(html_cache, encoding="utf-8").read()
    processed = ue._preprocess_html(html, article)
    context = f"[処理日時: {datetime.now(ZoneInfo('Asia/Tokyo')).isoformat()}。これは発売日の根拠ではありません]\n{processed}"
    content = ue._build_vision_message(context, imgs)
    _, Result = ue._get_pydantic_models()
    t0 = time.time()
    data, text, usage = structured_request(client, model="claude-sonnet-5-5", system=ue._SYSTEM_PROMPT,
                                           content=content, schema=Result.model_json_schema(),
                                           max_tokens=ue._MAX_TOKENS)
    return data, usage, time.time() - t0


def retryable(fn, tries=3):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — 失敗は記録して続行する
            last = e
            msg = str(e)
            # 400系（スキーマ・パラメータ不正）はリトライしても無駄
            if "400" in msg[:80] or "invalid_request" in msg:
                break
            time.sleep(3 * (i + 1))
    raise last


def cmd_schema(a) -> None:
    """構造化出力スキーマと effort が実 API で通るか、各モデル 1 件で確認する。"""
    sel = jload(os.path.join(a.out, "selection.json"))
    card = sel["cards"][0]
    raw = open(card["image_file"], "rb").read()
    img = encode_image(raw, card["image_url"])
    crop = make_crop(raw, card["kind"])
    client = make_client()
    for m in MODELS:
        for label, fn in (("P1", lambda: run_p1(client, m, card, img)), ("P2", lambda: run_p2(client, m, card, crop))):
            try:
                data, usage, sec = fn()
                print(f"OK  {m} {label} {usage} {sec:.1f}s keys={sorted(data)} cost=${cost_of(m, usage):.4f}")
            except Exception as e:  # noqa: BLE001
                print(f"NG  {m} {label} {type(e).__name__}: {str(e)[:300]}")


def cmd_run(a) -> None:
    out = a.out
    sel = jload(os.path.join(out, "selection.json"))
    cards = sel["cards"][: a.limit] if a.limit else sel["cards"]
    path = os.path.join(out, f"results_{a.tag}.json")
    res = jload(path, {})
    budget = Budget(a.budget, sum(v.get("cost", 0) for v in res.values()))
    print(f"対象 {len(cards)}枚 / 既存結果 {len(res)}件 / 既出費用 ${budget.spent:.3f} / 上限 ${a.budget}")
    client = make_client()
    lock = threading.Lock()
    raws = {c["code"]: open(c["image_file"], "rb").read() for c in cards}
    imgs = {c["code"]: encode_image(raws[c["code"]], c["image_url"]) for c in cards}

    jobs = []  # (key, fn, model, n_cards)
    methods = a.methods.split(",")
    models = a.models.split(",")
    if "P0" in methods:
        by_article: dict[str, list] = {}
        for c in cards:
            by_article.setdefault(c["article"], []).append(c)
        for art, cs in by_article.items():
            for i in range(0, len(cs), 4):
                b = cs[i:i + 4]
                key = "P0|claude-sonnet-5-5|" + ",".join(x["code"] for x in b)
                jobs.append((key, "claude-sonnet-5-5", b,
                             lambda b=b: run_p0(client, b, [imgs[x["code"]] for x in b], b[0]["html_cache"])))
    for m in models:
        for c in cards:
            if "P1" in methods:
                jobs.append((f"P1|{m}|{c['code']}", m, [c], lambda m=m, c=c: run_p1(client, m, c, imgs[c["code"]])))
            if "P2" in methods:
                crop = make_crop(raws[c["code"]], c["kind"])
                jobs.append((f"P2|{m}|{c['code']}", m, [c], lambda m=m, c=c, crop=crop: run_p2(client, m, c, crop)))

    todo = [j for j in jobs if j[0] not in res]
    print(f"ジョブ {len(jobs)}件 / 未実行 {len(todo)}件")

    def work(job):
        key, model, cs, fn = job
        if budget.exceeded():
            return
        try:
            data, usage, sec = retryable(fn)
            rec = {"data": data, "usage": usage, "seconds": sec, "cost": cost_of(model, usage),
                   "n_cards": len(cs), "codes": [x["code"] for x in cs], "model": model}
        except Exception as e:  # noqa: BLE001
            rec = {"error": f"{type(e).__name__}: {str(e)[:400]}", "cost": 0.0, "n_cards": len(cs),
                   "codes": [x["code"] for x in cs], "model": model}
        budget.add(rec["cost"])
        with lock:
            res[key] = rec
            jsave(path, res)
        print(f"  {key[:48]:48s} ${rec['cost']:.4f} {rec.get('seconds', 0):.1f}s {'ERR ' + rec['error'][:80] if 'error' in rec else ''}"
              f" 累計 ${budget.spent:.3f}", flush=True)

    # 安い順（haiku→sonnet→opus）に流し、上限超過を早めに検知する
    order = {m: i for i, m in enumerate(MODELS)}
    todo.sort(key=lambda j: (order[j[1]], j[0]))
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(work, todo))
    print(f"完了。累計費用 ${budget.spent:.3f}" + ("（上限に達したため中断）" if budget.exceeded() else ""))


# ---------------------------------------------------------------------------
# 指標
# ---------------------------------------------------------------------------

UNREADABLE = "〓"


def norm(s: str) -> str:
    """本番の突き合わせと同じ正規化（effect_text_reread.norm）。"""
    from effect_text_reread import norm as _norm
    return _norm(s)


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def fabricated(gt: str, out: str, min_run: int = 5) -> list[str]:
    """正解に無い連続 min_run 文字以上の挿入・置換区間（「〓」で区切る。正解内に存在する断片は除く）。"""
    sm = difflib.SequenceMatcher(None, gt, out, autojunk=False)
    found = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag not in ("insert", "replace"):
            continue
        for seg in out[j1:j2].split(UNREADABLE):
            if len(seg) >= min_run and seg not in gt:
                found.append(seg)
    return found


def omitted(gt: str, out: str, min_run: int = 5) -> list[str]:
    """正解にある連続 min_run 文字以上が出力から抜け落ちた区間（出力側が「〓」を含む置換は除く＝正直な未読扱い）。"""
    sm = difflib.SequenceMatcher(None, gt, out, autojunk=False)
    found = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace") and i2 - i1 >= min_run and UNREADABLE not in out[j1:j2]:
            found.append(gt[i1:i2])
    return found


def card_text(effect: str, pend: str) -> str:
    return norm(pend) + norm(effect)


def score(gt_eff: str, gt_pend: str, out_eff: str, out_pend: str) -> dict:
    gt = card_text(gt_eff, gt_pend)
    o = card_text(out_eff, out_pend)
    lev = levenshtein(gt, o)
    fab = fabricated(gt, o)
    omit = omitted(gt, o)
    return {"cer": lev / max(len(gt), 1), "lev": lev, "len": len(gt), "exact": gt == o,
            "fab": fab, "has_fab": bool(fab), "omit": omit, "has_omit": bool(omit), "unreadable_mark": UNREADABLE in o,
            "n_unreadable": o.count(UNREADABLE)}


def inline_diff(gt: str, out: str) -> str:
    sm = difflib.SequenceMatcher(None, gt, out, autojunk=False)
    parts = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            parts.append(gt[i1:i2])
        else:
            parts.append(f"[-{gt[i1:i2]}-]{{+{out[j1:j2]}+}}" if gt[i1:i2] else f"{{+{out[j1:j2]}+}}")
    return "".join(parts)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def collect(res: dict, cards: list[dict]) -> dict:
    """(method, model, code) -> {out_eff, out_pend, out_name, cost, seconds, unreadable_flag}"""
    items = {}
    by_code = {c["code"]: c for c in cards}
    for key, rec in res.items():
        method, model, rest = key.split("|", 2)
        if "error" in rec:
            for code in rec["codes"]:
                items[(method, model, code)] = {"error": rec["error"]}
            continue
        n = rec["n_cards"]
        base = {"cost": rec["cost"] / n, "seconds": rec["seconds"] / n, "usage": rec["usage"]}
        if method == "P0":
            got = {}
            for cd in rec["data"].get("cards", []):
                got.setdefault((cd.get("image_url") or "").strip(), cd)
            for code in rec["codes"]:
                cd = got.get(by_code[code]["image_url"])
                if cd is None:
                    items[(method, model, code)] = {"error": "P0 の出力に該当画像のカードが無い", **base}
                    continue
                items[(method, model, code)] = {**base, "out_eff": cd.get("effect_text", ""),
                                                "out_pend": cd.get("pendulum_effect", ""),
                                                "out_name": cd.get("name", ""), "unreadable_flag": None}
        else:
            d = rec["data"]
            items[(method, model, rec["codes"][0])] = {
                **base, "out_eff": d.get("effect_text", ""), "out_pend": d.get("pendulum_effect", ""),
                "out_name": d.get("name"), "unreadable_flag": d.get("unreadable")}
    return items


def summarize(rows: list[dict]) -> dict:
    ok = [r for r in rows if "score" in r]
    if not ok:
        return {"n": 0, "errors": len(rows)}
    cers = [r["score"]["cer"] for r in ok]
    names = [r["name_ok"] for r in ok if r.get("name_ok") is not None]
    return {
        "n": len(ok), "errors": len(rows) - len(ok),
        "cer_mean": statistics.mean(cers), "cer_median": statistics.median(cers),
        "exact_rate": sum(r["score"]["exact"] for r in ok) / len(ok),
        "fab_rate": sum(r["score"]["has_fab"] for r in ok) / len(ok),
        "omit_rate": sum(r["score"]["has_omit"] for r in ok) / len(ok),
        "unreadable_rate": sum(r["score"]["unreadable_mark"] for r in ok) / len(ok),
        "unreadable_flag_rate": (sum(bool(r["unreadable_flag"]) for r in ok) / len(ok)
                                 if all(r["unreadable_flag"] is not None for r in ok) else None),
        "name_match": (sum(names) / len(names)) if names else None,
        "cost_per_card": statistics.mean(r["cost"] for r in ok),
        "sec_per_card": statistics.mean(r["seconds"] for r in ok),
        "tokens_in": statistics.mean(r["usage"]["input_tokens"] for r in ok),
        "tokens_out": statistics.mean(r["usage"]["output_tokens"] for r in ok),
    }


def cmd_report(a) -> None:
    out = a.out
    sel = jload(os.path.join(out, "selection.json"))
    cards = sel["cards"]
    by_code = {c["code"]: c for c in cards}
    res = jload(os.path.join(out, f"results_{a.tag}.json"), {})
    items = collect(res, cards)

    per_card: dict[tuple, dict] = {}
    for (method, model, code), it in items.items():
        c = by_code[code]
        rec = {"method": method, "model": model, "code": code, "name": c["name"], "kind": c["kind"]}
        if "error" in it and "out_eff" not in it:
            rec["error"] = it["error"]
        else:
            rec.update({k: it[k] for k in ("cost", "seconds", "usage", "unreadable_flag")})
            rec["out_eff"], rec["out_pend"], rec["out_name"] = it["out_eff"], it["out_pend"], it["out_name"]
            rec["score"] = score(c["effect_text"], c["pendulum_effect_text"], it["out_eff"], it["out_pend"])
            rec["name_ok"] = (norm(it["out_name"]) == norm(c["name"])) if it.get("out_name") is not None else None
        per_card[(method, model, code)] = rec

    groups: dict[tuple, list] = {}
    for (method, model, code), rec in per_card.items():
        groups.setdefault((method, model), []).append(rec)
    order = {"P0": 0, "P1": 1, "P2": 2}
    summary = {}
    for (method, model), rows in sorted(groups.items(), key=lambda kv: (order[kv[0][0]], MODELS.index(kv[0][1]))):
        summary[f"{method}|{model}"] = summarize(rows)

    # P3: 同モデルの P1 と P2 の食い違い
    p3 = {}
    for m in MODELS:
        rows = []
        for c in cards:
            r1, r2 = per_card.get(("P1", m, c["code"])), per_card.get(("P2", m, c["code"]))
            if not r1 or not r2 or "score" not in r1 or "score" not in r2:
                continue
            t1 = card_text(r1["out_eff"], r1["out_pend"])
            t2 = card_text(r2["out_eff"], r2["out_pend"])
            rows.append({"code": c["code"], "disagree": t1 != t2, "err1": not r1["score"]["exact"],
                         "err2": not r2["score"]["exact"], "dist": levenshtein(t1, t2)})
        if not rows:
            continue

        def rates(err_key):
            bad = [r for r in rows if r[err_key]]
            good = [r for r in rows if not r[err_key]]
            return {"n_error_cards": len(bad), "n_clean_cards": len(good),
                    "recall": (sum(r["disagree"] for r in bad) / len(bad)) if bad else None,
                    "false_alarm": (sum(r["disagree"] for r in good) / len(good)) if good else None}
        p3[m] = {"n": len(rows), "disagree_rate": sum(r["disagree"] for r in rows) / len(rows),
                 "adopt_P1": rates("err1"), "adopt_P2": rates("err2"),
                 "both_exact_but_differ": sum(1 for r in rows if not r["err1"] and not r["err2"] and r["disagree"])}
    result = {"tag": a.tag, "n_cards": len(cards), "summary": summary, "p3": p3,
              "total_cost": sum(v.get("cost", 0) for v in res.values())}
    jsave(os.path.join(out, f"report_{a.tag}.json"), result)
    jsave(os.path.join(out, f"per_card_{a.tag}.json"),
          [{"key": "|".join(k), **{kk: vv for kk, vv in v.items()}} for k, v in per_card.items()])

    # Markdown
    def pct(x):
        return "-" if x is None else f"{x * 100:.1f}%"
    def table(summ):
        T = ["| 方式 | モデル | n(失敗) | CER平均 | CER中央値 | 完全一致 | 作り話率 | 脱落率 | 〓率 | unreadable申告 | 名前一致 | 費用/枚 | 秒/枚 | in tok | out tok |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for k, s_ in summ.items():
            method, model = k.split("|")
            if s_["n"] == 0:
                T.append(f"| {method} | {model} | 0({s_['errors']}) |" + " - |" * 12)
                continue
            T.append(f"| {method} | {model} | {s_['n']}({s_['errors']}) | {s_['cer_mean'] * 100:.2f}% | {s_['cer_median'] * 100:.2f}% | "
                     f"{pct(s_['exact_rate'])} | {pct(s_['fab_rate'])} | {pct(s_['omit_rate'])} | {pct(s_['unreadable_rate'])} | "
                     f"{pct(s_['unreadable_flag_rate'])} | {pct(s_['name_match'])} | ${s_['cost_per_card']:.4f} | "
                     f"{s_['sec_per_card']:.1f} | {s_['tokens_in']:.0f} | {s_['tokens_out']:.0f} |")
        return T

    # P0 が出力を返した枚数に揃えた比較（P0 が出力しなかったカードを除く）
    p0_codes = {c for (m_, mo, c), r in per_card.items() if m_ == "P0" and "score" in r}
    subset_summary = {}
    for (method, model), rows in sorted(groups.items(), key=lambda kv: (order[kv[0][0]], MODELS.index(kv[0][1]))):
        subset_summary[f"{method}|{model}"] = summarize([r for r in rows if r["code"] in p0_codes])
    result["summary_p0_subset"] = subset_summary
    jsave(os.path.join(out, f"report_{a.tag}.json"), result)

    L = [f"# 効果テキスト読み取り評価（{a.tag}）", "",
         f"評価カード {len(cards)}枚 / effort={EFFORT} / 総費用 ${result['total_cost']:.3f}", "",
         "作り話率＝正解に無い連続5文字以上の挿入・置換があるカードの割合（〓は除外）。脱落率＝正解にある連続5文字以上が（〓を出さずに）抜け落ちたカードの割合。",
         "", "## 方式×モデル（全カード）", ""] + table(summary)
    L += ["", f"## 方式×モデル（P0 が出力を返した {len(p0_codes)}枚に揃えた比較）", ""] + table(subset_summary)
    L += ["", "## P3（同モデルの P1 と P2 の食い違い＝不確か）", "",
          "再現率＝誤りのあるカードのうち食い違いで見つけた割合 / 誤警報率＝誤りの無いカードで食い違いが出た割合。",
          "「採用=P1」は P1 の出力を最終結果とみなした場合（P1 の誤りカードが分母）、「採用=P2」も同様。", "",
          "| モデル | n | 食い違い率 | 採用=P1: 誤り枚数 / 再現率 / 誤警報率 | 採用=P2: 誤り枚数 / 再現率 / 誤警報率 |", "|---|---|---|---|---|"]
    for m, p in p3.items():
        def cell(r):
            return f"{r['n_error_cards']} / {pct(r['recall'])} / {pct(r['false_alarm'])}"
        L.append(f"| {m} | {p['n']} | {pct(p['disagree_rate'])} | {cell(p['adopt_P1'])} | {cell(p['adopt_P2'])} |")
    L += ["", "## カードごとの CER（%）", "",
          "| コード | 種別 | 字数 | 名前 | " + " | ".join(f"{k.replace('claude-', '').replace('-5-5', '')}" for k in summary) + " |",
          "|---|---|---|---|" + "---|" * len(summary)]
    for c in cards:
        cells = []
        for k in summary:
            method, model = k.split("|")
            r = per_card.get((method, model, c["code"]))
            cells.append("ERR" if not r or "score" not in r else f"{r['score']['cer'] * 100:.1f}")
        L.append(f"| {c['code']} | {c['kind']} | {len(norm(c['effect_text']))} | {c['name']} | " + " | ".join(cells) + " |")
    open(os.path.join(out, f"report_{a.tag}.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")

    # 差分（正解 vs 出力）
    D = [f"# 正解との差分（{a.tag}）", "", "表記: `[-正解にあって出力に無い-]{+出力にだけ有る+}`（正規化後の文字列で比較）", ""]
    for c in cards:
        D += [f"## {c['code']} {c['name']}（{c['kind']}）", "", f"正解: {norm(c['effect_text'])}", ""]
        for k in summary:
            method, model = k.split("|")
            r = per_card.get((method, model, c["code"]))
            if not r:
                continue
            if "score" not in r:
                D.append(f"- {method} {model}: 失敗 {r.get('error', '')[:120]}")
                continue
            gt = card_text(c["effect_text"], c["pendulum_effect_text"])
            o = card_text(r["out_eff"], r["out_pend"])
            tail = (f" 作り話={r['score']['fab']}" if r["score"]["fab"] else "") +                    (f" 脱落={r['score']['omit']}" if r["score"]["omit"] else "")
            D.append(f"- {method} {model}: CER {r['score']['cer'] * 100:.1f}%{' 完全一致' if r['score']['exact'] else ''}{tail}")
            if not r["score"]["exact"]:
                D.append(f"  - 差分: {inline_diff(gt, o)}")
        D.append("")
    open(os.path.join(out, f"diffs_{a.tag}.md"), "w", encoding="utf-8").write("\n".join(D) + "\n")
    print("\n".join(L))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["select", "crops", "schema", "run", "report"])
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--tag", default="full")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--models", default=",".join(MODELS))
    ap.add_argument("--methods", default="P0,P1,P2")
    ap.add_argument("--budget", type=float, default=6.0)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    load_env()
    {"select": cmd_select, "crops": cmd_crops, "schema": cmd_schema, "run": cmd_run, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    main()
