"""未発売カードのリンク画像（card_og_image.py・no_repost_logo.py）と、そのルート・ページのメタ情報のテスト。

外部（画像取得・DB・設定）はすべて偽物にする。
"""
import importlib
import io
import os
import random
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import card_og_image as g  # noqa: E402
import no_repost_logo as n  # noqa: E402
import replay_og_image  # noqa: E402

_JP_FONT = "C:/Windows/Fonts/NotoSansJP-VF.ttf"


def _card_image(with_logo: bool, size=(407, 593)) -> Image.Image:
    """カード風のダミー画像。with_logo なら 607 から切り出したテンプレートを右下付近に貼る。"""
    rnd = random.Random(1)
    im = Image.new("RGB", size, (30, 120, 110))
    d = ImageDraw.Draw(im)
    for _ in range(60):   # イラスト部分の色むら（赤っぽい塗りも混ぜ、赤みだけでは通らないことを見る）
        x, y = rnd.randint(40, 330), rnd.randint(100, 380)
        d.ellipse([x, y, x + rnd.randint(10, 50), y + rnd.randint(10, 50)],
                  fill=(rnd.randint(0, 255), rnd.randint(0, 120), rnd.randint(0, 255)))
    if with_logo:
        tpl = Image.open(n.TEMPLATE_PATH).convert("RGB")
        scale = size[0] / 407
        tpl = tpl.resize((round(tpl.width * scale), round(tpl.height * scale)))
        im.paste(tpl, (round(176 * scale), round(362 * scale)))
    return im


# ── ロゴ判定・ぼかし ──

def test_detect_logo_present_and_absent():
    on, s_on = n.detect(_card_image(True))
    off, s_off = n.detect(_card_image(False))
    assert on is True and s_on >= n.MATCH_THRESHOLD
    assert off is False and s_off < n.MATCH_THRESHOLD


def test_detect_is_scale_tolerant():
    # 画像の大きさが違っても（幅で正規化するので）同じように見つかる
    assert n.detect(_card_image(True, size=(480, 700)))[0] is True
    assert n.detect(_card_image(True, size=(445, 649)))[0] is True


def test_detect_accepts_bytes():
    buf = io.BytesIO()
    _card_image(True).save(buf, "PNG")
    assert n.detect(buf.getvalue())[0] is True


@pytest.mark.parametrize("ct,kind", [("通常魔法", "normal"), ("効果モンスター", "normal"), (None, "normal"),
                                      ("ペンデュラム・効果モンスター", "pendulum"), ("リンク・効果モンスター", "link")])
def test_art_kind(ct, kind):
    assert n.art_kind(ct) == kind


def test_blur_changes_only_art_area():
    src = _card_image(True)
    out = n.blur_art(src, "通常魔法")
    w, h = src.size
    l, t, r, b = n.ART_BOX["normal"]
    # 枠の外（カード名・効果欄）は完全に同じ
    assert out.crop((0, 0, w, int(h * t) - 2)).tobytes() == src.crop((0, 0, w, int(h * t) - 2)).tobytes()
    assert out.crop((0, int(h * b) + 2, w, h)).tobytes() == src.crop((0, int(h * b) + 2, w, h)).tobytes()
    # 枠の中は変わる（ロゴの位置もぼける）
    inner = (int(w * 0.3), int(h * 0.3), int(w * 0.8), int(h * 0.65))
    assert out.crop(inner).tobytes() != src.crop(inner).tobytes()
    assert out.size == src.size
    # ぼかしたあとは、もうロゴとして見つからない
    assert n.detect(out)[0] is False


# ── 折り返し ──

def _wrap(text, width=620, size=56):
    if not os.path.exists(_JP_FONT):
        pytest.skip("日本語フォントが無い環境")
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    return g.wrap_name(d, text, ImageFont.truetype(_JP_FONT, size), width)


def test_wrap_prefers_break_after_nakaguro():
    assert _wrap("キャッスル・オブ・ヴァンパイア") == ["キャッスル・オブ・", "ヴァンパイア"]


def test_wrap_short_name_single_line():
    assert _wrap("不死なる太陽神") == ["不死なる太陽神"]


def test_wrap_never_starts_line_with_small_kana_or_prolonged_mark():
    for name in ["あ" * 9 + "ょ" + "い" * 9, "ア" * 10 + "ー" + "イ" * 8, "ア" * 10 + "・" + "ー" * 3 + "イ" * 8]:
        for ln in _wrap(name)[1:]:
            assert ln[0] not in g._NO_LINE_START, (name, ln)


def test_wrap_keeps_text_intact():
    name = "ダダダダサウルス・ティラノ・ェックス・ロード・オブ・ナイトメア・ブレイカー"
    assert "".join(_wrap(name)) == name


# ── 文字・キー ──

def test_headline_text():
    assert g.HEADLINE == "遊戯王OCG 新カード"


def test_sub_line_variants():
    assert g.sub_line({"product_name": "P", "release_date": "2026-10-31"}) == "P｜10月31日発売"
    assert g.sub_line({"product_name": "P", "release_date": None}) == "P"
    assert g.sub_line({"product_name": "", "release_date": "2026-10-31"}) == "10月31日発売"
    assert g.sub_line({"product_name": "", "release_date": "bad"}) == ""


def _card(**over):
    c = {"id": 5, "name": "テスト", "product_name": "P", "release_date": "2026-10-31", "card_type": "通常魔法",
         "image_url": "https://img.test/5.webp", "image_key": "2026-10-15T00:00:00|https://img.test/5.webp",
         "watermark": None}
    c.update(over)
    return c


def test_version_key_changes_with_every_input():
    base = g.version_key(_card(), True)
    assert g.version_key(_card(), True) == base
    for over in ({"name": "別名"}, {"product_name": "Q"}, {"release_date": "2026-11-01"},
                 {"image_key": "2026-10-16T00:00:00|https://img.test/5.webp"}, {"card_type": "効果モンスター"}):
        assert g.version_key(_card(**over), True) != base, over
    assert g.version_key(_card(), False) != base   # ぼかし設定の切替


# ── 描画 ──

def test_render_png_is_1200x630_png():
    png = g.render_png(_card(), _card_image(False))
    im = Image.open(io.BytesIO(png))
    assert im.format == "PNG" and im.size == (1200, 630)
    assert im.getpixel((600, 625)) == (0xf3, 0xce, 0x46)   # 下端の金色帯


def test_render_png_without_product_and_date():
    png = g.render_png(_card(product_name="", release_date=None), _card_image(False))
    assert Image.open(io.BytesIO(png)).size == (1200, 630)


class _Tbl:
    def __init__(self, rows):
        self.rows, self.f = rows, []

    def select(self, *a, **k):
        return self

    def eq(self, c, v):
        self.f.append((c, v))
        return self

    def is_(self, c, v):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a):
        return self

    def execute(self):
        class R:
            pass
        r = R()
        r.data = [x for x in self.rows if all(x.get(c) == v for c, v in self.f)]
        return r


class _SB:
    def __init__(self, cards, imgs):
        self.t = {"unreleased_cards": cards, "official_card_images": imgs}

    def table(self, name):
        return _Tbl(self.t[name])


def test_fetch_public_card_only_public_with_image():
    imgs = [{"unreleased_card_id": 1, "public_url": "https://i/1.webp", "fetched_at": "T", "hidden": False}]

    def mk(**o):
        return {"id": 1, "name": "A", "status": "approved", "hidden": False, **o}
    assert g.fetch_public_card(_SB([mk()], imgs), 1)["image_url"] == "https://i/1.webp"
    assert g.fetch_public_card(_SB([mk(status="linked")], imgs), 1) is not None
    assert g.fetch_public_card(_SB([mk(status="pending")], imgs), 1) is None
    assert g.fetch_public_card(_SB([mk(hidden=True)], imgs), 1) is None
    assert g.fetch_public_card(_SB([mk()], []), 1) is None
    assert g.fetch_public_card(_SB([], imgs), 1) is None


# ── 生成の入口（ロゴ判定の保存・設定・キャッシュ）──

@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    monkeypatch.setattr(g, "_CACHE_DIR", str(tmp_path))
    replay_og_image._budget_log.clear()
    state = {"img": _card_image(True), "saved": [], "blurred": []}
    monkeypatch.setattr(g, "_download", lambda url: state["img"])
    import unreleased_image_store
    monkeypatch.setattr(unreleased_image_store, "_merge_extraction_raw",
                        lambda sb, cid, upd: state["saved"].append((cid, upd)) or True)
    real_blur = n.blur_art
    monkeypatch.setattr(n, "blur_art", lambda im, ct=None: state["blurred"].append(ct) or real_blur(im, ct))
    return state


def test_logo_judgement_saved_and_reused(pipeline):
    g.render_card(None, _card(), True)
    (cid, upd), = pipeline["saved"]
    wm = upd["watermark_no_repost"]
    assert cid == 5 and wm["detected"] is True and "score" in wm and "at" in wm
    assert pipeline["blurred"] == ["通常魔法"]
    # 保存済み（同じ画像）なら再計算も再保存もしない
    pipeline["saved"].clear()
    g.render_card(None, _card(watermark=wm), True)
    assert pipeline["saved"] == []
    # 画像が差し替わっていれば計算し直す
    g.render_card(None, _card(watermark=wm, image_key="new"), True)
    assert len(pipeline["saved"]) == 1


def test_blur_only_when_enabled_and_detected(pipeline):
    g.render_card(None, _card(), False)
    assert pipeline["blurred"] == []                 # 設定が無効ならぼかさない
    pipeline["img"] = _card_image(False)
    g.render_card(None, _card(), True)
    assert pipeline["blurred"] == []                 # ロゴが無ければぼかさない


def test_get_or_render_caches_by_key(pipeline, monkeypatch):
    calls = []
    real = g.render_card
    monkeypatch.setattr(g, "render_card", lambda sb, c, b: calls.append(1) or real(sb, c, b))
    a = g.get_or_render(None, _card(), True)
    b = g.get_or_render(None, _card(), True)
    assert a == b and len(calls) == 1
    g.get_or_render(None, _card(), False)            # 設定が変われば作り直す
    g.get_or_render(None, _card(name="別名"), True)   # 編集で作り直す
    assert len(calls) == 3


def test_get_or_render_busy_when_budget_exhausted(pipeline, monkeypatch):
    monkeypatch.setattr(replay_og_image, "consume_render_budget", lambda: False)
    with pytest.raises(replay_og_image.RenderBusy):
        g.get_or_render(None, _card(), True)


# ── ルートとページのメタ情報 ──

@pytest.fixture
def web(monkeypatch):
    monkeypatch.setenv("ENABLE_VISUAL_SOLO_PLAY", "1")
    import solitaire_routes
    importlib.reload(solitaire_routes)
    import app as app_module
    importlib.reload(app_module)
    app_module.app.config.update(TESTING=True)
    import card_display
    monkeypatch.setattr(solitaire_routes, "_get_supabase", lambda: object())
    env = {"card": _card(), "blur": True, "rec": {"id": 5, "name": "テスト"}, "png": b"PNGDATA", "err": None}

    def fetch(sb, cid):
        return env["card"] if env["card"] and cid == env["card"]["id"] else None

    def render(sb, card, blur):
        if env["err"]:
            raise env["err"]
        env["last_blur"] = blur
        return env["png"]

    monkeypatch.setattr(g, "fetch_public_card", fetch)
    monkeypatch.setattr(g, "get_or_render", render)
    monkeypatch.setattr(card_display, "is_setting_enabled", lambda k, d=True: env["blur"])
    monkeypatch.setattr(card_display, "get_unreleased_record", lambda name: env["rec"] if name == "テスト" else None)
    env["client"] = app_module.app.test_client()
    return env


def test_route_returns_card_png_with_kind_header(web):
    r = web["client"].get("/solitaire/og/card/5.png?v=abc")
    assert r.status_code == 200 and r.data == b"PNGDATA" and r.mimetype == "image/png"
    assert r.headers["X-OG-Kind"] == "card" and "86400" in r.headers["Cache-Control"]
    assert "3600" in web["client"].get("/solitaire/og/card/5.png").headers["Cache-Control"]


def test_route_passes_blur_setting(web):
    web["blur"] = False
    web["client"].get("/solitaire/og/card/5.png")
    assert web["last_blur"] is False


def test_route_falls_back_to_generic_when_not_public(web):
    web["card"] = None
    r = web["client"].get("/solitaire/og/card/5.png")
    assert r.status_code == 200 and "X-OG-Kind" not in r.headers and r.mimetype == "image/png"
    assert r.data != b"PNGDATA"


@pytest.mark.parametrize("err", [replay_og_image.RenderBusy("x"), RuntimeError("boom")])
def test_route_falls_back_to_generic_on_failure(web, err):
    web["err"] = err
    r = web["client"].get("/solitaire/og/card/5.png")
    assert r.status_code == 200 and "X-OG-Kind" not in r.headers


def test_page_meta_uses_card_image_for_focus(web):
    html = web["client"].get("/solitaire?newcards=1&focus=テスト").get_data(as_text=True)
    key = g.version_key(web["card"], True)
    assert f"/solitaire/og/card/5.png?v={key}" in html
    assert 'property="og:title" content="テストを一人回しで試す｜TCGYM"' in html
    assert 'name="twitter:card" content="summary_large_image"' in html
    assert html.count(f"/solitaire/og/card/5.png?v={key}") >= 2   # og:image と twitter:image


@pytest.mark.parametrize("url", ["/solitaire?newcards=1&focus=存在しない", "/solitaire?focus=テスト",
                                 "/solitaire?newcards=1", "/solitaire"])
def test_page_meta_generic_otherwise(web, url):
    html = web["client"].get(url).get_data(as_text=True)
    assert "/solitaire/og/card/" not in html
    assert "一人回しシミュレーター | TCGYM" in html


def test_page_meta_generic_when_card_not_public(web):
    web["card"] = None
    html = web["client"].get("/solitaire?newcards=1&focus=テスト").get_data(as_text=True)
    assert "/solitaire/og/card/" not in html


# ── レビュー指摘（低1・低2・低3）──

def test_saved_score_is_rejudged_with_current_threshold(pipeline, monkeypatch):
    # 保存済みの detected ではなく score を、現行の閾値で判定し直す（閾値だけ変えても再計算は要らない）
    wm = {"detected": True, "score": 0.7, "at": "T", "image_key": _card()["image_key"], "v": n.DETECT_VERSION}
    pipeline["img"] = _card_image(False)   # 画像自体にはロゴが無い。保存値が使われていることの確認
    g.render_card(None, _card(watermark=wm), True)
    assert pipeline["blurred"] == ["通常魔法"] and pipeline["saved"] == []
    monkeypatch.setattr(n, "MATCH_THRESHOLD", 0.8)
    pipeline["blurred"].clear()
    g.render_card(None, _card(watermark=wm), True)
    assert pipeline["blurred"] == [] and pipeline["saved"] == []   # 閾値を上げたので判定が変わる（再保存はしない）


def test_saved_judgement_with_old_detect_version_is_recomputed(pipeline):
    wm = {"detected": True, "score": 0.99, "at": "T", "image_key": _card()["image_key"], "v": "old"}
    pipeline["img"] = _card_image(False)
    g.render_card(None, _card(watermark=wm), True)
    (cid, upd), = pipeline["saved"]
    assert upd["watermark_no_repost"]["v"] == n.DETECT_VERSION and upd["watermark_no_repost"]["detected"] is False
    assert pipeline["blurred"] == []
    # 版の無い旧形式（score なし）も再計算
    pipeline["saved"].clear()
    g.render_card(None, _card(watermark={"detected": True, "image_key": _card()["image_key"]}), True)
    assert len(pipeline["saved"]) == 1


def test_version_key_reflects_threshold_and_detect_version(monkeypatch):
    base = g.version_key(_card(), True)
    monkeypatch.setattr(n, "MATCH_THRESHOLD", 0.9)
    assert g.version_key(_card(), True) != base
    monkeypatch.undo()
    monkeypatch.setattr(n, "DETECT_VERSION", "2")
    assert g.version_key(_card(), True) != base


def test_prepare_watermark_saves_judgement(pipeline, monkeypatch):
    monkeypatch.setattr(g, "fetch_public_card", lambda sb, cid: _card() if cid == 5 else None)
    assert g.prepare_watermark(None, 5) is True
    (cid, upd), = pipeline["saved"]
    assert cid == 5 and upd["watermark_no_repost"]["detected"] is True
    assert g.prepare_watermark(None, 99) is False   # 公開中でなければ何もしない
    assert len(pipeline["saved"]) == 1


def test_render_failure_is_remembered_briefly(pipeline, monkeypatch):
    g._failures.clear()
    calls = []

    def boom(sb, card, blur):
        calls.append(1)
        raise OSError("画像取得失敗")
    monkeypatch.setattr(g, "render_card", boom)
    with pytest.raises(OSError):
        g.get_or_render(None, _card(), True)
    # 同じ key の再描画は60秒ほど抑える（描画せず RenderBusy）
    with pytest.raises(replay_og_image.RenderBusy):
        g.get_or_render(None, _card(), True)
    assert len(calls) == 1
    # 別の key（編集された・設定が違う）は抑えない
    with pytest.raises(OSError):
        g.get_or_render(None, _card(name="別名"), True)
    assert len(calls) == 2
    # 期限が切れれば再び試す
    real_mono = g.time.monotonic
    monkeypatch.setattr(g.time, "monotonic", lambda: real_mono() + g.FAILURE_MEMO_SEC + 1)
    with pytest.raises(OSError):
        g.get_or_render(None, _card(), True)
    assert len(calls) == 3
    g._failures.clear()


def test_busy_is_not_remembered_as_failure(pipeline, monkeypatch):
    g._failures.clear()
    monkeypatch.setattr(replay_og_image, "consume_render_budget", lambda: False)
    with pytest.raises(replay_og_image.RenderBusy):
        g.get_or_render(None, _card(), True)
    assert g._failures == {}
