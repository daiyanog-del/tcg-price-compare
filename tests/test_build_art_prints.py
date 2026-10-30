"""tools/build_art_prints（Yugipedia から型番ごと違う絵の表を作る）の読み取りのテスト。
wikitext は 2026-10-29 に Yugipedia から取れた実際の値（必要な欄だけ抜き出したもの）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import build_art_prints as bp  # noqa: E402

TTT = """{{CardTable2
| ja_name         = 三戦の号
| image           = 
1; TripleTacticsThrust-LAVD-EN-C-1E.png;  TripleTacticsThrust-MADU-EN-VG-artwork.png
2; TripleTacticsThrust-CH01-EN-UR-1E.png; TripleTacticsThrust-MADU-EN-VG-artwork-2.png
| jp_sets         = 
PHHY-JP069; Photon Hypernova; Rare
TT01-JPA17; Tactical-Try Deck: Decisive Strike Cyber Dragon; Common
QCAC-JP096; Quarter Century Art Collection; Ultra Rare, Secret Rare, Quarter Century Secret Rare
CH01-JP030; THE CHRONICLES DECK: The Fallen & The Virtuous (All-Foil Edition); Super Rare, Secret Rare
| card_type = Spell
}}"""

MASQ = """{{CardTable2
| ja_name               = {{Ruby|Ｉ|アイ}}：{{Ruby|Ｐ|ピー}}マスカレーナ
| image                 = 
1;   IPMasquerena-QCAC-JP-UR.png    ; IPMasquerena-MADU-JP-VG-artwork.png
1.1; IPMasquerena-LAVD-EN-C-1E.png  ; IPMasquerena-MADU-EN-VG-artwork.png
2;   IPMasquerena-RA02-EN-SR-1E.png ; IPMasquerena-MADU-EN-VG-artwork-2.png
3;   IPMasquerena-RA05-EN-UR-1E.png ; IPMasquerena-CA.png
4;   IPMasquerena-LOCH-JP-UR.png    ; IPMasquerena-MADU-JP-VG-artwork-3.png
| jp_sets               = 
CHIM-JP049; Chaos Impact; Super Rare, Secret Rare, 20th Secret Rare
PAC1-JP034; Prismatic Art Collection; Ultra Rare, Secret Rare, Prismatic Secret Rare
QCAC-JP069; Quarter Century Art Collection; Ultra Rare, Secret Rare, Quarter Century Secret Rare
LOCH-JP027; Limit Over Collection: The Heroes; Ultra Rare, Secret Rare, Prismatic Secret Rare
| a = b
}}"""

ASH_GALLERY = """{{Card gallery|region=EN|
RA04-EN009; Quarter Century Stampede; SR; 1E
}}
{{Card gallery|region=JP|
PAC1-JP016; Prismatic Art Collection; ScR
PAC1-JP016; Prismatic Art Collection; ScR; AA //description::Alternate artwork
QCAC-JP050; Quarter Century Art Collection; ScR; AA //description::Alternate artwork
LPST-JP009; Limited Pack: Stamp Edition ::OP
}}"""


def test_reprint_pack_of_second_art_is_candidate():
    info = bp.card_info(TTT)
    assert info == {"ja_name": "三戦の号", "arts": 2, "alt_candidates": {"CH01-JP030": 2}}


def test_ruby_name_and_later_art_number():
    # 1.1 は1番の絵の小さな違い。2・3 は英語版だけのパックなので日本の型番に届かず、4 の LOCH だけが残る
    info = bp.card_info(MASQ)
    assert info == {"ja_name": "Ｉ：Ｐマスカレーナ", "arts": 4, "alt_candidates": {"LOCH-JP027": 4}}


def test_single_art_card_is_skipped():
    assert bp.card_info(TTT.replace("2; TripleTacticsThrust-CH01-EN-UR-1E.png;", "1.1; X-CH01-EN-UR-1E.png;")) is None


def test_pack_shared_with_first_art_is_not_candidate():
    text = TTT.replace("1; TripleTacticsThrust-LAVD-EN-C-1E.png;", "1; TripleTacticsThrust-CH01-JP-SR.png;")
    assert bp.card_info(text)["alt_candidates"] == {}


def test_gallery_aa_codes_only_from_jp_section():
    assert bp.gallery_aa_codes(ASH_GALLERY) == {"PAC1-JP016", "QCAC-JP050"}


def test_aa_code_is_removed_from_table():
    info = {"ja_name": "x", "arts": 3, "alt_candidates": {"PAC1-JP016": 2, "LOCH-JP027": 3}}
    assert bp.alt_codes(info, {"PAC1-JP016"}) == {"LOCH-JP027": 3}


def test_build_keys_by_comparable_name(monkeypatch):
    monkeypatch.setattr(bp, "_galleries", lambda titles: {})
    table, n = bp.build([("Triple Tactics Thrust", TTT), ("I:P Masquerena", MASQ)])
    assert n == 2
    assert table == {"三戦の号": {"arts": 2, "codes": {"CH01-JP030": 2}},
                     "IPマスカレーナ": {"arts": 4, "codes": {"LOCH-JP027": 4}}}


def test_lowercase_ruby_is_removed():
    # 光霊使いライナのページは {{ruby|…}}（小文字）で書かれている
    assert bp._unruby("光{{ruby|霊|れい}}{{ruby|使|つか}}いライナ") == "光霊使いライナ"
