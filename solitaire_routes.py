"""
solitaire_routes.py — 一人回し(solo play) シミュレータのルート群

このモジュールは一人回し機能を単一の feature flag 背後に閉じた
self-contained な Blueprint として提供する。IP 停止要請が来た際は
環境変数 ENABLE_VISUAL_SOLO_PLAY=0 を設定すれば、この機能だけが止まり、
相場・デッキ・コレクション等のコア機能は無傷で動き続ける（kill-switch）。

OFF 時の挙動:
  - GET /solitaire                      → 503 + solo_disabled.html（案内ページ）
  - GET /solitaire/replays              → 503 + solo_disabled.html（リプレイ一覧ページ）
  - POST /api/solitaire/replay          → 404
  - GET  /api/solitaire/replay/<id>     → 404
  - GET  /api/solitaire/public          → 404（みんなのリプレイの一覧）
  - POST /api/solitaire/replay/<id>/unpublish → 404（公開の取り下げ）
  - POST /api/solitaire/replay/<id>/rename    → 404（公開中のリプレイの名前を変える）
  - GET  /solitaire/og/<id>.png         → 404
  - GET  /solitaire/og/card/<id>.png    → 404（未発売カードのリンク画像）

依存注入（循環 import 回避）:
  app.py は _supabase_client を保持するため、起動時に init_solitaire() で
  Supabase クライアントへのアクセサ関数を注入する。card_display.py の
  register_released_resolver() と同じ関数注入方式。
"""

import io
import os
import re
import time
import hmac
import hashlib
import secrets
import logging
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from urllib.parse import urlsplit

from flask import Blueprint, render_template, request, abort, Response, send_file

from client_ip import _client_ip
from constants import REPLAY_IMAGE_SOURCES
from replay_board import validate_board
import card_display
import card_og_image
import replay_og_image
import replay_moderation

logger = logging.getLogger(__name__)

# ── feature flag（import 時評価。停止時は env で 0 を明示セット） ──
ENABLE_VISUAL_SOLO_PLAY = os.environ.get("ENABLE_VISUAL_SOLO_PLAY", "1") == "1"

solitaire_bp = Blueprint("solitaire", __name__)

# ── リプレイ保存の専用レートリミット（同一IPからの連投による保存乱用対策） ──
# TODO: calibrate from data — 閾値は実データ未収集のため仮置き。
# 60秒10回（当初3回から緩和 2026-08-20）: 「共有リンクコピー」「X投稿」経路が
# 都度サーバー保存を行うため、通常の共有フローだけで3回を踏むことを実測確認したため。
REPLAY_RATE_LIMIT_WINDOW_SEC = 60
REPLAY_RATE_LIMIT_MAX_REQUESTS = 10
REPLAY_RATE_LIMIT_MAX_TRACKED_IPS = 10000  # 掃除用上限（他モジュールの慣例に合わせる）
_replay_rate_lock = threading.Lock()
_replay_rate_log: dict[str, list[float]] = {}


def _consume_replay_rate_limit():
    """リプレイ保存のIP単位レートリミット。通過時はNone、制限時は(dict, status)。"""
    ip = _client_ip()
    now = time.time()
    with _replay_rate_lock:
        timestamps = _replay_rate_log.setdefault(ip, [])
        timestamps[:] = [t for t in timestamps if now - t < REPLAY_RATE_LIMIT_WINDOW_SEC]
        if len(timestamps) >= REPLAY_RATE_LIMIT_MAX_REQUESTS:
            return {"error": "しばらく待ってから再度保存してください"}, 429
        timestamps.append(now)
        if len(_replay_rate_log) > REPLAY_RATE_LIMIT_MAX_TRACKED_IPS:
            # ウィンドウが完全に空になった（=直近アクセスがウィンドウ外）IPだけを掃除。
            # 旧実装の `if not v` は空リストになったキーは setdefault で即座に消えるため
            # 恒久的にヒットしないno-opだった。
            stale = [k for k, v in _replay_rate_log.items()
                     if not v or now - v[-1] >= REPLAY_RATE_LIMIT_WINDOW_SEC]
            for k in stale:
                del _replay_rate_log[k]
            # それでも上限超過なら、最終アクセスが古い順に強制削除する
            # （継続フラッド下ではstaleが1件も出ない可能性があるための保険）
            if len(_replay_rate_log) > REPLAY_RATE_LIMIT_MAX_TRACKED_IPS:
                overflow = len(_replay_rate_log) - REPLAY_RATE_LIMIT_MAX_TRACKED_IPS
                oldest_keys = sorted(
                    _replay_rate_log, key=lambda k: _replay_rate_log[k][-1]
                )[:overflow]
                for k in oldest_keys:
                    del _replay_rate_log[k]
    return None

# ── 公開一覧の閲覧・公開の取り下げ用レートリミット（保存とは別の枠） ──
# TODO: calibrate from data — 閾値は実データ未収集のため仮置き。
PUBLIC_LIST_RATE_LIMIT_WINDOW_SEC = 60
PUBLIC_LIST_RATE_LIMIT_MAX_REQUESTS = 30
UNPUBLISH_RATE_LIMIT_WINDOW_SEC = 60
UNPUBLISH_RATE_LIMIT_MAX_REQUESTS = 10
_public_list_rate_lock = threading.Lock()
_public_list_rate_log: dict[str, list[float]] = {}
_unpublish_rate_lock = threading.Lock()
_unpublish_rate_log: dict[str, list[float]] = {}
RENAME_RATE_LIMIT_WINDOW_SEC = 60
RENAME_RATE_LIMIT_MAX_REQUESTS = 10
_rename_rate_lock = threading.Lock()
_rename_rate_log: dict[str, list[float]] = {}


def _consume_ip_rate_limit(lock, log, window_sec, max_requests, message):
    """IP 単位のレートリミット（_consume_replay_rate_limit と同じ作りで、枠ごとに log を分ける）。
    通過時は None、制限時は (dict, 429)。"""
    ip = _client_ip()
    now = time.time()
    with lock:
        timestamps = log.setdefault(ip, [])
        timestamps[:] = [t for t in timestamps if now - t < window_sec]
        if len(timestamps) >= max_requests:
            return {"error": message}, 429
        timestamps.append(now)
        if len(log) > REPLAY_RATE_LIMIT_MAX_TRACKED_IPS:
            stale = [k for k, v in log.items() if not v or now - v[-1] >= window_sec]
            for k in stale:
                del log[k]
            if len(log) > REPLAY_RATE_LIMIT_MAX_TRACKED_IPS:
                overflow = len(log) - REPLAY_RATE_LIMIT_MAX_TRACKED_IPS
                for k in sorted(log, key=lambda k: log[k][-1])[:overflow]:
                    del log[k]
    return None


def _consume_public_list_rate_limit():
    return _consume_ip_rate_limit(
        _public_list_rate_lock, _public_list_rate_log,
        PUBLIC_LIST_RATE_LIMIT_WINDOW_SEC, PUBLIC_LIST_RATE_LIMIT_MAX_REQUESTS,
        "しばらく待ってから再度読み込んでください")


def _consume_unpublish_rate_limit():
    return _consume_ip_rate_limit(
        _unpublish_rate_lock, _unpublish_rate_log,
        UNPUBLISH_RATE_LIMIT_WINDOW_SEC, UNPUBLISH_RATE_LIMIT_MAX_REQUESTS,
        "しばらく待ってから再度お試しください")


def _consume_rename_rate_limit():
    return _consume_ip_rate_limit(
        _rename_rate_lock, _rename_rate_log,
        RENAME_RATE_LIMIT_WINDOW_SEC, RENAME_RATE_LIMIT_MAX_REQUESTS,
        "しばらく待ってから再度お試しください")


# app.py から注入される Supabase クライアントアクセサ（引数なし → client or None）
_get_supabase = lambda: None


def init_solitaire(get_supabase):
    """app.py 起動時に一度だけ呼ぶ。Supabase クライアントのアクセサを登録する。

    例:
        from solitaire_routes import solitaire_bp, init_solitaire
        init_solitaire(lambda: _supabase_client)
        app.register_blueprint(solitaire_bp)
    """
    global _get_supabase
    _get_supabase = get_supabase


# ── 一人回しシミュレータ ──

REPLAY_DEFAULT_TITLE = "共有リプレイ"


def _lookup_replay_og_meta(replay_id):
    """共有リプレイの (title, board) を DB から引く。

    戻り値: ("found", title, board_or_None) / ("missing",) / ("unknown",)
    unknown は DB 未接続・取得失敗（ページ側は一過性の失敗を固定しないよう、生成ルートに任せる）。
    """
    sb = _get_supabase()
    if not sb:
        return ("unknown",)
    try:
        resp = (sb.table("solitaire_replays")
                .select("title, board, hidden")
                .eq("id", replay_id)
                .limit(1)
                .execute())
    except Exception as e:
        logger.warning(f"リプレイのOGP情報取得エラー: {e}")
        return ("unknown",)
    rows = resp.data if resp.data else []
    if not rows:
        return ("missing",)
    if rows[0].get("hidden"):
        # 運営が非表示にしたもの（悪質な投稿への対処）は「無いもの」として扱う（OGメタ情報・画像とも汎用）
        return ("missing",)
    return ("found", (rows[0].get("title") or "").strip(), rows[0].get("board"))


# ── 一人回しシミュレータ ──

@solitaire_bp.route("/solitaire")
def solitaire_page():
    """一人回しシミュレータ。?replay=<id> 付きなら、リンク画像（og:image）をそのリプレイの盤面画像にする。

    og:title のためにリプレイ名を DB から1件引く（主キー検索・数十バイトのため許容。
    取得失敗時は既定の題名のまま続行する）。#replay=（ハッシュ方式）はサーバーから見えないので汎用画像のまま。
    """
    if not ENABLE_VISUAL_SOLO_PLAY:
        return render_template("solo_disabled.html"), 503
    og_image_path = None   # None = 汎用画像（static/og-solitaire.png）
    og_title = None
    og_url_path = None     # None = /solitaire（?replay= 付きのときはそのURL）
    replay_id = request.args.get("replay", "")
    if replay_og_image.is_valid_replay_id(replay_id):
        og_url_path = f"/solitaire?replay={replay_id}"
        meta = _lookup_replay_og_meta(replay_id)
        _remember_og_meta(replay_id, meta[0])   # 直後に X のクローラーが読む絵のために、聞き直さずに済むよう覚える
        if meta[0] == "found":
            og_title = meta[1] or REPLAY_DEFAULT_TITLE
            if validate_board(meta[2]):
                og_image_path = f"/solitaire/og/{replay_id}.png"
        elif meta[0] == "unknown":
            og_image_path = f"/solitaire/og/{replay_id}.png"   # 生成ルート側で board 無し等なら汎用画像を返す
    og_title_full = None   # 指定時は og:title / twitter:title をそのまま使う（カード用）
    if not og_image_path and request.args.get("newcards") == "1":
        card_meta = _lookup_focus_card_og(request.args.get("focus", ""))
        if card_meta:
            og_image_path, og_title_full = card_meta
    return render_template("solitaire.html", og_image_path=og_image_path, og_title=og_title,
                           og_url_path=og_url_path, og_title_full=og_title_full)


def _lookup_focus_card_og(focus):
    """?newcards=1&focus=<カード名> のリンク画像・題名。focus が公開中の未発売カード（画像あり）なら
    (og_image_path, og_title)、そうでなければ None（汎用のまま）。失敗しても例外は投げない。"""
    focus = (focus or "").strip()
    if not focus or len(focus) > 200:
        return None
    try:
        rec = card_display.get_unreleased_record(focus)
        sb = _get_supabase()
        if not rec or not sb:
            return None
        card = card_og_image.fetch_public_card(sb, rec["id"])
        if not card:
            return None
        blur = card_display.is_setting_enabled(card_og_image.SETTING_KEY, True)
        return card_og_image.og_image_path(card, blur), f"{card['name']}を一人回しで試す｜強欲なラボ"
    except Exception as e:
        logger.warning(f"カードのOGP情報取得エラー: {e}")
        return None


@solitaire_bp.route("/solitaire/replays")
def solitaire_replays_page():
    """リプレイ一覧ページ。このブラウザ（localStorage）に残した端末保存・共有履歴を並べる。

    一覧の中身はすべてブラウザ側（static/solitaire/js/replays-page.js）で組み立てるため、
    サーバーは枠だけを返す（ユーザーごとの情報は持たない）。
    """
    if not ENABLE_VISUAL_SOLO_PLAY:
        return render_template("solo_disabled.html"), 503
    return render_template("solitaire_replays.html", public_first_count=_public_first_count())


# 「みんなのリプレイ」の最初のページの件数（読み込み中の仮の枠の数に使う）。ページを開くたびに DB を引かないよう覚えておく。
# 以前は前回の訪問の件数（初めては1枚）で枠を取り、実際の件数との差で下の欄が押し下がっていた（2026-10-21 PC で CLS 0.004・スマホ 0.009）。
# 公開・取り下げ・非表示の直後は最大この秒数だけずれる（ずれても枠の数が合わないだけ）
_PUBLIC_COUNT_TTL_SEC = 120
_public_count_cache = {"at": 0.0, "value": None}


def _public_first_count():
    """最初のページ（PUBLIC_LIST_DEFAULT_LIMIT 件まで）に並ぶ公開リプレイの件数。取れなければ None（テンプレートは前回の件数を使う）。"""
    now = time.time()
    if _public_count_cache["value"] is not None and now - _public_count_cache["at"] < _PUBLIC_COUNT_TTL_SEC:
        return _public_count_cache["value"]
    sb = _get_supabase()
    if not sb:
        return None
    try:
        rows = (sb.table("solitaire_replays")
                .select("id")
                .eq("is_public", True)
                .eq("hidden", False)
                .limit(PUBLIC_LIST_DEFAULT_LIMIT)
                .execute().data or [])
    except Exception as e:
        logger.warning(f"公開リプレイの件数の取得エラー: {e}")
        return None
    _public_count_cache.update(at=now, value=len(rows))
    return len(rows)


@solitaire_bp.route("/solitaire/og/<replay_id>.png")
def solitaire_og_image(replay_id):
    """共有リプレイのリンク画像（1200x630）。board があれば盤面画像、無ければ汎用画像。"""
    if not ENABLE_VISUAL_SOLO_PLAY:
        abort(404)
    if not replay_og_image.is_valid_replay_id(replay_id):
        abort(404)

    hit = replay_og_image.read_any_cached(replay_id)
    if hit is not None:
        if _replay_og_alive(replay_id) is False:
            # 非表示・削除・90日の自動削除で無くなったリプレイの絵は、キャッシュに残っていても返さない
            forget_replay_og(replay_id)
            return _generic_og_response(300)
        png, degraded = hit
        # 画像取得に失敗して不完全だった描画（メモリの短期キャッシュ）は、ブラウザ側の期限も短くする
        return _og_png_response(png, 60 if degraded else 86400)

    meta = _lookup_replay_og_meta(replay_id)
    _remember_og_meta(replay_id, meta[0])
    board = validate_board(meta[2]) if meta[0] == "found" else None
    if board is None:
        # 旧リプレイ（board 無し）・存在しない id・DB 失敗: 汎用画像。一過性の失敗を長く固定しないため短い期限にする
        return _generic_og_response(86400 if meta[0] == "found" else 300)

    try:
        png, degraded = replay_og_image.get_or_render(replay_id, board, meta[1])
    except replay_og_image.RenderBusy:
        return _generic_og_response(60)   # 描画枠が空かない・描画の予算超過: 待たずに汎用画像
    except Exception as e:
        logger.error(f"リプレイ画像の生成エラー {replay_id}: {e}")
        return _generic_og_response(60)
    # カード画像が一部取れなかった描画は期限を短くする（次の取得で復旧できるように）
    return _og_png_response(png, 60 if degraded else 86400)


# リプレイ一覧の絵（縮小版）。リンク画像（1200x630 の PNG・約225KB）をそのまま一覧に出すと、
# スマホ相当でリプレイ一覧の LCP が 4〜8秒になっていた（2026-10-22）。幅 640px の WebP にして返す。
# 縮めるのは CPU を使うので、期限の長い（完成した）画像の縮小版だけメモリに覚える
_OG_THUMB_WIDTH = 640
_OG_THUMB_MAX_ENTRIES = 128   # 1枚 数十KB。TODO: calibrate from data（一覧の閲覧数を見て）
_og_thumb_cache: "OrderedDict[str, bytes]" = OrderedDict()
_og_thumb_lock = threading.Lock()


# キャッシュした絵を返す前に、そのリプレイが今もあるか（非表示・削除・90日の自動削除）を確かめた結果を覚える。
# 削除は管理画面（別の worker のこともある）と collect_prices.py（別のプロセス）で起きるので、キャッシュを消す合図が
# 届かない worker でも、この期限が切れれば汎用画像に変わる。一覧は1回で24枚読むため、毎回 DB には聞かない
# （みんなのリプレイの一覧・リプレイのページで引いた結果も覚えるので、普段は一覧の絵のための問い合わせは起きない）
_OG_ALIVE_TTL_SEC = 300          # TODO: calibrate from data（削除してから絵が消えるまでの最長の待ち）
_OG_ALIVE_UNKNOWN_TTL_SEC = 60   # DB に聞けなかったとき「ある」とみなして聞き直さない時間。TODO: calibrate from data
_OG_ALIVE_MAX_ENTRIES = 1024     # TODO: calibrate from data
_og_alive_cache: "OrderedDict[str, tuple]" = OrderedDict()   # id -> (期限の時刻, あるか)
_og_alive_lock = threading.Lock()


def _remember_og_alive(replay_id, alive, ttl=_OG_ALIVE_TTL_SEC):
    with _og_alive_lock:
        _og_alive_cache[replay_id] = (time.monotonic() + ttl, alive)
        _og_alive_cache.move_to_end(replay_id)
        while len(_og_alive_cache) > _OG_ALIVE_MAX_ENTRIES:
            _og_alive_cache.popitem(last=False)


def _remember_og_meta(replay_id, status):
    """_lookup_replay_og_meta の結果（found / missing / unknown）を覚える。unknown は覚えない（すぐ聞き直してよい）。"""
    if status in ("found", "missing"):
        _remember_og_alive(replay_id, status == "found")


def _lookup_replay_alive(replay_id):
    """行があって非表示でないか（True / False）。DB に聞けなければ None。board は読まない（重い列のため）。"""
    sb = _get_supabase()
    if not sb:
        return None
    try:
        rows = (sb.table("solitaire_replays")
                .select("id, hidden")
                .eq("id", replay_id)
                .limit(1)
                .execute()).data or []
    except Exception as e:
        logger.warning(f"リプレイの有無の確認エラー: {e}")
        return None
    return bool(rows) and not rows[0].get("hidden")


def _replay_og_alive(replay_id):
    """そのリプレイが今もあるか。True=ある / False=無い（非表示を含む） / None=DB に聞けなかった。
    聞けなかったときは「ある」とみなして短い間覚え、DB が遅い・止まっている間にキャッシュ済みの絵まで DB 待ちにしない。"""
    with _og_alive_lock:
        hit = _og_alive_cache.get(replay_id)
    if hit is not None and time.monotonic() < hit[0]:
        return hit[1]
    alive = _lookup_replay_alive(replay_id)
    if alive is None:
        _remember_og_alive(replay_id, True, _OG_ALIVE_UNKNOWN_TTL_SEC)
        return None
    _remember_og_alive(replay_id, alive)
    return alive


def forget_replay_og(replay_id):
    """そのリプレイの絵のキャッシュ（ディスク・描画失敗用・一覧の縮小版）と、ある・無いの記憶を消す。
    管理画面で非表示・削除したときと、無くなったリプレイの絵を見つけたときに呼ぶ。"""
    replay_og_image.purge_cache(replay_id)
    with _og_thumb_lock:
        _og_thumb_cache.pop(replay_id, None)
    with _og_alive_lock:
        _og_alive_cache.pop(replay_id, None)


@solitaire_bp.route("/solitaire/og/thumb/<replay_id>.webp")
def solitaire_og_thumb(replay_id):
    """リプレイ一覧に並べる絵の縮小版（幅 640px の WebP）。中身は /solitaire/og/<id>.png と同じ絵。"""
    with _og_thumb_lock:
        hit = _og_thumb_cache.get(replay_id)
        if hit is not None:
            _og_thumb_cache.move_to_end(replay_id)
    if hit is not None:
        if _replay_og_alive(replay_id) is not False:
            return Response(hit, mimetype="image/webp", headers={"Cache-Control": "public, max-age=86400"})
        forget_replay_og(replay_id)   # 無くなったリプレイ: 下の solitaire_og_image が汎用画像を返す
    full = solitaire_og_image(replay_id)   # 404 は abort で抜ける
    full.direct_passthrough = False
    max_age = full.cache_control.max_age or 60
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(full.get_data()))
        img = img.convert("RGB")
        height = round(img.height * _OG_THUMB_WIDTH / img.width)
        out = io.BytesIO()
        img.resize((_OG_THUMB_WIDTH, height), Image.LANCZOS).save(out, "WEBP", quality=80, method=4)
        webp = out.getvalue()
    except Exception as e:
        logger.warning(f"リプレイ一覧の絵の縮小に失敗 {replay_id}: {e}")
        return full   # 縮められなければ原寸の PNG をそのまま返す
    if max_age >= 86400:
        with _og_thumb_lock:
            _og_thumb_cache[replay_id] = webp
            while len(_og_thumb_cache) > _OG_THUMB_MAX_ENTRIES:
                _og_thumb_cache.popitem(last=False)
    return Response(webp, mimetype="image/webp", headers={"Cache-Control": f"public, max-age={max_age}"})


@solitaire_bp.route("/solitaire/og/card/<int:card_id>.png")
def solitaire_card_og_image(card_id):
    """未発売カードのリンク画像（1200x630）。公開中のカードだけ。それ以外・失敗時は汎用画像。

    カード画像から作った画像には X-OG-Kind: card を付ける（X 投稿前の生成確認が、汎用画像への退避と見分けるため）。
    URL の ?v= はページ側が付ける版キー（編集・画像差し替え・設定切替で変わる）。付いていれば長めに、無ければ短めにキャッシュさせる。
    """
    if not ENABLE_VISUAL_SOLO_PLAY:
        abort(404)
    sb = _get_supabase()
    if not sb:
        return _generic_og_response(60)
    try:
        card = card_og_image.fetch_public_card(sb, card_id)
    except Exception as e:
        logger.warning(f"カード画像の元情報取得エラー card_id={card_id}: {e}")
        return _generic_og_response(60)
    if card is None:
        return _generic_og_response(300)   # 非公開・存在しない・画像なし
    blur = card_display.is_setting_enabled(card_og_image.SETTING_KEY, True)
    try:
        png = card_og_image.get_or_render(sb, card, blur)
    except replay_og_image.RenderBusy:
        return _generic_og_response(60)
    except Exception as e:
        logger.error(f"カードのリンク画像の生成エラー card_id={card_id}: {e}")
        return _generic_og_response(60)
    resp = _og_png_response(png, 86400 if request.args.get("v") else 3600)
    resp.headers["X-OG-Kind"] = "card"
    return resp


def _og_png_response(png, max_age):
    return Response(png, mimetype="image/png", headers={"Cache-Control": f"public, max-age={max_age}"})


def _generic_og_response(max_age):
    return send_file(replay_og_image.GENERIC_IMAGE_PATH, mimetype="image/png", max_age=max_age)


# TODO: calibrate from data — 上限値（logs件数・本文サイズ）は実データ未収集のため仮置き。
REPLAY_MAX_LOG_ENTRIES = 2000
REPLAY_MAX_BODY_BYTES = 512 * 1024


_CARD_TYPES = ("monster", "spell", "trap")

# 公開（みんなのリプレイ）の上限。TODO: calibrate from data — 仮置き
PUBLIC_TITLE_MAX_LEN = 40
PUBLIC_CARD_NAMES_MAX = 80
PUBLIC_CARD_NAME_MAX_LEN = 60
PUBLIC_LIST_DEFAULT_LIMIT = 24
PUBLIC_LIST_MAX_LIMIT = 48
PUBLIC_LIST_CARD_NAMES_SHOWN = 6   # 一覧で返すカード名の件数（総数は card_count）
_PUBLIC_CARD_QUERY_MAX_LEN = 100

# before（published_at の続き読み位置）の形式。PostgREST のフィルタに文字列のまま渡すため、形を確かめてから使う
_BEFORE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}(:?\d{2})?)?$")


def _pg_array_literal(value: str) -> str:
    """text[] の1要素を PostgreSQL の配列リテラル '{"…"}' にする。postgrest-py は Python の list を
    引用符なしの {a,b} に組み立てるため、カンマ・引用符・{}・バックスラッシュ・NULL を含む名前が誤動作する。
    バックスラッシュと " をエスケープして引用符で囲んだ文字列を、そのまま渡す。"""
    escaped = value.replace(chr(92), chr(92) * 2).replace('"', chr(92) + '"')
    return '{"' + escaped + '"}'


def _hash_delete_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _build_card_names(names, images):
    """names の値から公開用のカード名一覧を作る。トークン（画像が data:）と重複を除き、
    1件は PUBLIC_CARD_NAME_MAX_LEN 文字までで切り、PUBLIC_CARD_NAMES_MAX 件まで。
    禁止語を含む名前は外す。"""
    out, seen = [], set()
    for card_id, name in names.items():
        if not isinstance(name, str):
            continue
        img = images.get(card_id)
        if isinstance(img, str) and img.startswith("data:"):
            continue
        name = name.strip()[:PUBLIC_CARD_NAME_MAX_LEN]
        if not name or name in seen:
            continue
        # カード名も利用者が送る値なので、タイトルと同じ禁止語チェックを掛ける。当たった名前は外す（保存は断らない）
        if replay_moderation.contains_ng_word(name):
            continue
        seen.add(name)
        out.append(name)
        if len(out) >= PUBLIC_CARD_NAMES_MAX:
            break
    return out


# トークンの画像（data:）1件の上限。今のトークンは約10KB（token-image.js）。TODO: calibrate from data
REPLAY_TOKEN_IMAGE_MAX_CHARS = 64 * 1024
_TOKEN_IMAGE_RE = re.compile(r"data:image/(?:jpeg|png);base64,[A-Za-z0-9+/]+={0,2}")
_TOKEN_PLACEHOLDER = "data:image/png;base64,"   # 中身の無いトークンの印（isTokenSrc は data: で始まるかだけを見る）
_CARD_SENTINEL_MAX_LEN = 8 + PUBLIC_CARD_NAME_MAX_LEN * 2   # "card://" ＋ カード名


def _clean_images(images, names):
    """画像辞書（{cardId: src}）を、保存してよい値だけにする。
    通すのは card://カード名・トークンの data: 画像（形と大きさを確かめる）・REPLAY_IMAGE_SOURCES の https URL。
    形の違う data: は中身の無いトークンの印（_TOKEN_PLACEHOLDER）に置き換える（トークンのまま扱われ、画面が名前から描き直す）。
    それ以外（任意の外部URL・文字列でない値）は card://カード名 に置き換える。
    画面側（replay-image-policy.js）も同じ規則で読み直すが、DB に任意の値を溜めないためにここでも絞る。
    戻り値: (直した辞書, 置き換えた件数)"""
    out, replaced = {}, 0
    for card_id, src in images.items():
        if not isinstance(card_id, str):
            replaced += 1
            continue
        if isinstance(src, str):
            if src.startswith("card://") and len(src) <= _CARD_SENTINEL_MAX_LEN:
                out[card_id] = src
                continue
            if src.startswith("data:"):
                if len(src) <= REPLAY_TOKEN_IMAGE_MAX_CHARS and _TOKEN_IMAGE_RE.fullmatch(src):
                    out[card_id] = src
                else:
                    # card:// にするとトークンでなくなり、トークンの削除の手が効かず再生の盤面がずれる
                    out[card_id] = _TOKEN_PLACEHOLDER
                    replaced += 1
                continue
            elif _is_allowed_image_url(src):
                out[card_id] = src
                continue
        name = names.get(card_id) if isinstance(names, dict) else None
        name = name.strip()[:PUBLIC_CARD_NAME_MAX_LEN * 2] if isinstance(name, str) else ""
        out[card_id] = f"card://{name}"
        replaced += 1
    return out, replaced


_REPLAY_IMAGE_SOURCES = tuple((origin, re.compile(pat)) for origin, pat in REPLAY_IMAGE_SOURCES)


def _is_allowed_image_url(src: str) -> bool:
    """REPLAY_IMAGE_SOURCES の発行元で、パスが出している形の https URL か（クエリ・#・認証情報つきは通さない）。"""
    try:
        u = urlsplit(src)
    except ValueError:
        return False
    if u.query or u.fragment or "@" in u.netloc:
        return False
    origin = f"{u.scheme}://{u.netloc}"
    return any(origin == o and pat.fullmatch(u.path) for o, pat in _REPLAY_IMAGE_SOURCES)


def _count_steps(logs):
    """手数（setup:true の初期配置ログを除いた数）"""
    return sum(1 for ev in logs if not (isinstance(ev, dict) and ev.get("setup")))


def _clean_card_types(card_types, images):
    """cardTypes（{cardId: 'monster'|'spell'|'trap'}）を検証して返す。
    images に無いカードID・不正な種類の値は捨てる（任意の値をDBに溜めないため）。"""
    if not isinstance(card_types, dict):
        return {}
    return {
        k: v for k, v in card_types.items()
        if isinstance(k, str) and k in images and v in _CARD_TYPES
    }


@solitaire_bp.route("/api/solitaire/replay", methods=["POST"])
def solitaire_replay_save():
    """リプレイデータをSupabaseに保存してIDを返す"""
    if not ENABLE_VISUAL_SOLO_PLAY:
        abort(404)

    rate_error = _consume_replay_rate_limit()
    if rate_error:
        return rate_error

    # リクエスト本文サイズの上限チェック（JSONパース前に生バイト長で判定）
    if len(request.get_data()) > REPLAY_MAX_BODY_BYTES:
        return {"error": "リクエストサイズが大きすぎます"}, 413

    _supabase_client = _get_supabase()
    if not _supabase_client:
        return {"error": "データベース未接続"}, 503
    try:
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            return {"error": "不正なリクエストです"}, 400
        images = data.get("images", {})
        names  = data.get("names", {})
        ex_card_ids = data.get("exCardIds", [])
        card_types_in = data.get("cardTypes")   # 省略可（旧クライアントは送らない）
        logs = data.get("logs", [])
        raw_title = data.get("title")
        title = raw_title[:100] if isinstance(raw_title, str) else ""   # 文字列でなければ空（None が「None」にならないように）
        is_public = data.get("public") is True
        # 型チェック: 期待型でなければ拒否
        if not isinstance(logs, list) or not isinstance(images, dict) \
                or not isinstance(names, dict) or not isinstance(ex_card_ids, list):
            return {"error": "不正なリクエストです"}, 400
        if card_types_in is not None and not isinstance(card_types_in, dict):
            return {"error": "不正なリクエストです"}, 400
        if not logs:
            return {"error": "logsが空です"}, 400
        if len(logs) > REPLAY_MAX_LOG_ENTRIES:
            return {"error": "logsが多すぎます"}, 400
        images, replaced = _clean_images(images, names)
        if replaced:
            logger.warning(f"リプレイ保存: 保存できない画像の指定 {replaced} 件を card:// に置き換えました")
        # board（リンク画像用の最後の盤面）。不備があっても共有自体は失敗させない（捨てて保存する）
        if is_public:
            # 公開はタイトル必須（誰でも見られる一覧に載るため）。不適切な語は断る
            title = replay_moderation.sanitize_title(title)   # 改行・制御文字・文字の向きを変える制御文字を除く
            if not title:
                return {"error": "公開するにはタイトルを入れてください"}, 400
            if len(title) > PUBLIC_TITLE_MAX_LEN:
                return {"error": f"公開するタイトルは{PUBLIC_TITLE_MAX_LEN}文字以内にしてください"}, 400
            try:
                if replay_moderation.contains_ng_word(title):
                    return {"error": "タイトルに使えない言葉が含まれています"}, 400
            except replay_moderation.NgListUnavailable:
                # 禁止語リストが使えないときは公開だけを断る（普通の共有は通す）。根本対応はリストの配置の修正
                logger.error("禁止語リストが使えないため、公開を断りました（data/replay_ng_words.txt を確認）")
                return {"error": "現在公開できません"}, 503
        board_in = data.get("board")
        board = validate_board(board_in)
        if board_in is not None and board is None:
            logger.warning("リプレイ保存: board が不正のため破棄して保存を続行")
        replay_id = secrets.token_urlsafe(8)
        now_iso = datetime.now(timezone.utc).isoformat()
        payload = {
            "id": replay_id,
            "title": title,
            "images": images,
            "names": names,
            "ex_card_ids": ex_card_ids,
            "logs": logs,
            "created_at": now_iso,
        }
        if board:
            payload["board"] = board
        card_types = _clean_card_types(card_types_in, images)
        if card_types:
            payload["card_types"] = card_types
        delete_token = None
        if is_public:
            # 鍵はここで一度だけ返し、DB にはハッシュだけを残す（取り下げは鍵を持つブラウザだけ）
            delete_token = secrets.token_urlsafe(24)
            payload.update({
                "is_public": True,
                "published_at": now_iso,
                "card_names": _build_card_names(names, images),
                "steps": _count_steps(logs),
                "delete_token_hash": _hash_delete_token(delete_token),
            })
        _supabase_client.table("solitaire_replays").insert(payload).execute()
        if board:
            # X のクローラーは待ち時間が短いので、投稿される前に画像を先に描いてキャッシュを温める
            replay_og_image.warm_cache_async(replay_id, board, title)
        if delete_token:
            return {"id": replay_id, "deleteToken": delete_token}
        return {"id": replay_id}
    except Exception as e:
        logger.error(f"リプレイ保存エラー: {e}")
        return {"error": "保存に失敗しました"}, 500


@solitaire_bp.route("/api/solitaire/replay/<replay_id>", methods=["GET"])
def solitaire_replay_get(replay_id):
    """Supabaseからリプレイデータを取得"""
    if not ENABLE_VISUAL_SOLO_PLAY:
        abort(404)
    _supabase_client = _get_supabase()
    if not _supabase_client:
        return {"error": "データベース未接続"}, 503
    try:
        resp = _supabase_client.table("solitaire_replays") \
            .select("images, names, ex_card_ids, logs, title, card_types, hidden") \
            .eq("id", replay_id) \
            .limit(1) \
            .execute()
        rows = resp.data if resp.data else []
        if not rows or rows[0].get("hidden"):
            return {"error": "見つかりません"}, 404   # 非表示（運営の対処）は無いものとして扱う
        row = rows[0]
        return {
            "images":     row["images"],
            "names":      row.get("names") or {},
            "exCardIds":  row.get("ex_card_ids") or [],
            "cardTypes":  row.get("card_types") or {},
            "logs":       row["logs"],
            "title":      row.get("title", ""),
        }
    except Exception as e:
        logger.error(f"リプレイ取得エラー: {e}")
        return {"error": "取得に失敗しました"}, 500


@solitaire_bp.route("/api/solitaire/public", methods=["GET"])
def solitaire_public_list():
    """みんなのリプレイの一覧（公開かつ非表示でないもの。published_at の新しい順）。

    ?card=<カード名>（完全一致）・?before=<published_at>（これより古いものを返す＝続き読み）・?limit=24（上限48）。
    戻り値: {"items": [{id, title, card_names(先頭6件), card_count, steps, published_at}],
             "next": 続きを読むときの before、無ければ null}
    """
    if not ENABLE_VISUAL_SOLO_PLAY:
        abort(404)
    rate_error = _consume_public_list_rate_limit()
    if rate_error:
        return rate_error
    try:
        limit = int(request.args.get("limit", PUBLIC_LIST_DEFAULT_LIMIT))
    except ValueError:
        return {"error": "limit が不正です"}, 400
    limit = max(1, min(limit, PUBLIC_LIST_MAX_LIMIT))
    card = request.args.get("card", "").strip()
    if len(card) > _PUBLIC_CARD_QUERY_MAX_LEN:
        return {"error": "card が長すぎます"}, 400
    # クエリ中の + が空白に化けた場合の復元
    before = request.args.get("before", "").strip().replace(" ", "+")
    if before and not _BEFORE_RE.match(before):
        return {"error": "before が不正です"}, 400

    sb = _get_supabase()
    if not sb:
        return {"error": "データベース未接続"}, 503
    try:
        q = (sb.table("solitaire_replays")
             .select("id, title, card_names, steps, published_at")
             .eq("is_public", True)
             .eq("hidden", False))
        if card:
            q = q.contains("card_names", _pg_array_literal(card))
        if before:
            q = q.lt("published_at", before)
        # 続きがあるかを知るため1件多く読む
        rows = q.order("published_at", desc=True).limit(limit + 1).execute().data or []
    except Exception as e:
        logger.error(f"公開リプレイ一覧の取得エラー: {e}")
        return {"error": "取得に失敗しました"}, 500
    has_more = len(rows) > limit
    items = []
    for r in rows[:limit]:
        _remember_og_alive(r.get("id"), True)   # 公開かつ非表示でない行。一覧の絵（縮小版）が聞き直さずに済む
        names = r.get("card_names") or []
        items.append({
            "id": r.get("id"),
            "title": r.get("title") or "",
            "card_names": names[:PUBLIC_LIST_CARD_NAMES_SHOWN],
            "card_count": len(names),
            "steps": r.get("steps") or 0,
            "published_at": r.get("published_at"),
        })
    return {"items": items, "next": items[-1]["published_at"] if has_more and items else None}


# 取り下げできない（鍵違い・id なし・公開でない）ときの応答。クライアントは code のあるときだけ手元の鍵を捨てる
# （プロキシ等の別の 403 で鍵を失わないため）
_NOT_UNPUBLISHABLE = ({"error": "取り下げできません", "code": "not_unpublishable"}, 403)


@solitaire_bp.route("/api/solitaire/replay/<replay_id>/unpublish", methods=["POST"])
def solitaire_replay_unpublish(replay_id):
    """公開の取り下げ。本文 {token}（公開時に返した deleteToken）。鍵のハッシュが一致したときだけ、
    is_public=false・published_at=null・delete_token_hash=null にする（行は消さない。90日ルールに戻る）。"""
    if not ENABLE_VISUAL_SOLO_PLAY:
        abort(404)
    rate_error = _consume_unpublish_rate_limit()
    if rate_error:
        return rate_error
    data = request.get_json(silent=True)
    token = data.get("token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token or len(token) > 200:
        return _NOT_UNPUBLISHABLE
    sb = _get_supabase()
    if not sb:
        return {"error": "データベース未接続"}, 503
    try:
        resp = (sb.table("solitaire_replays")
                .select("delete_token_hash")
                .eq("id", replay_id)
                .eq("is_public", True)
                .limit(1)
                .execute())
        rows = resp.data if resp.data else []
        stored = (rows[0].get("delete_token_hash") if rows else None) or ""
        # id が無い・公開でない・鍵が違うは同じ応答にする（id の有無を推測させない）
        if not stored or not hmac.compare_digest(_hash_delete_token(token), stored):
            return _NOT_UNPUBLISHABLE
        (sb.table("solitaire_replays")
         .update({"is_public": False, "published_at": None, "delete_token_hash": None})
         .eq("id", replay_id)
         .execute())
        return {"ok": True}
    except Exception as e:
        logger.error(f"公開の取り下げエラー {replay_id}: {e}")
        return {"error": "取り下げに失敗しました"}, 500


# 名前を変えられない（鍵違い・id なし・公開でない）ときの応答。取り下げと同じく、code のあるときだけクライアントは鍵を捨てる
_NOT_RENAMABLE = ({"error": "名前を変えられません", "code": "not_renamable"}, 403)


@solitaire_bp.route("/api/solitaire/replay/<replay_id>/rename", methods=["POST"])
def solitaire_replay_rename(replay_id):
    """公開中のリプレイの名前を変える。本文 {token, title}（token は公開時に返した deleteToken）。
    鍵のハッシュが一致したときだけ title を書き換える。タイトルの条件は公開するときと同じ
    （整形・必須・PUBLIC_TITLE_MAX_LEN 文字以内・禁止語）。
    題名はリンク画像にも描かれているので、画像のキャッシュを消して新しい題名で描き直す。"""
    if not ENABLE_VISUAL_SOLO_PLAY:
        abort(404)
    rate_error = _consume_rename_rate_limit()
    if rate_error:
        return rate_error
    data = request.get_json(silent=True)
    token = data.get("token") if isinstance(data, dict) else None
    raw_title = data.get("title") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token or len(token) > 200:
        return _NOT_RENAMABLE
    title = replay_moderation.sanitize_title(raw_title if isinstance(raw_title, str) else "")
    if not title:
        return {"error": "名前を入れてください"}, 400
    if len(title) > PUBLIC_TITLE_MAX_LEN:
        return {"error": f"名前は{PUBLIC_TITLE_MAX_LEN}文字以内にしてください"}, 400
    try:
        if replay_moderation.contains_ng_word(title):
            return {"error": "名前に使えない言葉が含まれています"}, 400
    except replay_moderation.NgListUnavailable:
        logger.error("禁止語リストが使えないため、名前の変更を断りました（data/replay_ng_words.txt を確認）")
        return {"error": "現在名前を変えられません"}, 503
    sb = _get_supabase()
    if not sb:
        return {"error": "データベース未接続"}, 503
    try:
        resp = (sb.table("solitaire_replays")
                .select("delete_token_hash, board")
                .eq("id", replay_id)
                .eq("is_public", True)
                .limit(1)
                .execute())
        rows = resp.data if resp.data else []
        stored = (rows[0].get("delete_token_hash") if rows else None) or ""
        # id が無い・公開でない・鍵が違うは同じ応答にする（id の有無を推測させない）
        if not stored or not hmac.compare_digest(_hash_delete_token(token), stored):
            return _NOT_RENAMABLE
        (sb.table("solitaire_replays")
         .update({"title": title})
         .eq("id", replay_id)
         .execute())
    except Exception as e:
        logger.error(f"リプレイの名前変更エラー {replay_id}: {e}")
        return {"error": "名前を変えられませんでした"}, 500
    # 古い題名入りの画像（原寸のファイルと一覧用の縮小版）を消して描き直す。
    # ブラウザが1日覚えている画像と、X などが取り込み済みの画像はこちらから消せない（一覧ページは ?v= で取り直す）
    replay_og_image.purge_cache(replay_id)
    with _og_thumb_lock:
        _og_thumb_cache.pop(replay_id, None)
    board = validate_board(rows[0].get("board"))
    if board:
        replay_og_image.warm_cache_async(replay_id, board, title)
    return {"ok": True, "title": title}
