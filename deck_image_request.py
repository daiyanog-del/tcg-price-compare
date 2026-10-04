"""デッキ画像の入力検証。欠けた項目を黙って削らず依頼全体を検証する。"""


def validate_deck_image_request(body):
    if not isinstance(body, dict):
        raise ValueError("デッキ情報はオブジェクトで指定してください")
    name = body.get("name", "デッキ")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 50:
        raise ValueError("デッキ名は1〜50文字で指定してください")
    total = body.get("total", 0)
    # JavaScriptで正確に扱える整数の範囲をAPIでも守る。
    if type(total) is not int or not 0 <= total <= 9007199254740991:
        raise ValueError("合計金額は0以上の整数で指定してください")
    raw_cards = body.get("cards")
    if not isinstance(raw_cards, list) or not 1 <= len(raw_cards) <= 60:
        raise ValueError("カード一覧は1〜60項目で指定してください")
    cards = []
    for card in raw_cards:
        if not isinstance(card, dict):
            raise ValueError("各カードにはカード名と枚数を指定してください")
        card_name = card.get("name")
        qty = card.get("qty", 1)
        if not isinstance(card_name, str) or not card_name.strip() or len(card_name.strip()) > 50:
            raise ValueError("カード名は1〜50文字で指定してください")
        if type(qty) is not int or not 1 <= qty <= 99:
            raise ValueError("枚数は1〜99の整数で指定してください")
        cards.append({"name": card_name.strip(), "qty": qty})
    return name.strip(), cards, total
