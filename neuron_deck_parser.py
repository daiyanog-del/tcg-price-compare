"""
遊戯王ニューロン デッキ/カード取り込みパーサ
============================================
モバイル向けニューロン連携の中核。Chrome拡張に頼らず、ニューロンのURLを
サーバー側で取得・解析する。

スマホのブラウザは拡張をサポートしないため、従来 deck-content.js が
ブラウザDOMから抽出していた処理を、サーバー側の静的HTML解析に置き換える。

検証済み（2026-06-15、通常UA + requests でJS実行なし取得）:
  - デッキページ member_deck.action は static HTML にカード名を含む（curl_cffi 不要）
  - 画像ブロック #main / #extra / #side（div.card_set）内の a[href*="cid="] が
    枚数ぶん並び、title 属性にカード名が入る → 同 title の出現回数 = 枚数
  - テキストブロック #detailtext_main / _ext / _side の .card_name は
    カード名のみ（枚数情報なし）→ 画像ブロックが空のときのフォールバック用
"""

import re
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup

# scraper の Chrome UA を再利用（取得ヘッダの一元管理）
from scraper import HEADERS

# ── 設定（セレクタ・許可ホストはここに集約。HTML構造変化に備える）──

# SSRF対策: 取得を許可するホストとスキーム
ALLOWED_HOSTS = {"db.yugioh-card.com", "www.db.yugioh-card.com"}
ALLOWED_SCHEME = "https"

TIMEOUT_SEC = 20
MAX_REDIRECTS = 3

# デッキの画像ブロック（要素id → 返却キー）。同 title の出現回数で枚数を数える
DECK_IMAGE_SECTIONS = (
    ("main", "main"),
    ("extra", "ex"),
    ("side", "side"),
)
# フォールバック用テキストブロック（画像ブロックが空のとき名前のみ拾う。枚数不明）
DECK_TEXT_SECTIONS = {
    "main": "detailtext_main",
    "extra": "detailtext_ext",
    "side": "detailtext_side",
}
CARD_LINK_SELECTOR = "a[href*='cid=']"
CARD_NAME_SELECTOR = ".card_name"

# 制限マーク（【禁止カード】【制限カード】等）をカード名先頭から除去
_LIMIT_PREFIX = re.compile(r"^【[^】]*】")
# og:title の汎用文字列（デッキ名/カード名として無効なもの）
_GENERIC_TITLE = ("遊戯王 デッキレシピ", "遊戯王ニューロン", "Yu-Gi-Oh")
# og:title 区切り（半角/全角の縦棒）
_TITLE_SEP = re.compile(r"\s*[|｜]\s*")


# ── URL検証・取得 ──

def validate_neuron_url(url: str) -> str | None:
    """ニューロンの許可ホスト・https のみ通す。OKなら正規化URL、NGなら None。"""
    if not url:
        return None
    try:
        p = urlparse(url.strip())
    except Exception:
        return None
    if p.scheme != ALLOWED_SCHEME:
        return None
    if p.hostname not in ALLOWED_HOSTS:
        return None
    return p.geturl()


def fetch_neuron_html(url: str) -> str | None:
    """許可ホストのページを取得して HTML 文字列を返す。失敗時 None。

    リダイレクトは手動で追従し、許可ホスト外への遷移は追わない（SSRF対策）。
    """
    current = validate_neuron_url(url)
    if not current:
        return None
    try:
        for _ in range(MAX_REDIRECTS + 1):
            res = requests.get(
                current, headers=HEADERS, timeout=TIMEOUT_SEC, allow_redirects=False
            )
            if res.is_redirect or res.is_permanent_redirect:
                # 相対 Location も解決したうえで再度ホワイトリスト検証
                nxt = validate_neuron_url(urljoin(current, res.headers.get("Location", "")))
                if not nxt:
                    print("  ⚠️ ニューロン: 許可外ホストへのリダイレクトを中断")
                    return None
                current = nxt
                continue
            if res.status_code == 200:
                # ニューロンはUTF-8。文字化け防止に明示
                res.encoding = res.apparent_encoding or "utf-8"
                return res.text
            print(f"  ❌ ニューロン取得失敗: HTTP {res.status_code}")
            return None
    except requests.RequestException as e:
        print(f"  ❌ ニューロン取得失敗: {e}")
    return None


# ── 解析 ──

def _extract_title_segment(soup: BeautifulSoup) -> str | None:
    """og:title の先頭セグメントを取り出す。汎用文字列なら None。"""
    og = soup.find("meta", attrs={"property": "og:title"})
    content = (og.get("content") if og else "") or ""
    seg = _TITLE_SEP.split(content)[0].strip()
    if seg and len(seg) < 100 and not any(seg.startswith(g) for g in _GENERIC_TITLE):
        return seg
    return None


def _clean_card_name(raw: str) -> str:
    return _LIMIT_PREFIX.sub("", (raw or "").strip()).strip()


def _extract_section(soup: BeautifulSoup, image_id: str, text_id: str,
                     label: str, warnings: list) -> list:
    """1セクション分のカードを [{qty, name}, ...]（出現順）で返す。

    まず画像ブロック（枚数=同名出現回数）。空ならテキストブロックで名前のみ
    補完し qty=1 + warning（枚数不明を握りつぶさず明示）。
    """
    block = soup.find(id=image_id)
    if block is not None:
        cards: list = []
        index: dict = {}
        for a in block.select(CARD_LINK_SELECTOR):
            raw = a.get("title")
            if not raw:
                img = a.find("img")
                raw = img.get("alt") if img else ""
            name = _clean_card_name(raw)
            if not name:
                continue
            if name in index:
                index[name]["qty"] += 1
            else:
                entry = {"qty": 1, "name": name}
                index[name] = entry
                cards.append(entry)
        if cards:
            return cards

    # フォールバック: テキストブロックから名前のみ（枚数不明）
    tblock = soup.find(id=text_id)
    if tblock is not None:
        names = [_clean_card_name(el.get_text(strip=True))
                 for el in tblock.select(CARD_NAME_SELECTOR)]
        names = [n for n in names if n]
        if names:
            warnings.append(f"{label}デッキの枚数を取得できなかったため各1枚で取り込みました")
            return [{"qty": 1, "name": n} for n in names]
    return []


def parse_neuron_deck(html: str) -> dict:
    """デッキページHTMLを解析。{ok, kind, name, main, ex, side, warnings}。"""
    soup = BeautifulSoup(html, "html.parser")
    warnings: list = []
    name = _extract_title_segment(soup) or "インポートデッキ"

    section_labels = {"main": "メイン", "extra": "エクストラ", "side": "サイド"}
    result_cards = {}
    for image_id, out_key in DECK_IMAGE_SECTIONS:
        result_cards[out_key] = _extract_section(
            soup, image_id, DECK_TEXT_SECTIONS[image_id],
            section_labels[image_id], warnings,
        )

    main, ex, side = result_cards["main"], result_cards["ex"], result_cards["side"]
    # 「0件＝空デッキ」と「取得失敗」を区別。main も ex も空なら失敗扱い
    if not main and not ex:
        return {"ok": False, "error": "デッキのカード情報を読み取れませんでした"}

    return {
        "ok": True, "kind": "deck", "name": name,
        "main": main, "ex": ex, "side": side, "warnings": warnings,
    }


def parse_neuron_card(html: str) -> dict:
    """カード詳細ページHTMLを解析。{ok, kind, name}。"""
    soup = BeautifulSoup(html, "html.parser")
    name = _extract_title_segment(soup)
    if not name:
        return {"ok": False, "error": "カード名を読み取れませんでした"}
    return {"ok": True, "kind": "card", "name": name}


def import_neuron(url: str) -> dict:
    """ニューロンURLを取得・解析する公開エントリ。app.py から呼ぶ。

    戻り値はデッキ/カードいずれも {"ok": bool, ...}。失敗は {"ok": False, "error"}。
    """
    valid = validate_neuron_url(url)
    if not valid:
        return {"ok": False,
                "error": "対応していないURLです（遊戯王ニューロンのURLを貼り付けてください）"}

    html = fetch_neuron_html(valid)
    if html is None:
        return {"ok": False, "error": "ページを取得できませんでした。時間をおいて再度お試しください"}

    if "member_deck.action" in valid:
        return parse_neuron_deck(html)
    if "card_search.action" in valid:
        return parse_neuron_card(html)
    return {"ok": False,
            "error": "デッキレシピまたはカードのURLを貼り付けてください"}
