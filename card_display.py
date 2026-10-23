"""
card_display.py — 未発売カード表示解決の唯一の入口

優先順位:
  (1) 発売済み（ygoresources 名前インデックスにヒット）
        → {"kind": "image", "url": <ygores/kanabell URL>}
  (2) 未発売 status IN ('approved','linked') + 公式画像あり
        + app_settings.OFFICIAL_IMAGE_DISPLAY enabled
        + unreleased_cards.hidden=false
        → {"kind": "image", "url": <Storage public_url>}
  (3) 未発売 approved/linked（画像なし or 設定無効）
        → {"kind": "proxy", "proxy": {カードデータ dict}}
  (4) TCG先行（tcg_preview_cards の英語名。status='active' かつ hidden=false）
        画像あり + OFFICIAL_IMAGE_DISPLAY enabled → {"kind": "image", "url": ..., "tcg_preview": True}
        それ以外                                   → {"kind": "proxy", "proxy": {..., "tcg_preview": True}}
  (5) それ以外（pending/rejected/不明）
        → {"kind": "none"}

Supabase 環境変数（SUPABASE_URL/SUPABASE_KEY）が未設定の場合は
未発売機能を無効として (1)/(5) のみで動作する。
既存挙動は完全に保持される。

循環 import 防止:
  app.py 側の _ygores_image_url / kanabell_card_image_url は
  register_released_resolver() で注入する関数注入方式をとる。
  card_display.py は app.py を import しない。

キャッシュ:
  app_settings・unreleased_cards・tcg_preview_cards の辞書はモジュール内メモリに TTL 30 秒で保持。
  TTL を過ぎても _CACHE_STALE_MAX 秒以内なら古い値を即返し、裏スレッドで取り直す
  （stale-while-revalidate。取り直しは実測1.9秒かかり、/api/new-cards が待たされていたため）。
  invalidate_cache() を呼ぶと即時クリアされる（P2 管理画面トグルAPIから使用）。
  クリア後の最初の取得は同期で行うため、管理操作の結果は古い値で隠れない。
"""

import os
import time
import logging
import threading
from typing import Callable
from datetime import datetime, date, timezone
from constants import JST

from name_normalize import fuzzy_key  # 記号・ハイフン種のゆれ吸収（app.py/reconcile と同一）

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 関数注入（循環 import 回避）
# ──────────────────────────────────────────────

# app.py 側から register_released_resolver() で登録される。
# 引数: カード名(str) → URL文字列 or None
_released_resolver: Callable[[str], str | None] | None = None


# 発売済み名前索引が使える状態か（True/False）を返す関数。app.py が登録する（任意）。
# 索引の取得失敗時、resolver は「見つからない」と同じ None を返してしまうため、両者を区別する用。
_released_index_probe: Callable[[], bool] | None = None


def register_released_index_probe(fn: Callable[[], bool]) -> None:
    """発売済み名前索引が使える状態かを返す関数を登録する（app.py の起動時に一度だけ）。"""
    global _released_index_probe
    _released_index_probe = fn


def released_index_available() -> bool:
    """発売済み名前索引が使えるか。probe 未登録なら True（判定不能のため疑わない）。"""
    return True if _released_index_probe is None else bool(_released_index_probe())


def unreleased_data_available() -> bool:
    """未発売カード辞書を（キャッシュまたは再取得で）読めるか。読めない＝「見つからない」と区別すべき状態。"""
    try:
        _get_unreleased(strict=True)
        return True
    except DisplayDataUnavailable:
        return False


def tcg_preview_data_available() -> bool:
    """TCG先行カード辞書を（キャッシュまたは再取得で）読めるか。読めない＝「TCG先行カードが無い」と区別すべき状態。"""
    try:
        _get_tcg_preview(strict=True)
        return True
    except DisplayDataUnavailable:
        return False


def register_released_resolver(fn: Callable[[str], str | None]) -> None:
    """
    発売済みカード URL 解決関数を登録する。
    app.py の起動時に一度だけ呼ぶこと。

    例:
        import card_display
        card_display.register_released_resolver(_ygores_image_url)

    注意: 注入する関数はメモリ内索引等の高速なものに限ること。
    外部API検索（カーナベル等）を注入するとバッチ解決が直列の
    ネットワーク呼び出しになり性能劣化する。
    """
    global _released_resolver
    _released_resolver = fn


# ──────────────────────────────────────────────
# Supabase クライアント
# ──────────────────────────────────────────────

_SUPABASE_URL = os.environ.get("SUPABASE_URL")
_SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

_supabase_client = None

if _SUPABASE_URL and _SUPABASE_KEY:
    try:
        from supabase import create_client
        _supabase_client = create_client(_SUPABASE_URL, _SUPABASE_KEY)
        logger.info("[card_display] Supabase 接続成功 — 未発売カード機能が有効です")
    except Exception as _e:
        logger.warning(f"[card_display] Supabase 接続失敗（未発売機能無効）: {_e}")
else:
    logger.info("[card_display] SUPABASE_URL/KEY 未設定 — 未発売カード機能は無効です")


def _is_enabled() -> bool:
    """Supabase クライアントが有効かどうか"""
    return _supabase_client is not None


# ──────────────────────────────────────────────
# メモリキャッシュ（TTL 30 秒）
# ──────────────────────────────────────────────

_CACHE_TTL = 30.0  # 秒
# TTL 切れでもこの秒数以内なら古い値を返して裏で取り直す。超えたら従来どおり同期で取り直す
# （DB 障害時に古い値を際限なく出し続けず、strict 呼び出しへ取得失敗を伝えるための上限）。
# データから導く値ではなく方針値（障害を何分まで古い値で覆うか）。app の _cache_warmer が
# 5 分ごとに取り直すので、通常はこの上限に届かない。
_CACHE_STALE_MAX = 600.0  # 秒

_cache_lock = threading.Lock()
_cache_generation = 0
# 裏更新中のキャッシュ名（同じキャッシュの取り直しを多重に起動しない）と、直近の裏更新失敗時刻。
_refreshing: set[str] = set()
_refresh_failed_at: dict[str, float] = {}


class DisplayDataUnavailable(RuntimeError):
    """公開データの取得失敗。正常な0件とは区別する。"""

# app_settings キャッシュ
_settings_cache: dict | None = None
_settings_cache_at: float = 0.0

# unreleased_cards キャッシュ（name → レコード dict）
# approved / linked のみ、画像情報（official_card_images）込み
_unreleased_cache: dict[str, dict] | None = None
_unreleased_cache_at: float = 0.0

# tcg_preview_cards キャッシュ（英語名 name_en → レコード dict）。status='active' かつ hidden=false のみ
_tcg_preview_cache: dict[str, dict] | None = None
_tcg_preview_cache_at: float = 0.0


def invalidate_cache() -> None:
    """
    メモリキャッシュを即時クリアする。
    P2 の管理画面トグル API から呼ぶことで 30 秒待たずに反映できる。
    """
    global _settings_cache, _settings_cache_at
    global _unreleased_cache, _unreleased_cache_at
    global _tcg_preview_cache, _tcg_preview_cache_at
    global _cache_generation
    with _cache_lock:
        _cache_generation += 1
        _settings_cache = None
        _settings_cache_at = 0.0
        _unreleased_cache = None
        _unreleased_cache_at = 0.0
        _tcg_preview_cache = None
        _tcg_preview_cache_at = 0.0
    logger.info("[card_display] キャッシュをクリアしました")


# ──────────────────────────────────────────────
# 内部: Supabase データ取得
# ──────────────────────────────────────────────

def _fetch_app_settings(*, strict: bool = False) -> dict:
    """
    app_settings テーブルから全設定を取得して {key: value} の dict で返す。
    失敗時は空の dict を返す（機能を縮退させる）。
    """
    if not _is_enabled():
        if strict:
            raise DisplayDataUnavailable("未発売カードの接続が利用できません")
        return {}
    try:
        resp = _supabase_client.table("app_settings").select("key, value").execute()
        return {row["key"]: row["value"] for row in (resp.data or [])}
    except Exception as e:
        logger.warning(f"[card_display] app_settings 取得失敗: {e}")
        if strict:
            raise DisplayDataUnavailable("表示設定を取得できません") from e
        return {}


def _read_all_rows(query, order_column: str = "id") -> list[dict]:
    """Supabaseの既定取得上限で、新着候補の並べ替え前に欠落しないようページ取得する。"""
    rows = []
    page_size = 500  # APIの既定上限1000件以内で取得する通信単位。
    while True:
        page = query.order(order_column).range(len(rows), len(rows) + page_size - 1).execute().data or []
        rows.extend(page)
        if len(page) < page_size:
            return rows


def _fetch_unreleased_cards(*, strict: bool = False) -> dict[str, dict]:
    """
    approved / linked の unreleased_cards を取得し、
    official_card_images (hidden=false, deleted_at IS NULL) を LEFT JOIN して
    name → レコード dict の辞書を返す。

    Supabase Python SDK は JOIN をサポートしないため、2 クエリで結合する。
    失敗時は空の dict を返す。
    """
    if not _is_enabled():
        if strict:
            raise DisplayDataUnavailable("未発売カードの接続が利用できません")
        return {}
    try:
        # 1. unreleased_cards（approved/linked かつ hidden=false）
        cards = _read_all_rows(
            _supabase_client.table("unreleased_cards")
            .select(
                "id, name, reading, card_type, attribute, race, "
                "level, rank, link_val, atk, def, pendulum_scale, pendulum_effect, effect_text, "
                "product_name, release_date, extracted_at, status, hidden"
            )
            .in_("status", ["approved", "linked"])
            .eq("hidden", False)
        )
        if not cards:
            return {}

        # id → レコード の辞書を作成
        id_to_card = {c["id"]: c for c in cards}
        card_ids = list(id_to_card.keys())

        # 2. official_card_images（non-hidden, non-deleted）
        images = []
        # 大量のIDでURLが長くなりすぎないよう、ページと同じ単位で取得する。
        for offset in range(0, len(card_ids), 500):
            images.extend(_read_all_rows(
                _supabase_client.table("official_card_images")
                .select("unreleased_card_id, public_url, thumb_url")
                .in_("unreleased_card_id", card_ids[offset:offset + 500])
                .eq("hidden", False)
                .is_("deleted_at", "null")
            ))

        # カードIDごとに最初の画像を紐付け（複数ある場合は先頭を使用）
        id_to_image_url: dict[int, str] = {}
        id_to_thumb_url: dict[int, str | None] = {}
        for img in images:
            cid = img["unreleased_card_id"]
            if cid not in id_to_image_url:
                id_to_image_url[cid] = img["public_url"]
                id_to_thumb_url[cid] = img.get("thumb_url")

        # 画像情報をカードレコードにマージ
        for cid, card in id_to_card.items():
            card["_image_url"] = id_to_image_url.get(cid)  # None の場合は画像なし
            card["_thumb_url"] = id_to_thumb_url.get(cid)  # 新着欄用の縮小版。None なら原寸を使う

        # name → レコード の辞書を返す（同名複数ある場合は先勝ち）
        result: dict[str, dict] = {}
        for card in cards:
            name = card["name"]
            if name not in result:
                result[name] = id_to_card[card["id"]]

        logger.debug(f"[card_display] 未発売カード取得: {len(result)} 件")
        return result

    except Exception as e:
        logger.warning(f"[card_display] unreleased_cards 取得失敗: {e}")
        if strict:
            raise DisplayDataUnavailable("未発売カードを取得できません") from e
        return {}


def _fetch_tcg_preview_cards(*, strict: bool = False) -> dict[str, dict]:
    """
    TCG先行カード（tcg_preview_cards の status='active' かつ hidden=false）を
    英語名 name_en → レコード dict の辞書で返す。失敗時は空の dict（strict なら例外）。

    日本に来たカード（status='ocg_linked'）は日本語の正式名で今の流れ（発売済み・未発売）に乗るので含めない。
    """
    if not _is_enabled():
        if strict:
            raise DisplayDataUnavailable("TCG先行カードの接続が利用できません")
        return {}
    try:
        rows = _read_all_rows(
            _supabase_client.table("tcg_preview_cards")
            .select(
                "konami_id, name_en, card_type, attribute, race, level, rank, link_val, atk, def, "
                "pendulum_scale, effect_text_en, effect_text_ja, pendulum_effect_en, pendulum_effect_ja, "
                "translation_status, product_name_en, set_code, tcg_release_date, image_url, image_page_url"
            )
            .eq("status", "active")
            .eq("hidden", False),
            order_column="konami_id",
        )
        result: dict[str, dict] = {}
        for row in rows:
            name = row.get("name_en") or ""
            if name and name not in result:
                result[name] = row
        logger.debug(f"[card_display] TCG先行カード取得: {len(result)} 件")
        return result
    except Exception as e:
        logger.warning(f"[card_display] tcg_preview_cards 取得失敗: {e}")
        if strict:
            raise DisplayDataUnavailable("TCG先行カードを取得できません") from e
        return {}


# ──────────────────────────────────────────────
# 内部: キャッシュ付き取得
# ──────────────────────────────────────────────

def _start_background_refresh(name: str, refresh: Callable[[], None]) -> None:
    """古い値を返した直後に呼ぶ。同じキャッシュの裏更新が走っていなければスレッドで取り直す。

    _cache_lock を持った状態で呼ぶこと。直近の失敗から _CACHE_TTL 秒は再試行しない
    （DB 障害中にリクエストのたびに取り直しが走るのを防ぐ）。
    """
    if name in _refreshing:
        return
    if time.monotonic() - _refresh_failed_at.get(name, float("-inf")) < _CACHE_TTL:
        return
    _refreshing.add(name)

    def run():
        try:
            refresh()
        except DisplayDataUnavailable:
            # 取得関数側で warning を出し済み。古い値は _CACHE_STALE_MAX まで使い続ける。
            with _cache_lock:
                _refresh_failed_at[name] = time.monotonic()
        except Exception:
            logger.exception(f"[card_display] {name} の裏更新に失敗")
            with _cache_lock:
                _refresh_failed_at[name] = time.monotonic()
        finally:
            with _cache_lock:
                _refreshing.discard(name)

    try:
        threading.Thread(target=run, daemon=True, name=f"card_display_refresh_{name}").start()
    except RuntimeError:
        # スレッドを作れないときは名前を外して古い値を返す（残すと以後の裏更新が永久に止まる）
        logger.exception(f"[card_display] {name} の裏更新スレッドを起動できません")
        _refreshing.discard(name)
        _refresh_failed_at[name] = time.monotonic()


def _refresh_settings() -> None:
    """app_settings を取り直してキャッシュへ入れる（取得中に無効化されたら捨てる）。"""
    global _settings_cache, _settings_cache_at
    with _cache_lock:
        generation = _cache_generation
    fresh = _fetch_app_settings(strict=True)
    with _cache_lock:
        if generation == _cache_generation:
            _settings_cache = fresh
            _settings_cache_at = time.monotonic()


def _refresh_unreleased() -> None:
    """unreleased_cards を取り直してキャッシュへ入れる（取得中に無効化されたら捨てる）。"""
    global _unreleased_cache, _unreleased_cache_at
    with _cache_lock:
        generation = _cache_generation
    fresh = _fetch_unreleased_cards(strict=True)
    with _cache_lock:
        if generation == _cache_generation:
            _unreleased_cache = fresh
            _unreleased_cache_at = time.monotonic()


def _refresh_tcg_preview() -> None:
    """tcg_preview_cards を取り直してキャッシュへ入れる（取得中に無効化されたら捨てる）。"""
    global _tcg_preview_cache, _tcg_preview_cache_at
    with _cache_lock:
        generation = _cache_generation
    fresh = _fetch_tcg_preview_cards(strict=True)
    with _cache_lock:
        if generation == _cache_generation:
            _tcg_preview_cache = fresh
            _tcg_preview_cache_at = time.monotonic()


def refresh_caches() -> None:
    """既にあるキャッシュを同期で取り直す（app の _cache_warmer から定期的に呼ぶ）。

    まだ一度も取れていないキャッシュは対象外（コールド充填は最初のリクエストに任せる）。
    失敗しても古い値は残る。例外は呼び出し側へ伝える。
    """
    with _cache_lock:
        has_settings = _settings_cache is not None
        has_unreleased = _unreleased_cache is not None
        has_tcg_preview = _tcg_preview_cache is not None
    errors = []
    # 1つの失敗でほかの取り直しを飛ばさない
    for has, refresh in ((has_settings, _refresh_settings), (has_unreleased, _refresh_unreleased),
                         (has_tcg_preview, _refresh_tcg_preview)):
        if not has:
            continue
        try:
            refresh()
        except Exception as e:
            errors.append(e)
    if errors:
        raise errors[0]


def _get_settings(*, strict: bool = False) -> dict:
    """TTL 付きキャッシュから app_settings を返す（TTL 切れは古い値＋裏更新。冒頭の説明参照）"""
    global _settings_cache, _settings_cache_at
    now = time.monotonic()
    with _cache_lock:
        if _settings_cache is not None:
            age = now - _settings_cache_at
            if age < _CACHE_TTL:
                return _settings_cache
            if age < _CACHE_STALE_MAX:
                _start_background_refresh("settings", _refresh_settings)
                return _settings_cache
        generation = _cache_generation
    # キャッシュ失効 or 未初期化 → 再取得（ロック外で実行してブロックを最小化）
    try:
        fresh = _fetch_app_settings(strict=True)
    except DisplayDataUnavailable:
        if strict:
            raise
        return {}  # 失敗した空辞書は正常キャッシュとして保存しない。
    with _cache_lock:
        if generation == _cache_generation:
            _settings_cache = fresh
            _settings_cache_at = time.monotonic()
            return fresh
    # 管理操作による無効化が取得中に起きた場合、古い応答で復活させない。
    return _get_settings(strict=strict)


def _get_unreleased(*, strict: bool = False) -> dict[str, dict]:
    """TTL 付きキャッシュから unreleased_cards 辞書を返す（TTL 切れは古い値＋裏更新）"""
    global _unreleased_cache, _unreleased_cache_at
    now = time.monotonic()
    with _cache_lock:
        if _unreleased_cache is not None:
            age = now - _unreleased_cache_at
            if age < _CACHE_TTL:
                return _unreleased_cache
            if age < _CACHE_STALE_MAX:
                _start_background_refresh("unreleased", _refresh_unreleased)
                return _unreleased_cache
        generation = _cache_generation
    try:
        fresh = _fetch_unreleased_cards(strict=True)
    except DisplayDataUnavailable:
        if strict:
            raise
        return {}
    with _cache_lock:
        if generation == _cache_generation:
            _unreleased_cache = fresh
            _unreleased_cache_at = time.monotonic()
            return fresh
    return _get_unreleased(strict=strict)


def _get_tcg_preview(*, strict: bool = False) -> dict[str, dict]:
    """TTL 付きキャッシュから tcg_preview_cards 辞書を返す（TTL 切れは古い値＋裏更新。未発売カードと同じ方式）"""
    global _tcg_preview_cache, _tcg_preview_cache_at
    now = time.monotonic()
    with _cache_lock:
        if _tcg_preview_cache is not None:
            age = now - _tcg_preview_cache_at
            if age < _CACHE_TTL:
                return _tcg_preview_cache
            if age < _CACHE_STALE_MAX:
                _start_background_refresh("tcg_preview", _refresh_tcg_preview)
                return _tcg_preview_cache
        generation = _cache_generation
    try:
        fresh = _fetch_tcg_preview_cards(strict=True)
    except DisplayDataUnavailable:
        if strict:
            raise
        return {}
    with _cache_lock:
        if generation == _cache_generation:
            _tcg_preview_cache = fresh
            _tcg_preview_cache_at = time.monotonic()
            return fresh
    return _get_tcg_preview(strict=strict)


def cached_new_cards(limit: int | None = 36) -> dict | None:
    """覚えている値だけで新着の未発売カードを返す。DB を待つ必要があるときは None（ページの HTML に埋め込む用）。

    トップと /new-cards の HTML に最初の一覧を埋め込み、画像を早く読み始めるために使う（2026-10-22 LCP）。
    キャッシュが無い・古すぎる（_CACHE_STALE_MAX 超え）ときに DB を待つと、DB が遅いときにページの HTML ごと止まる
    （2026-10-22 朝の Disk IO Budget 枯渇で実際に起きた）。その場合は None を返し、画面は従来どおり API で取る。
    """
    now = time.monotonic()
    with _cache_lock:
        if _settings_cache is None or _unreleased_cache is None:
            return None
        if now - _settings_cache_at >= _CACHE_STALE_MAX or now - _unreleased_cache_at >= _CACHE_STALE_MAX:
            return None
    try:
        # 両方のキャッシュが期限内（古ければ裏で取り直す）なので、ここでは DB を待たない
        return get_new_cards(limit=limit)
    except Exception:
        return None


def get_new_cards(limit: int | None = 36) -> dict:
    """承認・表示許可済みの未発売カードを登録日時順に返す。

    limit が整数ならその件数まで（既定36件・超過分は has_more）。None なら上限なしで全件返す。
    """
    while True:
        with _cache_lock:
            generation = _cache_generation
        cards = _get_unreleased(strict=True)
        settings = _get_settings(strict=True)
        with _cache_lock:
            if generation == _cache_generation:
                break
    image_enabled = bool(settings.get("OFFICIAL_IMAGE_DISPLAY", {}).get("enabled", True))
    today = datetime.now(JST).date()
    rows = []
    for card in cards.values():
        # linkedは照合ジョブで発売確認済み。発売日が未設定・未来日のままでも一覧から除く。
        if card.get("status") != "approved" or card.get("hidden"):
            continue
        release_date = card.get("release_date")
        if release_date and date.fromisoformat(release_date) <= today:
            continue
        added_at = card.get("extracted_at")
        # extracted_atは公式公開日ではなく、当サービスに追加した日時。
        added = datetime.fromisoformat(added_at.replace("Z", "+00:00")) if added_at else None
        if added is not None and added.tzinfo is None:
            added = added.replace(tzinfo=timezone.utc)
        rows.append((added.timestamp() if added else float("-inf"), {
            "name": card["name"], "added_at": added.isoformat() if added else None,
            "release_date": release_date or None,
            "product_name": card.get("product_name") or None,
            "image_url": (card.get("_image_url") or None) if image_enabled else None,
            # 縮小版（幅300px の WebP）。原寸が無ければ出さない
            "thumb_url": (card.get("_thumb_url") or None) if image_enabled and card.get("_image_url") else None,
        }))
    rows.sort(key=lambda row: (row[0], row[1]["name"]), reverse=True)
    if limit is None:
        return {"cards": [row for _, row in rows], "has_more": False}
    return {"cards": [row for _, row in rows[:limit]], "has_more": len(rows) > limit}


def _official_image_enabled() -> bool:
    """OFFICIAL_IMAGE_DISPLAY 設定が enabled かどうか"""
    settings = _get_settings()
    val = settings.get("OFFICIAL_IMAGE_DISPLAY", {})
    return bool(val.get("enabled", True))


def _is_ex_from_card_type(card_type: str) -> bool:
    """card_type 文字列から EX デッキカードかどうかを判定"""
    if not card_type:
        return False
    t = card_type.lower()
    return any(kw in t for kw in ["融合", "シンクロ", "エクシーズ", "リンク"])


# ──────────────────────────────────────────────
# 内部: プロキシデータ構築
# ──────────────────────────────────────────────

def _build_proxy_data(card: dict) -> dict:
    """
    unreleased_cards レコードからフロントエンドのプロキシ描画用 dict を構築する。
    app.py の card-info レスポンスのキー名（broad_type / is_ex 等）にできる限り合わせる。
    """
    card_type = card.get("card_type", "")

    # broad_type 判定（app.py 既存ロジックと同様の区分）
    ct_lower = card_type.lower()
    # 魔法・罠を先に見る。「通常魔法」「通常罠」「儀式魔法」は "通常"・"儀式" を含むため、
    # モンスターの語を先に見るとモンスター扱いになっていた（2026-10-23 TCG先行カードの表示で発見）
    if "モンスター" in ct_lower:
        broad_type = "monster"
    elif "魔法" in ct_lower:
        broad_type = "spell"
    elif "罠" in ct_lower:
        broad_type = "trap"
    else:
        broad_type = "monster"  # 「チューナー」等の略記・不明はモンスター扱い（EXフラグ判定に安全）

    return {
        "name":         card.get("name", ""),
        "reading":      card.get("reading", ""),
        "card_type":    card_type,
        "attribute":    card.get("attribute", ""),
        "race":         card.get("race", ""),
        "level":        card.get("level"),
        "rank":         card.get("rank"),
        "link_val":     card.get("link_val"),
        "atk":          card.get("atk"),
        "def":          card.get("def", ""),
        "pendulum_scale": card.get("pendulum_scale"),
        "pendulum_effect": card.get("pendulum_effect", ""),
        "effect_text":  card.get("effect_text", ""),
        "product_name": card.get("product_name", ""),
        "release_date": str(card.get("release_date") or ""),
        # app.py 互換キー（card-info レスポンスに合わせる）
        "broad_type":   broad_type,
        "is_ex":        _is_ex_from_card_type(card_type),
    }


def _build_tcg_preview_proxy(row: dict) -> dict:
    """
    tcg_preview_cards の行から、未発売カードの proxy と同じ形（_build_proxy_data 互換）の dict を作る。

    足すキー:
      tcg_preview           … True（TCG先行カードの印。画面は「TCG先行」の札・価格の操作を出さない）
      unofficial_translation … effect_text が機械訳（非公式訳）なら True。訳が無い（未訳・失敗・通常モンスター）ときは
                               effect_text は空で False（画面は英語の原文だけを出す）
      effect_text_en / pendulum_effect_en … 英語の原文
      product_name / tcg_release_date / set_code … 英語版の収録商品・発売日・型番
    release_date は空にする（未発売カードの release_date は「発売日が来たら発売済み」の判定に使われるため、
    TCG の発売日を入れると価格の流れに入ってしまう）。
    """
    translated = row.get("translation_status") == "done"
    proxy = _build_proxy_data({
        "name":            row.get("name_en") or "",
        "reading":         "",
        "card_type":       row.get("card_type") or "",
        "attribute":       row.get("attribute") or "",
        "race":            row.get("race") or "",
        "level":           row.get("level"),
        "rank":            row.get("rank"),
        "link_val":        row.get("link_val"),
        "atk":             row.get("atk"),
        "def":             row.get("def") or "",
        "pendulum_scale":  row.get("pendulum_scale"),
        "pendulum_effect": (row.get("pendulum_effect_ja") or "") if translated else "",
        "effect_text":     (row.get("effect_text_ja") or "") if translated else "",
        "product_name":    row.get("product_name_en") or "",
        "release_date":    "",
    })
    proxy.update({
        "tcg_preview":            True,
        "unofficial_translation": translated and bool(row.get("effect_text_ja") or row.get("pendulum_effect_ja")),
        "effect_text_en":         row.get("effect_text_en") or "",
        "pendulum_effect_en":     row.get("pendulum_effect_en") or "",
        "tcg_release_date":       str(row.get("tcg_release_date") or ""),
        "set_code":               row.get("set_code") or "",
    })
    return proxy


def _find_tcg_preview_card(name: str, cards: dict[str, dict] | None = None) -> dict | None:
    """英語名から TCG先行カードの行を引く。完全一致 → 大文字小文字・記号のゆれを吸収して再照合。無ければ None。"""
    if not name or not _is_enabled():
        return None
    if cards is None:
        cards = _get_tcg_preview()
    card = cards.get(name)
    if card is not None:
        return card
    fk = fuzzy_key(name).lower()
    if not fk:
        return None
    for cand_name, cand in cards.items():
        if fuzzy_key(cand_name).lower() == fk:
            return cand
    return None


def _tcg_preview_display(card: dict, img_enabled: bool) -> dict:
    """TCG先行カードの表示解決結果（画像は公式SAMPLEと同じ扱い＝透かしなし・contain 表示）。"""
    img_url = card.get("image_url")
    if img_url and img_enabled:
        return {"kind": "image", "url": img_url, "source": "official_sample", "tcg_preview": True}
    return {"kind": "proxy", "proxy": _build_tcg_preview_proxy(card)}


# ──────────────────────────────────────────────
# 公開 API
# ──────────────────────────────────────────────

def get_tcg_preview_names() -> list[str]:
    """サジェスト用に、表示対象の TCG先行カードの英語名を返す。Supabase 未設定・取得失敗時は空配列。"""
    return list(_get_tcg_preview().keys())


def get_tcg_preview_proxy(name: str) -> dict | None:
    """英語名から TCG先行カードのカード情報（proxy と同じ形＋tcg_preview 等）を返す。無ければ None。"""
    card = _find_tcg_preview_card(name)
    return _build_tcg_preview_proxy(card) if card is not None else None


def get_display_proxy(name: str) -> dict | None:
    """カード情報パネル用: 未発売カード → TCG先行カードの順に proxy を返す。どちらでもなければ None。

    価格の関門（発売日の判定）には使わないこと。そちらは get_unreleased_proxy（未発売カードだけ）を使う。
    """
    return get_unreleased_proxy(name) or get_tcg_preview_proxy(name)


def get_unreleased_names() -> list[str]:
    """
    サジェスト用に、表示対象の未発売カード名（approved / linked かつ hidden=false）を返す。
    Supabase 未設定・取得失敗時は空配列を返す（機能を縮退させる）。
    """
    return list(_get_unreleased().keys())


def get_unreleased_release_map() -> dict[str, str]:
    """
    未発売テーブル（approved / linked かつ hidden=false）の
    name → release_date(ISO文字列 "YYYY-MM-DD"、無ければ "") のマップを返す。

    呼び出し側で「発売日が到来済みか（=実質発売済み）」を判定するために使う。
    発売済み名インデックス（cardnames_ja.json）は週次更新で鮮度が低いため、
    発売日ベースの判定でラグ期間中の誤分類（発売済みカードを未発売扱い）を防ぐ。
    """
    return {name: str(card.get("release_date") or "")
            for name, card in _get_unreleased().items()}


def _find_unreleased_card(name: str) -> dict | None:
    """名前から未発売カードの行（approved / linked かつ hidden=false。id・_image_url を含む）を引く。無ければ None。"""
    if not name or not _is_enabled():
        return None
    cards = _get_unreleased()
    card = cards.get(name)
    if card is None:
        # 完全一致なし: 記号・ハイフン種のゆれを吸収して再照合。
        # unreleased_cards.name は全角ハイフン（－/―等）を含む一方、検索クエリは
        # 半角ハイフンに正規化されるため、完全一致だとハイフン入りカードが取りこぼされる。
        fk = fuzzy_key(name)
        if fk:
            for cand_name, cand in cards.items():
                if fuzzy_key(cand_name) == fk:
                    card = cand
                    break
    return card


def get_unreleased_record(name: str) -> dict | None:
    """名前から公開中の未発売カードの行（id を含む）を返す。無ければ None（OG 画像・ページのメタ情報用）。"""
    return _find_unreleased_card(name)


def is_setting_enabled(key: str, default: bool = True) -> bool:
    """app_settings の {"enabled": bool} 形式の設定を返す。行が無い・取得失敗は default。"""
    val = _get_settings().get(key)
    if isinstance(val, dict) and isinstance(val.get("enabled"), bool):
        return val["enabled"]
    return default


def get_unreleased_proxy(name: str) -> dict | None:
    """
    表示種別（画像 / プロキシ）に関わらず、未発売カード（approved / linked かつ
    hidden=false）のカード情報（proxy データ dict）を返す。無ければ None。

    resolve_card_display は「画像で見せるか / プロキシ描画か」という表示判定のため、
    公式画像があるカードは proxy データを返さない。一方カード情報パネルは画像の有無に
    関係なくテキスト情報（効果・種別等）が必要なため、その用途で使う専用の入口。
    """
    card = _find_unreleased_card(name)
    if card is None:
        return None
    return _build_proxy_data(card)


def resolve_card_display(name: str) -> dict:
    """
    カード名から表示解決結果を返す唯一の入口。

    戻り値の種類:
      {"kind": "image",  "url": str, "source": "released"|"official_sample"[, "tcg_preview": True]}
      {"kind": "proxy",  "proxy": dict}
      {"kind": "none"}

    source は画像の出所を示す:
      "released"        … 発売済みクリーン画像（外部URL直リンク）
      "official_sample" … 未発売の公式 SAMPLE 画像（Storage）
    フロント側で透かし適用可否・object-fit の出し分けに使う。
    """
    if not name:
        return {"kind": "none"}

    # (1) 発売済み: 注入された resolver に問い合わせ
    if _released_resolver is not None:
        url = _released_resolver(name)
        if url:
            return {"kind": "image", "url": url, "source": "released"}

    # Supabase 未設定なら (4) へ
    if not _is_enabled():
        return {"kind": "none"}

    # 未発売カード辞書を取得
    unreleased = _get_unreleased()
    card = unreleased.get(name)

    if card is None:
        # (4) TCG先行カード（英語名）
        tcg_card = _find_tcg_preview_card(name)
        if tcg_card is not None:
            return _tcg_preview_display(tcg_card, _official_image_enabled())
        # (5) どこにも無い（pending/rejected を含む）
        return {"kind": "none"}

    # (2) 画像あり + 設定有効
    img_url = card.get("_image_url")
    if img_url and _official_image_enabled():
        return {"kind": "image", "url": img_url, "source": "official_sample"}

    # (3) 画像なし or 設定無効 → プロキシ
    return {"kind": "proxy", "proxy": _build_proxy_data(card)}


def resolve_card_displays(names: list[str]) -> dict[str, dict]:
    """
    複数カード名をまとめて解決するバッチ版。
    未発売辞書・設定は1回だけ取得してすべての名前に使い回す。

    戻り値: {カード名: resolve_card_display の結果}
    """
    if not names:
        return {}

    # 発売済み一括解決（O(1) × n 件、ネットワーク不要）
    result: dict[str, dict] = {}
    needs_check: list[str] = []

    for name in names:
        if not name:
            result[name] = {"kind": "none"}
            continue
        if _released_resolver is not None:
            url = _released_resolver(name)
            if url:
                result[name] = {"kind": "image", "url": url, "source": "released"}
                continue
        needs_check.append(name)

    if not needs_check:
        return result

    # Supabase 未設定なら残りは全て none
    if not _is_enabled():
        for name in needs_check:
            result[name] = {"kind": "none"}
        return result

    # 未発売辞書を1回だけ取得
    unreleased = _get_unreleased()
    img_enabled = _official_image_enabled()
    tcg_preview = None  # TCG先行の辞書は、未発売に無い名前があったときだけ取る

    for name in needs_check:
        card = unreleased.get(name)
        if card is None:
            if tcg_preview is None:
                tcg_preview = _get_tcg_preview()
            tcg_card = _find_tcg_preview_card(name, tcg_preview)
            result[name] = _tcg_preview_display(tcg_card, img_enabled) if tcg_card is not None else {"kind": "none"}
            continue
        img_url = card.get("_image_url")
        if img_url and img_enabled:
            result[name] = {"kind": "image", "url": img_url, "source": "official_sample"}
        else:
            result[name] = {"kind": "proxy", "proxy": _build_proxy_data(card)}

    return result
