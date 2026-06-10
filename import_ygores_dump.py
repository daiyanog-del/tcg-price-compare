"""
ygoresources ダンプ importer
=============================
運営が公開しているデータを ygores_cards に一括投入する。
全件クロールは公式に禁止されているため、初期データ投入は必ずこのimporterで行うこと。

データソース（運営が公式Discordで案内している公開ミラー）:
    https://github.com/db-ygoresources-com/yugioh-card-history
    言語別フォルダに <konami_id>.json が1カード1ファイルで入っている（日本語は ja/）。
    ※収録パック情報・禁止制限ステータスはミラーに含まれない（運営談、APIから取得する設計）。

使い方:
    # ミラーのディレクトリ（1カード1ファイル）を投入
    SUPABASE_URL=... SUPABASE_KEY=... python import_ygores_dump.py <ja ディレクトリ>
    # 単一のまとめJSON（API形式 or ミラー形式の配列/マップ）も投入可
    SUPABASE_URL=... SUPABASE_KEY=... python import_ygores_dump.py <dump.json>

raw はそのまま保存する（API形式・ミラー形式の差は get_card_summary が吸収する）。
"""

import os
import json
import sys
import glob
import logging

from ygores_repository import repository

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _extract_id(konami_id, entry):
    """エントリ（API形式 or ミラー形式）から konami_id を決定。失敗時 None"""
    if konami_id is not None:
        try:
            return int(konami_id)
        except (TypeError, ValueError):
            return None
    if not isinstance(entry, dict):
        return None
    if entry.get("id") is not None:                       # ミラー形式は top-level id
        try:
            return int(entry["id"])
        except (TypeError, ValueError):
            return None
    for loc in (entry.get("cardData") or {}).values():    # API形式は cardData.<lang>.id
        if isinstance(loc, dict) and loc.get("id"):
            try:
                return int(loc["id"])
            except (TypeError, ValueError):
                pass
    return None


def _iter_items(path):
    """投入対象を (konami_id, raw) で列挙する。ディレクトリ/単一JSONの両対応。"""
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "*.json")))
        logger.info(f"[import] ディレクトリから {len(files)}ファイルを読み込みます")
        for fp in files:
            try:
                with open(fp, encoding="utf-8") as f:
                    entry = json.load(f)
            except Exception as e:
                logger.warning(f"[import] 読み込み失敗 {fp}: {e}")
                continue
            cid = _extract_id(None, entry)
            if cid is not None:
                yield cid, entry
        return
    with open(path, encoding="utf-8") as f:
        dump = json.load(f)
    if isinstance(dump, dict):
        pairs = dump.items()
    elif isinstance(dump, list):
        pairs = ((None, v) for v in dump)
    else:
        return
    for konami_id, entry in pairs:
        cid = _extract_id(konami_id, entry)
        if cid is not None:
            yield cid, entry


def import_dump(path: str) -> int:
    if repository._supabase() is None:
        logger.error("[import] Supabase未接続（SUPABASE_URL/KEYを確認）")
        return 1

    items = list(_iter_items(path))
    if not items:
        logger.error("[import] 取り込めるエントリがありません。形式の確認が必要です")
        return 1

    logger.info(f"[import] {len(items)}件 を投入します...")
    saved = repository.save_cards_bulk(items)
    logger.info(f"[import] 完了: {saved}/{len(items)}件 保存")
    return 0 if saved == len(items) else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("使い方: python import_ygores_dump.py <ディレクトリ or dump.json>")
        sys.exit(1)
    sys.exit(import_dump(sys.argv[1]))
