"""
purge_images.py — 画像出所ドメイン単位の一括削除 CLI

使い方:
  python purge_images.py --list
      source_domain ごとの件数一覧を表示する。

  python purge_images.py --domain yu-gi-oh.jp
      第1段階: 該当 source_domain の全行を hidden=true に更新（即時非表示）。

  python purge_images.py --domain yu-gi-oh.jp --delete
      第2段階込み: hidden=true ＋ Storage物理削除 ＋ deleted_at 記録。

オプション:
  --yes   確認プロンプトをスキップする。

環境変数:
  SUPABASE_URL   Supabase プロジェクト URL
  SUPABASE_KEY   Supabase サービスロール or アノンキー
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse


def _get_supabase():
    """Supabase クライアントを生成して返す。環境変数未設定時は終了する。"""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        print("[エラー] 環境変数 SUPABASE_URL と SUPABASE_KEY を設定してください。")
        sys.exit(1)
    try:
        from supabase import create_client
        return create_client(url, key)
    except ImportError:
        print("[エラー] supabase パッケージがインストールされていません。")
        print("  pip install supabase")
        sys.exit(1)
    except Exception as e:
        print(f"[エラー] Supabase 接続失敗: {e}")
        sys.exit(1)


def cmd_list(supabase) -> None:
    """source_domain ごとの件数一覧を表示する。"""
    try:
        resp = (
            supabase.table("official_card_images")
            .select("source_domain, hidden")
            .is_("deleted_at", "null")
            .execute()
        )
        rows = resp.data or []
    except Exception as e:
        print(f"[エラー] データ取得失敗: {e}")
        sys.exit(1)

    if not rows:
        print("official_card_images に未削除の行はありません。")
        return

    # ドメインごとに集計
    stats: dict[str, dict] = {}
    for row in rows:
        domain = row["source_domain"] or "(不明)"
        if domain not in stats:
            stats[domain] = {"total": 0, "hidden": 0, "visible": 0}
        stats[domain]["total"] += 1
        if row["hidden"]:
            stats[domain]["hidden"] += 1
        else:
            stats[domain]["visible"] += 1

    # 表示（total 降順）
    sorted_domains = sorted(stats.items(), key=lambda x: x[1]["total"], reverse=True)
    print(f"{'ドメイン':<40} {'全件':>6} {'表示中':>6} {'非表示':>6}")
    print("-" * 62)
    for domain, s in sorted_domains:
        print(f"{domain:<40} {s['total']:>6} {s['visible']:>6} {s['hidden']:>6}")
    print(f"\n合計: {len(rows)}件（{len(stats)}ドメイン）")


def cmd_purge(supabase, domain: str, physical: bool, yes: bool) -> None:
    """指定ドメインの画像を一括処理する。"""
    # 対象件数を事前確認
    try:
        target_resp = (
            supabase.table("official_card_images")
            .select("id, storage_path, hidden")
            .eq("source_domain", domain)
            .is_("deleted_at", "null")
            .execute()
        )
        targets = target_resp.data or []
    except Exception as e:
        print(f"[エラー] 対象件数確認失敗: {e}")
        sys.exit(1)

    if not targets:
        print(f"ドメイン {domain!r} に対象の画像行はありません（未削除かつ該当なし）。")
        return

    already_hidden = sum(1 for r in targets if r["hidden"])
    not_hidden = len(targets) - already_hidden

    # 処理内容を表示
    print(f"\n対象ドメイン: {domain}")
    print(f"  対象行数: {len(targets)}件")
    print(f"    うち既に hidden=true: {already_hidden}件")
    print(f"    うち hidden=false（新たに非表示化）: {not_hidden}件")
    if physical:
        print(f"  処理内容: 第2段階（hidden=true ＋ Storage物理削除 ＋ deleted_at 記録）")
    else:
        print(f"  処理内容: 第1段階（hidden=true のみ）")

    # 確認プロンプト
    if not yes:
        answer = input("\n上記の処理を実行しますか？ [yes/no]: ").strip().lower()
        if answer != "yes":
            print("中止しました。")
            return

    target_ids = [r["id"] for r in targets]
    errors = []

    # 第1段階: hidden=true
    try:
        hidden_resp = (
            supabase.table("official_card_images")
            .update({"hidden": True})
            .in_("id", target_ids)
            .execute()
        )
        hidden_count = len(hidden_resp.data or [])
        print(f"\n[完了] hidden=true に更新: {hidden_count}件")
    except Exception as e:
        print(f"[エラー] hidden=true 更新失敗: {e}")
        sys.exit(1)

    deleted_count = 0

    if physical:
        # 第2段階: Storage物理削除
        storage_paths = [r["storage_path"] for r in targets if r.get("storage_path")]
        if storage_paths:
            try:
                supabase.storage.from_("official-card-images").remove(storage_paths)
                print(f"[完了] Storage 物理削除: {len(storage_paths)}件")
            except Exception as e:
                err_msg = f"Storage 削除失敗: {e}"
                print(f"[警告] {err_msg}")
                errors.append(err_msg)
        else:
            print("[スキップ] storage_path が空の行があったため Storage 削除をスキップしました。")

        # deleted_at を記録
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            del_resp = (
                supabase.table("official_card_images")
                .update({"deleted_at": now_iso})
                .in_("id", target_ids)
                .execute()
            )
            deleted_count = len(del_resp.data or [])
            print(f"[完了] deleted_at 記録: {deleted_count}件")
        except Exception as e:
            print(f"[エラー] deleted_at 記録失敗: {e}")
            errors.append(f"deleted_at 記録失敗: {e}")

    # 結果サマリ
    print("\n── 処理結果 ──")
    print(f"  対象ドメイン : {domain}")
    print(f"  hidden=true  : {hidden_count}件")
    if physical:
        print(f"  物理削除記録 : {deleted_count}件")
    if errors:
        print(f"  エラー       : {len(errors)}件")
        for err in errors:
            print(f"    - {err}")
    else:
        print("  エラー       : なし")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="official_card_images の画像をドメイン単位で一括削除するCLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  python purge_images.py --list
  python purge_images.py --domain yu-gi-oh.jp
  python purge_images.py --domain yu-gi-oh.jp --delete
  python purge_images.py --domain yu-gi-oh.jp --delete --yes
""",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="source_domain ごとの件数一覧を表示する",
    )
    parser.add_argument(
        "--domain",
        metavar="DOMAIN",
        help="処理対象のドメイン（例: yu-gi-oh.jp）",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="第2段階: Storage物理削除 ＋ deleted_at 記録も実行する",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="確認プロンプトをスキップする",
    )

    args = parser.parse_args()

    if not args.list and not args.domain:
        parser.print_help()
        sys.exit(1)

    supabase = _get_supabase()

    if args.list:
        cmd_list(supabase)

    if args.domain:
        cmd_purge(
            supabase=supabase,
            domain=args.domain,
            physical=args.delete,
            yes=args.yes,
        )


if __name__ == "__main__":
    main()
