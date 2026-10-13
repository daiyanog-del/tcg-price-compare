"""
reconcile_unreleased.py -- 未発売カード発売済み自動照合ジョブ

対象: unreleased_cards の status='approved' かつ konami_id IS NULL のレコード
処理: ygoresources の日本語カード名インデックス（カード名→konami_id リスト）と照合し、
      発売が確認できたカードを自動的に status='linked' へ更新する。

照合ロジック:
  (a) ファジーキー完全一致 + 候補1件 -> konami_id 設定 + status='linked'
  (b) ファジーキー完全一致 + 候補複数 -> status='needs_review'（管理画面で要確認）
  (c) 一致なし・発売日が未来/NULL        -> スキップ（まだ未発売）
  (d) 一致なし・発売日が7日以上前            -> 画像AIの1字誤読を疑い、ファジーキーが
                                            ちょうど1文字だけ違う正式名を探す
        見つかる   -> reason=near_match で needs_review（自動 linked はしない）
        見つからず -> reason=release_passed_no_match で needs_review
  needs_review にした理由と候補は review_candidates(JSONB) に保存し、
  管理画面の「承認待ち」タブで人が紐付ける。

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
from datetime import date, datetime, timedelta, timezone

from constants import JST
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
            .select('id, name, release_date')
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


# near_match の候補数上限（短い名前だと1字違いが大量に出るため、画面が埋まらないようにする）
# TODO: calibrate from data
MAX_NEAR_CANDIDATES = 10


def _build_fuzzy_names_index(name_index: dict) -> dict[str, list[tuple[str, int]]]:
    """
    ygores の名前インデックスから ファジーキー -> [(正式名, konami_id)] の辞書を構築する。
    管理画面に候補の正式名を出すために使う。同一 (正式名, konami_id) は重複除去する。
    """
    idx: dict[str, list[tuple[str, int]]] = {}
    for name, ids in name_index.items():
        fk = fuzzy_key(name)
        id_list = ids if isinstance(ids, list) else [ids]
        bucket = idx.setdefault(fk, [])
        for cid in id_list:
            pair = (name, int(cid))
            if pair not in bucket:
                bucket.append(pair)
    return idx


def _build_length_index(names_idx: dict[str, list[tuple[str, int]]]) -> dict[int, list[str]]:
    """ファジーキーの文字数 -> そのキー一覧。1字違い探索を同じ長さのキーだけに絞るため。"""
    by_len: dict[int, list[str]] = {}
    for fk in names_idx:
        by_len.setdefault(len(fk), []).append(fk)
    return by_len


def _is_one_char_diff(a: str, b: str) -> bool:
    """同じ長さで、ちょうど1文字だけ違うなら True（完全一致・長さ違いは False）。"""
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a, b):
        if x != y:
            diff += 1
            if diff > 1:
                return False
    return diff == 1


def _find_near_candidates(
    fk: str,
    names_idx: dict[str, list[tuple[str, int]]],
    length_idx: dict[int, list[str]],
) -> list[dict]:
    """fk と1字だけ違う正式名を {konami_id, name} のリストで返す（konami_id 重複除去・上限あり）。"""
    found: list[dict] = []
    seen: set[int] = set()
    for other in length_idx.get(len(fk), []):
        if not _is_one_char_diff(fk, other):
            continue
        for name, cid in names_idx[other]:
            if cid in seen:
                continue
            seen.add(cid)
            found.append({'konami_id': cid, 'name': name})
    found.sort(key=lambda c: c['konami_id'])
    return found[:MAX_NEAR_CANDIDATES]


# 発売日からこの日数が経つまでは近似判定しない（ygores への登録遅れで、発売直後のカードが
# needs_review に回って公開から消える／似た名前の別カードを誤検知するのを防ぐ猶予期間）
RELEASE_GRACE_DAYS = 7  # TODO: calibrate from data


def _is_release_passed(release_date) -> bool:
    """release_date が今日(JST)より RELEASE_GRACE_DAYS 日以上前なら True。NULL・不正値は False。"""
    if not release_date:
        return False
    try:
        limit = datetime.now(JST).date() - timedelta(days=RELEASE_GRACE_DAYS)
        return date.fromisoformat(str(release_date)[:10]) <= limit
    except (TypeError, ValueError):
        return False


def _make_review(reason: str, candidates: list[dict]) -> dict:
    """review_candidates に保存する辞書を作る。"""
    return {
        'reason': reason,
        'candidates': candidates,
        'checked_at': datetime.now(timezone.utc).isoformat(),
    }


def _update_card(
    sb,
    card_id: int,
    konami_id: str | None,
    new_status: str,
    review_candidates: dict | None = None,
) -> bool:
    """unreleased_cards の1件を更新する。
    status='linked' に更新する場合は、7日後クリーンアップの起点となる
    linked_at も併せて記録し、review_candidates は NULL にクリアする。
    status='needs_review' の場合は review_candidates（理由と候補）を保存する。"""
    try:
        row: dict = {'status': new_status}
        if konami_id is not None:
            row['konami_id'] = int(konami_id)
        if new_status == 'linked':
            row['linked_at'] = datetime.now(timezone.utc).isoformat()
            row['review_candidates'] = None
        elif review_candidates is not None:
            row['review_candidates'] = review_candidates
        sb.table('unreleased_cards').update(row).eq('id', card_id).execute()
        return True
    except Exception as e:
        logger.warning(f'更新失敗 id={card_id}: {e}')
        return False


def reconcile(sb) -> dict[str, int]:
    """
    照合処理本体。サマリ辞書
    {linked, needs_review, near_match, release_passed_no_match, skipped, error} を返す。
    needs_review は multiple_exact / near_match / release_passed_no_match の合計。
    """
    summary = {
        'linked': 0, 'needs_review': 0,
        'near_match': 0, 'release_passed_no_match': 0,
        'skipped': 0, 'error': 0,
    }

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
    names_idx = _build_fuzzy_names_index(name_index)
    length_idx = _build_length_index(names_idx)

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
            if not _is_release_passed(card.get('release_date')):
                # 一致なし・発売日が未来/NULL: まだ未発売 → スキップ
                # （似た名前の別の未発売カードを誤検知しないため近似判定はしない）
                logger.debug(f'一致なし（未発売）: {name!r}')
                summary['skipped'] += 1
                continue

            # 一致なし・発売日が過去: 画像AIの1字誤読を疑う（自動 linked はしない）
            near = _find_near_candidates(fk, names_idx, length_idx)
            if near:
                reason = 'near_match'
                logger.warning(f'needs_review（1字違い）: {name!r} -> {near}')
            else:
                reason = 'release_passed_no_match'
                logger.warning(f'needs_review（発売日経過・一致なし）: {name!r}')
            ok = _update_card(sb, card_id, None, 'needs_review', _make_review(reason, near))
            if ok:
                summary['needs_review'] += 1
                summary[reason] += 1
            else:
                summary['error'] += 1
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
            review = _make_review(
                'multiple_exact',
                [{'konami_id': cid, 'name': n} for n, cid in names_idx.get(fk, [])],
            )
            ok = _update_card(sb, card_id, None, 'needs_review', review)
            if ok:
                summary['needs_review'] += 1
            else:
                summary['error'] += 1

    return summary


# ──────────────────────────────────────────────
# 不要になったレコードの自動削除
#   1) status='linked' かつ linked_at が7日以上前 -> 物理削除
#   2) status='rejected' かつ (ygoresと一致 or release_dateが過去) -> 物理削除
# ──────────────────────────────────────────────

def _delete_images_for_cards(sb, card_ids: list[int]) -> None:
    """
    指定した unreleased_cards に紐づく official_card_images を
    Supabase Storage（バケット official-card-images）から削除する。

    unreleased_cards -> official_card_images は ON DELETE CASCADE のため
    DB側の行は unreleased_cards 削除時に自動で消えるが、Storage の実ファイルは
    CASCADEでは消えないため、ここで明示的に削除する。
    """
    if not card_ids:
        return
    resp = (
        sb.table('official_card_images')
        .select('storage_path')
        .in_('unreleased_card_id', card_ids)
        .execute()
    )
    rows = resp.data or []
    storage_paths = [r['storage_path'] for r in rows if r.get('storage_path')]
    if storage_paths:
        sb.storage.from_('official-card-images').remove(storage_paths)


def _delete_card(sb, card_id: int) -> None:
    """unreleased_cards の1件を物理削除する。"""
    sb.table('unreleased_cards').delete().eq('id', card_id).execute()


def _fetch_linked_stale_cards(sb) -> list[dict]:
    """status='linked' かつ linked_at が7日以上前のレコードを取得する。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    try:
        resp = (
            sb.table('unreleased_cards')
            .select('id, name')
            .eq('status', 'linked')
            .lte('linked_at', cutoff)
            .execute()
        )
        return resp.data or []
    except Exception as e:
        logger.error(f'linked クリーンアップ対象取得失敗: {e}')
        return []


def _fetch_rejected_cards(sb) -> list[dict]:
    """status='rejected' のレコードを取得する。"""
    try:
        resp = (
            sb.table('unreleased_cards')
            .select('id, name, release_date')
            .eq('status', 'rejected')
            .execute()
        )
        return resp.data or []
    except Exception as e:
        logger.error(f'rejected クリーンアップ対象取得失敗: {e}')
        return []


def _cleanup_stale_cards(sb) -> dict[str, int]:
    """
    不要になった unreleased_cards レコードを自動削除する。
    サマリ辞書 {linked_deleted, rejected_deleted, error} を返す。
    """
    summary = {'linked_deleted': 0, 'rejected_deleted': 0, 'error': 0}

    # --- 1) 「公開中」のうち linked のクリーンアップ ---
    linked_cards = _fetch_linked_stale_cards(sb)
    logger.info(f'linked クリーンアップ対象: {len(linked_cards)} 件')
    for card in linked_cards:
        card_id = card['id']
        name = card.get('name')
        try:
            _delete_images_for_cards(sb, [card_id])
            _delete_card(sb, card_id)
            logger.info(f'linked 削除: id={card_id} {name!r}')
            summary['linked_deleted'] += 1
        except Exception as e:
            logger.warning(f'linked 削除失敗（画像またはDB削除）id={card_id}: {e}')
            summary['error'] += 1

    # --- 2) 「却下済み」(rejected) のクリーンアップ ---
    logger.info('ygores 名前インデックスを取得中（rejected クリーンアップ用）...')
    name_index = _ygores_repo.get_name_index()
    fuzzy_idx = _build_fuzzy_index(name_index) if name_index else {}

    rejected_cards = _fetch_rejected_cards(sb)
    logger.info(f'rejected クリーンアップ候補: {len(rejected_cards)} 件')

    today = date.today()
    for card in rejected_cards:
        card_id = card['id']
        name = card.get('name') or ''
        release_date = card.get('release_date')

        matched_ygores = fuzzy_key(name) in fuzzy_idx

        is_past = False
        if release_date:
            try:
                is_past = date.fromisoformat(str(release_date)[:10]) < today
            except (TypeError, ValueError):
                is_past = False

        if not (matched_ygores or is_past):
            continue

        try:
            _delete_images_for_cards(sb, [card_id])
            _delete_card(sb, card_id)
            reason = 'ygores一致' if matched_ygores else 'release_date過去'
            logger.info(f'rejected 削除（{reason}）: id={card_id} {name!r}')
            summary['rejected_deleted'] += 1
        except Exception as e:
            logger.warning(f'rejected 削除失敗（画像またはDB削除）id={card_id}: {e}')
            summary['error'] += 1

    return summary


def main():
    sb = _get_supabase()
    if sb is None:
        sys.exit(1)

    logger.info('=== reconcile_unreleased 開始 ===')
    summary = reconcile(sb)
    logger.info(
        f'=== 照合完了 | linked: {summary["linked"]} / '
        f'needs_review: {summary["needs_review"]} '
        f'(near_match: {summary["near_match"]} / '
        f'release_passed_no_match: {summary["release_passed_no_match"]}) / '
        f'skipped(未発売): {summary["skipped"]} / '
        f'error: {summary["error"]} ==='
    )

    cleanup_summary = _cleanup_stale_cards(sb)
    logger.info(
        f'=== クリーンアップ完了 | linked_deleted: {cleanup_summary["linked_deleted"]} / '
        f'rejected_deleted: {cleanup_summary["rejected_deleted"]} / '
        f'error: {cleanup_summary["error"]} ==='
    )

    # エラーがあった場合は終了コードを1にする
    if summary['error'] > 0 or cleanup_summary['error'] > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
