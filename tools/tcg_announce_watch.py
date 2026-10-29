"""
tcg_announce_watch.py — 海外先行の予告カードの取り込み（1時間ごとの定期実行用）

予告カード＝英語版の新弾の収録として名前・効果の概要だけが先に出回り、公式データベースにまだ無いカード。
鮮度が大事なので1時間ごとに見る（2026-10-29 Maze of Machines は、翻訳アカウントの投稿 08:57・Yugipedia 10:00 で、
週1回の tcg_preview_watch では4日遅れになった）。

流れ:
  1. Yugipedia の「直近 N 日に新しく作られた Set Card Lists:… (TCG-EN)」と、表で追いかけ中（status=active）の弾を集める
  2. 弾ごとに一覧のページと、一覧の「New」の行のカードのページを毎回読む（1弾あたり問い合わせ数回）
  3. 公式DBに既にあるカード（database_id か jp_sets がある＝日本のカードの再録・既存カード）は除き、予告として
     持っていたなら in_db にして引退させる。一覧の「New」から外れたカードは dropped にする。カードのページの版が
     前回と同じで、要約も済んでいれば（失敗・見送りは MAX_ATTEMPTS 回まで取り直す）作り直さない
  4. 種別・属性・種族を tools/tcg_preview_watch の対応表で日本語にする
  5. 宣伝文（非公式）・英語テキストを日本語の概要にする（Claude）。英語テキスト（official）は全文も機械訳する
     （tcg_translate.translate）。要約したときの英文を残し、同じなら作り直さない
  6. 関連カード＝宣伝文がリンクしている既存カード（対応表で日本語の正式名が分かるものだけ。推測で元ネタを決めない）
  7. tcg_preview_cards（公式DB由来）に同じ英語名があるカードは status='in_db' にして引退させる
  8. 英語版の発売から CLOSE_AFTER_DAYS 日経った弾は status='closed'（追いかけをやめる）

手順 4〜6 に YGOResources のミラーが要る。変わったカードがあるときだけ読む（無ければ clone もしない）。

実行:
  python tools/tcg_announce_watch.py                         # 本番（DB に書く）
  python tools/tcg_announce_watch.py --dry-run --no-summarize # DB に書かず・API も呼ばず、読み取り結果を表示するだけ
  --mirror <フォルダ>（無ければ一時フォルダに sparse clone する）/ --days N（新しい弾を探す日数）
  --list-title "Set Card Lists:… (TCG-EN)"（その弾だけを試す）

認証: SUPABASE_URL / SUPABASE_KEY / ANTHROPIC_API_KEY（DISCORD_WEBHOOK_URL は任意）。未設定なら ~/.cardsouba/watcher.env。
モデルは TCG_ANNOUNCE_MODEL（既定 claude-opus-5-5。投稿にそのまま出る文なので、翻訳と同じ上位のモデル）。
"""

import argparse
import json
import logging
import os
import re
import subprocess
import unicodedata
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
_TOOLS = os.path.dirname(os.path.abspath(__file__))
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import tcg_announce as ya  # noqa: E402
import tcg_translate  # noqa: E402
import tcg_preview_watch as tpw  # noqa: E402  語彙の対応表・環境の読み込みを共用する

logger = logging.getLogger("tcg_announce_watch")

SETS = "tcg_announce_sets"
CARDS = "tcg_announce_cards"
DEFAULT_MODEL = "claude-opus-5-5"
MIRROR_REPO = "https://github.com/db-ygoresources-com/yugioh-card-history.git"
# 新しい弾を探す日数（1時間ごとに見るので短くてよいが、定期実行が止まっていた分も拾えるよう長めに）
DEFAULT_DAYS = 14
# 英語版の発売からこの日数で追いかけをやめる（それまでに公式DBに載って tcg_preview_cards へ移るはず）。TODO: calibrate from data
CLOSE_AFTER_DAYS = 30
# 1回で要約する上限（宣伝文が一度に大量に出回っても費用を抑える。残りは次の回）。TODO: calibrate from data
MAX_SUMMARIZE = 40
# 同じ英文で要約を試す回数の上限（失敗・見送りは次の回に取り直すが、失敗が続いても費用が膨らまないように）
MAX_ATTEMPTS = 3


# ──────────────────────────────────────────────
# 要約（宣伝文・英語テキスト → 日本語の概要）
# ──────────────────────────────────────────────

SUMMARY_SYSTEM = """遊戯王カードの英語の説明文を読み、そのカードが何をするかを日本語の短い箇条書きにまとめます。

- 説明文に書かれていることだけを書く。書かれていない効果・条件・数値を足さない。
- 販売店の宣伝文のことがある。宣伝の言い回し（「最高の」「お見逃しなく」など）は落とし、カードの働きだけを残す。
- 遊戯王OCGの日本語の用語で書く（Special Summon→特殊召喚、Graveyard/GY→墓地、Deck→デッキ、destroy→破壊、
  banish→除外、Quick Effect→相手ターンにも使える効果、DARK→闇属性、Machine→機械族）。
- 英語の "…" で囲まれた名前は、名前の対応表にあれば日本語の正式名を「」に入れる。無ければ英語のまま「」に入れる（カタカナにしない）。
- lines は効果1つにつき1行、1行40字前後まで。
- 説明文に書かれていないことについての注記（「〜は記載なし」など）は書かない。分からない部分は書かずに省く。
- short は投稿に載せる1〜2文（80字以内）。いちばん特徴的な働きが伝わるように書く。カード名・カードの種類（融合モンスターなど）は別の欄に出るので書かない。"""

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {"type": "array", "items": {"type": "string"}},
        "short": {"type": "string"},
    },
    "required": ["lines", "short"],
    "additionalProperties": False,
}


def summary_message(corpus, name_en: str, kind_label: str, text_en: str) -> str:
    gl = corpus.glossary_for(text_en)
    names = "\n".join(f"- {k} → {v}" for k, v in gl.items()) if gl else "（該当なし）"
    return (f"名前の対応表（英語 → 日本語の正式名）:\n{names}\n\n"
            f"カード: {name_en}（{kind_label}）\n説明文:\n{text_en}")


def summarize(client, corpus, card: dict, name_en: str, *, model: str) -> dict:
    """1枚の概要を作る。戻り値: {lines, short, warnings, usage}。"""
    lore = card["lore"]
    kind_label = "販売店の宣伝文" if lore["kind"] == "blurb" else "カードの英語テキスト"
    text_en = lore["text_en"] + (("\nペンデュラム効果: " + card["pendulum_lore"]["text_en"])
                                 if card["pendulum_lore"]["text_en"] else "")
    message = client.messages.create(
        model=model, max_tokens=2000,
        system=[{"type": "text", "text": SUMMARY_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": summary_message(corpus, name_en, kind_label, text_en)}],
        extra_body={"output_config": {"format": {"type": "json_schema", "schema": SUMMARY_SCHEMA}}},
    )
    if message.stop_reason != "end_turn":
        raise ValueError(f"要約の応答が未完了です: {message.stop_reason}")
    raw = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
    out = json.loads(raw)
    lines = [s.strip() for s in out.get("lines") or [] if s.strip()]
    short = (out.get("short") or "").strip()
    if not lines or not short:
        raise ValueError("要約が空です")
    # 英文に無い数字を作っていないかを確かめる。あれば自動投稿しない（ページには出す）
    warnings = invented_numbers(text_en, "\n".join(lines) + "\n" + short)
    return {"lines": lines, "short": short, "warnings": warnings,
            "usage": tcg_translate.usage_dict(message.usage)}


_DIGITS = re.compile(r"\d+")
_NUM_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "twice": 2}


def invented_numbers(text_en: str, text_ja: str) -> list[str]:
    """要約に出てくる2以上の数字のうち、元の英文に無いもの（作り話の目印）。

    tcg_translate.check は全文の訳の点検（英文の名前・数字が訳に全部あるか）で、要約に使うと
    関連カードの名前や「MZMC-001」の番号まで「訳に無い」と出る。要約で困るのは逆向き＝英文に無い数字を足すことなので、それだけを見る。
    1 は「an enemy monster → 1体」のように英文に数字が無くても自然に出るので見ない。
    """
    src = {int(n) for n in _DIGITS.findall(unicodedata.normalize("NFKC", text_en))}
    src |= {v for w, v in _NUM_WORDS.items() if re.search(r"\b" + w + r"\b", text_en, re.I)}
    out = []
    for n in _DIGITS.findall(unicodedata.normalize("NFKC", text_ja)):
        msg = f"数値 {n} が英文に無い"
        if int(n) >= 2 and int(n) not in src and msg not in out:
            out.append(msg)
    return out


def related_cards(corpus, raw_lore: str, own_name: str) -> list[str]:
    """宣伝文がリンクしている既存カードの日本語の正式名（対応表に完全一致するものだけ・自分自身は除く）。"""
    out = []
    for t in ya.link_targets(raw_lore):
        ja = corpus.card_names.get(t)
        if ja and t != own_name and ja not in out:
            out.append(ja)
    return out


# ──────────────────────────────────────────────
# 行の組み立て
# ──────────────────────────────────────────────

def card_type_ja(card: dict, vocab) -> tuple[str, str, str]:
    """(card_type, attribute, race)。種類が空欄の魔法・罠は「通常」と決めつけず「魔法」「罠」とだけ書く。"""
    m = card["mirror_like"]
    if m["type"] in ("spell", "trap"):
        word = tpw._SPELL_TRAP_WORD[m["type"]]
        if m["englishProperty"] is None and not card.get("property_is_normal"):
            return word, "", ""
        return tpw.build_card_type(m, vocab), "", ""
    if m["type"] != "monster":
        return "", "", ""
    ctype, race = tpw.translate_monster_terms(m, vocab)
    attr = tpw.translate_attribute(m, vocab) if m["englishAttribute"] else ""
    return ctype, attr, race


def build_row(page_id: int, page: dict, card: dict, list_row: dict, prefix: str, vocab) -> dict:
    ctype, attr, race = card_type_ja(card, vocab)
    code = list_row["code"]
    # 一覧の番号が未定（??）でも、カードのページ側で決まっていればそちらを使う
    if "?" in code:
        known = [c for c in card["en_set_codes"] if c.startswith(prefix + "-") and "?" not in c]
        code = known[0] if known else code
    return {
        "page_id": page_id,
        "set_prefix": prefix,
        "set_code": code,
        "list_order": list_row["order"],
        "page_title": page["title"],
        "page_revid": page["revid"],
        "name_en": page["title"],
        "unofficial_name": card["unofficial_name"],
        "card_type": ctype,
        "attribute": attr,
        "race": race,
        "level": card["level"],
        "rank": card["rank"],
        "link_val": card["link_val"],
        "atk": card["atk"],
        "def": card["def"],
        "pendulum_scale": card["pendulum_scale"],
        "link_arrows": card["link_arrows"],
        "lore_kind": card["lore"]["kind"],
        "lore_en": card["lore"]["text_en"],
        "lore_source_url": card["lore"]["source_url"],
        "pendulum_lore_en": card["pendulum_lore"]["text_en"],
        # 一覧から一度消えて（dropped）戻ってきたカードも予告中に戻す
        "status": "announced",
    }


def summary_source(row: dict) -> str:
    """要約の材料の英文（これが変わったら作り直す）。"""
    return row["lore_kind"] + "\n" + row["lore_en"] + "\n" + row["pendulum_lore_en"]


def attempts_so_far(existing: dict | None, row: dict) -> int:
    """要約を試した回数。英文が変わったら数え直す（新しい英文は改めて MAX_ATTEMPTS 回まで試す）。"""
    if existing is None or existing.get("summary_source_seen") != summary_source(row):
        return 0
    return existing.get("summary_attempts") or 0


def needs_summary(existing: dict | None, row: dict) -> bool:
    """要約を作る（作り直す）か。失敗・見送りは MAX_ATTEMPTS 回まで取り直す（費用が膨らまないよう上限つき）。"""
    if row["lore_kind"] == "none":
        return False
    if existing is not None and existing.get("summary_status") == "done" \
            and existing.get("summarized_source") == summary_source(row):
        return False
    return attempts_so_far(existing, row) < MAX_ATTEMPTS


def settled(existing: dict) -> bool:
    """前回の行が、ページが変わらない限り読み直さなくてよい状態か。"""
    if existing.get("status") != "announced":
        return False
    st = existing.get("summary_status")
    return st in ("done", "skipped") or (existing.get("summary_attempts") or 0) >= MAX_ATTEMPTS


# ──────────────────────────────────────────────
# DB
# ──────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state(sb) -> tuple[dict[str, dict], dict[int, dict]]:
    sets = {r["prefix"]: r for r in (sb.table(SETS).select("*").execute().data or [])}
    cards = {}
    start = 0
    while True:
        rows = sb.table(CARDS).select("*").range(start, start + 999).execute().data or []
        cards.update({r["page_id"]: r for r in rows})
        if len(rows) < 1000:
            break
        start += 1000
    return sets, cards


def preview_names(sb) -> set[str]:
    """tcg_preview_cards（公式DB由来）の英語名。ここにある予告カードは引退させる。"""
    rows = sb.table("tcg_preview_cards").select("name_en").execute().data or []
    return {r["name_en"] for r in rows if r.get("name_en")}


def ensure_mirror(path: str | None) -> str:
    """ミラーのフォルダを返す。無ければ一時フォルダに en/ ja/ だけ sparse clone する。"""
    if path and os.path.isdir(os.path.join(path, "en")):
        return path
    dest = os.path.join(tempfile.mkdtemp(prefix="ygores-"), "mirror")
    logger.info(f"ミラーを取得します: {dest}")
    subprocess.run(["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", MIRROR_REPO, dest],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", dest, "sparse-checkout", "set", "en", "ja"], check=True, capture_output=True)
    return dest


# ──────────────────────────────────────────────
# 本体
# ──────────────────────────────────────────────

def collect(list_titles: list[str], known_sets: dict[str, dict], known_cards: dict[int, dict],
            in_db_names: set[str]) -> dict:
    """Yugipedia を読み、変わった弾・カードを集める（DB にも AI にも触らない）。

    戻り値: {sets: [弾の行], changed: [(page_id, page, card, list_row, prefix)], seen_ids: {prefix: set(page_id)},
            retire: [page_id]（公式DBに載った）, drop: [page_id]（一覧の「New」から消えた）}
    """
    by_title = {s["list_title"]: s for s in known_sets.values()}
    out = {"sets": [], "changed": [], "seen_ids": {}, "retire": [], "drop": [], "skipped_known": 0}
    # 一覧のページは毎回読む（1弾あたり問い合わせ数回）。カードのページだけ書き換わること（宣伝文→正式テキスト）が
    # あるので、カードのページも毎回読み、版の比較で作り直すかを決める
    lists = ya.fetch_pages(titles=list_titles)
    for list_pid, lp in lists.items():
        prev = by_title.get(lp["title"])
        rows = [r for r in ya.parse_set_list(lp["text"]) if ya.is_new_card(r)]
        set_title = lp["title"].removeprefix("Set Card Lists:").removesuffix(ya.SET_LIST_SUFFIX)
        sp = ya.fetch_pages(titles=[set_title]) if rows else {}
        info = ya.parse_set_page(next(iter(sp.values()))["text"]) if sp else {}
        prefix = info.get("prefix") or (rows[0]["code"].split("-")[0] if rows else (prev or {}).get("prefix", ""))
        aliases: dict[str, str] = {}
        cards = ya.fetch_pages(titles=[r["name"] for r in rows], aliases=aliases) if rows else {}
        by_name = {c["title"]: (pid, c) for pid, c in cards.items()}
        seen = set()
        for r in rows:
            # 名前の正規化・ページの移動（予告名→正式名）は API の対応表でたどる
            hit = by_name.get(ya.resolve_title(r["name"], aliases))
            if hit is None:
                continue  # まだカードのページが無い（赤リンク）
            pid, page = hit
            card = ya.parse_card(page["text"])
            if card is None:
                continue
            if ya.exists_in_konami_db(card) or page["title"] in in_db_names:
                # 公式DBに載った（または日本のカードの再録）。予告として持っていたカードなら引退させる
                if pid in known_cards:
                    seen.add(pid)
                    out["retire"].append(pid)
                continue
            seen.add(pid)
            prev_card = known_cards.get(pid)
            if (prev_card and prev_card.get("page_revid") == page["revid"]
                    and prev_card.get("list_order") == r["order"] and settled(prev_card)):
                out["skipped_known"] += 1
                continue
            out["changed"].append((pid, page, card, r, prefix))
        if prefix:
            # 前回まで予告中だったのに、今回の一覧の「New」に無いカード（一覧から外れた・ページの作り直しで番号が変わった）
            out["drop"] += [pid for pid, c in known_cards.items()
                            if c.get("set_prefix") == prefix and c.get("status") == "announced" and pid not in seen]
        if not seen and prev is None:
            continue  # 新規カードが全部既存（日本のカードの再録だけの弾）なら追いかけない
        out["seen_ids"][prefix] = seen
        out["sets"].append({
            "prefix": prefix,
            "slug": ya.slugify(info.get("name_en") or set_title),
            "name_en": info.get("name_en") or set_title,
            "list_title": lp["title"],
            "list_page_id": list_pid,
            "list_revid": lp["revid"],
            "set_title": set_title if sp else "",
            "size": info.get("size"),
            "tcg_release_date": info.get("release_date"),
        })
    return out


def set_payload(s: dict, known_sets: dict[str, dict]) -> dict:
    """弾の行。slug（URL）は最初の1回だけ決める（名前が変わっても投稿済みのリンクを 404 にしない）。
    他の弾と重なるとき・英数字の無い名前のときは接頭辞を付けて重ならないようにする。"""
    payload = dict(s)
    if s["prefix"] in known_sets:
        payload.pop("slug", None)
        return payload
    taken = {k.get("slug") for p, k in known_sets.items() if p != s["prefix"]}
    if payload["slug"] in taken or payload["slug"] == "set":
        payload["slug"] = f"{payload['slug']}-{s['prefix'].lower()}"
    return payload


def process(collected: dict, known_cards: dict[int, dict], *, sb, client, model: str, mirror: str | None,
            summarize_on: bool, dry_run: bool, today: date, known_sets: dict[str, dict] | None = None) -> dict:
    stats = {"sets": len(collected["sets"]), "new": 0, "updated": 0, "summarized": 0, "summary_failed": 0,
             "summary_deferred": 0, "official_new": 0, "retired": 0, "closed": 0, "_new_names": [],
             "_new_sets": [], "_cost_usd": 0.0, "skipped_known": collected["skipped_known"],
             "_rows": {"sets": [], "cards": []}}
    corpus = vocab = None
    if collected["changed"]:
        mdir = ensure_mirror(mirror)
        en, ja = tcg_translate.load_mirror(mdir)
        if not en or not ja:
            raise RuntimeError(f"ミラーを読めません: {mdir}（en={len(en)}件・ja={len(ja)}件）")
        vocab = tpw.build_vocab(en, ja)
        corpus = tcg_translate.Corpus(en, ja)

    for s in collected["sets"]:
        payload = dict(set_payload(s, known_sets or {}), updated_at=_now())
        rd = s.get("tcg_release_date")
        if rd and date.fromisoformat(rd) + timedelta(days=CLOSE_AFTER_DAYS) < today:
            payload["status"] = "closed"
            stats["closed"] += 1
        stats["_rows"]["sets"].append(payload)
        if dry_run:
            logger.info(f"[dry-run] 弾 {s['prefix']} {s['name_en']}（発売 {rd}・収録 {s.get('size')}）")
        else:
            sb.table(SETS).upsert(payload, on_conflict="prefix").execute()

    summarized = 0
    for pid, page, card, list_row, prefix in collected["changed"]:
        row = build_row(pid, page, card, list_row, prefix, vocab)
        existing = known_cards.get(pid)
        if existing is None:
            stats["new"] += 1
            stats["_new_names"].append(row["name_en"])
        else:
            stats["updated"] += 1
        if row["lore_kind"] == "official" and (existing is None or existing.get("lore_kind") != "official"):
            row["official_at"] = _now()
            stats["official_new"] += 1
        row["related_ja"] = related_cards(corpus, card["lore"]["raw"], row["name_en"])
        if row["lore_kind"] == "none":
            row.update(summary_status="skipped", summary_ja=[], summary_short_ja="", effect_text_ja="")
        elif needs_summary(existing, row):
            # 試した回数は英文ごとに数える（summary_source_seen が今の英文と違えば 0 から）
            row["summary_source_seen"] = summary_source(row)
            row["summary_attempts"] = attempts_so_far(existing, row)
            if not summarize_on:
                row["summary_status"] = "pending"
            elif summarized >= MAX_SUMMARIZE:
                row["summary_status"] = "pending"
                stats["summary_deferred"] += 1
            else:
                summarized += 1
                row["summary_attempts"] += 1
                try:
                    s = summarize(client, corpus, card, row["name_en"], model=model)
                    row.update(summary_ja=s["lines"], summary_short_ja=s["short"], summary_warnings=s["warnings"],
                               summary_status="done", summary_error=None, summary_model=model,
                               summarized_source=summary_source(row), summarized_at=_now())
                    stats["_cost_usd"] += tpw._cost(s["usage"], model)
                    if row["lore_kind"] == "official":
                        t = tcg_translate.translate(client, corpus, dict(card["mirror_like"], name=row["name_en"],
                                                                         effectText=row["lore_en"]), model=model)
                        row["effect_text_ja"] = t["text"]
                        # 全文訳の点検（名前・数字が訳に全部あるか）は警告が出やすいので、投稿を止める要約の警告とは分ける
                        row["effect_warnings"] = t["warnings"]
                        stats["_cost_usd"] += tpw._cost(t["usage"], model)
                        if row["pendulum_lore_en"]:
                            tp = tcg_translate.translate(client, corpus, dict(card["mirror_like"], name=row["name_en"],
                                                                              pendEffect=row["pendulum_lore_en"]),
                                                         model=model, text_key="pendEffect")
                            row["pendulum_effect_ja"] = tp["text"]
                            stats["_cost_usd"] += tpw._cost(tp["usage"], model)
                    stats["summarized"] += 1
                except Exception as e:
                    stats["summary_failed"] += 1
                    row.update(summary_status="failed", summary_error=f"{type(e).__name__}: {e}"[:500])
                    logger.error(f"要約に失敗: {row['name_en']}: {e}")
        stats["_rows"]["cards"].append(row)
        if dry_run:
            logger.info(f"[dry-run] {row['set_code']} {row['name_en']}｜{row['card_type']}｜{row['attribute']}"
                        f"{row['race']}｜★{row['level']}｜{row['atk']}/{row['def']}｜{row['lore_kind']}"
                        f"｜関連 {row['related_ja']}｜{row.get('summary_short_ja', '')}")
        else:
            row["updated_at"] = _now()
            sb.table(CARDS).upsert(row, on_conflict="page_id").execute()

    retire = [p for p in collected["retire"] if known_cards.get(p, {}).get("status") != "in_db"]
    drop = [p for p in collected.get("drop", []) if p not in set(collected["retire"])]
    stats["retired"], stats["dropped"] = len(retire), len(drop)
    if not dry_run:
        if retire:
            sb.table(CARDS).update({"status": "in_db", "updated_at": _now()}).in_("page_id", retire).execute()
        if drop:
            sb.table(CARDS).update({"status": "dropped", "updated_at": _now()}).in_("page_id", drop).execute()
    return stats


def format_summary(stats: dict) -> str:
    return (f"弾{stats['sets']}・新規{stats['new']}・更新{stats['updated']}（変わらず{stats['skipped_known']}）"
            f"・要約{stats['summarized']}（失敗{stats['summary_failed']}・上限で見送り{stats['summary_deferred']}）"
            f"・正式テキスト判明{stats['official_new']}・公式DBへ移行{stats['retired']}・一覧から外れた{stats.get('dropped', 0)}"
            f"・追いかけ終了{stats['closed']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mirror", help="YGOResources ミラー（en/ ja/）のフォルダ。無ければ一時フォルダに取る")
    ap.add_argument("--dry-run", action="store_true", help="DB に書かず、読み取り結果を表示するだけ")
    ap.add_argument("--no-summarize", action="store_true", help="要約しない（AI を呼ばない）")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS, help="新しい弾を探す日数")
    ap.add_argument("--list-title", action="append", help="この「Set Card Lists:… (TCG-EN)」だけを見る（複数可）")
    ap.add_argument("--dump", help="作った行（弾・カード）を JSON に書き出す（手元で画面を確かめる用）")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    tpw._load_env_fallback()

    sb = client = None
    known_sets, known_cards, in_db = {}, {}, set()
    if not a.dry_run:
        from supabase import create_client
        if not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY")):
            logger.error("SUPABASE_URL / SUPABASE_KEY が未設定です")
            return 1
        sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
        known_sets, known_cards = load_state(sb)
        in_db = preview_names(sb)
    summarize_on = not a.no_summarize
    model = os.environ.get("TCG_ANNOUNCE_MODEL") or DEFAULT_MODEL
    if summarize_on:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            logger.error("ANTHROPIC_API_KEY が未設定です（要約しないなら --no-summarize）")
            return 1
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], timeout=300.0, max_retries=2)

    if a.list_title:
        titles = a.list_title
    else:
        titles = [r["title"] for r in ya.recent_set_lists(a.days)]
        titles += [s["list_title"] for s in known_sets.values() if s.get("status") == "active"]
        titles = list(dict.fromkeys(titles))
    logger.info(f"見る弾: {len(titles)}件 {titles[:5]}")
    if not titles:
        return 0

    collected = collect(titles, known_sets, known_cards, in_db)
    new_prefixes = [s for s in collected["sets"] if s["prefix"] not in known_sets]
    stats = process(collected, known_cards, sb=sb, client=client, model=model, mirror=a.mirror,
                    summarize_on=summarize_on, dry_run=a.dry_run, today=date.today(), known_sets=known_sets)
    if a.dump:
        with open(a.dump, "w", encoding="utf-8") as f:
            json.dump(stats["_rows"], f, ensure_ascii=False, indent=1, default=str)
    logger.info("まとめ: " + format_summary(stats) + f"・費用の目安 ${stats['_cost_usd']:.3f}")
    if not a.dry_run:
        if new_prefixes:
            tpw._notify("海外先行の新しい弾を見つけました: " + "、".join(f"{s['name_en']}（{s['prefix']}）" for s in new_prefixes))
        if stats["new"]:
            names = "、".join(stats["_new_names"][:5]) + ("…" if len(stats["_new_names"]) > 5 else "")
            tpw._notify(f"海外先行の予告カードを{stats['new']}枚取り込みました（{names}）")
        # 取り込みの後に、X 投稿を1件だけ判断して出す（既定は無効。tcg_announce_post の説明を参照）
        try:
            import tcg_announce_post
            logger.info("投稿: " + tcg_announce_post.run(sb))
        except Exception as e:
            logger.error(f"投稿の判断に失敗: {type(e).__name__}: {e}")
            return 1
    return 1 if stats["summary_failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
