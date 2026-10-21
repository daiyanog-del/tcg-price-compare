"""トップの代わりのフォント（Noto Sans JP が届くまで・PC だけ）の検証。

system-ui で描いてから Noto Sans JP に入れ替わると、タブの幅と行の高さが変わり PC で CLS 0.016 が出ていた（2026-10-21）。
寸法を Noto Sans JP に合わせた 'Noto Sans JP Fallback' を、Noto Sans JP と同じ media の中だけで定義する。
"""
import re

NOTO_MEDIA = '(min-width:768px) and (hover:hover) and (pointer:fine)'


def test_fallback_face_is_pc_only_and_used_by_font_var():
    import app as app_module
    html = app_module.app.test_client().get('/').get_data(as_text=True)
    m = re.search(r'<style media="([^"]+)">\s*(@font-face\{font-family:\'Noto Sans JP Fallback\'.*?)</style>', html, re.S)
    assert m, '代わりのフォントの定義が無い'
    # Noto Sans JP を読む media と同じ（スマホ・タブレットでは使わない）
    assert m.group(1) == NOTO_MEDIA
    assert f'media="{NOTO_MEDIA}"' in html
    faces = m.group(2)
    assert faces.count('@font-face') == 2  # 通常と太字
    assert faces.count('ascent-override:116%') == 2 and faces.count('descent-override:28.8%') == 2
    assert "--font:       'Noto Sans JP', 'Noto Sans JP Fallback', system-ui, sans-serif;" in html
