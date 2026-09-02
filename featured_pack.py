"""
新弾フィーチャー管理モジュール
================================
発売日ベースで「運用対象の新弾」を自動判定し、収録カード一覧と
運用ウィンドウ（発売後 N 日間）の判定を提供する。

動作の流れ:
  1. Supabase の featured_pack テーブルに active=True の行があればそれを優先
  2. なければ pack_list.release_date から「発売後7日以内の最新弾」を自動選択
  → pack_list は毎日 5:00 に collect_prices.py が更新するため、
    次回新弾の発売日になると自動的に切り替わり、8日目以降は自動停止する。

手動オーバーライドが必要な場合:
  Supabase 管理画面で featured_pack テーブルに1行追加するだけ。
  DDL は featured_pack.sql 参照。
"""

import time
from datetime import date, datetime
from constants import JST


def _today_jst() -> date:
    return datetime.now(JST).date()


# featured_pack テーブルが未作成と判定した時刻（time.time()）。未作成は設計上の
# 許容だが、判定後も毎回問い合わせると1日24回（呼び出し頻度分）404が出続けるため、
# 1時間はテーブル存在確認自体をスキップする。テーブル未作成だと確実に分かる
# エラー（PGRST205/PGRST202/42P01）のときだけ記憶し、一過性のエラーは従来どおり
# 毎回試す。2026-09-02 レビュー差し戻し: "does not exist" の文字列一致は
# 列不存在（42703）等の別種エラーまで拾ってしまい誤判定するため外した
_FEATURED_PACK_TABLE_MISSING_SINCE = None
_FEATURED_PACK_MISSING_TTL_SEC = 3600


def _is_table_missing_error(e: Exception) -> bool:
    # postgrest-py の APIError は .code にPostgREST/Postgresのエラーコードを持つ。
    # コードが取れる場合はそれを正として判定し、取れない例外（ネットワークエラー等）
    # のときだけ文字列一致にフォールバックする
    code = getattr(e, "code", None)
    if code is not None:
        return code in {"PGRST205", "PGRST202", "42P01"}
    msg = str(e)
    return "PGRST205" in msg or "42P01" in msg


def get_featured_pack(sb):
    """
    運用対象の新弾情報を返す。

    Returns:
        {
            "pack_name": str,
            "wiki_page": str,
            "tcg_name": str,
            "start_date": str  # "YYYY-MM-DD"
            "window_days": int  # 運用期間日数（既定 7）
        }
        または None（運用対象なし）
    """
    global _FEATURED_PACK_TABLE_MISSING_SINCE
    today = _today_jst()

    # 1. 手動オーバーライドテーブルを確認
    #    テーブルが未作成の場合も例外を出さずフォールバックする。
    #    未作成と判定済み・かつTTL内なら問い合わせ自体をスキップする
    now = time.time()
    table_known_missing = (
        _FEATURED_PACK_TABLE_MISSING_SINCE is not None
        and now - _FEATURED_PACK_TABLE_MISSING_SINCE < _FEATURED_PACK_MISSING_TTL_SEC
    )
    if not table_known_missing:
        try:
            resp = (sb.table("featured_pack")
                    .select("pack_name, wiki_page, tcg_name, start_date, window_days")
                    .eq("active", True)
                    .order("start_date", desc=True)
                    .limit(1)
                    .execute())
            rows = resp.data or []
            if rows:
                r = rows[0]
                pack = {
                    "pack_name": r["pack_name"],
                    "wiki_page": r.get("wiki_page") or "",
                    "tcg_name": r.get("tcg_name") or "",
                    "start_date": r["start_date"][:10],
                    "window_days": r.get("window_days") or 7,
                }
                print(f"  [featured] オーバーライド使用: {pack['pack_name']} (start={pack['start_date']})")
                return pack
        except Exception as e:
            if _is_table_missing_error(e):
                _FEATURED_PACK_TABLE_MISSING_SINCE = now
                print(f"  [featured] featured_packテーブル未作成と判定。{_FEATURED_PACK_MISSING_TTL_SEC}秒は問い合わせをスキップします: {e}")
            else:
                print(f"  [featured] オーバーライドテーブル未確認（フォールバック）: {e}")

    # 2. pack_list から自動算出
    try:
        resp = (sb.table("pack_list")
                .select("name, wiki_page, tcg_name, release_date")
                .lte("release_date", today.isoformat())
                .order("release_date", desc=True)
                .limit(5)
                .execute())
        rows = resp.data or []
    except Exception as e:
        print(f"  [featured] pack_list 取得失敗: {e}")
        return None

    for r in rows:
        release_str = (r.get("release_date") or "")[:10]
        if not release_str:
            continue
        try:
            release_date = date.fromisoformat(release_str)
        except ValueError:
            continue
        elapsed = (today - release_date).days
        if 0 <= elapsed <= 7:
            pack = {
                "pack_name": r["name"],
                "wiki_page": r.get("wiki_page") or "",
                "tcg_name": r.get("tcg_name") or "",
                "start_date": release_str,
                "window_days": 7,
            }
            print(f"  [featured] 自動判定: {pack['pack_name']} (発売{elapsed}日目)")
            return pack

    print("  [featured] 運用ウィンドウ内の新弾なし")
    return None


def is_within_window(pack) -> bool:
    """
    pack が今日の運用ウィンドウ内（発売日から window_days 日以内）かを返す。
    pack は get_featured_pack() の戻り値。
    """
    if not pack:
        return False
    today = _today_jst()
    try:
        start = date.fromisoformat(pack["start_date"][:10])
    except (KeyError, ValueError):
        return False
    elapsed = (today - start).days
    window = pack.get("window_days") or 7
    return 0 <= elapsed <= window


def get_days_since_release(pack) -> int:
    """発売日から何日目か（発売当日=0）を返す。判定不能時は -1。"""
    if not pack:
        return -1
    today = _today_jst()
    try:
        start = date.fromisoformat(pack["start_date"][:10])
    except (KeyError, ValueError):
        return -1
    return (today - start).days


def get_featured_cards(sb, pack) -> list:
    """
    pack の収録カード名リスト（normalize_card_name 適用済み）を返す。
    Supabaseキャッシュを優先参照し、未ヒット時はWikiスクレイピングして保存する。
    取得失敗時は空リストを返す。
    """
    if not pack:
        return []

    import re
    from datetime import datetime, timezone, timedelta
    from pack_scraper import fetch_pack_cards
    from collect_prices import normalize_card_name

    pack_name = pack["pack_name"]
    wiki_page = pack.get("wiki_page") or ""
    tcg_name = pack.get("tcg_name") or ""
    safe_key = "pack_" + re.sub(r"[^\w]", "_", pack_name)[:80]

    # Supabaseキャッシュを確認（Renderデプロイ後もデータが残る）
    if sb:
        try:
            resp = sb.table("pack_cards_cache").select("cards, updated_at").eq("pack_key", safe_key).execute()
            if resp.data:
                row = resp.data[0]
                updated_at = datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - updated_at < timedelta(hours=24):
                    raw_cards = row["cards"] or []
                    print(f"  [featured] {pack_name}: Supabaseキャッシュから {len(raw_cards)} 枚取得")
                    return [normalize_card_name(c) for c in raw_cards if c]
        except Exception as e:
            print(f"  [featured] Supabaseキャッシュ読み込み失敗: {e}")

    # スクレイピング実行
    try:
        result = fetch_pack_cards(pack_name, wiki_page, tcg_name)
        raw_cards = result.get("cards") or []
        normalized = [normalize_card_name(c) for c in raw_cards if c]
        print(f"  [featured] {pack_name}: 収録カード {len(normalized)} 枚取得（スクレイピング）")

        # Supabaseに保存（次回デプロイ後もスクレイピング不要）
        if sb and raw_cards:
            try:
                sb.table("pack_cards_cache").upsert({
                    "pack_key": safe_key,
                    "cards": raw_cards,
                }).execute()
                print(f"  [featured] {pack_name}: Supabaseに {len(raw_cards)} 枚を保存")
            except Exception as e:
                print(f"  [featured] Supabaseキャッシュ書き込み失敗: {e}")

        return normalized
    except Exception as e:
        print(f"  [featured] 収録カード取得失敗 [{pack_name}]: {e}")
        return []


def get_initial_prices(sb, card_names) -> list:
    """
    発売当日用: 当日の最安値降順でカードリストを返す。
    Returns: list of {"name": str, "today": int}
    """
    if not card_names:
        return []

    today_str = datetime.now(JST).strftime("%Y-%m-%d")

    try:
        all_rows = []
        page_size = 1000
        offset = 0
        while True:
            resp = (sb.table("price_history")
                    .select("card_name, min_price")
                    .eq("recorded_at", today_str)
                    # ページ間の順序安定化（order無しrangeはページ境界で行を取りこぼしうる。監査F8）
                    .order("card_name", desc=False)
                    .order("shop", desc=False)
                    .order("rarity", desc=False)
                    .range(offset, offset + page_size - 1)
                    .execute())
            batch = resp.data or []
            all_rows.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size

        name_set = set(card_names)
        best = {}
        for row in all_rows:
            name = row["card_name"]
            price = row.get("min_price", 0)
            if name not in name_set or price <= 10:
                continue
            if name not in best or price < best[name]:
                best[name] = price

        result = sorted(
            [{"name": k, "today": v} for k, v in best.items()],
            key=lambda x: -x["today"]
        )
        return result

    except Exception as e:
        print(f"  [featured] 初値取得失敗: {e}")
        return []


def get_card_history_since(sb, card_name, since_date_str) -> list:
    """
    指定カードの since_date_str 以降の price_history 行を返す。
    グラフ生成用。
    """
    try:
        # limit/range無しの一括読みはPostgREST上限1000行で黙って切られる
        # （監査F2: docs/audit-price-logic-2026-08-06.md）。range分割＋複合キーorderで取得する
        page_size = 1000
        max_pages = 10  # 暴走ガード
        offset = 0
        rows: list = []
        while True:
            resp = (sb.table("price_history")
                    .select("card_name, shop, rarity, min_price, recorded_at")
                    .eq("card_name", card_name)
                    .gte("recorded_at", since_date_str)
                    .order("recorded_at", desc=False)
                    .order("shop", desc=False)
                    .order("rarity", desc=False)
                    .range(offset, offset + page_size - 1)
                    .execute())
            batch = resp.data or []
            rows.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size
            if offset >= max_pages * page_size:
                print(f"  [featured] 価格履歴が{max_pages}ページ超過のため打ち切り [{card_name}]")
                break
        return rows
    except Exception as e:
        print(f"  [featured] 価格履歴取得失敗 [{card_name}]: {e}")
        return []
