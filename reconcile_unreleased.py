"""
reconcile_unreleased.py -- 未発売カード発売済み自動照合ジョブ

対象: unreleased_cards の status='approved' かつ konami_id IS NULL のレコード
処理: ygoresources の日本語カード名インデックス（カード名→konami_id リスト）と照合し、
      発売が確認できたカードを自動的に status='linked' へ更新する。

照合ロジック:
  (a) ファジーキー完全一致 + 候補1件 -> konami_id 設定 + status='linked'
  (b) ファジーキー完全一致 + 候補複数 -> status='needs_review'（管理画面で要確認）
  (c) 一致なし                          -> スキップ（まだ未発売）

ファジーキーは name_normalize.fuzzy_key を使う（app.py と同一ロジック）。

実行環境: GitHub Actions（update-cardnames.yml の末尾ステップ）
環境変数: SUPABASE_URL, SUPABASE_KEY

備考:
  - このジョブは Render 外（GitHub Actions）で動くため、
    card_display.py のメモリキャッシュへの無効化呼び出しは不要。
    Render 側のキャッシュは次回 TTL（30秒）到達時に自動更新される。
  - status='linked' になった後は、発売済みカードとして
    card_display.py の (1) 優先パスで解決される（ygores 名前インデックス経由）。
"""

import os
import sys
import logging

from name_normalize import fuzzy_key
from ygores_repository import repository as _ygores_repo

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)


def _get_supabase():
    """Supabase クライアントを生成して返す。環境変数未設定なら None を返す。"""
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_KEY')
    if not url or not key:
        logger.error('SUPABASE_URL または SUPABASE_KEY が未設定です')
        return None
    try:
        from supabase import create_client
        return create_client(url, key)
    except Exception as e:
        logger.error(f'Supabase 接続失敗: {e}')
        return None


def _fetch_approved_cards(sb) -> list[dict]:
    """
    status='approved' かつ konami_id IS NULL の unreleased_cards を取得する。
    """
    try:
        resp = (
            sb.table('unreleased_cards')
            .select('id, name')
            .eq('status', 'approved')
            .is_('konami_id', 'null')
            .execute()
        )
        return resp.data or []
    except Exception as e:
        logger.error(f'unreleased_cards 取得失敗: {e}')
        return []


def _build_fuzzy_index(name_index: dict) -> dict[str, list[str]]:
    """
    ygores の名前インデックス（カード名 -> konami_id リスト）から
    ファジーキー -> konami_id リスト の辞書を構築する。

    同一ファジーキーに複数の正規名称が対応するケース（異体字等）も考慮し、
    konami_id のリストを重複除去して保持する。
    """
    fuzzy_idx: dict[str, list[str]] = {}
    for name, ids in name_index.items():
        fk = fuzzy_key(name)
        # ids は int または int のリストどちらの場合もある
        id_list = ids if isinstance(ids, list) else [ids]
        if fk not in fuzzy_idx:
            fuzzy_idx[fk] = []
        for cid in id_list:
            cid_str = str(cid)
            if cid_str not in fuzzy_idx[fk]:
                fuzzy_idx[fk].append(cid_str)
    return fuzzy_idx


def _update_card(sb, card_id: int, konami_id: str | None, new_status: str) -> bool:
    """unreleased_cards の1件を更新する。"""
    try:
        row: dict = {'status': new_status}
        if konami_id is not None:
            row['konami_id'] = int(konami_id)
        sb.table('unreleased_cards').update(row).eq('id', card_id).execute()
        return True
    except Exception as e:
        logger.warning(f'更新失敗 id={card_id}: {e}')
        return False


def reconcile(sb) -> dict[str, int]:
    """
    照合処理本体。サマリ辞書 {linked, needs_review, skipped, error} を返す。
    """
    summary = {'linked': 0, 'needs_review': 0, 'skipped': 0, 'error': 0}

    # ygores 名前インデックスを取得（Supabase キャッシュ優先）
    logger.info('ygores 名前インデックスを取得中...')
    name_index = _ygores_repo.get_name_index()
    if not name_index:
        logger.error('名前インデックスが空です。ygores_blobs または API を確認してください')
        return summary

    logger.info(f'名前インデックス: {len(name_index)} 件')

    # ファジーキー索引を構築
    fuzzy_idx = _build_fuzzy_index(name_index)
    logger.info(f'ファジーキー索引: {len(fuzzy_idx)} 件')

    # 照合対象カードを取得
    cards = _fetch_approved_cards(sb)
    logger.info(f'照合対象カード: {len(cards)} 件')

    if not cards:
        logger.info('照合対象がありません。終了します')
        return summary

    for card in cards:
        card_id = card['id']
        name = card['name']
        fk = fuzzy_key(name)

        if fk not in fuzzy_idx:
            # 一致なし: まだ未発売 → スキップ
            logger.debug(f'一致なし（未発売）: {name!r}')
            summary['skipped'] += 1
            continue

        candidates = fuzzy_idx[fk]

        if len(candidates) == 1:
            # 完全一致・候補1件 -> linked
            konami_id = candidates[0]
            ok = _update_card(sb, card_id, konami_id, 'linked')
            if ok:
                logger.info(f'linked: {name!r} -> konami_id={konami_id}')
                summary['linked'] += 1
            else:
                summary['error'] += 1
        else:
            # 候補複数 -> needs_review（管理画面で要確認）
            logger.warning(
                f'needs_review（候補複数）: {name!r} -> candidates={candidates}'
            )
            ok = _update_card(sb, card_id, None, 'needs_review')
            if ok:
                summary['needs_review'] += 1
            else:
                summary['error'] += 1

    return summary


def main():
    sb = _get_supabase()
    if sb is None:
        sys.exit(1)

    logger.info('=== reconcile_unreleased 開始 ===')
    summary = reconcile(sb)
    logger.info(
        f'=== 完了 | linked: {summary["linked"]} / '
        f'needs_review: {summary["needs_review"]} / '
        f'skipped(未発売): {summary["skipped"]} / '
        f'error: {summary["error"]} ==='
    )

    # エラーがあった場合は終了コードを1にする
    if summary['error'] > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
