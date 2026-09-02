"""
ygoresources 差分同期ジョブ
============================
GitHub Actions（sync-ygores.yml、6時間毎）から実行する。

公式の利用作法に沿った差分同期:
1. 軽量エンドポイントを1回叩いて現在の X-Cache-Revision を取得
2. 保存済みリビジョンと同じなら何もせず終了
3. 異なれば /manifest/<保存済みリビジョン> で変更パス一覧を取得
4. ローカル（Supabase）に保持しているパスとの交差分だけを直列・低レートで再取得
5. 全件成功したらリビジョンを更新。ローカルに無いパスの変更は無視（必要時にオンデマンド取得）

manifest のレスポンス形式（2026-06-10 に実レスポンスで確認済み）:
    {"data": {"card": {"<id>": 1, ...}, "qa": {"<id>": 1, ...},
              "idx": {"card": {"name": {"ja": 1}}}, "meta": {...}}}
値1の葉までのキーを連結したものが /data/ 以下の変更パスに対応する。

差分規模（2026-06-15以降で最大18,613パス・card 9,354件）は 60分タイムアウト・1件/秒の
直列取得では1回で完走しない。そのため「セッション」という状態を ygores_sync_meta に持ち、
複数回の実行にまたがって同じ対象集合を安全に処理し続ける（下記「セッション方式」）。

セッション方式（保証内容）:
- セッションは (起点リビジョン saved, 取得モード mode, セッション開始時点の現在リビジョン
  start_current) の組で識別する（ygores_sync_meta.refetch_session = "saved|mode|start_current"）。
  saved・mode が前回と一致する限り同一セッションとみなし、カーソル（refetch_cursor）と
  失敗フラグ（refetch_session_failed）を引き継ぐ。一致しなければ新セッションとして両方を
  クリアする（manifestが取れた回とフォールバック回でカーソルを混同しない）
- card はカーソルで処理位置を追跡し、1回の実行では YGORES_SYNC_MAX_ITEMS 件までしか進めない。
  blob・qa は件数が少ないためカーソル対象外＝毎回全件取り直す
- セッション中に1件でも失敗した card があれば refetch_session_failed="1" を即座に永続化する
  （カーソルは失敗したidも通過するため、記録しないと次回そのidが二度と再取得されなくなる）
- card ループが対象集合の末尾まで到達した回（＝予算による打ち切りではない回）で完走とみなし、
  セッション状態（カーソル・セッション識別子・失敗フラグ）を全てクリアする。このときセッション中に
  失敗が1件でもあれば ygores_sync_meta.last_revision は更新しない（＝次回は同じ saved から
  新セッションとしてやり直す。取りこぼしを防ぐため成功分も含め先頭から再実行する）。
  失敗が無ければ last_revision には「今回観測した current」ではなく「セッション開始時点の
  start_current」を保存する。セッション実行中に current がさらに進んでいても、実際に manifest
  として検証・取得したのは saved..start_current の差分だけなので、それ以降の変更を取りこぼさない
  ようにするためである（次回 manifest/{start_current} でその先の変更が再び対象になる）
"""

import os
import sys
import logging

from ygores_repository import repository, REVISION_CHECK_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

META_KEY_REVISION = "last_revision"
META_KEY_SYNCED_AT = "last_sync_at"
META_KEY_CURSOR = "refetch_cursor"                  # 最後に処理した card の konami_id（再開用）
META_KEY_SESSION = "refetch_session"                # "{saved}|{mode}|{start_current}"
META_KEY_SESSION_FAILED = "refetch_session_failed"  # セッション中に1件でも失敗があれば "1"

# 1回の実行あたりの card 再取得件数の上限。
# 出典: docs/audit-2026-09-02.md の実測ログ — 60分のタイムアウトで3,408リクエスト
# （≒1.06秒/件）。2,500件 ≒ 44分（manifest取得等の分を差し引いた安全マージン）。
# TODO: calibrate from data（実測のリクエスト所要時間・失敗率が変われば再校正する）
DEFAULT_MAX_ITEMS = 2500
DEFAULT_BULK_WARN = 8000


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


def _parse_int_env(name: str, default: int) -> int:
    """整数の環境変数を読む。未設定なら既定値をそのまま返す。
    設定されているが整数として解釈できなければ WARNING を出し既定値にフォールバックする。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            f"[sync] 環境変数 {name}={raw!r} が不正な整数です。既定値 {default} を使用します"
        )
        return default


def _parse_cursor(raw) -> int:
    """カーソル文字列をintへ。空/Noneは『無し』(None)。非数値ならWARNINGを出し『無し』として扱う"""
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"[sync] カーソル値 {raw!r} が不正です。カーソル無しとして扱います")
        return None


def _parse_session(raw):
    """'{saved}|{mode}|{start_current}' を分解。壊れていれば None を返す"""
    if not raw:
        return None
    parts = raw.split("|")
    if len(parts) != 3:
        return None
    saved_s, mode, start_s = parts
    if mode not in ("manifest", "fallback"):
        return None
    try:
        return int(saved_s), mode, int(start_s)
    except ValueError:
        return None


def refetch_paths(repo, card_ids, blob_paths, qa_ids, cursor, max_items, session_failed) -> tuple:
    """該当キャッシュを再取得（クライアント内蔵レートリミットで直列・低レート）。

    処理順序: blob → card（カーソル再開＋予算） → qa（cardが末尾まで到達した場合のみ）。
    blob を先に処理するのは、名前索引（idx/card/name/ja、1件のみ）が新カードの可視化を
    左右するため、card予算に関係なく毎回反映させたいから。blob/qaはカーソル対象外
    （毎回全件取り直す。件数が少ないため許容）。

    card_ids は sorted(..., key=int) 済みの文字列id列であること。
    cursor が指定されていれば、そのidより大きいものから再開する（int比較）。
    max_items で card の処理件数をそこで打ち切り、50件ごとにカーソル（最後に処理したid）を
    repo.set_sync_meta で保存する。打ち切った場合は qa には進まない（次回、続きの card から
    処理するため）。
    card の再取得が1件でも失敗したら、session_failed がまだ False の場合に限り
    META_KEY_SESSION_FAILED="1" を即座に永続化する（カーソルは失敗idも通過するため、
    記録しないと次回そのidを二度と再取得しなくなる）。

    戻り値: (ok, failed, budget_exhausted, remaining_card_count, session_failed)
    """
    ok = 0
    failed = 0

    for path in blob_paths:
        if repo.fetch_and_store_blob(path):
            ok += 1
        else:
            failed += 1
            logger.warning(f"[sync] 再取得失敗: {path}")

    if cursor is not None:
        remaining_ids = [cid for cid in card_ids if int(cid) > cursor]
    else:
        remaining_ids = card_ids

    processed = 0
    last_cursor = cursor
    budget_exhausted = False
    card_failed = 0
    for cid in remaining_ids:
        if processed >= max_items:
            budget_exhausted = True
            break
        if repo.fetch_and_store_card(int(cid)):
            ok += 1
        else:
            failed += 1
            card_failed += 1
            if not session_failed:
                session_failed = True
                repo.set_sync_meta(META_KEY_SESSION_FAILED, "1")
            logger.warning(f"[sync] 再取得失敗: card/{cid}")
        last_cursor = cid
        processed += 1
        if processed % 50 == 0:
            repo.set_sync_meta(META_KEY_CURSOR, str(last_cursor))

    remaining_count = len(remaining_ids) - processed

    if budget_exhausted:
        # 打ち切り位置を必ず保存する（50件区切りに満たない端数があっても取りこぼさないため）
        repo.set_sync_meta(META_KEY_CURSOR, str(last_cursor) if last_cursor is not None else "")
        logger.warning(
            f"[sync] 予算打ち切り: 処理{processed}件 / 失敗{card_failed}件 / 残り{remaining_count}件"
        )
        return ok, failed, True, remaining_count, session_failed

    for qid in qa_ids:
        if repo.fetch_and_store_qa(int(qid)):
            ok += 1
        else:
            failed += 1
            logger.warning(f"[sync] 再取得失敗: qa/{qid}")

    return ok, failed, False, 0, session_failed


def run_sync(repo=repository) -> int:
    """差分同期を1回実行。終了コード: 0=成功（変更なし・予算到達含む）、1=失敗"""
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

    # 環境変数（予算・警告閾値）の読み込みと検証
    max_items = _parse_int_env("YGORES_SYNC_MAX_ITEMS", DEFAULT_MAX_ITEMS)
    if max_items <= 0:
        logger.error(f"[sync] YGORES_SYNC_MAX_ITEMS は正の整数である必要があります（値: {max_items}）")
        return 1
    bulk_warn = _parse_int_env("YGORES_SYNC_BULK_WARN", DEFAULT_BULK_WARN)

    held_cards = set(str(i) for i in repo.cached_card_ids())
    held_blobs = set(repo.cached_blob_paths())
    held_qa = set(str(i) for i in repo.cached_qa_ids())

    # 3. 変更パス一覧を取得（mode を決定）
    manifest = repo.client.get_json(f"manifest/{saved}", timeout=30)
    if manifest is None:
        # リビジョン飛び（保存値が古すぎる等）またはmanifest障害。
        # フォールバック: 保持している全パスを再取得して整合を回復する
        mode = "fallback"
        logger.warning(
            f"[sync] manifest/{saved} が取得できません（リビジョン飛びの可能性）— "
            f"保持パス全件({len(held_cards) + len(held_blobs) + len(held_qa)}件)を再取得します"
        )
        target_cards = sorted(held_cards, key=int)
        target_blobs = sorted(held_blobs)
        target_qa = sorted(held_qa, key=int)
    else:
        mode = "manifest"
        changed = flatten_manifest(manifest)
        logger.info(f"[sync] 変更パス: {len(changed)}件（リビジョン {saved} → {current}）")

        # 4. ローカル保持分との交差のみ再取得
        target_cards = sorted(
            (p.split("/", 1)[1] for p in changed
             if p.startswith("card/") and p.split("/", 1)[1] in held_cards),
            key=int,
        )
        target_qa = sorted(
            (p.split("/", 1)[1] for p in changed
             if p.startswith("qa/") and p.split("/", 1)[1] in held_qa),
            key=int,
        )
        target_blobs = sorted(p for p in changed if p in held_blobs)
        logger.info(
            f"[sync] 再取得対象: card {len(target_cards)}件 / "
            f"blob {len(target_blobs)}件 / qa {len(target_qa)}件（保持外の変更は無視）"
        )

    # セッション判定: 既存セッションの saved・mode が今回と一致すれば再開、しなければ新セッション
    # （manifestが取れた回とフォールバック回でカーソルを混同しないようにする）
    existing_session = _parse_session(repo.get_sync_meta(META_KEY_SESSION))
    if existing_session is not None and existing_session[0] == saved and existing_session[1] == mode:
        start_current = existing_session[2]
        cursor = _parse_cursor(repo.get_sync_meta(META_KEY_CURSOR))
        session_failed = repo.get_sync_meta(META_KEY_SESSION_FAILED) == "1"
        logger.info(f"[sync] セッション再開: 起点={saved} mode={mode} start_current={start_current}")
    else:
        start_current = current
        cursor = None
        session_failed = False
        repo.set_sync_meta(META_KEY_SESSION, f"{saved}|{mode}|{start_current}")
        repo.set_sync_meta(META_KEY_CURSOR, "")
        repo.set_sync_meta(META_KEY_SESSION_FAILED, "")
        logger.info(f"[sync] 新セッション開始: 起点={saved} mode={mode} start_current={start_current}")

    logger.info(
        f"[sync] カーソル: {('有(開始id>' + str(cursor) + ')') if cursor is not None else '無'}"
        f"・予算 {max_items}件/回"
    )

    # 一括投入WARNING: 残り件数（カーソルより先の card ＋ blob ＋ qa）が閾値超のとき
    remaining_cards_preview = [c for c in target_cards if cursor is None or int(c) > cursor]
    total_remaining = len(remaining_cards_preview) + len(target_blobs) + len(target_qa)
    if total_remaining > bulk_warn:
        logger.warning(
            f"[sync] 差分が大きすぎます（残り{total_remaining}件 > {bulk_warn}）。"
            f"公開ミラーからの一括投入 import-ygores.yml の実行を推奨します"
        )

    ok, failed, budget_exhausted, remaining, session_failed = refetch_paths(
        repo, target_cards, target_blobs, target_qa,
        cursor=cursor, max_items=max_items, session_failed=session_failed,
    )

    # 5. 予算到達: カーソル保存済み・リビジョン未更新・セッションは維持（次回続きから再開。失敗ではない）
    if budget_exhausted:
        return 0

    # 6. card ループが末尾まで到達（完走）→ セッション状態を全てクリアする
    repo.set_sync_meta(META_KEY_CURSOR, "")
    repo.set_sync_meta(META_KEY_SESSION, "")
    repo.set_sync_meta(META_KEY_SESSION_FAILED, "")

    if session_failed or failed > 0:
        logger.error(
            "[sync] セッション中に失敗が残りました — リビジョンを更新せず、"
            "次回は同じ起点から新セッションとしてやり直します"
        )
        return 1

    # start_current（セッション開始時点の現在リビジョン）を保存する。今回の current ではない理由:
    # セッション実行中にさらに current が進んでいても、実際に manifest として検証・取得したのは
    # saved..start_current の差分だけなので、それ以降の変更は次回 manifest/{start_current} で
    # 改めて対象になるようにし、取りこぼしを防ぐ
    repo.set_sync_meta(META_KEY_REVISION, str(start_current))
    repo.set_sync_meta(META_KEY_SYNCED_AT, repo._now_iso())
    logger.info(f"[sync] 完了: {ok}件更新、リビジョン {start_current} を保存")
    return 0


if __name__ == "__main__":
    sys.exit(run_sync())
