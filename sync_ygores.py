"""
ygoresources 差分同期ジョブ
============================
GitHub Actions（sync-ygores.yml、6時間毎）から実行する。

公式の利用作法に沿った差分同期:
1. 軽量エンドポイントを1回叩いて現在の X-Cache-Revision を取得
2. 保存済みリビジョンと同じなら何もせず終了
3. 異なれば /manifest/<保存済みリビジョン> で変更パス一覧を取得
4. ローカル（Supabase）に保持しているパスとの交差分だけを直列・低レートで再取得
5. リビジョンを更新。ローカルに無いパスの変更は無視（必要時にオンデマンド取得）

manifest のレスポンス形式（2026-06-10 に実レスポンスで確認済み）:
    {"data": {"card": {"<id>": 1, ...}, "qa": {"<id>": 1, ...},
              "idx": {"card": {"name": {"ja": 1}}}, "meta": {...}}}
値1の葉までのキーを連結したものが /data/ 以下の変更パスに対応する。
"""

import sys
import logging

from ygores_repository import repository, REVISION_CHECK_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

META_KEY_REVISION = "last_revision"
META_KEY_SYNCED_AT = "last_sync_at"


def flatten_manifest(tree: dict) -> set:
    """manifest の入れ子ツリーを 'card/4007' のようなパス集合に平坦化する"""
    paths = set()

    def walk(node, prefix):
        if isinstance(node, dict):
            for key, child in node.items():
                walk(child, f"{prefix}/{key}" if prefix else str(key))
        else:
            # 葉（値は変更マーカー）。ここまでのキー列が変更パス
            if prefix:
                paths.add(prefix)

    walk(tree.get("data", {}), "")
    return paths


def refetch_paths(repo, card_ids, blob_paths, qa_ids) -> tuple:
    """該当キャッシュを再取得（クライアント内蔵レートリミットで直列・低レート）"""
    ok = 0
    failed = 0
    for cid in card_ids:
        if repo.fetch_and_store_card(int(cid)):
            ok += 1
        else:
            failed += 1
            logger.warning(f"[sync] 再取得失敗: card/{cid}")
    for path in blob_paths:
        if repo.fetch_and_store_blob(path):
            ok += 1
        else:
            failed += 1
            logger.warning(f"[sync] 再取得失敗: {path}")
    for qid in qa_ids:
        if repo.fetch_and_store_qa(int(qid)):
            ok += 1
        else:
            failed += 1
            logger.warning(f"[sync] 再取得失敗: qa/{qid}")
    return ok, failed


def run_sync(repo=repository) -> int:
    """差分同期を1回実行。終了コード: 0=成功（変更なし含む）、1=失敗"""
    if repo._supabase() is None:
        logger.error("[sync] Supabase未接続のため同期できません（SUPABASE_URL/KEYを確認）")
        return 1

    # 1. 現在のリビジョンを確認（軽量エンドポイントを1回だけ取得）
    probe = repo.client.get_json(REVISION_CHECK_PATH, timeout=15)
    current = repo.client.last_revision
    if probe is None or current is None:
        logger.error("[sync] 現在リビジョンの取得に失敗（API障害の可能性）— 同期を中止")
        return 1
    logger.info(f"[sync] 現在リビジョン: {current}")

    # 2. 保存済みリビジョンと比較
    saved = repo.get_sync_meta(META_KEY_REVISION)
    if saved is None:
        # 初回実行: 差分の起点が無いので現在値を保存して終了
        # （キャッシュはオンデマンド取得とダンプimportで蓄積される）
        repo.set_sync_meta(META_KEY_REVISION, current)
        repo.set_sync_meta(META_KEY_SYNCED_AT, repo._now_iso())
        logger.info(f"[sync] 初回実行: リビジョン {current} を保存して終了")
        return 0
    saved = int(saved)
    if saved == current:
        logger.info("[sync] リビジョン不変 — 何もしません")
        repo.set_sync_meta(META_KEY_SYNCED_AT, repo._now_iso())
        return 0

    held_cards = set(str(i) for i in repo.cached_card_ids())
    held_blobs = set(repo.cached_blob_paths())
    held_qa = set(str(i) for i in repo.cached_qa_ids())

    # 3. 変更パス一覧を取得
    manifest = repo.client.get_json(f"manifest/{saved}", timeout=30)
    if manifest is None:
        # リビジョン飛び（保存値が古すぎる等）またはmanifest障害。
        # フォールバック: 保持している全パスを再取得して整合を回復する
        logger.warning(
            f"[sync] manifest/{saved} が取得できません（リビジョン飛びの可能性）— "
            f"保持パス全件({len(held_cards) + len(held_blobs) + len(held_qa)}件)を再取得します"
        )
        ok, failed = refetch_paths(repo, sorted(held_cards), sorted(held_blobs), sorted(held_qa))
    else:
        changed = flatten_manifest(manifest)
        logger.info(f"[sync] 変更パス: {len(changed)}件（リビジョン {saved} → {current}）")

        # 4. ローカル保持分との交差のみ再取得
        target_cards = sorted(p.split("/", 1)[1] for p in changed
                              if p.startswith("card/") and p.split("/", 1)[1] in held_cards)
        target_qa = sorted(p.split("/", 1)[1] for p in changed
                           if p.startswith("qa/") and p.split("/", 1)[1] in held_qa)
        target_blobs = sorted(p for p in changed if p in held_blobs)
        total = len(target_cards) + len(target_blobs) + len(target_qa)
        logger.info(
            f"[sync] 再取得対象: card {len(target_cards)}件 / "
            f"blob {len(target_blobs)}件 / qa {len(target_qa)}件（保持外の変更は無視）"
        )
        if total == 0:
            ok, failed = 0, 0
        else:
            ok, failed = refetch_paths(repo, target_cards, target_blobs, target_qa)

    # 5. リビジョン更新（再取得に失敗が残った場合は次回リトライさせるため更新しない）
    if failed == 0:
        repo.set_sync_meta(META_KEY_REVISION, current)
        repo.set_sync_meta(META_KEY_SYNCED_AT, repo._now_iso())
        logger.info(f"[sync] 完了: {ok}件更新、リビジョン {current} を保存")
        return 0
    logger.error(f"[sync] {failed}件の再取得に失敗 — リビジョンを更新せず次回再試行します")
    return 1


if __name__ == "__main__":
    sys.exit(run_sync())
