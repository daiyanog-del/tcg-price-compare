"""
ニューロン連携 — cid（konami_id）からカード名を解決する
=======================================================

拡張機能（tcgym-neuron-extension）はニューロンのカード詳細URLに必ず含まれる
cid を送ってくる。cid はコナミ公式DBのカードIDで、ygoresources の konami_id と
同一体系（2026-08-05 に11ペアの実測突合で確認）。

DOMからカード名をスクレイピングする旧方式は、ニューロンの読み仮名
（<span class="ruby">）を誤って拾う・ページ改版で壊れるという構造的な弱さが
あったため、IDで受けてサーバー側で名前解決する方式に置き換えた。

ygoresources 未収録の新カード（発売直後・未発売）に備え、拡張はDOMから読んだ
カード名も併送する。cid の解決に失敗した場合のみそのフォールバック名を使う。

Flask には依存しない（テスト容易性のため）。ルート配線は app.py 側。
"""

# card_page（/card/<name>）の上限と同じ値。超える名前は解決失敗として扱う
MAX_CARD_NAME_LEN = 50


def resolve_card_name(cid, fallback_name, summary_getter):
    """cid をカード名に解決する。解決できなければ None。

    Args:
        cid: コナミ公式DBのカードID（= ygoresources の konami_id）
        fallback_name: 拡張がDOMから読んだカード名（ygores未収録カード用の予備）
        summary_getter: konami_id -> summary dict を返す callable
            （ygores_repository.CardDataRepository.get_card_summary 互換。
              失敗時は None を返すか例外を投げる）
    """
    try:
        summary = summary_getter(cid)
    except Exception:
        summary = None
    name = ((summary or {}).get("name") or "").strip()
    if name and len(name) <= MAX_CARD_NAME_LEN:
        return name
    fb = (fallback_name or "").strip()
    if fb and len(fb) <= MAX_CARD_NAME_LEN:
        return fb
    return None
