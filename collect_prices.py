"""
価格履歴データ収集スクリプト
============================
GitHub Actionsで毎日実行し、監視対象カードの価格をSupabaseに記録する。

収集方式:
  注目カード（新弾・現メタ主要・規制・検索ヒット）は毎日全件収集。
  周辺カード（テーマ関連・入賞レシピ等）は last_collected_at 古い順に
  DAILY_COLD_BUDGET 枚をローテーション収集。
  → 全カードをDBに載せつつ毎日の収集時間を予算内に収める。
"""

import os
import re
import sys
import time
import zlib
from collections import Counter
from datetime import datetime, timezone, timedelta

from supabase import create_client, Client
from postgrest.types import CountMethod, ReturnMethod
from scraper import compare_prices, SHOPS, count_request, get_host_request_counts
from pack_scraper import (
    get_pack_list, fetch_pack_cards, _try_wiki_page_variants,
    fetch_theme_cards, fetch_card_themes,
)
from price_persist import (art_price_context, build_art_price_rows, build_min_price_rows, upsert_price_art_rows,
                           upsert_price_rows)
from clabo_catalog import collect_clabo_catalog
from torecolo_catalog import collect_torecolo_catalog
from kanabell_catalog import collect_kanabell_catalog
from cardrush_catalog import collect_cardrush_catalog
from overframe import apply_overframe, overframe_context
import shop_request_log
from collection_run import record_collection_run, attach_host_request_counts
from db_write_guard import (
    ConsecutiveDbFailureGuard, DbWriteAborted, GuardedSupabase, is_db_unavailable, report_db_abort,
)
from discord_notify import send_discord_message
from shop_status import SUSPENDED_SHOPS
from meta_scraper import fetch_tier_list, fetch_deck_cards
from name_normalize import load_cardnames_index, canonicalize_card_name, normalize_card_name

# ── Supabase接続 ──
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

from constants import JST, PRICE_RETENTION_DAYS, PRICE_DAILY_RETENTION_DAYS, REPLAY_RETENTION_DAYS

WAIT_BETWEEN_CARDS = 1.0

# 毎日の周辺カード収集予算（注目カードは予算外で全件収集）
DAILY_COLD_BUDGET = int(os.environ.get("DAILY_COLD_BUDGET", "800"))
# 収集ループの時間上限（秒）。超過で周辺カードを打ち切り、翌日再開
TIME_BUDGET_SEC = int(os.environ.get("TIME_BUDGET_SEC", str(300 * 60)))
# トレコロの在庫一覧の巡回（torecolo_catalog.py・2026-10-31）の使い方。Render の環境変数で切り替える
#   shadow: 巡回はするが保存はしない。トレコロは従来どおりカード名で探して保存し、巡回の値との食い違いを記録する（既定）
#   on:     巡回の値で保存し、トレコロのカード名の検索をやめる（1晩約3,900回 → 約530ページ＋検索し直し数十回）
#   off:    巡回しない（従来どおり）
TORECOLO_CATALOG_MODE = os.environ.get("TORECOLO_CATALOG_MODE", "shadow").strip().lower()
# カーナベルの販売中の全件巡回（kanabell_catalog.py・2026-11-01）の使い方。切り替えはトレコロと同じ
#   shadow: 巡回はするが保存はしない。カーナベルは従来どおりカード名で探して保存し、巡回の行との食い違いを記録する（既定）
#   on:     巡回の行で保存し、カーナベルのカード名の検索をやめる（1日約3,400回 → 約38ページ）
#   off:    巡回しない（従来どおり）
KANABELL_CATALOG_MODE = os.environ.get("KANABELL_CATALOG_MODE", "shadow").strip().lower()
# カードラッシュの型番の頭の検索でまとめて読む巡回（cardrush_catalog.py・2026-11-01）の使い方。切り替えはカーナベルと同じ
#   shadow: 巡回はするが保存はしない。カードラッシュは従来どおりカード名で探して保存し、巡回の行との食い違いを記録する（既定）
#   on:     巡回の行で保存し、カードラッシュのカード名の検索をやめる（1日約3,400回 → 約800〜900回の見込み）
#   off:    巡回しない（従来どおり）
CARDRUSH_CATALOG_MODE = os.environ.get("CARDRUSH_CATALOG_MODE", "shadow").strip().lower()
# 巡回で読む型番の頭の数。全頭（約670種）より少ないと試し読み（毎晩同じ頭を読んで応答時間だけ記録し、比べ・保存はしない）。
# "all" で全頭。初めて出す型番の頭の検索はお店の側で重い（6〜24秒）ので、2晩目以降に速くなるかを試し読みで見てから広げる
def _int_env(name: str, default: int, allow_all: bool = False) -> int | None:
    """整数の環境変数。allow_all なら "all" を None（上限なし）にする。読めない値は警告して既定値
    （収集全体を起動時に落とさない）。"""
    raw = os.environ.get(name, str(default)).strip().lower()
    if allow_all and raw == "all":
        return None
    try:
        return int(raw)
    except ValueError:
        print(f"  ⚠ {name}={raw!r} は数として読めません。既定の {default} を使います")
        return default


CARDRUSH_CATALOG_HEADS = _int_env("CARDRUSH_CATALOG_HEADS", 30, allow_all=True)
# カードラッシュで直近2晩とも行が無かったカード（在庫なし・売り切れだけ）を何日に1回検索するか（_cardrush_rests・2026-11-01）。
# 1以下で間引かない。11-01 の朝はカードラッシュの3,387枚のうち530枚が0件
CARDRUSH_EMPTY_REST_DAYS = _int_env("CARDRUSH_EMPTY_REST_DAYS", 3)  # TODO: calibrate from data

# 収集から除外する店舗。実行環境ごとに環境変数 COLLECT_SKIP_SHOPS（カンマ区切り）で上書きできる。
#   - 未設定（GitHub Actions 等の従来環境）: データセンターIPから遅い/到達不能な店舗を従来どおり除外
#   - Render など店舗に到達できる環境: 例 COLLECT_SKIP_SHOPS="駿河屋"（Cloudflare対策のみ）/ ""（除外なし）
_DEFAULT_SKIP_SHOPS = {"遊々亭", "駿河屋", "カードラボ"}
_skip_env = os.environ.get("COLLECT_SKIP_SHOPS")
if _skip_env is None:
    SKIP_SHOPS_IN_CI = set(_DEFAULT_SKIP_SHOPS)
else:
    SKIP_SHOPS_IN_CI = {s.strip() for s in _skip_env.split(",") if s.strip()}

# ヘルスチェックURLは「実スクレイパーが実際にアクセスするドメイン」と一致させること。
# （cardrush-yugioh.jp / torecolo.jp はスクレイパーの実ドメイン cardrush.jp / www.torecolo.jp と
#   食い違っており、Renderから ConnectionError で誤って当日スキップされていた — 2026-06-15修正）
SHOP_HEALTH_URLS = {
    "カードラッシュ": "https://www.cardrush.jp/",
    "トレコロCB": "https://www.torecolo.jp/",
    "カーナベル": "https://www.ka-nabell.com/",
    "まんぞく屋": "https://shopmanzokuya.com/",
}


def check_shop_availability(shop_names: list[str], health_urls: dict[str, str] | None = None) -> list[str]:
    """各店舗に接続確認し、応答した店舗名のリストを返す（失敗店舗は当日スキップ）

    health_urls は確認先の上書き。買取のようにスクレイパーが販売と別のドメインへ行く収集は、
    そのドメインを渡す（SHOP_HEALTH_URLS の注意書きと同じ理由）。
    """
    import requests as _req
    headers = {"User-Agent": "Mozilla/5.0 (compatible; TCGYMBot/1.0)"}
    urls = health_urls if health_urls is not None else SHOP_HEALTH_URLS
    available = []
    for name in shop_names:
        url = urls.get(name)
        if not url:
            available.append(name)
            continue
        try:
            try:
                count_request(url)  # 接続確認も店への取得として数える
                resp = _req.get(url, timeout=5, headers=headers, stream=True)
                resp.close()
            except _req.RequestException:
                # 一度のタイムアウト・接続失敗でその日の収集をまるごと落とさないよう、少し待って1回取り直す
                # （2026-10 に買取のカードラッシュが9日、1回のタイムアウトで丸ごと飛ばされていた）。
                # 取り直しは実際の取得と同じくらい待つ（safe_get の既定は15秒・買取のカードラッシュは20秒。トップが12秒かかる時がある）
                time.sleep(5)
                count_request(url)  # 接続確認も店への取得として数える
                resp = _req.get(url, timeout=15, headers=headers, stream=True)
                resp.close()
            if resp.status_code >= 500:
                # 一瞬の 502/503 でその日の収集をまるごと落とさないよう、5xx だけ少し待って1回取り直す
                time.sleep(5)
                count_request(url)  # 接続確認も店への取得として数える
                resp = _req.get(url, timeout=5, headers=headers, stream=True)
                resp.close()
        except Exception as e:
            print(f"  NG: {name} — 当日スキップ（{type(e).__name__}）")
            continue
        # 5xx はサイトが止まっている（2026-10-21 カーナベルは停止のお知らせを 503 で返していた。
        # 以前は応答があれば通していたため、裏側の検索サービスに残った在庫が DB に入った）。
        # 4xx は通す（ボット対策の 403 でもスクレイパー側は取れている店があるため。従来どおり）
        if resp.status_code >= 500:
            print(f"  NG: {name} — 当日スキップ（HTTP {resp.status_code}）")
            continue
        available.append(name)
        print(f"  OK: {name}")
    return available


def watch_suspended_shops() -> None:
    """休止中の店（SUSPENDED_SHOPS）のサイトが応答を返すようになったら Discord に知らせる。
    再開したら新しいサイトの作りで取得できるか確かめてから SUSPENDED_SHOPS を外すため、自動では外さない。"""
    import requests as _req
    headers = {"User-Agent": "Mozilla/5.0 (compatible; TCGYMBot/1.0)"}
    for name in sorted(SUSPENDED_SHOPS):
        url = SHOP_HEALTH_URLS.get(name)
        if not url:
            print(f"  休止中: {name}（確認先URLなし）")
            continue
        try:
            count_request(url)  # 接続確認も店への取得として数える
            resp = _req.get(url, timeout=10, headers=headers, stream=True)
            resp.close()
        except Exception as e:
            print(f"  休止中: {name} — 応答なし（{type(e).__name__}）")
            continue
        print(f"  休止中: {name} — HTTP {resp.status_code}")
        if resp.status_code < 400:
            send_discord_message(
                f"[ラボ] 休止中の店「{name}」のサイトが応答を返しています（HTTP {resp.status_code}・{url}）。"
                "再開していれば、新しいサイトの作りで取得できるか確かめてから、"
                "Render の SUSPENDED_SHOPS からこの店を外してください。",
                no_mentions=True)


def get_supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("エラー: SUPABASE_URL と SUPABASE_KEY を環境変数に設定してください")
        sys.exit(1)
    return create_client(SUPABASE_URL, SUPABASE_KEY)


# PostgREST（SupabaseのREST層）は1リクエスト最大1000行しか返さない。
# tracked_cards は1000件を超えているため、全件が必要な読み出しは必ず分割取得する。
# （2026-07-10 根治: 上限超過で existing 集合が欠け、新規カード追加の一括insertが
#   duplicate key で全滅し続け、収集対象が痩せていく障害が起きていた。TASKS.md 参照）
# 注意: 終端判定（len(batch) < _PAGE_SIZE）は「サーバの返却上限 >= _PAGE_SIZE」が前提。
# Supabase の Max rows 設定を1000未満に下げる場合はこの値も合わせること。
_PAGE_SIZE = 1000


def _fetch_all_rows(make_query, order_by: str = "card_name") -> list[dict]:
    """make_query が返す select ビルダーを range 指定で繰り返し実行し、全行を返す。

    ページ間で並び順が揺れると境界の行を取りこぼすため、安定キーで必ず order する。
    """
    rows: list[dict] = []
    offset = 0
    while True:
        resp = make_query().order(order_by) \
            .range(offset, offset + _PAGE_SIZE - 1).execute()
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < _PAGE_SIZE:
            return rows
        offset += _PAGE_SIZE


def _fetch_all_tracked_names(sb: Client) -> set[str]:
    """tracked_cards の全カード名（active を問わず）。新規追加時の重複チェック用"""
    rows = _fetch_all_rows(lambda: sb.table("tracked_cards").select("card_name"))
    return {row["card_name"] for row in rows}


def fetch_tracked_cards(sb: Client) -> list[str]:
    """監視対象カードの名前一覧を取得（後方互換用）"""
    rows = _fetch_all_rows(
        lambda: sb.table("tracked_cards").select("card_name").eq("active", True)
    )
    cards = [row["card_name"] for row in rows]
    print(f"監視対象カード: {len(cards)}件")
    return cards


def fetch_tracked_cards_with_meta(sb: Client) -> list[dict]:
    """監視対象カードを last_collected_at つきで取得。列未追加時は名前のみ返す。"""
    try:
        rows = _fetch_all_rows(
            lambda: sb.table("tracked_cards")
            .select("card_name, last_collected_at")
            .eq("active", True)
        )
    except Exception:
        # last_collected_at カラム未追加時のフォールバック
        rows = _fetch_all_rows(
            lambda: sb.table("tracked_cards").select("card_name").eq("active", True)
        )
    cards = [
        {"card_name": row["card_name"], "last_collected_at": row.get("last_collected_at")}
        for row in rows
    ]
    print(f"監視対象カード: {len(cards)}件")
    return cards


# price_history への upsert を分割する行数（中4）。カード単位で直列 upsert すると
# カード枚数ぶんのラウンドトリップが発生するため、全カードの行をまとめてから
# チャンクに分けて渡す。500はPostgRESTの実務的なペイロードサイズを考慮した目安。
_UPSERT_CHUNK_SIZE = 500


def _prepare_clabo_catalog(selected: list[dict], available_shops: list[str],
                           shop_stats: dict) -> tuple[list[str], dict[str, list[dict]] | None, dict | None]:
    """カードラボのカタログ巡回（P1・403対策）を、カード単位ループの前に実行する。

    カード単位の検索をやめ、遊戯王カテゴリ14個のカタログページを巡回して全商品を
    取得し、ローカルで監視カードに名寄せする（docs/audit-403-plan-2026-08-08.md、
    docs/decisions.md 2026-08-07 F10）。

    戻り値は (loop_shops, matched, catalog_stats)。
      - loop_shops: 以後のカード単位ループに渡す店舗リスト。成立時はカードラボを除く。
        available_shops 自体は変更しない（collection_runs には観測店舗としてカードラボ
        入りのまま渡すため。reviewer中1）
      - matched: 成立時はカード名→商品リストのdict。対象外/不成立/例外時は None
      - catalog_stats: crawl_catalog由来の統計。matched が None の場合は保存フェーズが
        呼ばれないため None のまま返る

    保存（rows構築・upsert）はここでは行わない（reviewer中3）。時間予算打ち切りで
    ループが処理しなかったカードにカードラボ単独の行を書くと、その日の全店minが
    カードラボ価格になり偽の棘が立つため、実際にループで処理されたカードだけを
    対象に後段の _save_clabo_catalog_rows で保存する。

    shop_stats["カードラボ"] は「対象外」以外のケースで先にここで作っておく。
    不成立/例外時は ok/empty/error を 0 で初期化するだけで、実際の集計は後続の
    カード単位ループ（collect_and_save）が従来どおり書き込む（loop_shopsに
    カードラボが残っているため）。成立時は _save_clabo_catalog_rows が後で設定する。

    collect_clabo_catalog が例外を送出した場合（reviewer高3）は、目立つ警告を出し
    shop_stats に mode="error" を記録したうえで loop_shops を変更せず返す
    （＝従来の検索方式へフォールバック）。
    """
    loop_shops = list(available_shops)
    if "カードラボ" not in available_shops:
        return loop_shops, None, None

    clabo_card_names = [c["card_name"] for c in selected]
    print("\n--- カードラボ カタログ巡回 ---")
    try:
        matched, catalog_stats = collect_clabo_catalog(clabo_card_names)
    except Exception as e:
        print(
            f"  ⚠⚠ カードラボ カタログ巡回で予期しない例外が発生しました: {e}。"
            "従来の検索方式へフォールバックします"
        )
        shop_stats["カードラボ"] = {
            "ok": 0, "empty": 0, "error": 0,
            "mode": "error", "exception": str(e),
        }
        return loop_shops, None, None

    if matched is None:
        print(
            "  カタログ巡回が不成立（カテゴリ/ページ取得失敗）。"
            "従来の検索方式へフォールバックします"
        )
        shop_stats["カードラボ"] = {
            "ok": 0, "empty": 0, "error": 0,
            "mode": "fallback", "catalog": catalog_stats,
        }
        return loop_shops, None, None

    loop_shops = [s for s in loop_shops if s != "カードラボ"]
    return loop_shops, matched, catalog_stats


def _save_clabo_catalog_rows(sb: Client, processed: list[dict], matched: dict[str, list[dict]],
                             today: str, shop_stats: dict, catalog_stats: dict,
                             art_contexts: dict[str, dict] | None = None) -> int:
    """カタログ巡回の結果を、実際にカード単位ループで処理されたカードだけ保存する。

    processed は selected のうち、時間予算打ち切りの break が起きる前にループが
    実際に処理したカードのスライス（reviewer中3）。打ち切られたカードにはカードラボ
    単独の行を書かない（偽の価格棘を防ぐ）。

    全カード分の行をまとめて構築し、_UPSERT_CHUNK_SIZE 行ずつのチャンクで
    upsert_price_rows に渡す（reviewer中4。カード単位の直列upsertをやめる）。
    """
    clabo_ok, clabo_empty, all_rows, art_rows = _build_catalog_rows(processed, matched, today, art_contexts)

    saved = 0
    for i in range(0, len(all_rows), _UPSERT_CHUNK_SIZE):
        chunk = all_rows[i:i + _UPSERT_CHUNK_SIZE]
        saved += upsert_price_rows(sb, chunk)
    for i in range(0, len(art_rows), _UPSERT_CHUNK_SIZE):
        upsert_price_art_rows(sb, art_rows[i:i + _UPSERT_CHUNK_SIZE])

    shop_stats["カードラボ"] = {
        "ok": clabo_ok, "empty": clabo_empty, "error": 0,
        "mode": "catalog", "catalog": catalog_stats,
    }
    print(
        f"  カタログ巡回成立: 対象{clabo_ok}枚 / 0件{clabo_empty}枚 / "
        f"保存{saved}行（商品{catalog_stats['products']}件・"
        f"カテゴリ失敗{len(catalog_stats['categories_failed'])}件）"
    )
    return saved


def _build_catalog_rows(processed: list[dict], matched: dict[str, list[dict]], today: str,
                        art_contexts: dict[str, dict] | None) -> tuple[int, int, list[dict], list[dict]]:
    """カタログ巡回の名寄せ結果から、ループで処理したカードの保存行を作る（カードラボ・トレコロ共通。保存はしない）。
    戻り値: (商品のあったカード数, 無かったカード数, 本表の行（キーで一意化済み）, 絵ごとの行)"""
    clabo_ok = 0
    clabo_empty = 0
    all_rows: list[dict] = []
    art_rows: list[dict] = []
    for card_data in processed:
        card_name = card_data["card_name"]
        products = matched.get(card_name, [])
        # 印の無い OF の行を、ループ側（ほかの店の行）で分かった型番ごとの OF の書き方にそろえる（overframe.py）
        products = apply_overframe(products, ((art_contexts or {}).get(card_name) or {}).get("overframe"))
        if products:
            clabo_ok += 1
            all_rows.extend(build_min_price_rows(card_name, products, today))
            # 絵ごとの内訳。ループ側（ほかの店の行）で分かった「絵が分かれるレアリティ」「型番ごと違う絵の型番」に
            # そろえて判定する（カードラボの行だけだと、分かれるレアリティのカードラボの行が抜ける。10-30 reviewer 指摘）
            art_rows.extend(build_art_price_rows(card_name, products, today, (art_contexts or {}).get(card_name)))
        else:
            clabo_empty += 1

    # reviewer低6: (card_name, shop, rarity, recorded_at) が重複した行がチャンク内に
    # 混ざると、upsert の ON CONFLICT DO UPDATE がチャンクまとめてエラーになる
    # （build_min_price_rows は (shop, rarity) で集約するため現状は到達不能だが、
    # 複数カードにまたがる集約後の全体リストとしての保険）。同キーは後勝ちで一意化する
    deduped: dict[tuple, dict] = {}
    for row in all_rows:
        key = (row["card_name"], row["shop"], row["rarity"], row["recorded_at"])
        deduped[key] = row
    return clabo_ok, clabo_empty, list(deduped.values()), art_rows


TORECOLO = "トレコロCB"


def _prepare_torecolo_catalog(selected: list[dict], loop_shops: list[str], available_shops: list[str],
                              shop_stats: dict, mode: str | None = None
                              ) -> tuple[list[str], dict[str, list[dict]] | None, dict | None]:
    """トレコロの在庫一覧の巡回（torecolo_catalog.py）を、カード単位ループの前に実行する。

    戻り値は (loop_shops, matched, catalog_stats)。カードラボの _prepare_clabo_catalog と同じ形。
      - mode "on" で成立: loop_shops からトレコロを外す。カードごとの巡回の商品は、ループで collect_and_save に
        torecolo_products として渡し、ほかの店の行と合わせて保存する（_torecolo_card_plan）
      - mode "shadow" で成立: loop_shops はそのまま（トレコロはカード名で探して保存し、後で食い違いを比べる）
      - 不成立・例外・"off"・トレコロが使えない晩: matched は None（従来どおり）
    """
    mode = mode or TORECOLO_CATALOG_MODE
    loop_shops = list(loop_shops)
    if mode not in ("on", "shadow", "off"):
        print(f"  ⚠ TORECOLO_CATALOG_MODE={mode!r} は分からない値です（shadow / on / off）。巡回しません")
        return loop_shops, None, None
    if mode == "off" or TORECOLO not in available_shops:
        return loop_shops, None, None
    print(f"\n--- トレコロ 在庫一覧の巡回（{mode}） ---")
    try:
        matched, catalog_stats = collect_torecolo_catalog([c["card_name"] for c in selected])
    except Exception as e:
        print(f"  ⚠⚠ トレコロ 在庫一覧の巡回で予期しない例外: {e}。トレコロはカード名で探します")
        shop_stats[TORECOLO] = {"ok": 0, "empty": 0, "error": 0, "mode": "error", "exception": str(e)}
        return loop_shops, None, None
    if matched is None:
        print("  トレコロ 在庫一覧の巡回が不成立（系列の取得失敗・上限）。トレコロはカード名で探します")
        shop_stats[TORECOLO] = {"ok": 0, "empty": 0, "error": 0, "mode": "fallback", "catalog": catalog_stats}
        return loop_shops, None, None
    print(f"  トレコロ 在庫一覧の巡回: 商品{catalog_stats['products']}件・名寄せ{catalog_stats['matched_products']}件")
    if mode == "shadow":
        # ループがトレコロの ok/empty/error を足していく（印だけ先に置く）
        shop_stats[TORECOLO] = {"ok": 0, "empty": 0, "error": 0, "mode": "shadow", "catalog": catalog_stats}
        return loop_shops, matched, catalog_stats
    # on: ループがトレコロの ok/empty/error（巡回の商品のあった・無かったカード、検索し直しの結果）を足していく
    shop_stats[TORECOLO] = {"ok": 0, "empty": 0, "error": 0, "mode": "catalog", "catalog": catalog_stats,
                            "recheck": {"cards": 0, "prev_cards": None, "prev_read_failed": False}}
    return [s for s in loop_shops if s != TORECOLO], matched, catalog_stats


KANABELL = "カーナベル"


def _prepare_kanabell_catalog(selected: list[dict], loop_shops: list[str], available_shops: list[str],
                              shop_stats: dict, mode: str | None = None
                              ) -> tuple[list[str], dict[str, list[dict]] | None]:
    """カーナベルの全件巡回（kanabell_catalog.py）を、カード単位ループの前に実行する。

    戻り値は (loop_shops, matched)。
      - mode "on" で成立: loop_shops からカーナベルを外す。カードごとの巡回の行は collect_and_save に
        kanabell_products として渡し、ほかの店の行と合わせて保存する
      - mode "shadow" で成立: loop_shops はそのまま（カーナベルはカード名で探して保存し、ループの後で
        _compare_kanabell_shadow が検索の行と巡回の行を比べる）
      - 不成立・例外・"off"・カーナベルが使えない晩: matched は None（従来どおり）
    読み方は ES の search_after（商品IDの昇順）なので、読んでいる間の売れ行きでページの境目がずれて読み落とす
    ことはない（トレコロの _torecolo_card_plan のような検索し直しは要らない）。
    """
    mode = mode or KANABELL_CATALOG_MODE
    loop_shops = list(loop_shops)
    if mode not in ("on", "shadow", "off"):
        print(f"  ⚠ KANABELL_CATALOG_MODE={mode!r} は分からない値です（shadow / on / off）。巡回しません")
        return loop_shops, None
    if mode == "off" or KANABELL not in available_shops:
        return loop_shops, None
    print(f"\n--- カーナベル 全件巡回（{mode}） ---")
    try:
        matched, catalog_stats = collect_kanabell_catalog([c["card_name"] for c in selected])
    except Exception as e:
        print(f"  ⚠⚠ カーナベル 全件巡回で予期しない例外: {e}。カーナベルはカード名で探します")
        shop_stats[KANABELL] = {"ok": 0, "empty": 0, "error": 0, "mode": "error", "exception": str(e)}
        return loop_shops, None
    if matched is None:
        print("  カーナベル 全件巡回が不成立（取得失敗・読み切れず）。カーナベルはカード名で探します")
        shop_stats[KANABELL] = {"ok": 0, "empty": 0, "error": 0, "mode": "fallback", "catalog": catalog_stats}
        return loop_shops, None
    print(f"  カーナベル 全件巡回: {catalog_stats['listed']}商品・{catalog_stats['rows']}行・"
          f"名寄せ{catalog_stats['matched_products']}商品")
    if mode == "shadow":
        shop_stats[KANABELL] = {"ok": 0, "empty": 0, "error": 0, "mode": "shadow", "catalog": catalog_stats}
        return loop_shops, matched
    shop_stats[KANABELL] = {"ok": 0, "empty": 0, "error": 0, "mode": "catalog", "catalog": catalog_stats}
    return [s for s in loop_shops if s != KANABELL], matched


CARDRUSH = "カードラッシュ"


def _prepare_cardrush_catalog(selected: list[dict], loop_shops: list[str], available_shops: list[str],
                              shop_stats: dict, mode: str | None = None
                              ) -> tuple[list[str], dict[str, list[dict]] | None]:
    """カードラッシュの型番の頭の巡回（cardrush_catalog.py）を、カード単位ループの前に実行する。

    戻り値・モードの扱いは _prepare_kanabell_catalog と同じ（on で成立すれば loop_shops からカードラッシュを外し、
    shadow ならループの後で _compare_catalog_shadow が検索の行と比べる）。
    CARDRUSH_CATALOG_HEADS が全頭より少ない晩は試し読み: 巡回の統計だけ mode "probe" で残し、matched は None
    （カードラッシュは従来どおりカード名で探して保存する。on でも同じ）。
    """
    mode = mode or CARDRUSH_CATALOG_MODE
    loop_shops = list(loop_shops)
    if mode not in ("on", "shadow", "off"):
        print(f"  ⚠ CARDRUSH_CATALOG_MODE={mode!r} は分からない値です（shadow / on / off）。巡回しません")
        return loop_shops, None
    if mode == "off" or CARDRUSH not in available_shops:
        return loop_shops, None
    print(f"\n--- カードラッシュ 型番の頭の巡回（{mode}） ---")
    try:
        matched, catalog_stats = collect_cardrush_catalog([c["card_name"] for c in selected],
                                                          heads_limit=CARDRUSH_CATALOG_HEADS)
    except Exception as e:
        print(f"  ⚠⚠ カードラッシュ 型番の頭の巡回で予期しない例外: {e}。カードラッシュはカード名で探します")
        shop_stats[CARDRUSH] = {"ok": 0, "empty": 0, "error": 0, "mode": "error", "exception": str(e)}
        return loop_shops, None
    if matched is None and catalog_stats.get("probe"):
        print(f"  カードラッシュ 型番の頭の試し読み: {catalog_stats['heads_read']}/{catalog_stats['heads']}種・"
              f"{catalog_stats['pages']}ページ・合計{catalog_stats['page_sec_sum']}秒（遅いページ{catalog_stats['slow_pages']}）")
        shop_stats[CARDRUSH] = {"ok": 0, "empty": 0, "error": 0, "mode": "probe", "catalog": catalog_stats}
        return loop_shops, None
    if matched is None:
        print("  カードラッシュ 型番の頭の巡回が不成立（取得失敗・読み切れず）。カードラッシュはカード名で探します")
        shop_stats[CARDRUSH] = {"ok": 0, "empty": 0, "error": 0, "mode": "fallback", "catalog": catalog_stats}
        return loop_shops, None
    print(f"  カードラッシュ 型番の頭の巡回: 在庫あり{catalog_stats['listed']}商品・{catalog_stats['pages']}ページ・"
          f"重複{catalog_stats['duplicates']}・名寄せ{catalog_stats['matched_products']}商品")
    if mode == "shadow":
        shop_stats[CARDRUSH] = {"ok": 0, "empty": 0, "error": 0, "mode": "shadow", "catalog": catalog_stats}
        return loop_shops, matched
    shop_stats[CARDRUSH] = {"ok": 0, "empty": 0, "error": 0, "mode": "catalog", "catalog": catalog_stats}
    return [s for s in loop_shops if s != CARDRUSH], matched


def _fetch_cardrush_cards_with_rows(sb: Client, days: list[str]) -> dict[str, set[str]] | None:
    """days（JST の日付）ごとに、カードラッシュの行があったカード名（日付 → カード名の集合）。読めなければ None。

    _cardrush_rest_set の材料。price_history（ビュー）を店と日付で読む。並びは card_id, rarity_id, recorded_at
    （店を固定すれば一意なので range の境目で行が落ちない・重ならない）。11-01 のカードラッシュは5,170行・2,307枚。"""
    names: dict[str, set[str]] = {d: set() for d in days}
    offset = 0
    try:
        while True:
            resp = (sb.table("price_history").select("card_name, recorded_at")
                    .eq("shop", CARDRUSH).in_("recorded_at", days)
                    .order("card_id").order("rarity_id").order("recorded_at")
                    .range(offset, offset + _PAGE_SIZE - 1).execute())
            batch = resp.data or []
            for r in batch:
                names.setdefault(str(r.get("recorded_at"))[:10], set()).add(r["card_name"])
            if len(batch) < _PAGE_SIZE:
                return names
            offset += _PAGE_SIZE
    except Exception as e:
        print(f"  ⚠ カードラッシュ 直近の行を読めませんでした: {type(e).__name__}: {e}。今夜は検索を間引きません")
        return None


# 前の晩のカードラッシュが「平常に取れていた」とみなす基準（_cardrush_run_normal）。check_collection の見張りと同じ値
_CARDRUSH_NORMAL_MAX_ERROR = 0.03   # エラーの割合がこれ未満（check_collection.ERROR_RATIO）
_CARDRUSH_NORMAL_MIN_OK = 0.5       # 取れたカードの割合がこれ以上（11-01 は 2,851/3,387＝84%）。TODO: calibrate from data
# 行が保存されたカードの数が「取れた」カードの数のこれ以上（10-31 は 2,934/3,389＝87%・11-01 は 2,307/2,851＝81%）。
# 取れた（ok）は売り切れだけのカードも数えるので、在庫の欄の作りが変わって全部が在庫0と読まれた晩も ok は普段どおりになる。
# その晩は保存された行が減るので、ここで見分ける（reviewer 2回目 中1）
_CARDRUSH_NORMAL_MIN_SAVED = 0.5    # TODO: calibrate from data


def _cardrush_run_normal(run: dict | None) -> str | None:
    """1晩分の collection_runs の行で、カードラッシュをカード名で検索して平常に取れていたか。平常なら None、
    そうでなければ理由。行が無い晩・カードラッシュを飛ばした晩・連続失敗で問い合わせを止めた晩・エラーが多い晩・
    取れた割合が低い晩（作りの変化で全件0件など）・打ち切りの晩・巡回の行で保存した晩（on）は平常でない
    （行が無いのが「在庫なし」とは限らない。reviewer 高1）。"""
    if not run:
        return "記録が無い"
    if CARDRUSH in (run.get("skipped_shops") or {}):
        return "カードラッシュを飛ばした"
    stats = run.get("shop_stats") or {}
    if not isinstance(stats, dict) or stats.get("_aborted"):
        return "打ち切り"
    st = stats.get(CARDRUSH)
    if not isinstance(st, dict):
        return "カードラッシュの記録が無い"
    if st.get("mode") == "catalog":
        return "巡回の行で保存した"
    ok, empty, error = (int(st.get(k) or 0) for k in ("ok", "empty", "error"))
    attempted = ok + empty + error
    if attempted == 0:
        return "検索していない"
    if int(st.get("skipped") or 0) > 0:
        return "連続失敗で問い合わせを止めた"
    if error / attempted >= _CARDRUSH_NORMAL_MAX_ERROR:
        return f"エラーが多い（{error}/{attempted}）"
    if ok / attempted < _CARDRUSH_NORMAL_MIN_OK:
        return f"取れた割合が低い（{ok}/{attempted}）"
    return None


def _cardrush_day_normal(runs: list[dict], saved_cards: int) -> str | None:
    """1日分（同じ JST の日に始まった毎朝の収集の行すべて）でカードラッシュが平常だったか。平常なら None、
    そうでなければ理由。行が1つでも平常でなければ平常でない（朝の収集が異常で、あとで手で回した2回目が平常な日を
    平常と読まない。reviewer 2回目 中2）。保存されたカードの数が取れたカードの数に比べて少なすぎる日も平常でない。"""
    if not runs:
        return "記録が無い"
    for run in runs:
        reason = _cardrush_run_normal(run)
        if reason:
            return reason
    ok = sum(int(((r.get("shop_stats") or {}).get(CARDRUSH) or {}).get("ok") or 0) for r in runs)
    if ok and saved_cards < ok * _CARDRUSH_NORMAL_MIN_SAVED:
        return f"保存された行が少ない（{saved_cards}枚／取れた {ok}枚）"
    return None


def _fetch_recent_price_runs(sb: Client, days: list[str]) -> dict[str, list[dict]] | None:
    """days（JST の日付）の毎朝の収集の collection_runs の行（日付 → その日に始まった行すべて）。読めなければ None。"""
    try:
        since = (datetime.fromisoformat(min(days)) - timedelta(days=1)).date().isoformat()
        resp = (sb.table("collection_runs").select("started_at, skipped_shops, shop_stats")
                .eq("run_type", "prices").gte("started_at", since)
                .order("started_at", desc=True).limit(10).execute())
    except Exception as e:
        print(f"  ⚠ カードラッシュ 直近の収集の記録を読めませんでした: {type(e).__name__}: {e}。今夜は検索を間引きません")
        return None
    out: dict[str, list[dict]] = {}
    for run in resp.data or []:
        started = _parse_timestamp(run.get("started_at"))
        day = started.astimezone(JST).date().isoformat() if started else None
        if day in days:
            out.setdefault(day, []).append(run)
    return out


def _parse_timestamp(value) -> datetime | None:
    """DB の timestamptz の文字列（"2026-11-01T01:01:17.3456+00:00" など）を読む。読めなければ None。
    Postgres は秒の小数の末尾の0を落とし、Python 3.10 の fromisoformat は3桁・6桁以外を読めないので、6桁にそろえる
    （reviewer 低2）。タイムゾーンが無ければ JST とみなす。"""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1)
    text = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=JST)


def _cardrush_rests(card: dict, with_rows: set[str], today: str, rest_days: int) -> bool:
    """このカードのカードラッシュの検索を今夜は休むか（B1・2026-11-01）。

    休むのは、昨日たしかに集めた（last_collected_at が JST の昨日以降＝昨日の収集で行を保存した）のに、カードラッシュの
    行が昨日も一昨日も無かったカード（在庫なし・売り切れだけ。保存は売り切れの行を捨てるので、検索しても行は増えない）。
    昨日と一昨日のカードラッシュが平常に取れていたかは呼び出し側（_cardrush_rest_set）が確かめる。
    そういうカードも rest_days 日に1回は検索する（カード名の crc32 で日をずらして、毎晩の検索の数をならす）。
    入荷に気づくのは最大 rest_days−1 日遅れる。
    昨日の収集で処理されなかった（打ち切り）・保存0件だったカードは last_collected_at が古いので休まない。
    last_collected_at は DB から UTC（"2026-11-01T01:01:17.345629+00:00"）で返るので、JST の日付に直して比べる
    （先頭10文字だと、朝5〜9時に集めたカードが前の日になる）。読めない値なら休まない。"""
    if rest_days <= 1 or card["card_name"] in with_rows:
        return False
    last_dt = _parse_timestamp(card.get("last_collected_at"))
    if last_dt is None:
        return False
    yesterday = datetime.fromisoformat(today).date() - timedelta(days=1)
    if last_dt.astimezone(JST).date() < yesterday:
        return False
    day_no = datetime.fromisoformat(today).date().toordinal()
    return zlib.crc32(card["card_name"].encode("utf-8")) % rest_days != day_no % rest_days


def _cardrush_rest_set(sb: Client, selected: list[dict], today: str, rest_days: int | None = None,
                       never_rest: set[str] | None = None) -> tuple[set[str], dict]:
    """今夜カードラッシュの検索を休むカード名と統計（shop_stats["カードラッシュ"]["rest"]）。

    昨日・一昨日のどちらかでカードラッシュが平常に取れていなければ、今夜は間引かない（_cardrush_run_normal）。
    never_rest（新弾の収録カード）は休ませない（入荷直後の値を一番見たいカード。reviewer 中5）。"""
    rest_days = CARDRUSH_EMPTY_REST_DAYS if rest_days is None else rest_days
    stats = {"days": rest_days, "cards": 0, "read_failed": False, "not_normal": None}
    if rest_days <= 1:
        return set(), stats
    d = datetime.fromisoformat(today).date()
    days = [(d - timedelta(days=k)).isoformat() for k in (1, 2)]
    runs = _fetch_recent_price_runs(sb, days)
    if runs is None:
        stats["read_failed"] = True
        return set(), stats
    rows_by_day = _fetch_cardrush_cards_with_rows(sb, days)
    if rows_by_day is None:
        stats["read_failed"] = True
        return set(), stats
    for day in days:
        reason = _cardrush_day_normal(runs.get(day) or [], len(rows_by_day.get(day) or ()))
        if reason:
            stats["not_normal"] = f"{day}: {reason}"
            print(f"  カードラッシュ: {day} の収集が平常でない（{reason}）ため、今夜は検索を間引きません")
            return set(), stats
    with_rows = set().union(*rows_by_day.values())
    never_rest = never_rest or set()
    rest = {c["card_name"] for c in selected
            if c["card_name"] not in never_rest and _cardrush_rests(c, with_rows, today, rest_days)}
    stats["cards"] = len(rest)
    print(f"  カードラッシュ: 直近2晩に行の無かった {len(rest)}枚は今夜の検索を休みます（{rest_days}日に1回だけ検索）")
    return rest, stats


def _compare_kanabell_shadow(processed: list[dict], matched: dict[str, list[dict]], sink: dict,
                             today: str) -> dict:
    """mode "shadow": カーナベルの巡回の行と検索の行を比べる（_compare_catalog_shadow）。"""
    return _compare_catalog_shadow(KANABELL, processed, matched, sink, today)


def _compare_catalog_shadow(shop: str, processed: list[dict], matched: dict[str, list[dict]], sink: dict,
                            today: str, in_stock_only: bool = False) -> dict:
    """mode "shadow": 巡回の行と、同じ晩のループでカード名の検索から得たその店の行を比べる（DB は読まない）。
    カーナベル・カードラッシュで共有する。in_stock_only=True（カードラッシュ。巡回は在庫ありの範囲だけ読む）の
    ときは、検索の行も在庫ありだけにして比べる（売り切れの行は保存されないので、比べても意味が無い）。

    以下はカーナベルで書いた説明（カードラッシュも同じ形）。

    sink は collect_and_save の kanabell_sink（カード → カーナベルの行、取得に失敗したら None）。取得に失敗した
    カード・ループで処理されなかったカードは比べず数えるだけ。巡回は収集の始め・検索は最大5時間後の値なので、
    その間の売買も食い違いに見える。比べは2段（reviewer 中3）:
      - 商品（URL）: 巡回だけ・検索だけ・両方。両方にある商品で値段か在庫が違う数（売買の雑音の大きさ）
      - 保存と同じ (カード, レアリティ) の最安の行（build_min_price_rows）: 同じ / 巡回が高い / 巡回が安い /
        上下混在 / 巡回だけ / 検索だけ（トレコロの shadow と同じ分け方）
    種類ごとに少しずつ、商品名つきで例を残す（collection_runs.shop_stats["カーナベル"]["shadow"]。on への切り替えの
    判断材料。巡回だけの商品に別のカードの名寄せ違いが混ざっていないかを見る）。
    検索は商品を最大150件までしか引かず、ダッシュの表記が違う商品を取りこぼすことがある（2026-11-01 の突き合わせで
    閃刀姫－アザレア・テンペランス: 検索は1商品・巡回は6商品）ので、巡回だけの商品・巡回が安い行は一定数出る。"""
    stats = {"products_both": 0, "products_only_catalog": 0, "products_only_search": 0,
             "products_value_differ": 0,
             "same": 0, "catalog_higher": 0, "catalog_lower": 0, "mixed": 0, "only_catalog": 0, "only_search": 0,
             "search_failed_cards": 0, "not_searched_cards": 0, "examples": []}
    per_kind: dict[str, int] = {}

    def _example(kind, **fields):
        if per_kind.get(kind, 0) < _SHADOW_EXAMPLES_PER_KIND:
            per_kind[kind] = per_kind.get(kind, 0) + 1
            stats["examples"].append({"kind": kind, **fields})

    for card_data in processed:
        name = card_data["card_name"]
        if name not in sink:
            stats["not_searched_cards"] += 1
            continue
        if sink[name] is None:
            stats["search_failed_cards"] += 1
            continue
        # 検索の行は compare_prices の中で OF の印のそろえを通っている。巡回の行も同じに通してから比べる
        # （通さないと OF の商品が「巡回だけ」と「検索だけ」の偽の組になる。11-01 の巡回で79商品名。reviewer 2回目 低1）
        search_rows, cat_rows = sink[name], apply_overframe(matched.get(name, []), None)
        if in_stock_only:
            search_rows = [r for r in search_rows if not r.get("sold_out")]

        def by_url(rows):
            out: dict[str, dict] = {}
            for r in rows:
                out.setdefault(r["url"], {"name": r["name"], "rarity": r["rarity"], "vals": set()})["vals"].add(
                    (r["condition"], r["price"], r["stock"]))
            return out

        s_urls, c_urls = by_url(search_rows), by_url(cat_rows)
        for url in sorted(set(s_urls) | set(c_urls)):
            a, b = c_urls.get(url), s_urls.get(url)
            if a and b:
                stats["products_both"] += 1
                if a["vals"] != b["vals"]:
                    stats["products_value_differ"] += 1
            elif a:
                stats["products_only_catalog"] += 1
                _example("product_only_catalog", card=name, url=url, name=a["name"], rarity=a["rarity"])
            else:
                stats["products_only_search"] += 1
                _example("product_only_search", card=name, url=url, name=b["name"], rarity=b["rarity"])

        cat = {r["rarity"]: r for r in build_min_price_rows(name, cat_rows, today) if r["shop"] == shop}
        srch = {r["rarity"]: r for r in build_min_price_rows(name, search_rows, today) if r["shop"] == shop}
        for rarity in sorted(set(cat) | set(srch)):
            a, b = cat.get(rarity), srch.get(rarity)
            if a and not b:
                kind = "only_catalog"
            elif b and not a:
                kind = "only_search"
            else:
                diffs = [a[c] - b[c] for c in ("min_price", "min_price_any")]
                if not any(diffs):
                    stats["same"] += 1
                    continue
                kind = ("catalog_higher" if all(d >= 0 for d in diffs) else
                        "catalog_lower" if all(d <= 0 for d in diffs) else "mixed")
            stats[kind] += 1
            _example(kind, card=name, rarity=rarity,
                     catalog=[a["min_price"], a["min_price_any"]] if a else None,
                     search=[b["min_price"], b["min_price_any"]] if b else None)
    print(f"  {shop} 巡回とカード名の検索の比べ: 商品 両方{stats['products_both']}（値の違い"
          f"{stats['products_value_differ']}）/ 巡回だけ{stats['products_only_catalog']} / 検索だけ"
          f"{stats['products_only_search']}。最安の行 同じ{stats['same']} / 巡回が高い{stats['catalog_higher']} / "
          f"巡回が安い{stats['catalog_lower']} / 上下混在{stats['mixed']} / 巡回だけ{stats['only_catalog']} / "
          f"検索だけ{stats['only_search']}（検索失敗{stats['search_failed_cards']}枚・未処理{stats['not_searched_cards']}枚）")
    return stats


def _fetch_torecolo_prev_rows(sb: Client, day: str) -> dict[str, dict[str, tuple]] | None:
    """day（JST の日付）のトレコロの行を (カード名 → レアリティ → (min_price, min_price_any)) で返す。読めなければ None。

    mode "on" で、巡回の読み落としが疑われるカードをカード名で検索し直すかの判定に使う（_torecolo_card_plan）。
    price_history（ビュー）を店と日付の等号で読む。並びは card_id, rarity_id（1日・1店の中では一意なので
    range の境目で行が落ちない・重ならない）。2026-11-01 本番の実測: 10-31 のトレコロは7,902行・1ページ約0.7秒。"""
    prev: dict[str, dict[str, tuple]] = {}
    offset = 0
    try:
        while True:
            resp = (sb.table("price_history").select("card_name, rarity, min_price, min_price_any")
                    .eq("shop", TORECOLO).eq("recorded_at", day)
                    .order("card_id").order("rarity_id")
                    .range(offset, offset + _PAGE_SIZE - 1).execute())
            batch = resp.data or []
            for r in batch:
                prev.setdefault(r["card_name"], {})[r["rarity"]] = (r.get("min_price"), r.get("min_price_any"))
            if len(batch) < _PAGE_SIZE:
                return prev
            offset += _PAGE_SIZE
    except Exception as e:
        print(f"  ⚠ トレコロ 前日（{day}）の行を読めませんでした: {type(e).__name__}: {e}。今夜は検索し直しをしません")
        return None


def _torecolo_looks_missed(prev: dict[str, tuple], rows: list[dict]) -> bool:
    """前日のトレコロの行（レアリティ → (min_price, min_price_any)）と巡回から作った今日の行を比べ、読み落としが疑われるか。

    巡回は1系列を最大約3分かけて読むので、読んでいる途中に前のページの商品が売れると、後ろの商品が1つ前のページへ
    ずれて読まれないことがある（どの商品が落ちたかは一覧からは分からない）。落ちたのが
      - そのレアリティの唯一の商品 → 前日にあったレアリティが今日は無い
      - 最安の商品 → min_price か min_price_any が前日より上がる（偽の値上がり）
      - 通常品だけ（キズありが残る） → 前日は通常品がキズありより高く並んでいたのに、今日は通常品が無い
        （min_price は通常品が無いと min_price_any になる。build_min_price_rows）
    のどれかに見える。本当に売れた・値上がりしたカードも含むが、それはカード名の検索で同じ値が取れるだけ。
    2026-11-01 本番の 10-31→11-01 のトレコロの行で数えると、前日に行のあった3,285枚のうち76枚。
    残る穴: 前日が通常品だけで今日がキズありだけ（値下がりに見える）は分からない。"""
    cur = {r["rarity"]: (r["min_price"], r["min_price_any"]) for r in rows}
    for rarity, (pm, pa) in prev.items():
        if rarity not in cur:
            return True
        cm, ca = cur[rarity]
        if None in (pm, pa, cm, ca):
            continue
        if cm > pm or ca > pa or (pm > pa and cm == ca):
            return True
    return False


def _torecolo_card_plan(card_name: str, loop_shops: list[str], matched: dict[str, list[dict]],
                        prev_rows: dict[str, dict[str, tuple]] | None, today: str
                        ) -> tuple[list[str], list[dict] | None, bool]:
    """mode "on" のとき、1枚のカードでトレコロをどう扱うかを決める。戻り値は (この回の店, 巡回の商品, 検索し直すか)。

    巡回の行が前日の行と比べて読み落としに見えるカード（_torecolo_looks_missed）は、トレコロもカード名で検索する。
    巡回の商品も一緒に渡し、同じ URL の商品は検索の値を使う（compare_prices）。カード名の検索に出ないキズあり品
    （NCF1-JP015-K）のレアリティをその晩だけ落とさず、検索に失敗した晩も巡回の値が残る（reviewer 2回目 低2）。トレコロの行のレアリティ・値はほかの店の行では
    変わらない（OF のそろえで変わるのはカードラボの行だけ。トレコロの行は商品名の印だけで決まるので apply_overframe を
    文脈なしで通して保存時とそろえる）ので、取得の前に決められる。
    前日の行が読めなかった晩（prev_rows が None）・前日の行が無いカードは検索し直さない。
    巡回だけにある（前日に無い）レアリティでは検索し直さない（カード名の検索に出ないキズあり品があるため。
    2026-11-01 自宅で NCF1-JP015-K・在庫ありが検索の結果に無いことを確認）。"""
    products = matched.get(card_name, [])
    prev = (prev_rows or {}).get(card_name)
    if prev:
        rows = [r for r in build_min_price_rows(card_name, apply_overframe(products, None), today)
                if r["shop"] == TORECOLO]
        if _torecolo_looks_missed(prev, rows):
            return [*loop_shops, TORECOLO], products, True
    return loop_shops, products, False


_SHADOW_EXAMPLES_PER_KIND = 5
# on で検索し直したカードの名前を collection_runs に残す数
_RECHECK_NAMES_KEPT = 30


def _compare_torecolo_shadow(processed: list[dict], matched: dict[str, list[dict]], sink: dict, today: str,
                             art_contexts: dict[str, dict] | None = None) -> dict:
    """mode "shadow": 巡回の値と、同じ晩のループでカード名の検索から得たトレコロの結果を (カード, レアリティ) ごとに比べる。

    DB は読まない（同じ日にライブ検索・新弾の収集が書いた行が混ざるため。reviewer 中1）。sink は collect_and_save の
    torecolo_sink（カード → トレコロの商品、取得に失敗したら None）。取得に失敗したカード・ループで処理されなかった
    カードは比べず数えるだけ。巡回は 5:00 頃・カード名の検索は最大5時間後の値なので、その間の売買も食い違いに見える。
    同じ / 巡回が高い / 巡回が安い / 上下が混ざる（min_price_any と min_price で向きが違う）/ 巡回だけ / 検索だけ を数え、
    種類ごとに少しずつ例を残す（collection_runs.shop_stats["トレコロCB"]["shadow"]。切り替えの判断材料）。"""
    stats = {"same": 0, "catalog_higher": 0, "catalog_lower": 0, "mixed": 0, "only_catalog": 0, "only_search": 0,
             "search_failed_cards": 0, "not_searched_cards": 0, "examples": []}
    per_kind: dict[str, int] = {}

    def _example(kind, card, rarity, a, b):
        if per_kind.get(kind, 0) < _SHADOW_EXAMPLES_PER_KIND:
            per_kind[kind] = per_kind.get(kind, 0) + 1
            stats["examples"].append({"kind": kind, "card": card, "rarity": rarity,
                                      "catalog": [a["min_price"], a["min_price_any"]] if a else None,
                                      "search": [b["min_price"], b["min_price_any"]] if b else None})

    compared = []
    for card_data in processed:
        name = card_data["card_name"]
        if name not in sink:
            stats["not_searched_cards"] += 1
        elif sink[name] is None:
            stats["search_failed_cards"] += 1
        else:
            compared.append(card_data)
    _, _, cat_rows, _ = _build_catalog_rows(compared, matched, today, art_contexts)
    cat = {(r["card_name"], r["rarity"]): r for r in cat_rows if r["shop"] == TORECOLO}
    search = {}
    for card_data in compared:
        name = card_data["card_name"]
        for r in build_min_price_rows(name, sink[name], today):
            if r["shop"] == TORECOLO:
                search[(name, r["rarity"])] = r

    for key in sorted(set(cat) | set(search)):
        a, b = cat.get(key), search.get(key)
        if a and not b:
            kind = "only_catalog"
        elif b and not a:
            kind = "only_search"
        else:
            diffs = [a[c] - b[c] for c in ("min_price", "min_price_any")]
            if not any(diffs):
                stats["same"] += 1
                continue
            kind = ("catalog_higher" if all(d >= 0 for d in diffs) else
                    "catalog_lower" if all(d <= 0 for d in diffs) else "mixed")
        stats[kind] += 1
        _example(kind, key[0], key[1], a, b)
    print(f"  トレコロ 巡回とカード名の検索の比べ: 同じ{stats['same']} / 巡回が高い{stats['catalog_higher']} / "
          f"巡回が安い{stats['catalog_lower']} / 上下混在{stats['mixed']} / 巡回だけ{stats['only_catalog']} / "
          f"検索だけ{stats['only_search']}（検索失敗{stats['search_failed_cards']}枚・未処理{stats['not_searched_cards']}枚）")
    return stats


def collect_and_save(sb: Client, card_name: str, today: str, shop_names: list[str],
                     shop_stats: dict | None = None,
                     db_guard: ConsecutiveDbFailureGuard | None = None,
                     art_contexts: dict[str, dict] | None = None,
                     torecolo_sink: dict | None = None,
                     torecolo_products: list[dict] | None = None,
                     kanabell_products: list[dict] | None = None,
                     kanabell_sink: dict | None = None,
                     cardrush_products: list[dict] | None = None,
                     cardrush_sink: dict | None = None) -> int:
    """1枚のカードの価格を取得してSupabaseに保存。保存した行数を返す。

    torecolo_sink に dict を渡すと、このカードのトレコロの検索結果（取得に失敗したら None）を残す
    （トレコロの在庫一覧の巡回の shadow で、巡回の値と比べるため。_compare_torecolo_shadow）。

    torecolo_products に list を渡すと（トレコロの在庫一覧の巡回の on）、トレコロは検索せずにこの商品を使う。
    ほかの店の行と合わせてから型番の補完（infer_codes）・OF のそろえをかけ、同じ行として保存する。
    カードラボの巡回の保存に渡す判定材料（art_contexts）にもトレコロの行が入る。shop_stats では
    商品が1件以上なら取得、0件なら0件に数える（巡回は全系列を読み切った晩だけ使うので取得失敗は無い）。

    kanabell_products に list を渡すと（カーナベルの全件巡回の on）、カーナベルは検索せずにこの行を使う
    （torecolo_products と同じ。両方あれば合わせて渡す）。kanabell_sink に dict を渡すと（shadow）、このカードの
    カーナベルの検索の行（取得に失敗したら None）を残す（ループの後で _compare_kanabell_shadow が巡回の行と比べる）。
    cardrush_products / cardrush_sink はカードラッシュの型番の頭の巡回の on / shadow で、カーナベルと同じ。

    shop_stats に dict を渡すと、店舗ごとの 取得成功/0件/取得エラー を集計する。
    「0件＝在庫なし」と「取得失敗（接続エラー・セレクタ破損等）」を区別するため。

    db_guard を渡すと、保存（upsert）が例外で失敗した回数を数え、続けて上限回数
    失敗したら DbWriteAborted を送出する（呼び元で収集を打ち切る。db_write_guard.py）。
    """
    status: dict = {}
    try:
        extra_results = None
        if torecolo_products is not None or kanabell_products is not None or cardrush_products is not None:
            extra_results = [*(torecolo_products or []), *(kanabell_products or []), *(cardrush_products or [])]
        results = compare_prices(card_name, shop_names=shop_names, status_out=status,
                                 **({"extra_results": extra_results} if extra_results is not None else {}))
    except Exception as e:
        print(f"  スクレイピング失敗 [{card_name}]: {e}")
        return 0
    if torecolo_products is not None and TORECOLO not in status:
        # トレコロも検索したとき（巡回の読み落としの疑い）は、検索の取得状況で数える
        status[TORECOLO] = {"count": len(torecolo_products), "fetch_errors": 0}
    if kanabell_products is not None and KANABELL not in status:
        status[KANABELL] = {"count": len(kanabell_products), "fetch_errors": 0}
    if cardrush_products is not None and CARDRUSH not in status:
        status[CARDRUSH] = {"count": len(cardrush_products), "fetch_errors": 0}

    # カードラボのカタログ巡回（後で別に保存する）の OF の判定に使う。保存できなかった（ほかの店が全部売り切れ等）
    # カードでも残す。早く return すると、その夜だけカードラボの行が寄らず札が揺れる（10-30 reviewer 指摘）
    of_ctx = overframe_context(results or [])
    if art_contexts is not None and of_ctx:
        art_contexts[card_name] = {"rarities": set(), "alt_codes": set(), "overframe": of_ctx}

    had_error = False
    for shop, st in status.items():
        failed = bool(st.get("exception")) or (st.get("fetch_errors", 0) > 0 and st.get("count", 0) == 0)
        if failed:
            had_error = True
        if shop_stats is not None:
            agg = shop_stats.setdefault(shop, {"ok": 0, "empty": 0, "error": 0})
            if failed:
                agg["error"] += 1
            elif st.get("count", 0) == 0:
                agg["empty"] += 1
            else:
                agg["ok"] += 1

    if kanabell_sink is not None and KANABELL in shop_names:
        st = status.get(KANABELL) or {}
        kanabell_failed = (not st) or bool(st.get("exception")) or st.get("fetch_errors", 0) > 0
        kanabell_sink[card_name] = None if kanabell_failed else [
            r for r in (results or []) if r.get("shop") == KANABELL]

    if cardrush_sink is not None and CARDRUSH in shop_names:
        st = status.get(CARDRUSH) or {}
        cardrush_failed = (not st) or bool(st.get("exception")) or st.get("fetch_errors", 0) > 0
        cardrush_sink[card_name] = None if cardrush_failed else [
            r for r in (results or []) if r.get("shop") == CARDRUSH]

    if torecolo_sink is not None and TORECOLO in shop_names:
        st = status.get(TORECOLO) or {}
        torecolo_failed = (not st) or bool(st.get("exception")) or st.get("fetch_errors", 0) > 0
        torecolo_sink[card_name] = None if torecolo_failed else [
            r for r in (results or []) if r.get("shop") == TORECOLO]

    if not results:
        if had_error:
            print(f"  取得失敗 [{card_name}]（店舗側エラー — 在庫なしとは区別）")
        else:
            print(f"  結果なし [{card_name}]")
        return 0

    # 行構築は price_persist.build_min_price_rows に集約
    rows = build_min_price_rows(card_name, results, today)
    if not rows:
        print(f"  有効な価格なし [{card_name}]")
        return 0

    # UNIQUE(card_name, shop, rarity, recorded_at) 制約に基づく upsert。
    # 同日に複数回呼ばれても重複行が増えず last-write-wins になる。
    try:
        saved = upsert_price_rows(sb, rows, raise_on_error=db_guard is not None)
    except Exception as e:
        print(f"  DB保存失敗 [{card_name}]")
        # 4xx など DB が応答しない系以外の失敗もここで数える（保存の仕組みが壊れたときも止めるため）。
        # DB が応答しない系は、同じ見張り役の見張り付きクライアント（GuardedSupabase）が数え済みなので重ねて数えない
        if not (is_db_unavailable(e) and getattr(sb, "guard", None) is db_guard):
            db_guard.record_failure(e)  # 上限に達したら DbWriteAborted が上がる
        return 0
    if db_guard is not None:
        db_guard.record_success()
    # 絵が分かれるカード×レアリティだけ、絵ごとの内訳も別表へ（失敗しても本表の記録は数えない・止めない）
    upsert_price_art_rows(sb, build_art_price_rows(card_name, results, today))
    # カードラボのカタログ巡回（後で別に保存する）の判定をそろえるために、分かったことを残す
    if art_contexts is not None:
        ctx = art_price_context(card_name, results)
        ctx["overframe"] = of_ctx
        if ctx["rarities"] or ctx["alt_codes"] or ctx["overframe"]:
            art_contexts[card_name] = ctx
    if saved:
        print(f"  保存完了 [{card_name}]: {saved}件")
        return saved
    print(f"  DB保存失敗 [{card_name}]")
    return 0


# ── カード名の名寄せ（フェーズ3 P1）──────────────────────────────────
# 経路A（本ファイルの sync_*系）は従来 normalize_card_name（全半角・ダッシュ統一のみ）
# しか通しておらず、中黒ゆれ等（例:「ブラックマジシャン」「ブラック・マジシャン」）が
# 別カードとして tracked_cards に分裂登録されていた。ここで cardnames_ja.json 照合つきの
# canonicalize_card_name（name_normalize.py、app._correct_cardname と共通ロジック）を
# 登録前に通し、正式名に解決してから existing チェック・insert を行う。
# 挙動: 完全一致→そのまま / fuzzy一意一致→正式名 / fuzzy衝突→補正せず入力のまま＋警告
#       （別カードへの誤合流を避ける。P6a と同じ安全側動作）/ 未知→normalize_card_name のみ。
_cardnames_set: set[str] = set()
_cardnames_fuzzy: dict[str, list[str]] = {}
_cardnames_index_loaded = False


def _ensure_cardnames_index() -> None:
    """data/cardnames_ja.json を初回参照時に遅延ロードする（app.py 起動には依存しない）。"""
    global _cardnames_set, _cardnames_fuzzy, _cardnames_index_loaded
    if _cardnames_index_loaded:
        return
    _, _cardnames_set, _cardnames_fuzzy = load_cardnames_index()
    _cardnames_index_loaded = True


def canonicalize_tracked_name(name: str) -> str:
    """tracked_cards への登録前にカード名を正式名へ解決する（フェーズ3 P1）。"""
    _ensure_cardnames_index()
    resolved, _ = canonicalize_card_name(
        name, _cardnames_set, _cardnames_fuzzy,
        warn=lambda msg: print(f"  {msg}"), context="collect_prices",
    )
    return resolved


def _insert_new_cards(sb: Client, new_cards: list[dict], label: str) -> None:
    """new_cards を tracked_cards に登録してログを出す。

    insert ではなく upsert(ignore_duplicates) を使う: existing 集合とDBに
    わずかな差があっても、1枚の重複でバッチ全体が 23505 で全滅しないように。
    """
    if not new_cards:
        print(f"  新規カードなし")
        return
    try:
        resp = sb.table("tracked_cards").upsert(
            new_cards, on_conflict="card_name", ignore_duplicates=True
        ).execute()
        inserted = resp.data or []
        print(f"  新規追加: {len(inserted)}枚（候補{len(new_cards)}枚中）")
        for c in inserted[:10]:
            print(f"    + {c['card_name']}")
        if len(inserted) > 10:
            print(f"    ... 他{len(inserted) - 10}枚")
    except Exception as e:
        print(f"  カード追加失敗 ({label}): {e}")


# ── 注目カード同期（各関数は全該当カードの set[str] を返す） ──

def sync_latest_packs(sb: Client) -> tuple[set[str], set[str]]:
    """最新弾のカードを追加。(全最新弾カード集合, 今回新規追加分) を返す。"""
    print("\n--- 最新弾カード同期 ---")

    # 収集用: カードリスト未掲載（発売前など）のパックは除外し、枠を消費させない
    packs = get_pack_list(include_empty=False)
    if not packs:
        print("  パック情報を取得できませんでした")
        return set(), set()

    existing = _fetch_all_tracked_names(sb)

    all_pack_cards: set[str] = set()
    new_cards = []
    seen_new: set[str] = set()

    for pack in packs:
        wiki_page = _try_wiki_page_variants(pack["name"])
        result = fetch_pack_cards(pack["name"], wiki_page=wiki_page)
        print(f"  {pack['name']}: {result['count']}枚")

        for card in result.get("cards", []):
            normalized = canonicalize_tracked_name(normalize_card_name(card))
            all_pack_cards.add(normalized)
            if normalized not in existing and normalized not in seen_new:
                seen_new.add(normalized)
                new_cards.append({"card_name": normalized, "active": True})

    _insert_new_cards(sb, new_cards, "最新弾")
    newly_added = {c["card_name"] for c in new_cards}
    return all_pack_cards, newly_added


def sync_meta_decks(sb: Client) -> set[str]:
    """環境デッキの主要カード（採用率30%以上）を追加。全該当カード集合を返す。"""
    print("\n--- 環境デッキカード同期 ---")

    tiers = fetch_tier_list(force=True)
    if not tiers:
        print("  Tier表を取得できませんでした")
        return set()

    existing = _fetch_all_tracked_names(sb)

    hot_meta: set[str] = set()
    new_cards = []
    seen_new: set[str] = set()
    target = [t for t in tiers if t.get("tier", 99) <= 4]
    print(f"  対象テーマ: {len(target)}件（Tier1〜4）")

    for theme in target:
        deck = fetch_deck_cards(theme["name"], force=True)
        for card in deck.get("cards", []):
            name = canonicalize_tracked_name(normalize_card_name(card["name"]))
            if card.get("adoption", 0) >= 30.0:
                hot_meta.add(name)
                if name not in existing and name not in seen_new:
                    seen_new.add(name)
                    new_cards.append({"card_name": name, "active": True})

    _insert_new_cards(sb, new_cards, "環境デッキ")
    return hot_meta


def fetch_regulation_cards() -> list[str]:
    """リミットレギュレーション対象カード（禁止・制限・準制限）を取得。

    2026-07: 公式イベントページ（yugioh-card.com/japan/event/limitregulation/）から
    カード表が撤去されて全損したため、公式カードDBの禁止制限ページへ移行。
    日本語名は request_locale=ja で取得する。
    """
    import requests
    from bs4 import BeautifulSoup

    url = ("https://www.db.yugioh-card.com/yugiohdb/forbidden_limited.action"
           "?request_locale=ja")
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    try:
        res = requests.get(url, headers=headers, timeout=15)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
    except Exception as e:
        print(f"  リミットレギュレーション取得失敗: {e}")
        return []

    cards: list[str] = []
    seen: set[str] = set()
    # 解除（list_release_of_restricted）は3枚解禁で規制の文脈から外れるため対象外（従来同等）
    for section_id in ("list_forbidden", "list_limited", "list_semi_limited"):
        section = soup.select_one(f"#{section_id}")
        if section is None:
            print(f"  セクションが見つかりません: {section_id}")
            continue
        for el in section.select(".card_name"):
            text = el.get_text(strip=True)
            if text and text not in seen:
                seen.add(text)
                cards.append(text)

    return cards


def sync_regulation(sb: Client) -> set[str]:
    """リミットレギュレーション対象カードを追加。全該当カード集合を返す。"""
    print("\n--- リミットレギュレーション同期 ---")

    cards = fetch_regulation_cards()
    if not cards:
        print("  カードを取得できませんでした")
        return set()

    print(f"  規制対象カード: {len(cards)}枚")

    existing = _fetch_all_tracked_names(sb)

    regulation_set: set[str] = set()
    new_cards = []
    seen_new: set[str] = set()

    for card in cards:
        name = canonicalize_tracked_name(normalize_card_name(card))
        regulation_set.add(name)
        if name not in existing and name not in seen_new:
            seen_new.add(name)
            new_cards.append({"card_name": name, "active": True})

    _insert_new_cards(sb, new_cards, "規制")
    return regulation_set


def sync_searched_cards(sb: Client, recent_days: int = 7, min_count: int = 2) -> set[str]:
    """検索ログ（単体・デッキ）から頻繁に検索されたカードを追加。全該当カード集合を返す。"""
    print(f"\n--- 検索ログからカード同期（{recent_days}日/{min_count}回以上）---")

    since = (datetime.now(JST).date() - timedelta(days=recent_days)).isoformat()

    # 単体検索ログ
    try:
        resp = sb.table("search_logs") \
            .select("card_name") \
            .gte("searched_at", since) \
            .execute()
    except Exception as e:
        print(f"  検索ログ取得失敗: {e}")
        return set()

    counts = Counter(row["card_name"] for row in (resp.data or []))

    # デッキ検索ログ（テーブル未作成時はスキップ）
    try:
        deck_resp = sb.table("deck_search_logs") \
            .select("card_name") \
            .gte("searched_at", since) \
            .execute()
        counts.update(row["card_name"] for row in (deck_resp.data or []))
        print(f"  デッキ検索ログ: {len(deck_resp.data or [])}件")
    except Exception as e:
        print(f"  デッキ検索ログ取得スキップ: {e}")

    if not counts:
        print("  検索ログなし")
        return set()

    qualifying = {name for name, cnt in counts.items() if cnt >= min_count}
    print(f"  直近{recent_days}日間: {len(counts)}種, うち{min_count}回以上: {len(qualifying)}種")

    if not qualifying:
        return set()

    existing = _fetch_all_tracked_names(sb)

    new_cards = []
    seen_new: set[str] = set()
    for name in qualifying:
        normalized = canonicalize_tracked_name(normalize_card_name(name))
        if normalized not in existing and normalized not in seen_new:
            seen_new.add(normalized)
            new_cards.append({"card_name": normalized, "active": True})

    _insert_new_cards(sb, new_cards, "検索ログ")
    return {canonicalize_tracked_name(normalize_card_name(n)) for n in qualifying}


# ── 周辺カード同期（tracked_cards に追加するが注目集合には加えない） ──

def sync_recipe_decks(sb: Client) -> None:
    """入賞デッキレシピの全カードを監視対象に追加（周辺カード枠）"""
    print("\n--- 入賞レシピ全カード同期 ---")

    # sync_meta_decks が直前で force=True 実行済みのためキャッシュを流用（HTTP追加ゼロ）
    tiers = fetch_tier_list(force=False)
    if not tiers:
        print("  Tier表なし")
        return

    existing = _fetch_all_tracked_names(sb)

    target = [t for t in tiers if t.get("tier", 99) <= 4]
    new_cards = []
    seen_new: set[str] = set()

    for theme in target:
        deck = fetch_deck_cards(theme["name"], force=False)
        for c in deck.get("full_deck", []):
            name = canonicalize_tracked_name(normalize_card_name(c["name"]))
            if name and name not in existing and name not in seen_new:
                seen_new.add(name)
                new_cards.append({"card_name": name, "active": True})

    _insert_new_cards(sb, new_cards, "入賞レシピ")


def sync_theme_wiki_cards(sb: Client) -> None:
    """環境テーマのWikiページから全関連カードを追加（旧カード含む、周辺カード枠）"""
    print("\n--- テーマWiki全カード同期 ---")

    tiers = fetch_tier_list(force=False)
    if not tiers:
        print("  Tier表なし")
        return

    existing = _fetch_all_tracked_names(sb)

    target = [t for t in tiers if t.get("tier", 99) <= 4]
    new_cards = []
    seen_new: set[str] = set()

    for theme in target:
        theme_name = theme["name"]
        time.sleep(0.5)
        wiki_cards = fetch_theme_cards(theme_name)
        print(f"  【{theme_name}】: {len(wiki_cards)}枚検出")
        for card in wiki_cards:
            name = canonicalize_tracked_name(normalize_card_name(card))
            if name and name not in existing and name not in seen_new:
                seen_new.add(name)
                new_cards.append({"card_name": name, "active": True})

    _insert_new_cards(sb, new_cards, "テーマWiki")


def sync_remake_themes(sb: Client, new_pack_cards: set[str]) -> None:
    """新弾カードの所属テーマから旧カード群を追加（リメイク高騰の先回り、周辺カード枠）"""
    if not new_pack_cards:
        return

    # 新規カードが多い場合は先頭30枚に絞る（Wiki呼び出し時間の抑制）
    targets = list(new_pack_cards)[:30]
    print(f"\n--- リメイクテーマ逆引き同期（新規{len(new_pack_cards)}枚中{len(targets)}枚対象）---")

    existing = _fetch_all_tracked_names(sb)

    # 新弾カードからテーマを逆引き
    theme_set: set[str] = set()
    for card_name in targets:
        time.sleep(0.5)
        themes = fetch_card_themes(card_name)
        theme_set.update(themes)

    if not theme_set:
        print("  テーマ検出なし")
        return

    print(f"  検出テーマ: {len(theme_set)}件")

    new_cards = []
    seen_new: set[str] = set()
    for theme in theme_set:
        time.sleep(0.5)
        theme_cards = fetch_theme_cards(theme)
        for name_raw in theme_cards:
            name = canonicalize_tracked_name(normalize_card_name(name_raw))
            if name and name not in existing and name not in seen_new:
                seen_new.add(name)
                new_cards.append({"card_name": name, "active": True})

    _insert_new_cards(sb, new_cards, "リメイク逆引き")


def sync_trending_cards(sb: Client) -> None:
    """外部値上がりランキングから未追跡カードを追加（ベストエフォート、周辺カード枠）"""
    print("\n--- 外部値上がりランキング同期 ---")

    try:
        from trending_scraper import fetch_trending_cards
        cards = fetch_trending_cards()
    except Exception as e:
        print(f"  取得失敗（スキップ）: {e}")
        return

    if not cards:
        print("  取得カードなし")
        return

    print(f"  ランキング取得: {len(cards)}枚")

    existing = _fetch_all_tracked_names(sb)

    new_cards = []
    seen_new: set[str] = set()
    for card in cards:
        name = canonicalize_tracked_name(normalize_card_name(card))
        if name and name not in existing and name not in seen_new:
            seen_new.add(name)
            new_cards.append({"card_name": name, "active": True})

    _insert_new_cards(sb, new_cards, "外部ランキング")


def save_pack_list(sb: Client):
    """パック一覧をSupabaseに保存（Webアプリのフォールバック用）"""
    print("\n--- パック一覧保存 ---")
    packs = get_pack_list()
    if not packs:
        print("  パック一覧が空のためスキップ")
        return

    try:
        sb.table("pack_list").delete().neq("id", 0).execute()
        rows = [{"name": p["name"], "wiki_page": p.get("wiki_page", ""),
                 "tcg_name": p.get("tcg_name", ""), "release_date": p.get("date", "")}
                for p in packs]
        sb.table("pack_list").insert(rows).execute()
        print(f"  保存完了: {len(rows)}件")
        for p in packs:
            print(f"    {p['name']}")
    except Exception as e:
        print(f"  パック一覧保存失敗: {e}")


def send_daily_report(
    sb: Client, today: str,
    success_count: int, fail_count: int,
    cutoff_count: int = 0, elapsed_min: float = 0.0,
    shop_stats: dict | None = None,
):
    """前日の利用状況をDiscordに日次レポートとして送信"""
    import requests as _req
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL未設定 — 日次レポートをスキップ")
        return

    print("\n=== 日次レポート送信 ===")

    yesterday = (datetime.now(JST).date() - timedelta(days=1)).isoformat()
    try:
        resp = sb.table("search_logs") \
            .select("card_name") \
            .gte("searched_at", yesterday) \
            .lt("searched_at", today) \
            .execute()
        logs = resp.data or []
    except Exception as e:
        print(f"検索ログ取得失敗: {e}")
        logs = []

    total = len(logs)
    counts = Counter(r["card_name"] for r in logs)
    unique = len(counts)
    top5 = counts.most_common(5)
    top5_lines = "\n".join(
        f"  {i+1}. {name}（{cnt}回）" for i, (name, cnt) in enumerate(top5)
    ) or "  （なし）"

    cutoff_line = f"打ち切り: {cutoff_count}枚（周辺カード予算超過）\n" if cutoff_count > 0 else ""

    # 店舗別取得状況。「全カードでエラー/0件」の店舗はセレクタ破損の疑いとして警告する
    shop_block = ""
    if shop_stats:
        rows = []
        suspect = []
        for shop, s in sorted(shop_stats.items()):
            attempted = s["ok"] + s["empty"] + s["error"]
            rows.append(f"  {shop}: 取得 {s['ok']} / 0件 {s['empty']} / エラー {s['error']}")
            if attempted >= 20 and s["ok"] == 0:
                suspect.append(shop)
        shop_block = "\n**店舗別取得状況**\n" + "\n".join(rows) + "\n"
        if suspect:
            shop_block += (
                f"警告: {'、'.join(suspect)} は全カードで取得0件 — "
                f"サイト構造変更（セレクタ破損）またはブロックの可能性\n"
            )

        # カードラボ カタログ巡回の異常（カテゴリ差分・検証失敗・未検証）を警告として足す
        # （reviewer中2/中3b/低2/低3）
        clabo_stat = shop_stats.get("カードラボ", {})
        if clabo_stat.get("mode") == "error":
            shop_block += f"警告: カードラボ カタログ巡回が例外で失敗 — {clabo_stat.get('exception', '')}\n"
        catalog = clabo_stat.get("catalog") or {}

        # category_drift は None（harvest_categories 取得失敗）と []（差分なし）を区別する。
        # どちらも `or []` で潰すと「取得失敗」を検知できなくなるため in 判定で分ける（低3）
        drift = catalog.get("category_drift")
        if drift is None:
            if "category_drift" in catalog:
                shop_block += "警告: カードラボ カテゴリ差分チェック取得失敗\n"
        elif drift:
            # Discord本文の文字数上限対策（低2）: 先頭5件のみ本文に出し、残りは件数だけ示す
            shown = drift[:5]
            remainder = len(drift) - len(shown)
            suffix = f"（他{remainder}件）" if remainder > 0 else ""
            shop_block += f"警告: カードラボ カテゴリ差分 {len(drift)}件 — {'; '.join(shown)}{suffix}\n"

        cat_failed = catalog.get("categories_failed") or []
        if cat_failed:
            shop_block += (
                f"警告: カードラボ カタログ巡回でカテゴリ{len(cat_failed)}件が検証失敗 "
                f"— {', '.join(cat_failed)}\n"
            )

        cat_unverified = catalog.get("categories_unverified") or []
        if cat_unverified:
            shop_block += (
                f"警告: カードラボ カタログ巡回でカテゴリ{len(cat_unverified)}件が未検証"
                f"（item_count読取不能・自然終端のため成立扱い） — {', '.join(cat_unverified)}\n"
            )

        # カーナベルの全件巡回が不成立・例外の晩（カード名の検索に戻って取得量が約3,400回に戻る）を知らせる
        kana_stat = shop_stats.get(KANABELL, {})
        if kana_stat.get("mode") in ("fallback", "error"):
            shop_block += (f"警告: カーナベル 全件巡回が{'不成立' if kana_stat['mode'] == 'fallback' else '例外'}"
                           "のため、カード名の検索に戻しました\n")
        # カードラッシュの型番の頭の巡回も同じ（試し読みの晩は "probe" で、取得に失敗したときだけ知らせる）
        rush_stat = shop_stats.get(CARDRUSH, {})
        if rush_stat.get("mode") in ("fallback", "error"):
            shop_block += (f"警告: カードラッシュ 型番の頭の巡回が{'不成立' if rush_stat['mode'] == 'fallback' else '例外'}"
                           "のため、カード名の検索に戻しました\n")
        elif rush_stat.get("mode") == "probe" and (rush_stat.get("catalog") or {}).get("failed"):
            # 試し読みは保存に使わないが、取得の失敗はお店に止められ始めた兆しになりうる（reviewer 中6）
            shop_block += "警告: カードラッシュ 型番の頭の試し読みで取得に失敗しました（お店の遅れ・遮断が無いか確認）\n"

        # トレコロの在庫一覧の巡回（on）で、読み落としの検索し直しが効いていない晩を知らせる
        tore_stat = shop_stats.get(TORECOLO, {})
        recheck = tore_stat.get("recheck") or {}
        if tore_stat.get("mode") == "catalog":
            if recheck.get("prev_read_failed"):
                shop_block += "警告: トレコロ 前日の行を読めず、巡回の読み落としの検索し直しをしていません\n"
            elif recheck.get("prev_cards") == 0:
                shop_block += "警告: トレコロ 前日の行が0枚で、巡回の読み落としの検索し直しが効いていません\n"
            shop_block += f"トレコロ 巡回の読み落としの疑いでカード名の検索し直し: {recheck.get('cards', 0)}枚\n"

    message = (
        f"**TCGYM 日次レポート {today}**\n"
        f"\n"
        f"**前日の検索状況**\n"
        f"検索回数: {total}回 / カード種類: {unique}種\n"
        f"\n"
        f"**検索TOP5**\n"
        f"{top5_lines}\n"
        f"\n"
        f"**収集結果**\n"
        f"成功: {success_count}件 / 失敗: {fail_count}件 / 所要: {elapsed_min:.0f}分\n"
        f"{cutoff_line}"
        f"{shop_block}"
    )

    try:
        _req.post(webhook_url, json={"content": message}, timeout=10)
        print("日次レポート送信完了")
    except Exception as e:
        print(f"日次レポート送信失敗: {e}")


# 1晩に畳む日数の上限。普段は1日。止まっていた日があっても数晩で追いつき、
# 1回の処理を小さく保つ（2026-10-22 の Disk IO 枯渇の再発を避ける）
MAX_ROLLUP_DAYS_PER_NIGHT = 3


def _oldest_store_date(sb: Client) -> str | None:
    """price_history_v2 に残っている最も古い日（日付先頭の被覆索引で1行だけ読む）。"""
    resp = (sb.table("price_history_v2").select("recorded_at")
            .order("recorded_at", desc=False).range(0, 0).execute())
    rows = resp.data or []
    return rows[0]["recorded_at"][:10] if rows else None


def rollup_and_delete_old_prices(sb: Client, cutoff: str) -> None:
    """cutoff より前の店舗別の行を、古い日から1日ずつ「日ごとの全店最安」へ畳んでから消す。

    畳めなかった日は消さない（消すと相場グラフの15日目以降が欠けるため）。失敗や
    追いつけていない状態は Discord に知らせる（黙って price_history_v2 が増え続けないように）。
    """
    problem = ""
    for _ in range(MAX_ROLLUP_DAYS_PER_NIGHT):
        try:
            day = _oldest_store_date(sb)
        except Exception as e:
            problem = f"最古の日の取得に失敗: {e}"
            break
        if day is None or day >= cutoff:
            return
        next_day = (datetime.fromisoformat(day).date() + timedelta(days=1)).isoformat()
        # 成功時の所要秒数を残す（関数の時間の上限 16秒 を見直す材料。price_daily_rollup.sql）。
        # 失敗時は db_write_guard の待ち時間が混ざるので出さない
        started = time.monotonic()
        try:
            resp = sb.rpc("rollup_price_daily", {"p_before": next_day, "p_from": day}).execute()
            n = (resp.data or [{}])[0].get("rolled_rows", 0) if isinstance(resp.data, list) else 0
            print(f"価格履歴を日ごとの最安へ畳んだ: {day} {n}組 {time.monotonic() - started:.1f}秒")
        except Exception as e:
            problem = f"{day} の畳み込みに失敗（店舗別の行は消さずに残す）: {e}"
            break
        # ビューは delete 不可のため正本テーブル price_history_v2 を直接削除。
        # 消した行を返させない（1日約3.6万行の JSON を受け取らない）
        try:
            resp = (sb.table("price_history_v2")
                    .delete(count=CountMethod.exact, returning=ReturnMethod.minimal)
                    .eq("recorded_at", day)
                    .execute())
            print(f"古い価格履歴を削除: {day} {getattr(resp, 'count', None)}件")
        except Exception as e:
            problem = f"{day} の店舗別の行の削除に失敗: {e}"
            break
    else:
        try:
            day = _oldest_store_date(sb)
        except Exception:
            day = None
        if day is not None and day < cutoff:
            problem = f"1晩の上限（{MAX_ROLLUP_DAYS_PER_NIGHT}日）まで畳んでもまだ {day} が残っている"
    if problem:
        print(f"価格履歴の畳み込み: {problem}")
        send_discord_message(
            f"⚠ **価格履歴の畳み込みが止まっています**\n{problem}\n"
            f"店舗別の行（price_history_v2）は {PRICE_RETENTION_DAYS} 日を超えて残り、DB容量が増えます。"
            f"price_daily_rollup.sql を確認してください。"
        )


def rollup_and_delete_old_art_prices(sb: Client, cutoff: str) -> None:
    """絵ごとの内訳（price_history_art）も、cutoff より前の日を1日ずつ price_daily_art へ畳んでから消す。
    本表の rollup_and_delete_old_prices と同じ手順。表がまだ無い（price_history_art.sql 未適用）ときは何もしない。
    止まったときは Discord に知らせる（別表は小さいので1晩の上限は本表と同じでよい）"""
    problem = ""
    for _ in range(MAX_ROLLUP_DAYS_PER_NIGHT):
        try:
            rows = (sb.table("price_history_art").select("recorded_at")
                    .order("recorded_at", desc=False).range(0, 0).execute()).data or []
        except Exception as e:
            if "PGRST205" in str(e) or "Could not find the table" in str(e):
                return
            problem = f"最古の日の取得に失敗: {e}"
            break
        day = rows[0]["recorded_at"][:10] if rows else None
        if day is None or day >= cutoff:
            return
        next_day = (datetime.fromisoformat(day).date() + timedelta(days=1)).isoformat()
        try:
            resp = sb.rpc("rollup_price_daily_art", {"p_before": next_day, "p_from": day}).execute()
            n = (resp.data or [{}])[0].get("rolled_rows", 0) if isinstance(resp.data, list) else 0
            print(f"絵ごとの内訳を日ごとの最安へ畳んだ: {day} {n}組")
        except Exception as e:
            problem = f"{day} の畳み込みに失敗（店舗別の行は消さずに残す）: {e}"
            break
        try:
            (sb.table("price_history_art")
             .delete(count=CountMethod.exact, returning=ReturnMethod.minimal)
             .eq("recorded_at", day)
             .execute())
        except Exception as e:
            problem = f"{day} の店舗別の行の削除に失敗: {e}"
            break
    if problem:
        print(f"絵ごとの内訳の畳み込み: {problem}")
        send_discord_message(
            f"⚠ **絵ごとの内訳（price_history_art）の畳み込みが止まっています**\n{problem}\n"
            f"price_history_art.sql を確認してください。"
        )


def cleanup_old_data(sb: Client):
    """保持期間を超えた古いデータを削除"""
    today = datetime.now(JST).date()
    cutoff = (today - timedelta(days=PRICE_RETENTION_DAYS)).isoformat()
    rollup_and_delete_old_prices(sb, cutoff)
    rollup_and_delete_old_art_prices(sb, cutoff)
    # パックの版ごとの価格（新弾用の小さな表）は畳まずに消す（新弾情報ページの表が直近2日だけ読む。price_history_pack.sql）
    try:
        (sb.table("price_history_pack")
         .delete(count=CountMethod.exact, returning=ReturnMethod.minimal)
         .lt("recorded_at", cutoff)
         .execute())
    except Exception as e:
        if not ("PGRST205" in str(e) or "Could not find the table" in str(e)):
            print(f"古い版ごとの価格の削除失敗: {e}")
    daily_cutoff = (today - timedelta(days=PRICE_DAILY_RETENTION_DAYS)).isoformat()
    try:
        (sb.table("price_daily_art")
         .delete(count=CountMethod.exact, returning=ReturnMethod.minimal)
         .lt("recorded_at", daily_cutoff)
         .execute())
    except Exception as e:
        if not ("PGRST205" in str(e) or "Could not find the table" in str(e)):
            print(f"古い絵ごとの日ごとの最安の削除失敗: {e}")
    try:
        resp = (sb.table("price_daily_min")
                .delete(count=CountMethod.exact, returning=ReturnMethod.minimal)
                .lt("recorded_at", daily_cutoff)
                .execute())
        deleted = getattr(resp, "count", None) or 0
        if deleted > 0:
            print(f"古い日ごとの最安を削除: {deleted}件（{daily_cutoff}より前）")
    except Exception as e:
        print(f"古い日ごとの最安の削除失敗: {e}")
    # 一人回しリプレイの削除（90日超）。公開中（みんなのリプレイ）は対象外＝取り下げか運営の削除まで残す。
    # 取り下げると is_public=false に戻るので、作成日が古ければ次の掃除で消える
    replay_cutoff = (today - timedelta(days=REPLAY_RETENTION_DAYS)).isoformat()
    try:
        resp = sb.table("solitaire_replays") \
            .delete() \
            .eq("is_public", False) \
            .lt("created_at", replay_cutoff) \
            .execute()
        deleted = len(resp.data) if resp.data else 0
        if deleted > 0:
            print(f"古いリプレイを削除: {deleted}件（{replay_cutoff}より前）")
    except Exception as e:
        print(f"古いリプレイ削除失敗: {e}")


def main():
    """_main をくるみ、DBへの要求が続けて失敗して打ち切ったとき（カード収集のループの外で止まったとき）に
    Discord と記録に残して終了コード1で終える。ループの中で止まったときは _main 側で件数つきで知らせて終える。
    state: _main が「今どの段階か（sync/post）」と、後処理に入った時点の件数・記録の中身を書き込む。
    正常終了の行を記録し終えたあとに止まったとき（recorded）は、行を重ねないよう記録しない。
    """
    started_at = datetime.now(JST)
    state: dict = {"phase": "sync"}
    try:
        _main(state)
    except DbWriteAborted as e:
        report_db_abort(
            get_supabase(), e, label="価格収集", table="tracked_cards など", started_at=started_at,
            today=datetime.now(JST).date().isoformat(), phase=state["phase"],
            processed=state.get("processed"), total=state.get("total"),
            success_count=state.get("success_count", 0),
            run_type=None if state.get("recorded") else "prices",
            record_kwargs=state.get("record_kwargs") or dict(attempted_shops=[], available_shops=[]),
        )
        sys.exit(1)


def _main(state: dict):
    started_at = datetime.now(JST)
    print(f"=== 価格履歴収集 {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    print(f"設定: 周辺予算={DAILY_COLD_BUDGET}枚 / 時間上限={TIME_BUDGET_SEC//60}分")

    # DB が応答しない系の失敗はすべての DB 呼び出しで数えて待って再試行し、続けて失敗したら打ち切る
    # （同期処理も含む。db_write_guard.GuardedSupabase）。カード単位の保存失敗も同じ見張り役で数える
    sb = GuardedSupabase(get_supabase(), ConsecutiveDbFailureGuard())
    # お店ごとの取得回数を終了時に shop_request_daily へ書く（見張りを通さない短いタイムアウトの接続で。書けなくても収集は止めない）
    shop_request_log.install_job("prices", shop_request_log.make_store(), prune=True)

    # ── 注目カードの sync ──
    hot: set[str] = set()
    pack_all, pack_new = sync_latest_packs(sb)
    hot |= pack_all
    hot |= sync_meta_decks(sb)
    hot |= sync_regulation(sb)
    hot |= sync_searched_cards(sb, recent_days=7, min_count=2)

    # ── 周辺カードの sync ──
    try:
        sync_recipe_decks(sb)
    except Exception as e:
        print(f"sync_recipe_decks失敗（スキップ）: {e}")
    try:
        sync_remake_themes(sb, pack_new)
    except Exception as e:
        print(f"sync_remake_themes失敗（スキップ）: {e}")
    try:
        sync_theme_wiki_cards(sb)
    except Exception as e:
        print(f"sync_theme_wiki_cards失敗（スキップ）: {e}")
    sync_searched_cards(sb, recent_days=30, min_count=1)
    try:
        sync_trending_cards(sb)
    except Exception as e:
        print(f"sync_trending_cards失敗（スキップ）: {e}")

    save_pack_list(sb)

    # ── 収集対象の選定 ──
    all_cards = fetch_tracked_cards_with_meta(sb)
    if not all_cards:
        print("監視対象カードが登録されていません")
        return

    hot_cards = [c for c in all_cards if c["card_name"] in hot]
    cold_cards = sorted(
        [c for c in all_cards if c["card_name"] not in hot],
        key=lambda c: c["last_collected_at"] or "",  # NULL="" で最古扱い
    )
    cold_selected = cold_cards[:DAILY_COLD_BUDGET]
    selected = hot_cards + cold_selected
    cold_name_set = {c["card_name"] for c in cold_selected}

    print(
        f"\n注目: {len(hot_cards)}件 / "
        f"周辺予算: {len(cold_selected)}/{len(cold_cards)}件 / "
        f"収集対象計: {len(selected)}件"
    )

    # ── 店舗ヘルスチェック ──
    ci_shops = [name for name, _ in SHOPS if name not in SKIP_SHOPS_IN_CI]
    print("\n--- 店舗ヘルスチェック ---")
    available_shops = check_shop_availability(ci_shops)
    if SUSPENDED_SHOPS:
        # 休止中の店は SHOPS から外れているので収集しない。再開の兆しだけ見張る（shop_status.py）
        watch_suspended_shops()

    # 観測店舗集合の記録用（P5）: ヘルスチェック落ち・APIキー未設定のスキップ理由を積む
    skipped_shops: dict[str, str] = {
        name: "health_check_failed" for name in ci_shops if name not in available_shops
    }

    # カーナベルはAPIキー必須。未設定のまま収集すると全カードが0件になり、
    # 「カーナベルに在庫なし」という誤ったデータがDBに蓄積されるため当日除外する
    if "カーナベル" in available_shops and not (
        os.environ.get("KANABELL_ES_ENDPOINT") and os.environ.get("KANABELL_ES_API_KEY")
    ):
        print("  NG: カーナベル — KANABELL_ES_ENDPOINT / KANABELL_ES_API_KEY 未設定のため当日スキップ")
        available_shops.remove("カーナベル")
        skipped_shops["カーナベル"] = "api_key_missing"
    # 休止中の店は SHOPS に無いので収集しない。後から追えるように理由だけ残す
    for name in sorted(SUSPENDED_SHOPS):
        skipped_shops[name] = "suspended"

    if not available_shops:
        print("全店舗が応答しないため収集を中止します")
        record_collection_run(
            sb, "prices", started_at, datetime.now(JST),
            attempted_shops=ci_shops, available_shops=available_shops,
            skipped_shops=skipped_shops,
        )
        return

    today = datetime.now(JST).date().isoformat()
    total_saved = 0
    success_count = 0
    fail_count = 0
    cutoff_count = 0
    shop_stats: dict = {}  # 店舗別の 取得成功/0件/エラー 集計

    # カードラボ: カタログ巡回（P1・403対策）。成立すれば loop_shops（カード単位
    # ループ専用の店舗リスト）からカードラボを外す。available_shops はここでは
    # 変更しない（record_collection_run に渡す観測店舗集合はカードラボ入りのまま。
    # reviewer中1。経路の区別は shop_stats["カードラボ"]["mode"] で表現する）
    loop_shops, clabo_matched, clabo_catalog_stats = _prepare_clabo_catalog(
        selected, available_shops, shop_stats
    )
    # トレコロ: 在庫一覧の巡回（torecolo_catalog.py）。"on" で成立すれば loop_shops からトレコロを外す
    loop_shops, torecolo_matched, _ = _prepare_torecolo_catalog(
        selected, loop_shops, available_shops, shop_stats
    )
    # カーナベル: 全件巡回（kanabell_catalog.py）。"on" で成立すれば loop_shops からカーナベルを外す
    loop_shops, kanabell_matched = _prepare_kanabell_catalog(
        selected, loop_shops, available_shops, shop_stats
    )
    kanabell_on = kanabell_matched is not None and KANABELL not in loop_shops
    # カードラッシュ: 型番の頭の巡回（cardrush_catalog.py）。"on" で成立すれば loop_shops からカードラッシュを外す
    loop_shops, cardrush_matched = _prepare_cardrush_catalog(
        selected, loop_shops, available_shops, shop_stats
    )
    cardrush_on = cardrush_matched is not None and CARDRUSH not in loop_shops
    cardrush_sink: dict | None = {} if (cardrush_matched is not None and CARDRUSH in loop_shops) else None
    # カードラッシュの検索をする晩だけ、直近2晩に行の無かったカードの検索を間引く（店への取得を減らす）
    cardrush_rest: set[str] = set()
    if CARDRUSH in loop_shops:
        cardrush_rest, rest_stats = _cardrush_rest_set(sb, selected, today, never_rest=pack_all)
        # 間引いた数は "skipped" に入れない（check_collection が「連続失敗で問い合わせを止めた」と読む鍵）
        shop_stats.setdefault(CARDRUSH, {"ok": 0, "empty": 0, "error": 0})["rest"] = rest_stats
    # shadow のとき、ループでカード名の検索から得たカーナベルの行を残す（ループの後で巡回の行と比べる）
    kanabell_sink: dict | None = {} if (kanabell_matched is not None and KANABELL in loop_shops) else None
    # カタログ巡回が成立したときだけ、ループ側の絵の判定をカードごとに残してカタログの保存へ渡す
    art_contexts: dict[str, dict] | None = (
        {} if (clabo_matched is not None or torecolo_matched is not None) else None)
    # shadow のとき、ループでカード名の検索から得たトレコロの結果を残す（巡回の値と比べる）
    torecolo_sink: dict | None = {} if (torecolo_matched is not None and TORECOLO in loop_shops) else None
    # on のとき、トレコロはカードごとに巡回の商品を渡して、ほかの店の行と一緒に保存する（_torecolo_card_plan）
    torecolo_on = torecolo_matched is not None and TORECOLO not in loop_shops
    torecolo_prev: dict[str, dict[str, tuple]] | None = None
    if torecolo_on:
        prev_day = (datetime.fromisoformat(today).date() - timedelta(days=1)).isoformat()
        torecolo_prev = _fetch_torecolo_prev_rows(sb, prev_day)
        shop_stats[TORECOLO]["recheck"]["prev_read_failed"] = torecolo_prev is None
        # 今夜のカードのうち前日の行があった数（名前が食い違って引けないときも 0 に近づいて警告に出る。reviewer 2回目 低3）
        shop_stats[TORECOLO]["recheck"]["prev_cards"] = (
            None if torecolo_prev is None else sum(1 for c in selected if c["card_name"] in torecolo_prev))

    # ループが処理し終えたカードの数（打ち切り・想定外の例外で抜けたときはそこまで。ループを回さない晩は全件）。
    # カタログの保存対象を「実際にループで処理されたカード」に絞るために使う（reviewer中3）
    loop_end_index = len(selected)

    # DB保存の連続失敗で打ち切る（2026-10-22 DB停止時に約2分ずつ失敗しながら要求を送り続けた）
    db_guard = sb.guard
    db_abort: DbWriteAborted | None = None
    abort_phase = "loop"  # カタログ保存（後処理）で止まったときは "post"

    # try/finally で囲む（reviewer低1）: カード単位ループ中に想定外の例外が起きても、
    # 既に巡回済みのカタログ結果（最大2万件）を失わずに保存する。例外自体は
    # finally 実行後に再送出される（catch はしない）
    try:
        if not loop_shops and not torecolo_on and not kanabell_on and not cardrush_on:
            # loop_shops が空＝カタログ巡回でカードラボが外れ、かつ他店が全滅した晩。
            # scraper.compare_prices 側でも空リストはDEFAULT_SHOPSへフォールバックしない
            # よう塞いだが（reviewer中1）、2,700枚ぶんの空スクレイプ＋待機を毎回
            # 走らせるのは無駄なので、ここでループ自体をスキップする
            print("  ⚠ カード単位ループの対象店舗が空のためスキップします（カタログ巡回の保存のみ実施）")
        else:
            # カードごとに処理し終えたら進める。想定外の例外で抜けたときに、処理していないカードへカタログの行を
            # 書かないため（以前は len(selected) のままで、例外の後ろのカードにもカードラボの行を書いていた）
            loop_end_index = 0
            for i, card_data in enumerate(selected):
                card_name = card_data["card_name"]

                # 周辺カードのみ時間予算チェック（注目カードは予算外で必ず収集）
                if card_name in cold_name_set:
                    elapsed = (datetime.now(JST) - started_at).total_seconds()
                    if elapsed > TIME_BUDGET_SEC:
                        cutoff_count = len(selected) - i
                        print(f"時間予算超過（{elapsed/60:.0f}分）。周辺カード残り{cutoff_count}枚を打ち切り")
                        loop_end_index = i
                        break

                print(f"[{i+1}/{len(selected)}] {card_name}")
                card_shops, extra = loop_shops, {}
                if torecolo_on:
                    card_shops, torecolo_products, recheck = _torecolo_card_plan(
                        card_name, loop_shops, torecolo_matched, torecolo_prev, today)
                    extra["torecolo_products"] = torecolo_products
                    if recheck:
                        print("  トレコロ: 巡回の行が前日と比べて読み落としに見えるため、カード名でも検索します")
                        rc = shop_stats[TORECOLO]["recheck"]
                        rc["cards"] += 1
                        # 毎晩同じカードが検索し直しになっていないか後で見るため、名前を少し残す（reviewer 2回目 中1）
                        if len(rc.setdefault("names", [])) < _RECHECK_NAMES_KEPT:
                            rc["names"].append(card_name)
                if torecolo_sink is not None:
                    extra["torecolo_sink"] = torecolo_sink
                if kanabell_on:
                    extra["kanabell_products"] = kanabell_matched.get(card_name, [])
                if kanabell_sink is not None:
                    extra["kanabell_sink"] = kanabell_sink
                if card_name in cardrush_rest:
                    card_shops = [s for s in card_shops if s != CARDRUSH]
                if cardrush_on:
                    extra["cardrush_products"] = cardrush_matched.get(card_name, [])
                if cardrush_sink is not None:
                    extra["cardrush_sink"] = cardrush_sink
                saved = 0
                try:
                    saved = collect_and_save(sb, card_name, today, card_shops,
                                             shop_stats=shop_stats, db_guard=db_guard,
                                             art_contexts=art_contexts, **extra)
                    if saved > 0:
                        total_saved += saved
                        success_count += 1
                        # 収集完了を記録（途中タイムアウトでも進捗が保持されローテーションが正しく進む）
                        try:
                            sb.table("tracked_cards").update(
                                {"last_collected_at": datetime.now(JST).isoformat()}
                            ).eq("card_name", card_name).execute()
                        except Exception:
                            pass  # スキーマ未移行時は無視
                    else:
                        fail_count += 1
                except DbWriteAborted as e:
                    # 保存でも tracked_cards の更新でも止まる（後者のときは保存は済んで成功に数え済み）
                    if saved == 0:
                        fail_count += 1
                    db_abort = e
                    loop_end_index = i + 1
                    break

                loop_end_index = i + 1
                # 店に取りに行かなかったカード（ほかの店が全滅した晩に巡回の商品だけで保存した）は待たない
                if i < len(selected) - 1 and card_shops:
                    time.sleep(WAIT_BETWEEN_CARDS)
    finally:
        # カタログ巡回が成立していれば、実際に処理されたカードだけを対象にここで保存する。
        # ループを回さずスキップした場合は loop_end_index が len(selected) のままなので、
        # selected 全件が保存対象になる（正しい）。
        # DB保存の連続失敗で打ち切ったときは保存しない（固まった DB へ大きな書き込みを重ねないため）
        if clabo_matched is not None and db_abort is None:
            processed = selected[:loop_end_index]
            try:
                clabo_saved = _save_clabo_catalog_rows(
                    sb, processed, clabo_matched, today, shop_stats, clabo_catalog_stats, art_contexts=art_contexts
                )
                total_saved += clabo_saved
            except DbWriteAborted as e:
                # 件数つきの打ち切り処理へ合流させる（ループが別の例外で抜けた途中なら、その例外がそのまま上がる）
                db_abort = e
                abort_phase = "post"
        if torecolo_sink is not None and db_abort is None:
            # shadow: 保存はカード名の検索の側。巡回の値との食い違いだけ記録する（比べに失敗しても収集は続ける）。
            # on のときはループで保存し終えているので、ここでは何もしない
            try:
                shop_stats.setdefault(TORECOLO, {})["shadow"] = _compare_torecolo_shadow(
                    selected[:loop_end_index], torecolo_matched, torecolo_sink, today, art_contexts)
            except Exception as e:
                print(f"  トレコロ 巡回とカード名の検索の比べに失敗: {e}")
        if kanabell_sink is not None and db_abort is None:
            # shadow: カーナベルも同じ。保存はカード名の検索の側で、比べに失敗しても収集は続ける（reviewer 中2）
            try:
                shop_stats.setdefault(KANABELL, {})["shadow"] = _compare_kanabell_shadow(
                    selected[:loop_end_index], kanabell_matched, kanabell_sink, today)
            except Exception as e:
                print(f"  カーナベル 巡回とカード名の検索の比べに失敗: {e}")
        if cardrush_sink is not None and db_abort is None:
            # shadow: カードラッシュも同じ。保存はカード名の検索の側で、比べに失敗しても収集は続ける
            try:
                shop_stats.setdefault(CARDRUSH, {})["shadow"] = _compare_catalog_shadow(
                    CARDRUSH, selected[:loop_end_index], cardrush_matched, cardrush_sink, today, in_stock_only=True)
            except Exception as e:
                print(f"  カードラッシュ 巡回とカード名の検索の比べに失敗: {e}")

    if db_abort is not None:
        # 古いデータの掃除・日次レポートは DB への問い合わせを重ねるので行わない
        report_db_abort(
            sb, db_abort, label="価格収集", table="price_history", started_at=started_at, today=today,
            phase=abort_phase, processed=loop_end_index, total=len(selected), success_count=success_count,
            run_type="prices",
            record_kwargs=dict(
                attempted_shops=ci_shops, available_shops=available_shops,
                skipped_shops=skipped_shops, shop_stats=shop_stats,
                success_count=success_count, fail_count=fail_count,
                cutoff_count=len(selected) - loop_end_index, saved_rows=total_saved,
            ),
        )
        sys.exit(1)

    # ここから後処理（掃除・記録・日次レポート）。DB で止まったら外側の main が件数つきで知らせる
    state.update(
        phase="post", processed=loop_end_index, total=len(selected), success_count=success_count,
        record_kwargs=dict(
            attempted_shops=ci_shops, available_shops=available_shops,
            skipped_shops=skipped_shops, shop_stats=shop_stats,
            success_count=success_count, fail_count=fail_count,
            cutoff_count=cutoff_count, saved_rows=total_saved,
        ),
    )
    cleanup_old_data(sb)

    finished_at = datetime.now(JST)
    # ホスト別アクセス回数（店舗サイトへの負担の記録）。shop_stats に載せて collection_runs へ保存する
    host_count_lines = attach_host_request_counts(shop_stats, get_host_request_counts())
    state["recorded"] = record_collection_run(
        sb, "prices", started_at, finished_at,
        attempted_shops=ci_shops, available_shops=available_shops,
        skipped_shops=skipped_shops, shop_stats=shop_stats,
        success_count=success_count, fail_count=fail_count,
        cutoff_count=cutoff_count, saved_rows=total_saved,
    )

    elapsed_min = (finished_at - started_at).total_seconds() / 60
    send_daily_report(sb, today, success_count, fail_count, cutoff_count, elapsed_min, shop_stats)

    print(f"\n=== 完了（{elapsed_min:.0f}分）===")
    print(f"成功: {success_count}件 / 失敗: {fail_count}件 / 打ち切り: {cutoff_count}件 / 保存行数: {total_saved}")
    if shop_stats:
        print("店舗別取得状況:")
        for shop, s in sorted(shop_stats.items()):
            print(f"  {shop}: 取得 {s['ok']} / 0件 {s['empty']} / エラー {s['error']}")
    if host_count_lines:
        print("ホスト別アクセス回数:")
        for line in host_count_lines:
            print(line)


if __name__ == "__main__":
    main()
