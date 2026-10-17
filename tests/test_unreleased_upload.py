"""画像アップロードからの取込と、公式版が届いたときの差し替え（unreleased_upload.py）を検証する。"""

import base64
import io
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

import unreleased_auto_approve as aa
import unreleased_upload as up
from fake_supabase import FakeSB, Query
from name_normalize import fuzzy_key


# ──────────────────────────────────────────────
# 偽DB: JSON の ->> と、upsert 時の id 採番に対応させる
# ──────────────────────────────────────────────

class _Query(Query):
    def eq(self, col, val):
        if "->>" in col:
            base, key = col.split("->>", 1)
            self.filters.append(lambda r: str((r.get(base) or {}).get(key)) == str(val))
            return self
        return super().eq(col, val)

    def execute(self):
        if self.op in ("upsert", "insert") and isinstance(self.payload, dict) and "id" not in self.payload \
                and self.name in ("unreleased_cards", "official_card_images"):
            self.payload = dict(self.payload, id=self.db.next_id())
        return super().execute()


class _SB(FakeSB):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._n = 1000

    def next_id(self):
        self._n += 1
        return self._n

    def table(self, name):
        return _Query(self, name)


def _png(w=40, h=30) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _data_url(data: bytes, mime="image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


SRC = "https://www.youtube.com/watch?v=abc&t=6448s"

_REAL_CROP = up._default_crop


@pytest.fixture(autouse=True)
def _no_real_crop(monkeypatch):
    """差し替え後の自動クロップは本物の Storage を使うので、既定では何もしない（本物を通すテストだけ戻す）。"""
    monkeypatch.setattr(up, "_default_crop", lambda *a: None)


# ──────────────────────────────────────────────
# 入力の検証
# ──────────────────────────────────────────────

def test_decode_upload_accepts_png():
    data = _png()
    got, mime = up.decode_upload(_data_url(data))
    assert got == data and mime == "image/png"


@pytest.mark.parametrize("value, msg", [
    ("", "形式が不正"),
    ("data:image/png,abc", "base64"),
    ("data:image/gif;base64,R0lGOD", "対応していない"),
    ("data:image/png;base64,@@@", "読み取れません"),
    ("data:image/png;base64," + base64.b64encode(b"not an image").decode(), "開けません"),
])
def test_decode_upload_rejects(value, msg):
    with pytest.raises(up.UploadError, match=msg):
        up.decode_upload(value)


def test_decode_upload_rejects_mime_mismatch():
    # 中身は PNG なのに JPEG と名乗る
    with pytest.raises(up.UploadError, match="一致しません"):
        up.decode_upload(_data_url(_png(), "image/jpeg"))


def test_decode_upload_rejects_too_large(monkeypatch):
    monkeypatch.setattr(up, "MAX_UPLOAD_BYTES", 10)
    with pytest.raises(up.UploadError, match="大きすぎます"):
        up.decode_upload(_data_url(_png()))


@pytest.mark.parametrize("url", ["", "ftp://example.com/a", "javascript:alert(1)", "https://localhost/x"])
def test_validate_source_url_rejects(url):
    with pytest.raises(up.UploadError):
        up.validate_source_url(url)


# ──────────────────────────────────────────────
# 取込
# ──────────────────────────────────────────────

def _row(name, **kw):
    row = {"name": name, "product_name": "IMMORTAL PHOENIX", "card_type": "効果モンスター",
           "effect_text": "効果", "atk": 1500, "def": "1300", "level": 4, "confidence": "medium",
           "extraction_raw": {"source": "x_tweet"}}
    row.update(kw)
    return row


@pytest.fixture
def no_context(monkeypatch):
    monkeypatch.setattr(aa, "load_active_names", lambda sb: [])
    monkeypatch.setattr(aa, "load_text_context", lambda sb: None)
    monkeypatch.setattr(aa, "compute_text_check", lambda *a, **k: {"status": "checked", "strong": 0})


def test_import_inserts_marks_and_stores_images(no_context):
    sb = _SB({"unreleased_cards": [{"id": 1, "name": "既存のカード", "product_name": "X",
                                    "extraction_raw": {"source": "x_tweet"}}]})
    data = _png()
    extract = MagicMock(return_value=[_row("六花のくろひめ"), _row("既存のカード"), _row("発売済みのカード")])
    store = MagicMock(return_value=(True, "取込成功"))

    result = up.import_uploaded_image(sb, data_url=_data_url(data), source_url=SRC, note="メモ",
                                      filename="list1.png", extract=extract, store_image=store,
                                      known_keys={fuzzy_key("発売済みのカード")})

    # 抽出には前処理済みの画像（PNG・幅高さ・識別子）と出典・メモが渡る
    image, src, note = extract.call_args.args
    assert image["url"].startswith("upload:") and image["media_type"] == "image/png"
    assert (image["width"], image["height"]) == (40, 30)
    assert src == SRC and note == "メモ"
    # 同名が登録済み・発売済みのカードは追加しない
    assert [c["name"] for c in result["inserted"]] == ["六花のくろひめ"]
    assert result["skipped"] == [{"name": "既存のカード", "reason": "同名のカードが登録済み"},
                                 {"name": "発売済みのカード", "reason": "発売済みのカード"}]
    card = next(r for r in sb.tables["unreleased_cards"] if r["name"] == "六花のくろひめ")
    assert card["source_url"] == SRC and card["source_domain"] == "www.youtube.com"
    assert card["extraction_raw"]["source"] == "image_upload"
    assert card["extraction_raw"]["upload"]["filename"] == "list1.png"
    # 自動承認の判定は「画像取込は人が確認する」で不適格
    verdict = card["extraction_raw"]["auto_approve_verdict"]
    assert verdict["eligible"] is False and "upload_source" in verdict["reasons"]
    # 画像はカードごとに、抽出に使った PNG と識別子で保存する（クロップ座標と向きが揃う）
    store.assert_called_once()
    s_args = store.call_args.args
    assert s_args[1] == card["id"] and s_args[2] == image["png"] and s_args[3] == "image/png"
    assert s_args[4] == image["url"] and s_args[5] == SRC and s_args[6] == "www.youtube.com"
    assert result["images"] == {card["id"]: "取込成功"}


def test_import_reattaches_image_when_previous_store_failed(no_context):
    prev = {"id": 5, "name": "六花氷月", "status": "pending",
            "extraction_raw": {"source": "image_upload", "crop_suggestion": {"source_image_url": "upload:old"}}}
    sb = _SB({"unreleased_cards": [prev], "official_card_images": []})
    row = _row("六花氷月", extraction_raw={"source": "x_tweet", "crop_suggestion": {"source_image_url": "upload:new"},
                                         "card_bbox": [1, 2, 3, 4]})
    store = MagicMock(return_value=(True, "取込成功"))

    result = up.import_uploaded_image(sb, data_url=_data_url(_png()), source_url=SRC,
                                      extract=lambda *a: [row], store_image=store, known_keys=set())

    assert result["inserted"] == []
    assert result["skipped"] == [{"name": "六花氷月", "reason": "登録済みのため画像だけ付け直し"}]
    assert store.call_args.args[1] == 5
    raw = sb.tables["unreleased_cards"][0]["extraction_raw"]
    assert raw["crop_suggestion"] == {"source_image_url": "upload:new"} and raw["card_bbox"] == [1, 2, 3, 4]
    assert len(sb.tables["unreleased_cards"]) == 1


def test_import_does_not_reattach_when_image_exists(no_context):
    prev = {"id": 5, "name": "六花氷月", "status": "pending", "extraction_raw": {"source": "image_upload"}}
    sb = _SB({"unreleased_cards": [prev],
              "official_card_images": [{"id": 1, "unreleased_card_id": 5, "deleted_at": None}]})
    store = MagicMock()
    result = up.import_uploaded_image(sb, data_url=_data_url(_png()), source_url=SRC,
                                      extract=lambda *a: [_row("六花氷月")], store_image=store, known_keys=set())
    assert result["skipped"] == [{"name": "六花氷月", "reason": "同名のカードが登録済み"}]
    store.assert_not_called()


def test_normalize_uploaded_image_applies_exif_rotation():
    import unreleased_extractor as ux
    im = Image.new("RGB", (40, 20), (200, 0, 0))
    exif = im.getexif()
    exif[0x0112] = 6  # 右に90度回して見る写真
    buf = io.BytesIO()
    im.save(buf, format="JPEG", exif=exif.tobytes())
    out = ux.normalize_uploaded_image(buf.getvalue(), "upload:x")
    assert (out["width"], out["height"]) == (20, 40)
    assert out["media_type"] == "image/png" and out["png"].startswith(b"\x89PNG")
    assert base64.b64decode(out["data"]) == out["png"]


def test_extract_cards_from_uploaded_image_passes_size_and_context(monkeypatch):
    import unreleased_extractor as ux
    seen = {}
    monkeypatch.setattr(ux, "_extract_cards", lambda text, images, src, from_x: seen.update(
        text=text, images=images, src=src, from_x=from_x) or [])
    image = ux.normalize_uploaded_image(_png(), "upload:k")
    ux.extract_cards_from_uploaded_image(image, SRC, "メモ")
    assert seen["from_x"] is True and seen["src"] == SRC
    assert seen["images"] == [{k: image[k] for k in ("url", "media_type", "data", "width", "height")}]
    assert "メモ" in seen["text"] and SRC in seen["text"]


def test_evaluate_marks_upload_source_ineligible():
    card = {"name": "a", "effect_text": "x", "extraction_raw": {"source": "image_upload"}}
    verdict = aa.evaluate(card, other_names=[], text_check={"status": "checked", "strong": 0})
    assert "upload_source" in verdict["reasons"]
    assert aa.reason_label("upload_source") != "upload_source"


# ──────────────────────────────────────────────
# 公式版が届いたときの差し替え
# ──────────────────────────────────────────────

def _upload_card(cid, name, status, **kw):
    card = {"id": cid, "name": name, "status": status, "product_name": "IMMORTAL PHOENIX",
            "source_url": SRC, "card_type": "効果モンスター", "effect_text": "動画の効果文",
            "atk": 1500, "def": "1300", "level": 4, "confidence": "medium",
            "extraction_raw": {"source": "image_upload", "upload": {"filename": "a.png"},
                               "original_image": {"storage_path": "unreleased/originals/1.png"}}}
    card.update(kw)
    return card


def _db(*cards, images=None):
    return _SB({
        "unreleased_cards": [dict(c) for c in cards],
        "official_card_images": images if images is not None else [
            {"id": 1, "unreleased_card_id": c["id"], "deleted_at": None, "storage_path": f"unreleased/{c['id']}.png",
             "public_url": f"https://cdn.example/{c['id']}.png"} for c in cards],
    })


OFFICIAL = "https://x.com/YuGiOh_OCG_INFO/status/1"


def _official_row(name, **kw):
    return _row(name, effect_text="公式の効果文", extraction_raw={
        "source": "x_tweet", "card_image_url": "https://pbs.twimg.com/a.jpg",
        "crop_suggestion": {"left": 0.1, "top": 0.1, "right": 0.5, "bottom": 0.9,
                            "source_image_url": "https://pbs.twimg.com/a.jpg"}}, **kw)


def _store_ok(sb, cid, row, page_url):
    # 保存処理が新しい画像の行と記録を書く（ingest_x_card_image の代わり）
    sb.table("official_card_images").insert({"unreleased_card_id": cid, "deleted_at": None,
                                             "storage_path": f"unreleased/{cid}.jpg",
                                             "public_url": f"https://cdn.example/unreleased/{cid}.jpg"}).execute()
    raw = sb.table("unreleased_cards").select("extraction_raw").eq("id", cid).execute().data[0]["extraction_raw"]
    raw = dict(raw, crop_state={"mode": "auto"}, original_image={"storage_path": f"unreleased/originals/{cid}.jpg"})
    sb.table("unreleased_cards").update({"extraction_raw": raw}).eq("id", cid).execute()
    return True, "取込成功"


def test_load_replaceable_picks_waiting_upload_rows():
    sb = _db(_upload_card(1, "待ち", "pending"),
             _upload_card(2, "却下", "rejected"),
             _upload_card(3, "済み", "approved", extraction_raw={"source": "image_upload",
                                                                 "replaced_by_official": {"at": "x"}}),
             {"id": 4, "name": "公式", "status": "pending", "extraction_raw": {"source": "x_tweet"}})
    q = up.load_replaceable(sb)
    assert q.keys() == {fuzzy_key("待ち")}


def test_pending_card_is_replaced_with_official_content(no_context):
    sb = _db(_upload_card(1, "六花のくろひめ", "pending"))
    q = up.load_replaceable(sb)
    notify = MagicMock()
    rows = [_official_row("六花のくろひめ"), _official_row("別のカード")]

    remaining = up.split_replaceable(sb, rows, q, OFFICIAL, store=_store_ok, notify=notify)

    assert [r["name"] for r in remaining] == ["別のカード"]
    card = sb.tables["unreleased_cards"][0]
    assert card["effect_text"] == "公式の効果文"
    assert card["source_url"] == OFFICIAL and card["source_domain"] == "x.com"
    raw = card["extraction_raw"]
    assert raw["source"] == "x_tweet"           # 以後は公式版として扱う（再度の差し替え対象外）
    assert raw["upload"] == {"filename": "a.png"}
    rep = raw["replaced_by_official"]
    assert rep["mode"] == "content_replaced" and rep["previous"]["effect_text"] == "動画の効果文"
    assert rep["previous_source_url"] == SRC
    # 保存処理が書いた記録（クロップ状態・退避画像）は残る
    assert raw["crop_state"] == {"mode": "auto"}
    assert raw["original_image"]["storage_path"].endswith("1.jpg")
    # 古い画像は論理削除、新しい画像だけが見える（URLは時刻つきで古い表示を避ける）
    imgs = sb.tables["official_card_images"]
    assert [i["deleted_at"] is None for i in imgs] == [False, True]
    assert "?t=" in imgs[1]["public_url"]
    assert q.keys() == set() and fuzzy_key("六花のくろひめ") in q.done
    assert up.admin_view(raw)["state"] == "replaced"
    notify.assert_called_once()


def test_approved_card_keeps_content_and_records_diff():
    sb = _db(_upload_card(1, "誘惑の鏡壁", "approved"))
    q = up.load_replaceable(sb)
    notify = MagicMock()

    up.split_replaceable(sb, [_official_row("誘惑の鏡壁")], q, OFFICIAL, store=_store_ok, notify=notify)

    card = sb.tables["unreleased_cards"][0]
    assert card["effect_text"] == "動画の効果文"   # 人が確認した内容は変えない
    raw = card["extraction_raw"]
    assert raw["source"] == "image_upload"
    rep = raw["replaced_by_official"]
    assert rep["mode"] == "image_only"
    assert rep["diff"] == {"effect_text": {"current": "動画の効果文", "official": "公式の効果文"}}
    assert up.admin_view(raw)["diff_fields"] == ["effect_text"]
    assert "要確認" in notify.call_args.args[0]
    # 2回目は差し替え待ちに無い
    assert up.load_replaceable(sb).keys() == set()


def test_store_failure_keeps_old_image_and_waiting():
    sb = _db(_upload_card(1, "六花氷月", "approved"))
    q = up.load_replaceable(sb)
    notify = MagicMock()

    remaining = up.split_replaceable(sb, [_official_row("六花氷月")], q, OFFICIAL,
                                     store=lambda *a: (False, "画像取得失敗"), notify=notify)

    assert remaining == []                         # 失敗しても同名の新しい行は作らない
    assert sb.tables["official_card_images"][0]["deleted_at"] is None   # 古い画像は一度も消さない
    raw = sb.tables["unreleased_cards"][0]["extraction_raw"]
    assert "replaced_by_official" not in raw and raw["replace_error"]["reason"] == "画像取得失敗"
    assert raw["original_image"] == {"storage_path": "unreleased/originals/1.png"}   # 記録は元のまま
    assert up.load_replaceable(sb).keys() == {fuzzy_key("六花氷月")}
    view = up.admin_view(raw)
    assert view == {"state": "waiting", "error": "画像取得失敗", "official_page": OFFICIAL}


def test_exception_before_store_keeps_old_image_and_record():
    sb = _db(_upload_card(1, "六花氷月", "approved"))
    q = up.load_replaceable(sb)

    def boom(*a):
        raise RuntimeError("一時的な失敗")

    up.split_replaceable(sb, [_official_row("六花氷月")], q, OFFICIAL, store=boom, notify=MagicMock())
    assert [i["deleted_at"] for i in sb.tables["official_card_images"]] == [None]
    raw = sb.tables["unreleased_cards"][0]["extraction_raw"]
    assert raw["original_image"] == {"storage_path": "unreleased/originals/1.png"}
    assert "replaced_by_official" not in raw


def test_exception_after_store_keeps_new_image_visible(monkeypatch):
    sb = _db(_upload_card(1, "六花氷月", "approved"))
    q = up.load_replaceable(sb)

    def broken_diff(*a):
        raise RuntimeError("記録の更新失敗")

    monkeypatch.setattr(up, "field_diff", broken_diff)
    notify = MagicMock()

    up.split_replaceable(sb, [_official_row("六花氷月")], q, OFFICIAL, store=_store_ok, notify=notify)

    imgs = sb.tables["official_card_images"]
    assert any(i["deleted_at"] is None for i in imgs)   # どれかの画像は必ず見える
    assert "保存済み" in notify.call_args.args[0]


def test_store_without_raw_updates_does_not_keep_capture_records():
    # 公式サイト経由の保存が記録を書かない場合でも、キャプチャの元画像・クロップ状態を引き継がない
    card = _upload_card(1, "誘惑の鏡壁", "approved")
    card["extraction_raw"]["crop_state"] = {"mode": "auto", "box": {"left": 0.2}}
    sb = _db(card)
    q = up.load_replaceable(sb)

    def store_rows_only(sb, cid, row, page_url):
        sb.table("official_card_images").insert({"unreleased_card_id": cid, "deleted_at": None,
                                                 "storage_path": f"unreleased/{cid}.jpg"}).execute()
        return True, "ok"

    up.split_replaceable(sb, [_official_row("誘惑の鏡壁")], q, OFFICIAL, store=store_rows_only, notify=MagicMock())
    raw = sb.tables["unreleased_cards"][0]["extraction_raw"]
    assert "original_image" not in raw and "crop_state" not in raw
    assert raw["crop_suggestion"]["source_image_url"] == "https://pbs.twimg.com/a.jpg"


def test_lacks_card_body_row_is_not_used_for_replacement():
    sb = _db(_upload_card(1, "六花のくろひめ", "pending"))
    q = up.load_replaceable(sb)
    art = _official_row("六花のくろひめ")
    art.update(effect_text="", atk=None, level=None, card_type="")
    art["def"] = ""
    store = MagicMock()
    remaining = up.split_replaceable(sb, [art], q, OFFICIAL, store=store, notify=MagicMock())
    assert remaining == [] and store.call_count == 0
    assert q.keys() == {fuzzy_key("六花のくろひめ")}   # 本体のある投稿を待つ


def test_pending_replacement_records_auto_approve_verdict(no_context):
    sb = _db(_upload_card(1, "華麗なる三面鏡", "pending", release_date=None))
    q = up.load_replaceable(sb)
    row = _official_row("華麗なる三面鏡", release_date="2026-10-31")
    up.split_replaceable(sb, [row], q, OFFICIAL, store=_store_ok, notify=MagicMock())
    card = sb.tables["unreleased_cards"][0]
    assert "auto_approve_verdict" in card["extraction_raw"]
    assert "upload_source" in card["extraction_raw"]["auto_approve_verdict"]["reasons"]   # 画像取込由来は残る
    assert card["release_date"] == "2026-10-31"     # 未確定だった発売日は公式版の値を採る


def test_same_name_twice_in_one_run_is_discarded_after_replacement(no_context):
    sb = _db(_upload_card(1, "華麗なる三面鏡", "pending"))
    q = up.load_replaceable(sb)
    first = up.split_replaceable(sb, [_official_row("華麗なる三面鏡")], q, OFFICIAL, store=_store_ok, notify=MagicMock())
    second = up.split_replaceable(sb, [_official_row("華麗なる三面鏡")], q, OFFICIAL, store=_store_ok, notify=MagicMock())
    assert first == [] and second == []
    assert len(sb.tables["unreleased_cards"]) == 1


def test_admin_view_none_for_normal_rows():
    assert up.admin_view({"source": "x_tweet"}) is None
    assert up.admin_view({}) is None
    assert up.admin_view({"source": "image_upload"}) == {"state": "waiting", "error": None, "official_page": None}


def test_admin_view_diff_disappears_when_human_fixes_it():
    raw = {"source": "image_upload", "replaced_by_official": {
        "mode": "image_only", "diff": {"effect_text": {"current": "動画", "official": "公式"}}}}
    assert up.admin_view(raw, {"effect_text": "動画"})["diff_fields"] == ["effect_text"]
    assert up.admin_view(raw, {"effect_text": "公式"})["diff_fields"] == []


# ──────────────────────────────────────────────
# 画像の保存（unreleased_image_store）
# ──────────────────────────────────────────────

class _Storage:
    def __init__(self):
        self.files, self.removed = {}, []

    def from_(self, bucket):
        return self

    def upload(self, path, file, file_options=None):
        self.files[path] = file

    def get_public_url(self, path):
        return f"https://cdn.example/{path}"

    def download(self, path):
        return self.files.get(path)

    def remove(self, paths):
        self.removed.extend(paths)


def test_ingest_uploaded_card_image_stores_and_auto_crops(monkeypatch):
    import unreleased_image_store as store
    sb = _SB({"unreleased_cards": [{"id": 3, "extraction_raw": {"crop_suggestion": {
        "left": 0.1, "top": 0.1, "right": 0.5, "bottom": 0.9, "source_image_url": "upload:k", "method": "contour"}}}],
        "official_card_images": []})
    sb.storage = _Storage()
    crops = []

    def fake_crop(sb, cid, *box, **k):
        crops.append((cid, box))
        return True, "ok"

    monkeypatch.setattr(store, "crop_and_save_image", fake_crop)

    ok, reason = store.ingest_uploaded_card_image(sb, 3, _png(), "image/png", "upload:k", SRC, "www.youtube.com")

    assert ok, reason
    img = sb.tables["official_card_images"][0]
    assert img["source_image_url"] == "upload:k" and img["source_domain"] == "www.youtube.com"
    assert img["source_page_url"] == SRC and "unreleased/3.png" in sb.storage.files
    assert "unreleased/originals/3.png" in sb.storage.files
    assert crops == [(3, (0.1, 0.1, 0.5, 0.9))]
    # 2回目は取込済みとして何もしない
    assert store.ingest_uploaded_card_image(sb, 3, _png(), "image/png", "upload:k", SRC, "x") == (True, "取込済み")


def test_store_replacement_image_official_site_does_not_crop(monkeypatch):
    import fetch_guard
    import unreleased_image_store as store
    sb = _SB({"unreleased_cards": [{"id": 3, "extraction_raw": {"crop_suggestion": {
        "left": 0.1, "top": 0.1, "right": 0.5, "bottom": 0.9, "source_image_url": "https://www.yu-gi-oh.jp/a.png"}}}],
        "official_card_images": [{"id": 1, "unreleased_card_id": 3, "deleted_at": None}]})
    sb.storage = _Storage()
    monkeypatch.setattr(fetch_guard, "is_whitelisted", lambda url: True)
    monkeypatch.setattr(fetch_guard, "fetch_whitelisted", lambda url, **k: SimpleNamespace(
        status_code=200, headers={"Content-Type": "image/png"}, content=_png()))
    crop = MagicMock()
    monkeypatch.setattr(store, "crop_and_save_image", crop)

    ok, _ = store.store_replacement_image(sb, 3, "https://www.yu-gi-oh.jp/a.png", "https://www.yu-gi-oh.jp/news/x/",
                                          official_site=True)

    assert ok
    assert len(sb.tables["official_card_images"]) == 2   # 既存の画像があっても保存する（古い方の削除は呼び出し側）
    crop.assert_not_called()


def test_x_replacement_crops_the_new_image_only(monkeypatch):
    """本物の保存・切り抜き処理で、PNG のキャプチャを JPEG の公式画像に差し替える（再レビューの高-A）。"""
    import requests
    import unreleased_image_store as store

    def jpeg(w, h):
        buf = io.BytesIO()
        Image.new("RGB", (w, h), (1, 2, 3)).save(buf, format="JPEG")
        return buf.getvalue()

    monkeypatch.setattr(up, "_default_crop", _REAL_CROP)
    official = "https://pbs.twimg.com/a.jpg"
    card = _upload_card(1, "誘惑の鏡壁", "approved")
    card["extraction_raw"]["original_image"] = {"storage_path": "unreleased/originals/1.png", "mime": "image/png"}
    sb = _SB({"unreleased_cards": [card],
              "official_card_images": [{"id": 1, "unreleased_card_id": 1, "deleted_at": None,
                                        "storage_path": "unreleased/1.png", "public_url": "https://cdn.example/unreleased/1.png",
                                        "source_image_url": "upload:old"}]})
    sb.storage = _Storage()
    sb.storage.files = {"unreleased/1.png": _png(), "unreleased/originals/1.png": _png()}
    monkeypatch.setattr(requests, "get", lambda url, timeout=30: SimpleNamespace(
        status_code=200, headers={"Content-Type": "image/jpeg"}, content=jpeg(200, 100),
        raise_for_status=lambda: None))
    row = _official_row("誘惑の鏡壁")
    row["extraction_raw"]["crop_suggestion"] = {"left": 0.0, "top": 0.0, "right": 0.5, "bottom": 1.0,
                                                "source_image_url": official, "method": "contour"}

    up.split_replaceable(sb, [row], up.load_replaceable(sb), OFFICIAL, notify=MagicMock())

    visible = [i for i in sb.tables["official_card_images"] if i.get("deleted_at") is None]
    assert len(visible) == 1
    new = visible[0]
    assert new["storage_path"] == "unreleased/1.jpg"
    assert new["public_url"].split("?")[0].endswith("unreleased/1.jpg")       # 公開URLは新しい保存先を指す
    with Image.open(io.BytesIO(sb.storage.files["unreleased/1.jpg"])) as im:
        assert im.size == (100, 100)                                          # 新しい画像が切り抜かれている
    # 使われなくなったキャプチャの画像・元画像は消し、新しい方は残す
    assert "unreleased/1.png" in sb.storage.removed and "unreleased/originals/1.png" in sb.storage.removed
    assert "unreleased/1.jpg" not in sb.storage.removed
    raw = sb.tables["unreleased_cards"][0]["extraction_raw"]
    assert raw["original_image"]["storage_path"] == "unreleased/originals/1.jpg"
    assert raw["crop_state"]["mode"] == "auto"


# ──────────────────────────────────────────────
# 公式サイト巡回への組み込み
# ──────────────────────────────────────────────

def test_watch_unreleased_replaces_instead_of_inserting(monkeypatch, no_context):
    import unreleased_approve
    import watch_unreleased
    queue = up.ReplaceQueue({fuzzy_key("誘惑の鏡壁"): {"id": 7, "name": "誘惑の鏡壁"}})
    monkeypatch.setattr(up, "load_replaceable", lambda sb: queue)
    replaced = []

    def fake_replace(sb, card, row, **k):
        replaced.append(card["id"])
        return True, "x"

    monkeypatch.setattr(up, "replace_with_official", fake_replace)
    monkeypatch.setattr(unreleased_approve, "maybe_auto_approve", lambda sb, rows: {})
    sb = _SB({"unreleased_cards": []})
    rows = [_row("誘惑の鏡壁", source_url="https://yu-gi-oh.jp/news/a/"),
            _row("新しいカード", source_url="https://yu-gi-oh.jp/news/a/")]

    n = watch_unreleased._upsert_cards(sb, rows)

    assert replaced == [7]
    assert n == 1 and [r["name"] for r in sb.tables["unreleased_cards"]] == ["新しいカード"]


# ──────────────────────────────────────────────
# X監視への組み込み
# ──────────────────────────────────────────────

def test_watch_x_replaces_instead_of_inserting(monkeypatch):
    import watch_x_unreleased as watcher
    monkeypatch.setattr(watcher, "_get_known_fuzzy_keys", lambda: set())
    ext = ModuleType("unreleased_extractor")
    body = _official_row("六花のくろひめ")
    art = dict(_official_row("六花のくろひめ"), effect_text="", atk=None, level=None, card_type="")
    ext.extract_cards_from_tweet = MagicMock(side_effect=[[art], [body]])
    monkeypatch.setitem(sys.modules, "unreleased_extractor", ext)
    store_mod = ModuleType("unreleased_image_store")
    store_mod.ingest_x_card_image = MagicMock(return_value=(True, "ok"))
    monkeypatch.setitem(sys.modules, "unreleased_image_store", store_mod)
    replaced = []
    monkeypatch.setattr(up, "replace_with_official",
                        lambda sb, card, row, **k: (replaced.append((card["id"], row["effect_text"])), (True, "x"))[1])
    insert = MagicMock()
    monkeypatch.setattr(watcher, "_upsert_cards", insert)

    q = up.ReplaceQueue({fuzzy_key("六花のくろひめ"): {"id": 7, "name": "六花のくろひめ"}})
    tweets = [{"id": "1", "text": "◤六花のくろひめ◢", "attachments": {"media_keys": ["m"]}},
              {"id": "2", "text": "◤六花のくろひめ◢", "attachments": {"media_keys": ["m"]}}]
    n = watcher.process_tweets(tweets, {"m": "https://example.test/x.jpg"}, MagicMock(), set(), q)

    # イラストのみの投稿では差し替えず、本体のある投稿で1回だけ差し替える。新しい行は作らない
    assert n == 0
    assert replaced == [(7, "公式の効果文")]
    insert.assert_not_called()


# ──────────────────────────────────────────────
# 管理API
# ──────────────────────────────────────────────

def _admin_client(monkeypatch, sb):
    import admin_unreleased as admin_module
    import app as app_module
    monkeypatch.setattr(admin_module, "_supabase", sb, raising=False)
    monkeypatch.setattr(admin_module, "_ADMIN_KEY", "test-key", raising=False)
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


def test_import_image_api_requires_admin_key(monkeypatch):
    client = _admin_client(monkeypatch, _SB())
    resp = client.post("/api/admin/unreleased/import-image", json={})
    assert resp.status_code == 401


def test_import_image_api_rejects_bad_input(monkeypatch):
    client = _admin_client(monkeypatch, _SB())
    resp = client.post("/api/admin/unreleased/import-image", headers={"X-Admin-Key": "test-key"},
                       json={"image": _data_url(_png()), "source_url": ""})
    assert resp.status_code == 400 and "出典URL" in resp.get_json()["error"]


def test_import_image_api_success(monkeypatch):
    called = {}

    def fake_import(sb, **kw):
        called.update(kw)
        return {"inserted": [{"id": 9, "name": "六花のくろひめ"}], "skipped": ["六花氷月"],
                "images": {9: "取込成功"}, "extracted": 2}

    monkeypatch.setattr(up, "import_uploaded_image", fake_import)
    client = _admin_client(monkeypatch, _SB())
    resp = client.post("/api/admin/unreleased/import-image", headers={"X-Admin-Key": "test-key"},
                       json={"image": "data:image/png;base64,AA", "source_url": SRC, "note": "n", "filename": "f.png"})
    body = resp.get_json()
    assert resp.status_code == 201
    assert body["inserted"] == 1 and body["cards"] == [{"id": 9, "name": "六花のくろひめ", "image": "取込成功"}]
    assert body["skipped"] == ["六花氷月"]
    assert called == {"data_url": "data:image/png;base64,AA", "source_url": SRC, "note": "n", "filename": "f.png"}
