"""海外先行の予告カードのまとめページと OG 画像の受け口（Blueprint）。

  - GET /tcg-preview/<slug>                  → 弾ごとのまとめページ（サーバー側で中身まで描く。検索とリンクの見出しで人を呼ぶため）
  - GET /tcg-preview/og/<slug>.png           → 弾の OG 画像（まとめの投稿のリンクプレビュー）
  - GET /tcg-preview/og/<slug>/<anchor>.png  → カードの OG 画像（カードごとの投稿のリンクプレビュー）
  - カードごとの投稿のリンクは /tcg-preview/<slug>?c=<anchor>。ページは同じで、OG 画像だけそのカードのものにする
    （X はリンク先の og:image を使うので、カードごとに見出し画像を変えるには URL を分ける必要がある。#… は届かない）

中身は tcg_announce_view（Supabase の tcg_announce_*）から読む。
"""
import logging
import threading
import time
from collections import OrderedDict

from flask import Blueprint, Response, abort, render_template, request

import card_display
import card_og_image
import tcg_announce_og
import tcg_announce_view as view

logger = logging.getLogger(__name__)

tcg_announce_bp = Blueprint("tcg_announce", __name__)


def _find_card(entry: dict, anchor: str) -> dict | None:
    return next((c for c in entry["cards"] if view.anchor(c) == anchor), None)


@tcg_announce_bp.route("/tcg-preview/<slug>")
def set_page(slug: str):
    try:
        entry = view.get_set(slug)
    except Exception:
        logger.exception("予告カードのまとめの取得失敗")
        return render_template("error.html", title="一時的に表示できません",
                               message="海外先行カードを取得できませんでした。少し時間をおいて開き直してください。"), 503
    if entry is None:
        abort(404)
    focus = request.args.get("c", "")
    focus_card = _find_card(entry, focus) if focus else None
    s = entry["set"]
    og = f"/tcg-preview/og/{slug}" + (f"/{focus}" if focus_card else "") + ".png"
    sources = sorted({_domain(c.get("lore_source_url")) for c in entry["cards"] if c.get("lore_source_url")})
    return render_template(
        "tcg_announce.html", s=s, cards=entry["cards"], focus_card=focus_card, og_path=og,
        anchor=view.anchor, stats_line=tcg_announce_og.stats_line, format_release=tcg_announce_og.format_release_ja,
        summary_of=tcg_announce_og.summary_of, headline=tcg_announce_og.headline,
        known=len(entry["cards"]), sources=[x for x in sources if x],
        updated=max((c.get("updated_at") or "" for c in entry["cards"]), default="")[:10].replace("-", "/"),
    )


def _domain(url: str | None) -> str:
    if not url:
        return ""
    host = url.split("//", 1)[-1].split("/", 1)[0]
    return host.removeprefix("www.")


def _png(data: bytes) -> Response:
    r = Response(data, mimetype="image/png")
    r.headers["Cache-Control"] = "public, max-age=600"
    return r


def _entry_or_503(slug: str):
    """(entry, None) か、取得失敗なら (None, 503 の応答)。X がプレビューを取りに来たときの障害は 503 で返す。"""
    try:
        return view.get_set(slug), None
    except Exception:
        logger.exception("予告カードの OG 画像の材料の取得失敗")
        return None, ("", 503)


# ── OG 画像の材料（商品写真・ゆかりのカードの絵）。取れたものはしばらく持ち、失敗した URL はしばらく取り直さない ──
_IMG_MAX = 32
_FAIL_TTL = 600
_img_cache: "OrderedDict[str, object]" = OrderedDict()
_img_failed: dict[str, float] = {}
_img_lock = threading.Lock()


def _image(url: str | None):
    """画像を PIL で返す。取れなければ None（その部分を外して描く）。"""
    if not url:
        return None
    with _img_lock:
        if url in _img_cache:
            _img_cache.move_to_end(url)
            return _img_cache[url]
        if time.monotonic() - _img_failed.get(url, -1e9) < _FAIL_TTL:
            return None
    try:
        img = card_og_image._download(url)
    except Exception as e:
        logger.warning(f"OG 画像の材料を取れません（外して描きます）: {url[:80]}: {e}")
        with _img_lock:
            _img_failed[url] = time.monotonic()
        return None
    with _img_lock:
        _img_cache[url] = img
        while len(_img_cache) > _IMG_MAX:
            _img_cache.popitem(last=False)
    return img


def _related_images(names: list[str], limit: int) -> list[tuple[str, object, str]]:
    """ゆかりのカード（日本語の正式名）の公式画像を (名前, 画像, URL) で最大 limit 枚。画像の無いカードは飛ばす。"""
    if not names:
        return []
    try:
        disp = card_display.resolve_card_displays(names)
    except Exception:
        logger.exception("ゆかりのカードの画像の解決に失敗（外して描きます）")
        return []
    out = []
    for n in names:
        d = disp.get(n) or {}
        if d.get("kind") != "image" or d.get("source") == "official_sample":
            continue  # 発売済みカードの画像だけ（未発売の公式 SAMPLE は使わない）
        img = _image(d.get("url"))
        if img is not None:
            out.append((n, img, d["url"]))
        if len(out) >= limit:
            break
    return out


def set_related_names(cards: list[dict]) -> list[str]:
    """弾の画像に並べる既存カード: ゆかりのカード（カードの並び順）→ テーマの代表カード。重ならないように。"""
    names: list[str] = []
    for c in cards:
        for r in c.get("related_ja") or []:
            if r not in names:
                names.append(r)
    for c in cards:
        t = c.get("theme_card_ja") if c.get("theme_kind") in ("member", "support", "new_theme") else ""
        if t and t not in names:
            names.append(t)
    return names


def set_arts(s: dict, cards: list[dict]) -> tuple[list, list, list[str]]:
    """弾の画像に並べる新カードの絵 (arts) と、（ゆかりのカードの絵, 新カードの絵）の組 (pairs)、鍵にする URL。
    並びはカードごとの投稿と同じ（表紙のカード → 既存カードの絵が出るカード → 一覧の順）。取れない絵は飛ばす。"""
    import tcg_announce_post
    order = sorted(cards, key=lambda c: tcg_announce_post.card_order_key(c, s.get("cover_cards") or []))
    arts, pairs, keys, used = [], [], [], set()
    for c in order:
        art = _image(c.get("art_url"))
        if art is None:
            continue
        if len(arts) < 4:
            arts.append(art)
            keys.append(c["art_url"])
        rel = [r for r in c.get("related_ja") or [] if r not in used][:1]
        old = _related_images(rel, 1) if rel and len(pairs) < 2 else []
        if old:
            used.add(old[0][0])
            pairs.append((old[0][1], art))
            keys += [old[0][2], c["art_url"]]
    return arts, pairs, keys


@tcg_announce_bp.route("/tcg-preview/og/<slug>.png")
def set_og(slug: str):
    entry, err = _entry_or_503(slug)
    if err:
        return err
    if entry is None:
        abort(404)
    s = entry["set"]
    box = _image(s.get("box_image_url"))
    rel = _related_images(set_related_names(entry["cards"]), 4)
    arts, pairs, art_keys = set_arts(s, entry["cards"])
    return _png(tcg_announce_og.set_png(s, entry["cards"], box=box, box_key=s.get("box_image_url") or "" if box else "",
                                        related=[(n, i) for n, i, _ in rel], related_keys=[u for _, _, u in rel],
                                        arts=arts, pairs=pairs, art_keys=art_keys))


@tcg_announce_bp.route("/tcg-preview/og/<slug>/<anchor>.png")
def card_og(slug: str, anchor: str):
    entry, err = _entry_or_503(slug)
    if err:
        return err
    card = _find_card(entry, anchor) if entry else None
    if card is None:
        abort(404)
    others = [c["name_en"] for c in entry["cards"] if c is not card]
    s = entry["set"]
    box = _image(s.get("box_image_url"))
    pic = tcg_announce_og.picture_card(card)  # ゆかりのカード → テーマの代表カード
    rel = _related_images([pic] if pic else [], 1)
    return _png(tcg_announce_og.card_png(s, card, others, box=box, box_key=s.get("box_image_url") or "" if box else "",
                                         related=(rel[0][0], rel[0][1]) if rel else None,
                                         related_key=rel[0][2] if rel else "", art=_image(card.get("art_url"))))
