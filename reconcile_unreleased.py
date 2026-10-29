"""
reconcile_unreleased.py -- 未発売カード発売済み自動照合ジョブ

対象: unreleased_cards の status='approved' かつ konami_id IS NULL のレコード
処理: ygoresources の日本語カード名インデックス（カード名→konami_id リスト）と照合し、
      発売が確認できたカードを自動的に status='linked' へ更新する。

照合ロジック（status は approved のまま。公開サイトから消さない）:
  (a) ファジーキー完全一致 + 候補1件 -> konami_id 設定 + status='linked'
  (b) ファジーキー完全一致 + 候補複数 -> review_candidates に reason=multiple_exact を記録
  (c) 一致なし・発売日が未来/NULL/7日以内 -> スキップ（まだ未発売、または ygores 登録待ち）
  (d) 一致なし・発売日が7日以上前         -> 画像AIの1字誤読を疑い、ファジーキーが
                                            ちょうど1文字だけ違う正式名を探す
        見つかる   -> review_candidates に reason=near_match（自動 linked はしない）
        見つからず -> review_candidates に reason=release_passed_no_match
  review_candidates(JSONB) = {reason, candidates(上位最大10件), total_candidates,
                              all_candidate_ids(切り詰め前の全候補ID・確認済み判定用),
                              checked_at, dismissed_at}
  管理画面の「公開中」タブに「要確認」と表示され、人が紐付け or「確認済み（まだ未発売）」を選ぶ。
  dismissed_at がある（確認済み）カードは、dismissed_signature（理由＋候補ID）と今回の結果が
  同じ（理由が同じで、候補IDが signature の部分集合）ときだけ上書きしない。
  理由が変わる／signature に無い候補が増えたら確認済みを外して印を付け直す。
  完全一致1件の linked 化は dismissed でも行う。
  一致なしで猶予期間内／未来日／NULL に戻ったカードに古い印（未確認）が残っていれば NULL に戻す。
  全 UPDATE は status='approved' を条件に付け、同時実行で状態を壊さない。

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


def _fetch_approved_cards(sb) -> list[dict] | None:
    """
    status='approved' かつ konami_id IS NULL の unreleased_cards を取得する。
    取得に失敗した場合は None を返す（0件と区別し、呼び出し側で終了コード1にする）。
    """
    try:
        resp = (
            sb.table('unreleased_cards')
            .select('id, name, release_date, review_candidates')
            .eq('status', 'approved')
            .is_('konami_id', 'null')
            .execute()
        )
        return resp.data or []
    except Exception as e:
        logger.error(f'unreleased_cards 取得失敗: {e}')
        return None


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
            try:
                pair = (name, int(cid))
            except (TypeError, ValueError):
                logger.warning(f'konami_id が整数でないため読み飛ばし: {name!r} -> {cid!r}')
                continue
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
    """fk と1字だけ違う正式名を、切り詰め前の全件 {konami_id, name}（konami_id 昇順・重複除去）で返す。
    表示用の上位10件への切り詰めは _make_review が行う。"""
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
    return found


# 発売日からこの日数が経つまでは近似判定しない（ygores への登録遅れで、発売直後のカードが
# 要確認扱いになる／似た名前の別カードを誤検知するのを防ぐ猶予期間）
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
    """review_candidates に保存する辞書を作る。candidates は全候補を渡す。
    表示用 candidates は上位 MAX_NEAR_CANDIDATES 件に切り詰め、確認済み判定用に
    切り詰め前の全候補IDを all_candidate_ids（ソート済み）として保存する。"""
    return {
        'reason': reason,
        'candidates': candidates[:MAX_NEAR_CANDIDATES],
        'total_candidates': len(candidates),
        'all_candidate_ids': sorted(c['konami_id'] for c in candidates),
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'dismissed_at': None,
    }


def _update_card(
    sb,
    card_id: int,
    konami_id: str | None,
    new_status: str,
    review_candidates: dict | None = None,
) -> str:
    """unreleased_cards の1件を更新する。戻り値: 'ok' / 'conflict'(status が approved でなくなっていた) / 'error'。
    UPDATE は必ず status='approved' を条件に付ける（同時実行で管理画面の操作結果を上書きしない）。
    status='linked' に更新する場合は、7日後クリーンアップの起点となる
    linked_at も併せて記録し、review_candidates は NULL にクリアする。
    status を変えず印だけ付ける場合は new_status='approved' と review_candidates を渡す。"""
    try:
        row: dict = {'status': new_status}
        if konami_id is not None:
            row['konami_id'] = int(konami_id)
        if new_status == 'linked':
            row['linked_at'] = datetime.now(timezone.utc).isoformat()
            row['review_candidates'] = None
        elif review_candidates is not None:
            row['review_candidates'] = review_candidates
        resp = (
            sb.table('unreleased_cards').update(row)
            .eq('id', card_id).eq('status', 'approved').execute()
        )
        if not resp.data:
            logger.warning(f'更新対象なし（状態が変わった可能性）id={card_id}')
            return 'conflict'
        return 'ok'
    except Exception as e:
        logger.warning(f'更新失敗 id={card_id}: {e}')
        return 'error'


def _is_dismissed(card: dict) -> bool:
    """管理者が「確認済み（まだ未発売）」にしたカードなら True。"""
    rc = card.get('review_candidates')
    return bool(isinstance(rc, dict) and rc.get('dismissed_at'))


def _dismissal_still_valid(card: dict, reason: str, candidates: list[dict]) -> bool:
    """確認済み時点の dismissed_signature と今回の結果を比べ、確認済みを維持してよいなら True。
    理由が同じで、今回の候補IDがすべて signature に含まれるときだけ維持する
    （理由が変わった／signature に無い候補が増えたら、再確認が必要）。
    signature が無い（旧データ）場合は維持しない。"""
    rc = card.get('review_candidates') or {}
    sig = rc.get('dismissed_signature')
    if not isinstance(sig, dict) or sig.get('reason') != reason:
        return False
    known = set(sig.get('candidate_ids') or [])
    return {c['konami_id'] for c in candidates} <= known


def _clear_review(sb, card_id: int) -> str:
    """古い review_candidates を NULL に戻す。戻り値は _update_card と同じ。status='approved' 条件付き。"""
    try:
        resp = (
            sb.table('unreleased_cards').update({'review_candidates': None})
            .eq('id', card_id).eq('status', 'approved').execute()
        )
        return 'ok' if resp.data else 'conflict'
    except Exception as e:
        logger.warning(f'印のクリア失敗 id={card_id}: {e}')
        return 'error'


def reconcile(sb) -> dict[str, int]:
    """
    照合処理本体。サマリ辞書を返す。
      linked / review_marked(印付け合計) / multiple_exact / near_match /
      release_passed_no_match / dismissed_skipped / review_cleared / skipped / conflict / error
    """
    summary = {
        'linked': 0, 'review_marked': 0,
        'multiple_exact': 0, 'near_match': 0, 'release_passed_no_match': 0,
        'dismissed_skipped': 0, 'review_cleared': 0,
        'skipped': 0, 'conflict': 0, 'error': 0,
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
    if cards is None:
        # 取得失敗。0件（正常）と区別して error に数え、main で終了コード1にする
        summary['error'] += 1
        return summary
    logger.info(f'照合対象カード: {len(cards)} 件')

    if not cards:
        logger.info('照合対象がありません。終了します')
        return summary

    def _mark(card, reason, cands):
        """status を変えず review_candidates だけ記録する。"""
        if _is_dismissed(card):
            if _dismissal_still_valid(card, reason, cands):
                logger.info(f'確認済みのためスキップ: {card["name"]!r} ({reason})')
                summary['dismissed_skipped'] += 1
                return
            logger.warning(f'確認済みを外して再度印を付ける（理由/候補が変化）: {card["name"]!r}')
        result = _update_card(sb, card['id'], None, 'approved', _make_review(reason, cands))
        if result == 'ok':
            summary['review_marked'] += 1
            summary[reason] += 1
        elif result == 'conflict':
            summary['conflict'] += 1
        else:
            summary['error'] += 1

    for card in cards:
        card_id = card['id']
        name = card['name']
        fk = fuzzy_key(name)

        if fk not in fuzzy_idx:
            if not _is_release_passed(card.get('release_date')):
                # 一致なし・発売日が未来/NULL/猶予期間内: まだ未発売 → スキップ
                # （似た名前の別の未発売カードを誤検知しないため近似判定はしない）
                logger.debug(f'一致なし（未発売）: {name!r}')
                summary['skipped'] += 1
                # 以前付けた未確認の印が残っていれば消す（編集や発売日の変更で前提が変わった可能性）
                if isinstance(card.get('review_candidates'), dict) and not _is_dismissed(card):
                    result = _clear_review(sb, card_id)
                    if result == 'ok':
                        summary['review_cleared'] += 1
                    elif result == 'conflict':
                        summary['conflict'] += 1
                    else:
                        summary['error'] += 1
                continue

            # 一致なし・発売日が7日以上前: 画像AIの1字誤読を疑う（自動 linked はしない）
            near = _find_near_candidates(fk, names_idx, length_idx)
            if near:
                logger.warning(f'要確認（1字違い）: {name!r} -> {near}')
                _mark(card, 'near_match', near)
            else:
                logger.warning(f'要確認（発売日経過・一致なし）: {name!r}')
                _mark(card, 'release_passed_no_match', [])
            continue

        candidates = fuzzy_idx[fk]

        if len(candidates) == 1:
            # 完全一致・候補1件 -> linked（確認済みでも発売済みと確定したので linked にする）
            konami_id = candidates[0]
            result = _update_card(sb, card_id, konami_id, 'linked')
            if result == 'ok':
                logger.info(f'linked: {name!r} -> konami_id={konami_id}')
                summary['linked'] += 1
            elif result == 'conflict':
                summary['conflict'] += 1
            else:
                summary['error'] += 1
        else:
            # 候補複数 -> 要確認の印だけ付ける（公開は維持）
            logger.warning(f'要確認（候補複数）: {name!r} -> candidates={candidates}')
            allc = [{'konami_id': cid, 'name': n} for n, cid in names_idx.get(fk, [])]
            _mark(card, 'multiple_exact', allc)

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
    # 退避した元画像も消す（カード行を消す前に extraction_raw を読む必要があるため、ここで収集する）
    from unreleased_image_store import collect_original_paths
    storage_paths += collect_original_paths(sb, card_ids)
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
            .select('id, name, release_date, review_candidates')
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

    today = datetime.now(JST).date()
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
        f'要確認の印付け: {summary["review_marked"]} '
        f'(multiple_exact: {summary["multiple_exact"]} / '
        f'near_match: {summary["near_match"]} / '
        f'release_passed_no_match: {summary["release_passed_no_match"]}) / '
        f'確認済みスキップ: {summary["dismissed_skipped"]} / '
        f'古い印の解除: {summary["review_cleared"]} / '
        f'競合: {summary["conflict"]} / '
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
