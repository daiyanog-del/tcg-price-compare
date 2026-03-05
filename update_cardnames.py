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
import sys
import requests
from pathlib import Path

OUTPUT_FILE = Path(__file__).parent / "data" / "cardnames_ja.json"
MIN_EXPECTED_CARDS = 1000  # これ未満なら異常とみなす


def fetch_from_ygorganization() -> list[str]:
    """YGOrganization DB からカード名一覧を取得（推奨）"""
    print("[1] YGOrganization DB から取得中...")
    url = "https://db.ygorganization.com/data/idx/card/name/ja"
    resp = requests.get(
        url,
        headers={"User-Agent": "TCGPriceCompare/1.0"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    # レスポンス形式: { "カード名": cardId, ... } の辞書
    if isinstance(data, dict):
        names = sorted(data.keys())
    elif isinstance(data, list):
        # 万が一リスト形式の場合
        names = sorted(set(str(item) for item in data if item))
    else:
        raise ValueError(f"予期しないレスポンス形式: {type(data)}")

    print(f"  → {len(names)} 件取得")
    return names


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
    names = None

    # ソース1: YGOrganization DB
    try:
        names = fetch_from_ygorganization()
    except Exception as e:
        print(f"  ⚠️  YGOrganization 失敗: {e}")

    # ソース2: YGOProDeck (フォールバック)
    if not names or len(names) < MIN_EXPECTED_CARDS:
        try:
            names = fetch_from_ygoprodeck()
        except Exception as e:
            print(f"  ⚠️  YGOProDeck 失敗: {e}")

    # 結果チェック
    if not names or len(names) < MIN_EXPECTED_CARDS:
        print(f"❌ 取得件数不足 ({len(names) if names else 0} 件)")
        sys.exit(1)

    save_names(names)
    print("✅ 更新完了")


if __name__ == "__main__":
    main()
