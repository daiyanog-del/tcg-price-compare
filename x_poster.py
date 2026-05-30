"""X（Twitter）自動投稿モジュール — 値動きランキングを毎日投稿"""

import os
import tempfile
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict

import requests as _requests

SITE_URL = "https://tcg-price-compare.onrender.com"
JST = timezone(timedelta(hours=9))


def get_price_movers(sb, direction="up", limit=5, allowed_names=None):
    """Supabaseから値上がり/値下がりランキングを取得。
    allowed_names: set または None。指定すると対象カードを絞り込む（新弾フィーチャー用）。
    """
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
    # allowed_names が指定された場合はそのカード集合に絞る
    card_dates = defaultdict(dict)
    for r in all_rows:
        name = r["card_name"]
        if allowed_names is not None and name not in allowed_names:
            continue
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

# YGOResources セッション内キャッシュ（プロセス起動時に一度だけ取得）
_ygoresources_name_index = None  # {カード名: [konami_id, ...]}
_ygoresources_manifest = None    # {konami_id_str: {artwork_id: {...}}}


def _get_ygoresources_name_index():
    """db.ygoresources.com から日本語カード名→カードID対応表を取得（セッションキャッシュ）"""
    global _ygoresources_name_index
    if _ygoresources_name_index is not None:
        return _ygoresources_name_index
    try:
        resp = _requests.get(
            "https://db.ygoresources.com/data/idx/card/name/ja",
            timeout=30,
            headers={"User-Agent": _BROWSER_UA},
        )
        if resp.status_code == 200:
            _ygoresources_name_index = resp.json()
            print(f"  [YGOResources] 名前インデックス取得: {len(_ygoresources_name_index)}件")
        else:
            print(f"  [YGOResources] 名前インデックス取得失敗: HTTP {resp.status_code}")
            _ygoresources_name_index = {}
    except Exception as e:
        print(f"  [YGOResources] 名前インデックス取得エラー: {e}")
        _ygoresources_name_index = {}
    return _ygoresources_name_index


def _get_ygoresources_manifest():
    """artworks.ygoresources.com のmanifest.jsonを取得（セッションキャッシュ、約20MB）"""
    global _ygoresources_manifest
    if _ygoresources_manifest is not None:
        return _ygoresources_manifest
    try:
        print("  [YGOResources] manifest.json ダウンロード中（約20MB）...")
        resp = _requests.get(
            "https://artworks.ygoresources.com/manifest.json",
            timeout=120,
            headers={"User-Agent": _BROWSER_UA},
        )
        if resp.status_code == 200:
            _ygoresources_manifest = resp.json().get("cards", {})
            print(f"  [YGOResources] manifest.json 取得完了: {len(_ygoresources_manifest)}件")
        else:
            print(f"  [YGOResources] manifest.json 取得失敗: HTTP {resp.status_code}")
            _ygoresources_manifest = {}
    except Exception as e:
        print(f"  [YGOResources] manifest.json 取得エラー: {e}")
        _ygoresources_manifest = {}
    return _ygoresources_manifest


def _get_ygoresources_image_url(card_name):
    """YGOResources から OCG カード画像 URL を取得。OCG最新カード対応。失敗時は None"""
    name_index = _get_ygoresources_name_index()
    card_ids = name_index.get(card_name)
    if not card_ids:
        print(f"  [YGOResources] 名前未発見: {card_name}")
        return None
    card_id = str(card_ids[0])
    print(f"  [YGOResources] カードID: {card_name} → {card_id}")

    manifest = _get_ygoresources_manifest()
    card_entry = manifest.get(card_id)
    if not card_entry:
        print(f"  [YGOResources] マニフェスト未発見: card_id={card_id}")
        return None

    # artworkId "1" を優先（通常は基本アートワーク）
    artwork_key = "1" if "1" in card_entry else next(iter(card_entry), None)
    if not artwork_key:
        return None
    artwork = card_entry[artwork_key]

    # OCG日本語版（neuron_high）→ bestOCG → bestArt の順で優先
    path = None
    for entry in artwork.get("idx", {}).get("ja", []):
        path = entry.get("path")
        if path:
            break
    if not path:
        path = artwork.get("bestOCG") or artwork.get("bestArt") or artwork.get("bestTCG")

    if not path:
        print(f"  [YGOResources] パス未発見: card_id={card_id}")
        return None

    # プロトコル相対URL（//host/path）→ https:
    if path.startswith("//"):
        path = "https:" + path
    elif path.startswith("/"):
        path = "https://artworks.ygoresources.com" + path

    print(f"  [YGOResources] 画像URL: {path}")
    return path


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


def _get_yugipedia_en_name(card_name):
    """Yugipedia APIで日本語カード名から英語ページ名を取得。
    日本語名で検索すると英語ページにリダイレクトされるため、その先のタイトルを英語名として使う。
    失敗時はNone。
    """
    try:
        resp = _requests.get(
            "https://yugipedia.com/api.php",
            params={
                "action": "query",
                "titles": card_name,
                "redirects": 1,
                "format": "json",
            },
            timeout=10,
            headers={"User-Agent": _BROWSER_UA},
        )
        if resp.status_code != 200:
            return None
        data = resp.json().get("query", {})
        # リダイレクト情報から英語名を取得
        for r in data.get("redirects", []):
            to = r.get("to", "")
            if to and to != card_name:
                print(f"  [英語名] {card_name} → {to}")
                return to
        # リダイレクトがなくても、ページが見つかっていれば英語タイトルを使う
        for page in data.get("pages", {}).values():
            title = page.get("title", "")
            if title and title != card_name and page.get("pageid", -1) != -1:
                print(f"  [英語名] {card_name} → {title}")
                return title
    except Exception as e:
        print(f"  Yugipedia英語名取得失敗 [{card_name}]: {e}")
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


def _get_ygoprodeck_image_url(name, language=None):
    """YGOPRODECK APIで画像URLを取得。英語名なら確実、日本語名はlanguage='ja'で試みる。失敗時はNone"""
    try:
        params = {"name": name}
        if language:
            params["language"] = language
        resp = _requests.get(
            "https://db.ygoprodeck.com/api/v7/cardinfo.php",
            params=params,
            timeout=10,
            headers={"User-Agent": _BROWSER_UA},
        )
        if resp.status_code == 200:
            cards = resp.json().get("data", [])
            if cards:
                return cards[0]["card_images"][0]["image_url"]
        print(f"  YGOPRODECK失敗 [{name}]: HTTP {resp.status_code}")
    except Exception as e:
        print(f"  YGOPRODECK失敗 [{name}]: {e}")
    return None


def get_card_image_path(card_name):
    """カード画像を取得し一時ファイルパスを返す。失敗時はNone。

    取得フロー:
    1. YGOResources（OCG最新カード含む全カード対応、Cloudflare CDN）
    2. Yugipediaで日本語名→英語名に変換 → YGOPRODECKで英語名検索
    3. Yugipediaで英語名から直接画像取得
    4. 日本語名でYugipedia pageimages（英語変換失敗時のフォールバック）
    5. 日本語名でYGOPRODECK（language=jaで一部カード対応）

    カーナベル画像はクラウドIPからのアクセスがIPレベルでブロックされるため使用しない。
    """
    # 1. YGOResources（OCG最新カード含む・Cloudflare CDN経由でIPブロックなし）
    url = _get_ygoresources_image_url(card_name)
    if url:
        path = _download_image(url, f"YGOResources:{card_name}")
        if path:
            return path

    # 2. Yugipedia で日本語名→英語名変換 → YGOPRODECK で画像取得
    en_name = _get_yugipedia_en_name(card_name)
    if en_name:
        url = _get_ygoprodeck_image_url(en_name)
        if url:
            path = _download_image(url, card_name)
            if path:
                return path
        # YGOPRODECK失敗時はYugipedia英語名で画像取得を試みる
        url = _get_yugipedia_image_url(en_name)
        if url:
            path = _download_image(url, card_name)
            if path:
                return path

    # 4. 日本語名でYugipedia pageimages（英語名変換失敗時）
    url = _get_yugipedia_image_url(card_name)
    if url:
        path = _download_image(url, card_name)
        if path:
            return path

    # 5. 日本語名でYGOPRODECK（language=ja、一部カードで有効）
    url = _get_ygoprodeck_image_url(card_name, language="ja")
    if url:
        path = _download_image(url, card_name)
        if path:
            return path

    print(f"  全ソースで画像取得失敗 [{card_name}]")
    return None


def format_tweet(movers, direction, date_old, date_new):
    """投稿テキストを生成。リンクはCTAリプライ側に分離しリーチを最大化する。"""
    label = "値上がり" if direction == "up" else "値下がり"
    period = f"{_format_date(date_old)}→{_format_date(date_new)}"
    tags = "#遊戯王 #遊戯王高騰" if direction == "up" else "#遊戯王 #遊戯王相場"

    lines = [f"【{label}カード】{period}\n"]
    for i, m in enumerate(movers):
        sign = "+" if m["diff"] > 0 else ""
        name = _truncate(m["name"])
        lines.append(
            f"{i+1}. {name} {sign}{m['pct']}%"
            f"({m['yesterday']:,}→{m['today']:,}円)"
        )
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
        lines.append(tags)
        text = "\n".join(lines)

    return text


def post_tweet(text, image_paths=None, reply_to_id=None):
    """X API v2でツイートを投稿。成功時はtweet_idを返す、失敗時はNone。
    image_paths: ファイルパスのリスト（最大4枚）。Noneまたは空リストで画像なし。
    """
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

        # メディアアップロード（v1.1 API経由、最大4枚）
        media_ids = []
        media_status = "なし"
        if image_paths:
            auth = tweepy.OAuth1UserHandler(api_key, api_secret, access_token, access_token_secret)
            api_v1 = tweepy.API(auth)
            for image_path in image_paths:
                try:
                    import os as _os
                    file_size = _os.path.getsize(image_path)
                    with open(image_path, "rb") as f:
                        file_magic = f.read(4).hex()
                    print(f"  [X] 画像ファイル確認: size={file_size}B magic={file_magic}")
                    media = api_v1.media_upload(image_path)
                    media_ids.append(str(media.media_id))
                    print(f"  メディアアップロード成功 (media_id={media_ids[-1]})")
                except Exception as e:
                    print(f"  メディアアップロード失敗: {type(e).__name__}: {e}")
                    if hasattr(e, "response") and e.response is not None:
                        print(f"    HTTPステータス={e.response.status_code}")
                        print(f"    レスポンス本文={e.response.text[:500]}")
                    if hasattr(e, "api_codes"):
                        print(f"    api_codes={e.api_codes} api_messages={e.api_messages}")
            media_status = f"添付済{len(media_ids)}枚" if media_ids else "アップロード失敗"

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
        if media_ids:
            kwargs["media_ids"] = media_ids
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

        # 上位2件の画像を取得
        image_paths = []
        for m in movers[:2]:
            p = get_card_image_path(m["name"])
            if p:
                image_paths.append(p)

        # 値下がりは値上がりへのリプライとして投稿
        reply_to = up_tweet_id if direction == "down" else None
        tweet_id = post_tweet(text, image_paths=image_paths, reply_to_id=reply_to)

        # 一時ファイルを削除
        for p in image_paths:
            try:
                os.unlink(p)
            except Exception:
                pass

        # 投稿成功時は tweet_log に記録（インプ計測用）
        if tweet_id and sb:
            content_type = f"movers_{direction}"
            try:
                sb.table("tweet_log").insert({
                    "tweet_id": str(tweet_id),
                    "posted_at": datetime.now(JST).isoformat(),
                    "content_type": content_type,
                    "parent_tweet_id": str(reply_to) if reply_to else None,
                }).execute()
                print(f"  tweet_log 記録済み (type={content_type})")
            except Exception as e:
                print(f"  tweet_log 書き込み失敗（投稿自体は成功）: {e}")

        # CTAリンクをリプライとして投稿（本文リンクなし化によるリーチ向上との両立）
        if tweet_id:
            encoded_name = urllib.parse.quote(movers[0]["name"])
            cta_text = f"▶ 最安値を比較する\n{SITE_URL}/card/{encoded_name}"
            cta_id = post_tweet(cta_text, reply_to_id=tweet_id)
            print(f"  CTAリプライ投稿{'成功' if cta_id else '失敗'} (parent={tweet_id})")

        if direction == "up":
            up_tweet_id = tweet_id


def format_featured_tweet(movers, pack_name, days_since_release, date_old=None, date_new=None):
    """新弾フィーチャー用の投稿テキストを生成。"""
    day_label = f"発売{days_since_release + 1}日目" if days_since_release >= 0 else "発売日"
    header = f"【{pack_name[:20]} 値動き】{day_label}\n"

    if date_old and date_new:
        period = f"{_format_date(date_old)}→{_format_date(date_new)}"
        header = f"【{pack_name[:20]} 値動き】{day_label} {period}\n"

    lines = [header]
    for i, m in enumerate(movers):
        sign = "+" if m["diff"] > 0 else ""
        name = _truncate(m["name"])
        lines.append(
            f"{i+1}. {name} {sign}{m['pct']}%"
            f"({m['yesterday']:,}→{m['today']:,}円)"
        )
    lines.append("#遊戯王 #高騰")
    text = "\n".join(lines)

    # 280文字を超える場合、末尾のカードから削って収める
    while len(text) > 280 and len(movers) > 1:
        movers.pop()
        lines = [header]
        for i, m in enumerate(movers):
            sign = "+" if m["diff"] > 0 else ""
            name = _truncate(m["name"])
            lines.append(
                f"{i+1}. {name} {sign}{m['pct']}%"
                f"({m['yesterday']:,}→{m['today']:,}円)"
            )
        lines.append("#遊戯王 #高騰")
        text = "\n".join(lines)

    return text


def format_initial_tweet(movers, pack_name):
    """発売当日用（値動きなし）の初値ランキング投稿テキストを生成。"""
    header = f"【{pack_name[:20]} 初値】発売日\n"
    lines = [header]
    for i, m in enumerate(movers):
        name = _truncate(m["name"])
        lines.append(f"{i+1}. {name} {m['today']:,}円")
    lines.append("#遊戯王 #新弾")
    text = "\n".join(lines)

    while len(text) > 280 and len(movers) > 1:
        movers.pop()
        lines = [header]
        for i, m in enumerate(movers):
            name = _truncate(m["name"])
            lines.append(f"{i+1}. {name} {m['today']:,}円")
        lines.append("#遊戯王 #新弾")
        text = "\n".join(lines)

    return text


def post_featured_movers(sb):
    """
    新弾フィーチャー投稿: 運用ウィンドウ内の新弾の値動きトップ5をスレッドで投稿。

    処理の流れ:
      1. 運用対象の新弾を確認（ウィンドウ外なら即return）
      2. 収録カード名を取得（Wikiスクレイピング）
      3. 発売当日は初値ランキング、2日目以降は値動きランキングを投稿
      4. 親ツイート（リスト+グラフ+写真）→ CTA リプライ（/featured への誘導）
    """
    from featured_pack import (
        get_featured_pack, is_within_window, get_days_since_release,
        get_featured_cards, get_initial_prices, get_card_history_since,
    )
    from chart_renderer import render_price_chart

    print("\n=== 新弾フィーチャー投稿 ===")

    # 運用ウィンドウの確認
    pack = get_featured_pack(sb)
    if not pack:
        print("  運用対象の新弾なし — スキップ")
        return

    if not is_within_window(pack):
        print(f"  {pack['pack_name']} は運用ウィンドウ外 — スキップ")
        return

    pack_name = pack["pack_name"]
    days = get_days_since_release(pack)
    print(f"  対象: {pack_name} (発売{days + 1}日目)")

    # 収録カード一覧を取得
    featured_cards = get_featured_cards(sb, pack)
    if not featured_cards:
        # Wiki未整備等でカードが取得できない場合は Discord 通知してスキップ
        discord_url = os.environ.get("DISCORD_WEBHOOK_URL")
        if discord_url:
            try:
                import requests as _req
                _req.post(discord_url, json={
                    "content": f"[新弾フィーチャー] {pack_name} の収録カードが取得できませんでした。"
                                "Wiki整備待ちの可能性があります。"
                }, timeout=5)
            except Exception:
                pass
        print(f"  収録カード取得失敗 — スキップ（Discord通知済み）")
        return

    allowed = set(featured_cards)

    # 発売当日（days=0）は値動きがないため初値ランキング、それ以降は値動きランキング
    if days == 0:
        print("  発売当日モード: 初値ランキング")
        initial_movers = get_initial_prices(sb, featured_cards)
        if not initial_movers:
            print("  初値データなし — スキップ")
            return
        movers_for_tweet = list(initial_movers[:5])  # ← format_initial_tweet で pop される可能性のためコピー
        text = format_initial_tweet(movers_for_tweet, pack_name)
        top_cards = [m["name"] for m in movers_for_tweet[:2]]
        date_old = date_new = None
    else:
        print("  通常モード: 値動きランキング")
        movers, date_old, date_new = get_price_movers(sb, direction="up", limit=5, allowed_names=allowed)
        if not movers:
            print("  値動きデータなし — スキップ（まだ2日分のデータが揃っていない可能性）")
            return
        movers_for_tweet = list(movers)
        text = format_featured_tweet(movers_for_tweet, pack_name, days, date_old, date_new)
        top_cards = [m["name"] for m in movers_for_tweet[:2]]

    print(f"\n--- 投稿本文 ---\n{text}\n")

    # 上位2枚の画像（グラフ＋カード写真）を取得
    chart_paths = []
    photo_paths = []

    for card_name in top_cards[:1]:  # グラフは1枚目のカードのみ
        history = get_card_history_since(sb, card_name, pack["start_date"])
        chart_path = render_price_chart(card_name, history)
        if chart_path:
            chart_paths.append(chart_path)

    for card_name in top_cards:  # カード写真は上位2枚
        photo_path = get_card_image_path(card_name)
        if photo_path:
            photo_paths.append(photo_path)

    # 画像を chart→photo の順に並べる（最大4枚）
    image_paths = (chart_paths + photo_paths)[:4]

    # 親ツイートを投稿
    tweet_id = post_tweet(text, image_paths=image_paths)

    # 一時ファイルを削除
    for p in image_paths:
        try:
            os.unlink(p)
        except Exception:
            pass

    # 投稿成功時の記録と CTA リプライ
    if tweet_id and sb:
        try:
            sb.table("tweet_log").insert({
                "tweet_id": str(tweet_id),
                "posted_at": datetime.now(JST).isoformat(),
                "content_type": "featured_movers",
                "parent_tweet_id": None,
            }).execute()
            print(f"  tweet_log 記録済み (type=featured_movers)")
        except Exception as e:
            print(f"  tweet_log 書き込み失敗（投稿自体は成功）: {e}")

        # CTA リプライ: WEBサイトの新弾特集ページへ誘導
        cta_text = f"▶ {pack_name} 収録カード全件の相場を見る\n{SITE_URL}/featured"
        cta_id = post_tweet(cta_text, reply_to_id=tweet_id)
        print(f"  CTAリプライ投稿{'成功' if cta_id else '失敗'} (parent={tweet_id})")

    elif not tweet_id:
        print("  投稿失敗")


if __name__ == "__main__":
    import sys
    from supabase import create_client
    _url = os.environ.get("SUPABASE_URL", "")
    _key = os.environ.get("SUPABASE_KEY", "")
    if not _url or not _key:
        print("エラー: SUPABASE_URL と SUPABASE_KEY を環境変数に設定してください")
        raise SystemExit(1)
    _sb = create_client(_url, _key)

    # --featured フラグで新弾フィーチャー投稿、それ以外は通常の値動きランキング
    if "--featured" in sys.argv:
        post_featured_movers(_sb)
    else:
        post_daily_movers(_sb)
