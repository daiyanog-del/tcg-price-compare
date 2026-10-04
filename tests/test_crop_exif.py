"""表示・認識と同じEXIF補正後座標で画像を保存する回帰確認。"""
import io
import json
from types import SimpleNamespace
from PIL import Image
import unreleased_image_store as store


def test_crop_applies_exif_before_normalized_coordinates(monkeypatch):
    original = Image.new('RGB', (60, 100), 'red')
    original.paste('blue', (0, 50, 60, 100))
    exif = Image.Exif()
    exif[274] = 6
    buf = io.BytesIO()
    original.save(buf, 'JPEG', exif=exif)
    response = SimpleNamespace(content=buf.getvalue(), headers={'Content-Type': 'image/jpeg'}, raise_for_status=lambda: None)
    monkeypatch.setattr(store.requests, 'get', lambda *a, **kw: response)
    uploads = []
    class Query:
        def __init__(self, table): self.table = table
        def select(self, *a): return self
        def eq(self, *a): return self
        def is_(self, *a): return self
        def update(self, *a): return self
        def execute(self):
            return SimpleNamespace(data=[{'storage_path': 'test.jpg', 'public_url': 'https://example.test/test.jpg', 'source_image_url': 'https://pbs.twimg.com/test.jpg'}] if self.table == 'official_card_images' else [])
    bucket = SimpleNamespace(upload=lambda **kw: uploads.append(kw['file']))
    sb = SimpleNamespace(table=lambda name: Query(name), storage=SimpleNamespace(from_=lambda name: bucket))
    ok, _ = store.crop_and_save_image(sb, 1, 0, 0, .5, 1)
    assert ok
    actual = Image.open(io.BytesIO(uploads[0]))
    assert actual.size == (50, 60)
    red, green, blue = actual.getpixel((25, 30))
    assert blue > 200 and red < 30


def test_crop_preserves_pixel_coordinates_after_json_roundtrip(monkeypatch):
    # 実画像で455/902を往復すると454.99999999999994になった回帰ケース。
    original = Image.new('RGB', (1200, 902), 'red')
    original.paste('blue', (36, 455, 294, 830))
    buf = io.BytesIO()
    original.save(buf, 'PNG')
    response = SimpleNamespace(content=buf.getvalue(), headers={'Content-Type': 'image/png'}, raise_for_status=lambda: None)
    monkeypatch.setattr(store.requests, 'get', lambda *a, **kw: response)
    uploads = []
    class Query:
        def __init__(self, table): self.table = table
        def select(self, *a): return self
        def eq(self, *a): return self
        def is_(self, *a): return self
        def update(self, *a): return self
        def execute(self):
            return SimpleNamespace(data=[{'storage_path': 'test.png', 'public_url': 'https://example.test/test.png', 'source_image_url': 'https://pbs.twimg.com/test.png'}] if self.table == 'official_card_images' else [])
    bucket = SimpleNamespace(upload=lambda **kw: uploads.append(kw['file']))
    sb = SimpleNamespace(table=lambda name: Query(name), storage=SimpleNamespace(from_=lambda name: bucket))
    box = json.loads(json.dumps([36 / 1200, 455 / 902, 294 / 1200, 830 / 902]))
    ok, _ = store.crop_and_save_image(sb, 1, *box)
    assert ok
    actual = Image.open(io.BytesIO(uploads[0]))
    assert actual.size == (258, 375)
    assert actual.getextrema() == ((0, 0), (0, 0), (255, 255))
