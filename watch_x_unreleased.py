"""
watch_x_unreleased.py — X公式アカウントから未発売OCGカード情報を取得

X API v2 を使って @YuGiOh_OCG_INFO のタイムラインを巡回し、
◤〜◢ パターンを含む新カード公開ポストを検出して unreleased_cards に取り込む。

実行環境: Render Cron（クラウドIPでX APIはアクセス可能）
スケジュール: 1日1回

フィルタリング方針:
  1. リポストを除外（X API の exclude=retweets）
  2. ◤〜◢ パターンを含まないポストをスキップ
  3. ◤〜◢ から抽出したカード名が unreleased_cards に存在すればスキップ（Vision呼び出しなし）
  4. ygoresources 既知カード（発売済み）もスキップ

重複防止:
  - ステップ3の事前チェックに加え、upsert の UNIQUE(name, product_name) が最終安全網
  - yugioh.jp 経由で先取り込み済みのカードは fuzzy_key 照合で除外される
"""

import logging
import os
import re

import requests
from supabase import create_client, Client

from name_normalize import fuzzy_key
from ygores_repository import repository as _ygores_repo

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 環境変数
# ──────────────────────────────────────────────

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# ──────────────────────────────────────────────
# 定数
# ──────────────────────────────────────────────

ACCOUNT_USERNAME = "YuGiOh_OCG_INFO"
X_API_BASE = "https://api.twitter.com/2"

# ◤〜◢ パターン（カード名抽出用）
_CARD_NAME_RE = re.compile(r"◤(.+?)◢")

# app_settings キー（前回処理済み最大 tweet_id）
_SINCE_ID_KEY = "X_WATCHER_SINCE_ID"


# ──────────────────────────────────────────────
# X API クライアント
# ──────────────────────────────────────────────

def _x_get(path: str, params: dict) -> dict:
    """X API v2 GET リクエスト（Bearer Token 認証）"""
    resp = requests.get(
        f"{X_API_BASE}{path}",
        headers={"Authorization": f"Bearer {X_BEARER_TOKEN}"},
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def get_user_id(username: str) -> str:
    """ユーザー名からユーザーIDを取得する"""
    data = _x_get(f"/users/by/username/{username}", {})
    return data["data"]["id"]


def fetch_tweets(user_id: str, since_id: str | None) -> tuple[list[dict], dict[str, str]]:
    """タイムラインを取得する。

    Returns:
        (ツイートリスト, media_map {media_key: url})
    """
    params: dict = {
        "exclude": "retweets",
        "expansions": "attachments.media_keys",
        "media.fields": "url",
        "tweet.fields": "text,attachments",
        "max_results": 100,
    }
    if since_id:
        params["since_id"] = since_id

    data = _x_get(f"/users/{user_id}/tweets", params)
    tweets = data.get("data") or []

    # media_key → URL のマップ
    media_map: dict[str, str] = {}
    for media in (data.get("includes") or {}).get("media", []):
        key = media.get("media_key", "")
        url = media.get("url", "")
        if key and url:
            media_map[key] = url

    return tweets, media_map


# ──────────────────────────────────────────────
# since_id 管理（app_settings テーブル）
# ──────────────────────────────────────────────

def _load_since_id(sb: Client) -> str | None:
    """前回処理済みの最大 tweet_id を取得する"""
    resp = sb.table("app_settings").select("value").eq("key", _SINCE_ID_KEY).execute()
    if resp.data:
        return (resp.data[0]["value"] or {}).get("since_id")
    return None


def _save_since_id(sb: Client, since_id: str) -> None:
    """処理済み最大 tweet_id を保存する"""
    sb.table("app_settings").upsert(
        {"key": _SINCE_ID_KEY, "value": {"since_id": since_id}},
        on_conflict="key",
    ).execute()


# ──────────────────────────────────────────────
# 重複チェック
# ──────────────────────────────────────────────

def _load_existing_fuzzy_keys(sb: Client) -> set[str]:
    """unreleased_cards の全カード名を fuzzy_key に変換した集合を返す"""
    resp = sb.table("unreleased_cards").select("name").execute()
    return {fuzzy_key(r["name"]) for r in (resp.data or []) if r.get("name")}


def _is_duplicate(card_name: str, existing_keys: set[str]) -> bool:
    """カード名が既に unreleased_cards に存在するか確認する"""
    return fuzzy_key(card_name) in existing_keys


# ──────────────────────────────────────────────
# ygoresources 既知カードフィルタ（発売済みカード除外）
# ──────────────────────────────────────────────

_known_fuzzy_keys: set[str] | None = None


def _get_known_fuzzy_keys() -> set[str]:
    """ygoresources の既知カード名 fuzzy_key 集合を返す（1実行1回）"""
    global _known_fuzzy_keys
    if _known_fuzzy_keys is not None:
        return _known_fuzzy_keys

    name_index = _ygores_repo.get_name_index()
    if not name_index:
        logger.warning("[X-Watcher] ygores 名前インデックスが空: フィルタをスキップします")
        _known_fuzzy_keys = set()
        return _known_fuzzy_keys

    _known_fuzzy_keys = {fuzzy_key(name) for name in name_index}
    logger.info(f"[X-Watcher] ygores 既知カード: {len(_known_fuzzy_keys)}件")
    return _known_fuzzy_keys


# ──────────────────────────────────────────────
# upsert
# ──────────────────────────────────────────────

def _upsert_cards(sb: Client, rows: list[dict]) -> list[dict]:
    """unreleased_cards に upsert する。既存行は上書きしない。挿入された行を返す。"""
    inserted = []
    for row in rows:
        try:
            resp = sb.table("unreleased_cards").upsert(
                row,
                on_conflict="name,product_name",
                ignore_duplicates=True,
            ).execute()
            if resp.data:
                inserted.extend(resp.data)
        except Exception as e:
            logger.warning(f"[X-Watcher] upsert失敗 ({row.get('name', '?')}): {e}")
    return inserted


# ──────────────────────────────────────────────
# ポスト処理
# ──────────────────────────────────────────────

def process_tweets(
    tweets: list[dict],
    media_map: dict[str, str],
    sb: Client,
    existing_keys: set[str],
) -> int:
    """ツイートリストを処理してDBに挿入する。戻り値: 挿入件数合計"""
    from unreleased_extractor import extract_cards_from_tweet

    known = _get_known_fuzzy_keys()
    total_inserted = 0

    for tweet in tweets:
        text = tweet.get("text", "")
        tweet_id = tweet.get("id", "")
        tweet_url = f"https://x.com/{ACCOUNT_USERNAME}/status/{tweet_id}"

        # ①◤◢ パターンフィルタ
        names = _CARD_NAME_RE.findall(text)
        if not names:
            logger.debug(f"[X-Watcher] ◤◢なし: {tweet_id!r}")
            continue

        logger.info(f"[X-Watcher] カード名候補: {names} ({tweet_url})")

        # ②unreleased_cards 重複チェック（全名前が既存ならVisionスキップ）
        if all(_is_duplicate(n, existing_keys) for n in names):
            logger.info(f"[X-Watcher] 全カードが取込済み: スキップ ({tweet_url})")
            continue

        # ③ygores 既知カードチェック（全名前が発売済みならスキップ）
        if known and all(fuzzy_key(n) in known for n in names):
            logger.info(f"[X-Watcher] 全カードがygores既知: スキップ ({tweet_url})")
            continue

        # ④画像URL取得
        media_keys = (tweet.get("attachments") or {}).get("media_keys", [])
        image_urls = [media_map[k] for k in media_keys if k in media_map]
        if not image_urls:
            logger.warning(f"[X-Watcher] 画像なし: {tweet_url}")
            continue

        # ⑤Vision 抽出
        logger.info(f"[X-Watcher] Vision抽出: {tweet_url} ({len(image_urls)}枚)")
        try:
            card_rows = extract_cards_from_tweet(text, image_urls, tweet_url)
        except Exception as e:
            logger.error(f"[X-Watcher] 抽出失敗: {tweet_url} → {e}")
            continue

        if not card_rows:
            logger.info(f"[X-Watcher] 抽出0件: {tweet_url}")
            continue

        # ⑥ygores 既知カードフィルタ（抽出結果に対して）
        filtered = [
            r for r in card_rows
            if not known or fuzzy_key(r.get("name", "")) not in known
        ]
        skipped = len(card_rows) - len(filtered)
        if skipped:
            logger.info(f"[X-Watcher] ygores既知スキップ: {skipped}件")

        # ⑦upsert
        inserted_rows = _upsert_cards(sb, filtered)
        n = len(inserted_rows)
        total_inserted += n
        # 新規取込分をキャッシュに追加（同一実行内の重複処理防止）
        for row in filtered:
            existing_keys.add(fuzzy_key(row.get("name", "")))
        logger.info(f"[X-Watcher] 挿入: {n}件 ({tweet_url})")

        # ⑧取り込み時点で画像をクロップ・保存（管理画面で承認前に確認できるようにする）
        from unreleased_image_store import ingest_x_card_image
        for card in inserted_rows:
            cid = card.get("id")
            raw = card.get("extraction_raw") or {}
            img_url = (raw.get("card_image_url") or "").strip()
            if not img_url:
                img_url = (raw.get("card_image_urls") or [""])[0]
            if cid and img_url:
                ok, reason = ingest_x_card_image(sb, cid, img_url, tweet_url)
                logger.info(f"[X-Watcher] 画像保存: card_id={cid}, ok={ok}, reason={reason!r}")

    return total_inserted


# ──────────────────────────────────────────────
# メイン処理
# ──────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if not all([SUPABASE_URL, SUPABASE_KEY, X_BEARER_TOKEN]):
        logger.error("[X-Watcher] SUPABASE_URL / SUPABASE_KEY / X_BEARER_TOKEN が未設定です")
        return

    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error("[X-Watcher] ANTHROPIC_API_KEY が未設定です")
        return

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    # ユーザーID取得
    try:
        user_id = get_user_id(ACCOUNT_USERNAME)
        logger.info(f"[X-Watcher] ユーザーID: {user_id}")
    except Exception as e:
        logger.error(f"[X-Watcher] ユーザーID取得失敗: {e}")
        return

    # 前回処理済みの最大 tweet_id
    since_id = _load_since_id(sb)
    logger.info(f"[X-Watcher] since_id: {since_id!r}")

    # ツイート取得
    try:
        tweets, media_map = fetch_tweets(user_id, since_id)
    except Exception as e:
        logger.error(f"[X-Watcher] ツイート取得失敗: {e}")
        _notify_discord(f"[watch_x_unreleased] ツイート取得失敗: {e}")
        return

    if not tweets:
        logger.info("[X-Watcher] 新規ツイートなし")
        return

    logger.info(f"[X-Watcher] ツイート取得: {len(tweets)}件")

    # unreleased_cards の既存カード名を一括取得（重複チェック用）
    existing_keys = _load_existing_fuzzy_keys(sb)
    logger.info(f"[X-Watcher] 既存カード数: {len(existing_keys)}件")

    # 処理
    total = process_tweets(tweets, media_map, sb, existing_keys)

    # since_id を最大値で更新（数値比較で最大値を取る）
    max_id = max(tweets, key=lambda t: int(t["id"]))["id"]
    _save_since_id(sb, max_id)
    logger.info(f"[X-Watcher] since_id を更新: {max_id}")
    logger.info(f"[X-Watcher] 完了: 挿入合計={total}件")


def _notify_discord(message: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        from discord_notify import send_discord_message
        send_discord_message(message)
    except Exception:
        pass


if __name__ == "__main__":
    main()
