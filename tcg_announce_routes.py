"""海外先行の予告カードのまとめページと OG 画像の受け口（Blueprint）。

  - GET /tcg-preview/<slug>                  → 弾ごとのまとめページ（サーバー側で中身まで描く。検索とリンクの見出しで人を呼ぶため）
  - GET /tcg-preview/og/<slug>.png           → 弾の OG 画像（まとめの投稿のリンクプレビュー）
  - GET /tcg-preview/og/<slug>/<anchor>.png  → カードの OG 画像（カードごとの投稿のリンクプレビュー）
  - カードごとの投稿のリンクは /tcg-preview/<slug>?c=<anchor>。ページは同じで、OG 画像だけそのカードのものにする
    （X はリンク先の og:image を使うので、カードごとに見出し画像を変えるには URL を分ける必要がある。#… は届かない）

中身は tcg_announce_view（Supabase の tcg_announce_*）から読む。
"""
import logging

from flask import Blueprint, Response, abort, render_template, request

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
        anchor=view.anchor, stats_line=tcg_announce_og.stats_line, format_release=tcg_announce_og.format_release,
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


@tcg_announce_bp.route("/tcg-preview/og/<slug>.png")
def set_og(slug: str):
    entry, err = _entry_or_503(slug)
    if err:
        return err
    if entry is None:
        abort(404)
    return _png(tcg_announce_og.set_png(entry["set"], entry["cards"]))


@tcg_announce_bp.route("/tcg-preview/og/<slug>/<anchor>.png")
def card_og(slug: str, anchor: str):
    entry, err = _entry_or_503(slug)
    if err:
        return err
    card = _find_card(entry, anchor) if entry else None
    if card is None:
        abort(404)
    others = [c["name_en"] for c in entry["cards"] if c is not card]
    return _png(tcg_announce_og.card_png(entry["set"], card, others))
