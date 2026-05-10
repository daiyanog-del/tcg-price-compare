"""X（Twitter）自動投稿モジュール — 値動きランキングを毎日投稿"""

import os
import tempfile
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict

import requests as _requests

SITE_URL = "https://tcg-price-compare.onrender.com"
JST = timezone(timedelta(hours=9))


def get_price_movers(sb, direction="up", limit=5):
    """Supabaseから値上がり/値下がりランキングを取得"""
    cutoff = (datetime.now(JST) - timedelta(days=3)).strftime("%Y-%m-%d")

    all_rows = []
    page_size = 1000
    offset = 0
    while True:
        resp = (sb.table("price_history")
                .select("card_name, min_price, recorded_at")
                .gte("recorded_at", cutoff)
                .order("recorded_at", desc=False)
                .range(offset, offset + page_size - 1)
                .execute())
        batch = resp.data or []
        all_rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size

    if not all_rows:
        return [], None, None

    # 日付ごと・カードごとの最安値（10円以下は除外）
    card_dates = defaultdict(dict)
    for r in all_rows:
        name = r["card_name"]
        d = r["recorded_at"][:10]
        price = r["min_price"]
        if price <= 10:
            continue
        if d not in card_dates[name] or price < card_dates[name][d]:
            card_dates[name][d] = price

    # 全カード共通で比較に使う2日分を特定
    all_dates_set = set()
    for dates in card_dates.values():
        all_dates_set.update(dates.keys())
    all_dates_sorted = sorted(all_dates_set)
    if len(all_dates_sorted) < 2:
        return [], None, None
    date_new = all_dates_sorted[-1]
    date_old = all_dates_sorted[-2]

    movers = []
    for name, dates in card_dates.items():
        if date_new not in dates or date_old not in dates:
            continue
        today_price = dates[date_new]
        yesterday_price = dates[date_old]
        if yesterday_price == 0:
            continue
        diff = today_price - yesterday_price
        if diff == 0:
            continue
        pct = round((diff / yesterday_price) * 100, 1)
        movers.append({
            "name": name, "today": today_price,
            "yesterday": yesterday_price, "diff": diff, "pct": pct,
        })

    # 50円以上の変動を優先、なければ最大変動の1枚のみ投稿
    up_all = sorted([m for m in movers if m["diff"] > 0], key=lambda x: -x["pct"])
    down_all = sorted([m for m in movers if m["diff"] < 0], key=lambda x: x["pct"])
    if direction == "up":
        filtered = [m for m in up_all if abs(m["diff"]) >= 50]
        result = filtered[:limit] if filtered else up_all[:1]
    else:
        filtered = [m for m in down_all if abs(m["diff"]) >= 50]
        result = filtered[:limit] if filtered else down_all[:1]
    return result, date_old, date_new


def _truncate(name, max_len=18):
    """長いカード名を省略"""
    return name if len(name) <= max_len else name[:max_len] + "..."


def _format_date(date_str):
    """'2026-03-25' → '3/25' 形式に変換"""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{d.month}/{d.day}"


_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def _download_image(image_url, label, referer=None):
    """画像URLをダウンロードして一時ファイルパスを返す。失敗時はNone"""
    try:
        headers = {"User-Agent": _BROWSER_UA}
        if referer:
            headers["Referer"] = referer

        # 最大2回リトライ
        session = _requests.Session()
        adapter = _requests.adapters.HTTPAdapter(max_retries=2)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        img_resp = session.get(image_url, timeout=15, headers=headers)
        ct = img_resp.headers.get("Content-Type", "不明")
        cl = img_resp.headers.get("Content-Length", "不明")
        print(f"  [DL] {label}: HTTP {img_resp.status_code} Content-Type={ct} Content-Length={cl}")
        if img_resp.status_code != 200:
            print(f"  カード画像ダウンロード失敗 [{label}]: HTTP {img_resp.status_code}")
            return None

        body = img_resp.content
        magic = body[:4].hex() if body else "空"
        print(f"  [DL] {label}: 先頭バイト={magic} 実サイズ={len(body)}B")

        # 1KB未満はエラーページの可能性が高いため弾く
        if len(body) < 1024:
            print(f"  カード画像ダウンロード失敗 [{label}]: サイズが小さすぎる ({len(body)}B)")
            return None

        # Content-Typeから拡張子を決定（tweepy/X APIは中身で判定するが念のため合わせる）
        if "png" in ct:
            suffix = ".png"
        elif "webp" in ct:
            suffix = ".webp"
        elif "gif" in ct:
            suffix = ".gif"
        else:
            suffix = ".jpg"

        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp.write(body)
        tmp.close()
        print(f"  カード画像取得成功 [{label}] suffix={suffix} 保存先={tmp.name}")
        return tmp.name
    except Exception as e:
        print(f"  カード画像ダウンロード失敗 [{label}]: {e}")
        return None


def _get_yugipedia_image_url(card_name):
    """Yugipedia MediaWiki APIでカード画像URLを取得。失敗時はNone"""
    try:
        resp = _requests.get(
            "https://yugipedia.com/api.php",
            params={
                "action": "query",
                "titles": card_name,
                "prop": "pageimages",
                "pithumbsize": 400,
                "redirects": 1,
                "format": "json",
            },
            timeout=10,
            headers={"User-Agent": _BROWSER_UA},
        )
        if resp.status_code != 200:
            print(f"  Yugipedia失敗 [{card_name}]: HTTP {resp.status_code}")
            return None
        pages = resp.json().get("query", {}).get("pages", {})
        for page in pages.values():
            src = page.get("thumbnail", {}).get("source")
            if src:
                return src
        print(f"  Yugipedia画像なし [{card_name}]")
    except Exception as e:
        print(f"  Yugipedia失敗 [{card_name}]: {e}")
    return None


def get_card_image_path(card_name):
    """カード画像を取得し一時ファイルパスを返す。失敗時はNone。
    取得元の優先順位: Renderプロキシ(カーナベル) → YGOPRODECK → Yugipedia
    """
    # 1. RenderサーバーのプロキシAPI経由でカーナベル画像を取得
    #    （GitHub ActionsのIPはカーナベルにブロックされるため、Renderを中継役にする）
    try:
        proxy_base = os.environ.get("IMAGE_PROXY_URL", "https://tcg-price-compare.onrender.com")
        proxy_url = f"{proxy_base}/api/card-image-proxy?name={urllib.parse.quote(card_name)}"
        print(f"  [プロキシ] {card_name}: {proxy_url}")
        path = _download_image(proxy_url, card_name)
        if path:
            return path
        print(f"  Renderプロキシ失敗 [{card_name}] — YGOPRODECKで再試行")
    except Exception as e:
        print(f"  Renderプロキシ失敗 [{card_name}]: {e} — YGOPRODECKで再試行")

    # 2. YGOPRODECKにフォールバック
    try:
        api_url = "https://db.ygoprodeck.com/api/v7/cardinfo.php"
        resp = _requests.get(
            api_url,
            params={"name": card_name, "language": "ja"},
            timeout=10,
            headers={"User-Agent": _BROWSER_UA},
        )
        if resp.status_code == 200:
            cards = resp.json().get("data", [])
            if cards:
                image_url = cards[0]["card_images"][0]["image_url"]
                return _download_image(image_url, card_name)
        print(f"  YGOPRODECK失敗 [{card_name}]: HTTP {resp.status_code} — Yugipediaで再試行")
    except Exception as e:
        print(f"  YGOPRODECK失敗 [{card_name}]: {e} — Yugipediaで再試行")

    # 3. Yugipediaにフォールバック（新カードや日本語名対応）
    image_url = _get_yugipedia_image_url(card_name)
    if image_url:
        return _download_image(image_url, card_name)

    return None


def format_tweet(movers, direction, date_old, date_new):
    """投稿テキストを生成"""
    label = "値上がり" if direction == "up" else "値下がり"
    period = f"{_format_date(date_old)}→{_format_date(date_new)}"
    tags = "#遊戯王 #遊戯王高騰" if direction == "up" else "#遊戯王 #遊戯王相場"

    # No.1カードの個別ページへ直リンク（末尾固定）
    encoded_name = urllib.parse.quote(movers[0]["name"])
    cta = f"\n▶ 最安値を比較する\n{SITE_URL}/card/{encoded_name}"

    lines = [f"【{label}カード】{period}\n"]
    for i, m in enumerate(movers):
        sign = "+" if m["diff"] > 0 else ""
        name = _truncate(m["name"])
        lines.append(
            f"{i+1}. {name} {sign}{m['pct']}%"
            f"({m['yesterday']:,}→{m['today']:,}円)"
        )
    lines.append(cta)
    lines.append(tags)
    text = "\n".join(lines)

    # 280文字を超える場合、末尾のカードから削って収める
    while len(text) > 280 and len(movers) > 1:
        movers.pop()
        lines = [f"【{label}カード】{period}\n"]
        for i, m in enumerate(movers):
            sign = "+" if m["diff"] > 0 else ""
            name = _truncate(m["name"])
            lines.append(
                f"{i+1}. {name} {sign}{m['pct']}%"
                f"({m['yesterday']:,}→{m['today']:,}円)"
            )
        lines.append(cta)
        lines.append(tags)
        text = "\n".join(lines)

    return text


def post_tweet(text, image_path=None, reply_to_id=None):
    """X API v2でツイートを投稿。成功時はtweet_idを返す、失敗時はNone"""
    api_key = os.environ.get("X_API_KEY")
    api_secret = os.environ.get("X_API_SECRET")
    access_token = os.environ.get("X_ACCESS_TOKEN")
    access_token_secret = os.environ.get("X_ACCESS_TOKEN_SECRET")

    if not all([api_key, api_secret, access_token, access_token_secret]):
        print("  X API認証情報が未設定のためスキップ")
        return None

    try:
        import tweepy
        print(f"  [X] tweepyバージョン: {tweepy.__version__}")

        # メディアアップロード（v1.1 API経由）
        media_id_str = None
        media_status = "なし"  # "添付済" / "アップロード失敗" / "なし"
        if image_path:
            try:
                import os as _os
                file_size = _os.path.getsize(image_path)
                with open(image_path, "rb") as f:
                    file_magic = f.read(4).hex()
                print(f"  [X] 画像ファイル確認: size={file_size}B magic={file_magic}")
                auth = tweepy.OAuth1UserHandler(api_key, api_secret, access_token, access_token_secret)
                api_v1 = tweepy.API(auth)
                media = api_v1.media_upload(image_path)
                media_id_str = str(media.media_id)  # v2 APIは文字列IDを要求
                media_status = "添付済"
                print(f"  メディアアップロード成功 (media_id={media_id_str})")
            except Exception as e:
                media_status = "アップロード失敗"
                print(f"  メディアアップロード失敗: {type(e).__name__}: {e}")
                if hasattr(e, "response") and e.response is not None:
                    print(f"    HTTPステータス={e.response.status_code}")
                    print(f"    レスポンス本文={e.response.text[:500]}")
                if hasattr(e, "api_codes"):
                    print(f"    api_codes={e.api_codes} api_messages={e.api_messages}")
                # 画像なしで投稿続行

        # DRY_RUNモード: 投稿直前でスキップ（ログのみ出力）
        if os.environ.get("X_POST_DRY_RUN") == "1":
            print(f"  [DRY_RUN] 投稿スキップ (画像={media_status}) text_len={len(text)}")
            print(f"  [DRY_RUN] 本文先頭: {text[:80]}")
            return None

        # ツイート投稿（v2 API）
        client = tweepy.Client(
            consumer_key=api_key,
            consumer_secret=api_secret,
            access_token=access_token,
            access_token_secret=access_token_secret,
        )
        kwargs = {"text": text}
        if media_id_str:
            kwargs["media_ids"] = [media_id_str]
        if reply_to_id:
            kwargs["in_reply_to_tweet_id"] = str(reply_to_id)

        response = client.create_tweet(**kwargs)
        tweet_id = response.data["id"] if response.data else None
        print(f"  投稿成功（画像{media_status}） (id={tweet_id}): {text[:50]}...")
        return tweet_id
    except Exception as e:
        print(f"  ツイート投稿失敗: {type(e).__name__}: {e}")
        if hasattr(e, "response") and e.response is not None:
            print(f"    HTTPステータス={e.response.status_code}")
            print(f"    レスポンス本文={e.response.text[:500]}")
        return None


def post_daily_movers(sb):
    """毎日の値動きランキングをXに投稿（値上がり→値下がりのスレッド形式）"""
    print("\n=== X自動投稿 ===")

    up_tweet_id = None

    for direction in ("up", "down"):
        movers, date_old, date_new = get_price_movers(sb, direction, limit=5)
        if not movers:
            label = "値上がり" if direction == "up" else "値下がり"
            print(f"  {label}データなし — スキップ")
            continue

        text = format_tweet(movers, direction, date_old, date_new)
        print(f"\n--- {direction} ---")
        print(text)

        # NO.1カードの画像を取得
        image_path = get_card_image_path(movers[0]["name"])

        # 値下がりは値上がりへのリプライとして投稿
        reply_to = up_tweet_id if direction == "down" else None
        tweet_id = post_tweet(text, image_path=image_path, reply_to_id=reply_to)

        # 一時ファイルを削除
        if image_path:
            try:
                os.unlink(image_path)
            except Exception:
                pass

        if direction == "up":
            up_tweet_id = tweet_id
