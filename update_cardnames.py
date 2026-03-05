"""
カード名データベース更新スクリプト
==================================
YGOProDeck API から全遊戯王カードの日本語名を取得し、
data/cardnames_ja.json に保存する。

使い方:
    python update_cardnames.py

GitHub Actions から自動実行される。手動でも実行可能。
"""

import json
import sys
import requests
from pathlib import Path

API_URL = "https://db.ygoprodeck.com/api/v7/cardinfo.php"
OUTPUT_FILE = Path(__file__).parent / "data" / "cardnames_ja.json"


def fetch_all_cards_ja() -> list[str]:
    """YGOProDeck API から日本語カード名を全件取得"""
    print("YGOProDeck API からカードデータを取得中...")
    resp = requests.get(
        API_URL,
        params={"language": "ja"},
        headers={"User-Agent": "TCGPriceCompare/1.0"},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json().get("data", [])
    print(f"  {len(data)} 件のカードデータを取得")

    names = set()
    for card in data:
        name = card.get("name", "").strip()
        if name:
            names.add(name)

    sorted_names = sorted(names)
    print(f"  ユニークなカード名: {len(sorted_names)} 件")
    return sorted_names


def save_names(names: list[str]):
    """JSON ファイルに保存"""
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(
        json.dumps(names, ensure_ascii=False, indent=0),
        encoding="utf-8",
    )
    size_kb = OUTPUT_FILE.stat().st_size / 1024
    print(f"  保存: {OUTPUT_FILE} ({size_kb:.0f} KB, {len(names)} 件)")


def main():
    try:
        names = fetch_all_cards_ja()
        if len(names) < 1000:
            print(f"⚠️  取得件数が少なすぎます ({len(names)} 件)。API に問題がある可能性。")
            sys.exit(1)
        save_names(names)
        print("✅ 更新完了")
    except Exception as e:
        print(f"❌ エラー: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
