"""
tcg_announce.py — 海外先行の予告カード（英語版の新弾で、公式データベースにまだ載っていないカード）の読み取り

予告カード＝英語版の新弾の収録として名前・効果の概要だけが先に出回ったカード。出どころは販売店の商品説明
（southernhobby.com など）や公式の先行公開で、Yugipedia が数時間以内にまとめる。公式データベースに載ると
YGOResources のミラー経由で tcg_preview_cards（tools/tcg_preview_watch.py）に入るので、そこで予告の行は引退する。

ここには Yugipedia の API への問い合わせと、wikitext の読み取りだけを置く（DB には触らない）。
使うのは tools/tcg_announce_watch.py（1時間ごとの取り込み）。

Yugipedia の書き方のくせ（2026-10-29 Maze of Machines で確認）:
  - 新しい弾は名前空間 3006 の「Set Card Lists:<弾名> (TCG-EN)」が新しく作られたことで分かる
  - 一覧の1行は「MZMC-EN005; Rampant Barrel Dragon;; New」。4つ目が New なら新規カード、
    「New artwork」は絵違いの再録、「Reprint」は再録。収録番号が未定だと「MZMC-EN0??」
  - 日本の漫画・アニメ由来のカードは、日本で未発売でも ja_name が入る（Space Megatron＝メガトロン）。
    日本にあるかは ja_name ではなく database_id（公式DBの番号）と jp_sets（日本の収録）で見る
  - 宣伝文は text = {{quote|text=…|source=Blurb from [URL 名前]}}、ページ先頭に {{Unofficial lore|English}}
  - 分からない欄は「???」、効果が未公開だと text = ''N/A''
"""

import json
import re
import time
import urllib.parse
import urllib.request

API_URL = "https://yugipedia.com/api.php"
WIKI_URL = "https://yugipedia.com/wiki/"
# Yugipedia は連絡先入りの UA を求めている
UA = "GreedyLabBot/1.0 (https://greedy-lab.com; daiyanog@gmail.com)"
SET_LIST_NS = 3006
SET_LIST_SUFFIX = " (TCG-EN)"
# 一度の問い合わせに入れるページ数（MediaWiki の上限は 50）
BATCH = 50
# 問い合わせの間隔（秒）。1時間ごとの実行で数回しか呼ばないが、連続呼び出しは間を空ける
MIN_INTERVAL = 1.0

_last_request = 0.0


def _get(params: dict) -> dict:
    global _last_request
    wait = MIN_INTERVAL - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    q = dict(params, format="json", formatversion="2")
    req = urllib.request.Request(API_URL + "?" + urllib.parse.urlencode(q), headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    finally:
        _last_request = time.monotonic()
    if "error" in data:
        raise RuntimeError(f"Yugipedia API エラー: {data['error']}")
    return data


def recent_set_lists(days: int = 14) -> list[dict]:
    """直近 days 日に新しく作られた「Set Card Lists:… (TCG-EN)」のページ（新しい順）。"""
    data = _get({"action": "query", "list": "recentchanges", "rcnamespace": SET_LIST_NS, "rctype": "new",
                 "rclimit": 500, "rcprop": "title|timestamp|ids",
                 "rcend": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))})
    return [{"title": r["title"], "page_id": r["pageid"], "created_at": r["timestamp"]}
            for r in data["query"]["recentchanges"] if r["title"].endswith(SET_LIST_SUFFIX)]


def fetch_pages(titles: list[str] | None = None, page_ids: list[int] | None = None,
                aliases: dict[str, str] | None = None) -> dict[int, dict]:
    """ページの wikitext と版番号を取る。返り値は page_id → {title, revid, text}。無いページは入らない。

    ページの移動（予告名→正式名など）は redirects で追い、移動先のページ番号で返す。aliases を渡すと、
    API が返す「頼んだ名前 → 正規化した名前」「移動元 → 移動先」をそこへ足す（resolve_title でたどる）。
    """
    keys = [("titles", t) for t in (titles or [])] + [("pageids", str(p)) for p in (page_ids or [])]
    out: dict[int, dict] = {}
    for kind in ("titles", "pageids"):
        vals = [v for k, v in keys if k == kind]
        for i in range(0, len(vals), BATCH):
            data = _get({"action": "query", kind: "|".join(vals[i:i + BATCH]), "redirects": 1,
                         "prop": "revisions", "rvprop": "content|ids", "rvslots": "main"})
            if aliases is not None:
                for m in data["query"].get("normalized", []) + data["query"].get("redirects", []):
                    aliases[m["from"]] = m["to"]
            for p in data["query"].get("pages", []):
                if p.get("missing") or p.get("invalid") or not p.get("revisions"):
                    continue
                rev = p["revisions"][0]
                out[p["pageid"]] = {"title": p["title"], "revid": rev["revid"],
                                    "text": rev["slots"]["main"]["content"]}
    return out


def resolve_title(title: str, aliases: dict[str, str]) -> str:
    """fetch_pages の aliases をたどって、最終的なページ名にする（正規化 → 移動 → 移動 …）。"""
    seen = set()
    while title in aliases and title not in seen:
        seen.add(title)
        title = aliases[title]
    return title


# ──────────────────────────────────────────────
# wikitext の読み取り
# ──────────────────────────────────────────────

def template_params(text: str, name: str) -> dict[str, str] | None:
    """{{name … }} の「| key = value」を辞書にする（値は複数行のこともある）。無ければ None。

    入れ子の {{…}}・[[…|…]] の中の「|」で区切らないよう、深さを数えて最上位の「|」だけで分ける。
    """
    m = re.search(r"\{\{\s*" + re.escape(name) + r"\s*(?=[|\n}])", text)
    if not m:
        return None
    i, depth, start = m.end(), 0, m.end()
    parts: list[str] = []
    while i < len(text):
        two = text[i:i + 2]
        if two in ("{{", "[["):
            depth += 1
            i += 2
            continue
        if two in ("}}", "]]"):
            if depth == 0 and two == "}}":
                parts.append(text[start:i])
                break
            # 対になる開きが無い「]]」（書き損じ）で深さを負にしない
            depth = max(depth - 1, 0)
            i += 2
            continue
        if text[i] == "|" and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    params: dict[str, str] = {}
    for part in parts:
        key, sep, val = part.partition("=")
        if sep:
            params[key.strip()] = val.strip()
    return params


_LINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]*))?\]\]")
_EXT_LINK = re.compile(r"\[(https?://\S+)(?:\s+([^\]]*))?\]")


def link_targets(text: str) -> list[str]:
    """[[A]]・[[A|B]] のリンク先 A を出た順に（重複なし）。"""
    seen: list[str] = []
    for m in _LINK.finditer(text or ""):
        t = m.group(1).strip()
        if t and t not in seen:
            seen.append(t)
    return seen


def plain_text(text: str) -> str:
    """リンク・太字斜体・改行タグを外した文。[[A|B]] は B、[[A]] は A、[URL 名前] は 名前。"""
    s = _LINK.sub(lambda m: (m.group(2) if m.group(2) is not None else m.group(1)), text or "")
    s = _EXT_LINK.sub(lambda m: m.group(2) or m.group(1), s)
    s = re.sub(r"'{2,}", "", s)
    s = re.sub(r"<br\s*/?>", "\n", s)
    return s.strip()


def parse_lore(text_param: str, page_text: str) -> dict:
    """効果文の欄を {kind, text_en, source_url, raw} にする。

    kind: blurb＝販売店などの宣伝文（非公式）/ official＝カードの英語テキスト / none＝未公開（N/A・空）
    raw は [[…]] を残した元の文（関連カードのリンク先を拾うのに使う）。
    """
    raw = (text_param or "").strip()
    if not raw or plain_text(raw).strip("' ") in ("", "N/A"):
        return {"kind": "none", "text_en": "", "source_url": "", "raw": ""}
    q = template_params(raw, "quote")
    if q is not None and q.get("text"):
        m = _EXT_LINK.search(q.get("source", ""))
        return {"kind": "blurb", "text_en": plain_text(q["text"]), "source_url": m.group(1) if m else "",
                "raw": q["text"]}
    kind = "blurb" if re.search(r"\{\{\s*Unofficial lore", page_text) else "official"
    return {"kind": kind, "text_en": plain_text(raw), "source_url": "", "raw": raw}


_LIST_LINE = re.compile(r"^([A-Z0-9]+-[A-Z]{2}[0-9?]+)\s*;\s*([^;]*?)\s*;\s*([^;]*?)\s*;\s*([^/\n]*?)\s*(?://.*)?$")


def parse_set_list(text: str) -> list[dict]:
    """「Set Card Lists」の {{Set list|…}} の各行を {code, name, rarity, mark, order} にする（名前が空の行は除く）。

    mark は 4つ目の欄（New / New artwork / Reprint / 空）。code は未定なら「MZMC-EN0??」のまま。
    """
    m = re.search(r"\{\{\s*Set list\b(.*?)\n\}\}", text, re.S)
    body = m.group(1) if m else text
    rows = []
    for order, line in enumerate(body.splitlines()):
        lm = _LIST_LINE.match(line.strip())
        if not lm or not lm.group(2).strip():
            continue
        rows.append({"code": lm.group(1), "name": lm.group(2).strip(), "rarity": lm.group(3).strip(),
                     "mark": lm.group(4).strip(), "order": order})
    return rows


def is_new_card(row: dict) -> bool:
    """一覧の行が新規カード（絵違い・再録ではない）か。"""
    return row.get("mark", "").strip().lower() == "new"


_MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August",
                                       "September", "October", "November", "December"], 1)}


def parse_release_date(s: str) -> str | None:
    """「March 19, 2027」→「2027-03-19」。読めなければ None。"""
    m = re.match(r"\s*([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})", plain_text(s or ""))
    if not m or m.group(1) not in _MONTHS:
        return None
    return f"{m.group(3)}-{_MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"


def parse_set_page(text: str) -> dict:
    """弾のページ（{{Infobox set}}）から {name_en, prefix, size, release_date}。"""
    p = template_params(text, "Infobox set") or {}
    size = re.match(r"\s*(\d+)", p.get("size", ""))
    image = plain_text(p.get("image", "")).strip()
    return {"name_en": plain_text(p.get("en_name", "")), "prefix": p.get("prefix", "").strip(),
            "size": int(size.group(1)) if size else None,
            "release_date": parse_release_date(p.get("na_release_date", "")),
            "image": image if re.search(r"\.(png|jpe?g|webp)$", image, re.I) else ""}


def file_url(filename: str) -> str:
    """Yugipedia にアップロードされた画像の URL（まだ無ければ空文字）。弾の商品写真（MZMC-BoosterEN.png など）に使う。"""
    if not filename:
        return ""
    data = _get({"action": "query", "titles": "File:" + filename, "prop": "imageinfo", "iiprop": "url"})
    for p in data["query"].get("pages", []):
        info = p.get("imageinfo") or []
        if info and info[0].get("url", "").startswith("https://"):
            return info[0]["url"]
    return ""


def slugify(name: str) -> str:
    """URL 用の名前「Maze of Machines」→「maze-of-machines」。"""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "set"


def _unknown(v: str) -> bool:
    return not v or "?" in v


def _int_or_none(v: str):
    v = plain_text(v or "")
    return int(v) if re.fullmatch(r"\d+", v) else None


# Yugipedia のリンク矢印 → 公式の番号（テンキー配列。ミラーの linkArrows "8","1","3" の並びから推定。未検証の前提）
_ARROW_NUM = {"Bottom-Left": 1, "Bottom-Center": 2, "Bottom": 2, "Bottom-Right": 3, "Middle-Left": 4, "Left": 4,
              "Middle-Right": 6, "Right": 6, "Top-Left": 7, "Top-Center": 8, "Top": 8, "Top-Right": 9}


def parse_card(page_text: str) -> dict | None:
    """カードのページ（{{CardTable2}}）から、ミラーと同じ形の英語の項目と、日本にあるかの手がかりを取る。

    返り値の mirror_like は tools/tcg_preview_watch.build_card_type などにそのまま渡せる形
    （type / properties / englishAttribute / englishProperty）。
    """
    p = template_params(page_text, "CardTable2")
    if p is None:
        return None
    card_type = plain_text(p.get("card_type", "")).lower()
    types = [t.strip() for t in plain_text(p.get("types", "")).split("/") if t.strip()]
    if card_type in ("spell", "trap"):
        kind = card_type
    elif types or p.get("attribute"):
        kind = "monster"
    else:
        kind = ""
    attr = plain_text(p.get("attribute", ""))
    prop = plain_text(p.get("property", ""))
    mirror_like = {
        "type": kind,
        "properties": [t for t in types if not _unknown(t)],
        "englishAttribute": "" if _unknown(attr) else attr.lower(),
        "englishProperty": None if (_unknown(prop) or prop.lower() == "normal") else prop.lower().replace("-", ""),
    }
    lore = parse_lore(p.get("text", ""), page_text)
    pend = parse_lore(p.get("pendulum_effect", ""), page_text)
    arrows = [_ARROW_NUM[a.strip()] for a in plain_text(p.get("link_arrows", "")).split(",") if a.strip() in _ARROW_NUM]
    atk, d = plain_text(p.get("atk", "")), plain_text(p.get("def", ""))
    en_sets = p.get("en_sets", "")
    return {
        "mirror_like": mirror_like,
        # 魔法・罠の種類が「Normal」と書かれている（空欄＝まだ分からない とは別）
        "property_is_normal": prop.lower() == "normal",
        "types_known": bool(types) and not any(_unknown(t) for t in types),
        "level": _int_or_none(p.get("level")),
        "rank": _int_or_none(p.get("rank")),
        "link_val": _int_or_none(p.get("link_rating")),
        # 「???」（未公開）も「?」（カード自体が ?）も、表示は「?」に揃える
        "atk": "?" if "?" in atk else atk,
        "def": "?" if "?" in d else d,
        "pendulum_scale": _int_or_none(p.get("pendulum_scale")),
        "link_arrows": ",".join(str(a) for a in arrows),
        "lore": lore,
        "pendulum_lore": pend,
        "unofficial_name": bool(re.search(r"\{\{\s*Unofficial name", page_text)),
        "database_id": plain_text(p.get("database_id", "")),
        "has_jp_sets": bool(plain_text(p.get("jp_sets", ""))),
        "en_set_codes": re.findall(r"([A-Z0-9]+-EN[0-9?]+)\s*;", en_sets),
    }


def exists_in_konami_db(card: dict) -> bool:
    """公式DB（日本・海外どちらか）に既にあるカードか。予告カードとしては扱わない。"""
    return bool(card.get("database_id")) or bool(card.get("has_jp_sets"))
