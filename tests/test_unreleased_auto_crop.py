"""取り込み時の自動クロップ・元画像の退避・元画像への復元（Supabase/HTTPはモック）。"""
import io
import logging
from types import SimpleNamespace

import pytest
from flask import Flask
from PIL import Image

import admin_unreleased as admin
import unreleased_image_store as store

SRC = 'https://pbs.twimg.com/media/test.png'


def _png(w=100, h=100):
    img = Image.new('RGB', (w, h), 'red')
    img.paste('blue', (0, 0, w // 2, h))
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


class FakeSB:
    """unreleased_cards / official_card_images / storage の最小モック。"""

    def __init__(self, raw=None, with_image_row=False, download_data=None, upload_fail_paths=(), public_url='https://s.test/unreleased/1.png'):
        self.card_id = 1  # このモックが持つカードID。eq の条件が違えば別行扱い（読めない・書けない）
        self.removed = []
        self.raw = dict(raw or {})
        self.images = []  # official_card_images 行
        if with_image_row:
            self.images.append({'id': 1, 'unreleased_card_id': 1, 'storage_path': 'unreleased/1.png',
                                'public_url': public_url, 'source_image_url': SRC})
        self.uploads = []  # (path, bytes)
        self.thumb_uploads = []  # (path, bytes, file_options)
        self.storage_files = {}
        self.downloads = []
        self.download_data = download_data
        self.upload_fail_paths = upload_fail_paths
        self.storage = SimpleNamespace(from_=lambda name: self)
        self.raw_writes = 0

    # storage バケット API
    def upload(self, path, file, file_options=None):
        if path in self.upload_fail_paths:
            raise RuntimeError('upload failed')
        self.storage_files[path] = file
        if path.startswith('unreleased/thumbs/'):
            # 縮小版は本体の書き込み順を見るテストと分けて記録する
            self.thumb_uploads.append((path, file, dict(file_options or {})))
            return
        self.uploads.append((path, file))

    def download(self, path):
        self.downloads.append(path)
        if self.download_data is not None:
            return self.download_data
        if path in self.storage_files:
            return self.storage_files[path]
        raise RuntimeError('not found')

    def remove(self, paths):
        self.removed.extend(paths)

    def get_public_url(self, path):
        return f'https://s.test/{path}'

    def table(self, name):
        return _Q(self, name)


class _Q:
    def __init__(self, sb, name):
        self.sb, self.name, self.op, self.payload = sb, name, 'select', None
        self.filters = []

    def select(self, *a): return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def is_(self, *a): return self
    def in_(self, *a): return self

    def _matches(self):
        key = 'id' if self.name == 'unreleased_cards' else 'unreleased_card_id'
        return all(v == self.sb.card_id for c, v in self.filters if c == key)

    def insert(self, payload):
        self.op, self.payload = 'insert', payload
        return self

    def update(self, payload):
        self.op, self.payload = 'update', payload
        return self

    def execute(self):
        sb = self.sb
        if not self._matches():
            return SimpleNamespace(data=[])
        if self.name == 'unreleased_cards':
            if self.op == 'update':
                sb.raw = dict(self.payload['extraction_raw'])
                sb.raw_writes += 1
                return SimpleNamespace(data=[])
            return SimpleNamespace(data=[{'extraction_raw': dict(sb.raw)}])
        if self.name == 'official_card_images':
            if self.op == 'insert':
                sb.images.append({'id': 1, **self.payload})
                return SimpleNamespace(data=[])
            if self.op == 'update':
                for r in sb.images:
                    r.update(self.payload)
                return SimpleNamespace(data=[])
            return SimpleNamespace(data=[dict(r) for r in sb.images])
        raise AssertionError(self.name)


def _resp(data, mime='image/png'):
    return SimpleNamespace(content=data, headers={'Content-Type': mime}, raise_for_status=lambda: None)


def _suggestion(**over):
    s = {'left': .1, 'top': .1, 'right': .6, 'bottom': .9, 'source_image_url': SRC}
    s.update(over)
    return s


@pytest.fixture
def http(monkeypatch):
    data = _png()
    monkeypatch.setattr(store.requests, 'get', lambda *a, **kw: _resp(data))
    return data


def _size_of(upload_bytes):
    return Image.open(io.BytesIO(upload_bytes)).size


# ── 自動クロップ ─────────────────────────────────────

def test_auto_crop_applied_and_state_recorded(http):
    sb = FakeSB(raw={'crop_suggestion': _suggestion(), 'keep': 'x'})
    ok, _ = store.ingest_x_card_image(sb, 1, SRC, 'https://x.com/t/1')
    assert ok
    paths = [p for p, _ in sb.uploads]
    assert paths == ['unreleased/1.png', 'unreleased/originals/1.png', 'unreleased/1.png']
    assert _size_of(sb.uploads[0][1]) == (100, 100)
    assert sb.uploads[1][1] == http  # 退避はバイトそのまま
    assert _size_of(sb.uploads[2][1]) == (50, 80)
    raw = sb.raw
    assert raw['crop_state']['mode'] == 'auto'
    assert raw['crop_state']['box'] == {'left': .1, 'top': .1, 'right': .6, 'bottom': .9}
    assert raw['crop_state']['at']
    # マージで既存キーが消えない
    assert raw['original_image'] == {'storage_path': 'unreleased/originals/1.png', 'mime': 'image/png'}
    assert raw['keep'] == 'x' and raw['crop_suggestion']['left'] == .1
    assert '?t=' in raw['card_image_url']
    assert sb.images[0]['public_url'] == raw['card_image_url']
    # 自動クロップは退避済みの元画像を Storage から取得する
    assert sb.downloads == ['unreleased/originals/1.png']


@pytest.mark.parametrize('raw', [
    {},
    {'crop_suggestion': _suggestion(source_image_url='https://other.test/a.png')},
    {'crop_suggestion': _suggestion(left=0, top=0, right=1, bottom=1)},
    {'crop_suggestion': _suggestion(left=float('nan'))},
    {'crop_suggestion': _suggestion(left='0.1')},
    {'crop_suggestion': _suggestion(left=.7, right=.6)},
    {'crop_suggestion': _suggestion(bottom=1.2)},
    {'crop_suggestion': _suggestion(top=-.1)},
])
def test_auto_crop_skipped(http, raw):
    sb = FakeSB(raw=raw)
    ok, _ = store.ingest_x_card_image(sb, 1, SRC, 't')
    assert ok
    assert 'crop_state' not in sb.raw
    assert [p for p, _ in sb.uploads] == ['unreleased/1.png', 'unreleased/originals/1.png']


def test_auto_crop_failure_keeps_ingest_success(http, monkeypatch):
    sb = FakeSB(raw={'crop_suggestion': _suggestion()})
    monkeypatch.setattr(store, 'crop_and_save_image', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('boom')))
    ok, _ = store.ingest_x_card_image(sb, 1, SRC, 't')
    assert ok and 'crop_state' not in sb.raw
    sb2 = FakeSB(raw={'crop_suggestion': _suggestion()})
    monkeypatch.setattr(store, 'crop_and_save_image', lambda *a, **kw: (False, 'クロップ失敗'))
    ok, _ = store.ingest_x_card_image(sb2, 1, SRC, 't')
    assert ok and sb2.raw['card_image_url'] == 'https://s.test/unreleased/1.png'


def test_already_ingested_does_nothing(http):
    sb = FakeSB(raw={'crop_suggestion': _suggestion()}, with_image_row=True)
    ok, reason = store.ingest_x_card_image(sb, 1, SRC, 't')
    assert ok and reason == '取込済み'
    assert sb.uploads == [] and 'crop_state' not in sb.raw


def test_backup_failure_does_not_fail_ingest(http):
    sb = FakeSB(raw={}, upload_fail_paths=('unreleased/originals/1.png',))
    ok, _ = store.ingest_x_card_image(sb, 1, SRC, 't')
    assert ok
    assert 'original_image' not in sb.raw
    assert sb.raw['card_image_url'] == 'https://s.test/unreleased/1.png'


def test_backup_failure_then_auto_crop_uses_source(http):
    sb = FakeSB(raw={'crop_suggestion': _suggestion()}, upload_fail_paths=('unreleased/originals/1.png',))
    ok, _ = store.ingest_x_card_image(sb, 1, SRC, 't')
    assert ok and sb.raw['crop_state']['mode'] == 'auto'
    assert sb.downloads == []


# ── 元画像取得の優先順 ───────────────────────────────

def test_crop_prefers_originals_then_source(monkeypatch):
    calls = []
    data = _png()
    monkeypatch.setattr(store.requests, 'get', lambda url, **kw: calls.append(url) or _resp(data))
    sb = FakeSB(raw={'original_image': {'storage_path': 'unreleased/originals/1.png', 'mime': 'image/png'}},
                with_image_row=True, download_data=data)
    ok, _ = store.crop_and_save_image(sb, 1, 0, 0, .5, 1)
    assert ok and calls == [] and sb.downloads == ['unreleased/originals/1.png']
    assert sb.raw['crop_state']['mode'] == 'manual'

    # originals のダウンロードに失敗 → source_image_url
    sb = FakeSB(raw={'original_image': {'storage_path': 'unreleased/originals/1.png', 'mime': 'image/png'}},
                with_image_row=True)
    ok, _ = store.crop_and_save_image(sb, 1, 0, 0, .5, 1)
    assert ok and calls == [SRC]


def test_crop_does_not_fall_back_to_public_when_source_fails_after_crop(monkeypatch):
    # クロップ済み・退避なし・Xの取得失敗: public_url（クロップ済み）から二重クロップしない
    calls = []

    def get(url, **kw):
        calls.append(url)
        raise RuntimeError('gone')
    monkeypatch.setattr(store.requests, 'get', get)
    sb = FakeSB(raw={'crop_state': {'mode': 'manual', 'box': {}, 'at': 'x'}}, with_image_row=True)
    ok, err = store.crop_and_save_image(sb, 1, 0, 0, .5, 1)
    assert not ok and '画像取得失敗' in err
    assert calls == [SRC] and sb.uploads == []


def test_crop_public_fallback_only_when_safe(monkeypatch):
    # source_url が無く、未クロップ（crop_state なし）なら public_url を使ってよい
    data = _png()
    calls = []
    monkeypatch.setattr(store.requests, 'get', lambda url, **kw: calls.append(url) or _resp(data))
    sb = FakeSB(with_image_row=True)
    sb.images[0]['source_image_url'] = ''
    ok, _ = store.crop_and_save_image(sb, 1, 0, 0, .5, 1)
    assert ok and calls == ['https://s.test/unreleased/1.png']
    # source_url が無くてもクロップ済みなら使わない
    sb = FakeSB(raw={'crop_state': {'mode': 'auto'}}, with_image_row=True)
    sb.images[0]['source_image_url'] = ''
    ok, err = store.crop_and_save_image(sb, 1, 0, 0, .5, 1)
    assert not ok


def test_ingest_auto_crop_may_use_public_when_source_fails_later(monkeypatch):
    # 取り込み直後（Storage 未クロップ）は明示フラグで public_url へのフォールバックを許す
    data = _png()
    seq = {'n': 0}

    def get(url, **kw):
        seq['n'] += 1
        if seq['n'] > 1 and url == SRC:   # 取り込み時の取得以外の SRC 取得は失敗
            raise RuntimeError('gone')
        return _resp(data)
    monkeypatch.setattr(store.requests, 'get', get)
    sb = FakeSB(raw={'crop_suggestion': _suggestion()}, upload_fail_paths=('unreleased/originals/1.png',))
    ok, _ = store.ingest_x_card_image(sb, 1, SRC, 't')
    assert ok and sb.raw['crop_state']['mode'] == 'auto'


def test_late_backup_from_source(http):
    sb = FakeSB(raw={'keep': 1}, with_image_row=True)
    ok, _ = store.crop_and_save_image(sb, 1, 0, 0, .5, 1)
    assert ok
    assert sb.raw['original_image'] == {'storage_path': 'unreleased/originals/1.png', 'mime': 'image/png'}
    assert sb.storage_files['unreleased/originals/1.png'] == http
    assert sb.raw['keep'] == 1


# ── 縮小版（新着欄用） ───────────────────────────────

def _thumb_size(sb, i=-1):
    path, data, opts = sb.thumb_uploads[i]
    im = Image.open(io.BytesIO(data))
    assert path == 'unreleased/thumbs/1.webp' and im.format == 'WEBP'
    assert opts['content-type'] == 'image/webp' and 'cache-control' not in opts  # 原寸と同じ既定（1時間）
    return im.size


def test_thumbnail_follows_auto_crop(http):
    sb = FakeSB(raw={'crop_suggestion': _suggestion()})
    ok, _ = store.ingest_x_card_image(sb, 1, SRC, 'https://x.com/t/1')
    assert ok
    # 取り込み時（原寸）と切り抜き後の2回作り、最後は切り抜き後の大きさ
    assert len(sb.thumb_uploads) == 2
    assert _thumb_size(sb, 0) == (100, 100) and _thumb_size(sb) == (50, 80)
    thumb = sb.images[0]['thumb_url']
    assert thumb.startswith('https://s.test/unreleased/thumbs/1.webp?v=')
    # 同じ秒の中でも、切り抜き前と後の縮小版は別の URL になる（中身のハッシュ）
    first = store.hashlib.sha1(sb.thumb_uploads[0][1]).hexdigest()[:12]
    last = store.hashlib.sha1(sb.thumb_uploads[1][1]).hexdigest()[:12]
    assert first != last and thumb.endswith('?v=' + last)


def test_thumbnail_failure_keeps_ingest_and_clears_url(http):
    sb = FakeSB(raw={'crop_suggestion': _suggestion()}, upload_fail_paths=('unreleased/thumbs/1.webp',))
    ok, _ = store.ingest_x_card_image(sb, 1, SRC, 'https://x.com/t/1')
    assert ok and sb.raw['crop_state']['mode'] == 'auto'
    assert sb.images[0]['thumb_url'] is None and '?t=' in sb.images[0]['public_url']


def test_crop_thumbnail_failure_does_not_leave_uncropped_thumb(http):
    sb = FakeSB(with_image_row=True)
    sb.images[0]['thumb_url'] = 'https://s.test/unreleased/thumbs/1.webp?t=old'
    sb.upload_fail_paths = ('unreleased/thumbs/1.webp',)
    ok, _ = store.crop_and_save_image(sb, 1, .1, .1, .6, .9)
    assert ok
    assert sb.images[0]['thumb_url'] is None  # 切り抜き前の縮小版を出し続けない


def test_restore_rebuilds_thumbnail(http):
    original = _png(80, 60)
    sb = FakeSB(raw={'original_image': {'storage_path': 'unreleased/originals/1.png', 'mime': 'image/png'}},
                with_image_row=True, download_data=original)
    ok, _ = store.restore_original_image(sb, 1)
    assert ok and _thumb_size(sb) == (80, 60)
    assert '?v=' in sb.images[0]['thumb_url']


def test_make_thumbnail_scales_down_keeps_alpha_and_orientation():
    def enc(img, fmt, **kw):
        b = io.BytesIO(); img.save(b, fmt, **kw); return b.getvalue()
    big = Image.open(io.BytesIO(store.make_thumbnail(enc(Image.new('RGB', (600, 874), 'red'), 'PNG'))))
    assert big.format == 'WEBP' and big.size == (300, 437)
    small = Image.open(io.BytesIO(store.make_thumbnail(enc(Image.new('RGB', (200, 291)), 'JPEG'))))
    assert small.size == (200, 291)  # 拡大はしない
    alpha = Image.open(io.BytesIO(store.make_thumbnail(enc(Image.new('RGBA', (40, 60), (0, 0, 0, 0)), 'PNG'))))
    assert alpha.mode == 'RGBA'
    palette = Image.new('P', (40, 60)); palette.info['transparency'] = 0
    assert Image.open(io.BytesIO(store.make_thumbnail(enc(palette, 'PNG')))).mode == 'RGBA'
    # EXIF の向き（6 = 90度回転）を反映してから縮める。ブラウザの表示と同じ向きになる
    exif = Image.Exif(); exif[0x0112] = 6
    rotated = Image.open(io.BytesIO(store.make_thumbnail(enc(Image.new('RGB', (60, 40)), 'JPEG', exif=exif))))
    assert rotated.size == (40, 60)
    # 色の基準（ICC）は引き継ぐ。CMYK は付けない
    from PIL import ImageCms
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
    kept = Image.open(io.BytesIO(store.make_thumbnail(enc(Image.new('RGB', (40, 60)), 'PNG', icc_profile=icc))))
    assert kept.info.get('icc_profile') == icc
    cmyk = Image.open(io.BytesIO(store.make_thumbnail(enc(Image.new('CMYK', (40, 60)), 'JPEG', icc_profile=icc))))
    assert cmyk.mode == 'RGB' and not cmyk.info.get('icc_profile')


def test_collect_paths_include_thumbnail():
    sb = FakeSB(raw={'original_image': {'storage_path': 'unreleased/originals/1.jpg', 'mime': 'image/jpeg'}})
    assert store.collect_original_paths(sb, [1]) == ['unreleased/originals/1.jpg', 'unreleased/thumbs/1.webp']


# ── 復元 ─────────────────────────────────────────

def test_restore_writes_original_bytes_as_is(http):
    original = _png(80, 60)
    sb = FakeSB(raw={'original_image': {'storage_path': 'unreleased/originals/1.png', 'mime': 'image/png'},
                     'crop_state': {'mode': 'auto', 'box': {}, 'at': 'x'}, 'keep': 1},
                with_image_row=True, download_data=original)
    ok, url = store.restore_original_image(sb, 1)
    assert ok and '?t=' in url
    assert sb.uploads == [('unreleased/1.png', original)]
    assert sb.images[0]['public_url'] == url and sb.raw['card_image_url'] == url
    assert sb.raw['crop_state']['mode'] == 'original' and sb.raw['crop_state']['box'] is None
    assert sb.raw['keep'] == 1 and sb.raw['original_image']['storage_path'] == 'unreleased/originals/1.png'


def test_restore_from_source_backs_up_and_failures(http, monkeypatch):
    sb = FakeSB(with_image_row=True)
    ok, url = store.restore_original_image(sb, 1)
    assert ok
    assert [p for p, _ in sb.uploads] == ['unreleased/1.png', 'unreleased/originals/1.png']
    assert sb.uploads[0][1] == http
    assert sb.raw['original_image']['storage_path'] == 'unreleased/originals/1.png'

    # 画像レコード無し／取得不能
    assert store.restore_original_image(FakeSB(), 1) == (False, '画像レコードが見つかりません')
    monkeypatch.setattr(store.requests, 'get', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('x')))
    ok, reason = store.restore_original_image(FakeSB(with_image_row=True), 1)
    assert not ok and '画像取得失敗' in reason


# ── エンドポイント ───────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(admin, '_ADMIN_KEY', 'k')
    monkeypatch.setattr(admin, '_check_global_auth_rate_limit', lambda: False)
    monkeypatch.setattr(admin, '_check_auth_rate_limit', lambda _ip: False)
    monkeypatch.setattr(admin, '_supabase', object())
    monkeypatch.setattr(admin._card_display, 'invalidate_cache', lambda: None)
    app = Flask(__name__)
    app.register_blueprint(admin.admin_bp)
    return app.test_client()


def test_restore_endpoint_auth_and_response(client, monkeypatch):
    monkeypatch.setattr(store, 'restore_original_image', lambda sb, cid: (True, 'https://s.test/a?t=1'))
    assert client.post('/api/admin/unreleased/1/restore-original-image').status_code == 401
    r = client.post('/api/admin/unreleased/1/restore-original-image', headers={'X-Admin-Key': 'k'})
    assert r.status_code == 200 and r.get_json() == {'ok': True, 'new_url': 'https://s.test/a?t=1'}
    monkeypatch.setattr(store, 'restore_original_image', lambda sb, cid: (False, '失敗'))
    r = client.post('/api/admin/unreleased/1/restore-original-image', headers={'X-Admin-Key': 'k'})
    assert r.status_code == 422 and r.get_json() == {'ok': False, 'error': '失敗'}


# ── 入力検証（restore / source 取得） ─────────────────

def _bad_response(monkeypatch, data, mime):
    monkeypatch.setattr(store.requests, 'get', lambda *a, **kw: _resp(data, mime))


@pytest.mark.parametrize('data,mime,msg', [
    (b'<html></html>', 'text/html', 'image/\\*'),
    (b'not an image', 'image/png', '不正'),
    (b'x' * (5 * 1024 * 1024 + 1), 'image/png', '5MB'),
], ids=['not-image-type', 'broken-bytes', 'too-large'])
def test_restore_rejects_invalid_source_without_writing(monkeypatch, data, mime, msg):
    _bad_response(monkeypatch, data, mime)
    sb = FakeSB(raw={'crop_state': {'mode': 'auto'}}, with_image_row=True)
    ok, reason = store.restore_original_image(sb, 1)
    assert not ok
    import re
    assert re.search(msg, reason)
    assert sb.uploads == []
    assert sb.raw == {'crop_state': {'mode': 'auto'}}


# ── EXIF・モック精度・ログ ───────────────────────────

def test_auto_crop_uses_exif_corrected_coordinates(monkeypatch):
    original = Image.new('RGB', (60, 100), 'red')
    original.paste('blue', (0, 50, 60, 100))
    exif = Image.Exif()
    exif[274] = 6
    buf = io.BytesIO()
    original.save(buf, 'JPEG', exif=exif)
    monkeypatch.setattr(store.requests, 'get', lambda *a, **kw: _resp(buf.getvalue(), 'image/jpeg'))
    sb = FakeSB(raw={'crop_suggestion': _suggestion(left=0, top=0, right=.5, bottom=1)})
    ok, _ = store.ingest_x_card_image(sb, 1, SRC, 't')
    assert ok
    cropped = Image.open(io.BytesIO(sb.uploads[-1][1]))
    assert cropped.size == (50, 60)  # EXIF 回転後(100x60)の左半分
    red, _g, blue = cropped.getpixel((25, 30))
    assert blue > 200 and red < 30


def test_fake_sb_ignores_other_card_rows():
    sb = FakeSB(raw={'a': 1}, with_image_row=True)
    assert store._read_extraction_raw(sb, 2) == {}
    assert store._merge_extraction_raw(sb, 2, {'b': 2}) is False
    assert sb.raw == {'a': 1}


def test_warning_logs(http, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger=store.logger.name)
    monkeypatch.setattr(store, 'crop_and_save_image', lambda *a, **kw: (False, '失敗理由X'))
    store.ingest_x_card_image(FakeSB(raw={'crop_suggestion': _suggestion()}), 1, SRC, 't')
    assert any('自動クロップ失敗' in r.message and '失敗理由X' in r.message and r.levelno == logging.WARNING for r in caplog.records)
    caplog.clear()
    store.ingest_x_card_image(FakeSB(raw={}), 1, SRC, 't')
    assert any('自動クロップ見送り' in r.message and r.levelno == logging.INFO for r in caplog.records)
    caplog.clear()
    store._merge_extraction_raw(FakeSB(), 99, {'a': 1})
    assert any('見つかりません' in r.message and r.levelno == logging.WARNING for r in caplog.records)
    caplog.clear()
    store.ingest_x_card_image(FakeSB(upload_fail_paths=('unreleased/originals/1.png',)), 1, SRC, 't')
    assert any('元画像の退避失敗' in r.message and r.levelno == logging.WARNING for r in caplog.records)


def test_crop_state_records_method_and_refinement(http):
    sb = FakeSB(raw={'crop_suggestion': _suggestion(method='vision', refinement='image_boundary')})
    store.ingest_x_card_image(sb, 1, SRC, 't')
    assert sb.raw['crop_state']['method'] == 'vision'
    assert sb.raw['crop_state']['refinement'] == 'image_boundary'


# ── GIF・公開URLなし ────────────────────────────────

def test_gif_cropped_as_png_with_matching_content_type(monkeypatch):
    img = Image.new('P', (40, 40), 1)
    buf = io.BytesIO()
    img.save(buf, 'GIF')
    monkeypatch.setattr(store.requests, 'get', lambda *a, **kw: _resp(buf.getvalue(), 'image/gif'))
    types = []
    sb = FakeSB(with_image_row=True)
    orig_upload = sb.upload
    sb.upload = lambda path, file, file_options=None: (types.append((path, file_options['content-type'])), orig_upload(path, file, file_options))
    ok, _ = store.crop_and_save_image(sb, 1, 0, 0, .5, 1)
    assert ok
    path, ctype = types[0]
    assert ctype == 'image/png'
    assert Image.open(io.BytesIO(sb.storage_files[path])).format == 'PNG'


def test_no_broken_url_when_public_url_empty(http):
    sb = FakeSB(with_image_row=True, public_url='')
    sb.get_public_url = lambda path: ''
    ok, url = store.crop_and_save_image(sb, 1, 0, 0, .5, 1)
    assert ok and url == ''
    assert sb.images[0]['public_url'] == ''
    assert 'card_image_url' not in sb.raw


# ── 削除時に退避画像も消す ───────────────────────────

def test_collect_original_paths_uses_record_or_candidates():
    sb = FakeSB(raw={'original_image': {'storage_path': 'unreleased/originals/1.webp', 'mime': 'image/webp'}})
    assert store.collect_original_paths(sb, [1]) == ['unreleased/originals/1.webp', 'unreleased/thumbs/1.webp']
    sb = FakeSB(raw={})
    assert store.collect_original_paths(sb, [1]) == [f'unreleased/originals/1.{e}' for e in ('jpg', 'png', 'gif', 'webp')] + ['unreleased/thumbs/1.webp']


def test_reconcile_delete_images_removes_originals():
    import reconcile_unreleased as rec

    class SB(FakeSB):
        def table(self, name):
            q = _Q(self, name)
            if name == 'official_card_images':
                orig = q.execute
                q.execute = lambda: SimpleNamespace(data=[{'storage_path': 'unreleased/1.png'}])
            return q
    sb = SB(raw={'original_image': {'storage_path': 'unreleased/originals/1.png', 'mime': 'image/png'}})
    rec._delete_images_for_cards(sb, [1])
    assert sb.removed == ['unreleased/1.png', 'unreleased/originals/1.png', 'unreleased/thumbs/1.webp']


def test_purge_domain_endpoint_removes_originals(client, monkeypatch):
    store_sb = FakeSB(raw={'original_image': {'storage_path': 'unreleased/originals/1.png', 'mime': 'image/png'}},
                      with_image_row=True)
    store_sb.images[0].update({'source_domain': 'pbs.twimg.com', 'hidden': False, 'deleted_at': None})

    class Q2(_Q):
        def in_(self, *a): return self
        def execute(self):
            if self.name == 'official_card_images' and self.op == 'update':
                return SimpleNamespace(data=[{'id': 1}])
            if self.name == 'official_card_images':
                return SimpleNamespace(data=[{'id': 1, 'unreleased_card_id': 1, 'storage_path': 'unreleased/1.png'}])
            return super().execute()
    store_sb.table = lambda name: Q2(store_sb, name)
    monkeypatch.setattr(admin, '_supabase', store_sb)
    r = client.post('/api/admin/images/purge-domain', json={'domain': 'pbs.twimg.com', 'physical': True},
                    headers={'X-Admin-Key': 'k'})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert store_sb.removed == ['unreleased/1.png', 'unreleased/originals/1.png', 'unreleased/thumbs/1.webp']


def _delete_sb(raw, remove_fail=False):
    sb = FakeSB(raw=raw)
    order = []

    class Q(_Q):
        def delete(self):
            self.op = 'delete'
            return self

        def execute(self):
            if self.op == 'delete':
                order.append('delete')
                return SimpleNamespace(data=[{'id': 1}] if self._matches() else [])
            return super().execute()
    sb.table = lambda name: Q(sb, name)

    def remove(paths):
        if remove_fail:
            raise RuntimeError('remove failed')
        order.append('remove')
        sb.removed.extend(paths)
    sb.remove = remove
    return sb, order


def test_delete_endpoint_removes_originals_after_row_delete(client, monkeypatch):
    sb, order = _delete_sb({'original_image': {'storage_path': 'unreleased/originals/1.png', 'mime': 'image/png'}})
    monkeypatch.setattr(admin, '_supabase', sb)
    r = client.delete('/api/admin/unreleased/1', headers={'X-Admin-Key': 'k'})
    assert r.status_code == 200 and r.get_json()['ok'] is True
    assert sb.removed == ['unreleased/originals/1.png', 'unreleased/thumbs/1.webp']
    assert order == ['delete', 'remove']  # パスは削除前に収集済みで、remove は行削除の後


def test_delete_endpoint_succeeds_even_if_remove_fails(client, monkeypatch, caplog):
    sb, _ = _delete_sb({}, remove_fail=True)
    monkeypatch.setattr(admin, '_supabase', sb)
    caplog.set_level(logging.WARNING)
    r = client.delete('/api/admin/unreleased/1', headers={'X-Admin-Key': 'k'})
    assert r.status_code == 200
    assert any('退避元画像の削除失敗' in rec.message for rec in caplog.records)


def test_delete_endpoint_404_does_not_remove(client, monkeypatch):
    sb, _ = _delete_sb({})
    monkeypatch.setattr(admin, '_supabase', sb)
    r = client.delete('/api/admin/unreleased/2', headers={'X-Admin-Key': 'k'})
    assert r.status_code == 404 and sb.removed == []
