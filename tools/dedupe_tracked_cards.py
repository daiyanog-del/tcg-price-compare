"""
tools/dedupe_tracked_cards.py -- tracked_cards の表記ゆれ統合（フェーズ3 P1、一回限りのバックフィル）

背景（docs/design-phase3-generation-side.md 「P1」「§4 tracked_cardsバックフィル」）:
  P1本体の修正（collect_prices.py 経路A への canonicalize 適用）は「これから」の
  新規登録を防ぐだけで、既存の tracked_cards に既に入ってしまった表記ゆれ分裂
  （例:「ブラックマジシャン」「ブラック・マジシャン」が別行）は自然には解消しない
  （price_history と違い90日保持で自然入替しないマスタテーブルのため）。
  本スクリプトは tracked_cards 全件を cardnames_ja.json 照合で正式名グルーピングし、
  統合が必要なグループを洗い出す。

安全設計:
  - 既定はドライラン。DBへの書き込みは `--apply` を明示したときのみ行う。
  - fuzzy_key が複数の正式名に衝突するケース（例: E・HERO と E-HERO）は
    誤って別カードへ合流させないため統合対象から除外し、別枠で報告する
    （P6a と同じ安全側動作。canonicalize_card_name が担保する）。
  - price_history は一切触らない（90日保持で自然入替する方針。A1、バックフィルなし）。
  - 統合時、残す行の card_name は正式名。active は OR、last_collected_at は MAX で引き継ぐ。
    正式名の行が元々存在しない場合は、グループ内の1行（card_name昇順の先頭）を
    正式名へ改名したうえで他行を統合する。ただし改名を実行する直前に改名先の存在を
    再クエリで確認する（フェッチ時点では存在しなかった正式名の行が、ドライラン〜適用の間や
    app側の並行書き込みで新規insertされている可能性があるため。UNIQUE制約 23505 が
    未捕捉のまま処理が中断するのを防ぐ）。既に存在していた場合はその既存行を真のkeepと
    みなし、改名せず削除のみで統合する（apply_merge/apply_rename 共通のガード）。

対象カテゴリ（司令塔裁定 2026-07-10）:
  1. 統合候補（統合グループ）: canonicalize で同一正式名に解決する行が2件以上あるグループ。
     残す1行に統合し、他行は削除する。
  2. リネーム候補（単独行）: canonicalize が一意に解決するが、正式名の行がグループ内に
     存在しない単独行。入口のcanonicalize稼働後に同カードが正式名で新規登録されると
     二重行が生まれる「時限爆弾」のため、今回のバックフィルで一緒に改名する。
     --apply 時は card_name を正式名へ改名するだけ（削除は発生しない）。
     ただし改名先の正式名が（ドライラン後の並行書き込み等で）DBに既に存在する場合は
     レースを避けるため統合候補と同じ merge 処理（plan_merge/apply_merge）に回す。
  3. fuzzy衝突除外: 複数の正式名に衝突するため対象外（別カードの疑い、報告のみ）。

実行:
  python tools/dedupe_tracked_cards.py            # ドライラン（一覧表示のみ、DB無変更）
  python tools/dedupe_tracked_cards.py --apply    # 統合を実行（DB書き込みあり）

  実行はユーザー承認後、ローカルから行うこと（Render cron の時間帯 JST 5:00/7:00 を避ける）。
  また app側の書き込み経路（購入候補追加・マイデッキ保存の _track_card_async /
  _track_cards_async 等、app.py の常時稼働エンドポイント）も並行してtracked_cardsに
  書き込みうるため、--apply は利用者の少ない低トラフィック時間帯に実行すること
  （レース自体はapply_merge/apply_renameの再確認ガードで安全側に倒すが、頻度は低いほど良い）。

認証:
  環境変数 SUPABASE_URL / SUPABASE_KEY を使う。未設定時は
  ~/.cardsouba/watcher.env から読み込む（ローカル実行の利便のため、tools/refetch_missing_images.py と同様）。
"""

import io
import os
import sys

# tcg-web 直下のモジュール（collect_prices 等）を import できるようにする
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _load_env_fallback() -> None:
    """SUPABASE_URL/KEY が未設定なら ~/.cardsouba/watcher.env から補完する。"""
    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY"):
        return
    env_path = os.path.join(os.path.expanduser("~"), ".cardsouba", "watcher.env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if key and key not in os.environ:
                os.environ[key] = val


def build_merge_groups(rows: list[dict], cardnames_set: set, cardnames_fuzzy: dict):
    """tracked_cards の全行を正式名でグルーピングし、3カテゴリに分類する。

    戻り値: (dup_groups, rename_candidates, collisions)
      dup_groups: {正式名: [行, ...]}  # 2行以上のグループ（統合が必要な表記ゆれペア）
      rename_candidates: {正式名: 行}  # 単独行だが正式名と異なる（改名のみで済む）
      collisions: fuzzy_key衝突（複数正式名に一致）のため対象外にした行名のリスト
    """
    from name_normalize import canonicalize_card_name

    groups: dict[str, list[dict]] = {}
    collisions: list[str] = []

    for row in rows:
        name = row["card_name"]

        def _warn(msg, _name=name):
            collisions.append(_name)

        resolved, _matched = canonicalize_card_name(
            name, cardnames_set, cardnames_fuzzy, warn=_warn, context="dedupe_tracked_cards"
        )
        groups.setdefault(resolved, []).append(row)

    dup_groups: dict[str, list[dict]] = {}
    rename_candidates: dict[str, dict] = {}
    for target, members in groups.items():
        if len(members) >= 2:
            dup_groups[target] = members
        elif members[0]["card_name"] != target:
            # 単独行だが canonicalize が別名を返した = 正式名の行がまだ存在しない改名候補
            rename_candidates[target] = members[0]

    return dup_groups, rename_candidates, collisions


def plan_merge(target_name: str, rows: list[dict]):
    """1グループの統合計画を作る。

    戻り値: (keep_row, others, merged_active, merged_last_collected_at)
      keep_row: 残す行（正式名の行が既にあればそれ、無ければ card_name 昇順の先頭を採用し改名する）
      others: 削除する行のリスト
      merged_active: 全行の active の OR
      merged_last_collected_at: 全行の last_collected_at の MAX（Noneは無視。全行Noneなら None）
    """
    keep_row = next((r for r in rows if r["card_name"] == target_name), None)
    if keep_row is None:
        keep_row = sorted(rows, key=lambda r: r["card_name"])[0]
    others = [r for r in rows if r is not keep_row]

    merged_active = any(bool(r.get("active")) for r in rows)
    last_dates = [r["last_collected_at"] for r in rows if r.get("last_collected_at")]
    merged_last = max(last_dates) if last_dates else None

    return keep_row, others, merged_active, merged_last


def apply_merge(sb, target_name: str, keep_row: dict, others: list[dict],
                 merged_active: bool, merged_last: str | None) -> None:
    """1グループの統合をDBに適用する。price_history には一切触れない。

    keep_row を target_name へ改名する必要がある場合（= フェッチ時点では正式名の行が
    存在しなかったケース）、改名を実行する直前に改名先の存在を再クエリで確認する。
    ドライラン〜適用の間、あるいは本スクリプト実行中に、app側の書き込み経路
    （購入候補追加・マイデッキ保存の _track_card_async / _track_cards_async 等、
    app.py の常時稼働エンドポイント）が正式名の行を新規insertしている可能性があるため
    （UNIQUE制約 23505 が未捕捉のまま処理が中断するのを防ぐ安全策。apply_rename と対称）。
    既に存在していた場合は、その既存行を真のkeepとみなし、当初のkeep_row・others
    全行をmerge対象に回す（削除のみで済み、改名は発生しない）。
    """
    if keep_row["card_name"] != target_name:
        existing_resp = (
            sb.table("tracked_cards")
            .select("card_name, active, last_collected_at")
            .eq("card_name", target_name)
            .execute()
        )
        existing_rows = existing_resp.data or []
        if existing_rows:
            all_rows = [existing_rows[0], keep_row, *others]
            new_keep_row, new_others, new_merged_active, new_merged_last = plan_merge(target_name, all_rows)
            _write_merge(sb, target_name, new_keep_row, new_others, new_merged_active, new_merged_last)
            return

    _write_merge(sb, target_name, keep_row, others, merged_active, merged_last)


def _write_merge(sb, target_name: str, keep_row: dict, others: list[dict],
                  merged_active: bool, merged_last: str | None) -> None:
    """apply_merge の実書き込み本体。改名先の非存在確認は呼び出し側（apply_merge）の責務。"""
    update_fields: dict = {}
    if keep_row["card_name"] != target_name:
        update_fields["card_name"] = target_name
    if keep_row.get("active") != merged_active:
        update_fields["active"] = merged_active
    if merged_last and keep_row.get("last_collected_at") != merged_last:
        update_fields["last_collected_at"] = merged_last

    if update_fields:
        sb.table("tracked_cards").update(update_fields).eq("card_name", keep_row["card_name"]).execute()

    for r in others:
        sb.table("tracked_cards").delete().eq("card_name", r["card_name"]).execute()


def apply_rename(sb, target_name: str, row: dict) -> str:
    """単独行を正式名へ改名する（削除は発生しない）。

    改名先の正式名が（ドライラン後の並行書き込み等で）DBに既に存在する場合は
    UNIQUE制約違反を避けるため、統合候補と同じ plan_merge/apply_merge に回す。

    戻り値: "renamed"（単純改名） または "merged"（レース検出によりmerge処理へ回った）
    """
    existing_resp = (
        sb.table("tracked_cards")
        .select("card_name, active, last_collected_at")
        .eq("card_name", target_name)
        .execute()
    )
    existing_rows = existing_resp.data or []
    if existing_rows:
        keep_row, others, merged_active, merged_last = plan_merge(target_name, [row, existing_rows[0]])
        apply_merge(sb, target_name, keep_row, others, merged_active, merged_last)
        return "merged"

    sb.table("tracked_cards").update({"card_name": target_name}).eq("card_name", row["card_name"]).execute()
    return "renamed"


def main() -> int:
    apply = "--apply" in sys.argv

    _load_env_fallback()
    if not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY")):
        print("エラー: SUPABASE_URL / SUPABASE_KEY が未設定です")
        return 1

    from collect_prices import _fetch_all_rows, get_supabase
    from name_normalize import load_cardnames_index

    sb = get_supabase()

    print("tracked_cards 全件取得中...")
    rows = _fetch_all_rows(lambda: sb.table("tracked_cards").select("card_name, active, last_collected_at"))
    print(f"tracked_cards 全件: {len(rows)}件")

    _, cardnames_set, cardnames_fuzzy = load_cardnames_index()
    if not cardnames_set:
        print("エラー: data/cardnames_ja.json が読み込めませんでした（update_cardnames.py を実行してください）")
        return 1

    dup_groups, rename_candidates, collisions = build_merge_groups(rows, cardnames_set, cardnames_fuzzy)

    total_affected = sum(len(members) for members in dup_groups.values())
    print(
        f"\nカテゴリ別サマリ: "
        f"統合候補グループ {len(dup_groups)}件（対象行合計 {total_affected}件） / "
        f"リネーム候補（単独行） {len(rename_candidates)}件 / "
        f"fuzzy衝突除外 {len(collisions)}件"
    )

    print(f"\n--- 統合候補（{len(dup_groups)}件）---")
    for target_name in sorted(dup_groups):
        group_rows = dup_groups[target_name]
        keep_row, others, merged_active, merged_last = plan_merge(target_name, group_rows)

        rename_note = "" if keep_row["card_name"] == target_name else f"（{keep_row['card_name']} → 改名）"
        print(f"\n  [統合] {target_name}{rename_note}")
        print(f"    残す行: {keep_row['card_name']}")
        for r in others:
            print(f"    消す行: {r['card_name']}  (active={r.get('active')}, last_collected_at={r.get('last_collected_at')})")
        print(f"    統合後: active={merged_active}, last_collected_at={merged_last}")

        if apply:
            apply_merge(sb, target_name, keep_row, others, merged_active, merged_last)
            print("    -> 適用完了")

    print(f"\n--- リネーム候補（単独行、{len(rename_candidates)}件）---")
    for target_name in sorted(rename_candidates):
        row = rename_candidates[target_name]
        print(f"\n  [改名] {row['card_name']} → {target_name}")

        if apply:
            result = apply_rename(sb, target_name, row)
            if result == "merged":
                print("    -> 改名先が既に存在したためmergeで適用完了（レース検出）")
            else:
                print("    -> 適用完了（改名）")

    if collisions:
        print(f"\n--- fuzzy_key衝突のため対象外（複数正式名の疑い・統合しない、{len(collisions)}件）---")
        for name in collisions:
            print(f"    {name}")

    if not apply:
        print("\n--apply が指定されていないため、DBへの書き込みは行っていません（ドライラン）。")
        print("内容を確認のうえ、必要であれば python tools/dedupe_tracked_cards.py --apply を実行してください。")
    else:
        print(
            f"\n適用完了: 統合 {len(dup_groups)}グループ / 改名 {len(rename_candidates)}件。"
            f" price_history は変更していません。"
        )

    return 0


if __name__ == "__main__":
    # Windows コンソールで日本語を出すため stdout を UTF-8 化する
    # （import 時点でやるとテストのpytest captureと衝突するため、実行時のみ行う）
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
