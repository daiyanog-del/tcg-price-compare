"""
遊戯王 パックカードリスト スクレイパー
========================================
・公式サイトから最新パック一覧を自動取得
・遊戯王Wikiからパックの収録カードリストを取得（メイン）
・YGOPRODeck APIでTCGセット名から日本語カード名を取得（フォールバック）
"""

import os
import re
import json
import tempfile
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from urllib.parse import quote

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

WIKI_BASE = "https://yugioh-wiki.net/index.php"
YGOPRODECK_API = "https://db.ygoprodeck.com/api/v7"

# ── キャッシュ ──
_CACHE_DIR = Path(__file__).parent / ".cache" / "packs"
_PACK_LIST_CACHE_TTL = timedelta(hours=12)

def _cache_path(key: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\-]", "_", key)
    return _CACHE_DIR / f"{safe}.json"

_CACHE_IO_LOCK = Lock()

def _cache_read(key: str, ttl: timedelta) -> dict | None:
    fp = _cache_path(key)
    try:
        # 存在確認も鍵の内側で行う。Windows の os.stat はファイルを共有不可で一瞬開くため、
        # 鍵の外で exists() すると別スレッドの os.replace が PermissionError で失敗する。
        with _CACHE_IO_LOCK:
            if not fp.exists():
                return None
            data = json.loads(fp.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(data["_ts"])
        if datetime.now() - ts > ttl:
            return None
        return data
    except Exception:
        return None

def _cache_write(key: str, data: dict):
    fp = _cache_path(key)
    # 別ファイルに書いてから置き換える。同じファイルを空にしてから書くと、書き込み中に
    # 読んだ側が途中までの中身を受け取り「キャッシュ無し」と判定していた。
    # 呼び出し元の dict には _ts を書き足さない。
    payload = {**data, "_ts": datetime.now().isoformat()}
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=fp.parent,
                                         suffix=".tmp", delete=False) as tmp:
            tmp_name = tmp.name
            json.dump(payload, tmp, ensure_ascii=False)
        with _CACHE_IO_LOCK:
            os.replace(tmp_name, fp)
    finally:
        if tmp_name and os.path.exists(tmp_name):
            os.remove(tmp_name)


# ── 公式サイトから最新パック一覧を自動取得 ──

_KONAMI_PRODUCTS_URL = "https://www.yugioh-card.com/japan/products/"
_PACK_CATEGORIES = ["basic", "special", "concept"]
_MAX_PACKS = 4  # 収集用に選ぶパック数（表示用は下の2つで決める）
# 表示用（/api/packs）: 発売前（公式取得時点で60日以内に絞り済み）は全部、発売済みは直近この件数。
# 以前は表示も _MAX_PACKS の直近4件だったため、発売前が4件あると発売済みが一覧から消えていた（2026-10-21 実測）。
_DISPLAY_RELEASED_PACKS = 4
_JST_OFFSET = timedelta(hours=9)


def _fetch_latest_packs_from_official() -> list[dict]:
    """
    公式サイトの商品ページからパック情報を取得。
    JS内の p[N]={...} 形式のデータを解析する。
    """
    all_packs = []

    for cat in _PACK_CATEGORIES:
        try:
            url = f"{_KONAMI_PRODUCTS_URL}?{cat}"
            res = requests.get(url, headers=HEADERS, timeout=15)
            # p[N]={...} パターンを抽出
            for m in re.finditer(r'p\[\d+\]\s*=\s*\{([^}]+)\}', res.text, re.DOTALL):
                content = m.group(1)
                title_m = re.search(r'"title":"([^"]+)"', content)
                date_m = re.search(r'"release-date":"([^"]+)"', content)
                if not title_m or not date_m:
                    continue

                title = title_m.group(1)
                raw_date = date_m.group(1)
                # 商品ページ（"detail":"page" のときだけ "url" に英字の略号が入る。例: "dbgv"）。
                # 略号は収録カードの型番の頭（DBGV-JP001）と同じなので、画面で「このパックの版」を見分けるのに使う
                url_m = re.search(r'"url":"([a-z0-9]+)"', content)
                detail_m = re.search(r'"detail":"([^"]*)"', content)
                slug = url_m.group(1) if (url_m and detail_m and detail_m.group(1) == "page") else ""

                # 日付を解析（例: "2025年3月22日(土)"）
                date_match = re.search(r'(\d{4})\D+(\d{1,2})\D+(\d{1,2})', raw_date)
                if not date_match:
                    continue
                y, mo, d = date_match.groups()
                iso_date = f"{y}-{int(mo):02d}-{int(d):02d}"

                # 発売前パックはWikiにカードリストがなければ get_pack_list() で自動除外される
                try:
                    release = datetime.strptime(iso_date, "%Y-%m-%d")
                    if release > datetime.now() + timedelta(days=60):
                        continue
                except ValueError:
                    continue

                # パック名の正規化（公式サイトのダッシュを統一）
                # 公式は半角ハイフン+スペースだが、Wikiは特殊ダッシュ(U+2212)の場合がある
                wiki_page = title.strip()

                all_packs.append({
                    "name": title.strip(),
                    "wiki_page": wiki_page,
                    "tcg_name": "",
                    "date": iso_date,
                    "category": cat,
                    "code": slug.upper(),
                    "product_url": f"{_KONAMI_PRODUCTS_URL}{slug}/" if slug else "",
                })
        except Exception as e:
            print(f"  pack_scraper 公式サイト取得失敗 ({cat}): {e}")

    # 発売日順（新しい順）にソート
    all_packs.sort(key=lambda x: x["date"], reverse=True)
    return all_packs


def _resolve_wiki_page(pack_name: str) -> tuple[str, bool]:
    """
    Wikiページ名のバリエーションを試し、(ページ名, カードリスト有無) を返す。
    公式サイトのダッシュ表記とWikiの表記が異なる場合がある。
    """
    # 全角ハイフン(－)はEUC-JPに変換できないため先に正規化
    normalized = pack_name.replace('－', '-')
    # ダッシュのバリエーション
    variants = [
        pack_name,  # そのまま
        normalized,  # 全角ハイフン→半角
        normalized.replace(" - ", " \u2212").replace(" -", "\u2212").replace("- ", "\u2212"),  # 半角→数学マイナス
        re.sub(r'\s*-\s*', ' \u2212', normalized),  # ハイフンを全てU+2212に
        re.sub(r'\s*-\s*', '\u2212', normalized),  # スペースなしU+2212
    ]

    # 公式名にダッシュが無くても、Wiki側はサブタイトルをU+2212で囲んでいる場合がある
    # 例: 公式「デッキビルドパック グロリアス・ヴィクターズ」
    #     Wiki「デッキビルドパック −グロリアス・ヴィクターズ−」
    # 既存のバリアントは「あるダッシュを変換する」ものばかりで、この形に到達できなかった
    # （2026-09-01 実測。この修正で44件取得できるようになった）
    if " " in pack_name:
        series, subtitle = pack_name.split(" ", 1)
        variants.append(f"{series} −{subtitle}−")

    seen = set()
    for v in variants:
        v = v.strip()
        if v in seen:
            continue
        seen.add(v)
        # quiet_404=True: ページ名が一致するまで順に試す正常動作の途中経過（404）は
        # 出力しない。500等の404以外の失敗は_fetch_from_wiki側で従来どおり出す
        cards = _fetch_from_wiki(v, v, quiet_404=True)
        if cards:
            return v, True

    # 全バリアントが尽きた（=どのページ名でも見つからなかった）場合のみ、
    # ここで従来相当のメッセージを1行出す。500やネットワーク断のときは各バリアント
    # 側で個別に出力済みのため、ここでは原因を断定しない文言にする
    # （2026-09-02 レビュー指摘）
    print(f"  pack_scraper Wiki取得失敗: {pack_name} — 全{len(seen)}バリアントで取得できず（404またはカードリスト無し）")
    return pack_name, False  # どれも取れなければ元の名前を返す


def _try_wiki_page_variants(pack_name: str) -> str:
    """後方互換ラッパー: ページ名のみ返す（collect_prices.py の同期処理が使用）"""
    page, _ = _resolve_wiki_page(pack_name)
    return page


def get_pack_list(include_empty: bool = True) -> list[dict]:
    """
    最新パック一覧を返す。
    公式サイトから自動取得し、Wikiでカード取得可能なパックを返す。
    結果はキャッシュする（24時間）。用途ごとにキャッシュを分離する。

    include_empty:
        True（既定・表示用 /api/packs, save_pack_list）:
            Wikiにカードリスト未掲載（発売前など）のパックも含め、発売前（60日以内）は全部、
            発売済みは直近 _DISPLAY_RELEASED_PACKS 件を返す。発売前パックが一覧から消えず、
            発売前が多い時期にも発売済みパックが押し出されないようにするための表示用挙動。
        False（収集用 collect_prices.sync_latest_packs）:
            カードリスト未掲載パックは除外し、_MAX_PACKS の枠を消費させない
            （0枚パックが収録数の多い既発売パックを圏外に押し出し、収集対象が
            痩せる問題の回避。2026-07-07 の買取収集激減の一因。TASKS.md 参照）。
    """
    # 表示用は 2026-10-21 に選び方を変えたためキーも変える（旧キーの24時間キャッシュを読まないように）
    cache_key = "pack_list_display_v2" if include_empty else "pack_list_collect"
    cached = _cache_read(cache_key, timedelta(hours=24))
    if cached and "packs" in cached and len(cached["packs"]) > 0:
        return cached["packs"]

    official_packs = _fetch_latest_packs_from_official()
    if not official_packs:
        # フォールバック: キャッシュがあれば古くても返す
        old = _cache_read(cache_key, timedelta(days=30))
        if old and "packs" in old:
            return old["packs"]
        return []

    if include_empty:
        # 表示用: カードリスト有無にかかわらず、発売前は全部＋発売済みは直近 _DISPLAY_RELEASED_PACKS 件
        today = (datetime.now(timezone.utc) + _JST_OFFSET).strftime("%Y-%m-%d")
        upcoming = [p for p in official_packs if p["date"] > today]
        released = [p for p in official_packs if p["date"] <= today][:_DISPLAY_RELEASED_PACKS]
        valid_packs = []
        for pack in upcoming + released:
            wiki_page, has_cards = _resolve_wiki_page(pack["name"])
            valid_packs.append({
                "name": pack["name"],
                "wiki_page": wiki_page,
                "tcg_name": pack.get("tcg_name", ""),
                "date": pack["date"],
                "has_cards": bool(has_cards),
                "code": pack.get("code", ""),
                "product_url": pack.get("product_url", ""),
                # 遊戯王Wikiのページ（ページ名は EUC-JP で URL にするのでブラウザ側では作れない）
                "wiki_url": f"{WIKI_BASE}?{quote(wiki_page.replace('－', '-'), encoding='euc-jp', errors='ignore')}" if has_cards else "",
            })
    else:
        # 収集用: 直近パックのうち、Wikiでカードリストが取れるものを_MAX_PACKS個選ぶ。
        # カードリスト未掲載（発売前など）のパックには枠を消費させない
        valid_packs = []
        cardless_packs = []
        for pack in official_packs:
            if len(valid_packs) >= _MAX_PACKS:
                break

            wiki_page, has_cards = _resolve_wiki_page(pack["name"])
            entry = {
                "name": pack["name"],
                "wiki_page": wiki_page,
                "tcg_name": pack.get("tcg_name", ""),
                "date": pack["date"],
            }
            if has_cards:
                valid_packs.append(entry)
            else:
                print(f"  pack_scraper: {pack['name']} はWikiにカードリスト未掲載のためスキップ")
                cardless_packs.append(entry)

        if not valid_packs:
            # Wiki全滅時（障害等）は従来どおり直近 _MAX_PACKS 件を返し、収集の全損を避ける
            valid_packs = cardless_packs[:_MAX_PACKS]

    if valid_packs:
        _cache_write(cache_key, {"packs": valid_packs})

    return valid_packs


# ── 方法1: 遊戯王Wikiからカードリスト取得 ──

def _fetch_from_wiki(pack_name: str, wiki_page: str, quiet_404: bool = False) -> list[str]:
    """遊戯王Wikiからパックのカード名リストを取得。

    quiet_404: True の場合、404（ページ無し）は出力しない。_resolve_wiki_page が
    ページ名バリアントを順に試す際の404は正常動作の途中経過であり、本番ログの
    大半を占めていたため（2026-09-02）。404以外の失敗（500等）は quiet_404 に
    関わらず従来どおり出力する。
    """
    page = wiki_page or pack_name
    try:
        url = f"{WIKI_BASE}?{quote(page, encoding='euc-jp')}"
    except UnicodeEncodeError:
        return []

    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        res.raise_for_status()
        res.encoding = res.apparent_encoding or 'euc-jp'
        soup = BeautifulSoup(res.text, "html.parser")
    except requests.RequestException as e:
        is_404 = getattr(e.response, "status_code", None) == 404
        if not (quiet_404 and is_404):
            print(f"  pack_scraper Wiki取得失敗: {url} — {e}")
        return []

    cards = []
    seen = set()

    for li in soup.select("li"):
        text = li.get_text()
        # 型番は「IMPH-JP015」に加えて、特別収録の「IMPH-JPS01」（JP のあとに英字1字）も拾う。
        # 以前は数字3桁だけで、特別収録の15種がパックの一覧から抜けていた（2026-11-01 IMMORTAL PHOENIX）。
        # 「IMPH-JPT01 《トークン》」のように JP のあとが T の行はトークン（売り物のカードではない）なので拾わない
        if not re.search(r'[A-Z0-9]+-JP(?:(?!T)[A-Z])?\d{2,3}', text):
            continue

        links = li.select("a")
        for a in links:
            a_text = a.get_text(strip=True)
            # 《》「」で囲まれたカード名を抽出
            # 貪欲マッチにしている。非貪欲だと名前の内側や末尾の閉じ括弧の手前で切れる
            # （例: 「道化の一座『開演」→ 正しくは「道化の一座『開演』」）。カード名は価格の
            # 名寄せに使うため実害があった。貪欲化の影響は2026-09-01に実データ2,068件で計測し、
            # 39件が閉じ括弧の復元のみ変化・取りこぼしと取りすぎは0件であることを確認済み。
            m = re.match(r'[\u300A\u300C\u300E\u3008](.+)[\u300B\u300D\u300F\u3009]', a_text)
            if m:
                # Wiki特有の特殊文字を正規化（U+2212→通常ハイフン等）
                card_name = m.group(1).replace('\u2212', '-').replace('\u2015', '-').replace('\u2014', '-').replace('\u2013', '-')
                if card_name not in seen:
                    seen.add(card_name)
                    cards.append(card_name)
                break

    return cards


def fetch_pack_card_numbers(pack_name: str) -> dict[str, str]:
    """遊戯王Wikiのパックのページから {カード名: 型番} を返す（2026-10-30・未発売カードの型番の補い）。

    ページの「IMPH-JP015 《六花双精プリムローズ》」の行から、カード名（_fetch_from_wiki と同じ切り出し・正規化）と、
    その行の最初の型番を組にする。ページが見つからない・取得できないときは空。
    未発売カードの型番は公式画像のURL → 抽出AIの読みで決め、それでも空のものだけをこれで埋める
    （Wiki は有志の更新で載るまで遅れがあり、商品名によってはページが見つからないため。tools/backfill_card_number.py）。
    """
    page, found = _resolve_wiki_page(pack_name)
    if not found:
        return {}
    try:
        res = requests.get(f"{WIKI_BASE}?{quote(page, encoding='euc-jp')}", headers=HEADERS, timeout=20)
        res.raise_for_status()
    except (requests.RequestException, UnicodeEncodeError) as e:
        print(f"  pack_scraper Wiki取得失敗（型番）: {page} — {e}")
        return {}
    res.encoding = res.apparent_encoding or 'euc-jp'
    numbers: dict[str, str] = {}
    for li in BeautifulSoup(res.text, "html.parser").select("li"):
        code = re.search(r'[A-Z0-9]{2,5}-JP[A-Z]?\d{2,3}', li.get_text())
        if not code:
            continue
        for a in li.select("a"):
            m = re.match(r'[《「『〈](.+)[》」』〉]', a.get_text(strip=True))
            if m:
                name = m.group(1).replace('−', '-').replace('―', '-').replace('—', '-').replace('–', '-')
                numbers.setdefault(name, code.group(0))
                break
    return numbers


# ── 方法2: YGOPRODeck APIからカードリスト取得 ──

def _fetch_from_ygoprodeck(tcg_set_name: str) -> list[str]:
    """YGOPRODeck APIからTCGセット名で日本語カード名を取得"""
    if not tcg_set_name:
        return []

    url = f"{YGOPRODECK_API}/cardinfo.php?cardset={quote(tcg_set_name)}&language=ja"

    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        if res.status_code != 200:
            return []
        data = res.json()
        cards = []
        seen = set()
        for card in data.get("data", []):
            name = card.get("name", "")
            if name and name not in seen:
                seen.add(name)
                cards.append(name)
        return cards
    except Exception as e:
        print(f"  pack_scraper YGOPRODeck取得失敗: {tcg_set_name} — {e}")
        return []


# ── メイン関数 ──

def pack_cards_cached(pack_name: str) -> dict | None:
    """キャッシュ済みのカードリストを返す。無ければ None。

    呼び出し側が「外部サイトへのアクセスが発生するか」を事前に知るための関数。
    判定条件は fetch_pack_cards 内のキャッシュ判定と同一に保つこと（片方だけ
    変更すると、キャッシュがあるのにレート制限を消費する／その逆が起きる）。
    """
    cached = _cache_read(f"pack_{pack_name}", _PACK_LIST_CACHE_TTL)
    if cached and "cards" in cached and len(cached["cards"]) > 0:
        return cached
    return None


def fetch_pack_cards(pack_name: str, wiki_page: str = "", tcg_name: str = "") -> dict:
    """
    パックのカードリストを取得。Wiki → YGOPRODeck APIの順で試行。

    Args:
        pack_name: パック名（表示用）
        wiki_page: Wikiページ名（URLエンコード前）
        tcg_name: TCG版のセット名（YGOPRODeck API用）

    Returns:
        {"pack": "...", "cards": [...], "count": N}
    """
    cache_key = f"pack_{pack_name}"
    cached = _cache_read(cache_key, _PACK_LIST_CACHE_TTL)
    if cached and "cards" in cached and len(cached["cards"]) > 0:
        return cached

    # 方法1: 遊戯王Wiki
    cards = _fetch_from_wiki(pack_name, wiki_page)

    # 方法2: Wikiで取得できなければYGOPRODeck API
    if not cards and tcg_name:
        cards = _fetch_from_ygoprodeck(tcg_name)

    result = {
        "pack": pack_name,
        "cards": cards,
        "count": len(cards),
    }

    if cards:
        _cache_write(cache_key, result)
    else:
        # 期限切れキャッシュがあればそれを返す
        old = _cache_read(cache_key, timedelta(days=30))
        if old and "cards" in old and len(old["cards"]) > 0:
            return old

    return result


# ── テーマ別カード取得（遊戯王Wiki【テーマ名】ページ） ──

def fetch_theme_cards(theme_name: str) -> list[str]:
    """
    遊戯王Wikiの【テーマ名】ページから関連カード名を取得。
    旧カードを含むテーマ全体の網羅が目的。取得失敗時は []。

    注意: テーマページのDOM構造はパックページと異なりカード番号を含まないため
    《カード名》リンクテキストを直接抽出する方式を採る。
    ノイズ（関連リンク等）が含まれる可能性があるため実ページで要確認。
    """
    page_name = f"【{theme_name}】"
    url = f"{WIKI_BASE}?{quote(page_name, encoding='euc-jp')}"

    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        res.raise_for_status()
        res.encoding = res.apparent_encoding or 'euc-jp'
        soup = BeautifulSoup(res.text, "html.parser")
    except Exception as e:
        print(f"  fetch_theme_cards 取得失敗 [{theme_name}]: {e}")
        return []

    cards = []
    seen: set[str] = set()

    for a in soup.select("a"):
        text = a.get_text(strip=True)
        # 《》（U+300A U+300B）で囲まれたカード名を抽出
        # 貪欲マッチにしている。非貪欲だと名前の内側や末尾の閉じ括弧の手前で切れる
        # （例: 「道化の一座『開演」→ 正しくは「道化の一座『開演』」）。カード名は価格の
        # 名寄せに使うため実害があった。貪欲化の影響は2026-09-01に実データ2,068件で計測し、
        # 39件が閉じ括弧の復元のみ変化・取りこぼしと取りすぎは0件であることを確認済み。
        m = re.match(r'[《「『〈](.+)[》」』〉]', text)
        if m:
            name = m.group(1).strip()
            # 短すぎるものや明らかに非カード名を除外
            if len(name) >= 3 and name not in seen:
                seen.add(name)
                cards.append(name)

    return cards


def fetch_card_themes(card_name: str) -> list[str]:
    """
    遊戯王Wikiのカードページ（《カード名》）から所属テーマ名を取得。
    【テーマ名】リンクを抽出する。取得失敗時は []。
    発売前カードはWikiページ未作成のことがあり空を返す（正常）。
    """
    page_name = f"《{card_name}》"
    url = f"{WIKI_BASE}?{quote(page_name, encoding='euc-jp')}"

    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        res.raise_for_status()
        res.encoding = res.apparent_encoding or 'euc-jp'
        soup = BeautifulSoup(res.text, "html.parser")
    except Exception as e:
        print(f"  fetch_card_themes 取得失敗 [{card_name}]: {e}")
        return []

    themes = []
    seen: set[str] = set()

    for a in soup.select("a"):
        text = a.get_text(strip=True)
        # 【】（U+3010 U+3011）で囲まれたテーマ名を抽出
        m = re.match(r'[【](.+?)[】]', text)
        if m:
            theme = m.group(1).strip()
            if theme and theme not in seen:
                seen.add(theme)
                themes.append(theme)

    return themes
