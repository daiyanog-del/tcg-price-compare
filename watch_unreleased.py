"""
watch_unreleased.py — 未発売カード情報 Watcher エントリポイント

GitHub Actions cron（2時間毎）から実行される。
処理フロー:
  1. Supabase watched_pages から enabled=true を取得。0件なら SEED_URLS を初期投入
  2. 各ページを取得 → SHA-256 でコンテンツ差分を検知 → last_checked_at を更新
  3. 変化あり → ページ内リンクから新規ページ候補を最大20件追加
  4. 変化があり last_extracted_at より新しい場合のみ Extractor 呼び出し
     → unreleased_cards に upsert（既存行は上書きしない）
  5. 1回の実行での抽出上限: MAX_EXTRACT_PAGES ページ
  6. ページ単位で例外捕捉して続行。全件失敗時は Discord 通知

HTTP取得: fetch_guard.fetch_whitelisted のみ使用（requests は直接 import しない）
"""

import hashlib
import logging
import os
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from supabase import create_client, Client

from fetch_guard import fetch_whitelisted, is_whitelisted

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 環境変数
# ──────────────────────────────────────────────

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# ──────────────────────────────────────────────
# 設定定数
# ──────────────────────────────────────────────

# 1実行あたりの Extractor 呼び出し上限（Claude API コスト暴走防止）
# 既定15。watcher.env の MAX_EXTRACT_PAGES で上書き可（新弾発表が重なる日だけ上げる等）。
# 上げるとその分だけ実行時間とAPIコストがページ数に比例して増えるため、無制限にはしない。
MAX_EXTRACT_PAGES = int(os.environ.get("MAX_EXTRACT_PAGES", "15"))

# ページ変化時に新規追加する最大リンク数
MAX_NEW_LINKS = 20

# 「カード画像はあるのにカード0件」ページの再抽出上限。
# 旧環境（curl_cffi/vision導入前）で抽出に失敗し0件のまま残ったページを
# 自動で拾い直すための上限。上限到達後は諦める（無限再試行・APIコスト暴走の防止）。
# コンテンツ変化時に extract_attempts は0へリセットされる。
MAX_EXTRACT_ATTEMPTS = 3

# ラッシュデュエル記事の除外キーワード（記事見出しで判定）。
# yu-gi-oh.jp は OCG とラッシュデュエルを同じ news_detail.php で配信するため、
# 価格比較対象外のラッシュ記事を抽出（Claude呼び出し）しないよう除外する。
_RUSH_KEYWORDS = ("ラッシュデュエル", "RUSH DUEL")

# 新規追加リンクのフィルタキーワード（小文字で一致確認）
_CARD_URL_KEYWORDS = (
    "product", "card", "news", "information", "howto",
    "booster", "pack", "deck", "set",
)

# SEED_URLS — 抽出対象は YU-GI-OH.jp（遊戯王ニュースサイト。2026-06-10 実在・到達確認済み）
# トップページがニュース一覧を兼ねており、記事は news_detail.php?page=details&id=NNNN 形式。
# 記事リンクは _CARD_URL_KEYWORDS の "news" に合致して自動発見される。
# anime.php / comic.php 等の非OCGセクションはキーワード不一致で自然に除外される。
SEED_URLS: list[str] = [
    "https://yu-gi-oh.jp/",  # トップ＝最新ニュース一覧
]


# ──────────────────────────────────────────────
# ユーティリティ
# ──────────────────────────────────────────────

def _sha256(text: str) -> str:
    """テキストのSHA-256ハッシュを返す"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_html(html: str) -> str:
    """差分検知用のHTMLテキスト正規化（script/style除去・空白統一）"""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    # 連続空白を単一スペースに統一
    import re
    return re.sub(r"\s+", " ", text).strip()


def _extract_candidate_links(html: str, base_url: str) -> list[str]:
    """ページ内の<a>リンクからホワイトリスト内かつカード/商品/ニュース系URLを抽出する"""
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    seen = set()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue

        # 相対URLを絶対URLに変換
        abs_url = urljoin(base_url, href)

        # フラグメントを除去
        abs_url = abs_url.split("#")[0]

        if abs_url in seen:
            continue
        seen.add(abs_url)

        # ホワイトリスト外はスキップ
        if not is_whitelisted(abs_url):
            continue

        # カード/商品/ニュース系キーワードでフィルタ
        path_lower = urlparse(abs_url).path.lower()
        if not any(kw in path_lower for kw in _CARD_URL_KEYWORDS):
            continue

        candidates.append(abs_url)

    return candidates


def _is_rush_duel(html: str) -> bool:
    """記事がラッシュデュエル（価格比較対象外）かを判定する。

    2段階で判定する:
      1. 記事見出し（h1〜h3）にラッシュキーワードが含まれるか（安価・短絡）
      2. ナビ/フッター/ヘッダー/サイドバーと全リンク(<a>)を除去した本文テキストに
         ラッシュキーワードが含まれるか

    見出しがロゴ画像や商品名のみでキーワード文字列を含まない記事は、見出し判定だけ
    では素通りするため本文テキストでも判定する。
    全ページ共通のラッシュデュエルへのリンクはヘッダー/フッター/ナビや本文中の <a> に
    存在するため、これらを除去してから判定し、本文の地の文で言及している記事のみを拾う
    （誤って OCG 記事をラッシュと誤判定して正規カードを取りこぼさないよう安全側に倒す）。
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    # 1. 記事見出しでの判定（安価なので先に評価）
    for sel in ("h1", "h2", "h3"):
        el = soup.find(sel)
        if not el:
            continue
        heading = el.get_text(strip=True)
        heading_up = heading.upper()
        if any(kw in heading or kw in heading_up for kw in _RUSH_KEYWORDS):
            return True

    # 2. 本文テキストでの判定。全ページ共通のラッシュリンクは <a> 内にあるため、
    #    aside（サイドバー）と全リンクを除去してから本文の地の文で判定する。
    for tag in soup.find_all(["aside", "a"]):
        tag.decompose()
    body_text = soup.get_text(separator=" ")
    body_up = body_text.upper()
    if any(kw in body_text or kw in body_up for kw in _RUSH_KEYWORDS):
        return True

    return False


def _notify_discord(message: str) -> None:
    """Discord Webhook へ通知を送る（DISCORD_WEBHOOK_URL が未設定なら何もしない）。
    HTTPライブラリ直接import禁止のAST検査を維持するため discord_notify モジュールに委譲する。
    """
    from discord_notify import send_discord_message
    send_discord_message(message)


# ──────────────────────────────────────────────
# watched_pages 操作
# ──────────────────────────────────────────────

def _seed_watched_pages(sb: Client) -> None:
    """SEED_URLS を watched_pages に投入する（冪等）。
    毎回実行し、既存行は ignore_duplicates で触らない
    （content_hash を上書きすると毎回「変化あり」と誤検知するため）。
    SEED_URLS を後から追加・変更しても次の実行で自動反映される。
    """
    rows = [
        {
            "url": url,
            "content_hash": "",
            "enabled": True,
        }
        for url in SEED_URLS
    ]
    sb.table("watched_pages").upsert(
        rows, on_conflict="url", ignore_duplicates=True
    ).execute()
    logger.info(f"[Watcher] SEED_URLS を確認・投入しました ({len(SEED_URLS)}件)")


def _get_enabled_pages(sb: Client) -> list[dict]:
    """enabled=true の watched_pages を全件取得する"""
    resp = sb.table("watched_pages").select("*").eq("enabled", True).execute()
    return resp.data or []


def _update_checked(sb: Client, url: str, content_hash: str) -> None:
    """last_checked_at と content_hash を更新する"""
    now = datetime.now(timezone.utc).isoformat()
    sb.table("watched_pages").update({
        "last_checked_at": now,
        "content_hash": content_hash,
    }).eq("url", url).execute()


def _update_changed(sb: Client, url: str, content_hash: str) -> None:
    """コンテンツ変化時に last_changed_at / last_checked_at / content_hash を更新する。
    新しい内容は抽出し直す価値があるため extract_attempts を0へリセットする。"""
    now = datetime.now(timezone.utc).isoformat()
    sb.table("watched_pages").update({
        "last_checked_at": now,
        "last_changed_at": now,
        "content_hash": content_hash,
        "extract_attempts": 0,
    }).eq("url", url).execute()


def _update_extracted(sb: Client, url: str, attempts: int) -> None:
    """Extractor処理完了時に last_extracted_at と extract_attempts を更新する。
    attempts には更新後の試行回数（今回の試行を含めた値）を渡す。"""
    now = datetime.now(timezone.utc).isoformat()
    sb.table("watched_pages").update({
        "last_extracted_at": now,
        "extract_attempts": attempts,
    }).eq("url", url).execute()


def _count_cards_for_url(sb: Client, url: str) -> int:
    """指定 source_url の unreleased_cards 件数を返す（再抽出要否の判定用）"""
    resp = sb.table("unreleased_cards").select("id", count="exact").eq("source_url", url).execute()
    return resp.count or 0


def _should_retry_empty(sb: Client, page: dict, html: str, url: str) -> bool:
    """「カード画像はあるのにカード0件」のページを再抽出すべきか判定する（原因の修正）。

    旧環境で抽出に失敗し0件のまま last_extracted_at が記録されたページは
    _extraction_pending では拾えない。カード画像が存在するのに DB が0件で、
    かつ試行回数が上限未満なら再抽出する。上限到達後は諦める（無限再試行防止）。
    """
    attempts = page.get("extract_attempts") or 0
    if attempts >= MAX_EXTRACT_ATTEMPTS:
        return False
    # カード画像抽出は extractor と同一ロジックを使う（単一情報源）
    from unreleased_extractor import _extract_card_image_urls
    if not _extract_card_image_urls(html, url):
        return False
    return _count_cards_for_url(sb, url) == 0


def _extraction_pending(page: dict) -> bool:
    """このページが「抽出保留」状態か判定する。

    変化検知済み（last_changed_at あり）だが、まだ一度も抽出していない
    （last_extracted_at が null）か、抽出後にさらに変化した場合に True。

    APIキー未設定だった時期に変化検知だけ記録され抽出されなかったページは
    last_extracted_at=null のまま残る。これを「変化なし」の早期スキップで
    取りこぼさないために使う（原因2の修正）。
    """
    last_changed = page.get("last_changed_at")
    if not last_changed:
        return False
    last_extracted = page.get("last_extracted_at")
    if not last_extracted:
        return True
    try:
        return last_changed > last_extracted
    except TypeError:
        # 比較できない場合は安全側（抽出する）に倒す
        return True


def _add_new_links(sb: Client, links: list[str]) -> list[str]:
    """新規リンクを watched_pages に追加する。既存URLはスキップ（upsert ignore）。
    戻り値: 追加対象とした URL のリスト（呼び出し側で同一実行のキューに積むため）
    """
    if not links:
        return []

    target_urls = links[:MAX_NEW_LINKS]
    rows = [{"url": url, "content_hash": "", "enabled": True} for url in target_urls]
    try:
        sb.table("watched_pages").upsert(rows, on_conflict="url", ignore_duplicates=True).execute()
        logger.info(f"[Watcher] 新規リンク追加: {len(rows)}件")
        return target_urls
    except Exception as e:
        logger.warning(f"[Watcher] リンク追加失敗: {e}")
        return []


# ──────────────────────────────────────────────
# unreleased_cards upsert
# ──────────────────────────────────────────────

def _upsert_cards(sb: Client, rows: list[dict]) -> int:
    """抽出カードを unreleased_cards に upsert する。既存行は上書きしない。
    戻り値: upsert 試行件数
    """
    if not rows:
        return 0

    inserted = 0
    for row in rows:
        try:
            sb.table("unreleased_cards").upsert(
                row,
                on_conflict="name,product_name",
                ignore_duplicates=True,
            ).execute()
            inserted += 1
        except Exception as e:
            logger.warning(f"[Watcher] upsert失敗 ({row.get('name', '?')}): {e}")

    logger.info(f"[Watcher] unreleased_cards upsert: {inserted}/{len(rows)}件")
    return inserted


# ──────────────────────────────────────────────
# メイン処理
# ──────────────────────────────────────────────

def main() -> None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.error("[Watcher] SUPABASE_URL / SUPABASE_KEY が未設定です")
        return

    sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

    # 1. SEED_URLS を冪等投入してから監視対象ページを取得
    #    （空チェック方式だと他URLが残っている場合にシードが入らない）
    _seed_watched_pages(sb)
    pages = _get_enabled_pages(sb)

    logger.info(f"[Watcher] 監視ページ数: {len(pages)}件")

    extract_count = 0      # 今回の抽出実行回数
    fail_count = 0         # ページ単位の失敗数
    total_inserted = 0     # 合計 INSERT 件数

    # キュー方式で巡回する。ループ中に新規発見したページを同一実行内で
    # 処理するため、固定リストではなく先頭から取り出すキューを使う（原因1の修正）。
    # seen_urls で同一実行内の重複処理を防ぐ。
    queue: list[dict] = [p for p in pages if p.get("url")]
    seen_urls: set[str] = {p["url"] for p in queue}

    while queue:
        page = queue.pop(0)
        url = page.get("url", "")
        if not url:
            continue

        logger.info(f"[Watcher] 処理中: {url}")

        try:
            # 2. ページ取得
            resp = fetch_whitelisted(url)
            if resp.status_code != 200:
                logger.warning(f"[Watcher] HTTP {resp.status_code}: {url}")
                fail_count += 1
                continue

            html = resp.text
            normalized = _normalize_html(html)
            new_hash = _sha256(normalized)
            prev_hash = page.get("content_hash", "")
            is_change = not (new_hash == prev_hash and prev_hash)

            # ラッシュデュエル記事は価格比較対象外。巡回・差分記録はするが
            # Extractor（Claude呼び出し）は行わない（修正1）。
            if _is_rush_duel(html):
                logger.info(f"[Watcher] ラッシュデュエル記事のため抽出スキップ: {url}")
                if is_change:
                    _update_changed(sb, url, new_hash)
                else:
                    _update_checked(sb, url, new_hash)
                continue

            if is_change:
                # 変化あり（または初回）
                logger.info(f"[Watcher] コンテンツ変化を検知: {url}")
                _update_changed(sb, url, new_hash)

                # 3. 新規リンクを抽出して追加（最大MAX_NEW_LINKS件）
                #    追加した URL は同一実行のキューへ積む（原因1の修正）
                candidate_links = _extract_candidate_links(html, url)
                logger.info(f"[Watcher] リンク候補: {len(candidate_links)}件")
                added_urls = _add_new_links(sb, candidate_links)
                for new_url in added_urls:
                    if new_url not in seen_urls:
                        seen_urls.add(new_url)
                        queue.append({"url": new_url, "content_hash": ""})
            else:
                # 変化なし。ただし以下は抽出に進む:
                #   (a) 抽出保留（変化検知済みだが未抽出。APIキー未設定時期の取りこぼし等）
                #   (b) カード画像があるのにDB0件（旧環境で抽出失敗・上限内）（修正2）
                _update_checked(sb, url, new_hash)
                if _extraction_pending(page):
                    logger.info(f"[Watcher] 変化なしだが未抽出のため抽出に進む: {url}")
                elif _should_retry_empty(sb, page, html, url):
                    logger.info(f"[Watcher] 変化なしだがカード画像あり/DB0件のため再抽出: {url}")
                else:
                    logger.info(f"[Watcher] 変化なし: {url}")
                    continue

            # 4. Extractor 呼び出し上限チェック
            if extract_count >= MAX_EXTRACT_PAGES:
                logger.info(
                    f"[Watcher] 抽出上限({MAX_EXTRACT_PAGES}件)到達。"
                    f"{url} は次回実行まで保留"
                )
                continue

            # APIキー未設定時は抽出をスキップ（巡回・差分検知は継続。
            # last_extracted_at を更新しないため、キー設定後の実行で
            # _extraction_pending=True となり抽出される）
            if not os.environ.get("ANTHROPIC_API_KEY"):
                logger.warning(
                    f"[Watcher] ANTHROPIC_API_KEY 未設定のため抽出をスキップ: {url}"
                )
                continue

            # Extractor 呼び出し
            logger.info(f"[Watcher] Extractor 実行: {url}")
            from unreleased_extractor import extract_cards_from_html
            card_rows = extract_cards_from_html(html, url)
            extract_count += 1

            # 5. unreleased_cards に upsert（既存行は上書きしない）
            n_inserted = _upsert_cards(sb, card_rows)
            total_inserted += n_inserted
            _update_extracted(sb, url, (page.get("extract_attempts") or 0) + 1)

            logger.info(
                f"[Watcher] 完了: {url} "
                f"| 抽出={len(card_rows)}件 | 挿入={n_inserted}件"
            )

        except Exception as e:
            fail_count += 1
            logger.exception(f"[Watcher] 例外: {url} → {e}")
            # 次のページの処理を継続

    # 6. 全件失敗時は Discord 通知
    logger.info(
        f"[Watcher] 実行完了: 抽出={extract_count}ページ, "
        f"挿入合計={total_inserted}件, 失敗={fail_count}件"
    )
    if fail_count > 0 and fail_count >= len(pages):
        msg = (
            f"[watch_unreleased] 全ページ失敗 ({fail_count}/{len(pages)}件)\n"
            f"GitHub Actions のログを確認してください。"
        )
        logger.error(msg)
        _notify_discord(msg)


if __name__ == "__main__":
    main()
