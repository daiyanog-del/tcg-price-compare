"""GA（gtag.js）を load 後に遅延読込するテンプレートの検証。"""
import pytest


@pytest.mark.parametrize('path', ['/', '/solitaire'])
def test_gtag_script_is_injected_after_load_and_calls_are_queued(monkeypatch, path):
    import app as app_module
    monkeypatch.setattr(app_module, 'GA_MEASUREMENT_ID', 'G-TEST123')
    html = app_module.app.test_client().get(path).get_data(as_text=True)
    # 本体の <script src=gtag/js> を直書きしない（読込はスクリプトで load 後に差し込む）
    assert '<script async src="https://www.googletagmanager.com/gtag/js' not in html
    assert "s.src='https://www.googletagmanager.com/gtag/js?id=G-TEST123'" in html
    assert "addEventListener('load',later" in html
    # gtag() と config は即時に定義・積まれる（読込前のイベントは dataLayer に溜まる）
    assert "function gtag(){dataLayer.push(arguments);}gtag('js',new Date());gtag('config','G-TEST123');" in html


@pytest.mark.parametrize('path', ['/', '/solitaire'])
def test_no_ga_output_without_measurement_id(monkeypatch, path):
    import app as app_module
    monkeypatch.setattr(app_module, 'GA_MEASUREMENT_ID', '')
    html = app_module.app.test_client().get(path).get_data(as_text=True)
    assert 'googletagmanager' not in html
