"""tcg_translate（TCG先行カードの和訳）の、API を使わない部分のテスト"""
import json
import types

import pytest

import tcg_translate as tt


def _card(cid, name, text, **kw):
    return {"id": cid, "type": "monster", "name": name, "effectText": text, **kw}


@pytest.fixture
def corpus():
    en = {
        1: _card(1, "Aleister the Invoker", 'Add 1 "Invocation" from your Deck to your hand.'),
        2: _card(2, "Aleister the Invoker of Madness", 'If you control "Aleister the Invoker": draw 1 card.'),
        3: _card(3, "Toon Alpha", 'If you control "Toon World": Special Summon this card.'),
        4: _card(4, "Toon Beta", 'If you control "Toon World": add 1 "Toon" monster from your Deck to your hand.'),
        5: _card(5, "Toon Gamma", 'Add 1 "Toon" Spell from your Deck to your hand.'),
        6: _card(6, "Invocation", "Fusion Summon 1 Fusion Monster."),
        7: _card(7, "Toon World", "Pay 1000 LP."),
        8: _card(8, "English Only", "Draw 1 card."),
    }
    ja = {
        1: _card(1, "召喚師アレイスター", "デッキから「召喚魔術」１枚を手札に加える。"),
        2: _card(2, "狂気の召喚師アレイスター", "「召喚師アレイスター」が存在する場合、１枚ドローする。"),
        3: _card(3, "トゥーン・アルファ", "「トゥーン・ワールド」が存在する場合、このカードを特殊召喚する。"),
        4: _card(4, "トゥーン・ベータ", "「トゥーン・ワールド」が存在する場合、デッキから「トゥーン」モンスター１体を手札に加える。"),
        5: _card(5, "トゥーン・ガンマ", "デッキから「トゥーン」魔法カード１枚を手札に加える。"),
        6: _card(6, "召喚魔術", "融合モンスター１体を融合召喚する。"),
        7: _card(7, "トゥーン・ワールド", "１０００LPを払う。"),
        # 日本語欄に英文が入っている組は使わない（ミラーに実在する形: 23414）
        8: _card(8, "English Only", "Draw 1 card."),
    }
    return tt.Corpus(en, ja)


def test_英文入りの組は対訳に入れない(corpus):
    assert 8 not in corpus.pairs
    assert 1 in corpus.pairs


def test_カード名とテーマ名の対応表(corpus):
    gl = corpus.glossary_for('Add 1 "Toon" monster and "Toon World" and "Aleister" and "Unknown Thing".')
    assert gl["Toon World"] == "トゥーン・ワールド"   # カード名はそのまま対応
    assert gl["Toon"] == "トゥーン"                 # 効果文の共起から
    assert gl["Aleister"] == "召喚師アレイスター"     # カード名の共通部分から（公式の「アレイスター」より長い・既知の限界）
    assert "Unknown Thing" not in gl                 # 知らない名前は対応表に入れない（英語のまま残させる）


def test_似たカードは自分自身を除く(corpus):
    ids = corpus.similar('If you control "Toon World": Special Summon this card.', k=3, exclude={3})
    assert 3 not in ids
    assert ids  # 何かは返る


def test_依頼文に例と対応表が入る(corpus):
    msg, ex_ids = tt.build_user_message(corpus, _card(99, "New Toon", 'Add 1 "Toon" monster.'), k=2)
    assert "似たカードの英語と日本語の公式文" in msg
    assert "- Toon → トゥーン" in msg
    assert msg.rstrip().endswith("日本語:")
    assert len(ex_ids) <= 2


def test_依頼の形は構造化出力(corpus):
    p = tt.request_params("x", model="claude-opus-5-5", effort="medium")
    assert p["output_config"]["format"]["schema"] == tt.OUTPUT_SCHEMA
    assert p["output_config"]["effort"] == "medium"


def _msg(text, stop="end_turn"):
    return types.SimpleNamespace(stop_reason=stop, content=[types.SimpleNamespace(type="text", text=text)])


def test_応答の取り出し():
    assert tt.parse_message(_msg(json.dumps({"translation": " ①：ドローする。 "}))) == "①：ドローする。"
    with pytest.raises(ValueError):
        tt.parse_message(_msg(json.dumps({"translation": ""})))      # 空の訳を成功にしない
    with pytest.raises(ValueError):
        tt.parse_message(_msg("{}", stop="max_tokens"))              # 途中切れを成功にしない


def test_点検_数値は回数でなく有無で比べる(corpus):
    en = 'Special Summon 1 "X Token" (Psychic/EARTH/ATK 800/DEF 800).'
    assert not [w for w in tt.check(en, "「X Token」（サイキック族・地・攻／守８００）１体を特殊召喚する。", corpus)
                if w.startswith("数値")]
    assert "数値 800 が訳文に無い" in tt.check(en, "「X Token」を特殊召喚する。", corpus)


def test_点検_名前と省略(corpus):
    en = 'You can only use this effect of "My Card" once per turn. Add 1 "Toon" monster.'
    # 自分の名前は「このカード名」と書くので指摘しない。対応表の名前が違えば指摘する
    w = tt.check(en, "このカード名の効果は１ターンに１度しか使用できない。「Toon」モンスター１体を……", corpus, own_name="My Card")
    assert "名前「トゥーン」が訳文に無い" in w
    assert "訳文が「…」で省略されている" in w
    assert not any("My Card" in x for x in w)


def test_費用の計算():
    u = {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    assert tt.cost_usd(u, "claude-opus-5-5") == pytest.approx(4.0 + 2.0)
    assert tt.cost_usd(u, "claude-opus-5-5", batch=True) == pytest.approx(3.0)
