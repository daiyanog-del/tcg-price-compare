"""
カード名データベース更新スクリプト
==================================
遊戯王OCGの全カード日本語名を取得し、data/cardnames_ja.json に保存する。

データソース:
  1. YGOrganization DB (優先) — /data/idx/card/name/ja
  2. YGOProDeck API (フォールバック) — /api/v7/cardinfo.php?language=ja

使い方:
    python update_cardnames.py

GitHub Actions から毎週自動実行。手動でも実行可能。
"""

import json
import re
import sys
import requests
from pathlib import Path

OUTPUT_FILE = Path(__file__).parent / "data" / "cardnames_ja.json"
READING_MAP_FILE = Path(__file__).parent / "data" / "cardnames_reading.json"
MIN_EXPECTED_CARDS = 1000  # これ未満なら異常とみなす


# 全てひらがな（＋長音・中黒・スペース等）で構成された名前は読み仮名と判定
_HIRAGANA_ONLY = re.compile(r'^[\u3040-\u309F\u30FC\u30FB\s・ー〜、。「」『』（）\-]+$')
# 漢字（CJK統合漢字 + CJK統合漢字拡張A）を含むかどうか
_HAS_KANJI = re.compile(r'[\u4E00-\u9FFF\u3400-\u4DBF]')


def _is_reading(name: str) -> bool:
    """読み仮名（ひらがなのみ）かどうか判定"""
    return bool(_HIRAGANA_ONLY.match(name))


def _has_kanji(name: str) -> bool:
    """漢字を含むかどうか判定"""
    return bool(_HAS_KANJI.search(name))


def fetch_from_ygorganization() -> tuple[list[str], dict[str, str]]:
    """YGOrganization DB からカード名一覧を取得（読み仮名を除外、読み→正式名称マップも返す）"""
    print("[1] YGOrganization DB から取得中...")
    url = "https://db.ygorganization.com/data/idx/card/name/ja"
    resp = requests.get(
        url,
        headers={"User-Agent": "TCGPriceCompare/1.0"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    reading_map = {}  # 読み仮名 → 正式名称

    if isinstance(data, dict):
        # カードIDごとに名前をグループ化
        from collections import defaultdict
        id_to_names = defaultdict(list)
        for name, card_ids in data.items():
            if isinstance(card_ids, list):
                for cid in card_ids:
                    id_to_names[cid].append(name)
            else:
                id_to_names[card_ids].append(name)

        # 各カードIDについて、読み仮名を除外して正式名称だけ残す
        names_set = set()
        for cid, name_list in id_to_names.items():
            kanji_names = [n for n in name_list if _has_kanji(n)]
            non_kanji_names = [n for n in name_list if not _has_kanji(n)]

            if kanji_names:
                # 漢字を含む名前を正式名称とし、含まない名前は読み仮名扱い
                names_set.update(kanji_names)
                for r in non_kanji_names:
                    reading_map[r] = kanji_names[0]
            else:
                # 漢字なし（カタカナのみ等）: 従来通り _is_reading で判定
                formal = [n for n in name_list if not _is_reading(n)]
                reading = [n for n in name_list if _is_reading(n)]
                if formal:
                    names_set.update(formal)
                    for r in reading:
                        reading_map[r] = formal[0]
                else:
                    # ひらがなが正式名称のカード
                    names_set.update(reading)
        names = sorted(names_set)
    elif isinstance(data, list):
        names = sorted(set(str(item) for item in data if item and not _is_reading(str(item))))
    else:
        raise ValueError(f"予期しないレスポンス形式: {type(data)}")

    print(f"  -> {len(names)} 件取得 (読み仮名マップ: {len(reading_map)} 件)")
    return names, reading_map


def fetch_from_ygoprodeck() -> list[str]:
    """YGOProDeck API からカード名を取得（フォールバック）"""
    print("[2] YGOProDeck API から取得中 (フォールバック)...")
    url = "https://db.ygoprodeck.com/api/v7/cardinfo.php"
    resp = requests.get(
        url,
        params={"language": "ja"},
        headers={"User-Agent": "TCGPriceCompare/1.0"},
        timeout=60,
    )
    resp.raise_for_status()
    cards = resp.json().get("data", [])

    names = sorted(set(
        card["name"].strip()
        for card in cards
        if card.get("name", "").strip()
    ))
    print(f"  → {len(names)} 件取得")
    return names


def save_names(names: list[str], reading_map: dict[str, str] | None = None):
    """JSON ファイルに保存"""
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(
        json.dumps(names, ensure_ascii=False, indent=0),
        encoding="utf-8",
    )
    size_kb = OUTPUT_FILE.stat().st_size / 1024
    print(f"  保存: {OUTPUT_FILE} ({size_kb:.0f} KB, {len(names)} 件)")

    # 読み仮名マップも保存
    if reading_map:
        READING_MAP_FILE.write_text(
            json.dumps(reading_map, ensure_ascii=False, indent=0),
            encoding="utf-8",
        )
        rsize_kb = READING_MAP_FILE.stat().st_size / 1024
        print(f"  保存: {READING_MAP_FILE} ({rsize_kb:.0f} KB, {len(reading_map)} 件)")


def main():
    names = None
    reading_map = None

    # ソース1: YGOrganization DB
    try:
        names, reading_map = fetch_from_ygorganization()
    except Exception as e:
        print(f"  YGOrganization 失敗: {e}")

    # ソース2: YGOProDeck (フォールバック) — 読み仮名マップは作れない
    if not names or len(names) < MIN_EXPECTED_CARDS:
        try:
            names = fetch_from_ygoprodeck()
            reading_map = None
        except Exception as e:
            print(f"  YGOProDeck 失敗: {e}")

    # 結果チェック
    if not names or len(names) < MIN_EXPECTED_CARDS:
        print(f"取得件数不足 ({len(names) if names else 0} 件)")
        sys.exit(1)

    save_names(names, reading_map)
    print("更新完了")


if __name__ == "__main__":
    main()
