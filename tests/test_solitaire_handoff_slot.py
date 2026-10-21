"""マイデッキから開いたシミュレーター（/solitaire?from=mydeck）で、通知の欄をサーバーが最初から置くことの検証。

JS（static/shared/deck-handoff.js）が後から body の先頭へ差し込むと盤面全体が押し下がり、
スマホで CLS が 0.19 出ていた（2026-10-21 本番計測）。欄の高さは solitaire-brand.css の .sol-handoff-status で固定する。
"""
import pytest

SLOT = '<div id="deckHandoffStatus" class="sol-handoff-status" role="status"></div>'


@pytest.mark.parametrize('path, expected', [
    ('/solitaire?from=mydeck', True),
    ('/solitaire?from=meta', True),  # 環境デッキの「シミュレーターで回す」（2026-10-21）
    ('/solitaire?from=meta&replay=abc', False),
    ('/solitaire', False),
    ('/solitaire?from=other', False),
    # 共有リプレイ・端末保存は JS 側でも取り込まない（受け渡しより優先）
    ('/solitaire?from=mydeck&replay=abc', False),
    ('/solitaire?from=mydeck&saved=r_1', False),
])
def test_handoff_slot_rendered_only_for_mydeck(path, expected):
    import app as app_module
    html = app_module.app.test_client().get(path).get_data(as_text=True)
    assert (SLOT in html) is expected
    if expected:
        # 盤面より前（body の先頭）に置く。JS の prepend と同じ位置
        assert html.index(SLOT) < html.index('class="sol-page-layout"')
