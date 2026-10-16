"""GA（gtag.js）を HTML 解析後の空き時間に遅延読込するテンプレートの検証。"""
import pytest


@pytest.mark.parametrize('path', ['/', '/solitaire'])
def test_gtag_script_is_injected_after_parse_and_calls_are_queued(monkeypatch, path):
    import app as app_module
    monkeypatch.setattr(app_module, 'GA_MEASUREMENT_ID', 'G-TEST123')
    html = app_module.app.test_client().get(path).get_data(as_text=True)
    # 本体の <script src=gtag/js> を直書きしない（読込はスクリプトで解析後に差し込む）
    assert '<script async src="https://www.googletagmanager.com/gtag/js' not in html
    assert "s.src='https://www.googletagmanager.com/gtag/js?id=G-TEST123'" in html
    assert "document.addEventListener('DOMContentLoaded',later,{once:true})" in html
    # トップは取得を先に始め、差し込み時のURLと一致させる（一致しないと二重取得になる）。
    # シミュレーターは初期化の回線を優先して先読みしない
    preload = '<link rel="preload" as="script" href="https://www.googletagmanager.com/gtag/js?id=G-TEST123">'
    assert (preload in html) == (path == '/')
    # gtag() と config は即時に定義・積まれる（読込前のイベントは dataLayer に溜まる）
    assert "function gtag(){dataLayer.push(arguments);}gtag('js',new Date());gtag('config','G-TEST123');" in html


@pytest.mark.parametrize('path', ['/', '/solitaire'])
def test_no_ga_output_without_measurement_id(monkeypatch, path):
    import app as app_module
    monkeypatch.setattr(app_module, 'GA_MEASUREMENT_ID', '')
    html = app_module.app.test_client().get(path).get_data(as_text=True)
    assert 'googletagmanager' not in html
