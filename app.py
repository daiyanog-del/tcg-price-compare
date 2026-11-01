"""
TCG 価格比較 Web サーバー
"""

import json
import sys
import time
import os
import hashlib

# コンソール出力を UTF-8 に固定する（2026-09-01）。
# Windows の既定は cp932 で、スクレイパーが print する診断メッセージに cp932 で
# 表現できない文字（em dash など）が1つ入るだけで UnicodeEncodeError になる。
# 実害: pack_scraper が Wiki の404を「捕捉して警告を出す」だけのはずが、その print が
# 落ちて未捕捉例外になり /api/packs が500を返していた（本番Linuxでは再現しない）。
# 個別の文字を書き換えるのは対症療法なので、出力側を直す。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass  # reconfigure 非対応の環境（古いPython・差し替え済みストリーム）では何もしない
import random
import logging
import unicodedata
import requests as _http
import threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as _FuturesTimeoutError
from flask import Flask, render_template, request, jsonify, Response, redirect, url_for, g
from flask_compress import Compress

from scraper import (
    SHOPS, DEFAULT_SHOPS, cache_get, cache_get_shops, cache_store_shops,
    cache_get_shops_stale,
    is_target_card, run_shop_with_status,
    BUYBACK_SHOPS, DEFAULT_BUYBACK_SHOPS,
    buyback_cache_get, buyback_cache_get_shops, buyback_cache_store_shops,
    buyback_cache_get_shops_stale,
    kanabell_card_image_url,
)
from rarity import normalize_rarity, config_for_frontend as _rarity_config_for_frontend
from shipping import (
    SHIPPING_CHECKED_ON, calc_shipping, amount_to_free_shipping,
    amount_to_min_order, is_orderable, rules_for_client as _shipping_rules_for_client,
)
from aggregations import daily_min_by_lowest_rarity, common_shop_price_change
from price_history_reads import fetch_daily_rows_by_card_names, jst_days_back
from name_normalize import db_lookup_names, pick_card_ids
from shop_status import SUSPENDED_SHOPS
from price_persist import (build_art_price_rows, build_min_price_rows, shallow_rows_to_write, upsert_price_art_rows,
                           upsert_price_rows)
from card_code import infer_codes
from overframe import infer_overframe
from search_status import search_coverage
from shop_search_pool import (
    ShopSearchPool, PoolCapacityError, SearchAbandonedError,
    PRIORITY_SINGLE, PRIORITY_BULK, iter_with_heartbeat,
)
from concurrent.futures import Future
from weakref import WeakSet
import upgrade_signals
import art_variant

# 店舗検索の取得口（2026-10-22 作り直し）。全利用者で共有し、店ごとの列で順番に取る。
# 数字は手元の負荷試験（ダミー店舗・gunicorn 1 worker）で決めた仮置き。
# TODO: calibrate from data — 本番の待ち時間・店ごとの取得時間・WAF の反応・メモリを見て校正する。
SHOP_FETCH_MAX_WORKERS = 6  # 全体の同時取得数（作り直し前と同じ6件。休止中の店で DEFAULT_SHOPS が減っても変えない）
SHOP_FETCH_PER_SHOP = 2  # 1店あたりの同時取得数（店の WAF に同時に多く当てない）
SHOP_QUEUE_TIMEOUT_SINGLE = 45  # 単品検索が1店の順番を待つ上限（秒）。過ぎた店は「混雑で取得できず」。
# 30秒だと単品16件同時で1件5店が欠け、いちばん遅い検索の所要はほぼ同じ（43.9秒→45.1秒）だったので45秒に（2026-10-22 ユーザー判断）
SHOP_QUEUE_TIMEOUT_BULK = 180  # 一括検索（デッキ・買取デッキ）が1件の順番を待つ上限（秒）
SEARCH_STREAM_LIMIT = 12  # 店舗へ取りに行く検索（SSE）の同時処理数。gunicorn 16 本のうち4本はページ表示用に残す
# うち一括検索（デッキ・買取デッキ・新商品タブ）が使えるのは8本まで。一括検索は1本で数十秒〜数分枠を握るので、
# 一括が重なっても単品検索が残り4本で入れるようにする（2026-10-22 reviewer 中-1・ユーザー判断）
SEARCH_STREAM_BULK_LIMIT = 8
SEARCH_BUSY_RETRY_SEC = 4  # 混雑応答から画面が取り直すまでの秒数（レート制限 RATE_LIMIT_SEC=3 より長くする）
SEARCH_HEARTBEAT_SEC = 5  # 順番待ちの間に画面へ送る合図の間隔（画面の60秒無応答タイムアウトを避け、切断も早く気づく）
# 順番待ち＋実行中の取得の安全上限。SSE の同時数（12本）× 一括検索1本の最大（5カード×6店）で頭打ちになる
SHOP_FETCH_MAX_PENDING = SEARCH_STREAM_LIMIT * 5 * SHOP_FETCH_MAX_WORKERS
_shop_search_pool = ShopSearchPool(
    max_workers=SHOP_FETCH_MAX_WORKERS, per_shop_limit=SHOP_FETCH_PER_SHOP,
    max_pending=SHOP_FETCH_MAX_PENDING,
    wait_observer=upgrade_signals.monitor.record_wait,
)
_shop_health_lock = threading.Lock()
_shop_health_recorded = WeakSet()

SHOP_BUSY_MESSAGE = "混雑で取得できませんでした"


def _submit_shop_fetch(search_request, shop, fn, card_name):
    try:
        return search_request.submit(fn, card_name, shop=shop, runner=run_shop_with_status)
    except PoolCapacityError as exc:
        if not isinstance(exc, SearchAbandonedError):
            logging.getLogger(__name__).warning("店舗検索の受付上限: %s", exc)
        future = Future()
        future.set_exception(exc)
        return future


class _SearchStreamSlots:
    """店舗へ取りに行く検索（SSE）の同時処理数を数える。SSE は gunicorn のスレッドを
    検索が終わるまで握るため、上限を超えた分はすぐ「混雑中」と返してページ表示の分を残す。

    全体の上限（limit）に加えて、一括検索（kind="bulk"）だけの上限（bulk_limit）を持つ。
    単品検索（kind="single"）は全体の上限まで使えるので、limit - bulk_limit 本は単品検索だけの枠になる。"""

    def __init__(self, limit: int, bulk_limit: int, log_interval: float = 60.0):
        if not 1 <= bulk_limit <= limit:
            raise ValueError("bulk_limit は1以上 limit 以下で指定してください")
        self._limit = limit
        self._bulk_limit = bulk_limit
        self._lock = threading.Lock()
        self._open = {"single": 0, "bulk": 0}
        # 混雑応答の件数。1件ずつログに出すと混雑のあいだ数秒おきに出続けるので、間隔をあけてまとめて出す
        self._busy_lock = threading.Lock()
        self._busy_count = 0
        self._busy_total = 0  # 起動からの混雑応答の累計（有料化の合図の見張りが差分を取る）
        self._busy_logged_at = None
        self._log_interval = log_interval

    def note_busy(self):
        """混雑応答を1件数える。前回のログから log_interval 秒たっていれば、出すべき件数を返す（それ以外は None）。"""
        now = time.monotonic()
        with self._busy_lock:
            self._busy_count += 1
            self._busy_total += 1
            if self._busy_logged_at is not None and now - self._busy_logged_at < self._log_interval:
                return None
            count, self._busy_count, self._busy_logged_at = self._busy_count, 0, now
            return count

    def try_open(self, kind: str = "single"):
        """空きがあれば、1回だけ効く解放関数を返す。空きがなければ None。kind は "single" か "bulk"。"""
        if kind not in self._open:
            raise ValueError(f"kind は {sorted(self._open)} のどちらかで指定してください")
        with self._lock:
            if sum(self._open.values()) >= self._limit:
                return None
            if kind == "bulk" and self._open["bulk"] >= self._bulk_limit:
                return None
            self._open[kind] += 1
        released = [False]

        def release():
            with self._lock:
                if released[0]:
                    return
                released[0] = True
                self._open[kind] -= 1
        return release

    def busy_total(self) -> int:
        """起動からの混雑応答の累計。"""
        with self._busy_lock:
            return self._busy_total

    def in_use(self) -> dict:
        """いま開いている SSE の本数（{"single": n, "bulk": n}）。"""
        with self._lock:
            return dict(self._open)


_search_stream_slots = _SearchStreamSlots(SEARCH_STREAM_LIMIT, SEARCH_STREAM_BULK_LIMIT)


def _search_busy_response():
    """検索の同時数が上限のときの応答。EventSource でも読めるよう 200 の SSE で1件だけ返す。
    fetch で読む画面は X-Search-Busy ヘッダーで判別し、本文を読まずに取り直す。"""
    count = _search_stream_slots.note_busy()
    if count is not None:
        logger.warning("検索の同時処理数の上限（全体%d・一括%d）で混雑応答: 前回のログから %d 件（今回 %s・使用中 %s）",
                       SEARCH_STREAM_LIMIT, SEARCH_STREAM_BULK_LIMIT, count, request.path,
                       _search_stream_slots.in_use())
    response = Response(_sse({"type": "busy", "retry_after": SEARCH_BUSY_RETRY_SEC,
                              "message": "混雑しています。数秒後に自動で再開します"}),
                        mimetype="text/event-stream")
    response.headers["X-Search-Busy"] = "1"
    response.headers["Retry-After"] = str(SEARCH_BUSY_RETRY_SEC)
    return response


def _search_stream_response(stream, release):
    """検索の SSE を返し、終わったら（途中で切断されても）枠を必ず返す。

    生成器が一度も動かずに閉じられた場合は finally が走らないので、call_on_close でも返す
    （release は1回だけ効く）。"""
    def wrapped():
        try:
            yield from stream
        finally:
            release()
    response = Response(wrapped(), mimetype="text/event-stream")
    response.call_on_close(release)
    return response


def _stale_shop_fallback(stale_getter, card_name, shop_name):
    """混雑で取れなかった店の、手元に残っている前回の結果。(結果, 取得時刻 or None)。"""
    try:
        entry = stale_getter(card_name, [shop_name]).get(shop_name)
    except Exception:
        logger.warning("前回の結果の読み出しに失敗: %s", shop_name, exc_info=True)
        entry = None
    if not entry:
        return [], None
    # 行ごとに「前回の結果」の印と取得時刻を付ける（画面・購入候補の集計が今回の値と見分けられるように）
    return [dict(item, stale=True, stale_at=entry["timestamp"]) for item in entry["results"]], entry["timestamp"]


def _stale_preview_rows(stale_getter, card_name, missing_shops, now=None):
    """店へ取りに行く前に先に見せる「前回の結果」。(行, 何分前か) か、出せないときは (None, None)。

    取りに行く店のすべてに前回の結果（通常の期限切れ・24時間以内）があるときだけ出す。
    一部の店しか無いと、最新が揃ったときに店が増えて表が大きく変わるので出さない。
    デッキ検索の1ページ目だけの結果（partial）は浅いので使わない。分は一番古い店で数える。
    キャッシュへ書き戻さないこと（行には stale の印が付いている）。"""
    try:
        kept = stale_getter(card_name, list(missing_shops))
    except Exception:
        logger.warning("前回の結果の読み出しに失敗（先出しは出しません）: %s", card_name, exc_info=True)
        return None, None
    if any(shop not in kept or kept[shop].get("partial") for shop in missing_shops):
        return None, None
    rows = [dict(item, stale=True, stale_at=kept[shop]["timestamp"])
            for shop in missing_shops for item in kept[shop]["results"]]
    oldest = min(datetime.fromisoformat(kept[shop]["timestamp"]) for shop in missing_shops)
    age_minutes = max(0, int(((now or datetime.now()) - oldest).total_seconds() // 60))
    return rows, age_minutes


def _is_shop_busy(exc):
    """自サーバーの混雑で取れなかった（待ち時間の上限・受付件数の上限）。利用者の取り消しは含めない。"""
    return isinstance(exc, PoolCapacityError) and not isinstance(exc, SearchAbandonedError)


def _shop_fetch_result(future):
    # 同時利用者が共有するFutureの結果は変更せず、利用者ごとにコピーする。
    items, errors = future.result()
    return [dict(item) for item in items], errors


def _record_shop_health_once(future, shop_name, results, fetch_errors):
    """実取得1件につき1回だけ監視記録する。自サーバーの受付上限は店舗障害ではない。"""
    if isinstance(future.exception(), PoolCapacityError):
        return
    # 取得口は利用者ごとに別の Future を返すので、共有した実取得の目印（fetch_token）で数える。
    token = getattr(future, "fetch_token", future)
    # 同じ取得を待つリクエスト間で判定と記録を排他する。
    # 弱参照なので、処理済みの取得を監視用集合が保持し続けることはない。
    with _shop_health_lock:
        if token in _shop_health_recorded:
            return
        if fetch_errors:
            tracker.record_failure(shop_name, "店舗からの取得に失敗しました")
        else:
            tracker.record_success(shop_name, len(results))
        _shop_health_recorded.add(token)

from meta_scraper import fetch_tier_list, fetch_deck_cards, build_deck_text, build_recipe_text, _cache_read, _DECK_CACHE_TTL, _TIER_CACHE_TTL
import top_page as _top_page
from pack_scraper import get_pack_list, fetch_pack_cards, pack_cards_cached
from featured_pack import (
    get_featured_pack, is_within_window, featured_card_key, get_featured_card_keys_cached,
)
from monitor import tracker, run_health_check
from constants import JST, VAPID_CLAIMS, PRICE_DAILY_RETENTION_DAYS, PRICE_FLOOR
from ygores_repository import repository as _ygores_repo
import card_display as _card_display
import module_graph as _module_graph
from admin_unreleased import admin_bp as _admin_bp
from neuron_deck_parser import import_neuron as _import_neuron
from neuron_link import resolve_card_name
from wiki_link import resolve_wiki_url
import sync as _sync
from client_ip import _client_ip
import site_urls
import domain_handoff

import re as _re
from urllib.parse import quote as _url_quote
from name_normalize import fuzzy_key as _fuzzy_key, _FUZZY_STRIP, canonicalize_card_name, normalize_card_name

# 検索クエリの表記ゆれ正規化
_DASH_CHARS = _re.compile(r'[\u2012\u2013\u2014\u2015\u2212\uFF0D\uFF70]')  # 各種ダッシュ・ハイフン
def _normalize_query(q: str) -> str:
    """ユーザー入力のカード名を正規化（ダッシュ統一・全角スペース変換）"""
    q = _DASH_CHARS.sub('-', q)       # 各種ダッシュ → 通常ハイフン
    q = q.replace('\u3000', ' ')      # 全角スペース → 半角
    return q.strip()

def _correct_cardname(name: str) -> str:
    """カード名をDBと照合し、補正が必要なら正式名称を返す。不要ならそのまま返す"""
    # 完全一致・fuzzy一致（一意/衝突）は name_normalize.canonicalize_card_name に集約
    # （collect_prices.py の書き込み経路と同一ロジックを共有する。フェーズ3 P1）
    corrected, matched = canonicalize_card_name(
        name, _cardnames_set, _cardnames_fuzzy, warn=logger.warning, context="_correct_cardname"
    )
    if matched:
        return corrected
    # ここに来るのは fuzzy_key がインデックスに存在しない場合のみ（app.py 固有の読み仮名フォールバック）
    # 読み仮名の完全一致
    if name in _cardnames_reading:
        return _cardnames_reading[name]
    # 読み仮名の記号除去版で一致
    q_fuzzy = _fuzzy_key(name)
    if q_fuzzy in _cardnames_reading_fuzzy:
        return _cardnames_reading_fuzzy[q_fuzzy]
    return name

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
Compress(app)
app.register_blueprint(_admin_bp)
# 旧URL（onrender）で開かれた画面を独自ドメインへ引き継ぐ（2026-10-21・CANONICAL_BASE_URL 設定時のみ）
domain_handoff.register(app)

# アップロード上限（デッキPDF読み取り等）。巨大ファイルによるメモリ枯渇を防ぐ
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

# レアリティ設定を全テンプレートに自動注入（色・スラグ・順序を一元供給）
_RARITY_CONFIG_JSON = json.dumps(_rarity_config_for_frontend(), ensure_ascii=False)

# Google Analytics 4 の測定ID（例: G-XXXXXXXXXX）。未設定なら計測タグを出力しない
GA_MEASUREMENT_ID = os.environ.get("GA_MEASUREMENT_ID", "").strip()

@app.context_processor
def inject_rarity_config():
    """全テンプレートに window.RARITY_CONFIG 用の JSON / 一人回し導線フラグを渡す。"""
    return {
        "rarity_config_json": _RARITY_CONFIG_JSON,
        "enable_solo_play": ENABLE_VISUAL_SOLO_PLAY,
        "self_watermark": SELF_WATERMARK_ENABLED,
        "self_watermark_opacity": SELF_WATERMARK_OPACITY,
        "ga_measurement_id": GA_MEASUREMENT_ID,
        # canonical / OGP は request.url_root だと Render のプロキシ配下で常に http に
        # なる（TLSはプロキシで終端されアプリへは http で転送されるため）。
        # 2026-08-17 に同じ理由でワンタイムリンクが http になる不具合を直した
        # _public_base_url() をテンプレートにも渡す（2026-09-01）。
        # 独自ドメイン移行（2026-10-21）後は CANONICAL_BASE_URL を正とする（旧URLで開いても同じ値）
        "public_base_url": _canonical_base_url(),
        # 休止中の店（shop_status.py）。画面の「○○はサイトを休止中のため対象外です」に使う（search-status.js）
        "suspended_shops": sorted(SUSPENDED_SHOPS),
    }

_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# YGOResources 名前インデックスキャッシュ（初回リクエスト時に1回だけ取得）
_ygores_name_index = None
_ygores_fuzzy_index = None  # あいまいキー → ids の逆引き（全角/半角・ハイフン種のゆれ吸収用）
# 排他ロック: 未ロード時に複数スレッドが同時に到達すると、全員が533KBのJSON
# ダウンロード＋辞書構築を並列実行してメモリが跳ねるため（2026-06 OOM調査で実測）、
# 最初の1スレッドだけが構築し他は待つ
_ygores_index_lock = threading.Lock()

def _get_ygores_name_index():
    global _ygores_name_index, _ygores_fuzzy_index
    if _ygores_name_index is not None:
        return _ygores_name_index
    with _ygores_index_lock:
        # ロック待ちの間に他スレッドが構築済みなら再利用
        if _ygores_name_index is not None:
            return _ygores_name_index
        try:
            # repository経由（Supabaseキャッシュ優先 → API。両方失敗時は {}）
            index = _ygores_repo.get_name_index()
            if not index:
                # 失敗時はメモリに固定せず、次のリクエストで再試行する
                # （API側の連続失敗はrepositoryのサーキットブレーカーが抑制する）
                logger.warning("[YGOResources] 名前インデックス取得失敗（次回リクエストで再試行）")
                return {}
            # あいまい一致用の逆引き辞書を構築（記号・全半角・ハイフン種のゆれを吸収）
            # 衝突時は先勝ち（実データで50件・全てスペース種違いの同一カードのみ）
            # 構築完了後にグローバルへ代入する（半構築状態を他スレッドに見せない）
            fuzzy = {}
            for nm, ids in index.items():
                fk = _fuzzy_key(nm)
                if fk and fk not in fuzzy:
                    fuzzy[fk] = ids
            _ygores_fuzzy_index = fuzzy
            _ygores_name_index = index
            logger.info(f"[YGOResources] 名前インデックス取得: {len(_ygores_name_index)}件")
        except Exception as e:
            logger.warning(f"[YGOResources] 名前インデックス取得失敗: {e}")
            return {}
        return _ygores_name_index

def _ygores_image_url(card_name):
    """カード名からYGOResourcesのOCGアートワーク画像URLを返す。未登録の場合はNone"""
    idx = _get_ygores_name_index()
    ids = idx.get(card_name)
    if not ids and _ygores_fuzzy_index:
        # 完全一致しない場合は記号・全半角ハイフン等のゆれを無視して再照合
        # （YGOResourcesは全角ハイフン「－」が標準だが、カード名は半角「-」で渡ることがある）
        ids = _ygores_fuzzy_index.get(_fuzzy_key(card_name))
    if not ids:
        return None
    cid = ids[0]
    path = f"/{cid // 10000}/{(cid % 10000) // 100}/{cid % 100}_1.png"
    return f"https://artworks-jp-n.ygoresources.com{path}"

# card_display に発売済みカードの URL 解決関数を注入する。
# ここで行うことで循環 import を回避する（card_display は app.py を import しない）。
# 注意: 注入するのはメモリ内索引の ygoresources のみ。カーナベルは外部ES検索で遅いため、
# 従来どおり各エンドポイント側でフォールバックする（バッチAPIの並列化を維持）。
_card_display.register_released_resolver(_ygores_image_url)
# 索引の取得失敗（resolver は None を返す）と「見つからない」を区別するため、索引の有無を返す probe も登録する
_card_display.register_released_index_probe(lambda: bool(_ygores_name_index))

FAVICON_FILES = {
    'favicon.svg', 'favicon-32.png', 'favicon-16.png',
    'apple-touch-icon.png', 'icon-192.png', 'icon-512.png',
    'icon-maskable-192.png', 'icon-maskable-512.png',
    'tcgym-icon-base.png', 'manifest.json',
}

# 読み取り専用GET APIのうち、レスポンス内容が数分単位でしか変わらないもの。
# ここに載せたパスだけ、GETかつstatus 200のときに短時間の公開キャッシュを許可する。
_CACHEABLE_GET_API_PATHS = {
    'api/top-movers', 'api/top-priced', 'api/buyback-movers',
    'api/top-decks', 'api/config', 'api/meta',
}

# static/ 配下ファイルの内容ハッシュキャッシュ: rel_path -> (mtime, size, hash)
# mtime/sizeが変わっていなければ再計算しない（毎リクエストの再ハッシュを避ける）
_static_hash_cache: dict[str, tuple[float, int, str]] = {}
_static_hash_lock = threading.Lock()


def _static_content_hash(rel_path: str) -> str | None:
    """static/ 配下の実ファイル内容から短いハッシュ(md5先頭12桁)を計算する。

    レビュー指摘（Critical）: 手書きの ?v= は更新時に上げ忘れることがあり
    （実例: packs.js 等4本で ?v= 据え置きのまま中身だけ更新済みだった）、
    immutable を付けると更新漏れが最大1年間ブラウザに残ってしまう。
    内容そのものから決まるハッシュを使えば、ファイルを直せばURLも自動で変わるため
    「更新したのにキャッシュが古いまま」という不整合が原理的に起きない。

    パストラバーサル防止: '..' セグメントを明示的に拒否した上で、実パスを正規化してから
    static_folder 配下であることを再確認する（シンボリックリンク等の抜け道も防ぐ）。
    ファイルが存在しない・static_folder外なら None を返す（呼び出し側は ?v= 無しにフォールバック）。
    """
    if not rel_path or any(seg == '..' for seg in rel_path.replace('\\', '/').split('/')):
        return None
    static_root = os.path.realpath(app.static_folder)
    abs_path = os.path.realpath(os.path.join(app.static_folder, rel_path))
    if os.path.commonpath([static_root, abs_path]) != static_root:
        return None
    try:
        st = os.stat(abs_path)
    except OSError:
        return None

    cached = _static_hash_cache.get(rel_path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]

    try:
        with open(abs_path, 'rb') as f:
            digest = hashlib.md5(f.read()).hexdigest()[:12]
    except OSError:
        return None

    with _static_hash_lock:
        _static_hash_cache[rel_path] = (st.st_mtime, st.st_size, digest)
    return digest


def static_url(rel_path: str) -> str:
    """static/配下のファイルを内容ハッシュ付きURLで返すJinjaグローバル。
    ハッシュが計算できない（ファイルが無い等）場合は ?v= を付けずに返す。
    """
    h = _static_content_hash(rel_path)
    base = url_for('static', filename=rel_path)
    return f"{base}?v={h}" if h else base


app.jinja_env.globals['static_url'] = static_url


# prefixes(tuple化したキー) -> 生成済みJSON文字列。本番はstatic配下が起動後に変わらない
# 前提でプロセス内メモ化する（os.walkの毎リクエスト実行を避ける）。ローカル開発時
# （_is_debug_mode()真）はファイル追加を即反映させたいためメモ化しない
_static_import_map_cache: dict[tuple[str, ...], str] = {}


def static_import_map(prefixes: list[str]) -> str:
    """static/配下の指定prefix群にある全 .js/.mjs ファイルを、内容ハッシュ付きURLへ写像する
    importmap用JSON文字列を返すJinjaグローバル。

    一人回し(solitaire.html)は多数のJSファイルを相対importで読み込んでおり、それぞれに
    static_url()を個別に書くと更新のたびに列挙漏れが起きうるため、prefix配下をos.walkで
    網羅的に列挙して自動生成する。パス区切りは常に '/' に正規化する。

    パストラバーサル防止: '..' セグメントを明示的に拒否した上で、実パスを正規化してから
    static_folder配下であることを再確認する（_static_content_hashと同じ方式）。
    """
    cache_key = tuple(prefixes)
    if not _is_debug_mode() and cache_key in _static_import_map_cache:
        return _static_import_map_cache[cache_key]

    imports: dict[str, str] = {}
    static_root = os.path.realpath(app.static_folder)
    for prefix in prefixes:
        prefix_norm = prefix.replace('\\', '/').strip('/')
        if not prefix_norm or any(seg == '..' for seg in prefix_norm.split('/')):
            continue
        abs_prefix = os.path.realpath(os.path.join(app.static_folder, *prefix_norm.split('/')))
        if os.path.commonpath([static_root, abs_prefix]) != static_root:
            continue
        if not os.path.isdir(abs_prefix):
            continue
        for dirpath, _dirnames, filenames in os.walk(abs_prefix):
            for fn in filenames:
                if not fn.lower().endswith(('.js', '.mjs')):
                    continue
                abs_path = os.path.join(dirpath, fn)
                rel_path = os.path.relpath(abs_path, static_root).replace(os.sep, '/')
                # 非ASCIIファイル名対策でpercent-encode（importmapのキーはURL文字列のため）
                key = '/static/' + _url_quote(rel_path, safe='/')
                imports[key] = static_url(rel_path)

    raw = json.dumps({"imports": imports}, ensure_ascii=False)
    # </script> 等の混入防止（index.html側で使っている | tojson と同等の対策。
    # importmapは<script type="importmap">の中にそのまま埋め込むため必要）
    safe_json = raw.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')

    if not _is_debug_mode():
        _static_import_map_cache[cache_key] = safe_json
    return safe_json


app.jinja_env.globals['static_import_map'] = static_import_map


# entry -> 先読みするURL一覧。static_import_map と同じく本番はプロセス内でメモ化する
_static_module_preloads_cache: dict[str, list[str]] = {}


def static_module_preloads(entry: str) -> list[str]:
    """ES module の入口 entry から静的 import でたどれる全ファイルの、内容ハッシュ付きURL一覧。

    テンプレートが <link rel="modulepreload"> に並べ、import の連鎖を待たずに一度に読ませる
    （一人回しの初期化短縮・2026-10-16）。URL は static_import_map の写像先と同じにして、
    先読みした取得結果がそのまま使われるようにする。
    """
    if not _is_debug_mode() and entry in _static_module_preloads_cache:
        return _static_module_preloads_cache[entry]
    urls = [static_url(rel) for rel in _module_graph.module_dependencies(app.static_folder, entry)]
    if not _is_debug_mode():
        _static_module_preloads_cache[entry] = urls
    return urls


app.jinja_env.globals['static_module_preloads'] = static_module_preloads


@app.after_request
def add_cache_headers(response):
    """レスポンスタイプに応じたキャッシュ制御"""
    ct = response.content_type
    path = request.path.lstrip('/')
    filename = path.split('/')[-1]
    query_v = request.args.get('v')
    is_static = path.startswith('static/')
    # static_url() が発行した「内容ハッシュ付き?v=」と一致する場合だけ1年immutable。
    # 一致判定を FAVICON_FILES 判定より先に置くことで、favicon/manifest.json も
    # ハッシュ一致時は immutable を優先させる（レビュー指摘: 順序調整）。
    content_hash = _static_content_hash(path[len('static/'):]) if is_static else None
    hash_matched = bool(is_static and query_v and content_hash and query_v == content_hash)

    if hash_matched:
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    elif filename in FAVICON_FILES:
        # ファビコン・アイコン類: ハッシュ不一致（未対応の古い?v=や?v=無し）のときはここに来る。
        # 以前は no-store で毎回再検証させていたが、1日キャッシュに緩める。
        response.headers['Cache-Control'] = 'public, max-age=86400'
    elif 'text/html' in ct:
        response.headers['Cache-Control'] = 'public, max-age=60, stale-while-revalidate=300'
    elif 'application/xml' in ct or 'text/plain' in ct:
        response.headers['Cache-Control'] = 'public, max-age=3600'
    elif is_static and query_v and filename not in FAVICON_FILES:
        # 手書きの?v=（ハッシュと不一致、または当該ファイルにハッシュが無い）はimmutableにせず
        # 短時間キャッシュに留める。ETagによる再検証は残るため、?v=の更新漏れがあっても
        # 最大1時間で自己修復する（レビュー指摘: 根本対策と保険の両立）。
        response.headers['Cache-Control'] = 'public, max-age=3600'
    elif is_static and 'image/' in ct and filename not in FAVICON_FILES:
        # ロゴ・OGP画像等、?v=の無い静的画像は従来通り1週間キャッシュ
        response.headers['Cache-Control'] = 'public, max-age=604800'
    elif (
        request.method == 'GET'
        and response.status_code == 200
        and path in _CACHEABLE_GET_API_PATHS
        and not getattr(g, 'no_cache', False)
    ):
        # 読み取り専用の集計系APIは短時間の公開キャッシュを許可する。
        # ただしハンドラ側が g.no_cache=True を立てた場合（error付き・空データ応答）は
        # 意味のない失敗応答をCDN等に公開キャッシュしないよう付けない
        response.headers['Cache-Control'] = 'public, max-age=300'
    if getattr(g, 'no_store', False):
        # 独自ドメインの引き継ぎページ（domain_handoff.py）は端末ごとの保存領域を運ぶ入口なので保存させない
        response.headers['Cache-Control'] = 'no-store'
    if response.status_code >= 400:
        # エラーレスポンス（HTMLエラーページ含む）が上記の public キャッシュ指定を
        # 引き継いだままCDN等に公開キャッシュされないよう、常に上書きで無効化する
        response.headers['Cache-Control'] = 'no-store'

    # セキュリティヘッダ（全応答共通・2026-08-20）。
    # CSPは今回入れない（インラインscript/onclickを多用しており段階導入が必要。TASKS.md参照）。
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    # 埋め込み利用の実績が無いため、将来の自サイト内iframe利用があっても壊れない安全側の
    # SAMEORIGINにする（DENYにはしない）。
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    return response


# ── エラーハンドラ ──
# /api/ 配下は JSON、それ以外は簡易HTMLページ（トップへの戻りリンク付き）を返す。
_ERROR_PAGES = {
    404: ("ページが見つかりません", "お探しのページは見つかりませんでした。URLをご確認ください。"),
    413: ("リクエストが大きすぎます", "送信内容のサイズが上限を超えています。"),
    429: ("アクセスが集中しています", "しばらく時間をおいてから再度お試しください。"),
    500: ("エラーが発生しました", "予期しないエラーが発生しました。しばらくしてから再度お試しください。"),
}


def _make_error_handler(status_code: int, json_error: str):
    def _handler(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": json_error}), status_code
        title, message = _ERROR_PAGES[status_code]
        return render_template("error.html", title=title, message=message), status_code
    return _handler


for _code, _json_error in [(404, "not_found"), (413, "payload_too_large"),
                            (429, "too_many_requests"), (500, "internal_error")]:
    app.register_error_handler(_code, _make_error_handler(_code, _json_error))


# 同時検索の簡易レートリミット (メモリ内)
_last_search: dict[str, float] = {}
# ニューロン取り込み用の独立バケット。価格検索と別管理にすることで、
# カード共有フロー（取り込み→直後に価格検索）が互いを弾かないようにする。
_last_import: dict[str, float] = {}
# 買取検索用の独立バケット。2026-08-25のC-2（販売ページに買取価格を併置）で、
# 販売検索の直後に同じ画面から買取取得が走る動線ができたため、共有バケットのままだと
# ユーザーの正当な2操作が互いを429で弾く（RATE_LIMIT_SEC=3）。系統ごとに分ける既存方針に従う。
_last_buyback: dict[str, float] = {}
# デッキ見積もり用の独立バケット。マイデッキ／環境デッキ／購入候補の合計計算は
# カード検索とは別のユーザー操作であり、共有していると「検索した直後にマイデッキを
# 開く」だけで 429 になる（2026-09-01 に /api/packs/cards で実際に報告された事象と同型）。
_last_deck: dict[str, float] = {}
# 最新弾の収録カードリスト用の独立バケット。こちらは店舗スクレイプを伴わず
# （遊戯王Wikiの取得＋キャッシュ）、性質からしてカード検索と枠を共有する理由がない。
# 共有していたため「パックを続けて2つ開く」と2つ目が必ず 429 になっていた。
_last_packs: dict[str, float] = {}


def _all_rate_limit_buckets() -> list[dict]:
    """レートリミットの全バケット。バケットを増やしたらここにも足すこと。

    テストが個別バケット名を直接 clear() していると、バケットを1つ増やすたびに
    無関係なテストが 429 で落ちる（2026-09-01 に実際に発生。/api/packs/cards の
    分離で test_deck_per_shop_rarity が5件落ちた）。一覧をここに集約して
    テスト側は _reset_rate_limits() を呼ぶだけにする。
    """
    return [_last_search, _last_import, _last_buyback, _last_deck, _last_packs,
            _last_sync, _last_sync_link, _last_feedback, _last_mercari, _last_text_report,
            _last_pack_matrix]


def _reset_rate_limits() -> None:
    """全レートリミットバケットを空にする（テスト用）。"""
    for b in _all_rate_limit_buckets():
        b.clear()
# 端末間同期(sync)用の独立バケット。既存APIとレート制限を奪い合わないようにする。
# キーは sync_id（IPだと宅内の複数端末が互いを弾いてしまうため）。
# init/push/pull は「裏で自動的に走る」背景同期。
_last_sync: dict[str, float] = {}
SYNC_RATE_LIMIT_SEC = 5  # TODO: calibrate from data（設計文書 §6.8）
# 2026-08-17 本番不具合の修正: link/create等（ユーザーが明示的に押すボタン起点）を
# _last_sync と同じバケット・同じキー(sync_id)で共有すると、購入候補タブを開いた直後に
# 走る pull（背景同期）がタイムスタンプを更新し、直後のボタン操作がほぼ必ず429で弾かれる
# （実測: pullの直後にlink/createを呼ぶと429）。ユーザー操作起点のバケットを別に分離する。
_last_sync_link: dict[str, float] = {}
LINK_CREATE_RATE_LIMIT_SEC = 10  # TODO: calibrate from data（設計文書 §6.8。link/create のみ10秒）
LINK_RATE_LIMIT_SEC = 5  # TODO: calibrate from data（link/preview・link/redeem・unlink）
_rate_limit_lock = __import__('threading').Lock()
RATE_LIMIT_SEC = 3
RATE_LIMIT_MAX_ENTRIES = 10000  # これ以上溜まったら古い記録を一括削除
MAX_CARD_NAME_LEN = 50
MAX_DECK_CARDS = 80
MAX_DECK_PARAM_CHARS = 6000
MAX_DECK_QTY = 99
# 購入候補の API（/api/wish-prices・/api/wish-shop-totals・/api/card-rarities）が1回に受け取るカードの数。
# 購入候補そのものに上限は無い。以前は50で、51種目より後に入れたカードの価格が「見つかりませんでした」になっていた
# （2026-10-31。新しいカードは一覧の末尾に足されるので、後から入れたものだけが切り捨てられた）。
# 200種でも price_history_reads の1束（_ID_CHUNK=200）に収まる。画面側は index-wish.js の _WISH_API_MAX と同じ値にする
MAX_WISH_CARDS = 200
MAX_PACK_PARAM_LEN = 120

# ヘルスチェック用シークレットキー
HEALTH_CHECK_KEY = os.environ.get("HEALTH_CHECK_KEY", "")

# 一人回し(solo play) feature flag。停止要請時に env で 0 を明示セットすると
# 一人回しの導線・ルートだけが止まり、相場等のコア機能は無傷で動く（kill-switch）。
# ルート側の gate は solitaire_routes.py が同じ env を import 時評価して持つ。
ENABLE_VISUAL_SOLO_PLAY = os.environ.get("ENABLE_VISUAL_SOLO_PLAY", "1") == "1"

# 自サイト透かし（cosmetic mitigation）: 発売済みクリーン画像に薄い透かしを重ねる。
# orthogonal な mitigation。落としても他機能は無傷（env で on/off・強度を制御）。
SELF_WATERMARK_ENABLED = os.environ.get("SELF_WATERMARK_ENABLED", "1") == "1"
try:
    SELF_WATERMARK_OPACITY = float(os.environ.get("SELF_WATERMARK_OPACITY", "0.08"))
except ValueError:
    SELF_WATERMARK_OPACITY = 0.08
SELF_WATERMARK_OPACITY = max(0.0, min(1.0, SELF_WATERMARK_OPACITY))

# アフィリエイト設定（環境変数から取得）
AFFILIATE_CONFIG = {
    "カーナベル": {
        "sell_a8mat": os.environ.get("A8_KANABELL_SELL", ""),
        "buy_a8mat": os.environ.get("A8_KANABELL_BUY", ""),
    },
    "駿河屋": {
        "user_id": os.environ.get("SURUGAYA_AFF_USER_ID", ""),
    },
}

# VAPID 設定（Web Push 通知用）。VAPID_CLAIMS は constants.py に一元定義
VAPID_PUBLIC_KEY  = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")


# ── 検索ランキング（Supabase永続化 + メモリフォールバック） ──

from collections import defaultdict
from threading import Lock, Thread

# フィードバック（不具合・要望）受付
FEEDBACK_RATE_LIMIT_SEC    = 30    # 同一IP 30秒に1回まで
MAX_FEEDBACK_BODY_CHARS    = 2000
MAX_FEEDBACK_CONTACT_CHARS = 200
_last_feedback: dict[str, float] = {}  # _consume_rate_limit の共有バケットとして使う
# 未発売カードの「テキストの誤り報告」専用バケット（IPごと30秒に1回。フィードバックと枠を奪い合わない）
TEXT_REPORT_RATE_LIMIT_SEC = 30  # TODO: calibrate from data
_last_text_report: dict[str, float] = {}
_DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

_ranking_lock = Lock()
_search_recent: list[tuple[float, str]] = []  # メモリフォールバック用
RANKING_RECENT_HOURS = 24

# カード情報キャッシュ（一人回しデッキ読込の高速化）
# konami_id (int) → api_card_info のレスポンスdict をメモリに保持する
# カード情報はほぼ不変のため TTL なし（再起動でリセット）
_card_info_cache: dict[int, dict] = {}
_card_info_lock = Lock()
MAX_CARD_INFO_CACHE = 5000  # これ以上溜まったら一括クリア

# Supabaseクライアント（環境変数が設定されていれば有効）
_supabase_client = None
_SUPABASE_URL = os.environ.get("SUPABASE_URL")
_SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if _SUPABASE_URL and _SUPABASE_KEY:
    try:
        from supabase import create_client
        _supabase_client = create_client(_SUPABASE_URL, _SUPABASE_KEY)
        logger.info("Supabase接続成功 — 検索ランキングを永続化します")
    except Exception as e:
        logger.warning(f"Supabase接続失敗（メモリモードで動作）: {e}")

# 一人回し(solo play) Blueprint を登録し、Supabase クライアントアクセサを注入する。
# ENABLE_VISUAL_SOLO_PLAY=0 で機能停止できる kill-switch（gate は Blueprint 内部）。
from solitaire_routes import solitaire_bp, init_solitaire
init_solitaire(lambda: _supabase_client)
app.register_blueprint(solitaire_bp)
from legal_routes import legal_bp  # /terms・/privacy（規約の単独ページ）
app.register_blueprint(legal_bp)

# 海外先行の予告カードのまとめページ（/tcg-preview/<slug>）と OG 画像（2026-10-29）
from tcg_announce_routes import tcg_announce_bp
import tcg_announce_view as _tcg_announce_view
app.register_blueprint(tcg_announce_bp)


def _is_debug_mode() -> bool:
    # フェイルクローズ化（2026-08-20）: FLASK_DEBUG未設定時は「本番」扱いにする。
    # 以前は既定"1"（デバッグ扱い）だったため、Render側で明示設定していない場合に
    # ヘルスチェックの鍵チェックが素通りする穴があった（ローカル開発時は
    # FLASK_DEBUG=1 を明示設定すること。tcg-web/CLAUDE.md 参照）。
    return os.environ.get("FLASK_DEBUG", "0") == "1"


def _consume_rate_limit(bucket: dict | None = None,
                        interval: int = RATE_LIMIT_SEC,
                        message: str = "しばらく待ってから再度検索してください",
                        key: str | None = None):
    """重いAPI用のレートリミット。制限時はレスポンス、通過時はNone。

    bucket を分けることで、別系統のAPI同士がレート制限を奪い合わないようにする
    （例: ニューロン取り込みと価格検索を連続実行しても互いに弾かない）。
    既定は価格検索用の共有バケット（_last_search）。

    key: バケットのキー。省略時は従来どおり IP アドレス。同期API等、IP単位だと
    宅内の複数端末が互いを弾いてしまう場合に sync_id 等の識別子を渡す
    （既存呼び出し側は key を渡さないため挙動は変わらない）。
    """
    if bucket is None:
        bucket = _last_search
    rate_key = key or _client_ip()
    now = time.time()
    with _rate_limit_lock:
        if rate_key in bucket and now - bucket[rate_key] < interval:
            return jsonify({"error": message}), 429
        bucket[rate_key] = now
        if len(bucket) > RATE_LIMIT_MAX_ENTRIES:
            # 件数ベースの強制退避。「1時間より古いエントリのみ削除」だと継続フラッド下
            # （常に直近1時間以内のキーばかりになる）では1件も消えず、ロック保持のまま
            # 毎回全件スキャンし続ける問題があった。最終アクセスが古い順に超過分を
            # 無条件で削除し、上限を確実に守る。
            overflow = len(bucket) - RATE_LIMIT_MAX_ENTRIES
            oldest_keys = sorted(bucket, key=bucket.get)[:overflow]
            for k in oldest_keys:
                del bucket[k]
    return None


def _parse_limit_param(default: int, maximum: int):
    raw = request.args.get("limit", str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, (jsonify({"error": "limit は整数で指定してください"}), 400)
    return max(1, min(value, maximum)), None


def _parse_deck_entries(names_raw: str):
    """デッキ入力（| 区切り・任意で「N 」枚数プレフィックス）をパースし名前補正する。

    名前補正は必須で省略不可。入口の _normalize_query が全角ダッシュを半角へ寄せる
    ため、補正せずにDB照合へ進むと正式名称（全角ダッシュ）のキーと永遠に一致しない
    （2026-08-03 デッキ見積もりのバグAの原因。旧 correct_names=False 引数は廃止した）。
    """
    if len(names_raw) > MAX_DECK_PARAM_CHARS:
        return None, (jsonify({"error": "入力が長すぎます"}), 400)

    entries = []
    for line in names_raw.split("|"):
        line = line.strip()
        if not line:
            continue
        if len(entries) >= MAX_DECK_CARDS:
            return None, (jsonify({"error": f"一括検索は最大{MAX_DECK_CARDS}枚までです"}), 400)

        m = _re.match(r"^(\d+)\s+(.+)$", line)
        qty = int(m.group(1)) if m else 1
        if qty < 1 or qty > MAX_DECK_QTY:
            return None, (jsonify({"error": f"枚数は1〜{MAX_DECK_QTY}で指定してください"}), 400)

        name = _normalize_query(m.group(2) if m else line)
        if not name:
            continue
        if len(name) > MAX_CARD_NAME_LEN:
            return None, (jsonify({"error": "カード名が長すぎます"}), 400)
        name = _correct_cardname(name)
        # 日本に来たTCG先行カードの英語名は日本語の正式名で店舗検索する（2026-10-23 段3）。保存デッキは画面側で書き換えるが、
        # 編集中の画面・まだ開いていない端末などに英語名が残る間に、英語名で全店へ問い合わせないため。
        # 応答（card_start/card_done）は index で画面の行に当たるので、名前を変えても対応は崩れない
        name = _card_display.renamed_name(name) or name
        entries.append({"qty": qty, "name": name})

    if not entries:
        return None, (jsonify({"error": "カード名がありません"}), 400)
    return entries, None


def _require_health_key():
    """ヘルス系APIの認証。本番はキー未設定でも公開しない。"""
    if not HEALTH_CHECK_KEY:
        if _is_debug_mode():
            return None
        return jsonify({"error": "health check key is not configured"}), 503
    if request.args.get("key") != HEALTH_CHECK_KEY:
        return jsonify({"error": "unauthorized"}), 403
    return None


def _claim_startup_job(name: str, ttl_sec: int = 300) -> bool:
    """複数worker起動時に同じプリフェッチを同時実行しないための軽いファイルロック。

    DISABLE_STARTUP_JOBS=1（pytest 実行時）は常に False を返し、ロックファイルにも触れない。
    相場キャッシュ・Tier表・パック収録カードの先読みはこの関数だけで起動を判定しており、
    ファイル末尾の _STARTUP_JOBS_DISABLED の判定が届いていなかった。ロックファイルが無い
    新しい checkout（CI）では import 時にパック先読みが走り、テスト中に yugioh-wiki を
    取りに行って、requests.get を差し替えた別のテストの記録に混ざっていた（2026-10-22 CI）。
    """
    if os.environ.get("DISABLE_STARTUP_JOBS") == "1":
        return False
    cache_dir = os.path.join(os.path.dirname(__file__), ".cache")
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"startup_{name}.lock")
    now = time.time()
    try:
        if os.path.exists(path) and now - os.path.getmtime(path) < ttl_sec:
            return False
        if os.path.exists(path):
            os.remove(path)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(now))
        return True
    except FileExistsError:
        return False
    except Exception as e:
        logger.warning(f"起動時ジョブロック取得失敗 ({name}): {e}")
        return True

# トレンドキャッシュ（Supabaseへのクエリを減らす）
_trending_cache: list[dict] = []
_trending_cache_time: float = 0
_TRENDING_CACHE_SEC = 300  # 5分キャッシュ

def _record_search(card_name: str):
    """検索をランキングに記録（Supabase or メモリ）"""
    # メモリにも記録（フォールバック用）
    with _ranking_lock:
        now = time.time()
        _search_recent.append((now, card_name))
        cutoff = now - RANKING_RECENT_HOURS * 3600
        while _search_recent and _search_recent[0][0] < cutoff:
            _search_recent.pop(0)

    # Supabaseへの書き込みはバックグラウンドで（レスポンスを遅延させない）
    if _supabase_client:
        Thread(target=_save_search_to_supabase, args=(card_name,), daemon=True).start()

def _save_search_to_supabase(card_name: str):
    """Supabaseにログを非同期で保存"""
    try:
        _supabase_client.table("search_logs").insert({"card_name": card_name}).execute()
    except Exception as e:
        logger.warning(f"検索ログ保存失敗: {e}")

def _record_deck_search(card_names: list):
    """デッキ検索カードをdeck_search_logsに記録（人気ランキングには影響しない）"""
    if not _supabase_client or not card_names:
        return
    # 重複排除してバックグラウンドで保存
    unique_names = list(dict.fromkeys(card_names))
    Thread(target=_save_deck_search_to_supabase, args=(unique_names,), daemon=True).start()

def _save_deck_search_to_supabase(card_names: list):
    """deck_search_logsにバッチinsert（非同期）"""
    try:
        rows = [{"card_name": name} for name in card_names]
        _supabase_client.table("deck_search_logs").insert(rows).execute()
    except Exception as e:
        logger.warning(f"デッキ検索ログ保存失敗: {e}")

def _get_trending(limit: int = 10) -> list[dict]:
    """直近24時間の検索ランキングを返す"""
    global _trending_cache, _trending_cache_time

    # キャッシュが有効ならそれを返す
    now = time.time()
    if _trending_cache and now - _trending_cache_time < _TRENDING_CACHE_SEC:
        return _trending_cache[:limit]

    # Supabaseから取得を試みる
    if _supabase_client:
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=RANKING_RECENT_HOURS)).isoformat()
            resp = _supabase_client.rpc("get_search_ranking", {
                "since_ts": cutoff,
                "max_results": 30
            }).execute()
            if resp.data:
                result = [{"name": r.get("card_name", ""), "count": r.get("search_count", 0)} for r in resp.data if r.get("card_name")]
                _trending_cache = result
                _trending_cache_time = now
                return result[:limit]
        except Exception as e:
            logger.warning(f"ランキング取得失敗（メモリにフォールバック）: {e}")

    # メモリフォールバック
    with _ranking_lock:
        cutoff_ts = now - RANKING_RECENT_HOURS * 3600
        recent_counts: dict[str, int] = defaultdict(int)
        for ts, name in _search_recent:
            if ts >= cutoff_ts:
                recent_counts[name] += 1
        ranked = sorted(recent_counts.items(), key=lambda x: -x[1])
        result = [{"name": name, "count": count} for name, count in ranked[:30]]
        _trending_cache = result
        _trending_cache_time = now
        return result[:limit]


def _new_cards_first_html(data: dict | None, count: int = 6) -> list[dict]:
    """トップの新着欄の最初の枚数を、サーバーが本物の HTML で書くための値（new-cards.js の cardElement と同じ中身）。

    画像を HTML の時点で取りに行き、JS の実行を待たずに表示する（2026-10-22 LCP）。JS は後から同じ中身で描き直す。
    日付は new-cards.js の addedDate と同じ「MM/DD 追加」（日本時間）。画像は縮小版を優先し、無ければ原寸。
    """
    rows = []
    for card in ((data or {}).get("cards") or [])[:count]:
        added = ""
        if card.get("added_at"):
            try:
                added = datetime.fromisoformat(card["added_at"]).astimezone(JST).strftime("%m/%d") + " 追加"
            except ValueError:
                added = ""
        img = card.get("thumb_url") or card.get("image_url") or ""
        rows.append({"name": card["name"], "img": img if img.startswith("https://") else "", "added": added})
    return rows


@app.route("/")
def index():
    # 新着の未発売カードを覚えていれば HTML に埋め込む（画像を早く読み始める。DB は待たない・card_display.cached_new_cards）
    initial = _card_display.cached_new_cards(limit=36)
    return render_template("index.html", card_name="", page_mode="search",
                           new_cards_initial=initial, new_cards_first=_new_cards_first_html(initial))


@app.route("/card/<path:card_name>")
def card_page(card_name):
    """カード個別ページ — SEO用"""
    card_name = card_name.strip()
    if not card_name or len(card_name) > 50:
        return render_template("index.html", card_name="", page_mode="search"), 404

    # キャッシュに価格データがあれば meta 用に渡す
    cached = cache_get(card_name)
    best_price = None
    shop_count = 0
    if cached:
        in_stock = [r for r in cached if not r.get("sold_out")]
        if in_stock:
            best = min(in_stock, key=lambda x: x["price"])
            best_price = best["price"]
        shop_count = len(set(r["shop"] for r in cached))

    return render_template("index.html",
                           card_name=card_name,
                           page_mode="search",
                           best_price=best_price,
                           shop_count=shop_count)


@app.route("/card/by-cid/<int:cid>")
def card_by_cid(cid):
    """ニューロン連携 — cid（konami_id）で受けてカード個別ページへ転送する。
    ?name= は拡張が併送するフォールバック名（ygores未収録カード用）。
    ロジックは neuron_link.resolve_card_name 参照。"""
    name = resolve_card_name(cid, request.args.get("name", ""),
                             _ygores_repo.get_card_summary)
    if not name:
        return render_template("index.html", card_name="", page_mode="search"), 404
    return redirect(url_for("card_page", card_name=name), code=302)


@app.route("/card-wiki")
def card_wiki():
    """カードのページの「Wiki」リンク — 遊戯王カードWiki のカードのページへ転送する。
    ページ名の表記ゆれで見つからなければ wiki のタイトル検索へ。ロジックは wiki_link 参照。
    ?cid=（konami_id）があれば正式名に直してから引く（読みの名前のページ対策・wiki_link の説明参照）。"""
    name = request.args.get("name", "")
    cid = request.args.get("cid", type=int)
    if cid:
        name = resolve_card_name(cid, name, _ygores_repo.get_card_summary) or name
    url = resolve_wiki_url(name)
    if not url:
        return "name required", 400
    return redirect(url, code=302)


@app.route("/buy/<path:card_name>")
def buy_page(card_name):
    """買取個別ページ — SEO用"""
    card_name = card_name.strip()
    if not card_name or len(card_name) > 50:
        return render_template("index.html", card_name="", page_mode="buyback"), 404

    cached = buyback_cache_get(card_name)
    # 検索結果の説明文・構造化データに出す最高買取額。未開封・付属品つきは入れない（_best_buy_item。2026-10-28）
    best = _best_buy_item([r for r in (cached or []) if r.get("price")])
    best_price = best["price"] if best else None

    return render_template("index.html",
                           card_name=card_name,
                           page_mode="buyback",
                           best_price=best_price,
                           shop_count=0)


@app.route("/sitemap.xml")
def sitemap():
    """動的サイトマップ — 検索されたカードを自動収集"""
    from flask import make_response
    base_url = _canonical_base_url()

    # ランキングからカード名を取得
    cards = _get_trending(limit=30)
    card_names = [c["name"] for c in cards]

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    # トップページ
    xml += f'  <url><loc>{base_url}/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>\n'
    # 固定ページ
    xml += f'  <url><loc>{base_url}/solitaire</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>\n'
    xml += f'  <url><loc>{base_url}/new-cards</loc><changefreq>daily</changefreq><priority>0.6</priority></url>\n'
    xml += f'  <url><loc>{base_url}/tcg-preview</loc><changefreq>weekly</changefreq><priority>0.5</priority></url>\n'
    # 予告中の英語版の新弾のまとめページ（2026-10-29。取得に失敗しても他のページは出す）
    try:
        for a in _tcg_announce_view.list_sets():
            xml += f'  <url><loc>{base_url}/tcg-preview/{a["set"]["slug"]}</loc><changefreq>daily</changefreq><priority>0.6</priority></url>\n'
    except Exception:
        logger.exception("サイトマップ: 予告中の新弾の取得失敗（省いて続ける）")
    # カード個別ページ
    for name in card_names:
        from urllib.parse import quote
        xml += f'  <url><loc>{base_url}/card/{quote(name)}</loc><changefreq>daily</changefreq><priority>0.8</priority></url>\n'
        xml += f'  <url><loc>{base_url}/buy/{quote(name)}</loc><changefreq>daily</changefreq><priority>0.7</priority></url>\n'
    xml += '</urlset>'

    resp = make_response(xml)
    resp.headers["Content-Type"] = "application/xml"
    return resp


@app.route("/robots.txt")
def robots():
    """robots.txt"""
    from flask import make_response
    base_url = _canonical_base_url()
    # /sync はワンタイムリンクのトークンがクエリ文字列に載るため検索エンジンにインデックスさせない
    # （設計文書 §7.2・§12。GA4もこのページには載せていない）
    txt = f"User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /sync\n\nSitemap: {base_url}/sitemap.xml\n"
    resp = make_response(txt)
    resp.headers["Content-Type"] = "text/plain"
    return resp


@app.route("/sync")
def sync_page():
    """端末間同期のワンタイムリンク着地ページ（設計文書 §7.2）。

    index.html とは別の最小テンプレート（GAタグ無し）で配信する。/sync?t=<トークン> を
    GA4入りのindex.htmlで配信すると、GA4が既定でクエリ文字列込みのURLを外部送信するため
    トークンがGoogleに漏れる（§12）。robots.txt にも Disallow: /sync を追加済み。
    """
    return render_template("sync.html")


# ── カード名サジェスト（ローカルファイル参照）──

_cardnames: list[str] = []
_cardnames_set: set[str] = set()               # 存在チェック用（O(1)）
_cardnames_fuzzy: dict[str, list[str]] = {}   # fuzzy_key → [正式カード名]
_cardnames_reading: dict[str, str] = {}       # 読み仮名 → 正式カード名
_cardnames_reading_fuzzy: dict[str, str] = {} # 読み仮名(fuzzy_key) → 正式カード名
_cardnames_loaded = False
# 排他ロック: 未ロード時に複数スレッドが同時に到達すると全員がJSON読込・辞書構築を
# 並列実行してしまうため（_ygores_index_lock と同じ設計）、最初の1スレッドだけが
# 構築し他は待つ
_cardnames_lock = threading.Lock()

def _load_cardnames():
    """data/cardnames_ja.json と reading マップをメモリに読み込む"""
    global _cardnames, _cardnames_set, _cardnames_fuzzy, _cardnames_reading, _cardnames_reading_fuzzy, _cardnames_loaded
    if _cardnames_loaded:
        return
    with _cardnames_lock:
        # ロック待ちの間に他スレッドが構築済みなら再利用
        if _cardnames_loaded:
            return
        _load_cardnames_locked()

def _load_cardnames_locked():
    """_load_cardnames のロック内本体（ダブルチェックロックのため分離）"""
    global _cardnames, _cardnames_set, _cardnames_fuzzy, _cardnames_reading, _cardnames_reading_fuzzy, _cardnames_loaded
    base = os.path.dirname(__file__)
    cardnames_path = os.path.join(base, "data", "cardnames_ja.json")
    reading_path = os.path.join(base, "data", "cardnames_reading.json")
    try:
        with open(cardnames_path, "r", encoding="utf-8") as f:
            _cardnames = json.loads(f.read())
        # あいまい検索用のインデックスを構築
        fuzzy = {}
        for name in _cardnames:
            key = _fuzzy_key(name)
            fuzzy.setdefault(key, []).append(name)
        _cardnames_set = set(_cardnames)
        _cardnames_fuzzy = fuzzy
        logger.info(f"カード名データベース読込: {len(_cardnames)} 件")
    except FileNotFoundError:
        logger.warning("data/cardnames_ja.json が見つかりません。update_cardnames.py を実行してください。")
        _cardnames = []
        _cardnames_set = set()
    # 読み仮名→正式名称マップ
    try:
        with open(reading_path, "r", encoding="utf-8") as f:
            _cardnames_reading = json.loads(f.read())
        # 読み仮名のfuzzyキーマップも構築（記号除去版で引けるように）
        _cardnames_reading_fuzzy = {_fuzzy_key(r): formal for r, formal in _cardnames_reading.items()}
        logger.info(f"読み仮名マップ読込: {len(_cardnames_reading)} 件")
    except FileNotFoundError:
        _cardnames_reading = {}
        _cardnames_reading_fuzzy = {}
    _cardnames_loaded = True

@app.route("/api/suggest")
def api_suggest():
    """カード名の入力補完候補を返す（あいまい検索対応）"""
    _load_cardnames()
    q = _normalize_query(request.args.get("q", ""))
    if not q or len(q) < 1:
        return jsonify([])

    q_lower = q.lower()
    q_fuzzy = _fuzzy_key(q)

    # 前方一致を優先、次に部分一致、最後にあいまい一致
    prefix_matches = []
    contains_matches = []
    fuzzy_matches = []
    seen = set()
    for name in _cardnames:
        name_lower = name.lower()
        if name_lower.startswith(q_lower):
            prefix_matches.append(name)
            seen.add(name)
        elif q_lower in name_lower:
            contains_matches.append(name)
            seen.add(name)
        if len(prefix_matches) + len(contains_matches) >= 15:
            break

    # あいまい検索（記号除去して比較）: 通常一致が少ない場合のみ
    if len(prefix_matches) + len(contains_matches) < 10 and q_fuzzy:
        for name in _cardnames:
            if name in seen:
                continue
            name_fuzzy = _fuzzy_key(name)
            if name_fuzzy.startswith(q_fuzzy) or q_fuzzy in name_fuzzy:
                fuzzy_matches.append(name)
                seen.add(name)
                if len(prefix_matches) + len(contains_matches) + len(fuzzy_matches) >= 15:
                    break

    # 読み仮名検索: まだ候補が少ない場合、読み仮名から正式名称を引く
    reading_matches = []
    total = len(prefix_matches) + len(contains_matches) + len(fuzzy_matches)
    if total < 10 and _cardnames_reading:
        for reading, formal in _cardnames_reading.items():
            if formal in seen:
                continue
            if reading.startswith(q_lower) or q_lower in reading:
                reading_matches.append(formal)
                seen.add(formal)
                if total + len(reading_matches) >= 15:
                    break

    results = (prefix_matches + contains_matches + fuzzy_matches + reading_matches)[:10]

    # include_unreleased=1 のときのみ、未発売カードを候補に統合してオブジェクト配列で返す。
    # param 無しの既存呼び出し（価格検索サジェスト等）は従来どおり文字列配列のままにする（ゼロ回帰）。
    if request.args.get("include_unreleased") == "1":
        released_set = set(results)  # 同名は発売済みを優先（重複排除用）
        # 未発売カード名を発売済みと同じ前方/部分一致でフィルタ（初版は reading/fuzzy 非対応）
        unreleased_hits = []
        for name in _card_display.get_unreleased_names():
            if name in released_set:
                continue  # 発売済みと同名 → 発売済みを優先
            name_lower = name.lower()
            if name_lower.startswith(q_lower) or q_lower in name_lower:
                unreleased_hits.append(name)
        # 発売済み優先 → 未発売の順で連結、上限10件。
        # 未発売候補でも release_date が到来済みなら発売済み扱い（unreleased=false）にして、
        # 選択時に価格検索へ進ませる（週次インデックス未反映のラグ期間の誤分類対策）。
        rel_map = _card_display.get_unreleased_release_map()
        today = datetime.now(JST).strftime("%Y-%m-%d")
        objs = [{"name": n, "unreleased": False} for n in results]
        for n in unreleased_hits:
            rel = rel_map.get(n, "")
            still_unreleased = not (rel and rel <= today)
            objs.append({"name": n, "unreleased": still_unreleased})
        # TCG先行カード（英語版で先に出て日本に無いカード）を英語名の前方一致 → 部分一致で最後に足す。
        # 価格が無いので unreleased=true（選んでも価格検索へ進ませない）に tcg_preview=true の印を付ける
        if len(objs) < 10:
            taken = {o["name"] for o in objs}
            tcg_prefix, tcg_contains = [], []
            for n in _card_display.get_tcg_preview_names():
                if n in taken:
                    continue
                n_lower = n.lower()
                if n_lower.startswith(q_lower):
                    tcg_prefix.append(n)
                elif q_lower in n_lower:
                    tcg_contains.append(n)
            for n in tcg_prefix + tcg_contains:
                objs.append({"name": n, "unreleased": True, "tcg_preview": True})
        return jsonify(objs[:10])

    return jsonify(results)


@app.route("/api/tcg-preview-names")
def api_tcg_preview_names():
    """TCG先行カード（英語版で先に出て日本に無いカード）の英語名の一覧。
    画面（static/shared/tcg-preview.js）が購入候補に入れる操作を出さないために使う。週1回しか増えないので5分キャッシュ。
    取得に失敗したときは空の一覧をキャッシュさせない（その間ずっと購入候補の操作が出てしまうため）。
    """
    if not _card_display.tcg_preview_data_available():
        resp = jsonify({"names": [], "unavailable": True})
        resp.headers["Cache-Control"] = "no-store"
        return resp
    resp = jsonify({"names": sorted(_card_display.get_tcg_preview_names())})
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@app.route("/api/tcg-preview-renames")
def api_tcg_preview_renames():
    """日本に来た TCG先行カードの 英語名 → 日本語の正式名。画面（static/shared/tcg-rename.js）が保存済みデッキの名前を書き換えるために使う。
    取得に失敗したときは 503（空の一覧を成功として返すと「書き換え対象なし」と区別できないため）。失敗はキャッシュさせない。
    """
    try:
        renames = _card_display.get_tcg_preview_renames()
    except _card_display.DisplayDataUnavailable:
        resp = jsonify({"error": "日本に来たカードの一覧を取得できません"})
        resp.status_code = 503
        resp.headers["Cache-Control"] = "no-store"
        return resp
    resp = jsonify({"renames": renames})
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


def _is_tcg_preview_name(name: str) -> bool:
    """TCG先行カード（英語版で先に出て日本に無いカード）の英語名か。価格が無いので店舗検索・記録に入れない。
    発売済みのカード辞書にある名前は発売済みを優先する（card_display の解決順と同じ）。"""
    if _cardnames_set and name in _cardnames_set:
        return False
    return _card_display.get_tcg_preview_proxy(name) is not None


def _wish_unreleased_mark(name: str) -> dict | None:
    """購入候補のカードがまだ店に並ばないカードなら印 {"release_date", "tcg_preview"} を返す。無ければ None。

    未発売カード（発売日が未定か未来）と TCG先行カード（日本に無い英語名）が対象。発売済みの
    カード辞書にある名前は発売済みを優先する（_is_tcg_preview_name・/api/validate と同じ順）。
    「どこで買うと安いか」が店へ探しに行って「見つからなかった」と出していたのを分ける（2026-10-30）。
    """
    if _cardnames_set and name in _cardnames_set:
        return None
    release_date = _card_display.get_still_unreleased_release_date(name)
    if release_date is not None:
        return {"release_date": release_date, "tcg_preview": False}
    if _is_tcg_preview_name(name):
        return {"release_date": "", "tcg_preview": True}
    return None


def _is_release_passed_unreleased(name: str) -> bool:
    """週次インデックス（_cardnames_set / cardnames_ja.json）未反映だが、
    未発売テーブルに登録済みで release_date が到来済みのカードか。

    cardnames_ja.json は週次更新のため、発売日〜翌週次反映までの間、発売済みカードが
    このインデックスに載らない。検索/登録ゲートで「既知カード」として扱うため、
    release_date が今日(JST)以前なら発売済みとみなす。validate と同じ判定を
    検索・登録エンドポイントでも共有する。
    """
    proxy = _card_display.get_unreleased_proxy(name)
    if not proxy:
        return False
    rel = proxy.get("release_date") or ""
    return bool(rel) and rel <= datetime.now(JST).strftime("%Y-%m-%d")


@app.route("/api/validate")
def api_validate():
    """カード名の存在チェック — 補正候補があれば suggestion として返す"""
    _load_cardnames()
    q = _normalize_query(request.args.get("q", ""))
    if not q:
        return jsonify({"valid": False, "suggestion": None})
    # DB上の正式名称に補正を試みる
    corrected = _correct_cardname(q)
    if corrected in _cardnames_set:
        # 補正成功、またはそのまま一致
        return jsonify({"valid": True, "name": corrected})
    # 発売済みに無い場合、未発売カード（approved/linked かつ hidden=false）を確認。
    # 価格検索の前段で未発売を判定し、フロント側で価格検索をスキップして
    # カード情報パネルのみを表示できるようにする（unreleased=true を付与）。
    proxy = _card_display.get_unreleased_proxy(q)
    if proxy:
        # 発売日が到来済みなら「発売済み」として価格検索へ進ませる。
        # cardnames_ja.json（_cardnames_set）は週次更新で鮮度が低く、発売日〜翌週次
        # 反映までの間は発売済みカードがこのインデックスに載らない。release_date を
        # 真値として使い、発売日到来済みカードを未発売と誤分類しないようにする。
        rel = proxy.get("release_date") or ""
        today = datetime.now(JST).strftime("%Y-%m-%d")
        if rel and rel <= today:
            return jsonify({"valid": True, "name": q})
        # 名前は登録上の正式名で返す。q はダッシュを半角に統一済みで、全角ハイフン入りのカード
        # （例: 太陽神の処刑人－マキュラ）を q のまま返すと、画面がその名前で画像・カード情報を引いて空になっていた
        return jsonify({"valid": True, "name": proxy.get("name") or q, "unreleased": True})
    # TCG先行カード（英語名）: 価格が無いので未発売と同じく価格検索をせず、カード情報だけを出させる
    tcg_proxy = _card_display.get_tcg_preview_proxy(q)
    if tcg_proxy:
        return jsonify({"valid": True, "name": tcg_proxy["name"], "unreleased": True, "tcg_preview": True})
    # 補正できなかった（corrected == q のまま、かつDBに存在しない）
    # TODO: _correct_cardname()が部分一致候補を返せるようになったらsuggestionを活用
    return jsonify({"valid": False, "suggestion": None})


def _persist_card_name(card_name: str) -> str:
    """価格の記録に書くカード名。収集（collect_prices の canonicalize_tracked_name(normalize_card_name(...))）と同じく、
    全角の英数字を半角にしてから正式名に直す。正式名の一覧に無い新しいカードを全角のまま書くと、収集が半角で書いた行と
    別のカードとして名簿（card_dim）に分かれる（2026-11-01。名簿には初期の作成時の全角・半角の2行が51組あった）。
    正式名そのものはそのまま使う（一覧に全角・半角だけ違う重複が22件あり、そろえると正式名から外れるため。reviewer 中1）"""
    name = card_name or ""
    if name in _cardnames_set:
        return name
    return _correct_cardname(normalize_card_name(name))


def _read_recent_rows(name: str, days: list[str]):
    """name の price_history を days の日付で読む（浅い結果の書き込みの判断用）。戻り値: (行, カード番号) / 読めなければ None。
    共有の実行枠（featured_matrix._executor）は使わず、このスレッドで2回だけ読む（パックの表・購入候補の読みを待たせない。reviewer 中2）"""
    try:
        lookup = db_lookup_names([name])
        dims = _supabase_client.table("card_dim").select("card_id, card_name").in_("card_name", list(lookup)).execute().data or []
        ids = sorted(pick_card_ids(dims, lookup))
        if not ids:
            return [], []
        # 1枚×2日なので1ページに収まる（店6×レアリティ×日2で数十行）。読み出しの決まり（range＋order）に合わせて書く
        rows = (_supabase_client.table("price_history")
                .select("card_id, shop, rarity, min_price, min_price_any, code, url, recorded_at")
                .in_("recorded_at", days).in_("card_id", ids)
                .order("recorded_at").order("card_id").order("shop_id").order("rarity_id")
                .range(0, 999).execute().data) or []
        return rows, ids
    except Exception as e:
        logger.warning(f"[persist] {name} の当日・前日の記録を読めないので浅い結果は書かない: {e}")
        return None


def _has_art_rows_today(ids: list[int], today: str) -> bool:
    """このカードに当日の絵ごとの内訳（price_history_art）があるか。読めなければ「ある」とみなす（内訳を消さない側）"""
    if not ids:
        return False
    try:
        return bool(_supabase_client.table("price_history_art").select("card_id")
                    .in_("card_id", ids).eq("recorded_at", today).limit(1).execute().data)
    except Exception:
        return True


def _persist_shallow(card_name: str, results: list):
    """浅い取り方の店（トレコロの1ページ目・カードラボのカード名の検索）の結果を記録に書く（2026-11-01）。
    price_persist.shallow_rows_to_write の決まりで、新しいカード・当日に無いレアリティは足し、当日の値下がりは下げ、
    前日の記録だけある深夜は書かない。カードラボは /api/search・fresh・まとめて検索のどこから来てもここを通す
    （以前は「その日の行が無いときだけ」書いていて、深夜の検索がその日の最初の行になり前日の巡回の行を押しのけていた）。
    下げる行の上書きは、本表の RPC が同じ組の絵ごとの内訳を消すので、当日の内訳があるカードでは下げない（reviewer 高2）。"""
    if not _supabase_client or not results:
        return
    name = _persist_card_name(card_name)
    today = datetime.now(JST).strftime("%Y-%m-%d")
    yesterday = (datetime.now(JST) - timedelta(days=1)).strftime("%Y-%m-%d")
    got = _read_recent_rows(name, [today, yesterday])
    if got is None:
        return
    existing, ids = got
    try:
        adds, lowers = shallow_rows_to_write(build_min_price_rows(name, results, today), existing, today)
        if adds:
            upsert_price_rows(_supabase_client, adds, ignore_duplicates=True)
        if lowers:
            if _has_art_rows_today(ids, today):
                logger.info(f"[persist] {name}: 当日の絵ごとの内訳があるので浅い結果の値下げは書かない（{len(lowers)}行）")
            else:
                upsert_price_rows(_supabase_client, lowers)
        # 絵ごとの内訳は、その店にまだ記録が無いとき（新しいカード）だけ足す
        known_shops = {r.get("shop") for r in existing}
        art_rows = [r for r in build_art_price_rows(name, results, today) if r["shop"] not in known_shops]
        upsert_price_art_rows(_supabase_client, art_rows, ignore_duplicates=True)
    except Exception as e:
        logger.warning(f"[persist] {name} の浅い結果の書き込み失敗: {e}")


def _persist_deck_async(card_name: str, deep_results: list, shallow_results: list):
    """ふだんのまとめて検索（/api/deck・fresh なし）の結果を記録に書く（2026-11-01）。
    deep_results: 全ページを読んだ店（遊々亭・カードラッシュ・まんぞく屋・カーナベル）。その日の記録に上書きする（/api/search と同じ）
    shallow_results: トレコロ（1ページ目だけ）・カードラボ。_persist_shallow の決まりで書く。
    以前はどちらも「その日の行が無いときだけ」書いていたため、深夜0時以降の浅い結果がその日の最初の行になり、
    深い収集の前日の行を押しのけていた（TASKS の深夜の件）。"""
    if deep_results:
        _persist_scrape_async(card_name, deep_results)
    if shallow_results:
        _persist_shallow(card_name, shallow_results)


def _persist_scrape_async(card_name: str, scrape_results: list, ignore_duplicates: bool = False):
    """scrape 結果を price_history に即時 upsert する。fire-and-forget。
    /api/search や /api/deck で取得した最新価格を DB に流し、
    購入候補リストのレアリティ候補ドロップダウンが即座に更新されるようにする。

    ignore_duplicates: True の場合、その日既に行があれば書き込まない
    （フェーズ3 P6b。/api/deck は店舗を1ページ目に制限した網羅性の低いスクレイプの
    ため、夜間収集の網羅的な最安値を同日上書きしないようにする。/api/search は
    全ページ取得のため既定の False のまま last-write-wins を維持する）。

    カードラボだけは呼び出し元の指定に関わらず _persist_shallow の決まりで書く
    （docs/audit-403-plan-2026-08-08.md M1。夜間収集がカタログ巡回（全数網羅）に
    切り替わったため、ライブ検索由来の行（取りこぼしあり）で同日の網羅的な最安値を
    上書きしない。2026-11-01 から、当日の値下がりと当日に無いレアリティは反映し、前日だけの深夜は書かない）。
    """
    if not _supabase_client or not scrape_results:
        return
    card_name = _persist_card_name(card_name)
    clabo_results = [r for r in scrape_results if r.get("shop") == "カードラボ"]
    try:
        today = datetime.now(JST).strftime("%Y-%m-%d")
        other_results = [r for r in scrape_results if r.get("shop") != "カードラボ"]
        if other_results:
            rows = build_min_price_rows(card_name, other_results, today)
            upsert_price_rows(_supabase_client, rows, ignore_duplicates=ignore_duplicates)
        # 絵ごとの内訳（別表）。絵の判定は全店の行をまとめて見る（型番ごとの絵をそろえるため）。
        # 上書きの決まりは本表と同じ。カードラボの内訳は _persist_shallow が書く
        art_rows = build_art_price_rows(card_name, scrape_results, today)
        upsert_price_art_rows(_supabase_client, [r for r in art_rows if r["shop"] != "カードラボ"],
                              ignore_duplicates=ignore_duplicates)
    except Exception as e:
        logger.warning(f"[persist] {card_name} の即時 upsert 失敗: {e}")
    if clabo_results:
        _persist_shallow(card_name, clabo_results)


@app.route("/api/search")
def api_search():
    original_name = _normalize_query(request.args.get("q", ""))
    if not original_name:
        return jsonify({"error": "カード名を入力してください"}), 400
    if len(original_name) > MAX_CARD_NAME_LEN:
        return jsonify({"error": "カード名が長すぎます"}), 400

    # カード名の存在チェック — DBに無い場合はあいまい検索・読み仮名で自動補正
    _load_cardnames()
    card_name = _correct_cardname(original_name)
    corrected = card_name if card_name != original_name else ""

    ALL_SHOP_NAMES = [name for name, _ in SHOPS]
    selected = request.args.getlist("shops") or DEFAULT_SHOPS
    selected = [s for s in selected if s in ALL_SHOP_NAMES]
    if not selected:
        return jsonify({"error": "有効な店舗を選択してください"}), 400

    active_shops = [(name, fn) for name, fn in SHOPS if name in selected]

    rate_error = _consume_rate_limit()
    if rate_error:
        return rate_error

    # 第1層: DB非存在のカード名はconfirmedフラグなしでは拒否（API防御）
    # ただし release_date 到来済みの未発売テーブル登録カード（週次インデックス未反映の
    # 発売済みカード）は既知カードとして許可する。
    confirmed = request.args.get("confirmed", "").lower() == "true"
    if not confirmed and card_name not in _cardnames_set and not _is_release_passed_unreleased(card_name):
        return jsonify({"error": "該当するカード名が見つかりません"}), 404

    # キャッシュ: 店舗単位で命中分と不足分を分ける（店舗束キャッシュ）。
    # 「調べていない店舗」を「0件」と混同しないため、不足店舗だけスクレイプする
    cached_map, missing_shops = cache_get_shops(card_name, selected)
    cached_results = [r for rs in cached_map.values() for r in rs]

    if not missing_shops:
        if cached_results:
            _record_search(card_name)
        # 全店キャッシュ命中の経路でも型番補完を通す。ここを素通しにすると、
        # キャッシュTTL(15分)の間ずっと補完前の値（カーナベルの弾略号・トレコロの
        # 接尾辞つき）が返り、補完を入れた意味がほぼ無くなる（大半の検索がこの経路）。
        # 補完は純粋なメモリ処理なので、キャッシュ命中の速さは損なわない。
        cached_results = infer_overframe(infer_codes(cached_results))
        def cached_stream():
            # 最初に調べる店の一覧を送る（画面は進み具合の札を全店ぶん先に並べる。1枚ずつ足すと既存の札が
            # 細くなって店名が折り返し、検索中の画面がずれていた。2026-10-22 スマホで CLS 0.019）
            yield _sse({"type": "shops", "shops": [name for name, _ in active_shops]})
            for shop_name, _ in active_shops:
                count = len(cached_map.get(shop_name, []))
                yield _sse({"type": "shop_done", "shop": shop_name, "count": count, "cached": True})
            yield _sse({**_build_done(cached_results, corrected, card_name),
                        **search_coverage(selected, cached_map, cached_results)})
        return Response(cached_stream(), mimetype="text/event-stream")

    shops_to_scrape = [(name, fn) for name, fn in active_shops if name in missing_shops]

    def generate():
        # 調べる店の一覧を最初に送る（cached_stream と同じ理由）
        yield _sse({"type": "shops", "shops": [name for name, _ in active_shops]})

        # 補正があった場合、補正通知を送信
        if corrected:
            yield _sse({"type": "corrected", "original": original_name, "corrected": corrected})

        # キャッシュ命中店舗は即時に確定表示（逐次表示用に結果も送る）
        for shop_name, _ in active_shops:
            if shop_name in cached_map:
                yield _sse({"type": "shop_done", "shop": shop_name,
                            "count": len(cached_map[shop_name]),
                            "results": cached_map[shop_name], "cached": True})

        # 不足店舗を「検索中」に
        for shop_name, _ in shops_to_scrape:
            yield _sse({"type": "progress", "shop": shop_name})

        # 取りに行く店すべてに前回の結果があれば、店へ取りに行く前にそれで表を先に描かせる
        # （画面は「○分前の価格」と添えて出し、done で最新に差し替える。2026-11-01 ユーザー判断 案A。
        # 待たされる検索が4〜18秒かかり、その間なにも出なかった）。
        # 型番の補完（infer_codes）は通さない: キャッシュの行は保存の前に補完済みで、前回の結果は補完の材料にしない
        # （混雑時の代わりの表示と同じ。2026-10-22 reviewer 低2）。OF のレアリティのそろえだけ通す
        # 組み立ての失敗（古い形のキャッシュの行など）で検索そのものを落とさない。先出しを飛ばして続ける（reviewer 低1）
        try:
            preview_rows, preview_age = _stale_preview_rows(
                cache_get_shops_stale, card_name, [name for name, _ in shops_to_scrape])
            preview_event = None
            if preview_rows is not None:
                preview = _build_done(infer_overframe(list(cached_results) + preview_rows), corrected, card_name)
                preview_event = _sse({**preview, "type": "preview", "age_minutes": preview_age})
        except Exception:
            logger.warning("前回の結果の先出しを組み立てられませんでした（飛ばします）: %s", card_name, exc_info=True)
            preview_event = None
        if preview_event is not None:
            logger.info("[先出し] %s・%d分前・%d行", card_name, preview_age, len(preview_rows))
            yield preview_event

        all_results = list(cached_results)
        scraped_items = []  # 今回スクレイプで得た行（persist用。キャッシュ命中分を含まない）
        scraped_ok = {}  # 取得に成功した店舗のみキャッシュする（失敗店舗は次回再試行）
        busy_shops = []  # 混雑で順番が回ってこなかった店（前回の結果を代わりに出す）
        stale_rows = []  # その店の前回の結果（完了の直前に結果へ足す）
        search_request = _shop_search_pool.request(PRIORITY_SINGLE, SHOP_QUEUE_TIMEOUT_SINGLE)
        disconnected = False

        try:
            # 全利用者が共有する店舗検索の列へ並べる（単品検索は一括検索より先に取られる）。
            if shops_to_scrape:
                futures = {
                    _submit_shop_fetch(search_request, name, fn, card_name): name
                    for name, fn in shops_to_scrape
                }
                for future in search_request.as_completed(futures, heartbeat=SEARCH_HEARTBEAT_SEC):
                    if future is None:
                        # 順番待ちの合図（画面は該当店を「順番待ち」にする。無応答タイムアウトも延びる）
                        yield _sse({"type": "waiting", "queued_shops": [
                            futures[f] for f in futures if search_request.is_queued(f)]})
                        continue
                    shop_name = futures[future]
                    if _is_shop_busy(future.exception()):
                        # 待ち時間（または受付件数）の上限で取れなかった店だけ「混雑で取得できず」。
                        # 手元の前回の結果で代わりに埋める（キャッシュにも DB にも書き戻さない。
                        # 型番の補完・人気の集計にも使わないので、完了の直前まで別に持つ）
                        stale_results, stale_at = _stale_shop_fallback(cache_get_shops_stale, card_name, shop_name)
                        busy_shops.append(shop_name)
                        stale_rows.extend(stale_results)
                        yield _sse({"type": "shop_error", "shop": shop_name,
                                    "error": SHOP_BUSY_MESSAGE, "busy": True})
                        yield _sse({"type": "shop_done", "shop": shop_name, "count": len(stale_results),
                                    "results": stale_results, "status": "busy",
                                    "stale": stale_at is not None, "stale_at": stale_at})
                        continue
                    error = None
                    try:
                        results, fetch_errors = _shop_fetch_result(future)
                    except Exception as exc:
                        results, fetch_errors, error = [], 1, str(exc)
                        logger.warning("店舗取得失敗: %s", shop_name, exc_info=True)
                    _record_shop_health_once(future, shop_name, results, fetch_errors)
                    if error or fetch_errors:
                        yield _sse({"type": "shop_error", "shop": shop_name,
                                    "error": "店舗からの取得に失敗しました"})
                    # 店舗完了時に結果データも送信（逐次表示用）
                    yield _sse({"type": "shop_done", "shop": shop_name, "count": len(results), "results": results,
                                "status": "partial" if (error or fetch_errors) else "ok"})
                    all_results.extend(results)
                    scraped_items.extend(results)
                    # 「0件かつ取得エラーなし」は在庫なしとしてキャッシュ対象。
                    # 例外・取得エラーありは書かない＝15分間「0件」に固定しない
                    if error is None and fetch_errors == 0:
                        scraped_ok[shop_name] = results

            # 同じカードの全店ぶん（キャッシュ命中分含む）が揃った直後に型番を補完する
            # （保存前）。カーナベルの弾略号・トレコロCBの接尾辞つき値を、他店の
            # 完全な型番で補う。infer_codes は新しい dict のリストを返す（元のリストは
            # 変更しない）ため、店舗別の内訳（scraped_ok・scraped_items）も
            # 補完後の値で作り直す。
            all_results = infer_overframe(infer_codes(all_results))
            corrected_by_shop: dict = {}
            for r in all_results:
                corrected_by_shop.setdefault(r.get("shop"), []).append(r)
            scraped_ok = {name: corrected_by_shop.get(name, []) for name in scraped_ok}
            scraped_items = [
                r for name, _ in shops_to_scrape for r in corrected_by_shop.get(name, [])
            ]

            cache_store_shops(card_name, scraped_ok)
            if all_results:
                _record_search(card_name)
                # 検索が成功したカードを収集対象に自動登録（次回 collect_prices で永続化）
                if _supabase_client:
                    Thread(target=_track_card_async, args=(card_name,), daemon=True).start()
                    # scrape 結果を price_history に即時 upsert（購入候補の rarity 候補を即時化）。
                    # キャッシュ命中分は再送しない（初回スクレイプ時の書き込み方針に従う。
                    # /api/deck 由来なら ignore_duplicates=True で書かれていないこともある）
                    if scraped_items:
                        Thread(target=_persist_scrape_async, args=(card_name, list(scraped_items)), daemon=True).start()
            # 混雑した店の前回の結果は、保存・記録が終わってから表示用に足す
            # （反映前のキャッシュの行もあるので、足したあと OF のレアリティのそろえをもう一度通す）
            all_results = infer_overframe(all_results + stale_rows)
        except GeneratorExit:
            # 利用者が画面を閉じた。まだ始まっていない取得は finally で取り消す
            disconnected = True
            raise
        except Exception as e:
            logger.error(f"検索処理で予期しないエラー: {e}")
        finally:
            search_request.close()
            # エラーが起きても必ず完了メッセージを送る（切断後は送れないので送らない）
            if not disconnected:
                yield _sse({**_build_done(all_results, corrected, card_name),
                            **search_coverage(selected, set(cached_map) | set(scraped_ok), all_results),
                            "busy_shops": busy_shops})

    release = _search_stream_slots.try_open()
    if release is None:
        return _search_busy_response()
    return _search_stream_response(generate(), release)


# ── ランキング・設定 ──

@app.route("/api/deck", methods=["GET", "POST"])
def api_deck():
    """デッキ一括検索 — 複数カード名を受け取り、各カードの最安値をSSEで返す

    v3: 高速化 — 各店舗1ページ目のみ取得 + カード5枚同時並列
    POST も受け付ける（最新弾の収録カード一括検索など、カード数が多くGETの
    URL長制限（gunicorn limit_request_line）を超えるケース向け）。
    """
    from functools import partial
    from scraper import scrape_torecolo

    # GET=クエリ / POST=フォームボディ の双方を request.values で吸収（GET挙動は不変）
    names_raw = request.values.get("cards", "")
    shops_raw = request.values.getlist("shops") or DEFAULT_SHOPS
    # 購入候補の「全店舗ランキング」用に各カードの店舗別最安値を返すフラグ。
    # "version" は新商品タブ用: 店×レアリティに加えて型番の頭（DBGV-JP001 の DBGV）でも分けて返す
    # （同じレアリティの再録版のほうが安いと、レアリティだけで最安1件に畳んだ時点でこのパックの版が消えるため。2026-10-21 reviewer 指摘）
    per_shop_mode = request.values.get("include_per_shop", "").lower()
    include_per_shop = per_shop_mode in ("1", "true", "yes", "version")
    per_shop_by_version = per_shop_mode == "version"
    # 購入候補の「店の今の価格で調べ直す」用（2026-10-31）。どの店も全ページを見て、結果をその日の行に上書きする
    # （/api/search と同じ扱い）。ふだんの一括検索は1ページ目だけの浅い結果なので、朝の収集の値を上書きしない
    fresh = request.values.get("fresh", "").lower() in ("1", "true", "yes")
    if not names_raw:
        return jsonify({"error": "カード名がありません"}), 400
    rate_error = _consume_rate_limit(bucket=_last_deck)
    if rate_error:
        return rate_error

    ALL_SHOP_NAMES = [name for name, _ in SHOPS]
    selected = [s for s in shops_raw if s in ALL_SHOP_NAMES]
    if not selected:
        return jsonify({"error": "有効な店舗を選択してください"}), 400

    # デッキ検索用: 複数ページ巡回する店舗は1ページ目のみに制限（トレコロはページごとに店へ取りに行き、負荷で応答しなくなった
    # 経緯がある）。カーナベルは1回の問い合わせで件数を指定する方式で、30件でも150件でも0.04〜0.1秒と差が無いので
    # 制限を外した（2026-11-01 実測）
    _FAST_OVERRIDES = {
        "トレコロCB": partial(scrape_torecolo, max_pages=1),
    }
    if fresh:
        _FAST_OVERRIDES = {}
    active_shops = [
        (name, _FAST_OVERRIDES.get(name, fn))
        for name, fn in SHOPS if name in selected
    ]

    _load_cardnames()
    card_entries, parse_error = _parse_deck_entries(names_raw)
    if parse_error:
        return parse_error

    # 店舗へ取りに行く検索の同時数の枠（一括検索の枠）。混雑で断る分は、下の記録・自動登録より前に返す
    # （画面が数秒後に取り直すので、記録が二重にならないようにする）
    # 調べ直し（fresh）は全ページを見て DB に上書きするので、1つの接続元から同時に流せるのは1本まで
    # （画面は80種ごとに順に送るので困らない。2026-10-31 reviewer 中3）
    fresh_release = None
    if fresh:
        fresh_release = _fresh_deck_open(_client_ip())
        if fresh_release is None:
            return jsonify({"error": "価格の更新はひとつずつ行います。終わってからもう一度お試しください"}), 429
    release = _search_stream_slots.try_open("bulk")
    if release is None:
        if fresh_release:
            fresh_release()
        return _search_busy_response()
    if fresh_release:
        release = _chain_release(release, fresh_release)
    try:
        return _search_stream_response(
            _api_deck_stream(card_entries, selected, active_shops, _FAST_OVERRIDES,
                             include_per_shop, per_shop_by_version, fresh),
            release)
    except BaseException:
        release()
        raise


_fresh_deck_lock = Lock()
_fresh_deck_active: set[str] = set()


def _fresh_deck_open(ip: str):
    """調べ直し（fresh）の接続元ごとの枠を取る。取れたら返す関数（1回だけ効く）を、取れなければ None を返す。"""
    with _fresh_deck_lock:
        if ip in _fresh_deck_active:
            return None
        _fresh_deck_active.add(ip)
    done = []

    def release():
        if done:
            return
        done.append(True)
        with _fresh_deck_lock:
            _fresh_deck_active.discard(ip)
    return release


def _chain_release(*releases):
    """複数の後始末を1つにまとめる（_search_stream_response は最後と切断時の2回呼びうる。各後始末は1回だけ効く前提）。"""
    def release():
        for r in releases:
            r()
    return release


def _api_deck_stream(card_entries, selected, active_shops, _FAST_OVERRIDES,
                     include_per_shop, per_shop_by_version, fresh=False):
    """/api/deck の本体（記録・自動登録と SSE の生成）。枠を取ったあとで呼ぶ。
    fresh: 全ページを見た結果だけを使い（1ページ目だけのキャッシュは命中にしない）、その日の行に上書きする。
    上書きは card_done を送る前に終える（画面は終わったら DB を読み直すため）。"""
    # デッキ検索カードを収集対象候補として記録（人気ランキングには影響しない）。TCG先行カードは価格が無いので記録しない
    _record_deck_search([e["name"] for e in card_entries if not _is_tcg_preview_name(e["name"])])
    # デッキ内カードを tracked_cards に自動登録（次回 collect_prices で永続化）。
    # 検索自体は全カード名で実行するが、DBへの永続化だけは /api/track-batch と同じ
    # 辞書ゲート（カード辞書に存在 or 発売済み未発売カード）を通す（スパム・誤登録対策）。
    # 既存登録は select で除外され insert されない＝冪等。fire-and-forget。
    trackable = [
        e["name"] for e in card_entries
        if not _cardnames_set or e["name"] in _cardnames_set
        or _is_release_passed_unreleased(e["name"])
    ]
    if _supabase_client and trackable:
        Thread(target=_track_cards_async, args=(trackable,), daemon=True).start()
    trackable_set = set(trackable)

    def _persist_fresh(card_name, items):
        """調べ直しの結果をその日の行に上書きする（/api/search と同じ扱い）。画面が終わったら DB を読み直すので、
        Thread に回さず書き終えてから返す。カード辞書に無い名前は書かない（自動登録と同じ関門）。
        items は全ページを見た店の結果だけを渡す（キャッシュの命中分も含める・取得に失敗した店は渡さない）"""
        if fresh and _supabase_client and items and card_name in trackable_set:
            _persist_scrape_async(card_name, list(items))

    def _aggregate_per_shop(items: list) -> dict:
        """店舗別・レアリティ別の最安値を {店舗名: {レアリティ: {price, url, rarity, ...}}} に
        集約する。sold_out は除外、同じ店舗・同じレアリティで複数ヒットがあれば最安を採用。
        レアリティが空・不明の商品は "" をキーにして残す（捨てない）。

        （2026-08-19修正: 旧実装は店舗ごとに1件へ畳んでいたため、同じ店が複数レアリティを
        在庫していても最安の1レアリティしか返らず、クライアント側のレアリティ一致判定で
        「その店にはそのレアリティが無い」と誤判定されていた）"""
        per_shop: dict = {}
        for r in items:
            # 混雑で取れなかった店の前回の結果（stale）は入れない。店別価格は購入先の決定（送料込み最安）と
            # 店舗×レアの表に使うので、古い値段で店を選ばせない（その店は failed_shops・busy_shops に残る）
            if r.get("sold_out") or r.get("stale"):
                continue
            shop = r.get("shop")
            if not shop or shop not in selected:
                continue
            rarity = r.get("rarity") or ""
            key = rarity
            if per_shop_by_version:
                # 型番の頭ごとにも分ける（キーは画面側では使わず、各品の rarity・code を読む）
                code = str(r.get("code") or "")
                key = rarity + "	" + (code.split("-", 1)[0].upper() if "-" in code else code.upper())
            by_rarity = per_shop.setdefault(shop, {})
            cur = by_rarity.get(key)
            if cur is None or r.get("price", 0) < cur.get("price", 0):
                by_rarity[key] = r
        return per_shop

    def _search_one_card(i, card_name, search_request):
        """1枚のカードを全店舗で検索して結果を返す"""
        # キャッシュチェック（店舗束）。デッキ検索は自身が partial な結果しか作らないため
        # partial エントリも命中扱いにする（完全版キャッシュがあればそちらが使われる）
        # 調べ直し（fresh）は全ページを見た結果だけを命中にする
        cached_map, missing = cache_get_shops(card_name, selected, include_partial=not fresh)
        cached_items = [r for rs in cached_map.values() for r in rs]
        if not missing:
            # /api/search の全件命中と同じく、型番の補完と OF のレアリティのそろえを通す
            cached_items = infer_overframe(infer_codes(cached_items))
            # 調べ直しでは、15分以内に誰かが全ページを調べた結果も朝の値より新しいので書き戻す（2026-10-31 reviewer 高1）
            _persist_fresh(card_name, cached_items)
            items = [r for r in cached_items if not r.get("sold_out")]
            best = min(items, key=lambda x: x["price"]) if items else None
            payload = {"type": "card_done", "index": i, "name": card_name,
                       "best": best, "count": len(items), "cached": True,
                       **search_coverage(selected, cached_map, items)}
            if include_per_shop:
                payload["per_shop"] = _aggregate_per_shop(items)
            return payload

        # 不足店舗のみ並列スクレイピング（共有の列に一括検索として並べる）
        shops_to_scrape = [(name, fn) for name, fn in active_shops if name in missing]
        scraped_items = []
        scraped_ok = {}  # 取得に成功した店舗のみキャッシュする（失敗店舗は次回再試行）
        busy_shops = []  # 混雑で順番が回ってこなかった店（前回の結果を代わりに使う）
        if shops_to_scrape:
            futures = {_submit_shop_fetch(search_request, name, fn, card_name): name
                       for name, fn in shops_to_scrape}
            for future in search_request.as_completed(futures):
                shop_name = futures[future]
                exc = future.exception()
                if _is_shop_busy(exc):
                    busy_shops.append(shop_name)
                    continue
                if isinstance(exc, SearchAbandonedError):
                    continue  # 利用者が画面を閉じた（結果を送る相手がいない）
                try:
                    items, fetch_errors = _shop_fetch_result(future)
                except Exception:
                    logger.warning("一括検索の店舗取得に失敗: %s", shop_name, exc_info=True)
                    continue
                scraped_items.extend(items)
                if fetch_errors == 0:
                    scraped_ok[shop_name] = items
        # 混雑で取れなかった店は、手元に残っている前回の結果（期限切れ・1ページ目限定も含む）で最安を出す。
        # 表示の参考にするだけで、キャッシュにも DB にも書き戻さない
        stale_items = []
        for shop_name in busy_shops:
            stale_items.extend(_stale_shop_fallback(cache_get_shops_stale, card_name, shop_name)[0])

        # 同じカードの今回スクレイプぶんが揃った直後に型番を補完する（保存前）。
        # カーナベルの弾略号・トレコロCBの接尾辞つき値を、他店の完全な型番で補う。
        # /api/deck は _FAST_OVERRIDES でトレコロ・カーナベルを1ページ目限定にしている。
        # 良質な型番を返す店（まんぞく屋・カードラボ・遊々亭・カードラッシュ）が
        # キャッシュ命中で今回スクレイプされていない場合、今回ぶんだけを候補にすると
        # 補完できる型番を取りこぼす。cached_items も候補集合に加えて補完し、
        # 保存・キャッシュ更新の対象は今回スクレイプぶんだけに絞る
        # （infer_codes は入力と同じ順序で返すため、後半のスライスが scraped 分になる）。
        _merged = infer_overframe(infer_codes(cached_items + scraped_items))
        # 表示にも補完後の値を使う（前半＝キャッシュ分。順序と件数は入力のまま）
        cached_items, scraped_items = _merged[:len(cached_items)], _merged[len(cached_items):]
        corrected_by_shop: dict = {}
        for r in scraped_items:
            corrected_by_shop.setdefault(r.get("shop"), []).append(r)
        scraped_ok = {name: corrected_by_shop.get(name, []) for name in scraped_ok}

        # トレコロ・カーナベルは1ページ目限定（_FAST_OVERRIDES）のため partial 印を付け、
        # 通常検索(/api/search)には流用させない
        cache_store_shops(card_name, scraped_ok,
                          partial_shops={n for n in scraped_ok if n in _FAST_OVERRIDES})
        # scrape 結果を price_history に即時 upsert（購入候補の rarity 候補を即時化）。
        # 全ページを読む店は上書き、浅い店は新しいカード・当日の値下がりだけ（_persist_deck_async。2026-11-01）
        if fresh:
            # 調べ直しは、キャッシュの命中分（全ページ）と今回取り切れた店だけを上書きする。取得に一部失敗した店は
            # 取れた分だけの値で朝の網羅的な値を壊さないよう書かない（画面は「前の値のまま」と出す。reviewer 高1・中2）
            _persist_fresh(card_name, cached_items + [r for rs in scraped_ok.values() for r in rs])
        elif _supabase_client and scraped_ok:
            # 取り切れた店だけ書く。全ページを読む店は上書き、浅い店（トレコロ・カードラボ）は条件つき（_persist_deck_async）
            shallow_shops = set(_FAST_OVERRIDES) | {"カードラボ"}
            deep = [r for n, rs in scraped_ok.items() if n not in shallow_shops for r in rs]
            shallow = [r for n, rs in scraped_ok.items() if n in shallow_shops for r in rs]
            Thread(target=_persist_deck_async, args=(card_name, deep, shallow), daemon=True).start()
        # 混雑時の前回の結果（stale_items）も表示前に OF の印の判定を通す（反映前のキャッシュの行のため）
        all_items = infer_overframe(cached_items + scraped_items + stale_items)
        in_stock = [r for r in all_items if not r.get("sold_out")]
        best = min(in_stock, key=lambda x: x["price"]) if in_stock else None
        payload = {"type": "card_done", "index": i, "name": card_name,
                   "best": best, "count": len(in_stock),
                   **search_coverage(selected, set(cached_map) | set(scraped_ok), all_items),
                   "busy_shops": busy_shops}
        if include_per_shop:
            payload["per_shop"] = _aggregate_per_shop(in_stock)
        return payload

    return _deck_card_stream(card_entries, selected, _search_one_card)


def _deck_card_stream(card_entries, selected, search_one_card):
    """デッキ一括検索（販売・買取）の SSE。カード単位で並列に調べ、終わった順に送る。

    1回の一括検索で1つの SearchRequest（一括検索の優先度）を共有する。利用者が切断したら
    （GeneratorExit）、まだ始まっていない取得とカードを取り消す。"""
    # カード単位で並列検索（最大5カード同時）
    CARD_PARALLEL = 5
    search_request = _shop_search_pool.request(PRIORITY_BULK, SHOP_QUEUE_TIMEOUT_BULK)
    card_executor = ThreadPoolExecutor(max_workers=CARD_PARALLEL)
    try:
        # まず全カードの card_start を送出
        for i, entry in enumerate(card_entries):
            yield _sse({"type": "card_start", "index": i, "name": entry["name"]})

        # TCG先行カード（英語版で先に出て日本に無いカード）は価格が無いので店舗へ取りに行かず、印だけ返す
        tcg_preview = {i for i, entry in enumerate(card_entries) if _is_tcg_preview_name(entry["name"])}
        for i in sorted(tcg_preview):
            yield _sse({"type": "card_done", "index": i, "name": card_entries[i]["name"],
                        "best": None, "count": 0, "tcg_preview": True})

        # 並列でカードを検索
        futures = {
            card_executor.submit(search_one_card, i, entry["name"], search_request): i
            for i, entry in enumerate(card_entries) if i not in tcg_preview
        }
        for future in iter_with_heartbeat(futures, SEARCH_HEARTBEAT_SEC):
            if future is None:
                # 順番待ちの合図。画面は読み飛ばす（切断にも次の送信で気づける）
                yield _sse({"type": "waiting"})
                continue
            try:
                result = future.result()
                yield _sse(result)
            except Exception as e:
                idx = futures[future]
                yield _sse({"type": "card_done", "index": idx,
                            "name": card_entries[idx]["name"],
                            "best": None, "count": 0,
                            **search_coverage(selected, [], [])})

        yield _sse({"type": "done"})
    finally:
        # 切断時: 始まっていない取得を取り消し、始まっていないカードも実行しない。
        # 実行中のカードは取り消された取得をすぐ受け取って終わるので、ここでは待たない
        search_request.close()
        card_executor.shutdown(wait=False, cancel_futures=True)

@app.route("/api/deck-buy", methods=["GET", "POST"])
def api_deck_buy():
    """デッキ一括買取検索 — 複数カード名の買取最高値をSSEで返す

    POST も受け付ける（カード数が多くGETのURL長制限を超えるケース向け）。
    """
    # GET=クエリ / POST=フォームボディ の双方を request.values で吸収（GET挙動は不変）
    names_raw = request.values.get("cards", "")
    shops_raw = request.values.getlist("shops") or DEFAULT_BUYBACK_SHOPS
    if not names_raw:
        return jsonify({"error": "カード名がありません"}), 400
    rate_error = _consume_rate_limit(bucket=_last_deck)
    if rate_error:
        return rate_error

    ALL_BUY_NAMES = [name for name, _ in BUYBACK_SHOPS]
    selected = [s for s in shops_raw if s in ALL_BUY_NAMES]
    if not selected:
        return jsonify({"error": "有効な店舗を選択してください"}), 400
    active_shops = [(name, fn) for name, fn in BUYBACK_SHOPS if name in selected]

    _load_cardnames()
    card_entries, parse_error = _parse_deck_entries(names_raw)
    if parse_error:
        return parse_error

    # 店舗へ取りに行く検索の同時数の枠（/api/deck と同じ一括検索の枠。記録より前に断る）
    release = _search_stream_slots.try_open("bulk")
    if release is None:
        return _search_busy_response()
    try:
        return _search_stream_response(
            _api_deck_buy_stream(card_entries, selected, active_shops), release)
    except BaseException:
        release()
        raise


def _api_deck_buy_stream(card_entries, selected, active_shops):
    """/api/deck-buy の本体（記録と SSE の生成）。枠を取ったあとで呼ぶ。"""
    # デッキ買取検索カードを収集対象候補として記録（人気ランキングには影響しない）。TCG先行カードは記録しない
    _record_deck_search([e["name"] for e in card_entries if not _is_tcg_preview_name(e["name"])])

    def _search_one_card_buy(i, card_name, search_request):
        """1枚のカードを全買取店舗で検索して最高値を返す"""
        # キャッシュチェック（店舗束）。買取デッキは全ページ取得のため partial 印は不要。
        # 将来ここに1ページ目限定の高速化を入れる場合は、販売デッキと同様に
        # include_partial=True での読み出しと partial_shops での書き込みを対にすること
        cached_map, missing = buyback_cache_get_shops(card_name, selected)
        cached_items = [r for rs in cached_map.values() for r in rs]
        if not missing:
            cached_items = infer_overframe(infer_codes(cached_items))  # 買取検索の全件命中と同じ
            best = _best_buy_item(cached_items)
            return {"type": "card_done", "index": i, "name": card_name,
                    "best": best, "count": len(cached_items), "cached": True,
                    **search_coverage(selected, cached_map, cached_items)}

        shops_to_scrape = [(name, fn) for name, fn in active_shops if name in missing]
        scraped_items = []
        scraped_ok = {}  # 取得に成功した店舗のみキャッシュする（失敗店舗は次回再試行）
        busy_shops = []  # 混雑で順番が回ってこなかった店（前回の結果を代わりに使う）
        if shops_to_scrape:
            futures = {_submit_shop_fetch(search_request, name, fn, card_name): name
                       for name, fn in shops_to_scrape}
            for future in search_request.as_completed(futures):
                shop_name = futures[future]
                exc = future.exception()
                if _is_shop_busy(exc):
                    busy_shops.append(shop_name)
                    continue
                if isinstance(exc, SearchAbandonedError):
                    continue  # 利用者が画面を閉じた
                try:
                    items, fetch_errors = _shop_fetch_result(future)
                except Exception:
                    logger.warning("一括検索の店舗取得に失敗: %s", shop_name, exc_info=True)
                    continue
                scraped_items.extend(items)
                if fetch_errors == 0:
                    scraped_ok[shop_name] = items

        buyback_cache_store_shops(card_name, scraped_ok)
        # 混雑で取れなかった店は前回の結果で最高値を出す（書き戻さない）
        stale_items = []
        for shop_name in busy_shops:
            stale_items.extend(_stale_shop_fallback(buyback_cache_get_shops_stale, card_name, shop_name)[0])
        # 表示は全店ぶんで型番の補完と OF のレアリティのそろえを通す（混雑時の前回の結果も含む）
        all_items = infer_overframe(infer_codes(cached_items + scraped_items + stale_items))
        best = _best_buy_item(all_items)
        return {"type": "card_done", "index": i, "name": card_name,
                "best": best, "count": len(all_items),
                **search_coverage(selected, set(cached_map) | set(scraped_ok), all_items),
                "busy_shops": busy_shops}

    return _deck_card_stream(card_entries, selected, _search_one_card_buy)

# ショップ名からカード検索URLを生成
def _shop_search_url(shop: str, card_name: str) -> str:
    """ショップ名とカード名から検索ページのURLを生成"""
    q = _url_quote(unicodedata.normalize('NFKC', card_name))  # ローマ数字等を半角化してURLエンコード
    urls = {
        "遊々亭": f"https://yuyu-tei.jp/sell/ygo/s/search?search_word={q}",
        "カードラッシュ": f"https://www.cardrush.jp/product-list?keyword={q}",
        "トレコロCB": f"https://www.torecolo.jp/shop/goods/search.aspx?search=x&keyword={q}&category=&oshiire_code=",
        "カーナベル": f"https://www.ka-nabell.com/ec/sell/yugioh/search?keyword={q}",
        "カードラボ": f"https://www.c-labo-online.jp/product-list?keyword={q}",
        "まんぞく屋": f"https://shopmanzokuya.com/products/list?category_id=1&name={q}&orderby=price_l&disp_number=100",
        "駿河屋": f"https://www.suruga-ya.jp/search?category=501&search_word={q}",
    }
    return urls.get(shop, "")

# 相場キャッシュ — サーバー起動時にSupabaseから全件ロードし、10分ごとに更新
_estimate_cache: dict[str, dict] = {}
_estimate_cache_time: float = 0
_ESTIMATE_CACHE_SEC = 600

_estimate_load_lock = threading.Lock()  # ローダーの多重起動防止（in-flight中はスキップ）

def _load_estimate_cache(startup: bool = False):
    """Supabaseから直近7日の全カード最安値をメモリにロード。
    startup=True のときはジッターを入れてワーカー間の同時アクセスを分散させる。
    分割取得化で1トリガあたりRPC最大数回になったため、実行中の重複起動は
    ロック（non-blocking）で弾く。失敗時は既存キャッシュを保持する（部分反映しない。
    先頭ページだけの部分キャッシュは名前順後半のカードがbest:nullになる＝
    1000行上限バグと同じ症状を再生産するため、古くても完全なキャッシュを優先）。
    """
    global _estimate_cache, _estimate_cache_time
    if not _supabase_client:
        return
    if not _estimate_load_lock.acquire(blocking=False):
        return  # 別スレッドがロード中

    try:
        _load_estimate_cache_inner(startup)
    finally:
        _estimate_load_lock.release()


class _UnexpectedJsonRouteResponse(Exception):
    """get_card_best_prices_json が list 以外を返した（関数の定義違いの疑い）"""


# json 経路をあきらめてページング経路へ移るのは、関数そのものが使えない設定のときだけ
# （PostgREST: PGRST202=関数が無い・PGRST203=同名関数が複数で1つに決まらない／
# Postgres: 42883=関数が無い・42501=権限が無い）。どれも DB の混み具合とは無関係で、
# ページング経路は別の関数 get_card_best_prices を呼ぶので助かる見込みがある。
# 42883 は関数本体の中の「演算子が無い」でも出るが、その場合ページング経路もすぐ失敗するだけで害は小さい
_JSON_ROUTE_UNUSABLE_CODES = ("PGRST202", "PGRST203", "42883", "42501")


def _is_function_unavailable_error(e: Exception) -> bool:
    """RPC 関数が設定上使えないことを示すエラーか。postgrest の APIError は code 属性を持つ。
    文字列でしか分からない例外も念のため見る"""
    code = getattr(e, "code", None)
    if code in _JSON_ROUTE_UNUSABLE_CODES:
        return True
    text = str(e)
    return any(c in text for c in _JSON_ROUTE_UNUSABLE_CODES)


def _load_estimate_cache_inner(startup: bool):
    global _estimate_cache, _estimate_cache_time
    if startup:
        jitter = random.uniform(0, 6)
        logger.info(f"相場キャッシュ: {jitter:.1f}秒後に開始")
        time.sleep(jitter)

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        pages = 0  # except節のログでも参照するためtryの外で初期化
        try:
            # recorded_at はJST日付で記録されるため、比較もJST基準（UTCだと0-9時に1日ずれる）
            cutoff = (datetime.now(JST) - timedelta(days=7)).strftime("%Y-%m-%d")

            # 2026-09-02: get_card_best_prices_json（jsonb一括集約）を優先する。従来の
            # range分割ページング経路はPostgRESTの1000行上限のためRPCを最大4回叩いており、
            # 本番実測でDISTINCT ONの並べ替えがwork_mem不足でディスクに溢れ1回6.9秒
            # （VACUUM後2.1秒）かかっていた＝起動時57014（statement timeout。work_mem不足→
            # ディスク並べ替え→遅延→3秒超過が原因。メモリ不足そのものの警告ではない）
            # の一因。work_mem='64MB'指定のjson一括関数なら0.8秒×1回で済む
            # （supabase_rpc_best_prices.sql参照）。ただし本番にまだ関数が未適用の環境でも
            # 落ちないよう、関数が無い（PGRST202 等）か想定外の型が返ったときは警告ログを
            # 出して従来のページング経路にフォールバックする（_JSON_ROUTE_UNUSABLE_CODES）。
            # 2026-10-22: statement timeout（57014）など「関数はあるが重い・DBが混んでいる」
            # 失敗ではフォールバックしない。ページング経路は同じ集計を最大4回走らせるため、
            # 混んでいるDBへ負荷を足すだけだった（本番の起動直後に json 経路が 57014 →
            # ページング経路で 7秒・4.5秒・3回目も 57014）。この場合は下の再試行
            # （5秒・10秒待って json 経路をやり直す）に任せる
            route = "paged"
            rows: list = []
            # 休止中の店（shop_status.py）は最安の候補から外す。外す店が無いときは引数を渡さない
            # （関数の新しい定義 excluded_shops が本番に入る前でも、従来どおり呼べるように）
            rpc_params = {"cutoff_date": cutoff}
            if SUSPENDED_SHOPS:
                rpc_params["excluded_shops"] = sorted(SUSPENDED_SHOPS)
            try:
                json_resp = _supabase_client.rpc(
                    "get_card_best_prices_json", rpc_params
                ).execute()
                if isinstance(json_resp.data, list):
                    rows = json_resp.data
                    route = "json"
                    pages = 1
                else:
                    raise _UnexpectedJsonRouteResponse(f"想定外のレスポンス型: {type(json_resp.data)}")
            except Exception as e:
                if not (isinstance(e, _UnexpectedJsonRouteResponse) or _is_function_unavailable_error(e)):
                    raise
                logger.warning(
                    f"相場キャッシュ: get_card_best_prices_json 経路失敗（{e}）。"
                    "ページング経路にフォールバック")

            if route == "paged":
                # PostgRESTの返却上限（このプロジェクトは1000行）を超えるカード数があるため、
                # RPC結果を range で分割取得する。旧実装の .limit(5000) は上限でサイレントに
                # 切られ、名前順1000種以降（漢字始まりの大半）が見積もりから消えていた
                # （2026-08-03 判明。tracked_cards の1000行上限根治と同じ罠）。
                # .order でページ間の並び順を明示的に安定化する（RPC本体の DISTINCT ON
                # (card_name) にも依存しない。reviewer指摘: 安定キーで必ず order する規律）。
                # page_size はサーバの返却上限以下であることが正しさの条件（上限超に上げると
                # 1ページ目で len(batch) < page_size が成立し、静かに切り捨てへ戻る）
                page_size = 1000
                max_pages = 20  # 暴走ガード（現在2,688種。offsetが効かない異常時の無限ループ防止）
                offset = 0
                while True:
                    resp = (_supabase_client.rpc("get_card_best_prices", rpc_params)
                            .order("card_name")
                            .range(offset, offset + page_size - 1)
                            .execute())
                    batch = resp.data or []
                    rows.extend(batch)
                    pages += 1
                    if len(batch) < page_size:
                        break
                    if pages >= max_pages:
                        logger.error(
                            f"相場キャッシュ: {max_pages}ページ({len(rows)}行)を超えたため打ち切り。"
                            "offsetが効いていないかカード数が異常。取得済み分のみで続行する")
                        break
                    offset += page_size

            if not rows:
                # 2,700枚規模のカードが7日窓に居るのが通常運用であり、0行は
                # 常に異常（RPC定義ミス・接続不調等）。空キャッシュで上書きすると
                # 全カードbest:nullになるため、例外化してリトライ＋既存キャッシュ
                # 保持（docstring の約束）に倒す
                raise ValueError("相場キャッシュ: 0行（RPC異常の疑い）")

            card_best = {}
            for row in rows:
                name = row.get("card_name", "")
                if not name:
                    continue
                card_best[name] = {
                    "shop": row.get("shop", ""),
                    "price": row.get("min_price", 0),
                    "rarity": row.get("rarity", ""),
                    "recorded_at": row.get("recorded_at", ""),
                }
            _estimate_cache = card_best
            _estimate_cache_time = time.time()
            logger.info(f"相場キャッシュロード完了: {len(card_best)}カード (経路={route}, ページ数={pages}, 行数={len(rows)})")
            return
        except Exception as e:
            wait = 5 * attempt
            # 途中失敗は全ページ破棄し、既存キャッシュを保持する（docstring参照）
            logger.error(f"相場キャッシュロード失敗 (試行{attempt}/{max_retries}, {pages}ページ目まで取得後): {e}")
            if attempt < max_retries:
                logger.info(f"  {wait}秒後にリトライします")
                time.sleep(wait)

# サーバー起動時にキャッシュをバックグラウンドでロード（複数workerでは1回だけ）
if _claim_startup_job("estimate_cache"):
    Thread(target=_load_estimate_cache, kwargs={"startup": True}, daemon=True).start()

@app.route("/api/deck-estimate", methods=["GET", "POST"])
def api_deck_estimate():
    """デッキ相場見積もり — メモリキャッシュから即時返却。
    GETは?cards=、POSTはJSON body {"cards":"..."} でカード名を受け取る（URL長制限回避）。
    """
    global _estimate_cache_time
    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        names_raw = body.get("cards", "")
    else:
        names_raw = request.args.get("cards", "")
    if not names_raw:
        return jsonify({"error": "カード名がありません"}), 400

    # キャッシュが古ければバックグラウンドで更新
    if time.time() - _estimate_cache_time > _ESTIMATE_CACHE_SEC:
        Thread(target=_load_estimate_cache, daemon=True).start()

    # 名前補正は必須。入口の _normalize_query が全角ダッシュを半角へ寄せるため、
    # 補正を省くと「召喚魔術－「剣」」等のDBキー（正式名称=全角ダッシュ）と永遠に
    # 一致せず best:null になる（2026-08-03 判明。旧 correct_names=False が原因）。
    # フロントはインデックスで突き合わせるため、補正で名前が変わっても表示は壊れない
    _load_cardnames()
    card_entries, parse_error = _parse_deck_entries(names_raw)
    if parse_error:
        return parse_error

    results = []
    total = 0
    for entry in card_entries:
        name = entry["name"]
        qty = entry["qty"]
        best = _estimate_cache.get(name)
        if best:
            total += best["price"] * qty
            # DBデータにはURLが無いため、ショップ名から検索URLを生成
            best = {**best, "url": _shop_search_url(best["shop"], name)}
        results.append({"name": name, "qty": qty, "best": best})

    return jsonify({"results": results, "total": total})

def _fetch_price_daily_rows(card_name: str, cutoff: str, skip_dates: set) -> list:
    """price_daily（31日目以前の日ごとの全店最安）を /api/price-history の行の形で返す。

    店舗別の行がある日（skip_dates）は除く（畳み込みと削除の間に両方ある日を二重にしない）。
    ビュー未作成・取得失敗のときは空で返し、店舗別の行だけのグラフにする（ログは残す）。
    """
    out: list = []
    try:
        page_size = 1000
        max_pages = 10  # 暴走ガード（100日×レアリティ数×同額の店で、通常は1ページに収まる）
        offset = 0
        while True:
            resp = (_supabase_client.table("price_daily")
                    .select("shop, rarity, min_price, recorded_at")
                    .eq("card_name", card_name)
                    .gte("recorded_at", cutoff)
                    .order("recorded_at", desc=False)
                    .order("shop", desc=False)
                    .order("rarity", desc=False)
                    .range(offset, offset + page_size - 1)
                    .execute())
            batch = resp.data or []
            for r in batch:
                d = (r.get("recorded_at") or "")[:10]
                if d in skip_dates:
                    continue
                out.append({"date": d, "shop": r.get("shop", ""),
                            "rarity": normalize_rarity(r.get("rarity", "")),
                            "price": r.get("min_price", 0)})
            if len(batch) < page_size:
                break
            offset += page_size
            if offset >= max_pages * page_size:
                logger.error(f"日ごとの最安取得: {max_pages}ページ超過で打ち切り [{card_name}]")
                break
    except Exception as e:
        logger.warning(f"日ごとの最安取得失敗（店舗別の行だけで返す） [{card_name}]: {e}")
        return []
    return out


@app.route("/api/price-history")
def api_price_history():
    """カードの価格推移データを返す"""
    card_name = request.args.get("card", "").strip()
    if not card_name:
        return jsonify({"card_name": "", "data": []})
    card_name = _normalize_query(card_name)

    # カード名の自動補正
    _load_cardnames()
    card_name = _correct_cardname(card_name)

    if not _supabase_client:
        return jsonify({"card_name": card_name, "data": []})

    try:
        cutoff = (datetime.now(JST) - timedelta(days=PRICE_DAILY_RETENTION_DAYS)).strftime("%Y-%m-%d")
        # 店舗別の行（price_history）は直近30日だけ残り、それより前は日ごとの全店最安
        # （price_daily）に畳まれている（price_daily_rollup.sql）。両方を読んで合わせる。
        # PostgRESTの返却上限1000行を超えるカードがある（2026-08 時点は90日×6店×複数レアリティで
        # 最大3,345行。30日保持の今は約1/3）。旧 .limit(2000) は上限で切られ、昇順のため新しい側＝グラフの
        # 直近数週間が欠落していた（監査F1: docs/audit-price-logic-2026-08-06.md）。
        # ページ間の順序を安定させるため複合キーで order する
        page_size = 1000
        max_pages = 10  # 暴走ガード（現在の最大は約3,345行=4ページ）
        offset = 0
        rows: list = []
        while True:
            resp = (_supabase_client.table("price_history")
                    .select("shop, rarity, min_price, recorded_at")
                    .eq("card_name", card_name)
                    .gte("recorded_at", cutoff)
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
                logger.error(f"価格推移取得: {max_pages}ページ超過で打ち切り [{card_name}]")
                break
        data = [
            {"date": r.get("recorded_at", "")[:10], "shop": r.get("shop", ""),
             "rarity": normalize_rarity(r.get("rarity", "")),  # 過渡期の分裂値を正準名に統合
             "price": r.get("min_price", 0)}
            for r in rows
        ]
        # 畳んだ分は店舗別の行より古いので前に置く（日付の昇順を保つ）
        data = _fetch_price_daily_rows(card_name, cutoff, {d["date"] for d in data}) + data
        # グラフは過去の記録なので休止中の店の行も残す。「いまの最安」を選ぶ画面（シミュレーターの
        # カード情報）が休止中の店を外せるように、店名を添える（shop_status.py）
        return jsonify({"card_name": card_name, "data": data,
                        "suspended_shops": sorted(SUSPENDED_SHOPS)})
    except Exception as e:
        logger.error(f"価格推移取得失敗: {e}")
        return jsonify({"card_name": card_name, "data": []})

# ── メルカリ参考相場（別枠表示専用。SHOPS/最安比較/夜間収集には混ぜない） ──
import mercari as _mercari
_last_mercari: dict[str, float] = {}
MERCARI_RATE_LIMIT_SEC = 1  # TODO: calibrate from data（カード詳細1表示につき1回。連続操作で弾きすぎない値）


@app.route("/api/mercari")
def api_mercari():
    """出品中のメルカリ参考相場をレアリティ別に返す（最安・中央値・件数）。"""
    card_name = _normalize_query(request.args.get("name", ""))
    if not card_name:
        return jsonify({"ok": False, "error": "name_required"}), 400
    if len(card_name) > MAX_CARD_NAME_LEN:
        return jsonify({"ok": False, "error": "name_too_long"}), 400
    rate_error = _consume_rate_limit(bucket=_last_mercari, interval=MERCARI_RATE_LIMIT_SEC)
    if rate_error:
        return jsonify({"ok": False, "error": "rate_limited"}), 429
    # 外部検索の踏み台にならないよう、既知のカード名だけ受け付ける（/api/search と同じ基準）
    _load_cardnames()
    # /api/search と同じ補正をかけてから判定・取得する（補正前の名前で呼ばれても1回で正しい結果を返す。
    # クライアントが補正後の名前で取り直すと IP 1秒制限で必ず429になるため）
    card_name = _correct_cardname(card_name)
    if card_name not in _cardnames_set and not _is_release_passed_unreleased(card_name):
        return jsonify({"ok": False, "error": "unknown_card"}), 404
    result = _mercari.get_reference(card_name, _cardnames_set)
    if result.get("error") == "busy":  # プロセス全体の負荷制限(同時実行/毎分上限)。待たせず失敗応答
        return jsonify(result), 503
    return jsonify(dict(result, card_name=card_name))  # card_name=補正後の名前


TRENDING_MIN_ITEMS = 10  # この件数未満は「利用者が少ない」と見なし非表示

@app.route("/api/trending")
def api_trending():
    """直近24時間の検索ランキングを返す"""
    limit, limit_error = _parse_limit_param(10, 30)
    if limit_error:
        return limit_error
    items = _get_trending(limit)
    if len(items) < TRENDING_MIN_ITEMS:
        return jsonify([])
    return jsonify(items)

# ── 値上がり/値下がりランキング（キャッシュ付き） ──
_movers_cache: dict[str, list] = {}
_movers_cache_time: float = 0
_MOVERS_CACHE_SEC = 3600  # 1時間キャッシュ（収集完了後にサイト表示を当日データへ早く追従させる）

def _aggregate_daily_min_lowest_rarity(rows: list) -> dict:
    """price_history 行リストから最安レアリティ系列を代表とした集計を返す。
    （aggregations.py に一元化）"""
    return daily_min_by_lowest_rarity(rows)

def _get_price_movers(direction: str, limit: int = 10) -> list[dict]:
    """Supabaseから値上がり/値下がりランキングを取得（DB側集計RPC版）。
    get_price_movers RPC が全件集計を DB 側で処理し、上位 top_n 行だけ返す。
    アプリ側の全件メモリ展開を廃止し、データ増加に対してメモリが増えない構造に変更。
    """
    global _movers_cache, _movers_cache_time

    now = time.time()
    if _movers_cache and now - _movers_cache_time < _MOVERS_CACHE_SEC:
        return _movers_cache.get(direction, [])[:limit]

    if not _supabase_client:
        return []

    try:
        cutoff = (datetime.now(JST) - timedelta(days=3)).strftime("%Y-%m-%d")
        resp = (_supabase_client
                .rpc("get_price_movers", {"cutoff_date": cutoff, "min_diff": 50, "top_n": 20})
                .execute())
        rows = resp.data or []
        logger.info(f"値動きランキング: {len(rows)}行取得（RPC集計）")

        if not rows:
            return []

        # RPC が direction 別に返すので振り分けるだけ
        up, down = [], []
        date_new = date_old = None
        for row in rows:
            entry = {
                "name":      row.get("card_name", ""),
                "rarity":    row.get("rarity", ""),
                "today":     row.get("today_price", 0),
                "yesterday": row.get("prev_price", 0),
                "diff":      row.get("diff", 0),
                "pct":       float(row.get("pct", 0)),
            }
            if row.get("direction") == "up":
                up.append(entry)
            else:
                down.append(entry)
            if date_new is None:
                date_new = row.get("date_new")
                date_old = row.get("date_old")

        # 空データはキャッシュしない（プリロード時点でデータが揃っていなかった場合に24時間空を返し続けるバグを防ぐ）
        if up or down:
            _movers_cache = {"up": up, "down": down, "date_old": date_old, "date_new": date_new}
            _movers_cache_time = now
        return (up if direction == "up" else down)[:limit]
    except Exception as e:
        logger.warning(f"価格変動ランキング取得失敗: {e}")
        return []

@app.route("/api/movers")
def api_movers():
    """値上がり/値下がりランキングを返す"""
    direction = request.args.get("direction", "up")
    if direction not in ("up", "down"):
        return jsonify({"error": "direction は up または down"}), 400
    limit, limit_error = _parse_limit_param(10, 20)
    if limit_error:
        return limit_error
    items = _get_price_movers(direction, limit)
    date_old = _movers_cache.get("date_old") if _movers_cache else None
    date_new = _movers_cache.get("date_new") if _movers_cache else None
    if _movers_cache_time:
        updated_dt = datetime.fromtimestamp(_movers_cache_time, JST)
        updated_at = f"{updated_dt.hour}:{updated_dt.minute:02d}"
    else:
        updated_at = None
    return jsonify({"items": items, "date_old": date_old, "date_new": date_new, "updated_at": updated_at})


# ── SWR（stale-while-revalidate）裏更新の共通ヘルパ ──
# 対象: top-decks/top-movers/top-priced/buyback-movers の4関数。
_SWR_RETRY_BACKOFF_SEC = 60  # 直近の裏更新失敗から この秒数が経つまでは再試行しない（失敗の多重発火防止）


def _run_swr_refresh(fn, on_failed=None, fn_kwargs=None):
    """SWR裏更新スレッドの target を包む共通ラッパ。

    レビュー指摘（Medium）: 各キャッシュ関数を裏スレッドで直接呼ぶだけだと、
    (1) 例外が出てもdaemonスレッドの中で握りつぶされてログに残らない、
    (2) error付き・空の「実質失敗」応答を成功と区別できず毎リクエストで再試行してしまう。
    ここで例外を必ずログに残し、戻り値（dict）に error が立っていれば on_failed() で
    バックオフ用の失敗時刻を更新する（buyback-movers はキャッシュ関数側が自前で
    失敗時刻を更新するため on_failed は渡さない）。
    """
    try:
        result = fn(**(fn_kwargs or {}))
        if on_failed and isinstance(result, dict) and result.get('error'):
            on_failed()
    except Exception:
        logger.exception(f"SWR 裏更新に失敗: {getattr(fn, '__name__', fn)}")
        if on_failed:
            on_failed()


# ── 買取値上がり/値下がりランキング（buyback_history テーブル、最高買取額ベース）──
_buyback_movers_cache: dict[str, list] = {}
_buyback_movers_cache_time: float = 0
_buyback_movers_cache_empty: bool = False  # 直近のキャッシュが0件だったか（TTL切り替え用）
_buyback_movers_refresh_failed_at: float = 0  # 直近の裏更新失敗（例外/空）時刻。SWRバックオフ用
_BUYBACK_MOVERS_CACHE_SEC = 3600  # 1時間キャッシュ
# collect_buyback が未開封・付属品つきを最高額から外した最初の収集日（JST。PR マージ翌朝 5:00 の収集。2026-10-28 決定）
_BUYBACK_SEALED_EXCLUDED_FROM = "2026-10-29"
# 空データ（RPCが0件を返した場合）は元々「24時間空を返し続けるバグを防ぐ」ため一切
# キャッシュしていなかった。ただしそのままだと失効直後は毎リクエストでRPCが走ってしまう
# ため、意図（長時間空を固定しない）は保ちつつ短時間だけキャッシュする。
_BUYBACK_MOVERS_EMPTY_CACHE_SEC = 300  # 0件時は5分だけキャッシュ
_buyback_movers_lock = threading.Lock()  # in-flightロック（同時アクセスでRPCが重複しないようにする）


def _buyback_new_release_keys() -> set:
    """発売から7日以内（featured_pack の運用期間）の新弾の収録カードの鍵の集合。無ければ空集合。"""
    try:
        pack = get_featured_pack(_supabase_client)
        if not (pack and is_within_window(pack)):
            return set()
        return get_featured_card_keys_cached(_supabase_client, pack)
    except Exception as e:
        logger.warning(f"買取値動きの新弾判定に失敗（印なしで出す）: {e}")
        return set()


def _fetch_buyback_movers_from_db() -> dict | None:
    """RPCから買取movers生データを取得してup/downに振り分ける。取得失敗時はNoneを返す。"""
    if not _supabase_client:
        return None
    try:
        today = datetime.now(JST).strftime("%Y-%m-%d")
        cutoff = (datetime.now(JST) - timedelta(days=3)).strftime("%Y-%m-%d")
        # 未開封・付属品つきを最高額から外した最初の収集日（以降）になったら、それより前とは比べない（外す前の記録は
        # 未開封込みの最高額で、比べると嘘の値下がりが出る）。切り替えた日だけランキングが空になり、3日後からは効かない。
        # 切り替え日より前の日は、新旧どちらも外す前の記録同士なので今までどおり比べる
        if today >= _BUYBACK_SEALED_EXCLUDED_FROM:
            cutoff = max(cutoff, _BUYBACK_SEALED_EXCLUDED_FROM)
        resp = (_supabase_client
                .rpc("get_buyback_movers", {"cutoff_date": cutoff, "min_diff": 50, "top_n": 20})
                .execute())
        rows = resp.data or []
        logger.info(f"買取値動きランキング: {len(rows)}行取得（RPC集計）")
        new_release_keys = _buyback_new_release_keys()

        # RPC が direction 別に返すので振り分けるだけ
        up, down = [], []
        date_new = date_old = None
        for row in rows:
            entry = {
                "name":      row.get("card_name", ""),
                "rarity":    row.get("rarity", ""),
                "today":     row.get("today_price", 0),
                "yesterday": row.get("prev_price", 0),
                "diff":      row.get("diff", 0),
                "pct":       float(row.get("pct", 0)),
            }
            # 発売から7日以内の新弾の収録カード。発売日は買取を出す店が少なく、比べる元が1店の初値に
            # なりやすいので、大きく動いて見える理由が分かるように画面で「発売直後」と添える
            # （2026-11-01 IMMORTAL PHOENIX でカーナベルの初値 300円→2,000円が +566.7% と出た）
            if featured_card_key(entry["name"]) in new_release_keys:
                entry["new_release"] = True
            if row.get("direction") == "up":
                up.append(entry)
            else:
                down.append(entry)
            if date_new is None:
                date_new = row.get("date_new")
                date_old = row.get("date_old")
        return {"up": up, "down": down, "date_old": date_old, "date_new": date_new}
    except Exception as e:
        logger.warning(f"買取価格変動ランキング取得失敗: {e}")
        return None


def _refresh_buyback_movers_cache(blocking: bool = False) -> None:
    """買取movers RPCを叩いてキャッシュを更新する。

    blocking=False（既定・stale-while-revalidate経路）: in-flightロックを取れない
    （既に別スレッドが更新中）場合は何もしない。
    blocking=True（コールド経路。レビュー指摘High）: 他スレッドが更新中ならその完了を
    最大15秒待ってから読む。ロック取得後、待っている間に他スレッドが既にキャッシュを
    埋めていればRPCを重複発行しない（ダブルチェック）。
    取得失敗時・空データ時は既存キャッシュを維持しつつ失敗時刻を記録する（SWRバックオフ用）。
    """
    global _buyback_movers_cache, _buyback_movers_cache_time, _buyback_movers_cache_empty
    global _buyback_movers_refresh_failed_at

    acquired = (
        _buyback_movers_lock.acquire(timeout=15) if blocking
        else _buyback_movers_lock.acquire(blocking=False)
    )
    if not acquired:
        return
    try:
        if blocking and _buyback_movers_cache:
            # ロック待ちの間に別スレッドが既に埋めていた（ダブルチェック）
            return
        data = _fetch_buyback_movers_from_db()
        if data is None:
            _buyback_movers_refresh_failed_at = time.time()
            return
        _buyback_movers_cache = data
        _buyback_movers_cache_time = time.time()
        _buyback_movers_cache_empty = not (data["up"] or data["down"])
        if _buyback_movers_cache_empty:
            _buyback_movers_refresh_failed_at = time.time()
    finally:
        _buyback_movers_lock.release()


def _get_buyback_movers(direction: str, limit: int = 10) -> dict:
    """Supabaseから買取の値上がり/値下がりランキングを取得（DB側集計RPC版）。
    get_buyback_movers RPC が全件集計を DB 側で処理し、上位 top_n 行だけ返す。
    アプリ側の全件メモリ展開を廃止し、データ増加に対してメモリが増えない構造に変更。

    stale-while-revalidate: キャッシュが期限切れでも古い値を即返し、裏スレッドで
    更新する（更新中に別リクエストが来てもRPCを重複発行しない）。
    コールド（未確立）時は初回訪問者に空を返さないよう、ブロッキングで取得を待つ
    （レビュー指摘High）。

    戻り値: {"items": [...], "date_old": ..., "date_new": ...} のスナップショット
    （レビュー指摘: 呼び出し側が items と date_old/date_new を別々に
    _buyback_movers_cache から読むと、裏更新スレッドと競合して不整合になりうるため
    1回の呼び出しでまとめて返す）
    """
    def _snapshot() -> dict:
        cache = _buyback_movers_cache
        if not cache:
            return {"items": [], "date_old": None, "date_new": None}
        return {
            "items": cache.get(direction, [])[:limit],
            "date_old": cache.get("date_old"),
            "date_new": cache.get("date_new"),
        }

    now = time.time()
    ttl = _BUYBACK_MOVERS_EMPTY_CACHE_SEC if _buyback_movers_cache_empty else _BUYBACK_MOVERS_CACHE_SEC
    if _buyback_movers_cache and now - _buyback_movers_cache_time < ttl:
        return _snapshot()

    if _buyback_movers_cache:
        # 古いキャッシュを即返しつつ裏で更新（直近の失敗からバックオフ期間内なら起動しない）
        if now - _buyback_movers_refresh_failed_at >= _SWR_RETRY_BACKOFF_SEC:
            Thread(target=_run_swr_refresh,
                   kwargs={"fn": _refresh_buyback_movers_cache}, daemon=True).start()
        return _snapshot()

    # コールド（未確立）: 初回はブロッキングで取得を待つ
    _refresh_buyback_movers_cache(blocking=True)
    return _snapshot()

@app.route("/api/buyback-movers")
def api_buyback_movers():
    """買取の値上がり/値下がりランキングを返す"""
    direction = request.args.get("direction", "up")
    if direction not in ("up", "down"):
        return jsonify({"error": "direction は up または down"}), 400
    limit, limit_error = _parse_limit_param(10, 20)
    if limit_error:
        return limit_error
    snapshot = _get_buyback_movers(direction, limit)
    items = snapshot["items"]
    date_old = snapshot["date_old"]
    date_new = snapshot["date_new"]
    if not items:
        # 空応答をCDN等に公開キャッシュさせない（レビュー指摘Critical）
        g.no_cache = True
    if _buyback_movers_cache_time:
        updated_dt = datetime.fromtimestamp(_buyback_movers_cache_time, JST)
        updated_at = f"{updated_dt.hour}:{updated_dt.minute:02d}"
    else:
        updated_at = None
    return jsonify({"items": items, "date_old": date_old, "date_new": date_new, "updated_at": updated_at})


# ── 最初の画面（検索前）向けランキング（N-1: デッキ / N-2: 価格推移） ──
# 集計ロジック本体は top_page.py に切り出し、ここは配線（取得・キャッシュ・ルート）のみ。

_top_decks_cache: dict = {}
_top_decks_cache_time: float = 0
_top_decks_refresh_failed_at: float = 0  # 直近の裏更新失敗（error付き返却/例外）時刻。SWRバックオフ用
_top_decks_lock = threading.Lock()  # Q-11: in-flightロック（TTL失効直後の同時アクセスでスクレイプが並走するのを防ぐ）


def _mark_top_decks_refresh_failed() -> None:
    global _top_decks_refresh_failed_at
    _top_decks_refresh_failed_at = time.time()


def _get_top_decks_cached(force: bool = False) -> dict:
    """環境デッキ「いま組むといくら」をシェア上位 top_page.TOP_DECKS_LIMIT 件だけ計算し、
    top_page.TOP_DECKS_CACHE_SEC 秒キャッシュする（デッキ数ぶんスクレイピングが
    発生するため、リクエストの度に全デッキを再計算しない）。
    """
    global _top_decks_cache, _top_decks_cache_time
    now = time.time()
    if not force and _top_decks_cache and now - _top_decks_cache_time < _top_page.TOP_DECKS_CACHE_SEC:
        return _top_decks_cache

    if not force and _top_decks_cache:
        # stale-while-revalidate: 期限切れでも古いキャッシュを即返し、
        # 裏でこの関数自身を force=True で再帰的に呼んで更新する（結果は破棄）。
        # 既に別スレッドが計算中なら下の lock.acquire(blocking=False) に阻まれて
        # 何もせず終わる（既存のin-flightロックをそのまま活かす）。
        # 直近の失敗からバックオフ期間内なら起動しない（毎リクエスト再試行を防ぐ）。
        if now - _top_decks_refresh_failed_at >= _SWR_RETRY_BACKOFF_SEC:
            Thread(target=_run_swr_refresh, kwargs={
                "fn": _get_top_decks_cached,
                "on_failed": _mark_top_decks_refresh_failed,
                "fn_kwargs": {"force": True},
            }, daemon=True).start()
        return _top_decks_cache

    if not _top_decks_lock.acquire(blocking=False):
        # 別スレッドが計算中（Q-11）。旧キャッシュがあればそれを返し、無ければ「計算中」を返す
        # pending: 時間が経てば解消する一時状態（起動直後のプリロード中など）。画面はこれを見て失敗扱いにせず待ち続ける
        return _top_decks_cache or {"decks": [], "updated": None, "error": "計算中です。しばらくしてから再度お試しください", "pending": True}

    try:
        # Q-5: 相場キャッシュが一度もロードされていないワーカー（gunicorn複数ワーカーの
        # うち1つしか _claim_startup_job のプリフェッチが当たらない）で計算すると
        # 全デッキ¥0になり、それが30分キャッシュされ続ける。未ロードなら計算自体を
        # スキップしてエラーを返す（バックグラウンドロードは既存どおり走らせる）
        if _estimate_cache_time == 0:
            if time.time() - _estimate_cache_time > _ESTIMATE_CACHE_SEC:
                Thread(target=_load_estimate_cache, daemon=True).start()
            logger.warning("top-decks: 相場キャッシュ未ロードのため今回の計算をスキップ")
            return _top_decks_cache or {"decks": [], "updated": None, "error": "価格データを準備中です", "pending": True}

        # Q-4: fetch_tier_list は最悪126秒かかりうる（窓2つ×3リトライ×timeout20秒）ため、
        # 既存 /api/meta と同じく executor 経由・タイムアウト付きで呼ぶ
        try:
            tiers = _meta_executor.submit(fetch_tier_list).result(timeout=25)
        except Exception as e:
            logger.error(f"top-decks: Tier表取得エラー: {e}")
            tiers = []
        if not tiers:
            return _top_decks_cache or {"decks": [], "updated": None, "error": "環境データを取得できませんでした"}

        targets = sorted(tiers, key=lambda t: -(t.get("share") or 0))[:_top_page.TOP_DECKS_LIMIT]

        futures = {_meta_executor.submit(fetch_deck_cards, t["name"], False): t for t in targets}
        deck_data_by_theme = {}
        try:
            # Q-3: as_completed(timeout=...) はイテレータ自体がTimeoutErrorを送出するため、
            # future.result()を囲むtryでは捕まらない。forループ全体を囲む必要がある
            for future in as_completed(futures, timeout=40):
                t = futures[future]
                try:
                    deck_data_by_theme[t["name"]] = future.result()
                except Exception as e:
                    logger.warning(f"top-decks: デッキ取得失敗 ({t['name']}): {e}")
        except _FuturesTimeoutError:
            logger.warning(
                f"top-decks: {len(targets)}件中{len(deck_data_by_theme)}件で40秒タイムアウト。"
                "取得できた分だけで続行")
        finally:
            # 未完了futureを残すと共有 _meta_executor を占有し続け、/api/meta/deck が
            # 巻き添えでタイムアウトする（Q-3）。キャンセルできるものはキャンセルする
            for f in futures:
                if not f.done():
                    f.cancel()

        _load_cardnames()

        decks = []
        for t in targets:
            data = deck_data_by_theme.get(t["name"])
            if not data:
                continue
            full_deck = data.get("full_deck") or []
            deck_text = build_recipe_text(full_deck) if full_deck else build_deck_text(data.get("cards") or [])
            # Q-8: [EX]はEXデッキの区切り見出し行でありカードではない。フロント側の
            # 既存パーサ（index.html: l!=='[EX]'）と同じく除外してから渡す
            lines = [l for l in deck_text.split("\n") if l.strip() and l.strip() != "[EX]"]
            if not lines:
                continue
            names_raw = "|".join(lines)
            entries, parse_error = _parse_deck_entries(names_raw)
            if parse_error or not entries:
                continue
            summary = _top_page.summarize_deck(entries, _estimate_cache)
            decks.append({
                "name": t["name"],
                "tier": t.get("tier", 0),
                "share": t.get("share", 0),
                "image": t.get("image", ""),
                **summary,
            })

        if not decks:
            return _top_decks_cache or {"decks": [], "updated": None, "error": "デッキ価格を計算できませんでした"}

        # Q-5続き: 相場キャッシュ由来の異常（全デッキ priced_count=0）はキャッシュしない
        if all(d.get("priced_count", 0) == 0 for d in decks):
            logger.warning("top-decks: 全デッキでpriced_count=0。相場キャッシュ異常の疑いがありキャッシュしない")
            return _top_decks_cache or {"decks": decks, "updated": None, "error": "価格データを取得できませんでした"}

        result = {"decks": decks, "updated": datetime.now(JST).isoformat(), "error": None}
        _top_decks_cache = result
        _top_decks_cache_time = now
        return result
    finally:
        _top_decks_lock.release()

@app.route("/api/top-decks")
def api_top_decks():
    """検索前の初期画面向け: 環境デッキを「いま組むといくら」で返す"""
    sort = request.args.get("sort", "price")
    if sort not in ("price", "tier"):
        return jsonify({"error": "sort は price または tier"}), 400
    data = _get_top_decks_cached()
    decks = _top_page.rank_decks(data.get("decks", []), sort=sort)
    if data.get("error"):
        g.no_cache = True
    return jsonify({"decks": decks, "updated": data.get("updated"), "error": data.get("error"),
                    "pending": bool(data.get("pending"))})


_top_movers_cache: dict = {}
_top_movers_cache_time: float = 0
_top_movers_lock = threading.Lock()  # Q-11: in-flightロック

# 2026-09-04: トップページのOF(オーバーフレーム)専用タブ用。既存の
# _top_movers_cache 一式（group=None、全レアリティ対象）はテストが直接
# monkeypatchしているため変数名・意味を変えず、グループ別（"of"）の分だけ
# こちらの辞書で別管理する。
_top_movers_group_cache: dict[str, dict] = {}
_top_movers_group_cache_time: dict[str, float] = {}
# L-2修正: ハードコードでなく top_page.RARITY_GROUPS（レアリティ群の定義集合）から
# 機械的に作る。実際に許可するgroupは api_top_movers 側で top_page.TOP_MOVERS_GROUPS
# により絞るため、ここに未使用のキー（例: gmr）が含まれても実害はない。
_top_movers_group_locks: dict[str, threading.Lock] = {g: threading.Lock() for g in _top_page.RARITY_GROUPS}

# 直近の裏更新失敗（error付き返却/例外）時刻。SWRバックオフ用。groupごとに別管理
# （キーは group文字列、全レアリティ対象=Noneは "__all__" とする）
_top_movers_refresh_failed_at: dict[str, float] = {}


def _mark_top_movers_refresh_failed(group: str | None = None) -> None:
    _top_movers_refresh_failed_at[group or "__all__"] = time.time()


def _get_top_movers_cached(group: str | None = None, force: bool = False) -> dict:
    """価格推移ランキング（代表レアリティ固定・共通店舗ガード・定着チェック、
    最安¥1,000以上）を DB側RPC（get_top_movers）で集計し、
    top_page.TOP_MOVERS_CACHE_SEC 秒キャッシュする。

    group: None なら従来どおり全レアリティ対象。"of" なら top_page.OF_RARITIES
        に絞る（RPCの p_rarities 引数、2026-09-04追加）。

    V-1（2026-09-01）: 従来は price_history を1日ぶん全行（本番実測30,851行）
    ×3日分をPostgREST経由でPython側に読み出して集計しており、キャッシュ失効後の
    初回計算に約15秒かかっていた。DB側RPCに集計を寄せ、上位N件だけを受け取る
    構造に変更した（買取版の _get_buyback_movers と同じ考え方）。

    RPCは「up/down合算・変化率(pct)の絶対値降順」で上位 p_limit 件を返すため、
    ここで pct の符号から up/down に振り分ける（RPC側はこの振り分けをしない）。
    """
    global _top_movers_cache, _top_movers_cache_time
    now = time.time()

    if group is None:
        cached = _top_movers_cache
        cached_time = _top_movers_cache_time
        lock = _top_movers_lock
    else:
        cached = _top_movers_group_cache.get(group, {})
        cached_time = _top_movers_group_cache_time.get(group, 0)
        lock = _top_movers_group_locks[group]

    if not force and cached and now - cached_time < _top_page.TOP_MOVERS_CACHE_SEC:
        return cached

    if not force and cached:
        # stale-while-revalidate: 古いキャッシュを即返しつつ裏で更新する。
        # 裏スレッドがロックを取れなければ（既に計算中なら）何もしない。
        # 直近の失敗からバックオフ期間内なら起動しない（毎リクエスト再試行を防ぐ）。
        failed_at = _top_movers_refresh_failed_at.get(group or "__all__", 0)
        if now - failed_at >= _SWR_RETRY_BACKOFF_SEC:
            Thread(target=_run_swr_refresh, kwargs={
                "fn": _get_top_movers_cached,
                "on_failed": lambda: _mark_top_movers_refresh_failed(group),
                "fn_kwargs": {"group": group, "force": True},
            }, daemon=True).start()
        return cached

    if not _supabase_client:
        return cached or {"up": [], "down": [], "date_old": None, "date_new": None,
                           "error": "価格データベースに接続できません"}

    if not lock.acquire(blocking=False):
        # 別スレッドが計算中（Q-11）
        return cached or {"up": [], "down": [], "date_old": None, "date_new": None,
                           "error": "計算中です。しばらくしてから再度お試しください"}

    try:
        params = {
            "p_min_price": _top_page.MOVERS_MIN_PRICE,
            "p_stability_days": _top_page.MOVERS_STABILITY_DAYS,
            "p_limit": _top_page.TOP_MOVERS_RPC_LIMIT,
        }
        if group is not None:
            params["p_rarities"] = list(_top_page.RARITY_GROUPS[group])
        try:
            resp = _supabase_client.rpc("get_top_movers", params).execute()
            rows = resp.data or []
        except Exception as e:
            logger.warning(f"価格推移ランキング(RPC)取得失敗(group={group}): {e}")
            if cached:
                return cached
            return {"up": [], "down": [], "date_old": None, "date_new": None, "error": "取得に失敗しました"}

        if not rows:
            return cached or {"up": [], "down": [], "date_old": None, "date_new": None,
                               "error": "価格データがまだありません"}

        up, down = [], []
        date_new = date_old = None
        stability_checked = False
        for row in rows:
            pct = float(row.get("pct") or 0)
            if pct == 0:
                continue  # 変化なしは対象外（旧Python実装と同じ扱い）
            price_new = row.get("price_new")
            price_old = row.get("price_old")
            entry = {
                "name": row.get("card_name", ""),
                "rarity": row.get("rarity", ""),
                "today": price_new,
                "yesterday": price_old,
                "diff": price_new - price_old,
                "pct": pct,
            }
            (up if pct > 0 else down).append(entry)
            if date_new is None:
                date_new = row.get("date_new")
                date_old = row.get("date_old")
                stability_checked = bool(row.get("stability_checked"))

        result = {"up": up, "down": down, "date_new": date_new, "date_old": date_old,
                  "stability_checked": stability_checked, "error": None}

        # 空データはキャッシュしない（買取movers等と同じ方針。データ未蓄積時に
        # 長時間空を返し続けるバグを防ぐ）
        if up or down:
            if group is None:
                _top_movers_cache = result
                _top_movers_cache_time = now
            else:
                _top_movers_group_cache[group] = result
                _top_movers_group_cache_time[group] = now
        return result
    except Exception as e:
        logger.warning(f"価格推移ランキング(RPC)取得失敗(group={group}): {e}")
        if cached:
            return cached
        return {"up": [], "down": [], "date_old": None, "date_new": None, "error": "取得に失敗しました"}
    finally:
        lock.release()

@app.route("/api/top-movers")
def api_top_movers():
    """検索前の初期画面向け: 価格推移ランキング（共通店舗ガードのみ、最安¥1,000以上）を返す

    group クエリ（省略時=全レアリティ対象／"of"=OF4種に絞る）を2026-09-04に追加。
    """
    direction = request.args.get("direction", "up")
    if direction not in ("up", "down"):
        return jsonify({"error": "direction は up または down"}), 400
    group = request.args.get("group")
    if group is not None and group not in _top_page.TOP_MOVERS_GROUPS:
        return jsonify({"error": f"group は {'/'.join(_top_page.TOP_MOVERS_GROUPS)} のみ指定できます"}), 400
    limit, limit_error = _parse_limit_param(10, 20)
    if limit_error:
        return limit_error
    data = _get_top_movers_cached(group)
    items = data.get(direction, [])[:limit]
    if data.get("error"):
        g.no_cache = True
    cache_time = _top_movers_cache_time if group is None else _top_movers_group_cache_time.get(group, 0)
    if cache_time:
        updated_dt = datetime.fromtimestamp(cache_time, JST)
        updated_at = f"{updated_dt.hour}:{updated_dt.minute:02d}"
    else:
        updated_at = None
    return jsonify({
        "items": items,
        "date_old": data.get("date_old"),
        "date_new": data.get("date_new"),
        "stability_checked": data.get("stability_checked", False),
        "updated_at": updated_at,
        "error": data.get("error"),
    })


@app.route("/api/staple-movers")
def api_staple_movers():
    """トップのランキング3列目「汎用カード」（2026-10-21）。集計は staple_ranking.py。

    sort: drop（1週間で値下がりした金額順・既定）/ themes（採用テーマが多い順）
    """
    import staple_ranking
    sort = request.args.get("sort", "drop")
    if sort not in staple_ranking.SORTS:
        return jsonify({"error": "sort は drop または themes"}), 400
    limit, limit_error = _parse_limit_param(10, 20)
    if limit_error:
        return limit_error
    data = staple_ranking.get_cached(_supabase_client)
    if data.get("error"):
        g.no_cache = True
    return jsonify({
        "items": data.get(sort, [])[:limit],
        "date_old": data.get("date_old"),
        "date_new": data.get("date_new"),
        "total": data.get("total", 0),
        "error": data.get("error"),
    })


_top_priced_cache: dict[str, dict] = {}
_top_priced_cache_time: dict[str, float] = {}
_top_priced_locks: dict[str, threading.Lock] = {g: threading.Lock() for g in _top_page.RARITY_GROUPS}
# 直近の裏更新失敗（error付き返却/例外）時刻。SWRバックオフ用。groupごとに別管理
_top_priced_refresh_failed_at: dict[str, float] = {}


def _mark_top_priced_refresh_failed(group: str) -> None:
    _top_priced_refresh_failed_at[group] = time.time()


def _get_top_priced_cached(group: str, force: bool = False) -> dict:
    """現在の最安値の高額順ランキング（DB側RPC get_top_priced）を
    top_page.TOP_PRICED_CACHE_SEC 秒キャッシュする（2026-09-04追加）。

    シリアル付きオーバーフレーム(GMR)は本番実測で7日間の値動きが0件だったため、
    get_top_movers と同じ「値動き」の枠組みではなく「現在の最安値が高い順」を返す。
    差額(diff/pct)は「7日前の共通店舗価格があれば」出し、無ければ出さない
    （RPC側で price_old/price_new_common が null になる）。

    _get_top_movers_cached と同じ方針: キャッシュ/in-flightロック/空データ非キャッシュ/
    例外時は既存キャッシュへフォールバック。
    """
    now = time.time()
    cached = _top_priced_cache.get(group, {})
    cached_time = _top_priced_cache_time.get(group, 0)
    lock = _top_priced_locks[group]

    if not force and cached and now - cached_time < _top_page.TOP_PRICED_CACHE_SEC:
        return cached

    if not force and cached:
        # stale-while-revalidate: 古いキャッシュを即返しつつ裏で更新する。
        # 裏スレッドがロックを取れなければ（既に計算中なら）何もしない。
        # 直近の失敗からバックオフ期間内なら起動しない（毎リクエスト再試行を防ぐ）。
        failed_at = _top_priced_refresh_failed_at.get(group, 0)
        if now - failed_at >= _SWR_RETRY_BACKOFF_SEC:
            Thread(target=_run_swr_refresh, kwargs={
                "fn": _get_top_priced_cached,
                "on_failed": lambda: _mark_top_priced_refresh_failed(group),
                "fn_kwargs": {"group": group, "force": True},
            }, daemon=True).start()
        return cached

    if not _supabase_client:
        return cached or {"items": [], "date_old": None, "date_new": None,
                           "error": "価格データベースに接続できません"}

    if not lock.acquire(blocking=False):
        return cached or {"items": [], "date_old": None, "date_new": None,
                           "error": "計算中です。しばらくしてから再度お試しください"}

    try:
        try:
            resp = (_supabase_client
                    .rpc("get_top_priced", {
                        "p_rarities": list(_top_page.RARITY_GROUPS[group]),
                        "p_limit": _top_page.TOP_PRICED_LIMIT,
                    })
                    .execute())
            rows = resp.data or []
        except Exception as e:
            logger.warning(f"高額順ランキング(RPC)取得失敗(group={group}): {e}")
            if cached:
                return cached
            return {"items": [], "date_old": None, "date_new": None, "error": "取得に失敗しました"}

        if not rows:
            return cached or {"items": [], "date_old": None, "date_new": None,
                               "error": "価格データがまだありません"}

        items = []
        date_new = date_old = None
        for row in rows:
            price_new = row.get("price_new")
            price_old = row.get("price_old")
            price_new_common = row.get("price_new_common")
            pct_raw = row.get("pct")
            pct = float(pct_raw) if pct_raw is not None else None  # L-3: movers側と型を揃える
            diff = None
            yesterday = price_old
            if price_new != price_new_common:
                # H-2修正: 主価格(price=全店最安)と差額(price_common基準)は
                # 基準が異なる。price_new(全店最安)とprice_new_common(共通店舗内
                # 最安)が別の店に由来する場合、価格−差額≠7日前 となり読み違えを
                # 生むため、その行は差額(diff/pct/yesterday)を出さない。
                # price_common自体はそのまま返してよい。
                diff = None
                pct = None
                yesterday = None
            elif price_old is not None and price_new_common is not None:
                diff = price_new_common - price_old
            items.append({
                "name": row.get("card_name", ""),
                "rarity": row.get("rarity", ""),
                "price": price_new,
                "price_common": price_new_common,
                "yesterday": yesterday,
                "diff": diff,
                "pct": pct,
            })
            if date_new is None:
                date_new = row.get("date_new")
                date_old = row.get("date_old")

        result = {"items": items, "date_new": date_new, "date_old": date_old, "error": None}

        if items:
            _top_priced_cache[group] = result
            _top_priced_cache_time[group] = now
        return result
    except Exception as e:
        logger.warning(f"高額順ランキング(RPC)取得失敗(group={group}): {e}")
        if cached:
            return cached
        return {"items": [], "date_old": None, "date_new": None, "error": "取得に失敗しました"}
    finally:
        lock.release()


@app.route("/api/top-priced")
def api_top_priced():
    """検索前の初期画面向け: 現在の最安値の高額順ランキングを返す（2026-09-04追加）。
    group クエリは top_page.TOP_PRICED_GROUPS（現状 "gmr"）のみ許可する
    （L-1修正: get_top_priced の代表レアリティ選定は「カードごとの最安レアリティ」
    のため、複数レアリティが混在する群の高額順ランキングには意味的に不向き）。
    """
    group = request.args.get("group")
    if group not in _top_page.TOP_PRICED_GROUPS:
        return jsonify({"error": f"group は {'/'.join(_top_page.TOP_PRICED_GROUPS)} のいずれかを指定してください"}), 400
    limit, limit_error = _parse_limit_param(10, 20)
    if limit_error:
        return limit_error
    data = _get_top_priced_cached(group)
    items = data.get("items", [])[:limit]
    if data.get("error"):
        g.no_cache = True
    cache_time = _top_priced_cache_time.get(group, 0)
    if cache_time:
        updated_dt = datetime.fromtimestamp(cache_time, JST)
        updated_at = f"{updated_dt.hour}:{updated_dt.minute:02d}"
    else:
        updated_at = None
    return jsonify({
        "items": items,
        "date_old": data.get("date_old"),
        "date_new": data.get("date_new"),
        "updated_at": updated_at,
        "error": data.get("error"),
    })


# ── 購入候補（wishlist）価格アラート ──

def _track_card_async(card_name: str):
    """tracked_cards に非同期で登録する（既存チェック→なければinsert）。"""
    try:
        resp = (_supabase_client.table("tracked_cards")
                .select("card_name")
                .eq("card_name", card_name)
                .limit(1)
                .execute())
        if not resp.data:
            _supabase_client.table("tracked_cards") \
                .insert({"card_name": card_name, "active": True}) \
                .execute()
            logger.info(f"[track] tracked_cards に追加: {card_name}")
    except Exception as e:
        logger.warning(f"[track] 追加失敗 ({card_name}): {e}")


def _track_cards_async(card_names: list):
    """複数カードをまとめて tracked_cards に登録する（既存を1クエリで除外し、新規だけ insert）。
    マイデッキ保存時に呼ばれ、簡易計算(DB相場)のカバー率を継続的に高めるための収集対象を増やす。
    """
    try:
        existing = set()
        resp = (_supabase_client.table("tracked_cards")
                .select("card_name")
                .in_("card_name", card_names)
                .execute())
        for row in (resp.data or []):
            existing.add(row.get("card_name"))
        new_rows = [{"card_name": n, "active": True} for n in card_names if n not in existing]
        if new_rows:
            _supabase_client.table("tracked_cards").insert(new_rows).execute()
            logger.info(f"[track] tracked_cards に一括追加: {len(new_rows)}件")
    except Exception as e:
        logger.warning(f"[track] 一括追加失敗: {e}")


@app.route("/api/track", methods=["POST"])
def api_track():
    """購入候補に追加されたカードを価格収集対象（tracked_cards）に登録する。
    入力: {"card": "カード名"}
    返却: {"ok": true, "card": "補正後カード名"} または {"ok": false, "reason": "unknown_card"}
    ログイン不要・fire-and-forget で使用する軽量エンドポイント。
    """
    data = request.get_json(silent=True) or {}
    card_name_raw = str(data.get("card", "")).strip()

    if not card_name_raw:
        return jsonify({"error": "カード名を指定してください"}), 400
    if len(card_name_raw) > MAX_CARD_NAME_LEN:
        return jsonify({"error": "カード名が長すぎます"}), 400

    _load_cardnames()
    corrected = _correct_cardname(card_name_raw)

    # カード辞書に存在しない文字列は登録拒否（スパム・誤登録対策）
    # O(1) のset検索を使う（_cardnames listへのO(n)検索は避ける）
    # release_date 到来済みの未発売テーブル登録カード（週次インデックス未反映の発売済み）は許可。
    if _cardnames_set and corrected not in _cardnames_set and not _is_release_passed_unreleased(corrected):
        return jsonify({"ok": False, "reason": "unknown_card"}), 200

    if _supabase_client:
        Thread(target=_track_card_async, args=(corrected,), daemon=True).start()

    return jsonify({"ok": True, "card": corrected})


@app.route("/api/track-batch", methods=["POST"])
def api_track_batch():
    """マイデッキ保存時に、デッキ内の複数カードをまとめて収集対象(tracked_cards)に登録する。
    入力: {"cards": ["カード名", ...]}  最大 MAX_DECK_CARDS 件
    返却: {"ok": true, "queued": 登録対象として受理した件数}
    ログイン不要・fire-and-forget。辞書に存在するカードのみ受理（スパム・誤登録対策）。
    """
    data = request.get_json(silent=True) or {}
    cards_raw = data.get("cards", [])
    if not isinstance(cards_raw, list):
        return jsonify({"error": "cards は配列で指定してください"}), 400

    _load_cardnames()
    seen = set()
    valid = []
    for raw in cards_raw[:MAX_DECK_CARDS]:
        name = str(raw).strip()
        if not name or len(name) > MAX_CARD_NAME_LEN:
            continue
        corrected = _correct_cardname(name)
        # カード辞書に無い文字列は弾く（O(1) のset検索）
        # release_date 到来済みの未発売テーブル登録カード（週次インデックス未反映の発売済み）は許可。
        if _cardnames_set and corrected not in _cardnames_set and not _is_release_passed_unreleased(corrected):
            continue
        if corrected in seen:
            continue
        seen.add(corrected)
        valid.append(corrected)

    if _supabase_client and valid:
        Thread(target=_track_cards_async, args=(valid,), daemon=True).start()

    return jsonify({"ok": True, "queued": len(valid)})


# 値下がり判定の閾値
_WISH_DROP_PCT = -5.0   # -5% 以上の下落
_WISH_DROP_ABS = -50    # かつ 50円以上安い


@app.route("/api/wish-prices", methods=["POST"])
def api_wish_prices():
    """購入候補カードの最新価格と7日前比を返す。
    入力: {"cards": ["カード名", ...]}  最大 MAX_WISH_CARDS 件
    返却: {"cards": [{name, latest, base_7d, diff, pct, status}, ...]}
      status: "ready" = データあり, "pending" = まだ収集されていない,
              "unreleased" = まだ店に並ばないカード（release_date・tcg_preview を添える）,
              "error" = DB の読み込みに失敗（値段なしとは別。画面は「価格を読み込めませんでした」）
    """
    global _estimate_cache_time

    data = request.get_json(silent=True) or {}
    cards_raw = data.get("cards", [])
    if not isinstance(cards_raw, list):
        return jsonify({"error": "cards は配列で指定してください"}), 400

    # 最大 MAX_WISH_CARDS 件・名前補正
    _load_cardnames()
    card_names = []
    for name in cards_raw[:MAX_WISH_CARDS]:
        name = str(name).strip()
        if name and len(name) <= MAX_CARD_NAME_LEN:
            card_names.append(_correct_cardname(name))
    if not card_names:
        return jsonify({"cards": []})
    # 表記の違う入力が同じ名前に直ることがある。読み込みは1回にする（返す cards は送った数のまま）
    unique_names = list(dict.fromkeys(card_names))

    # _estimate_cache でデータ有無を事前判定（DBクエリを最小化）
    if time.time() - _estimate_cache_time > _ESTIMATE_CACHE_SEC:
        Thread(target=_load_estimate_cache, daemon=True).start()

    have_data = [n for n in unique_names if n in _estimate_cache]
    pending_set = set(unique_names) - set(have_data)

    def _fetch_price_history(names: list, days: int) -> tuple[dict, dict, set]:
        """price_history から指定日数分の日次最安値（現在価格用）と、
        共通店舗比較による値動き（変動判定用）を取得して返す。
        戻り値: (card_dates, card_common, failed)
          card_dates: {card_name: {date_str: min_price}}
            代表レアリティの全店舗min。表示する「現在価格」はこの最新日の値を使う
          card_common: {card_name: {"base_7d", "diff", "pct"}}
            共通店舗（両日に記録がある店舗）どうしで比較した値動き（監査F5）
          failed: 読み込めなかったカード名。「値段なし」と区別して status "error" で返す
            （2026-10-22 読み込み失敗が全件「価格が見つかりませんでした」になっていた）
        """
        if not names:
            return {}, {}, set()
        if not _supabase_client:
            return {}, {}, set(names)
        # 窓は従来どおり今日の days 日前から今日まで。日付ごとに等号で読む（索引
        # (recorded_at, card_id, …) を両方効かせる。2026-10-22 名前＋日付の範囲で statement timeout）
        # 並べ替えに使う番号の列も select に入れる（featured_matrix と同じ形。本番で通っている形にそろえる）
        all_rows, failed = fetch_daily_rows_by_card_names(
            _supabase_client, names, "card_id, shop_id, rarity_id, card_name, rarity, shop, min_price, recorded_at",
            jst_days_back(days), "wish-prices")
        # 代表レアリティ選定は rarity 文字列でグループ化するため、DB読み出し境界で
        # 正準名に統合する（過渡期の分裂値を吸収。/api/price-history と同じ方針）
        for r in all_rows:
            r["rarity"] = normalize_rarity(r.get("rarity", "") or "")
        card_dates = _aggregate_daily_min_lowest_rarity(all_rows)

        # 共通店舗比較（変動判定用, 監査F5）は独立した try/except に分離する。
        # ここで例外が起きても、上で取得済みの card_dates（現在価格の表示）を
        # 巻き添えにしない（監査 2026-08-07, L9）。失敗時は変動表示なし扱い
        try:
            card_common = common_shop_price_change(all_rows)
        except Exception as e:
            logger.warning(f"[wish-prices] 共通店舗比較の計算失敗: {e}")
            card_common = {}

        return card_dates, card_common, failed

    # 主クエリ: estimate_cacheにあるカードを8日分で取得
    card_dates, card_common, failed_set = _fetch_price_history(have_data, days=8)

    # フォールバック: estimateキャッシュ未収録のカードを30日分で再検索
    if pending_set:
        fallback_dates, fallback_common, fallback_failed = _fetch_price_history(list(pending_set), days=30)
        for name, dates in fallback_dates.items():
            if dates:
                card_dates[name] = dates
                card_common[name] = fallback_common.get(name, {})
                pending_set.discard(name)
                have_data.append(name)
        # 読めなかったカードは「まだ集めていない」とも言えないので失敗として返す
        pending_set -= fallback_failed
        failed_set |= fallback_failed

    results = []
    for name in card_names:
        if name in failed_set:
            results.append({"name": name, "latest": None, "base_7d": None,
                            "diff": None, "pct": None, "status": "error",
                            "image": _ygores_image_url(name)})
            continue
        if name in pending_set:
            # 値が無いのが未発売のためなら「まだ集めていない」と分ける（2026-10-30）
            mark = _wish_unreleased_mark(name)
            item = {"name": name, "latest": None, "base_7d": None,
                    "diff": None, "pct": None, "status": "unreleased" if mark else "pending",
                    "image": _ygores_image_url(name)}
            if mark:
                item.update(mark)
            results.append(item)
            continue

        dates = card_dates.get(name, {})
        dates_sorted = sorted(dates.keys())
        # latest（現在価格）は全店舗min、pct/diff（変動判定）は共通店舗min。
        # 基準が異なるのは意図した仕様（裁定H3, 2026-08-07）: 現在価格はユーザーが
        # 実際に買える最安値をそのまま見せ、変動判定だけ比較可能性を優先して
        # 共通店舗に絞る。両者を同じ基準に揃えることはしない
        latest_price = dates[dates_sorted[-1]] if dates_sorted else None

        # 変動判定（base_7d/diff/pct）は共通店舗比較で算出済み（監査F5）。
        # 条件を満たす比較対象日が窓内に無ければ全て None（＝変動表示なし）
        common = card_common.get(name) or {}
        base_7d = common.get("base_7d")
        diff = common.get("diff")
        pct = common.get("pct")

        results.append({
            "name": name,
            "latest": latest_price,
            "base_7d": base_7d,
            "diff": diff,
            "pct": pct,
            "status": "ready",
            "image": _ygores_image_url(name),
        })

    return jsonify({"cards": results})


@app.route("/api/wish-shop-totals", methods=["POST"])
def api_wish_shop_totals():
    """購入候補リストを「1店舗でまとめ買いするといくらか」を全店舗で集計する。
    入力: {"cards": [{"name": str, "qty": int, "rarity": str?}, ...]} 最大 MAX_WISH_CARDS 件
       rarity 省略 or 空文字 = 全レアリティから最安（=「最安レアリティ」採用）
       rarity 指定あり = 該当レアリティのみ集計（同名カードを別行として扱う）
    返却: {
      "shops": [
        {"shop": "店舗名", "total": 商品代金の合計, "covered_qty": そろう枚数,
         "shipping": 送料（int。ルール未登録の店舗は null）,
         "total_with_shipping": 送料込みの支払額,
         "free_shipping_at": 送料無料まであといくら（null=無料条件なし/すでに無料）,
         "min_order_short": 最低注文金額まであといくら（null=制限なし/満たしている）,
         "orderable": この金額で注文できるか,
         "covered_cards": そろうカード種類数, "total_qty": 全枚数,
         "total_cards": 全カード種類数,
         "missing_cards": [{"name": str, "rarity": str}, ...],
         "items": [{"name": str, "qty": int, "price": int,
                    "rarity": 採用レアリティ, "rarity_pref": 希望レアリティ(""=未指定)}, ...],
         "url": 検索URL},
        ...
      ],
      "db_missing": [{"name": str, "rarity": str}, ...],
      "unreleased": [{"name": str, "rarity": str, "release_date": "YYYY-MM-DD"|"", "tcg_preview": bool}, ...],
      "entries": [{"name": 補正後name, "qty": int, "rarity": 正規化後rarity(""=未指定)}, ...],
      "shipping_rules": {店舗名: {base_fee, free_threshold, min_order, ...}},
      "shipping_checked_on": "YYYY-MM-DD"
    }
    """
    data = request.get_json(silent=True) or {}
    cards_raw = data.get("cards", [])
    if not isinstance(cards_raw, list):
        return jsonify({"error": "cards は配列で指定してください"}), 400

    _load_cardnames()
    entries: list[dict] = []
    # 重複統合キー: (corrected_name, normalized_rarity_or_empty)
    seen: dict[tuple, int] = {}
    for raw in cards_raw[:MAX_WISH_CARDS]:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "")).strip()
        if not name or len(name) > MAX_CARD_NAME_LEN:
            continue
        try:
            qty = int(raw.get("qty", 1))
        except (TypeError, ValueError):
            qty = 1
        if qty < 1:
            continue
        if qty > MAX_DECK_QTY:
            qty = MAX_DECK_QTY
        rarity_raw = str(raw.get("rarity", "") or "").strip()
        # フロントから受けた rarity も正準名へ統合（古いキャッシュ・表記揺れ対策）
        rarity = normalize_rarity(rarity_raw) if rarity_raw else ""
        corrected = _correct_cardname(name)
        key = (corrected, rarity)
        if key in seen:
            # 同名同レアリティは枚数を加算
            idx = seen[key]
            entries[idx]["qty"] = min(entries[idx]["qty"] + qty, MAX_DECK_QTY)
            continue
        seen[key] = len(entries)
        entries.append({"name": corrected, "qty": qty, "rarity": rarity})

    if not entries:
        return jsonify({"shops": [], "db_missing": [], "unreleased": [], "entries": []})

    names = sorted({e["name"] for e in entries})
    total_qty = sum(e["qty"] for e in entries)
    total_cards = len(entries)

    # 直近7日分の price_history を一括取得（販売店舗のみ／sold_out 概念は売り在庫履歴に無いので全行採用）。
    # 7日窓はあくまで「その日に観測が無い場合の欠測補完」用で、採用する価格は各
    # (card, shop, rarity) キーの**最新観測日**の行のみ（同日内に複数行があれば min）。
    # 古い日の安値を現在値として拾わないようにする（監査F6）。
    # 構造: rows[card_name][shop][rarity] = min_price  ← 最新観測日の最安値
    rows_by_card_shop_rarity: dict[str, dict[str, dict[str, int]]] = {}
    # 各 (card_name, shop, rarity) キーで現在採用している行の観測日（新しい日が来たら置き換える判定用）
    latest_date_by_key: dict[tuple, str] = {}
    if _supabase_client:
        # 購入候補と同じく、カード番号＋日付の等号で今日の7日前から今日まで読む（price_history_reads。
        # 名前＋日付の範囲は長い名前で statement timeout になった。2026-10-22）。
        # 読めなかったカードは行が無いので db_missing に入り、画面は店のリアルタイム検索で補う
        rows_7d, _failed = fetch_daily_rows_by_card_names(
            _supabase_client, names, "card_id, shop_id, rarity_id, card_name, shop, rarity, min_price, recorded_at",
            jst_days_back(7), "wish-shop-totals")
        # recorded_at 欠落行の件数。存在すべきでない欠損だが、
        # サイレントに握りつぶさず警告する（監査 2026-08-07 追加, L10）
        missing_recorded_at = 0
        for row in rows_7d:
            name = row.get("card_name")
            shop = row.get("shop")
            price = row.get("min_price")
            recorded_at = row.get("recorded_at")
            if not recorded_at:
                missing_recorded_at += 1
                continue
            if not name or not shop or price is None:
                continue
            # PRICE_FLOOR 未満は収集異常値として除外（他の集計経路(daily_min_by_lowest_rarity等)と
            # 2026-08-07 監査で統一。監査M5）
            if price < PRICE_FLOOR:
                continue
            rarity_db = normalize_rarity(row.get("rarity", "") or "")
            date = recorded_at[:10]
            key = (name, shop, rarity_db)
            prev_date = latest_date_by_key.get(key)
            by_shop = rows_by_card_shop_rarity.setdefault(name, {})
            by_rarity = by_shop.setdefault(shop, {})
            if prev_date is None or date > prev_date:
                # より新しい観測日 → 古い日の値を置き換える
                by_rarity[rarity_db] = price
                latest_date_by_key[key] = date
            elif date == prev_date:
                # 同一日内の複数行は min を採用
                if price < by_rarity[rarity_db]:
                    by_rarity[rarity_db] = price
            # date < prev_date（より古い行が後から来た場合）は無視する。
            # DB側の order 指定でほぼ発生しないが、順序に依存せず正しくするための明示的ガード
        if missing_recorded_at:
            logger.warning(f"[wish-shop-totals] recorded_at欠落で除外: {missing_recorded_at}件")

    def _lookup_price_and_rarity(name: str, shop: str, rarity_pref: str):
        """エントリと店舗から、その店舗の (採用価格, 採用レアリティ) を返す。
        rarity_pref が空なら全レアリティの最安を採用。指定があるなら一致した行のみ。"""
        by_shop = rows_by_card_shop_rarity.get(name, {})
        by_rarity = by_shop.get(shop)
        if not by_rarity:
            return None, ""
        if rarity_pref:
            price = by_rarity.get(rarity_pref)
            return (price, rarity_pref) if price is not None else (None, "")
        # 未指定: 全レアリティ中の最安
        chosen_rarity, chosen_price = "", None
        for r, p in by_rarity.items():
            if chosen_price is None or p < chosen_price:
                chosen_rarity, chosen_price = r, p
        return chosen_price, chosen_rarity

    # 店舗ごとに集計（販売店舗のみ。買取系は対象外）
    shop_summaries = []
    for shop_name, _scrape_fn in SHOPS:
        if shop_name not in DEFAULT_SHOPS:
            continue  # 駿河屋などの非デフォルト店舗は除外
        total = 0
        covered_qty = 0
        items_in_shop: list[dict] = []
        missing_cards: list[dict] = []
        for entry in entries:
            name = entry["name"]
            qty = entry["qty"]
            rarity_pref = entry["rarity"]
            price, used_rarity = _lookup_price_and_rarity(name, shop_name, rarity_pref)
            if price is None:
                missing_cards.append({"name": name, "rarity": rarity_pref})
            else:
                total += price * qty
                covered_qty += qty
                # rarity は実際に採用したレアリティ（表示用）、rarity_pref はエントリの
                # 希望レアリティ（未指定=""）。フロントはエントリとの照合に rarity_pref を
                # 使う。rarity で照合すると未指定エントリ（""）が採用レアリティ（"ノーマル"等）
                # と一致せず、DBに価格があるのに「見つからない」扱いになる
                items_in_shop.append({
                    "name": name, "qty": qty, "price": price,
                    "rarity": used_rarity or rarity_pref,
                    "rarity_pref": rarity_pref,
                })
        # 送料・最低注文金額。total は商品代金の合計（送料・手数料を含まない）で、
        # 各店のしきい値判定はこの額を基準にする。
        shipping = calc_shipping(shop_name, total)
        shop_summaries.append({
            "shop": shop_name,
            "total": total,
            "shipping": shipping,
            "total_with_shipping": total + (shipping or 0),
            "free_shipping_at": amount_to_free_shipping(shop_name, total),
            "min_order_short": amount_to_min_order(shop_name, total),
            "orderable": is_orderable(shop_name, total),
            "covered_qty": covered_qty,
            "covered_cards": len(items_in_shop),
            "total_qty": total_qty,
            "total_cards": total_cards,
            "missing_cards": missing_cards,
            "items": items_in_shop,
            "url": _shop_search_url(shop_name, ""),
        })

    # 並び順: 全枚数そろう店舗を優先 → 注文できる店舗を優先 → 送料込み合計が安い順。
    # データが一部しか無い店舗は covered_qty 降順で続ける。
    # 最低注文金額に届かない店舗は「表示はするが下げる」（あと少しで届く場合があるため除外はしない）。
    def _sort_key(s):
        full = s["covered_qty"] == total_qty
        return (0 if full else 1, -s["covered_qty"],
                0 if s["orderable"] else 1, s["total_with_shipping"])
    shop_summaries.sort(key=_sort_key)

    # db_missing: そのエントリの (name, rarity) で 1 店舗も値が得られなかったもの。
    # 「カードが DB に1件もない」と「指定レアリティだけ無い」を区別したいので entry 単位で返す。
    def _entry_has_any_data(entry: dict) -> bool:
        name = entry["name"]
        rarity_pref = entry["rarity"]
        by_shop = rows_by_card_shop_rarity.get(name, {})
        if not by_shop:
            return False
        if not rarity_pref:
            return any(by_shop.values())
        return any(rarity_pref in by_rarity for by_rarity in by_shop.values())

    # DB に値が無いエントリのうち、まだ店に並ばないカード（未発売・TCG先行）は店のリアルタイム検索に
    # 回さず unreleased で返す（探しても無いのが分かっていて、画面が「見つからなかった」と出していた）。
    # DB に値があるもの（予約の出品を集めた等）は通常どおり集計に入れる
    db_missing = []
    unreleased = []
    for e in entries:
        if _entry_has_any_data(e):
            continue
        mark = _wish_unreleased_mark(e["name"])
        if mark:
            unreleased.append({"name": e["name"], "rarity": e["rarity"], **mark})
        else:
            db_missing.append({"name": e["name"], "rarity": e["rarity"]})

    # 送料ルールも一緒に返す。リアルタイム検索で合計が後から増えたとき、
    # フロント側で送料と注文可否を計算し直すのに使う（shipping.py の docstring 参照）。
    return jsonify({
        "shops": shop_summaries,
        "db_missing": db_missing,
        # まだ店に並ばないカード（未発売・TCG先行）。どの店の集計にも入らず、店の検索もしない
        "unreleased": unreleased,
        # フロントの照合キーをサーバ正本に統一するため、正規化後の entries を返す
        # （name補正・rarity正規化・重複統合済み。フロントはこれをキー突合に使う）
        "entries": entries,
        "shipping_rules": _shipping_rules_for_client(),
        "shipping_checked_on": SHIPPING_CHECKED_ON,
    })


@app.route("/api/card-rarities", methods=["POST"])
def api_card_rarities():
    """指定カード名の直近7日に出現したレアリティ候補を返す。
    入力: {"cards": ["カード名", ...]} 最大 MAX_WISH_CARDS 件
    返却: {"rarities": {"カード名": ["シークレット", "ノーマル", ...]}}
       順序は出現件数の降順（UI のドロップダウンに頻出順で並べるため）。
       DB が未設定 or 取得失敗の場合は空辞書で返す（UI 側で「候補なし」扱い）。
    """
    data = request.get_json(silent=True) or {}
    cards_raw = data.get("cards", [])
    if not isinstance(cards_raw, list):
        return jsonify({"error": "cards は配列で指定してください"}), 400

    _load_cardnames()
    names: list[str] = []
    seen = set()
    for raw in cards_raw[:MAX_WISH_CARDS]:
        name = str(raw).strip()
        if not name or len(name) > MAX_CARD_NAME_LEN:
            continue
        corrected = _correct_cardname(name)
        if corrected in seen:
            continue
        seen.add(corrected)
        names.append(corrected)
    if not names:
        return jsonify({"rarities": {}})

    # 集計: rarity_counts[name][canonical_rarity] = 出現件数
    rarity_counts: dict[str, dict[str, int]] = {}
    if _supabase_client:
        # 購入候補と同じく、カード番号＋日付の等号で今日の7日前から今日まで読む（price_history_reads）。
        # 読めなかったカードは候補なし扱い（従来どおり）
        rows, _failed = fetch_daily_rows_by_card_names(
            _supabase_client, names, "card_id, shop_id, rarity_id, card_name, rarity, shop",
            jst_days_back(7), "card-rarities")
        for row in rows:
            nm = row.get("card_name")
            r = normalize_rarity(row.get("rarity", "") or "")
            if not nm or not r:
                continue
            by_r = rarity_counts.setdefault(nm, {})
            by_r[r] = by_r.get(r, 0) + 1

    # 出現件数の降順、同数のときは正準名のアルファベット順で安定化
    rarities = {
        nm: [r for r, _cnt in sorted(by_r.items(), key=lambda kv: (-kv[1], kv[0]))]
        for nm, by_r in rarity_counts.items()
    }
    return jsonify({"rarities": rarities})


@app.route("/api/buyback")
def api_buyback():
    """買取価格比較 — 各店舗の買取価格をSSEで返す"""
    card_name = _normalize_query(request.args.get("q", ""))
    if not card_name:
        return jsonify({"error": "カード名を入力してください"}), 400
    if len(card_name) > MAX_CARD_NAME_LEN:
        return jsonify({"error": "カード名が長すぎます"}), 400

    # カード名の自動補正
    _load_cardnames()
    card_name = _correct_cardname(card_name)

    ALL_BUY_NAMES = [name for name, _ in BUYBACK_SHOPS]
    selected = request.args.getlist("shops") or DEFAULT_BUYBACK_SHOPS
    selected = [s for s in selected if s in ALL_BUY_NAMES]
    if not selected:
        return jsonify({"error": "有効な店舗を選択してください"}), 400

    active_shops = [(name, fn) for name, fn in BUYBACK_SHOPS if name in selected]

    # 販売検索(_last_search)とは別バケット。同一画面から販売→買取と続けて操作する
    # 動線（C-2の併置）で互いを弾かないようにする。制限の強さ自体は据え置き。
    rate_error = _consume_rate_limit(bucket=_last_buyback)
    if rate_error:
        return rate_error

    # 第1層: DB非存在のカード名はconfirmedフラグなしでは拒否（API防御）
    # ただし release_date 到来済みの未発売テーブル登録カード（週次インデックス未反映の
    # 発売済みカード）は既知カードとして許可する。
    confirmed = request.args.get("confirmed", "").lower() == "true"
    if not confirmed and card_name not in _cardnames_set and not _is_release_passed_unreleased(card_name):
        return jsonify({"error": "該当するカード名が見つかりません"}), 404

    # キャッシュ: 店舗単位で命中分と不足分を分ける（店舗束キャッシュ）
    cached_map, missing_shops = buyback_cache_get_shops(card_name, selected)
    cached_results = [r for rs in cached_map.values() for r in rs]

    if not missing_shops:
        if cached_results:
            _record_search(card_name)
        # 販売側と同じく、全店キャッシュ命中の経路でも型番補完を通す
        cached_results = infer_overframe(infer_codes(cached_results))
        def cached_stream():
            for shop_name, _ in active_shops:
                count = len(cached_map.get(shop_name, []))
                yield _sse({"type": "shop_done", "shop": shop_name, "count": count, "cached": True})
            yield _sse({**_build_buyback_done(cached_results),
                        **search_coverage(selected, cached_map, cached_results)})
        return Response(cached_stream(), mimetype="text/event-stream")

    shops_to_scrape = [(name, fn) for name, fn in active_shops if name in missing_shops]

    def generate():
        # キャッシュ命中店舗は即時に確定表示
        for shop_name, _ in active_shops:
            if shop_name in cached_map:
                yield _sse({"type": "shop_done", "shop": shop_name,
                            "count": len(cached_map[shop_name]), "cached": True})

        for shop_name, _ in shops_to_scrape:
            yield _sse({"type": "progress", "shop": shop_name})

        all_results = list(cached_results)
        scraped_ok = {}  # 取得に成功した店舗のみキャッシュする（失敗店舗は次回再試行）
        busy_shops = []  # 混雑で順番が回ってこなかった店（前回の結果を代わりに出す）
        stale_rows = []  # その店の前回の結果（完了の直前に結果へ足す）
        search_request = _shop_search_pool.request(PRIORITY_SINGLE, SHOP_QUEUE_TIMEOUT_SINGLE)
        disconnected = False

        try:
            # 全利用者が共有する店舗検索の列へ並べる（単品検索は一括検索より先に取られる）。
            if shops_to_scrape:
                futures = {
                    _submit_shop_fetch(search_request, name, fn, card_name): name
                    for name, fn in shops_to_scrape
                }
                for future in search_request.as_completed(futures, heartbeat=SEARCH_HEARTBEAT_SEC):
                    if future is None:
                        yield _sse({"type": "waiting", "queued_shops": [
                            futures[f] for f in futures if search_request.is_queued(f)]})
                        continue
                    shop_name = futures[future]
                    if _is_shop_busy(future.exception()):
                        # 販売と同じく、混雑で取れなかった店だけ前回の結果で代わりに埋める
                        # （書き戻さない・型番の補完と人気の集計にも使わない）
                        stale_results, stale_at = _stale_shop_fallback(
                            buyback_cache_get_shops_stale, card_name, shop_name)
                        busy_shops.append(shop_name)
                        stale_rows.extend(stale_results)
                        yield _sse({"type": "shop_error", "shop": shop_name,
                                    "error": SHOP_BUSY_MESSAGE, "busy": True})
                        yield _sse({"type": "shop_done", "shop": shop_name, "count": len(stale_results),
                                    "status": "busy", "stale": stale_at is not None, "stale_at": stale_at})
                        continue
                    error = None
                    try:
                        results, fetch_errors = _shop_fetch_result(future)
                    except Exception as exc:
                        results, fetch_errors, error = [], 1, str(exc)
                        logger.warning("店舗取得失敗: %s", shop_name, exc_info=True)
                    _record_shop_health_once(future, shop_name, results, fetch_errors)
                    if error or fetch_errors:
                        yield _sse({"type": "shop_error", "shop": shop_name,
                                    "error": "店舗からの取得に失敗しました"})
                    yield _sse({"type": "shop_done", "shop": shop_name, "count": len(results),
                                "status": "partial" if (error or fetch_errors) else "ok"})
                    all_results.extend(results)
                    if error is None and fetch_errors == 0:
                        scraped_ok[shop_name] = results

            # 販売側と同様、全店ぶんが揃った直後に型番を補完する（保存前）
            all_results = infer_overframe(infer_codes(all_results))
            corrected_by_shop: dict = {}
            for r in all_results:
                corrected_by_shop.setdefault(r.get("shop"), []).append(r)
            scraped_ok = {name: corrected_by_shop.get(name, []) for name in scraped_ok}

            buyback_cache_store_shops(card_name, scraped_ok)
            if all_results:
                _record_search(card_name)
            # 混雑した店の前回の結果は、保存・記録が終わってから表示用に足す
            # （反映前のキャッシュの行もあるので、足したあと OF のレアリティのそろえをもう一度通す）
            all_results = infer_overframe(all_results + stale_rows)
        except GeneratorExit:
            disconnected = True  # 利用者が画面を閉じた。始まっていない取得は finally で取り消す
            raise
        except Exception as e:
            logger.error(f"買取検索で予期しないエラー: {e}")
        finally:
            search_request.close()
            if not disconnected:
                yield _sse({**_build_buyback_done(all_results),
                            **search_coverage(selected, set(cached_map) | set(scraped_ok), all_results),
                            "busy_shops": busy_shops})

    release = _search_stream_slots.try_open()
    if release is None:
        return _search_busy_response()
    return _search_stream_response(generate(), release)


@app.route("/api/config")
def api_config():
    """フロントエンド用の設定情報"""
    # アフィリエイト設定をフロントエンドに渡す
    aff = {}
    for shop, conf in AFFILIATE_CONFIG.items():
        if shop in SUSPENDED_SHOPS:
            continue  # 休止中の店はリンクを出さないので、広告の表示計測（affPixel）も送らない
        if conf.get("sell_a8mat") or conf.get("buy_a8mat"):
            aff[shop] = {
                "sell": conf.get("sell_a8mat", ""),
                "buy": conf.get("buy_a8mat", ""),
            }
        elif conf.get("user_id"):
            aff[shop] = {"user_id": conf["user_id"]}
    return jsonify({
        "shops": [{"name": name, "default": name in DEFAULT_SHOPS} for name, _ in SHOPS],
        "buyback_shops": [{"name": name, "default": name in DEFAULT_BUYBACK_SHOPS} for name, _ in BUYBACK_SHOPS],
        "affiliate": aff,
    })


@app.route("/api/new-cards")
def api_new_cards():
    """新着の未発売カードを返す。正常0件と取得障害を区別する。"""
    try:
        # all=1 は一覧ページ用（上限なしで全件）。トップは従来どおり36件＋has_more。
        limit = None if request.args.get("all") == "1" else 36
        response = jsonify(_card_display.get_new_cards(limit=limit))
        # 管理画面の表示制御は共通30秒キャッシュで扱い、ブラウザには残さない。
        response.headers["Cache-Control"] = "no-store"
        return response
    except Exception:
        logger.exception("新着の未発売カード取得失敗")
        return jsonify({"error": "新着の未発売カードを取得できませんでした"}), 503


@app.route("/api/tcg-preview-cards")
def api_tcg_preview_cards():
    """海外先行カード一覧（/tcg-preview）の中身。正常0件と取得障害を区別する（/api/new-cards と同じ）。"""
    try:
        response = jsonify(_card_display.get_tcg_preview_cards())
        # 表示制御（hidden・画像表示の設定）は共通30秒キャッシュで扱い、ブラウザには残さない
        response.headers["Cache-Control"] = "no-store"
        return response
    except Exception:
        logger.exception("海外先行カードの取得失敗")
        return jsonify({"error": "海外先行カードを取得できませんでした"}), 503


# ── 環境データ（TCG PORTAL） ──

_meta_executor = ThreadPoolExecutor(max_workers=4)

@app.route("/api/meta")
def api_meta():
    """環境Tier表を返す（キャッシュヒットなら即座、ミスならスレッドプール経由）"""
    from meta_scraper import fetch_tier_snapshot, tier_snapshot
    force = request.args.get("force") == "1"
    include_metadata = request.args.get("include_metadata") == "1"

    # キャッシュがあればExecutorを通さず即返却
    if not force:
        cached = _cache_read("tier_list", _TIER_CACHE_TTL)
        if cached and "tiers" in cached:
            snapshot = tier_snapshot(cached)
            return jsonify(snapshot if include_metadata else snapshot["tiers"])

    future = _meta_executor.submit(fetch_tier_snapshot, force)
    try:
        snapshot = future.result(timeout=25)
    except Exception as e:
        logger.error(f"Tier表取得エラー: {e}")
        cached = _cache_read("tier_list", timedelta(days=7))
        snapshot = tier_snapshot(cached, stale=bool(cached), refresh_error="timeout_or_error")
    if not snapshot["tiers"] or snapshot["metadata"]["refresh_error"]:
        # 空応答をCDN等に公開キャッシュさせない（レビュー指摘Critical）
        g.no_cache = True
    return jsonify(snapshot if include_metadata else snapshot["tiers"])

@app.route("/api/meta/deck")
def api_meta_deck():
    """テーマ別の主要カードを返す"""
    theme = request.args.get("theme", "").strip()
    if not theme:
        return jsonify({"error": "テーマ名を指定してください"}), 400
    force = request.args.get("force") == "1"

    # キャッシュがあればExecutorを通さず即返却（ワーカー待ちを回避）
    if not force:
        cached = _cache_read(f"deck_{theme}", _DECK_CACHE_TTL)
        if cached and "cards" in cached:
            cached.pop("_ts", None)
            # フルデッキがあればそちらを優先
            full_deck = cached.get("full_deck", [])
            if full_deck:
                cached["deck_text"] = build_recipe_text(full_deck)
            else:
                cached["deck_text"] = build_deck_text(cached.get("cards", []))
            return jsonify(cached)

    future = _meta_executor.submit(fetch_deck_cards, theme, force)
    try:
        data = future.result(timeout=25)
    except Exception as e:
        logger.error(f"デッキデータ取得エラー ({theme}): {e}")
        data = {"theme": theme, "tier": 0, "share": 0, "cards": [], "full_deck": []}
    # フルデッキがあればそちらを優先
    full_deck = data.get("full_deck", [])
    if full_deck:
        data["deck_text"] = build_recipe_text(full_deck)
    else:
        data["deck_text"] = build_deck_text(data.get("cards", []))
    return jsonify(data)


# ── パック（最新弾）API ──

# /api/packs が最後に返した一覧（新弾の印・収録数つき）。/featured の HTML に埋め込み、JS の問い合わせを待たずに描くため
# （2026-10-22 LCP）。DB・外部サイトは待たない（覚えていなければ埋め込まない）
_PACKS_PAYLOAD_TTL_SEC = 600
_packs_payload = {"at": 0.0, "packs": None}


def _cached_packs_payload():
    """覚えているパックの一覧（10分以内）。無ければ None。"""
    packs = _packs_payload["packs"]
    if packs and time.time() - _packs_payload["at"] < _PACKS_PAYLOAD_TTL_SEC:
        return packs
    return None


@app.route("/api/packs")
def api_packs():
    """パック一覧を返す（スクレイピング失敗時はSupabaseから取得）"""
    packs = get_pack_list()
    if not packs and _supabase_client:
        try:
            resp = (_supabase_client.table("pack_list")
                    .select("name, wiki_page, tcg_name, release_date")
                    .order("release_date", desc=True)
                    .limit(10)
                    .execute())
            packs = [{"name": r.get("name", ""), "wiki_page": r.get("wiki_page", ""),
                      "tcg_name": r.get("tcg_name", ""), "date": r.get("release_date", ""),
                      # DBにカードリスト有無の情報が無いため、不明時は従来どおりの
                      # 文言にフォールバックさせる意図で True を入れる（packs.js側の設計）
                      "has_cards": True}
                     for r in (resp.data or [])]
            logger.info(f"パック一覧をSupabaseから取得: {len(packs)}件")
        except Exception as e:
            logger.error(f"Supabaseパック一覧取得失敗: {e}")

    # 各弾に featured フラグを付与（運用ウィンドウ内の新弾だけ true）
    fp_name = ""
    fp_wiki = ""
    if _supabase_client:
        try:
            from featured_pack import get_featured_pack, is_within_window
            fp = get_featured_pack(_supabase_client)
            if fp and is_within_window(fp):
                fp_name = fp.get("pack_name", "")
                fp_wiki = fp.get("wiki_page", "")
        except Exception as e:
            logger.warning(f"新弾フラグ判定失敗（全 featured=false にフォールバック）: {e}")
    for p in packs:
        p["featured"] = bool(fp_name) and (
            p.get("name") == fp_name or
            (bool(fp_wiki) and p.get("wiki_page") == fp_wiki)
        )
        # 収録数（一覧の行に出す）。キャッシュ済みのときだけ付け、ここでは外部サイトを読まない
        # （起動時の _prefetch_pack_cards が一覧のパックを先読みするので、ふだんは揃っている）
        if "card_count" not in p:
            cached = pack_cards_cached(p.get("name", ""))
            if cached:
                p["card_count"] = len(cached.get("cards") or [])

    if packs:
        _packs_payload.update(at=time.time(), packs=packs)
    return jsonify(packs)

@app.route("/api/packs/cards")
def api_pack_cards():
    """パックのカードリストを返す"""
    pack_name = request.args.get("name", "").strip()
    wiki_page = request.args.get("wiki", "").strip()
    tcg_name = request.args.get("tcg", "").strip()
    if not pack_name:
        return jsonify({"error": "パック名を指定してください"}), 400
    if any(len(v) > MAX_PACK_PARAM_LEN for v in (pack_name, wiki_page, tcg_name)):
        return jsonify({"error": "パック指定が長すぎます"}), 400
    # キャッシュ済みなら外部サイトを叩かないのでレート制限を消費しない。
    # レート制限の目的は yugioh-wiki へのスクレイプ濫用防止であり、キャッシュ
    # ヒットはその対象外。ここで消費すると「パックAを見た直後にパックBを押す」
    # という正当な操作が3秒制限で429になる（2026-09-01 にユーザーから報告）。
    cached = pack_cards_cached(pack_name)
    if cached is not None:
        return jsonify(_canonical_pack_cards(cached))
    rate_error = _consume_rate_limit(bucket=_last_packs)
    if rate_error:
        return rate_error
    future = _meta_executor.submit(fetch_pack_cards, pack_name, wiki_page, tcg_name)
    try:
        data = future.result(timeout=30)
    except Exception as e:
        logger.error(f"パックカード取得エラー ({pack_name}): {e}")
        data = {"pack": pack_name, "cards": [], "count": 0}
    return jsonify(_canonical_pack_cards(data))


def _canonical_pack_cards(data: dict) -> dict:
    """収録カード名を正式名に直して返す（キャッシュそのものは書き換えない）。
    遊戯王Wikiの表記（例: 「BF-」の半角ハイフン）のままだと、価格のデータベース・まとめて検索・購入候補が
    正式名（「BF－」）で返す名前と一致せず、新商品タブで価格が入らなかった（2026-10-21 reviewer 指摘）。
    直した結果が重なったら1つにする。
    正式名の一覧（data/cardnames_ja.json・毎週更新）に未登録の新しいカードは補正されず Wiki の全角表記（例:「Ｎｏ.９６」）のまま
    残り、収集が normalize_card_name で半角にして保存した名前と一致せず、店舗×レアの表で「記録がありません」になった
    （2026-11-01）。収集と同じく normalize_card_name を先に通す。"""
    cards = data.get("cards") or []
    if not cards:
        return data
    _load_cardnames()
    seen, out = set(), []
    for name in cards:
        fixed = _correct_cardname(normalize_card_name(str(name).strip()))
        if fixed and fixed not in seen:
            seen.add(fixed)
            out.append(fixed)
    result = dict(data)
    result["cards"] = out
    result["count"] = len(out)
    return result


# ── バックグラウンド環境データ プリフェッチ ──

def _prefetch_meta():
    """サーバー起動時にTier表をバックグラウンドで取得"""
    import threading
    def _run():
        time.sleep(3)  # 起動完了を待つ
        try:
            tiers = fetch_tier_list()
            logger.info(f"環境データ プリフェッチ完了: {len(tiers)} テーマ")
        except Exception as e:
            logger.warning(f"環境データ プリフェッチ失敗: {e}")
    t = threading.Thread(target=_run, daemon=True)
    t.start()

# Flask debugモードのreloaderと複数workerでの二重起動を防ぐ
if (os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not _is_debug_mode()) and _claim_startup_job("meta_prefetch"):
    _prefetch_meta()


def _prefetch_pack_cards():
    """サーバー起動時にパック収録カードをバックグラウンドで先読みする。

    Renderのファイルシステムはデプロイで揮発するため、デプロイ直後は
    キャッシュが空になり、ユーザーが素早くパックを続けて開くと429が
    再発する（本番実測で3回目まで429）。これを緩和するための起動時先読み。
    """
    import threading
    def _run():
        time.sleep(5)  # 起動完了を待つ（Tier表プリフェッチと重ならないよう少し長めに）
        try:
            packs = get_pack_list()
        except Exception as e:
            logger.warning(f"パックカード プリフェッチ失敗（一覧取得）: {e}")
            return
        prefetched = 0
        for pack in packs:
            try:
                fetch_pack_cards(pack.get("name", ""), pack.get("wiki_page", ""), pack.get("tcg_name", ""))
                prefetched += 1
            except Exception as e:
                logger.warning(f"パックカード プリフェッチ失敗 ({pack.get('name', '')}): {e}")
            time.sleep(1)  # 遊戯王Wikiへの連続アクセスを避けるための間隔
        logger.info(f"パックカード プリフェッチ完了: {prefetched}/{len(packs)} パック")
    t = threading.Thread(target=_run, daemon=True)
    t.start()

# Flask debugモードのreloaderと複数workerでの二重起動を防ぐ
if (os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not _is_debug_mode()) and _claim_startup_job("pack_prefetch"):
    _prefetch_pack_cards()


# ── 一人回しシミュレータ ──
# ルート定義は solitaire_routes.py（Blueprint）へ切り出し済み。
# feature flag ENABLE_VISUAL_SOLO_PLAY 背後の self-contained モジュール。
# Blueprint の登録・依存注入はファイル冒頭付近（Supabase初期化直後）で行う。


# ── 新弾フィーチャーページ ──

@app.route("/featured")
def featured_page():
    """新弾フィーチャーページ: 最新弾の収録カード全件と価格推移を掲載する。
    X投稿の CTA リンク先 URL として固定で使用（次回新弾でも URL は変わらない）。
    """
    # 覚えているパックの一覧があれば埋め込む（packs.js が API を待たずに描く。DB は待たない）
    return render_template("index.html", card_name="", page_mode="featured", packs_initial=_cached_packs_payload())


@app.route("/new-cards")
def new_cards_page():
    """未発売カード一覧ページ: トップの「新着の未発売カード」を商品ごとに全件表示する。"""
    # 覚えている新着があれば全件を埋め込み、最初の6枚の画像を先に読ませる（LCP。2026-10-22）。
    # 一度試したときは、埋め込みで描いた直後に別の処理が取り直して全部描き直し、画像も lazy で後回しになって悪化した。
    # new-cards.js で「埋め込みを使った直後は取り直さない」「一覧ページも最初の6枚はすぐ読む」にしてから入れている
    return render_template("index.html", card_name="", page_mode="new_cards",
                           new_cards_initial=_card_display.cached_new_cards(limit=None))


@app.route("/tcg-preview")
def tcg_preview_page():
    """海外先行カード一覧ページ: 英語版で先に出て日本に無いカード（tcg_preview_cards）を商品ごとに全件表示する。
    中身は未発売カード一覧（/new-cards）と同じ部品で、new-cards.js が /api/tcg-preview-cards から描く。
    予告中の新弾（tcg_announce_*）があれば、一番上に商品ごとのまとまりとして出し、まとめページ（/tcg-preview/<slug>）へつなぐ。"""
    try:
        announce_sets = _tcg_announce_view.list_sets()
    except Exception:
        logger.exception("予告中の新弾の取得失敗（入口を出さずに続ける）")
        announce_sets = []
    return render_template("index.html", card_name="", page_mode="tcg_preview", announce_sets=announce_sets,
                           announce_anchor=_tcg_announce_view.anchor)


# ── 新弾フィーチャーキャッシュ（メモリ） ──
# Wikiスクレイプを毎リクエストに乗せないよう、結果をメモリにキャッシュする。
# stale-while-revalidate: 古くなったらバックグラウンドで更新しつつ古い値を即返す。

_featured_cache: dict = {"data": None, "ts": 0.0}
_FEATURED_CACHE_SEC = 600  # 10分


def _load_featured_cache():
    """Wikiスクレイプ + 価格付与をバックグラウンドで実行しメモリに格納する。"""
    global _featured_cache
    if not _supabase_client:
        return
    try:
        from featured_pack import get_featured_pack, get_days_since_release, get_featured_cards
        pack = get_featured_pack(_supabase_client)
        if not pack:
            _featured_cache = {"data": {"pack": None, "days_since_release": -1, "cards": []}, "ts": time.time()}
            return
        days = get_days_since_release(pack)
        card_names = get_featured_cards(_supabase_client, pack)
        cards = []
        for name in card_names:
            best = _estimate_cache.get(name)
            if best:
                cards.append({
                    "name": name, "price": best.get("price"),
                    "shop": best.get("shop", ""), "rarity": best.get("rarity", ""),
                    "recorded_at": best.get("recorded_at", ""),
                })
            else:
                cards.append({"name": name, "price": None, "shop": "", "rarity": "", "recorded_at": ""})
        cards.sort(key=lambda x: -(x["price"] or 0))
        _featured_cache = {
            "data": {"pack": pack, "days_since_release": days, "cards": cards},
            "ts": time.time(),
        }
        logger.info(f"フィーチャーキャッシュロード完了: {pack['pack_name']} {len(cards)}枚")
    except Exception as e:
        logger.error(f"フィーチャーキャッシュロード失敗: {e}")


@app.route("/api/featured")
def api_featured():
    """新弾フィーチャーデータをメモリキャッシュから即返す。
    {
      "pack": {"pack_name", "start_date", "window_days"},
      "days_since_release": int,
      "loading": bool,  # True の場合はキャッシュ未確立。数秒後に再試行してください
      "cards": [{"name", "price", "shop", "rarity", "recorded_at"}]
    }
    """
    if not _supabase_client:
        return jsonify({"error": "DBに接続できません"}), 503

    cached = _featured_cache.get("data")
    ts = _featured_cache.get("ts", 0.0)
    age = time.time() - ts

    if cached is not None and age < _FEATURED_CACHE_SEC:
        # キャッシュが新鮮: そのまま返す
        return jsonify(cached)

    if cached is not None and age >= _FEATURED_CACHE_SEC:
        # stale-while-revalidate: 古い値を返しつつバックグラウンドでリフレッシュ
        Thread(target=_load_featured_cache, daemon=True).start()
        return jsonify(cached)

    # コールド（起動直後でキャッシュ未確立）: バックグラウンドロードを起動し loading=True を返す
    Thread(target=_load_featured_cache, daemon=True).start()
    return jsonify({"pack": None, "days_since_release": -1, "loading": True, "cards": []})


# 新商品タブの「店舗×レア」で受け付けるカードの上限（収録100種の商品がある）
MAX_PACK_MATRIX_CARDS = 150
# 「店舗×レア」でデータベースを読むときの受付上限（IP ごと）
PACK_MATRIX_RATE_LIMIT_SEC = 2  # TODO: calibrate from data
_last_pack_matrix: dict[str, float] = {}


@app.route("/api/packs/matrix", methods=["POST"])
def api_packs_matrix():
    """パックの収録カードの店舗×レアリティ表を、毎朝の収集（price_history）から返す（featured_matrix.py、DB読みのみ）。
    入力: {"cards": ["カード名", ...]}（正式名・収録順。/api/packs/cards が返す名前）
    返却: {"updated_at": "YYYY-MM-DD", "shops": [店舗名, ...],
           "rows": [{"name", "rarity", "cells": {店舗名: {"price", "url", "code", "recorded_at"}}}]}
    2026-10-22 に新弾専用の /api/featured/matrix から寄せた（新弾もこれを使う）。
    同じカードの並びは1時間覚えるので、データベースを読むのはパックごとに1時間に1回まで。
    """
    if not _supabase_client:
        return jsonify({"error": "DBに接続できません"}), 503
    data = request.get_json(silent=True) or {}
    cards_raw = data.get("cards", [])
    if not isinstance(cards_raw, list):
        return jsonify({"error": "cards は配列で指定してください"}), 400
    cards = []
    for name in cards_raw[:MAX_PACK_MATRIX_CARDS]:
        name = str(name).strip()
        if name and len(name) <= MAX_CARD_NAME_LEN:
            cards.append(name)
    if not cards:
        return jsonify({"updated_at": "", "shops": [], "rows": []})
    from featured_matrix import get_pack_matrix, is_cached
    # データベースを読むとき（覚えていない集まり）だけ受付上限を消費する。名前を変えて連打されても読み出しが積み上がらないように（reviewer 中4）
    if not is_cached(cards):
        rate_error = _consume_rate_limit(bucket=_last_pack_matrix, interval=PACK_MATRIX_RATE_LIMIT_SEC)
        if rate_error:
            return rate_error
    try:
        result = get_pack_matrix(_supabase_client, cards)
    except Exception as e:
        logger.warning(f"店舗×レアの表の取得に失敗: {e}")
        return jsonify({"error": "価格データの取得に失敗しました"}), 503
    return jsonify(result)


# ── Web Push 通知 ──

@app.route("/api/push/vapid-key")
def api_push_vapid_key():
    """フロントエンドに VAPID 公開鍵を返す。"""
    if not VAPID_PUBLIC_KEY:
        return jsonify({"error": "push not configured"}), 503
    return jsonify({"publicKey": VAPID_PUBLIC_KEY})


def _same_origin_ok() -> bool:
    """Origin ヘッダが自サイトと一致するか（他サイトからの購読登録・削除POSTを拒否）。
    Origin が無いリクエスト（非ブラウザ等）はブラウザ起点のCSRFではないため許可する。
    プロキシ配下でスキームが書き換わる場合があるため、ホスト名のみ比較する。"""
    origin = request.headers.get("Origin", "")
    if not origin:
        return True
    from urllib.parse import urlparse
    return urlparse(origin).netloc == request.host


def _canonical_base_url() -> str:
    """canonical・OGP・sitemap に載せる正のURL。CANONICAL_BASE_URL が未設定なら
    このリクエストの公開URL（同期リンクの発行は引き続き _public_base_url() を使う）。"""
    return site_urls.canonical_base_url_env() or _public_base_url()


def _public_base_url() -> str:
    """このリクエストの公開URLのベース（scheme://host）を、プロキシ配下でも正しく返す。

    2026-08-17 本番不具合の修正: Renderのリバースプロキシ配下ではTLSがプロキシで終端され、
    アプリへは http で転送されるため、request.url_root（= request.scheme 由来）だけでは
    常に http になり、発行したリンクが http:// になってしまっていた
    （X-Forwarded-Proto を見ていなかったため）。ワンタイムリンクのトークンは sync_id と
    同等の権限を持つ資格情報であり、http で開くとhttpsへのリダイレクト前の最初のリクエストで
    平文で流れる（設計文書 §12「トークンをGoogleに送らない」と同種の重大度）。

    X-Forwarded-Proto を最優先で信頼し、それが無い場合でも localhost/127.0.0.1 以外では
    https を強制する（プロキシ設定側の不備に対する保険。ローカル開発の http は妨げない）。
    """
    proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    if not proto:
        proto = request.scheme
    host = request.host
    host_only = host.split(":")[0]
    is_local = host_only in ("localhost", "127.0.0.1")
    if proto != "https" and not is_local:
        proto = "https"
    return f"{proto}://{host}"


@app.route("/api/push/subscribe", methods=["POST"])
def api_push_subscribe():
    """プッシュ購読を登録または更新する。
    入力: {subscription: {endpoint, keys:{p256dh, auth}}, cards: ["カード名", ...]}
    """
    if not _same_origin_ok():
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    sub = data.get("subscription", {})
    endpoint = sub.get("endpoint", "").strip()
    keys = sub.get("keys", {})
    p256dh = keys.get("p256dh", "").strip()
    auth = keys.get("auth", "").strip()
    cards_raw = data.get("cards", [])

    if not endpoint or not p256dh or not auth:
        return jsonify({"error": "subscription が不完全です"}), 400

    _load_cardnames()
    card_names = [_correct_cardname(str(n).strip()) for n in cards_raw[:50]
                  if str(n).strip() and len(str(n).strip()) <= MAX_CARD_NAME_LEN]

    if not _supabase_client:
        return jsonify({"ok": False, "reason": "db_unavailable"}), 200

    try:
        _supabase_client.table("push_subscriptions").upsert({
            "endpoint": endpoint,
            "p256dh": p256dh,
            "auth": auth,
            "card_names": card_names,
        }).execute()
        return jsonify({"ok": True})
    except Exception as e:
        logger.warning(f"[push/subscribe] 登録失敗: {e}")
        return jsonify({"error": "登録に失敗しました"}), 500


@app.route("/api/push/unsubscribe", methods=["POST"])
def api_push_unsubscribe():
    """プッシュ購読を削除する。
    入力: {endpoint: "..."}
    """
    if not _same_origin_ok():
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    endpoint = data.get("endpoint", "").strip()
    if not endpoint:
        return jsonify({"error": "endpoint が必要です"}), 400

    if not _supabase_client:
        return jsonify({"ok": False, "reason": "db_unavailable"}), 200

    try:
        _supabase_client.table("push_subscriptions") \
            .delete().eq("endpoint", endpoint).execute()
        return jsonify({"ok": True})
    except Exception as e:
        logger.warning(f"[push/unsubscribe] 削除失敗: {e}")
        return jsonify({"error": "削除に失敗しました"}), 500


# ── 端末間同期（購入候補・保存デッキ。P1で購入候補、P2で保存デッキを追加。
#    docs/design-sync-2026-08-09.md 第2版） ──
# ロジックは sync.py に集約し、ここでは配線のみ行う。

@app.route("/api/sync/init", methods=["POST"])
def api_sync_init():
    """sync_id を新規発行する（設計文書 §6.3・§8 遅延発行）。
    購入候補または保存デッキが1件以上あるとき、または「他の端末でも見る」操作時にのみ
    クライアントから呼ばれる。
    入力: {wishlist: [...], decks: [...]}
    出力: {ok: true, sync_id, wishlist_rev, decks_rev} / {ok: false, reason: "db_unavailable"}

    穴ふさぎ（2026-08-13）: 購入候補・保存デッキがどちらも空、またはリクエスト本文が
    JSONとして読めない場合は行を作らず400で拒否する。空アカウントの量産を防ぐため。
    """
    if not _same_origin_ok():
        return jsonify({"error": "forbidden"}), 403
    rate_error = _consume_rate_limit(bucket=_last_sync, interval=SYNC_RATE_LIMIT_SEC,
                                      key=_client_ip())
    if rate_error:
        return rate_error
    if not _supabase_client:
        return jsonify({"ok": False, "reason": "db_unavailable"}), 200

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        # 本文がJSONとして読めない（Content-Type不一致・壊れたJSON等）。行を作らず拒否する
        return jsonify({"error": "リクエスト本文が不正です"}), 400

    wl_cleaned, wl_error = _sync.validate_wishlist(data.get("wishlist", []))
    if wl_error:
        return jsonify({"ok": False, "reason": "invalid", "error": wl_error}), 400
    dk_cleaned, dk_error = _sync.validate_decks(data.get("decks", []))
    if dk_error:
        return jsonify({"ok": False, "reason": "invalid", "error": dk_error}), 400

    if not wl_cleaned and not dk_cleaned:
        return jsonify({"error": "購入候補または保存デッキが必要です"}), 400

    try:
        result = _sync.init_account(_supabase_client, wl_cleaned, dk_cleaned)
        return jsonify(result)
    except Exception as e:
        logger.warning(f"[sync/init] 発行失敗: {e}")
        return jsonify({"ok": False, "reason": "db_unavailable"}), 200


@app.route("/api/sync/push", methods=["POST"])
def api_sync_push():
    """購入候補・保存デッキを条件付き更新する（設計文書 §4.3・§6.1）。
    kind="wishlist"（購入候補）と kind="decks"（保存デッキ。P2で追加）を対応する。
    入力: {sync_id, kind: "wishlist"|"decks", base_rev, items}
    出力: {ok: true, status: "applied"|"merged", rev, items?} / {ok: false, reason: ...}
    """
    if not _same_origin_ok():
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    sync_id = str(data.get("sync_id", "")).strip()
    if not sync_id:
        return jsonify({"error": "sync_id が必要です"}), 400

    rate_error = _consume_rate_limit(bucket=_last_sync, interval=SYNC_RATE_LIMIT_SEC, key=sync_id)
    if rate_error:
        return rate_error
    if not _supabase_client:
        return jsonify({"ok": False, "reason": "db_unavailable"}), 200

    kind = data.get("kind", "wishlist")
    if kind not in ("wishlist", "decks"):
        return jsonify({"ok": False, "reason": "unsupported_kind"}), 400
    try:
        base_rev = int(data.get("base_rev", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "base_rev が不正です"}), 400

    if kind == "wishlist":
        cleaned, error = _sync.validate_wishlist(data.get("items", []))
    else:
        cleaned, error = _sync.validate_decks(data.get("items", []))
    if error:
        return jsonify({"ok": False, "reason": "invalid", "error": error}), 400

    try:
        if kind == "wishlist":
            result = _sync.push_wishlist(_supabase_client, sync_id, base_rev, cleaned)
        else:
            result = _sync.push_decks(_supabase_client, sync_id, base_rev, cleaned)
    except Exception as e:
        logger.warning(f"[sync/push] 更新失敗: {e}")
        return jsonify({"ok": False, "reason": "db_unavailable"}), 200

    if result.get("reason") == "not_found":
        # 行が存在しないことが確定した場合のみ新規発行する（設計文書 §6.8）。
        # DBエラー・例外の経路（上のexcept）では絶対にここに来ない＝再発行しない。
        # 再発行時、今回pushしたkindのデータのみを初期値にする（もう片方は空で始まり、
        # 次にそちら側がpushされた際の競合マージで自然に解決される。sync-client.js側の設計）。
        try:
            if kind == "wishlist":
                reissued = _sync.init_account(_supabase_client, cleaned, [])
            else:
                reissued = _sync.init_account(_supabase_client, [], cleaned)
        except Exception as e:
            logger.warning(f"[sync/push] 再発行失敗: {e}")
            return jsonify({"ok": False, "reason": "db_unavailable"}), 200
        reissued["reissued"] = True
        return jsonify(reissued)
    return jsonify(result)


@app.route("/api/sync/pull", methods=["POST"])
def api_sync_pull():
    """リビジョンが変わっていれば購入候補・保存デッキを返す（設計文書 §4.4・§6.2）。
    入力: {sync_id, wishlist_rev, decks_rev}
    出力: {ok: true, wishlist: {unchanged: true} | {rev, items},
                     decks:    {unchanged: true} | {rev, items}} / {ok: false, reason: ...}

    decks_rev はP1のクライアント（デプロイ直後のキャッシュ済みJS）が送ってこない場合がある。
    その場合は既定値 -1（現在のdecks_revと一致し得ない値）として扱い、常にdecksを
    返す（古いクライアントはdecksフィールドを無視するだけで実害はない）。
    """
    if not _same_origin_ok():
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    sync_id = str(data.get("sync_id", "")).strip()
    if not sync_id:
        return jsonify({"error": "sync_id が必要です"}), 400

    rate_error = _consume_rate_limit(bucket=_last_sync, interval=SYNC_RATE_LIMIT_SEC, key=sync_id)
    if rate_error:
        return rate_error
    if not _supabase_client:
        return jsonify({"ok": False, "reason": "db_unavailable"}), 200

    try:
        wishlist_rev = int(data.get("wishlist_rev", -1))
    except (TypeError, ValueError):
        return jsonify({"error": "wishlist_rev が不正です"}), 400
    try:
        decks_rev = int(data.get("decks_rev", -1))
    except (TypeError, ValueError):
        return jsonify({"error": "decks_rev が不正です"}), 400

    try:
        result = _sync.pull(_supabase_client, sync_id, wishlist_rev, decks_rev)
    except Exception as e:
        logger.warning(f"[sync/pull] 取得失敗: {e}")
        return jsonify({"ok": False, "reason": "db_unavailable"}), 200

    if result.get("reason") == "not_found":
        # pullには端末のローカルデータが無いためサーバー側では再発行しない。
        # クライアントが reissued を見て sync_id をクリアし、次の push 時に init し直す。
        return jsonify({"ok": False, "reason": "not_found", "reissued": True})
    return jsonify(result)


# ── 端末間同期: ワンタイムリンク（P3。設計文書 §6.4〜§6.7・§7.1〜§7.2） ──
# ロジックは sync.py に集約し、ここでは配線のみ行う。sync_id は既存3本のAPIと同じく
# URLに載せずすべてPOSTボディで受け取る（GA4がクエリ文字列を外部送信するため。§12）。

@app.route("/api/sync/link/create", methods=["POST"])
def api_sync_link_create():
    """発行側: sync_id に対するワンタイムリンクのトークンを発行する（設計文書 §6.4）。
    入力: {sync_id}
    出力: {ok: true, token, url, expires_in} / {ok: false, reason: ...}
    """
    if not _same_origin_ok():
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    sync_id = str(data.get("sync_id", "")).strip()
    if not sync_id:
        return jsonify({"error": "sync_id が必要です"}), 400

    # ユーザーが明示的に押すボタン起点のため、背景同期(init/push/pull)とはバケットを分離する
    # （2026-08-17: 共有していたことで、タブを開いた直後の pull が枠を食い、ほぼ確実に
    # 429になる不具合が本番で発生した）
    rate_error = _consume_rate_limit(bucket=_last_sync_link, interval=LINK_CREATE_RATE_LIMIT_SEC, key=sync_id)
    if rate_error:
        return rate_error
    if not _supabase_client:
        return jsonify({"ok": False, "reason": "db_unavailable"}), 200

    try:
        result = _sync.create_link_token(_supabase_client, sync_id)
    except Exception as e:
        logger.warning(f"[sync/link/create] 発行失敗: {e}")
        return jsonify({"ok": False, "reason": "db_unavailable"}), 200

    if result.get("reason") == "not_found":
        return jsonify({"ok": False, "reason": "not_found"}), 200

    result["url"] = f"{_public_base_url()}/sync?t={result['token']}"
    return jsonify(result)


@app.route("/api/sync/link/preview", methods=["POST"])
def api_sync_link_preview():
    """引き換え側: リンクの下見（副作用なし。設計文書 §6.5）。
    入力: {token}
    出力: {ok: true, remote: {wishlist_count, decks_count}, expires_in} /
          {ok: false, reason: "expired"|"used"|"not_found"}
    """
    if not _same_origin_ok():
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    token = str(data.get("token", "")).strip()
    if not token:
        return jsonify({"error": "token が必要です"}), 400

    # preview/redeem/unlink は sync_id を持たない場合があるため IP をキーにする（設計文書 §6.8）。
    # ユーザー操作起点のため背景同期(_last_sync)とはバケットを分離する（上記 link/create と同じ理由）
    rate_error = _consume_rate_limit(bucket=_last_sync_link, interval=LINK_RATE_LIMIT_SEC,
                                      key=_client_ip())
    if rate_error:
        return rate_error
    if not _supabase_client:
        return jsonify({"ok": False, "reason": "db_unavailable"}), 200

    try:
        result = _sync.preview_link(_supabase_client, token)
    except Exception as e:
        logger.warning(f"[sync/link/preview] 下見失敗: {e}")
        return jsonify({"ok": False, "reason": "db_unavailable"}), 200
    return jsonify(result)


@app.route("/api/sync/link/redeem", methods=["POST"])
def api_sync_link_redeem():
    """引き換え側: 確認画面のボタンを押したときに呼ぶ。ここで和集合マージを実行する（設計文書 §6.6）。
    入力: {token, wishlist: [...], decks: [...], old_sync_id: uuid|null}
    出力: {ok: true, sync_id, wishlist: {rev, items}, decks: {rev, items}} /
          {ok: false, reason: "expired"|"used"|"not_found"|"conflict_retry_exceeded"}

    old_sync_id はこのエンドポイントでは一切使わない（削除や参照を行わない）。
    受け取るだけで sync.py に渡さないことで「old_sync_id の行は削除しない」（設計文書 §6.6）を
    コード上で保証する。
    """
    if not _same_origin_ok():
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    token = str(data.get("token", "")).strip()
    if not token:
        return jsonify({"error": "token が必要です"}), 400

    # ユーザー操作起点のため背景同期(_last_sync)とはバケットを分離する（上記2つと同じ理由）
    rate_error = _consume_rate_limit(bucket=_last_sync_link, interval=LINK_RATE_LIMIT_SEC,
                                      key=_client_ip())
    if rate_error:
        return rate_error
    if not _supabase_client:
        return jsonify({"ok": False, "reason": "db_unavailable"}), 200

    wl_cleaned, wl_error = _sync.validate_wishlist(data.get("wishlist", []))
    if wl_error:
        return jsonify({"ok": False, "reason": "invalid", "error": wl_error}), 400
    dk_cleaned, dk_error = _sync.validate_decks(data.get("decks", []))
    if dk_error:
        return jsonify({"ok": False, "reason": "invalid", "error": dk_error}), 400

    try:
        result = _sync.redeem_link(_supabase_client, token, wl_cleaned, dk_cleaned)
    except Exception as e:
        logger.warning(f"[sync/link/redeem] 引き換え失敗: {e}")
        return jsonify({"ok": False, "reason": "db_unavailable"}), 200
    return jsonify(result)


@app.route("/api/sync/unlink", methods=["POST"])
def api_sync_unlink():
    """発行側: この端末だけ切り離す。現在のデータを新しい sync_id に複製する（設計文書 §6.7）。
    入力: {sync_id}
    出力: {ok: true, sync_id: 新しいuuid, wishlist_rev: 1, decks_rev: 1} / {ok: false, reason: ...}
    """
    if not _same_origin_ok():
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    sync_id = str(data.get("sync_id", "")).strip()
    if not sync_id:
        return jsonify({"error": "sync_id が必要です"}), 400

    # ユーザー操作起点のため背景同期(_last_sync)とはバケットを分離する（上記3つと同じ理由）
    rate_error = _consume_rate_limit(bucket=_last_sync_link, interval=LINK_RATE_LIMIT_SEC, key=sync_id)
    if rate_error:
        return rate_error
    if not _supabase_client:
        return jsonify({"ok": False, "reason": "db_unavailable"}), 200

    try:
        result = _sync.unlink(_supabase_client, sync_id)
    except Exception as e:
        logger.warning(f"[sync/unlink] 解除失敗: {e}")
        return jsonify({"ok": False, "reason": "db_unavailable"}), 200

    if result.get("reason") == "not_found":
        return jsonify({"ok": False, "reason": "not_found"}), 200
    return jsonify(result)


# ── フィードバック（不具合・要望） ──

@app.route("/api/feedback", methods=["POST"])
def api_feedback():
    """一人回しページ等からの不具合・要望を受け付ける。
    入力: {kind: "bug"|"request"|"other", body: str, contact: str, page: str}
    """
    # フィードバック専用レートリミット（30秒、IP単位）。専用の掃除ロジックを二重管理
    # しないよう、共通経路（_consume_rate_limit）に統一する（bucketを分けているため
    # 他系統のAPIとレート制限を奪い合うことはない）。
    rate_error = _consume_rate_limit(bucket=_last_feedback, interval=FEEDBACK_RATE_LIMIT_SEC,
                                      message="しばらく待ってから再度送信してください")
    if rate_error:
        return rate_error

    data = request.get_json(silent=True) or {}

    # kind の正規化
    kind = str(data.get("kind", "other")).strip()
    if kind not in ("bug", "request", "other"):
        kind = "other"

    # body の検証
    body = str(data.get("body", "")).strip()
    if not body:
        return jsonify({"error": "本文が空です"}), 400
    if len(body) > MAX_FEEDBACK_BODY_CHARS:
        return jsonify({"error": "本文が長すぎます"}), 400

    # その他フィールド
    contact   = str(data.get("contact", "")).strip()[:MAX_FEEDBACK_CONTACT_CHARS]
    page      = str(data.get("page", "")).strip()[:50]
    ua        = (request.headers.get("User-Agent") or "")[:300]

    record = {
        "kind":       kind,
        "body":       body,
        "contact":    contact,
        "page":       page,
        "user_agent": ua,
    }

    # Supabase への永続化（失敗してもOKを返す）
    if _supabase_client:
        try:
            _supabase_client.table("feedback_reports").insert(record).execute()
        except Exception as e:
            logger.warning(f"[feedback] Supabase insert 失敗: {e}")

    # Discord 通知（外部HTTP遅延を避けるため非同期）
    if _DISCORD_WEBHOOK_URL:
        Thread(target=_notify_feedback_discord, args=(record,), daemon=True).start()

    return jsonify({"ok": True})


@app.route("/api/card-text-report", methods=["POST"])
def api_card_text_report():
    """未発売カードの「テキストの誤り報告」を受け付ける。入力: {name, field, note}
    ロジックは unreleased_text_report.py（ここは Origin 確認・レート制限・配線だけ）。
    報告だけではカードを非公開にしない。
    """
    if not _same_origin_ok():
        return jsonify({"error": "不正なリクエストです"}), 403
    rate_error = _consume_rate_limit(bucket=_last_text_report, interval=TEXT_REPORT_RATE_LIMIT_SEC,
                                      message="しばらく待ってから再度送信してください")
    if rate_error:
        return rate_error
    if not _supabase_client:
        return jsonify({"error": "現在受け付けられません"}), 503
    try:
        import unreleased_text_report
        result, status = unreleased_text_report.process_report(
            _supabase_client, request.get_json(silent=True), _client_ip(), base_url=_public_base_url())
        return jsonify(result), status
    except Exception as e:
        logger.warning(f"[text-report] 受付失敗: {e}")
        return jsonify({"error": "受け付けに失敗しました。時間を置いて再度お試しください"}), 500


def _notify_feedback_discord(record: dict):
    """フィードバックを Discord Webhook へ通知する（バックグラウンドスレッドで実行）。"""
    kind_label = {"bug": "[不具合]", "request": "[要望]", "other": "[その他]"}.get(
        record.get("kind", "other"), "[その他]"
    )
    contact = record.get("contact") or "（なし）"
    body_text = record.get("body", "")[:1800]
    content = (
        f"{kind_label} [{record.get('page', '')}]\n"
        f"{body_text}\n"
        f"連絡先: {contact}"
    )
    try:
        _http.post(
            _DISCORD_WEBHOOK_URL,
            json={"content": content[:1990]},
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"[feedback] Discord 通知失敗: {e}")


# ── ヘルスチェック・ステータス ──

@app.route("/api/health")
def api_health():
    """全店舗のスクレイパーをテスト実行して結果を返す

    UptimeRobot 等の外部監視サービスからこのURLを定期的に叩く。
    200以外が返ったらアラート、という設定にする。

    環境変数 HEALTH_CHECK_KEY を設定した場合、
    ?key=XXXXX パラメータが必要。
    """
    auth_error = _require_health_key()
    if auth_error:
        return auth_error

    result = run_health_check()
    status_code = 200 if result["status"] == "ok" else 503
    return jsonify(result), status_code


_ATTR_JA = {"dark":"闇","light":"光","fire":"炎","water":"水","earth":"地","wind":"風","divine":"神"}
_PROP_JA = {
    # YGOResources keys
    "quickplay":"速攻","quick-play":"速攻","continuous":"永続",
    "field":"フィールド","equip":"装備","ritual":"儀式","counter":"カウンター",
    # ygoprodeck race keys（魔法/罠カードのプロパティ）
    "Normal":"","Quick-Play":"速攻","Continuous":"永続",
    "Field":"フィールド","Equip":"装備","Ritual":"儀式","Counter":"カウンター",
}
_TYPE_JA = {
    # 大分類（YGOResources cardType）
    "monster":"モンスター","spell":"魔法","trap":"罠",
    # ygoprodeck type フィールド → 日本語モンスター種別
    "Normal Monster":"通常モンスター",
    "Effect Monster":"効果モンスター",
    "Flip Effect Monster":"リバース・効果モンスター",
    "Flip Monster":"リバースモンスター",
    "Toon Monster":"トゥーンモンスター",
    "Spirit Monster":"スピリットモンスター",
    "Union Effect Monster":"ユニオン・効果モンスター",
    "Gemini Monster":"デュアルモンスター",
    "Tuner Monster":"チューナーモンスター",
    "Fusion Monster":"融合モンスター",
    "Fusion/Effect Monster":"融合・効果モンスター",
    "Ritual Monster":"儀式モンスター",
    "Ritual/Effect Monster":"儀式・効果モンスター",
    "Synchro Monster":"シンクロモンスター",
    "Synchro/Effect Monster":"シンクロ・効果モンスター",
    "Synchro/Tuner Monster":"シンクロ・チューナーモンスター",
    "XYZ Monster":"エクシーズモンスター",
    "XYZ/Effect Monster":"エクシーズ・効果モンスター",
    "Link Monster":"リンクモンスター",
    "Pendulum/Normal Monster":"ペンデュラム・通常モンスター",
    "Pendulum/Effect Monster":"ペンデュラム・効果モンスター",
    "Pendulum Effect Fusion Monster":"フュージョン・ペンデュラムモンスター",
    "Pendulum Synchro Effect Monster":"シンクロ・ペンデュラムモンスター",
    "Pendulum XYZ Effect Monster":"エクシーズ・ペンデュラムモンスター",
    "Token":"トークンモンスター",
    # 魔法/罠（ygoprodeck では "Spell Card" / "Trap Card"）
    "Spell Card":"魔法","Trap Card":"罠",
}
_RACE_JA = {
    "Warrior":"戦士族","Spellcaster":"魔法使い族","Dragon":"ドラゴン族",
    "Beast":"獣族","Beast-Warrior":"獣戦士族","Fiend":"悪魔族","Fairy":"天使族",
    "Insect":"昆虫族","Dinosaur":"恐竜族","Reptile":"爬虫類族","Sea Serpent":"海竜族",
    "Aqua":"水族","Pyro":"炎族","Rock":"岩石族","Machine":"機械族","Plant":"植物族",
    "Winged Beast":"鳥獣族","Zombie":"アンデット族","Thunder":"雷族","Fish":"魚族",
    "Psychic":"サイキック族","Divine-Beast":"幻神獣族","Creator-God":"創造神族",
    "Wyrm":"幻竜族","Cyberse":"サイバース族",
}
# OCGリミットレギュレーション（banlist_info.ban_ocg → 日本語ラベル）
_LIMIT_JA = {"Forbidden":"禁止","Banned":"禁止","Limited":"制限","Semi-Limited":"準制限"}

def _fetch_card_info_by_id(card_id: int, name: str) -> dict:
    """konami_id からカード情報dictを取得して返す（キャッシュ付き）。
    キャッシュヒット時は外部APIを叩かずに即返却する。
    外部HTTP呼び出しはロック外で行い、ロック保持中のネットワーク待ちを避ける。
    """
    # キャッシュ参照（ロック内）
    with _card_info_lock:
        if card_id in _card_info_cache:
            return _card_info_cache[card_id]

    # キャッシュミス → repository経由で取得（Supabaseキャッシュ→外部API。ロック外）
    try:
        summary = _ygores_repo.get_card_summary(card_id)
        if summary is None:
            return {"found": False}
        card_type = summary["card_type"]
        prints = summary["prints"]
        latest_print = ""
        if prints:
            last = prints[-1]
            latest_print = last if isinstance(last, str) else last.get("code", "")
        # 種族: ミラー形式は日本語種族を直接持つ。無ければ ygoprodeck で補完
        race = summary.get("race_ja")
        limit = None
        prop = _PROP_JA.get(summary["property"] or "", summary["property"] or "")
        # EXデッキ判定は repository が形式（API整数コード/ミラー日本語）を吸収して返す
        is_ex = summary["is_ex"]
        card_subtype = ""
        try:
            pdeck = _http.get(
                f"https://db.ygoprodeck.com/api/v7/cardinfo.php?konami_id={card_id}&misc=yes",
                timeout=5,
                headers={"User-Agent": _BROWSER_UA},
            )
            if pdeck.status_code == 200:
                pitem = pdeck.json().get("data", [{}])[0]
                card_subtype = pitem.get("type", "")
                race_en = pitem.get("race", "")
                if card_type == "monster":
                    # ミラー由来の種族を優先し、欠ける場合のみ ygoprodeck で補う
                    race = race or (_RACE_JA.get(race_en, race_en) or None)
                else:
                    # 魔法/罠: race フィールドがプロパティ種別
                    prop = _PROP_JA.get(race_en, "") or prop
                ban_ocg = pitem.get("banlist_info", {}).get("ban_ocg")
                limit = _LIMIT_JA.get(ban_ocg) if ban_ocg else None
        except Exception:
            pass
        # サブタイプ優先、なければ大分類
        mapped_type = _TYPE_JA.get(card_subtype) or _TYPE_JA.get(card_type, card_type)
        # properties が欠ける異常系のみ ygoprodeck の card_subtype で補完
        if not is_ex and card_subtype:
            is_ex = any(kw in card_subtype for kw in ("Fusion", "Synchro", "Xyz", "XYZ", "Link"))
        result = {
            "found": True,
            "konami_id": card_id,
            "card_type": mapped_type,
            "broad_type": card_type,   # "monster" | "spell" | "trap"（表示判定用）
            "is_ex": is_ex,            # EXデッキカードか否か
            "atk": summary["atk"],
            "def": summary["def"],
            "level": summary["level"],
            "rank": summary["rank"],
            "link_val": summary["link_rating"],
            "race": race,
            "attribute": _ATTR_JA.get(summary["attribute"] or "", summary["attribute"] or ""),
            "property": prop,
            "effect_text": summary["effect_text"],
            "pendulum_scale": summary.get("pendulum_scale"),       # ペンデュラムスケール（非Pはnull）
            "pendulum_effect": summary.get("pendulum_effect", ""),  # ペンデュラム効果テキスト
            "latest_print": latest_print,
            "limit": limit,
        }
    except Exception as e:
        logger.warning(f"[card-info] {name}: {e}")
        return {"found": False}

    # キャッシュ格納（ロック内）。上限超過時は一括クリアして空きを作る
    with _card_info_lock:
        if len(_card_info_cache) >= MAX_CARD_INFO_CACHE:
            _card_info_cache.clear()
            logger.info("[card-info] キャッシュ上限到達のためクリアしました")
        _card_info_cache[card_id] = result

    return result


@app.route("/api/card-info")
def api_card_info():
    """カード名からYGOResourcesのカード情報（ATK/DEF/効果テキスト等）を返す。
    未発売カードの場合は card_display 経由でプロキシ用データを返す。
    レスポンスに kind（'released'|'proxy'|'none'）を追加（後方互換: found 等の既存キーは維持）。
    """
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    # card_display 経由で表示種別を先に解決
    display = _card_display.resolve_card_display(name)

    # 未発売カードはカード情報パネル用にプロキシデータを返す。
    # 公式画像が表示される（kind=image, source=official_sample）場合でも、
    # 情報パネルは画像有無に関係なくテキスト情報が必要なため proxy データを返す。
    # （resolve_card_display は表示判定のみで、画像ありカードは proxy を返さない）
    is_official_sample = (
        display["kind"] == "image"
        and display.get("source") == "official_sample"
    )
    if display["kind"] == "proxy" or is_official_sample:
        # get_display_proxy: 未発売カード → TCG先行カード（英語名）の順
        proxy = display.get("proxy") or _card_display.get_display_proxy(name)
        if proxy:
            result = {
                "found":      False,
                "kind":       "proxy",
                "broad_type": proxy.get("broad_type", "monster"),
                "is_ex":      proxy.get("is_ex", False),
                "proxy":      proxy,
            }
            if display.get("renamed_to"):
                result["renamed_to"] = display["renamed_to"]  # 日本に来た TCG先行カード（日本語名での情報）
            resp = jsonify(result)
            # 未発売カードは表示設定変更で即時反映が必要なためキャッシュなし
            resp.headers["Cache-Control"] = "no-store"
            return resp

    # 発売済み or none → 従来どおりYGOResources参照
    # 日本に来た TCG先行カードの英語名は、日本語名で名前索引を引く（応答のキーは頼まれた名前のまま）
    renamed_to = display.get("renamed_to")
    lookup = renamed_to or name
    idx = _get_ygores_name_index()
    ids = idx.get(lookup)
    if not ids and _ygores_fuzzy_index:
        # 完全一致しない場合は記号・全半角ハイフン等のゆれを無視して再照合
        # （画像取得 _ygores_image_url と同じフォールバック。card-info でもゆれを吸収する）
        ids = _ygores_fuzzy_index.get(_fuzzy_key(lookup))
    if not ids:
        result = {"found": False, "kind": "none"}
        if renamed_to:
            result["renamed_to"] = renamed_to
        return jsonify(result), 200
    card_id = ids[0]
    result = _fetch_card_info_by_id(card_id, lookup)
    # 発売済みカードには kind='released' を付与（後方互換: found 等の既存キーは維持）
    result["kind"] = "released"
    if renamed_to:
        result = {**result, "renamed_to": renamed_to}  # キャッシュ済み dict を書き換えないよう複製する
    resp = jsonify(result)
    if result.get("found") and not renamed_to:
        # カード情報はほぼ不変のため長期キャッシュ可（card-image と同様）
        resp.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=604800"
    return resp


@app.route("/api/card-image")
def api_card_image():
    """カード名から画像URLを返す。
    card_display 経由で解決し kind を付与する（後方互換: url キーは維持）。
    - 発売済み / 公式画像あり: {"url": str, "kind": "image"}
    - 未発売プロキシ:           {"url": null, "kind": "proxy", "proxy": {...}}
    - 不明/pending等:           {"url": null, "kind": "none"}
    """
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    display = _card_display.resolve_card_display(name)

    if display["kind"] == "image":
        # source（released|official_sample）を付与（フロントの透かし・object-fit 出し分け用）
        body = {"url": display["url"], "kind": "image", "source": display.get("source", "released")}
        if display.get("renamed_to"):
            body["renamed_to"] = display["renamed_to"]
        resp = jsonify(body)
        if display.get("renamed_to"):
            resp.headers["Cache-Control"] = "no-store"  # 読み替えの結果は変わりうるので長期キャッシュしない
        else:
            # 発売済みカードは従来どおり長期キャッシュ可
            resp.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=604800"
        return resp

    if display["kind"] == "proxy":
        body = {"url": None, "kind": "proxy", "proxy": display["proxy"]}
        if display.get("renamed_to"):
            body["renamed_to"] = display["renamed_to"]
        resp = jsonify(body)
        # 未発売カードは表示設定変更で即時反映が必要なためキャッシュなし
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # kind == "none" → カーナベルにフォールバック（従来挙動を維持）
    url = kanabell_card_image_url(name)
    if url:
        # カーナベル画像も発売済みクリーン画像扱い（released）
        resp = jsonify({"url": url, "kind": "image", "source": "released"})
        resp.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=604800"
        return resp
    resp = jsonify({"url": None, "kind": "none"})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/card-images", methods=["POST"])
def api_card_images():
    """複数カード名を一括受け取り、画像URLを一括返却するバッチAPI（デッキグリッド高速化用）。
    リクエスト: {"names": ["カード名A", "カード名B", ...]}
    レスポンス: {"images": {"カード名A": {url, kind, [proxy]}|旧形式url|null, ...}}

    後方互換対応:
    - 発売済みカードの値は従来どおり url 文字列（旧クライアントが文字列前提でも動く）。
    - 未発売・プロキシカードの値は {url: null, kind: 'proxy', proxy: {...}} オブジェクト。
    - none（pending等）の値は null（従来どおり）。
    deck-input-panel.js は kind を見て分岐するよう更新済み。
    部分成功を許容（解決できない名前は null を返す）。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed as futures_completed

    data = request.get_json(force=True, silent=True) or {}
    names = data.get("names", [])
    if not isinstance(names, list):
        return jsonify({"error": "names must be a list"}), 400
    # 上限をMAX_DECK_CARDSで切る（DoS防止）
    names = [str(n).strip() for n in names if n][:MAX_DECK_CARDS]

    # card_display バッチで一括解決（発売済み・未発売・プロキシを一度に処理）
    display_results = _card_display.resolve_card_displays(names)

    images = {}
    renames = {}  # 日本に来た TCG先行カードの 英語名 → 日本語名（値が文字列のカードにも付けられるよう別に返す）
    has_unreleased = False  # 未発売カードが1件でもあればキャッシュを短くする
    timed_out = False       # 並列取得がタイムアウトした場合、長期キャッシュを付けない
    needs_kanabell = []     # YGOResources・未発売どちらにも該当しない名前のカーナベル候補

    for name in names:
        disp = display_results.get(name, {"kind": "none"})
        if disp.get("renamed_to"):
            renames[name] = disp["renamed_to"]
            has_unreleased = True  # 読み替えの結果は変わりうるので長期キャッシュしない
        if disp["kind"] == "image":
            if disp.get("source") == "official_sample":
                # 未発売の公式 SAMPLE 画像: object で返す（フロントが contain 表示・透かし無しに分岐）
                images[name] = {"url": disp["url"], "kind": "image", "source": "official_sample"}
                if disp.get("tcg_preview"):
                    images[name]["tcg_preview"] = True  # TCG先行カード（画面が購入候補の操作を出さない）
                has_unreleased = True
            else:
                # 発売済みクリーン画像: 値は従来どおり URL 文字列（後方互換 = released 扱い）
                images[name] = disp["url"]
        elif disp["kind"] == "proxy":
            # 未発売プロキシ: オブジェクトで返す（deck-input-panel.js が kind を判定）
            images[name] = {"url": None, "kind": "proxy", "proxy": disp["proxy"]}
            has_unreleased = True
        else:
            # none の場合はカーナベルフォールバックを試みる
            needs_kanabell.append(name)

    # カーナベルフォールバック（外部ES検索）は並列化して短縮
    if needs_kanabell:
        def _fetch_kanabell(n):
            try:
                url = kanabell_card_image_url(n)
                return n, url if url else None
            except Exception:
                return n, None

        # Q-3と同形: as_completed(timeout=...)はイテレータ自体がTimeoutErrorを送出するため
        # for全体を囲む。`with`だと__exit__がshutdown(wait=True)して未完了futureの終了を
        # 待ってしまいタイムアウトの意味が無くなるため、ここでは明示shutdownにする
        executor = ThreadPoolExecutor(max_workers=min(len(needs_kanabell), 5))
        futs = {executor.submit(_fetch_kanabell, n): n for n in needs_kanabell}
        try:
            for future in futures_completed(futs, timeout=10):
                try:
                    n, url = future.result()
                    images[n] = url  # url は文字列 or None（後方互換）
                except Exception:
                    images[futs[future]] = None
        except _FuturesTimeoutError:
            timed_out = True
            logger.warning(
                f"card-images: カーナベル{len(needs_kanabell)}件中10秒タイムアウト。取得できた分だけで応答")
        finally:
            # 未完了分は失敗時と同じ値（None）で埋める
            for n in needs_kanabell:
                if n not in images:
                    images[n] = None
            executor.shutdown(wait=False, cancel_futures=True)

    body = {"images": images}
    if renames:
        body["renames"] = renames
    resp = jsonify(body)
    if has_unreleased or timed_out:
        # 未発売カードが含まれる場合、またはタイムアウトで一部が未取得の場合は
        # 一過性の欠損を24時間固定しないようキャッシュしない
        resp.headers["Cache-Control"] = "no-store"
    else:
        # 全て発売済み・全件取得成功: カード名→画像URLの対応はほぼ不変のため長期キャッシュ可
        resp.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=604800"
    return resp


@app.route("/api/card-infos", methods=["POST"])
def api_card_infos():
    """複数カード名を一括受け取り、カード情報を一括返却するバッチAPI（一人回しデッキ読込高速化用）。
    リクエスト: {"names": ["カード名A", "カード名B", ...]}
    レスポンス: {"infos": {"カード名A": {found, broad_type, is_ex, kind, [proxy], ...}, ...}}
    部分成功を許容（解決できない名前は {found: false, kind: "none"} を返す）。
    _card_info_cache を経由するため、キャッシュ命中分は外部APIを叩かない。
    未発売カードは kind='proxy' でプロキシデータを返す（found=False）。
    """
    data = request.get_json(force=True, silent=True) or {}
    names = data.get("names", [])
    if not isinstance(names, list):
        return jsonify({"error": "names must be a list"}), 400
    names = [str(n).strip() for n in names if n][:MAX_DECK_CARDS]

    # card_display バッチで未発売カードを先に検出する
    display_results = _card_display.resolve_card_displays(names)

    idx = _get_ygores_name_index()
    infos = {}
    has_unreleased = False
    timed_out = False  # 並列取得がタイムアウトした場合、長期キャッシュを付けない
    renamed = {}  # 日本に来た TCG先行カード: 英語名 → 日本語名（応答のキーは頼まれた英語名のまま）

    # name → card_id を解決し、キャッシュ命中分は即格納、未命中分を並列取得
    need_fetch = []  # (name, card_id) のリスト
    for name in names:
        disp = display_results.get(name, {"kind": "none"})
        if disp.get("renamed_to"):
            renamed[name] = disp["renamed_to"]
            has_unreleased = True  # 読み替えの結果は変わりうるので長期キャッシュしない

        # 未発売カードはプロキシデータを返す。公式画像表示
        # （kind=image, source=official_sample）の場合も情報パネルや
        # EX ゾーン判定でカード情報が必要なため proxy データを返す。
        is_official_sample = (
            disp["kind"] == "image" and disp.get("source") == "official_sample"
        )
        if disp["kind"] == "proxy" or is_official_sample:
            proxy = disp.get("proxy") or _card_display.get_display_proxy(name)
            if proxy:
                infos[name] = {
                    "found":      False,
                    "kind":       "proxy",
                    "broad_type": proxy.get("broad_type", "monster"),
                    "is_ex":      proxy.get("is_ex", False),
                    "proxy":      proxy,
                }
                if name in renamed:
                    infos[name]["renamed_to"] = renamed[name]
                has_unreleased = True
                continue

        # 発売済みは YGOResources から取得（従来どおり）。読み替えのあるカードは日本語名で引く
        lookup = renamed.get(name, name)
        ids = idx.get(lookup)
        if not ids and _ygores_fuzzy_index:
            ids = _ygores_fuzzy_index.get(_fuzzy_key(lookup))
        if not ids:
            infos[name] = {"found": False, "kind": "none"}
            if name in renamed:
                infos[name]["renamed_to"] = renamed[name]
            continue
        card_id = ids[0]
        # キャッシュ参照（ロック内）
        with _card_info_lock:
            cached = _card_info_cache.get(card_id)
        if cached is not None:
            infos[name] = {**cached, "renamed_to": renamed[name]} if name in renamed else cached
        else:
            need_fetch.append((name, card_id))

    # キャッシュ未命中分を ThreadPoolExecutor で並列取得
    if need_fetch:
        def _fetch_one(item):
            n, cid = item
            result = _fetch_card_info_by_id(cid, renamed.get(n, n))  # キャッシュ格納も内部で行う
            if n in renamed:
                result = {**result, "renamed_to": renamed[n]}  # キャッシュ済み dict を書き換えないよう複製する
            return n, result

        # Q-3と同形（app.py 3699行付近と同じ理由でwithを避け明示shutdownにする）
        executor = ThreadPoolExecutor(max_workers=min(len(need_fetch), 5))
        futs = {executor.submit(_fetch_one, item): item for item in need_fetch}
        try:
            for future in as_completed(futs, timeout=25):
                try:
                    n, result = future.result()
                    infos[n] = result
                except Exception:
                    infos[futs[future][0]] = {"found": False, "kind": "none"}
        except _FuturesTimeoutError:
            timed_out = True
            logger.warning(
                f"card-infos: {len(need_fetch)}件中25秒タイムアウト。取得できた分だけで応答")
        finally:
            for n, _cid in need_fetch:
                if n not in infos:
                    infos[n] = {"found": False, "kind": "none"}
            executor.shutdown(wait=False, cancel_futures=True)

    resp = jsonify({"infos": infos})
    if has_unreleased or timed_out:
        # 未発売カードが含まれる場合、またはタイムアウトで一部が未取得の場合は
        # 一過性の欠損を24時間固定しないようキャッシュしない
        resp.headers["Cache-Control"] = "no-store"
    else:
        resp.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=604800"
    return resp


# カード種別キャッシュ（カード名 → "monster"|"spell"|"trap"|"unknown"）
# 種別は変化しないので無期限保持（ただし上限は設ける。_card_info_cache と同方式）
_card_type_cache: dict = {}
_card_type_cache_lock = Lock()
MAX_CARD_TYPE_CACHE = 5000  # これ以上溜まったら一括クリア

@app.route("/api/card-types", methods=["POST"])
def api_card_types():
    """複数カード名の種別（monster/spell/trap）を一括返却。サーバー側メモリキャッシュ付き。
    リクエスト: {"names": ["カード名A", ...]}
    レスポンス: {"types": {"カード名A": "monster"|"spell"|"trap"|"unknown", ...}}
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed as _ac

    body = request.get_json(force=True, silent=True) or {}
    names = body.get("names", [])
    if not isinstance(names, list):
        return jsonify({"error": "names must be a list"}), 400
    names = [str(n).strip() for n in names if n][:MAX_DECK_CARDS]

    result = {}
    need_fetch = []
    for name in names:
        # in→[] の2段階だとロック外の隙間で clear() と競合しKeyErrorになりうるため、
        # 1回のロックで取得する（_card_info_cache と同方式に統一）
        with _card_type_cache_lock:
            cached = _card_type_cache.get(name)
        if cached is not None:
            result[name] = cached
        else:
            need_fetch.append(name)

    if need_fetch:
        def _fetch_one(n):
            try:
                idx = _get_ygores_name_index()
                ids = idx.get(n)
                if not ids and _ygores_fuzzy_index:
                    ids = _ygores_fuzzy_index.get(_fuzzy_key(n))
                if not ids:
                    return n, "unknown"
                cid = ids[0]
                summary = _ygores_repo.get_card_summary(cid)
                ct = (summary or {}).get("card_type") or "unknown"
                return n, ct  # "monster" | "spell" | "trap"
            except Exception:
                return n, "unknown"

        # Q-3と同形（app.py 3699行付近と同じ理由でwithを避け明示shutdownにする）
        executor = ThreadPoolExecutor(max_workers=min(len(need_fetch), 8))
        futs = {executor.submit(_fetch_one, n): n for n in need_fetch}
        try:
            for future in _ac(futs, timeout=15):
                try:
                    n, t = future.result()
                    result[n] = t
                    # "unknown" は辞書に無い任意の文字列がキーとして溜まり続ける主因のため保存しない
                    if t != "unknown":
                        with _card_type_cache_lock:
                            if len(_card_type_cache) >= MAX_CARD_TYPE_CACHE:
                                _card_type_cache.clear()
                                logger.info("[card-types] キャッシュ上限到達のためクリアしました")
                            _card_type_cache[n] = t
                except Exception:
                    result[futs[future]] = "unknown"
        except _FuturesTimeoutError:
            logger.warning(
                f"card-types: {len(need_fetch)}件中15秒タイムアウト。取得できた分だけで応答")
        finally:
            for n in need_fetch:
                if n not in result:
                    result[n] = "unknown"
            executor.shutdown(wait=False, cancel_futures=True)

    return jsonify({"types": result})


@app.route("/api/import-neuron", methods=["POST"])
def api_import_neuron():
    """ニューロンのデッキ/カードURLをサーバー側で取得・解析して返す。

    スマホは拡張が使えないため、拡張のDOM抽出に代わりサーバーで静的HTMLを解析する。
    解析の実体は neuron_deck_parser.import_neuron（SSRF対策・ホスト検証を内包）。
    """
    # 価格検索とは別バケット。カード共有（取り込み→直後に価格検索）で
    # 互いを弾かないようにする。
    limited = _consume_rate_limit(bucket=_last_import,
                                  message="しばらく待ってから再度お試しください")
    if limited:
        return limited

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "URLを指定してください"}), 400
    if len(url) > 500:
        return jsonify({"ok": False, "error": "URLが長すぎます"}), 400

    try:
        result = _import_neuron(url)
    except Exception as e:
        logger.warning(f"[import-neuron] {e}")
        return jsonify({"ok": False, "error": "取り込み処理でエラーが発生しました"}), 500

    return jsonify(result), (200 if result.get("ok") else 400)


@app.route("/share-target")
def share_target():
    """PWA共有ターゲット（Android）。OSの共有メニューから渡されたURL/テキストから
    ニューロンURLを取り出し、フロントの取り込みフローへ ?import_url= 付きで渡す。

    GET方式のため service worker 側の特別処理は不要。
    """
    from flask import redirect

    shared = (request.args.get("url") or request.args.get("text") or "").strip()
    m = _re.search(r"https?://\S+", shared)
    target = m.group(0) if m else ""
    if target:
        return redirect(f"/?import_url={_url_quote(target, safe='')}")
    return redirect("/")


@app.route("/api/parse-deck-pdf", methods=["POST"])
def api_parse_deck_pdf():
    """ニューロン デッキシートPDFをパースしてカードリストを返す"""
    if "file" not in request.files:
        return jsonify({"error": "ファイルが必要です"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "PDFファイルを選択してください"}), 400

    try:
        import pdfplumber, re as _re

        with pdfplumber.open(f.stream) as pdf:
            if not pdf.pages:
                return jsonify({"error": "PDFにページがありません"}), 400

            page = pdf.pages[0]
            # y_tolerance=1: 行番号(y=142)とカード名(y=140)のy差=2ptを別行に分離
            words = page.extract_words(x_tolerance=2, y_tolerance=1, keep_blank_chars=False)

        result = _parse_neuron_pdf_words(words)
        return jsonify(result)

    except ImportError:
        return jsonify({"error": "PDF解析ライブラリが利用できません（pdfplumber未インストール）"}), 500
    except Exception as e:
        logger.warning(f"[parse-deck-pdf] {e}")
        return jsonify({"error": f"PDF解析エラー: {e}"}), 500


def _parse_neuron_pdf_words(words):
    """pdfplumber extract_words() の結果からデッキシートをパース"""
    import re

    if not words:
        return {
            "ok": False, "main": [], "ex": [],
            "warnings": ["テキストを抽出できませんでした（0単語）"],
        }

    # items: {str, x, y}  (y = page top からの距離; 下方向が大きい)
    items = [{"str": w["text"].strip(), "x": w["x0"], "y": w["top"]}
             for w in words if w["text"].strip()]
    items.sort(key=lambda it: (it["y"], it["x"]))

    # 行番号がカード名に混入した場合に除去するフォールバック
    # 例: "1BF－毒風のシムーン" → "BF－毒風のシムーン"
    def strip_row_num(s):
        cleaned = re.sub(r"^\d+", "", s).strip()
        return cleaned if cleaned else s

    # ヘッダー検出
    def hm(s): return "モンスター" in s
    def hs(s): return "魔法" in s and "カード" in s
    def ht(s): return "罠" in s and "カード" in s
    def he(s): return "エクストラ" in s
    def hsd(s): return "サイド" in s and "デッキ" in s

    mon_h  = next((it for it in items if hm(it["str"]) and not hs(it["str"]) and not ht(it["str"])), None)
    spl_h  = next((it for it in items if hs(it["str"]) and not hm(it["str"])), None)
    trp_h  = next((it for it in items if ht(it["str"]) and not hm(it["str"]) and not hs(it["str"])), None)
    ex_h   = next((it for it in items if he(it["str"])), None)
    side_h = next((it for it in items if hsd(it["str"]) and not he(it["str"])), None)

    debug_info = [
        f"総単語数: {len(items)}",
        f"先頭30: {[it['str'] for it in items[:30]]}",
        f"ヘッダ: mon={mon_h and mon_h['str']}, ex={ex_h and ex_h['str']}",
    ]

    if not mon_h or not ex_h:
        return {
            "ok": False, "main": [], "ex": [],
            "warnings": [
                "ニューロン形式として認識できませんでした。手動で修正してください。",
                *debug_info,
            ],
        }

    # ヘッダ行から枚数列xを取得（ソート済み: [モンスター枚数x, 魔法枚数x, 罠枚数x]）
    main_hdr_y = mon_h["y"]
    hdr_row = sorted([it for it in items if abs(it["y"] - main_hdr_y) < 5], key=lambda it: it["x"])
    main_qty_xs = sorted([it["x"] for it in hdr_row if it["str"].strip() == "枚数"])

    # 除外語
    SKIP = {"モンスターカード","魔法カード","罠カード","エクストラデッキ","サイドデッキ",
            "カード名","枚数","かな","氏名","カードゲームID","参加番号","日付","イベント名",
            "モンスター","魔法","罠","エクストラ","サイド","デッキ","カード"}
    skip_re = [re.compile(p) for p in [r"合計\s*>+", r"^>+", r"^メインデッキ"]]

    def skip_item(s):
        if s in SKIP: return True
        if any(p.search(s) for p in skip_re): return True
        if hm(s) or hs(s) or ht(s) or he(s) or hsd(s): return True
        if re.match(r"^[\d\s]+$", s) and len(s.replace(" ","")) > 4: return True
        return False

    is_digit = lambda s: bool(re.match(r"^\d+$", s))

    main_y   = main_hdr_y
    ex_hdr_y = ex_h["y"]

    # EXヘッダ行から枚数列xを取得（[EX枚数x, サイド枚数x]）
    ex_hdr_row = sorted([it for it in items if abs(it["y"] - ex_hdr_y) < 5], key=lambda it: it["x"])
    ex_qty_xs  = sorted([it["x"] for it in ex_hdr_row if it["str"].strip() == "枚数"])
    ex_qty_x   = ex_qty_xs[0] if ex_qty_xs else None  # EX列の枚数x

    warnings = []
    QTY_TOL = 25   # 枚数列x からの横方向許容幅 (pt)
    ROW_TOL = 7    # 同一行とみなすy方向許容幅 (pt)

    def pair_by_row(name_items, digit_items, qty_xs):
        """カード名トークンを「同一y行 × 同一枚数列」でグループ化し、
        同じセル内の複数トークンをスペース結合した上で枚数と対応付ける。
        スペースを含むカード名（例: 結晶魔術 光の涙）や特殊文字の分割に対応。"""
        if not qty_xs:
            return []
        sorted_qty_xs = sorted(qty_xs)

        # (y_bucket, my_qty_x) をキーにトークンをグループ化
        groups = {}  # key → [token, ...]
        y_rep  = {}  # key → 代表y値
        for nm in name_items:
            my_qty_x = next((qx for qx in sorted_qty_xs if qx > nm["x"]), None)
            if my_qty_x is None:
                continue
            # ROW_TOLで割って丸めることで同一行のトークンが同じバケットに入る
            key = (round(nm["y"] / ROW_TOL), my_qty_x)
            groups.setdefault(key, []).append(nm)
            y_rep.setdefault(key, nm["y"])

        result = []
        for key in sorted(groups.keys()):
            _, my_qty_x = key
            tokens = sorted(groups[key], key=lambda t: t["x"])
            combined_str = " ".join(t["str"] for t in tokens)
            nm_y = y_rep[key]
            tok = next(
                (d for d in digit_items
                 if abs(d["x"] - my_qty_x) <= QTY_TOL and abs(d["y"] - nm_y) <= ROW_TOL),
                None,
            )
            qty = int(tok["str"]) if tok else None
            result.append(({"str": combined_str, "x": tokens[0]["x"], "y": nm_y}, qty))
        return result

    # ── メインデッキ ──────────────────────────────────
    main_all = [it for it in items if main_y < it["y"] < ex_hdr_y and not skip_item(it["str"])]
    main_digits = [it for it in main_all if is_digit(it["str"])]
    main_names  = []
    for it in main_all:
        if not is_digit(it["str"]):
            name_str = strip_row_num(it["str"])
            if name_str and not skip_item(name_str):
                main_names.append({"str": name_str, "x": it["x"], "y": it["y"]})

    # 枚数列未検出時フォールバック: 数字群の中で最も右のxを枚数列とする
    eff_main_qty_xs = main_qty_xs if main_qty_xs else (
        [max(it["x"] for it in main_digits)] if main_digits else []
    )

    result_main = []
    for nm, qty in pair_by_row(main_names, main_digits, eff_main_qty_xs):
        safe = max(1, min(3, qty)) if qty is not None and 1 <= qty <= 3 else 1
        if qty is None:
            warnings.append(f"「{nm['str']}」の枚数が読み取れません（1枚として扱います）")
        result_main.append({"qty": safe, "name": nm["str"]})

    # ── エクストラデッキ ──────────────────────────────
    # EXとサイドは同一y範囲に横並びのため、EX枚数列x より左のカード名のみEXとして取り込む
    # （EX名x≈70 < ex_qty_x≈217 < サイド名x≈240 なので左右で自然に分離できる）
    ex_all = [it for it in items if it["y"] > ex_hdr_y and not skip_item(it["str"])]
    ex_digits = [it for it in ex_all if is_digit(it["str"])]

    # EX枚数列x 未検出時フォールバック
    if ex_qty_x is None:
        dxs = [it["x"] for it in ex_digits]
        ex_qty_x = max(dxs) if dxs else None

    # EX列のカード名のみ: EX枚数列xより左にある名前
    ex_names = []
    for it in ex_all:
        if not is_digit(it["str"]):
            name_str = strip_row_num(it["str"])
            if name_str and not skip_item(name_str):
                if ex_qty_x is None or it["x"] < ex_qty_x:
                    ex_names.append({"str": name_str, "x": it["x"], "y": it["y"]})

    result_ex = []
    for nm, qty in pair_by_row(ex_names, ex_digits, [ex_qty_x] if ex_qty_x else []):
        safe = max(1, min(3, qty)) if qty is not None and 1 <= qty <= 3 else 1
        result_ex.append({"qty": safe, "name": nm["str"]})

    ok = len(result_main) > 0 or len(result_ex) > 0
    if not ok:
        warnings.extend(debug_info)

    return {"ok": ok, "main": result_main, "ex": result_ex, "warnings": warnings}


@app.route("/api/status")
def api_status():
    """エラー追跡状態を返す（テスト実行なし、軽量）"""
    auth_error = _require_health_key()
    if auth_error:
        return auth_error

    return jsonify({
        "status": "ok",
        "tracker": tracker.get_status(),
    })


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _best_buy_item(items: list[dict]) -> dict | None:
    """デッキ買取の1枚の最高値。未開封・付属品つきは通常品と別の値段なので入れない（売れる額を大きく見せない。2026-10-28）"""
    regular = [r for r in items if not art_variant.row_is_sealed(r)]
    return max(regular, key=lambda x: x["price"]) if regular else None


def _build_buyback_done(results: list[dict]) -> dict:
    """買取価格比較用の完了メッセージ（高い順）"""
    results = [dict(r) for r in results]
    # API境界でレアリティを正準名に統合（過渡期の分裂値を吸収）
    for r in results:
        if r.get("rarity"):
            # 販売と同じく、カードラッシュの「…GREEN Ver.」は画面では 25thシークレット に寄せる（複製した行だけ）
            r["rarity"] = art_variant.display_rarity({**r, "rarity": normalize_rarity(r["rarity"])})
        # 未開封・付属品つきの印。画面の最高額は通常品で比べる（art_variant.is_sealed）
        r["sealed"] = art_variant.row_is_sealed(r)
    # by_rarity・by_shop は未開封も含めた最高額（画面は使っていない）
    by_rarity = {}
    for r in results:
        key = r.get("rarity") or "(不明)"
        if key not in by_rarity or r["price"] > by_rarity[key]["price"]:
            by_rarity[key] = r
    by_shop = {}
    for r in results:
        key = r["shop"]
        if key not in by_shop or r["price"] > by_shop[key]["price"]:
            by_shop[key] = r

    return {
        "type": "done",
        "results": results,
        "by_rarity": by_rarity,
        "by_shop": by_shop,
        "total": len(results),
    }


def _build_done(results: list[dict], corrected_name: str = "", card_name: str = "") -> dict:
    # 行は複製してから書き換える（同じ dict を価格の記録の裏スレッド _persist_scrape_async にも渡しているため）
    results = [dict(r) for r in results]
    # 絵（元のイラスト／イラスト違い／アジア版…）の印。画面の「イラスト」の切り替えに使う（art_variant.py）
    art_variant.annotate(results, card_name)
    # API境界でレアリティを正準名に統合（過渡期の分裂値を吸収）
    for r in results:
        if r.get("rarity"):
            r["rarity"] = art_variant.display_rarity({**r, "rarity": normalize_rarity(r["rarity"])})
    in_stock = [r for r in results if not r.get("sold_out")]
    # by_rarity・by_shop は絵・言語版を混ぜた最安（画面は使っていない。使うときは art・edition で分けること）
    by_rarity = {}
    for r in in_stock:
        key = r.get("rarity") or "(不明)"
        if key not in by_rarity or r["price"] < by_rarity[key]["price"]:
            by_rarity[key] = r
    by_shop = {}
    for r in in_stock:
        key = r["shop"]
        if key not in by_shop or r["price"] < by_shop[key]["price"]:
            by_shop[key] = r

    d = {
        "type": "done",
        "results": results,
        "by_rarity": by_rarity,
        "by_shop": by_shop,
        "total": len(results),
        "in_stock_count": len(in_stock),
        "sold_out_count": len(results) - len(in_stock),
    }
    if corrected_name:
        d["corrected_name"] = corrected_name
    return d


# ── 起動時プリロード ──
# DISABLE_STARTUP_JOBS=1（pytest実行時。tests/conftest.py が設定）のときは
# 起動時バックグラウンドスレッドを一切起動しない。pytestはapp.pyを何度もimportするため、
# 起動のたびにSupabase未接続のプリロード失敗ログが出たり、daemonスレッドがテスト後も
# 残り続けたりするのを防ぐ（レビュー指摘Medium・2026-09-22）。
_STARTUP_JOBS_DISABLED = os.environ.get("DISABLE_STARTUP_JOBS") == "1"


# 2026-10-22: 販売の値動きランキング（/api/movers・RPC get_price_movers）の起動時
# プリロードは廃止した。/api/movers は画面から参照されておらず（static・templates に
# 呼び出しなし、Render のログで 10-20〜22 のアクセス0件）、起動直後に相場キャッシュ等と
# 同時に重い集計を走らせて statement timeout（57014）を招くだけだった。
# /api/movers 自体は残し、呼ばれたときに _get_price_movers が取得・1時間キャッシュする。

def _preload_buyback_movers():
    """サーバー起動直後にバックグラウンドで買取moversキャッシュを作成"""
    time.sleep(8)  # 相場キャッシュ（0〜6秒のジッター後に開始）と少しずらす
    try:
        _get_buyback_movers("up")
        logger.info("買取値動きランキングをプリロードしました")
    except Exception as e:
        logger.warning(f"買取プリロード失敗: {e}")

if not _STARTUP_JOBS_DISABLED and _claim_startup_job("buyback_movers_preload"):
    threading.Thread(target=_preload_buyback_movers, daemon=True).start()

# キャッシュのTTLが切れる少し手前で裏更新をかけ、ユーザーのリクエストが
# TTL失効直後の重い再計算（RPC/スクレイプ）に当たらないようにする常駐スレッド。
# 既存のSWR（stale-while-revalidate）経路はリクエストが来て初めて動くため、
# アクセスが疎な時間帯（深夜等）はTTL失効直後の1発目が重いままだった。
_CACHE_WARMER_INTERVAL_SEC = 60  # ループ間隔　# TODO: calibrate from data
_CACHE_WARMER_MARGIN_SEC = 300  # TTLのこの秒数手前で裏更新をかける　# TODO: calibrate from data
# レビュー指摘（High・2026-09-22）: 裏更新が失敗してもcache_timeは進まないため、
# バックオフ無しだと（特にtop-decksの最大65秒の外部取得を）60秒ごとに永久リトライしてしまう。
_CACHE_WARMER_FAIL_BACKOFF_SEC = 600  # 直近の失敗からこの秒数は同じキャッシュの裏更新を試みない　# TODO: calibrate from data
# top-decksプリロード前に相場キャッシュ(_estimate_cache)のロード完了を待つ上限秒数。
# 本番実測ではブート後7秒程度で完了するため30秒あれば十分に余裕がある値。
# 実測値そのものではなく安全マージンを含めた仮置きのため校正対象として残す
_CACHE_WARMER_ESTIMATE_WAIT_SEC = 30  # TODO: calibrate from data（本番実測: 相場キャッシュ完了はブート後7秒）
# 未発売カード表示キャッシュ（card_display・TTL30秒）の定期取り直し間隔。
# card_display は TTL 切れでも _CACHE_STALE_MAX(600秒) 以内なら古い値を返して裏で取り直すため、
# アクセスが途切れてもその上限に届かないよう、上限の半分の間隔で取り直す。
# 1回の転送は約84KB（2026-10-16 本番75件）で、5分ごとなら1日約24MB。
_CARD_DISPLAY_REFRESH_SEC = 300

_cache_warmer_lock_path = os.path.join(os.path.dirname(__file__), ".cache", "startup_cache_warmer.lock")


def _touch_cache_warmer_lock():
    """cache_warmerのロックファイルのmtimeを更新する（ハートビート）。
    _claim_startup_job のTTL(300秒)以内に必ず1回touchすることで、このプロセスが
    生きている間は他プロセスにロックを横取りされないようにする。"""
    try:
        os.utime(_cache_warmer_lock_path, None)
    except OSError:
        pass


def _cache_warmer():
    """主要キャッシュを起動時に温め、以後はTTL失効前に裏更新し続ける常駐スレッド。

    レビュー指摘（High・2026-09-22）:
    - _claim_startup_job は起動時の一度きりの判定だったため、ワーカーが
      ロックTTL(300秒)以内に再起動すると warmer が二度と起動しなかった。
      ここではスレッド自体は常に起動し、ループ内で毎回ロック取得を試み、
      保持中は毎周ロックファイルをtouchするハートビート方式にする
      （プロセスが死ねば300秒でロックが失効し、次のプロセスが引き継げる）。
    - 裏更新はTTL失効直前のホットキャッシュを保つのが目的で、コールド充填
      （まだ一度も成功していない状態）は初回プリロードとユーザーリクエスト契機の
      SWR経路に任せる。失敗時はcache_timeが更新されないため、「既存キャッシュの
      有無」を見ずに経過時間だけで判定すると、キャッシュが空のままTTL超過判定が
      常に真になり続け、重い取得（特にtop-decksは最大65秒）を永久リトライしてしまう。
    """
    have_lock = False
    claim_failed_logged = False
    card_display_refreshed_at = time.time()  # 初回プリロード直後は取り直さない
    while True:
        if not have_lock:
            have_lock = _claim_startup_job("cache_warmer")
            if not have_lock:
                if not claim_failed_logged:
                    logger.info("cache_warmer: 他プロセスが実行中のためロック取得を待機します")
                    claim_failed_logged = True
                time.sleep(_CACHE_WARMER_INTERVAL_SEC)
                continue
            claim_failed_logged = False
            logger.info("cache_warmer: ロックを取得しました")
            time.sleep(10)  # Supabase接続確立・他の起動時プリロードと重ならないよう少し待つ  # TODO: calibrate from data

            # 初回プリロード（top-decksは外部スクレイプで重いため最後）。
            # ローカルI/O（カード名辞書）を先に、ネットワーク取得（YGOResources索引）を
            # 後にする（2026-09-22 reviewer指摘: ネットワーク待ちでローカルI/Oの完了が
            # 遅れるのを避けるため）
            try:
                _load_cardnames()
                logger.info("カード名データベースをプリロードしました")
            except Exception as e:
                logger.warning(f"カード名データベースプリロード失敗: {e}")
            try:
                _get_ygores_name_index()
                logger.info("YGOResources名前インデックスをプリロードしました")
            except Exception as e:
                logger.warning(f"YGOResources名前インデックスプリロード失敗: {e}")
            try:
                # 新着の未発売カード（/api/new-cards）。初回の同期取得を誰かに待たせない
                _card_display.get_new_cards()
                logger.info("未発売カード表示キャッシュをプリロードしました")
            except Exception as e:
                logger.warning(f"未発売カード表示キャッシュプリロード失敗: {e}")
            try:
                _get_top_movers_cached(None)
                logger.info("価格推移ランキング(全体)をプリロードしました")
            except Exception as e:
                logger.warning(f"価格推移ランキング(全体)プリロード失敗: {e}")
            try:
                _get_top_movers_cached("of")
                logger.info("価格推移ランキング(OF)をプリロードしました")
            except Exception as e:
                logger.warning(f"価格推移ランキング(OF)プリロード失敗: {e}")
            try:
                _get_top_priced_cached("gmr")
                logger.info("最安値ランキング(GMR)をプリロードしました")
            except Exception as e:
                logger.warning(f"最安値ランキング(GMR)プリロード失敗: {e}")
            # top-decksは相場キャッシュ(_estimate_cache)未ロードだと計算をスキップし、
            # warmerは「既存キャッシュがある時だけ」裏更新する設計のため、ここで初回が
            # 空振りすると次のユーザーが重い取得を被ってしまう。相場キャッシュの
            # 別スレッドロード（起動時に開始済み）完了を最大30秒待ってから進む
            wait_start = time.time()
            while _estimate_cache_time == 0 and time.time() - wait_start < _CACHE_WARMER_ESTIMATE_WAIT_SEC:
                time.sleep(1)
            if _estimate_cache_time == 0:
                logger.warning(f"cache_warmer: 相場キャッシュのロード待機が{_CACHE_WARMER_ESTIMATE_WAIT_SEC}秒を超えました。top-decksプリロードは未ロードのまま進みます")
            try:
                _get_top_decks_cached()
                logger.info("環境デッキランキングをプリロードしました")
            except Exception as e:
                logger.warning(f"環境デッキランキングプリロード失敗: {e}")
            continue

        # ロック保持中: 毎周touchして他プロセスに取られないようにする
        _touch_cache_warmer_lock()
        time.sleep(_CACHE_WARMER_INTERVAL_SEC)

        # TTL失効前の裏更新。「既存キャッシュがある時だけ」かつ「直近の失敗から
        # _CACHE_WARMER_FAIL_BACKOFF_SEC秒経過している時だけ」裏更新する
        # （コールド充填はしない・失敗の永久リトライを防ぐ）。
        # 直列実行で最大65秒(top-decks)ずれるため、各ブロックの先頭でnowを取り直す。
        now = time.time()
        if _top_movers_cache and now - _top_movers_cache_time > _top_page.TOP_MOVERS_CACHE_SEC - _CACHE_WARMER_MARGIN_SEC:
            if now - _top_movers_refresh_failed_at.get("__all__", 0) >= _CACHE_WARMER_FAIL_BACKOFF_SEC:
                _run_swr_refresh(
                    _get_top_movers_cached,
                    on_failed=lambda: _mark_top_movers_refresh_failed(None),
                    fn_kwargs={"group": None, "force": True},
                )

        now = time.time()
        if _top_movers_group_cache.get("of") and now - _top_movers_group_cache_time.get("of", 0) > _top_page.TOP_MOVERS_CACHE_SEC - _CACHE_WARMER_MARGIN_SEC:
            if now - _top_movers_refresh_failed_at.get("of", 0) >= _CACHE_WARMER_FAIL_BACKOFF_SEC:
                _run_swr_refresh(
                    _get_top_movers_cached,
                    on_failed=lambda: _mark_top_movers_refresh_failed("of"),
                    fn_kwargs={"group": "of", "force": True},
                )

        now = time.time()
        if _top_priced_cache.get("gmr") and now - _top_priced_cache_time.get("gmr", 0) > _top_page.TOP_PRICED_CACHE_SEC - _CACHE_WARMER_MARGIN_SEC:
            if now - _top_priced_refresh_failed_at.get("gmr", 0) >= _CACHE_WARMER_FAIL_BACKOFF_SEC:
                _run_swr_refresh(
                    _get_top_priced_cached,
                    on_failed=lambda: _mark_top_priced_refresh_failed("gmr"),
                    fn_kwargs={"group": "gmr", "force": True},
                )

        now = time.time()
        if _top_decks_cache and now - _top_decks_cache_time > _top_page.TOP_DECKS_CACHE_SEC - _CACHE_WARMER_MARGIN_SEC:
            if now - _top_decks_refresh_failed_at >= _CACHE_WARMER_FAIL_BACKOFF_SEC:
                _run_swr_refresh(
                    _get_top_decks_cached,
                    on_failed=_mark_top_decks_refresh_failed,
                    fn_kwargs={"force": True},
                )

        now = time.time()
        if now - card_display_refreshed_at >= _CARD_DISPLAY_REFRESH_SEC:
            # 失敗しても古い値は card_display 側に残り、次の周期でまた試す（5分間隔なので多重発火しない）
            card_display_refreshed_at = now
            try:
                _card_display.refresh_caches()
            except Exception as e:
                logger.warning(f"未発売カード表示キャッシュ裏更新失敗: {e}")

        now = time.time()
        if _buyback_movers_cache and now - _buyback_movers_cache_time > _BUYBACK_MOVERS_CACHE_SEC - _CACHE_WARMER_MARGIN_SEC:
            try:
                _refresh_buyback_movers_cache()
            except Exception as e:
                logger.warning(f"買取値動きランキング裏更新失敗: {e}")


if not _STARTUP_JOBS_DISABLED:
    threading.Thread(target=_cache_warmer, daemon=True).start()

# 有料プランへ上げる合図の見張り（upgrade_signals.py）。プロセスごとに自分の取得口・DB 応答を数えるので、
# cache_warmer のようなプロセス間のロックは取らない（worker は1本）
if not _STARTUP_JOBS_DISABLED:
    upgrade_signals.start(
        get_pool_snapshot=_shop_search_pool.snapshot,
        get_search_busy_total=_search_stream_slots.busy_total,
        # 遅れて作られる接続は取り出す関数で渡し、作られたら次の周で差し込む（reread_trust は管理画面からだけ）
        db_clients=[_supabase_client, _card_display._supabase_client,
                    lambda: _ygores_repo._sb,
                    lambda: getattr(sys.modules.get("admin_unreleased"), "_supabase", None),
                    lambda: getattr(sys.modules.get("reread_trust"), "_client_cache", None)],
        store=upgrade_signals.make_store_client(_SUPABASE_URL, _SUPABASE_KEY),
    )

if not _STARTUP_JOBS_DISABLED and _claim_startup_job("featured_prefetch"):
    threading.Thread(target=_load_featured_cache, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # フェイルクローズ化（2026-08-20）: 既定"1"だとWerkzeugデバッガが既定で有効になり
    # 実質RCE（任意コード実行）になりうるため、_is_debug_mode() と揃えて既定"0"にする。
    # ローカル開発時は FLASK_DEBUG=1 を明示設定する（tcg-web/CLAUDE.md 参照）。
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug, host="0.0.0.0", port=port)
