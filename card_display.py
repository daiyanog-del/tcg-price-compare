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
  (4) それ以外（pending/rejected/不明）
        → {"kind": "none"}

Supabase 環境変数（SUPABASE_URL/SUPABASE_KEY）が未設定の場合は
未発売機能を無効として (1)/(4) のみで動作する。
既存挙動は完全に保持される。

循環 import 防止:
  app.py 側の _ygores_image_url / kanabell_card_image_url は
  register_released_resolver() で注入する関数注入方式をとる。
  card_display.py は app.py を import しない。

キャッシュ:
  app_settings と unreleased_cards の辞書はモジュール内メモリに TTL 30 秒で保持。
  invalidate_cache() を呼ぶと即時クリアされる（P2 管理画面トグルAPIから使用）。
"""

import os
import time
import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 関数注入（循環 import 回避）
# ──────────────────────────────────────────────

# app.py 側から register_released_resolver() で登録される。
# 引数: カード名(str) → URL文字列 or None
_released_resolver: Callable[[str], str | None] | None = None


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

_cache_lock = threading.Lock()

# app_settings キャッシュ
_settings_cache: dict | None = None
_settings_cache_at: float = 0.0

# unreleased_cards キャッシュ（name → レコード dict）
# approved / linked のみ、画像情報（official_card_images）込み
_unreleased_cache: dict[str, dict] | None = None
_unreleased_cache_at: float = 0.0


def invalidate_cache() -> None:
    """
    メモリキャッシュを即時クリアする。
    P2 の管理画面トグル API から呼ぶことで 30 秒待たずに反映できる。
    """
    global _settings_cache, _settings_cache_at
    global _unreleased_cache, _unreleased_cache_at
    with _cache_lock:
        _settings_cache = None
        _settings_cache_at = 0.0
        _unreleased_cache = None
        _unreleased_cache_at = 0.0
    logger.info("[card_display] キャッシュをクリアしました")


# ──────────────────────────────────────────────
# 内部: Supabase データ取得
# ──────────────────────────────────────────────

def _fetch_app_settings() -> dict:
    """
    app_settings テーブルから全設定を取得して {key: value} の dict で返す。
    失敗時は空の dict を返す（機能を縮退させる）。
    """
    if not _is_enabled():
        return {}
    try:
        resp = _supabase_client.table("app_settings").select("key, value").execute()
        return {row["key"]: row["value"] for row in (resp.data or [])}
    except Exception as e:
        logger.warning(f"[card_display] app_settings 取得失敗: {e}")
        return {}


def _fetch_unreleased_cards() -> dict[str, dict]:
    """
    approved / linked の unreleased_cards を取得し、
    official_card_images (hidden=false, deleted_at IS NULL) を LEFT JOIN して
    name → レコード dict の辞書を返す。

    Supabase Python SDK は JOIN をサポートしないため、2 クエリで結合する。
    失敗時は空の dict を返す。
    """
    if not _is_enabled():
        return {}
    try:
        # 1. unreleased_cards（approved/linked かつ hidden=false）
        cards_resp = (
            _supabase_client.table("unreleased_cards")
            .select(
                "id, name, reading, card_type, attribute, race, "
                "level, rank, link_val, atk, def, pendulum_scale, pendulum_effect, effect_text, "
                "product_name, release_date, status, hidden"
            )
            .in_("status", ["approved", "linked"])
            .eq("hidden", False)
            .execute()
        )
        cards = cards_resp.data or []
        if not cards:
            return {}

        # id → レコード の辞書を作成
        id_to_card = {c["id"]: c for c in cards}
        card_ids = list(id_to_card.keys())

        # 2. official_card_images（non-hidden, non-deleted）
        images_resp = (
            _supabase_client.table("official_card_images")
            .select("unreleased_card_id, public_url")
            .in_("unreleased_card_id", card_ids)
            .eq("hidden", False)
            .is_("deleted_at", "null")
            .execute()
        )
        images = images_resp.data or []

        # カードIDごとに最初の画像を紐付け（複数ある場合は先頭を使用）
        id_to_image_url: dict[int, str] = {}
        for img in images:
            cid = img["unreleased_card_id"]
            if cid not in id_to_image_url:
                id_to_image_url[cid] = img["public_url"]

        # 画像情報をカードレコードにマージ
        for cid, card in id_to_card.items():
            card["_image_url"] = id_to_image_url.get(cid)  # None の場合は画像なし

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
        return {}


# ──────────────────────────────────────────────
# 内部: キャッシュ付き取得
# ──────────────────────────────────────────────

def _get_settings() -> dict:
    """TTL 付きキャッシュから app_settings を返す"""
    global _settings_cache, _settings_cache_at
    now = time.monotonic()
    with _cache_lock:
        if _settings_cache is not None and (now - _settings_cache_at) < _CACHE_TTL:
            return _settings_cache
    # キャッシュ失効 or 未初期化 → 再取得（ロック外で実行してブロックを最小化）
    fresh = _fetch_app_settings()
    with _cache_lock:
        _settings_cache = fresh
        _settings_cache_at = time.monotonic()
    return fresh


def _get_unreleased() -> dict[str, dict]:
    """TTL 付きキャッシュから unreleased_cards 辞書を返す"""
    global _unreleased_cache, _unreleased_cache_at
    now = time.monotonic()
    with _cache_lock:
        if _unreleased_cache is not None and (now - _unreleased_cache_at) < _CACHE_TTL:
            return _unreleased_cache
    fresh = _fetch_unreleased_cards()
    with _cache_lock:
        _unreleased_cache = fresh
        _unreleased_cache_at = time.monotonic()
    return fresh


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
    if "モンスター" in ct_lower or any(k in ct_lower for k in ["通常", "効果", "儀式", "融合", "シンクロ", "エクシーズ", "リンク", "ペンデュラム", "トークン"]):
        broad_type = "monster"
    elif "魔法" in ct_lower:
        broad_type = "spell"
    elif "罠" in ct_lower:
        broad_type = "trap"
    else:
        broad_type = "monster"  # 不明はモンスター扱い（EXフラグ判定に安全）

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


# ──────────────────────────────────────────────
# 公開 API
# ──────────────────────────────────────────────

def get_unreleased_names() -> list[str]:
    """
    サジェスト用に、表示対象の未発売カード名（approved / linked かつ hidden=false）を返す。
    Supabase 未設定・取得失敗時は空配列を返す（機能を縮退させる）。
    """
    return list(_get_unreleased().keys())


def get_unreleased_proxy(name: str) -> dict | None:
    """
    表示種別（画像 / プロキシ）に関わらず、未発売カード（approved / linked かつ
    hidden=false）のカード情報（proxy データ dict）を返す。無ければ None。

    resolve_card_display は「画像で見せるか / プロキシ描画か」という表示判定のため、
    公式画像があるカードは proxy データを返さない。一方カード情報パネルは画像の有無に
    関係なくテキスト情報（効果・種別等）が必要なため、その用途で使う専用の入口。
    """
    if not name or not _is_enabled():
        return None
    card = _get_unreleased().get(name)
    if card is None:
        return None
    return _build_proxy_data(card)


def resolve_card_display(name: str) -> dict:
    """
    カード名から表示解決結果を返す唯一の入口。

    戻り値の種類:
      {"kind": "image",  "url": str, "source": "released"|"official_sample"}
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
        # (4) 未発売辞書にも無い（pending/rejected を含む）
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

    for name in needs_check:
        card = unreleased.get(name)
        if card is None:
            result[name] = {"kind": "none"}
            continue
        img_url = card.get("_image_url")
        if img_url and img_enabled:
            result[name] = {"kind": "image", "url": img_url, "source": "official_sample"}
        else:
            result[name] = {"kind": "proxy", "proxy": _build_proxy_data(card)}

    return result
