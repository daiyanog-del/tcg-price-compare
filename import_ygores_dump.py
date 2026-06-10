"""
ygoresources ダンプファイル importer
=====================================
運営から提供されたダンプファイル（JSON）を ygores_cards に一括投入する。
全件クロールは公式に禁止されているため、初期データ投入は必ずこのimporterで行うこと。

使い方:
    SUPABASE_URL=... SUPABASE_KEY=... python import_ygores_dump.py <ダンプファイル.json>

対応形式（自動判別）:
    A) {"<konami_id>": {"cardData": {...}}, ...}   — /data/card/<id> レスポンスのID別マップ
    B) {"<konami_id>": {"ja": {...}, "en": {...}}, ...} — cardData の中身だけのマップ
    C) [{"cardData": {...}}, ...]                  — レスポンスのリスト（ID は cardData 内から取得）

# TODO: 実際のダンプ入手後に形式を確認して調整する（上記は未検証の前提）
"""

import json
import sys
import logging

from ygores_repository import repository

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _normalize_entry(konami_id, entry):
    """ダンプの1件を (konami_id, /data/card 互換のraw) に正規化する"""
    if not isinstance(entry, dict):
        return None
    if "cardData" in entry:
        raw = entry
    elif "ja" in entry or "en" in entry:
        # cardData の中身だけの形式 → /data/card レスポンス互換に包む
        raw = {"cardData": entry}
    else:
        return None
    if konami_id is None:
        # リスト形式: ID を cardData 内から探す
        for loc in (raw.get("cardData") or {}).values():
            if isinstance(loc, dict) and loc.get("id"):
                konami_id = loc["id"]
                break
    if konami_id is None:
        return None
    try:
        return int(konami_id), raw
    except (TypeError, ValueError):
        return None


def import_dump(file_path: str) -> int:
    with open(file_path, encoding="utf-8") as f:
        dump = json.load(f)

    if isinstance(dump, dict):
        candidates = ((k, v) for k, v in dump.items())
    elif isinstance(dump, list):
        candidates = ((None, v) for v in dump)
    else:
        logger.error("[import] 未対応のダンプ形式です（dict/list以外）")
        return 1

    items = []
    skipped = 0
    for konami_id, entry in candidates:
        normalized = _normalize_entry(konami_id, entry)
        if normalized:
            items.append(normalized)
        else:
            skipped += 1

    if not items:
        logger.error("[import] 取り込める形式のエントリがありません。ダンプ形式の確認が必要です")
        return 1
    if skipped:
        logger.warning(f"[import] 形式不明のため {skipped}件 をスキップしました")

    logger.info(f"[import] {len(items)}件 を投入します...")
    saved = repository.save_cards_bulk(items)
    logger.info(f"[import] 完了: {saved}/{len(items)}件 保存")
    return 0 if saved == len(items) else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("使い方: python import_ygores_dump.py <ダンプファイル.json>")
        sys.exit(1)
    sys.exit(import_dump(sys.argv[1]))
