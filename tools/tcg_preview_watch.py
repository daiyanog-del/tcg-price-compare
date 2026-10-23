"""
tcg_preview_watch.py — TCG先行カードの収集・和訳・保存（週1回の定期実行用）

TCG先行カード＝英語版で先に出て、日本のOCGにまだ無いカード。今回は「取り込みだけ」で、画面にはまだ出さない。

流れ:
  1. YGOResources の公開ミラー（en/ ja/）を読み、英語にあって日本語に無い konami_id を候補にする
  2. 種族・種類・属性・魔法罠の種類を、ミラーの日英が揃うカードから作った対応表で日本語にする（AIは使わない。
     対応表に無い語は英語のまま入れて警告ログを出す）
  3. 公式データベースの英語ページから商品名・収録番号・発売日・画像を取る（1枚ずつ、1秒以上あける）
  4. 効果文を tcg_translate.translate で和訳する（通常モンスターの説明文は訳さない。訳したときの英文を
     translated_source_en に残し、今の英文と同じなら訳し直さない）
  5. tcg_preview_cards へ konami_id で upsert する（first_seen_at は payload に含めない＝最初の1回だけ）
  6. 日本に来たカードの切り替え: 表で status='active' の行のうち、ミラーの日本語側に同じ konami_id があり、
     かつ その日本語名が data/cardnames_ja.json（価格検索が使う日本語名の一覧）に載っているものを
     status='ocg_linked'・ocg_name=日本語名・ocg_linked_at=今 に更新する（更新だけ。翻訳・公式DBへのアクセスはしない）。
     一覧に載っていない間は切り替えない（日本語名で検索・表示できないうちに切り替えるとカードが表示されなくなるため）。
     照合は完全一致 → name_normalize.fuzzy_key 同士。一覧側の正式名を ocg_name に入れる

実行:
  python tools/tcg_preview_watch.py --mirror <ミラーのフォルダ>
  python tools/tcg_preview_watch.py --dry-run --no-translate   # DBに書かず・APIも呼ばず、置き換え結果を表示するだけ
  --limit N（先頭N枚。0は0件）/ --max-translate N（1回あたりの翻訳件数の上限）
  （--dry-run は DB に接続しないので、手順6の「日本に来た」判定は行わない。判定の確認はテストで行う）

認証: 環境変数 SUPABASE_URL / SUPABASE_KEY / ANTHROPIC_API_KEY（DISCORD_WEBHOOK_URL は任意）。
未設定なら ~/.cardsouba/watcher.env から読む。モデルは TCG_TRANSLATE_MODEL（既定 claude-opus-5-5。採点で Sonnet より公式文に近く、年100枚程度なら費用差が小さいため）、
effort は TCG_TRANSLATE_EFFORT（既定なし）。
"""

import argparse
import hashlib
import html as html_lib
import io
import logging
import os
import re
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import tcg_translate  # noqa: E402
from name_normalize import fuzzy_key  # noqa: E402  記号・ハイフン種のゆれ吸収（app.py と同一）

logger = logging.getLogger("tcg_preview_watch")

TABLE = "tcg_preview_cards"
BUCKET = "official-card-images"
STORAGE_DIR = "tcg_preview"
DB_BASE = "https://www.db.yugioh-card.com/yugiohdb/"
PAGE_URL = DB_BASE + "card_search.action?ope=2&cid={cid}&request_locale=en"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
# 公式データベースへのアクセス間隔（秒）
MIN_INTERVAL = 1.1
DEFAULT_MODEL = "claude-opus-5-5"
# 候補（英語にあって日本語に無いカード）の上限。ミラーが一部欠けると「日本語に無い」候補が数千件に膨らみ、
# 翻訳料金と公式DBへの負荷が跳ね上がるので、超えたら何もせず知らせて止める。通常は数十件。 TODO: calibrate from data
MAX_CANDIDATES = 100
# 価格検索が使う日本語名の一覧（JSON の配列。週1回月曜9時に更新）
CARDNAMES_PATH = os.path.join(_ROOT, "data", "cardnames_ja.json")
_MAX_IMAGE_BYTES = 5 * 1024 * 1024

_SPELL_TRAP_WORD = {"spell": "魔法", "trap": "罠"}


# ──────────────────────────────────────────────
# 環境
# ──────────────────────────────────────────────

def _load_env_fallback() -> None:
    """未設定の環境変数を ~/.cardsouba/watcher.env から補完する（既にある値は上書きしない）。"""
    env_path = os.path.join(os.path.expanduser("~"), ".cardsouba", "watcher.env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


# ──────────────────────────────────────────────
# 語彙の対応表（英語 → 日本語）
# ──────────────────────────────────────────────

class Vocab:
    """ミラーの日英が揃うカードから作った、英語の項目 → 日本語の語彙の対応表。

    props:       英語の properties の語 → 日本語（"Thunder"→"雷族"、"Effect"→"効果"）
    order:       日本語の properties の語 → 並び順（小さいほど前。種族を除く）
    attributes:  englishAttribute → 日本語1字（"earth"→"地"）
    spell_trap:  (type, englishProperty) → 日本語の種類（("spell","quickplay")→"速攻"）
    """

    def __init__(self, props, order, attributes, spell_trap):
        self.props = props
        self.order = order
        self.attributes = attributes
        self.spell_trap = spell_trap


def _is_race(ja_word: str) -> bool:
    return ja_word.endswith("族")


def _topological_order(words: set, prec: Counter) -> list[str]:
    """日本語側の並びの多数決（a が b より前に出た回数が多い）を辺にして、前から順に並べる。

    単純な「前に出た回数の合計」は、よく一緒に出る組（シンクロ・チューナー等）に引きずられて崩れるため、
    語の組ごとの多数決で辺を作り、トポロジカルソートする。循環があれば入ってくる辺が最も少ない語から出す。
    """
    after = {w: {b for b in words if b != w and prec[(w, b)] > prec[(b, w)]} for w in words}
    indeg = {w: sum(1 for x in words if w in after[x]) for w in words}
    out, left = [], set(words)
    while left:
        ready = [w for w in left if indeg[w] == 0] or [min(left, key=lambda w: (indeg[w], w))]
        w = sorted(ready)[0]
        out.append(w)
        left.discard(w)
        for b in after[w]:
            indeg[b] -= 1
    return out


def build_vocab(en: dict, ja: dict) -> Vocab:
    """日英が揃う konami_id から対応表を作る。複数の対応が出たら最頻を採る。"""
    props = defaultdict(Counter)
    attrs = defaultdict(Counter)
    st = defaultdict(Counter)
    prec = Counter()  # (a, b): 日本語側で a が b より前に出た回数
    words = set()
    for cid in set(en) & set(ja):
        e, j = en[cid], ja[cid]
        if e.get("type") == "monster":
            attr_ja = (j.get("localizedAttribute") or "").removesuffix("属性")
            if e.get("englishAttribute") and attr_ja:
                attrs[e["englishAttribute"]][attr_ja] += 1
            pe, pj = e.get("properties") or [], j.get("properties") or []
            # 日本語側だけにある語（「特殊召喚」など）で長さが合わないカードは、位置で対応付けられないので使わない
            if pe and len(pe) == len(pj):
                for a, b in zip(pe, pj):
                    props[a][b] += 1
            rest = [w for w in pj if not _is_race(w)]
            words.update(rest)
            for i, a in enumerate(rest):
                for b in rest[i + 1:]:
                    prec[(a, b)] += 1
        elif e.get("type") in _SPELL_TRAP_WORD:
            lp = j.get("localizedProperty")
            if e.get("englishProperty") and lp:
                st[(e["type"], e["englishProperty"])][lp] += 1
    ranked = _topological_order(words, prec)
    return Vocab(
        props={k: v.most_common(1)[0][0] for k, v in props.items()},
        order={w: i for i, w in enumerate(ranked)},
        attributes={k: v.most_common(1)[0][0] for k, v in attrs.items()},
        spell_trap={k: v.most_common(1)[0][0] for k, v in st.items()},
    )


def _warn_unknown(kind: str, word: str, cid) -> None:
    logger.warning(f"対応表に無い{kind}を英語のまま入れました: {word!r}（konami_id={cid}）。対応表の材料（日英が揃うカード）が無い語です")


def translate_monster_terms(card: dict, vocab: Vocab) -> tuple[str, str]:
    """モンスターの (card_type, race) を日本語にする。"""
    cid = card.get("id")
    race, rest = "", []
    for w in card.get("properties") or []:
        j = vocab.props.get(w)
        if j is None:
            _warn_unknown("語（種族・種類）", w, cid)
            j = w
        if _is_race(j):
            race = j
        else:
            rest.append(j)
    # 日本語の並び順に整える（対応表に順序が無い語は先頭側に寄せる）
    rest.sort(key=lambda w: vocab.order.get(w, -1))
    return "・".join(rest) + "モンスター", race


def translate_attribute(card: dict, vocab: Vocab) -> str:
    """モンスターの属性を「地」のような1字にする。魔法・罠は属性なし（空文字）。"""
    if card.get("type") != "monster":
        return ""
    a = card.get("englishAttribute") or ""
    j = vocab.attributes.get(a)
    if j is None:
        if a:
            _warn_unknown("属性", a, card.get("id"))
        return a
    return j


def build_card_type(card: dict, vocab: Vocab) -> str:
    """カード種別（日本語）。モンスターは種族を除いた properties を「・」でつなぎ、魔法罠は「<種類>魔法/罠」。

    限界: 日本語の公式 properties にだけある「特殊召喚」（特殊召喚・効果モンスター）は、英語の properties に
    対応する語が無く英語から判定できない。推測では足さないので、該当カードは「特殊召喚・」が抜けた種別になる。
    （日英が揃うモンスター約9000枚中、約340枚が該当。対応表の作成時はこの形のカードを位置の対応付けから除いている）
    """
    if card.get("type") == "monster":
        return translate_monster_terms(card, vocab)[0]
    word = _SPELL_TRAP_WORD.get(card.get("type"))
    if word is None:
        logger.warning(f"未知のカード種別 {card.get('type')!r}（konami_id={card.get('id')}）")
        return str(card.get("type") or "")
    prop = card.get("englishProperty")
    if not prop:
        return "通常" + word
    j = vocab.spell_trap.get((card["type"], prop))
    if j is None:
        _warn_unknown(f"{word}の種類", prop, card.get("id"))
        j = prop
    return j + word


def select_candidates(en: dict, ja: dict) -> list[int]:
    """英語にあって日本語に無い konami_id（昇順）。"""
    return sorted(set(en) - set(ja))


def _none_if_negative(v):
    return None if isinstance(v, int) and v < 0 else v


def is_normal_monster(card: dict) -> bool:
    """通常モンスター（説明文はフレーバーで効果ではないので訳さない）。"""
    return card.get("type") == "monster" and "Normal" in (card.get("properties") or [])


def build_row(card: dict, vocab: Vocab) -> dict:
    """ミラーの英語カード1枚から、表の文字情報の行（翻訳・商品・画像を除く）を作る。"""
    is_mon = card.get("type") == "monster"
    race = translate_monster_terms(card, vocab)[1] if is_mon else ""
    arrows = card.get("linkArrows") or []
    # ミラーは攻撃力・守備力の「?」を -1 で表す（実データで atk 82枚・def 53枚。level 等に -1 は無いが、念のため同じく None）
    atk, d = card.get("atk"), card.get("def")
    atk = None if atk is None or atk == -1 else atk
    d = "?" if d == -1 else ("" if d is None else str(d))
    return {
        "konami_id": int(card["id"]),
        "name_en": card.get("name") or "",
        "card_type": build_card_type(card, vocab),
        "attribute": translate_attribute(card, vocab),
        "race": race,
        "level": _none_if_negative(card.get("level")),
        "rank": _none_if_negative(card.get("rank")),
        "link_val": _none_if_negative(card.get("linkRating")),
        "atk": atk,
        "def": d,
        "pendulum_scale": _none_if_negative(card.get("pendScale")),
        "link_arrows": ",".join(str(a) for a in arrows),
        "effect_text_en": card.get("effectText") or "",
        "pendulum_effect_en": card.get("pendEffect") or "",
    }


# ──────────────────────────────────────────────
# 公式データベース
# ──────────────────────────────────────────────

_last_request = 0.0


def _throttle() -> None:
    """公式データベースへのアクセスの間隔を MIN_INTERVAL 秒以上あける。"""
    global _last_request
    wait = MIN_INTERVAL - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def _http_get(url: str, referer: str | None = None) -> tuple[bytes, str]:
    """GET して (本文, Content-Type) を返す。失敗は例外。"""
    _throttle()
    headers = {"User-Agent": UA, "Accept-Language": "en"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(_MAX_IMAGE_BYTES + 1), r.headers.get("Content-Type", "")


_ROW_RE = re.compile(
    r'<div class="time">\s*(\d{4}-\d{2}-\d{2})\s*</div>'
    r'(?:(?!<div class="time">).)*?'
    r'<div class="card_number">\s*(.*?)\s*</div>'
    r'(?:(?!<div class="time">).)*?'
    r'<div class="pack_name[^"]*"\s*>\s*(.*?)\s*</div>',
    re.S,
)


def parse_pack_list(page_html: str) -> dict | None:
    """収録商品の欄から、一番古い日付の行の {release_date, set_code, product_name_en} を返す。行が無ければ None。"""
    i = page_html.find('id="update_list"')
    if i < 0:
        return None
    rows = [(d, html_lib.unescape(n).strip(), html_lib.unescape(p).strip())
            for d, n, p in _ROW_RE.findall(page_html[i:])]
    if not rows:
        return None
    d, number, pack = min(rows, key=lambda r: r[0])  # 同じ日付なら先に出た行（min は先頭を返す）
    return {"tcg_release_date": d, "set_code": number, "product_name_en": pack}


_IMG_RE = re.compile(r'get_image\.action\?([^"\'\s<>]+)')


def parse_image_url(page_html: str, cid: int) -> str | None:
    """ページ内の大きいカード画像のURL（type=1・osplang付き・ciid=1）を返す。無ければ None。"""
    for m in _IMG_RE.finditer(page_html):
        q = parse_qs(html_lib.unescape(m.group(1)))
        if (q.get("cid") == [str(cid)] and q.get("type") == ["1"] and q.get("osplang")
                and q.get("ciid", ["1"]) == ["1"]):
            return DB_BASE + "get_image.action?" + html_lib.unescape(m.group(1))
    return None


_NAME_RE = re.compile(r'id="cardname"[^>]*>\s*<h1>\s*(.*?)\s*</h1>', re.S)


def _norm_name(s: str) -> str:
    """名前の比較用（大文字小文字・記号・空白の違いを無視する）。"""
    return re.sub(r"[\W_]+", "", html_lib.unescape(s)).casefold()


def page_is_card(page_html: str, name_en: str) -> bool:
    """ページが、そのカードの詳細ページか（メンテナンス画面・エラー画面・別カードを成功扱いにしない目印）。"""
    m = _NAME_RE.search(page_html)
    return bool(m) and _norm_name(m.group(1)) == _norm_name(name_en)


def fetch_official(cid: int, name_en: str) -> dict:
    """公式ページを取る。戻り値: {page_url, ok, status, pack(dict|None), image_url(str|None), error}。

    status: 'ok'=商品あり / 'empty'=ページは取れたが収録商品0件 / 'failed'=取得失敗（通信エラー・カードの目印なし）。
    ok は status != 'failed'。0件と取得失敗は区別する。
    """
    url = PAGE_URL.format(cid=cid)
    out = {"page_url": url, "ok": False, "status": "failed", "pack": None, "image_url": None, "error": ""}
    try:
        body, _ = _http_get(url)
        page = body.decode("utf-8", errors="replace")
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    if not page_is_card(page, name_en):
        out["error"] = 'ページにカード名の目印(id="cardname"・英語名)が無い（メンテナンス画面・エラー画面の可能性）'
        return out
    out["ok"] = True
    out["pack"] = parse_pack_list(page)
    out["status"] = "ok" if out["pack"] else "empty"
    out["image_url"] = parse_image_url(page, cid)
    return out


def download_image(image_url: str, page_url: str) -> tuple[bytes, str]:
    """画像を取り、PIL で開けるか確かめる。戻り値 (バイト, 拡張子)。失敗は例外。

    公式は Content-Type を application/octet-stream で返すため、中身で判定する。
    """
    from PIL import Image
    data, _ = _http_get(image_url, referer=page_url)
    if len(data) > _MAX_IMAGE_BYTES:
        raise ValueError("画像サイズが上限(5MB)を超えています")
    img = Image.open(io.BytesIO(data))
    fmt = (img.format or "").upper()
    img.verify()
    ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}.get(fmt)
    if ext is None:
        raise ValueError(f"想定外の画像形式: {fmt!r}")
    return data, ext


def store_image(sb, cid: int, data: bytes, ext: str) -> tuple[str, str]:
    """Storage に upsert 保存し (パス, 公開URL) を返す。失敗は例外。URLの ?v= は中身のハッシュ（unreleased_image_store と同じ考え方）。"""
    path = f"{STORAGE_DIR}/{cid}.{ext}"
    mime = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}[ext]
    sb.storage.from_(BUCKET).upload(path=path, file=data, file_options={"content-type": mime, "upsert": "true"})
    r = sb.storage.from_(BUCKET).get_public_url(path)
    base = (r if isinstance(r, str) else r.get("publicUrl", "")).split("?")[0]
    if not base:
        raise ValueError("公開URLが不明です")
    return path, f"{base}?v={hashlib.sha1(data).hexdigest()[:12]}"


# ──────────────────────────────────────────────
# 翻訳
# ──────────────────────────────────────────────

def needs_translation(existing: dict | None, row: dict) -> bool:
    """訳す必要があるか。新規・done/skipped 以外・「訳したときの英文」が今の英文と違う時だけ True。

    英文との比較は effect_text_en ではなく translated_source_* で行う（--no-translate で英文だけ更新されても、
    古い訳が done のまま固まらないように）。skipped（通常モンスター）は訳さないので False。
    """
    if existing is None:
        return True
    status = existing.get("translation_status")
    if status == "skipped":
        return False
    if status != "done":
        return True
    return ((existing.get("translated_source_en") or "") != row["effect_text_en"]
            or (existing.get("translated_source_pend_en") or "") != row["pendulum_effect_en"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def translated_fields(row: dict, model: str) -> dict:
    """翻訳済み（done）の記録欄。訳したときの英文を残す（英文が空のカードも同じ記録を入れる）。"""
    return {
        "effect_text_ja": "", "pendulum_effect_ja": "",
        "translation_model": model, "translation_warnings": [], "translated_at": _now(),
        "translation_status": "done", "translation_error": None,
        "translated_source_en": row["effect_text_en"],
        "translated_source_pend_en": row["pendulum_effect_en"],
    }


def translate_card(client, corpus, card: dict, row: dict, *, model: str, effort: str | None) -> dict:
    """効果文（とペンデュラム効果）を訳し、表に書く翻訳欄の dict を返す。失敗は例外。"""
    ja, pja, warnings = "", "", []
    usage = Counter()
    if row["effect_text_en"].strip():
        r = tcg_translate.translate(client, corpus, card, model=model, effort=effort)
        ja, warnings = r["text"], list(r["warnings"])
        usage.update(r["usage"])
    if row["pendulum_effect_en"].strip():
        r = tcg_translate.translate(client, corpus, card, model=model, effort=effort, text_key="pendEffect")
        pja = r["text"]
        warnings += [f"[ペンデュラム効果] {w}" for w in r["warnings"]]
        usage.update(r["usage"])
    out = translated_fields(row, model)
    out.update({"effect_text_ja": ja, "pendulum_effect_ja": pja, "translation_warnings": warnings,
                "_usage": dict(usage)})
    return out


# ──────────────────────────────────────────────
# 本体
# ──────────────────────────────────────────────

def process(en: dict, ja: dict, ids: list[int], *, sb, client, model: str, effort: str | None,
            translate: bool, dry_run: bool, max_translate: int | None = None) -> dict:
    """候補 ids を処理して件数のまとめを返す。dry_run のときは sb・client を使わない。"""
    vocab = build_vocab(en, ja)
    logger.info(f"対応表: 語{len(vocab.props)}・属性{len(vocab.attributes)}・魔法罠の種類{len(vocab.spell_trap)}")
    stats = Counter()
    new_names = []
    existing = {}
    if not dry_run and ids:
        resp = sb.table(TABLE).select("*").in_("konami_id", ids).execute()
        existing = {r["konami_id"]: r for r in (resp.data or [])}
    corpus = None
    cost = 0.0
    attempted = 0  # 翻訳APIを呼んだカード数（--max-translate の数え方）

    for cid in ids:
        card = en[cid]
        row = build_row(card, vocab)
        old = existing.get(cid)
        payload = dict(row)

        # 公式ページ: 商品と画像が揃っているカードは取り直さない
        have_all = bool(old and old.get("image_url") and old.get("product_name_en"))
        official = None if have_all else fetch_official(cid, row["name_en"])
        if official is not None:
            stats["fetched"] += 1
            payload["official_fetch_status"] = official["status"]
            payload["official_fetched_at"] = _now()
            if official["status"] == "failed":
                logger.warning(f"[{cid}] 公式ページの取得失敗（商品・画像は次回取り直す）: {official['error']}")
                stats["page_failed"] += 1
            elif official["status"] == "empty":
                logger.info(f"[{cid}] 公式ページは取得できたが収録商品は0件（まだ載っていない）")
                stats["pack_empty"] += 1
            else:
                payload.update(official["pack"])

        if dry_run:
            if official is None:
                shown = "（公式は取得済み）"
            else:
                shown = official["pack"] if official["ok"] else "公式ページ取得失敗"
            print(f"{cid} {row['name_en']} | {row['card_type']} | 属性={row['attribute']!r} | 種族={row['race']!r}"
                  f" | Lv{row['level']} R{row['rank']} L{row['link_val']} ATK={row['atk']} DEF={row['def']!r}"
                  f" | {shown} | 画像URL={'あり' if official and official['image_url'] else 'なし'}")
            stats["seen"] += 1
            continue

        # 画像（既にあれば取り直さない）。ページ取得失敗は page_failed で数えるので image_failed には足さない
        if official is not None and official["ok"] and not (old and old.get("image_url")):
            if official["image_url"]:
                try:
                    data, ext = download_image(official["image_url"], official["page_url"])
                    path, url = store_image(sb, cid, data, ext)
                    payload.update({"image_storage_path": path, "image_url": url,
                                    "image_source_url": official["image_url"], "image_page_url": official["page_url"]})
                except Exception as e:
                    logger.warning(f"[{cid}] 画像の取得・保存に失敗（文字情報は保存する・次回取り直す）: {type(e).__name__}: {e}")
                    stats["image_failed"] += 1
            else:
                logger.warning(f"[{cid}] 公式ページに画像URLが見つからない（次回取り直す）")
                stats["image_failed"] += 1

        # 翻訳
        if translate and needs_translation(old, row):
            if is_normal_monster(card):
                # 通常モンスターの説明文はフレーバーなので訳さない（effect_text_ja は空のまま）
                payload.update({"translation_status": "skipped", "translation_error": None})
                stats["skipped"] += 1
            elif not (row["effect_text_en"].strip() or row["pendulum_effect_en"].strip()):
                payload.update(translated_fields(row, model))
            elif max_translate is not None and attempted >= max_translate:
                logger.info(f"[{cid}] --max-translate の上限に達したので今回は訳さない（次回に回す）")
                stats["translate_deferred"] += 1
            else:
                attempted += 1
                if corpus is None:
                    corpus = tcg_translate.Corpus(en, ja)
                try:
                    tr = translate_card(client, corpus, card, row, model=model, effort=effort)
                    cost += _cost(tr.pop("_usage"), model)
                    payload.update(tr)
                    stats["translated"] += 1
                    if old is not None and old.get("translation_status") == "done":
                        stats["retranslated"] += 1
                except Exception as e:
                    logger.warning(f"[{cid}] 翻訳に失敗（次回取り直す）: {type(e).__name__}: {e}")
                    payload.update({"translation_status": "failed",
                                    "translation_error": f"{type(e).__name__}: {e}"[:500]})
                    stats["translate_failed"] += 1

        payload["updated_at"] = _now()
        # first_seen_at は含めない（INSERT 時だけ DB の DEFAULT が入り、更新では書き換わらない）
        sb.table(TABLE).upsert(payload, on_conflict="konami_id").execute()
        if old is None:
            stats["new"] += 1
            new_names.append(row["name_en"])
        else:
            stats["updated"] += 1
    stats["_cost_usd"] = cost
    stats["_new_names"] = new_names
    return stats


def load_cardnames(path: str = CARDNAMES_PATH) -> list[str]:
    """価格検索が使う日本語名の一覧（JSON の配列）を読む。読めない・配列でないときは例外。"""
    import json
    with open(path, encoding="utf-8") as f:
        names = json.load(f)
    if not isinstance(names, list):
        raise ValueError(f"{path} が配列ではありません")
    return [n for n in names if isinstance(n, str)]


def link_ocg_cards(ja: dict, cardnames: list[str], *, sb, dry_run: bool) -> dict:
    """日本に来たカード（表で active・ミラーの ja に同じ konami_id・日本語名が一覧に載っている）を ocg_linked に更新する。

    一覧に載っていない間は切り替えない。照合は完全一致 → fuzzy_key 同士（一覧側の正式名を ocg_name に入れる）。
    dry_run のときは書かず表示だけ（sb は読むだけ）。戻り値: {"linked": 件数, "pairs": [(英語名, 日本語名)]}
    """
    exact = set(cardnames)
    fuzzy = {}
    for n in cardnames:
        k = fuzzy_key(n)
        # 記号だけが違う別のカードが一覧に2つあるキーは None にして使わない（先勝ちで別のカード名を入れないため）
        fuzzy[k] = None if k in fuzzy and fuzzy[k] != n else n
    rows = sb.table(TABLE).select("konami_id, name_en").eq("status", "active").execute().data or []
    pairs = []
    for r in sorted(rows, key=lambda r: r["konami_id"]):
        j = ja.get(int(r["konami_id"]))
        jname = (j or {}).get("name") or ""
        if not jname:
            continue
        official = jname if jname in exact else fuzzy.get(fuzzy_key(jname))
        if official is None:
            logger.info(f"[{r['konami_id']}] 日本語版は出ているが日本語名が一覧(cardnames_ja.json)に未掲載"
                        f"（またはゆれで2つ以上に当たる）なので切り替えない: {jname}")
            continue
        if dry_run:
            print(f"{r['konami_id']} {r['name_en']} → {official}（日本に来た・dry-run のため更新しない）")
        else:
            now = _now()
            (sb.table(TABLE)
             .update({"status": "ocg_linked", "ocg_name": official, "ocg_linked_at": now, "updated_at": now})
             .eq("konami_id", r["konami_id"]).eq("status", "active").execute())
        pairs.append((r["name_en"], official))
    return {"linked": len(pairs), "pairs": pairs}


_warned_models = set()


def _cost(usage: dict, model: str) -> float:
    if model not in tcg_translate.PRICES:
        if model not in _warned_models:
            _warned_models.add(model)
            logger.warning(f"モデル {model!r} の単価が tcg_translate.PRICES に無いため費用を数えません（0扱い）")
        return 0.0
    return tcg_translate.cost_usd({k: usage.get(k, 0) for k in
                                   ("input_tokens", "output_tokens",
                                    "cache_creation_input_tokens", "cache_read_input_tokens")}, model)


def official_failure_message(stats: dict) -> str | None:
    """公式ページの取得失敗が全件、または半分以上なら知らせる文を返す（それ以外は None）。"""
    fetched, failed = stats.get("fetched", 0), stats.get("page_failed", 0)
    if fetched and failed * 2 >= fetched:
        return f"公式データベースのページ取得が{fetched}件中{failed}件失敗しました（メンテナンス・遮断の可能性）"
    return None


def format_summary(stats: dict) -> str:
    return (f"新規{stats.get('new', 0)}・更新{stats.get('updated', 0)}・訳し直し{stats.get('retranslated', 0)}"
            f"（翻訳計{stats.get('translated', 0)}・訳さない{stats.get('skipped', 0)}"
            f"・上限で見送り{stats.get('translate_deferred', 0)}）"
            f"・訳の失敗{stats.get('translate_failed', 0)}"
            f"・画像の失敗{stats.get('image_failed', 0)}・公式ページ取得失敗{stats.get('page_failed', 0)}"
            f"（取得{stats.get('fetched', 0)}件）・商品0件{stats.get('pack_empty', 0)}"
            f"・日本に来た{stats.get('ocg_linked', 0)}")


def _notify(message: str) -> None:
    from discord_notify import send_discord_message
    send_discord_message(message, no_mentions=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mirror", default=tcg_translate.DEFAULT_MIRROR_DIR, help="YGOResources ミラー（en/ ja/）のフォルダ")
    ap.add_argument("--dry-run", action="store_true", help="DBに書かず・翻訳APIも呼ばず、置き換え結果を表示するだけ")
    ap.add_argument("--limit", type=int, help="先頭からN枚だけ処理する（0は0件。未指定は全件）")
    ap.add_argument("--no-translate", action="store_true", help="翻訳しない")
    ap.add_argument("--max-translate", type=int, help="1回あたりの翻訳件数の上限（未指定は無制限）")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    _load_env_fallback()

    en, ja = tcg_translate.load_mirror(a.mirror)
    if not en or not ja:
        # ミラーが読めない（clone 失敗・パス違い）のに「候補0件」で成功扱いにしない
        logger.error(f"ミラーを読めません: {a.mirror}（en={len(en)}件・ja={len(ja)}件）")
        return 1
    ids = select_candidates(en, ja)
    logger.info(f"ミラー: 英語{len(en)}件・日本語{len(ja)}件 → 候補（英語のみ）{len(ids)}件")
    if len(ids) > MAX_CANDIDATES:
        # ミラーが一部欠けたと考えられる。翻訳も公式DBへのアクセスもせずに止める
        msg = (f"TCG先行カードの候補が{len(ids)}件で上限{MAX_CANDIDATES}件を超えました"
               f"（ミラーの欠けの可能性。英語{len(en)}件・日本語{len(ja)}件）。翻訳・公式DBアクセスは行わず中止しました")
        logger.error(msg)
        if not a.dry_run:
            _notify(msg)
        return 1
    if a.limit is not None:
        ids = ids[: a.limit]

    sb = client = None
    model = os.environ.get("TCG_TRANSLATE_MODEL") or DEFAULT_MODEL
    effort = os.environ.get("TCG_TRANSLATE_EFFORT") or None
    translate = not a.no_translate and not a.dry_run
    if not a.dry_run:
        from supabase import create_client
        if not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY")):
            logger.error("SUPABASE_URL / SUPABASE_KEY が未設定です")
            return 1
        sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
        if translate:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                logger.error("ANTHROPIC_API_KEY が未設定です")
                return 1
            import anthropic
            client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], timeout=300.0, max_retries=2)

    stats = process(en, ja, ids, sb=sb, client=client, model=model, effort=effort,
                    translate=translate, dry_run=a.dry_run, max_translate=a.max_translate)
    fail_msg = official_failure_message(stats)
    linked_pairs = []
    link_failed = False
    if a.dry_run:
        logger.info("dry-run は DB に接続しないので、日本に来たカードの判定は行いません")
    else:
        # 候補の処理とは別の段階。一覧が読めない・DB 更新に失敗しても、ここまでの取り込みは済んでいる
        try:
            linked = link_ocg_cards(ja, load_cardnames(), sb=sb, dry_run=False)
            stats["ocg_linked"] = linked["linked"]
            linked_pairs = linked["pairs"]
        except Exception as e:
            link_failed = True
            logger.error(f"日本に来たカードの切り替えに失敗: {type(e).__name__}: {e}")
    if a.dry_run:
        logger.info(f"dry-run 完了: {stats.get('seen', 0)}枚（公式ページ取得失敗{stats.get('page_failed', 0)}・商品0件{stats.get('pack_empty', 0)}）")
        return 1 if fail_msg else 0
    logger.info("まとめ: " + format_summary(stats) + f"・翻訳費用の目安 ${stats.get('_cost_usd', 0):.3f}")

    if stats.get("new", 0) >= 1:
        names = "、".join(stats["_new_names"][:5]) + ("…" if len(stats["_new_names"]) > 5 else "")
        _notify(f"TCG先行カードを新たに{stats['new']}枚取り込みました（{names}）")
    if linked_pairs:
        shown = "、".join(f"{en}→{jp}" for en, jp in linked_pairs[:5]) + ("…" if len(linked_pairs) > 5 else "")
        _notify(f"日本に来た{len(linked_pairs)}枚（{shown}）")
    if fail_msg:
        logger.error(fail_msg)
        _notify(fail_msg)
    # 訳の失敗は次回取り直すが、定期実行の失敗として気づけるよう終了コードに出す
    return 1 if (stats.get("translate_failed", 0) or fail_msg or link_failed) else 0


if __name__ == "__main__":
    sys.exit(main())
