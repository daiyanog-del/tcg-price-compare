"""X（Twitter）自動投稿モジュール — 大変動カードの単発投稿（2026-07-10〜）"""

import os
import logging
import tempfile
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict

import requests as _requests

from constants import JST
from name_normalize import fuzzy_key

logger = logging.getLogger(__name__)

SITE_URL = "https://tcg-price-compare.onrender.com"

# 毎日の値動き投稿の判定閾値（意味のある変動に絞る、旧 post_daily_movers 用）
# 2026-07-10 に price_history 直近60日(8,854日次ペア)で校正:
#   100円では中央値3枚で5枠が埋まらない日が60日中41日、該当ゼロ日も計3日。
#   50円/10% ならゼロ日なし・中央値 up6/down9 枚で5枠が埋まり、
#   10%バーで些末な絶対額変動は引き続き除外される（サイト側RPCの min_diff=50 とも整合）
DAILY_MIN_DIFF = 50    # 最安値の前日差（円）の下限
DAILY_MIN_PCT  = 10    # 前日比変動率（％）の下限

# 大変動カード単発投稿の判定閾値（対称。2ガード導入後は流れるのが本物のみのため
# 旧閾値ほど高いバーは不要）
# 2026-07-10 ガード①②適用後の60日実測で再校正（旧±2000円/+100%・-50%は偽陽性98%
# の汚染分布由来で失効）。±500円/±30%で合算週2.4件・1日1件制約後の実投稿約1.6件/週、
# 通過例は全件複数店確認済みの本物。docs/decisions.md 参照
BIGMOVE_UP_MIN_DIFF = 500      # 急騰: 前日差（円）の下限
BIGMOVE_UP_MIN_PCT = 30        # 急騰: 変動率（％）の下限
BIGMOVE_DOWN_MIN_DIFF = -500   # 急落: 前日差（円）の上限（マイナス）
BIGMOVE_DOWN_MIN_PCT = -30     # 急落: 変動率（％）の上限（マイナス）

# 7日クールダウン: 同一（正規化名, レアリティ）の bigmove 再投稿を抑止する期間
BIGMOVE_COOLDOWN_DAYS = 7


def get_price_movers(sb, direction="up", limit=5, allowed_names=None,
                     min_diff=50, min_pct=0, fallback=True):
    """Supabaseから値上がり/値下がりランキングを取得（Python集計・レアリティ横断min）。

    2026-08-06 監査F4で判明: レアリティ横断のカード単位minかつ店舗粒度のガード
    （共通店舗のみ・同方向2店以上）が無いため、ガード済みRPC版 get_price_movers
    （supabase_rpc_movers.sql）とほぼ無相関（実測: 42件中0件が重なる）。
    post_featured_movers からは2026-08-06にこの関数を外した（RPCベースへ移行）。
    残る利用者は post_daily_movers（--daily-legacy 手動フォールバック実行のみ）。

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

    # 閾値（円・％の両方）を満たす変動を抽出。fallback=True の場合は該当ゼロでも最大変動1枚を返す
    up_all = sorted([m for m in movers if m["diff"] > 0], key=lambda x: -x["pct"])
    down_all = sorted([m for m in movers if m["diff"] < 0], key=lambda x: x["pct"])
    if direction == "up":
        filtered = [m for m in up_all if abs(m["diff"]) >= min_diff and abs(m["pct"]) >= min_pct]
        result = filtered[:limit] if filtered else (up_all[:1] if fallback else [])
    else:
        filtered = [m for m in down_all if abs(m["diff"]) >= min_diff and abs(m["pct"]) >= min_pct]
        result = filtered[:limit] if filtered else (down_all[:1] if fallback else [])
    return result, date_old, date_new


def _rpc_rows_to_card_dates(rows):
    """get_price_movers RPC の戻り行を select_big_movers が受け取る
    card_dates 形式 {(name, rarity): {date_str: price}} に変換する。

    RPC 側で店舗粒度のガード①②（共通店舗のみ・複数店確認）を適用済みの
    today_price/prev_price を、date_new/date_old をキーとした2点系列に
    詰め直すだけ。select_big_movers 側の BIGMOVE 閾値判定・fuzzy dedup・
    top_n 選抜ロジックはそのまま利用できる。

    戻り値: (card_dates, date_old, date_new)。rows が空なら ({}, None, None)。
    """
    card_dates = {}
    date_old = date_new = None
    for row in rows:
        name = row.get("card_name", "")
        rarity = row.get("rarity") or ""
        d_new = row.get("date_new")
        d_old = row.get("date_old")
        card_dates[(name, rarity)] = {
            d_old: row.get("prev_price", 0),
            d_new: row.get("today_price", 0),
        }
        if date_old is None:
            date_old, date_new = d_old, d_new
    return card_dates, date_old, date_new


def _select_featured_movers(rows, allowed, limit=5):
    """get_price_movers RPC の戻り行から新弾フィーチャー投稿（値動きランキング）の
    候補を選抜する純関数（ネットワーク・sb不要）。

    - direction='up' の行のみ対象（新弾フィーチャーは値上がり紹介のみの既存仕様を維持）
    - card_name が allowed（新弾の収録カード集合）に含まれる行のみ対象
    - pct 降順で上位 limit 件

    RPC側で per_card=1（カードごと代表レアリティ1行=Web互換）・ガード①②③適用済みの
    ため、ここでは絞り込みと並べ替えのみ行う。

    pctはRPCのnumeric型がPostgREST/クライアント経由で文字列やDecimal相当で
    返る場合があるため float に明示キャストする（app.py:1383 の前例と同じ）。
    ソートは pct 降順を主キー、card_name 昇順を副キーにして同率時も決定的に並べる。

    戻り値: (movers, date_old, date_new)。
    movers = [{"name", "rarity", "today", "yesterday", "diff", "pct"}, ...]
    （format_featured_tweet が参照するキー）。date_old/date_new はRPC行から取得
    （同一呼び出し内は全行共通）。該当行が無ければ ([], None, None)。
    """
    filtered = [
        r for r in rows
        if r.get("direction") == "up" and r.get("card_name") in allowed
    ]
    if not filtered:
        return [], None, None
    filtered.sort(key=lambda r: (-float(r.get("pct") or 0), r.get("card_name", "")))
    date_old = filtered[0].get("date_old")
    date_new = filtered[0].get("date_new")
    filtered = filtered[:limit]
    movers = [
        {
            "name": r["card_name"],
            "rarity": r.get("rarity") or "",
            "today": r["today_price"],
            "yesterday": r["prev_price"],
            "diff": r["diff"],
            "pct": float(r.get("pct") or 0),
        }
        for r in filtered
    ]
    return movers, date_old, date_new


def select_big_movers(card_dates, date_old=None, date_new=None, top_n=1):
    """大変動カードの候補を純ロジックで選抜する（ネットワーク・sb不要）。

    card_dates: {(name, rarity): {date_str: price}}
    date_old/date_new: 比較する2日を明示指定する場合。None なら card_dates 全体から
        最新2日を自動特定する（get_price_movers と同じ方式）。
    top_n: 方向ごとに返す最大件数。投稿側はクールダウン時の次点繰り上げ用に
        複数件（top_n=3）を受け取り、実際に投稿するのは各方向1件のみ。

    非対称閾値（値下がりは数学的に-100%を超えられないため）:
      急騰: diff >= BIGMOVE_UP_MIN_DIFF   かつ pct >= BIGMOVE_UP_MIN_PCT
      急落: diff <= BIGMOVE_DOWN_MIN_DIFF かつ pct <= BIGMOVE_DOWN_MIN_PCT

    カード名の正規化dedup: 全角半角違い等の表記ゆれ同名（例:「！」/「!」）が
    別候補として並ばないよう、fuzzy_key(name) が同じものは変動率絶対値が大きい
    方のみ残す（表示名は元の名前のまま）。

    戻り値: [{"name", "rarity", "today", "yesterday", "diff", "pct", "direction"}, ...]
        方向ごとに変動率絶対値の降順で最大 top_n 件（該当なしの方向は含めない）。
        空リストもありうる。
    """
    if date_old is None or date_new is None:
        all_dates_set = set()
        for dates in card_dates.values():
            all_dates_set.update(dates.keys())
        all_dates_sorted = sorted(all_dates_set)
        if len(all_dates_sorted) < 2:
            return []
        date_new = all_dates_sorted[-1]
        date_old = all_dates_sorted[-2]

    candidates = []
    for (name, rarity), dates in card_dates.items():
        if date_new not in dates or date_old not in dates:
            continue
        today_price = dates[date_new]
        yesterday_price = dates[date_old]
        if yesterday_price <= 0:  # 実データ経路では10円以下除外済みだが、純関数として負値も防御
            continue
        diff = today_price - yesterday_price
        if diff == 0:
            continue
        pct = round((diff / yesterday_price) * 100, 1)

        if diff >= BIGMOVE_UP_MIN_DIFF and pct >= BIGMOVE_UP_MIN_PCT:
            direction = "up"
        elif diff <= BIGMOVE_DOWN_MIN_DIFF and pct <= BIGMOVE_DOWN_MIN_PCT:
            direction = "down"
        else:
            continue

        candidates.append({
            "name": name, "rarity": rarity,
            "today": today_price, "yesterday": yesterday_price,
            "diff": diff, "pct": pct, "direction": direction,
        })

    # 正規化名の重複排除: 同一 (fuzzy_key(name), rarity) は変動率絶対値最大の1件のみ残す
    dedup = {}
    for c in candidates:
        key = (fuzzy_key(c["name"]), c["rarity"])
        if key not in dedup or abs(c["pct"]) > abs(dedup[key]["pct"]):
            dedup[key] = c
    candidates = list(dedup.values())

    result = []
    for direction in ("up", "down"):
        dir_candidates = [c for c in candidates if c["direction"] == direction]
        if not dir_candidates:
            continue
        dir_candidates.sort(key=lambda c: -abs(c["pct"]))
        result.extend(dir_candidates[:top_n])

    return result


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
    """日本語カード名→カードID対応表を取得（セッションキャッシュ）。
    取得は ygores_repository 経由（Supabaseキャッシュ優先・レート制限つき）"""
    global _ygoresources_name_index
    if _ygoresources_name_index is not None:
        return _ygoresources_name_index
    try:
        from ygores_repository import repository as _ygores_repo
        _ygoresources_name_index = _ygores_repo.get_name_index()
        if _ygoresources_name_index:
            print(f"  [YGOResources] 名前インデックス取得: {len(_ygoresources_name_index)}件")
        else:
            print("  [YGOResources] 名前インデックス取得失敗")
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


def _already_posted_today(sb, content_type_prefix: str) -> bool:
    """当日(JST)に同種の投稿が tweet_log にあるか確認。
    ワークフローの手動再実行・重複起動による二重投稿を防ぐ。"""
    if not sb:
        return False
    try:
        today_start = datetime.now(JST).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        resp = (sb.table("tweet_log").select("tweet_id")
                .like("content_type", f"{content_type_prefix}%")
                .gte("posted_at", today_start)
                .limit(1).execute())
        return bool(resp.data)
    except Exception as e:
        print(f"  tweet_log 照会失敗（二重投稿チェックをスキップ）: {e}")
        return False


def _bigmove_content_type(direction, name, rarity):
    """bigmove投稿の content_type に (正規化名, レアリティ) を埋め込む。

    tweet_log にはカード識別子を保存できる専用カラムがないため、既存の
    content_type（自由記述のテキスト列、collect_x_metrics.py 等で完全一致は
    "movers_up" 等の定型値のみに依存しており本形式でも既存挙動は壊れない）を
    識別子の運搬先として利用する。区切りは "|"（fuzzy_key はコロンを除去しない
    ためカード名に「:」「：」が含まれると衝突しうるが、「|」はカード名に
    現れないため安全）。
    """
    return f"bigmove_{direction}|{fuzzy_key(name)}|{rarity}"


def _bigmove_recently_posted(sb, direction, name, rarity, cooldown_days=BIGMOVE_COOLDOWN_DAYS) -> bool:
    """同一 (正規化名, レアリティ, 方向) が直近 cooldown_days 日以内に bigmove 投稿済みか確認。

    tweet_log に専用の識別子カラムがないため、content_type に埋め込んだ識別子
    （_bigmove_content_type）を完全一致で照会する。スキーマ変更はしない制約下の実装。
    """
    if not sb:
        return False
    try:
        cutoff = (datetime.now(JST) - timedelta(days=cooldown_days)).isoformat()
        content_type = _bigmove_content_type(direction, name, rarity)
        resp = (sb.table("tweet_log").select("tweet_id")
                .eq("content_type", content_type)
                .gte("posted_at", cutoff)
                .limit(1).execute())
        return bool(resp.data)
    except Exception as e:
        print(f"  tweet_log クールダウン照会失敗（{name}/{rarity}）: {e}")
        return False


def format_bigmove_tweet(mover, date_old, date_new):
    """大変動カード単発投稿の本文を生成。カード名は切り詰めない（X検索ヒットの生命線）。"""
    label = "急騰" if mover["direction"] == "up" else "急落"
    sign = "+" if mover["diff"] > 0 else ""
    rarity_part = f"（{mover['rarity']}）" if mover["rarity"] else ""
    period = f"{_format_date(date_old)}→{_format_date(date_new)}"
    encoded_name = urllib.parse.quote(mover["name"])

    lines = [
        f"【{label}】{mover['name']}{rarity_part}",
        f"¥{mover['yesterday']:,} → ¥{mover['today']:,}"
        f"（{sign}{mover['diff']:,}円 / {sign}{mover['pct']}%）{period}",
        "全国のカードショップ横断の最安値比較はこちら",
        f"{SITE_URL}/card/{encoded_name}",
    ]
    return "\n".join(lines)


def post_big_movers(sb):
    """大変動カードの単発投稿（1カード1ポスト）。
    急騰・急落それぞれ1件のみ、該当なしの方向は投稿しない。
    7日クールダウンあり（クールダウン中は次点候補へ繰り上げ、最大3位まで）。"""
    print("\n=== X自動投稿（大変動） ===")

    if _already_posted_today(sb, "bigmove_"):
        print("  本日分は投稿済み（tweet_logに記録あり）— 二重投稿を回避してスキップ")
        return

    # 値動き集計はSupabase RPC（店舗粒度のガード①②適用済み）に一本化。
    # per_card=999・top_n=100 で全レアリティを候補として受け取る
    # （BIGMOVE閾値は diff にも依存するため、RPC側で pct 最大の1レアリティに
    # 事前に畳み込むと取りこぼしが生じるため。select_big_movers 側で
    # BIGMOVE閾値判定・fuzzy dedup・方向別top_n選抜を行う）
    cutoff = (datetime.now(JST) - timedelta(days=3)).strftime("%Y-%m-%d")
    try:
        resp = sb.rpc(
            "get_price_movers",
            {"cutoff_date": cutoff, "min_diff": 50, "top_n": 100, "per_card": 999},
        ).execute()
        rows = resp.data or []
    except Exception as e:
        logger.warning(f"RPC get_price_movers 呼び出し失敗 — スキップ: {e}")
        return

    if not rows:
        print("  値動きデータなし（RPC結果0件）— スキップ")
        return

    card_dates, date_old, date_new = _rpc_rows_to_card_dates(rows)
    if date_old is None or date_new is None:
        print("  比較可能な日付が取得できません — スキップ")
        return

    # クールダウン時の次点繰り上げ用に方向ごと上位3件まで受け取る（投稿は各方向1件のみ）
    candidates = select_big_movers(card_dates, date_old, date_new, top_n=3)
    if not candidates:
        print("  大変動候補なし — 本日は無投稿")
        return

    for direction in ("up", "down"):
        dir_movers = [m for m in candidates if m["direction"] == direction]
        for mover in dir_movers:
            name = mover["name"]
            rarity = mover["rarity"]

            if _bigmove_recently_posted(sb, direction, name, rarity):
                print(f"  クールダウン中のためスキップ: {name}（{rarity}）[{direction}] — 次点候補へ")
                continue

            text = format_bigmove_tweet(mover, date_old, date_new)
            print(f"\n--- {direction}: {name}（{rarity}） ---")
            print(text)

            image_path = get_card_image_path(name)
            image_paths = [image_path] if image_path else []

            tweet_id = post_tweet(text, image_paths=image_paths)

            for p in image_paths:
                try:
                    os.unlink(p)
                except Exception:
                    pass

            if tweet_id and sb:
                content_type = _bigmove_content_type(direction, name, rarity)
                try:
                    sb.table("tweet_log").insert({
                        "tweet_id": str(tweet_id),
                        "posted_at": datetime.now(JST).isoformat(),
                        "content_type": content_type,
                        "parent_tweet_id": None,
                    }).execute()
                    print(f"  tweet_log 記録済み (type={content_type})")
                except Exception as e:
                    print(f"  tweet_log 書き込み失敗（投稿自体は成功）: {e}")
            elif not tweet_id:
                print(f"  投稿されず: {name}（{rarity}）— DRY_RUN/API未設定のスキップ、またはAPI失敗")

            # 投稿を1件試行したらこの方向は終了（post_tweetの失敗はAPI要因の
            # 可能性が高く、次点で連打しない）
            break


# 旧形式（2026-07-10に新形式=post_big_movers へ切替。手動実行のフォールバック用に残置）
def post_daily_movers(sb):
    """毎日の値動きランキングをXに投稿（値上がり→値下がりのスレッド形式）"""
    print("\n=== X自動投稿 ===")

    if _already_posted_today(sb, "movers_"):
        print("  本日分は投稿済み（tweet_logに記録あり）— 二重投稿を回避してスキップ")
        return

    up_tweet_id = None

    for direction in ("up", "down"):
        movers, date_old, date_new = get_price_movers(
            sb, direction, limit=5,
            min_diff=DAILY_MIN_DIFF, min_pct=DAILY_MIN_PCT, fallback=False,
        )
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
        # レアリティ表記（format_bigmove_tweet と同じ流儀）。per_card=1 のRPC代表行は
        # 変動率最大のレアリティであり、無表示だと別レアリティの変動と誤解を招くため。
        rarity_part = f"（{m['rarity']}）" if m.get("rarity") else ""
        lines.append(
            f"{i+1}. {name}{rarity_part} {sign}{m['pct']}%"
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
            rarity_part = f"（{m['rarity']}）" if m.get("rarity") else ""
            lines.append(
                f"{i+1}. {name}{rarity_part} {sign}{m['pct']}%"
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
    from discord_notify import send_discord_message

    print("\n=== 新弾フィーチャー投稿 ===")

    if _already_posted_today(sb, "featured_movers"):
        print("  本日分は投稿済み（tweet_logに記録あり）— 二重投稿を回避してスキップ")
        return

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
        # 値動き集計はSupabase RPC（店舗粒度のガード①②③適用済み）に一本化
        # （2026-08-06 監査F4。旧Python集計 get_price_movers はガード無し・fallback
        # で必ず1枚投稿していたため、post_big_movers と同じRPCパターンに揃える）
        cutoff = (datetime.now(JST) - timedelta(days=3)).strftime("%Y-%m-%d")
        try:
            resp = sb.rpc(
                "get_price_movers",
                # top_n=250: RPCはdirectionごとにtop_n件返すため最大2×top_n行。
                # PostgREST返却上限1000行（本プロジェクトで4回踏んだ罠）に対し
                # 合計500行で安全域。切り捨て時は 'down'<'up' の並びでup側から
                # 消えるため上限接触は許容不可
                {"cutoff_date": cutoff, "min_diff": 50, "top_n": 250, "per_card": 1},
            ).execute()
            rows = resp.data or []
        except Exception as e:
            logger.warning(f"RPC get_price_movers 呼び出し失敗 — スキップ: {e}")
            send_discord_message(f"[新弾フィーチャー] RPC get_price_movers 呼び出し失敗: {e}")
            return

        print(f"  RPC取得行数: {len(rows)}")
        if len(rows) >= 1000:
            logger.error(
                f"RPC get_price_movers が{len(rows)}行返却 — "
                "PostgREST返却上限1000行に接触し切り捨ての疑い（'down'<'up'の並びでup側から消える）"
            )

        if not rows:
            print("  値動きデータなし（RPC結果0行）— スキップ")
            return

        movers_for_tweet, date_old, date_new = _select_featured_movers(rows, allowed, limit=5)
        if not movers_for_tweet:
            print(f"  ガードを満たす値動きなし（フィルタ後0件、rows {len(rows)}行中） — スキップ")
            return

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

    # --featured フラグで新弾フィーチャー投稿、--daily-legacy で旧形式（値動きランキング）の
    # 手動実行フォールバック、それ以外（通常の日次実行）は新形式の大変動単発投稿
    if "--featured" in sys.argv:
        post_featured_movers(_sb)
    elif "--daily-legacy" in sys.argv:
        post_daily_movers(_sb)
    else:
        post_big_movers(_sb)
