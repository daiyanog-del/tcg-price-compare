"""
unreleased_extractor.py — Claude API を使った未発売カード情報抽出モジュール

HTML から遊戯王OCGの未発売カード情報を抽出し、unreleased_cards テーブル用の
dict リストを返す。

yu-gi-oh.jp の新カード記事では、カード名・効果文・ステータスがすべてカード画像内に
焼き込まれており、HTML本文にテキストとして存在しない。そのため、カード画像を
Vision（画像をClaudeに直接渡す）で読み取るハイブリッド方式を採用する。

  - カード画像（/images/news/ 配下かつ _img_ を含むURL）をダウンロードして
    Claude の vision 入力として渡す
  - テキスト（記事タイトル・収録パック名・発売日等）も同時に渡す
  - カード画像が0枚の場合はテキスト抽出にフォールバック

依存パッケージ:
  - anthropic: GitHub Actions ワークフロー内でのみ pip install（ローカルでも動作するが
    ANTHROPIC_API_KEY が不要なため、import は起動時でなく関数呼び出し時に遅延実行）
  - beautifulsoup4: requirements.txt に含まれるため常に利用可能
  - fetch_guard: HTTPリクエストは必ずこのモジュール経由で行う

使い方:
  from unreleased_extractor import extract_cards_from_html
  rows = extract_cards_from_html(html_str, page_url)
"""

import base64
import logging
import os
import re
from typing import Any
from urllib.parse import urlparse, urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 定数
# ──────────────────────────────────────────────

# 使用モデル（環境変数で切替可能）
_DEFAULT_MODEL = "claude-sonnet-4-6"
EXTRACTOR_MODEL = os.environ.get("EXTRACTOR_MODEL", _DEFAULT_MODEL)

# Claude API のパラメータ
_MAX_TOKENS = 16000

# 前処理後のHTMLテキスト最大文字数（超過は末尾切り捨て）
_MAX_INPUT_CHARS = 80_000

# 1記事あたりのカード画像枚数上限（環境変数で変更可能）
_MAX_IMAGES = int(os.environ.get("EXTRACTOR_MAX_IMAGES", "30"))

# 1画像あたりのサイズ上限（バイト）: 5MB
_MAX_IMAGE_BYTES = 5 * 1024 * 1024

# カード画像のファイル名パターン「<記事ID>_<14桁日時>_...」
# 例: 2543_20260602093341_img_1_<hash>.jpg / 2542_20260602093009_<hash>.jpg
# バナー（news_1.jpg 等）を除外しつつ _img_ 有無に関わらずカード画像を採用する。
_CARD_IMAGE_NAME_RE = re.compile(r"^\d+_\d{14}_")

# ラッシュデュエル専用語（保険フィルタ用）。
# Claude の is_rush 判定をすり抜けても、ラッシュ固有の語が card_type / effect_text に
# 現れたカードは OCG に存在しないため除外する。OCGと共通の語は含めない。
_RUSH_ONLY_TERMS = ("マキシマム", "MAXIMUM", "ラッシュデュエル", "RUSH DUEL")

# システムプロンプト（固定・日本語）
_SYSTEM_PROMPT = """\
あなたは遊戯王OCGの新カード情報を公式ページから正確に抽出するアシスタントです。

## 画像からの抽出について

添付された画像はyu-gi-oh.jp公式ニュースページのカード画像です。

- **各画像は、それぞれ1枚の独立した完全な遊戯王カードです**（絵柄・カード名・属性・
  種族・ステータス・効果文がすべて1枚の画像内に収まっている）。img_1 / img_2 という
  連番は「同じカードの別カット」ではなく、別々のカードを指します。
  画像どうしを統合せず、**1画像 = 1カード**として読み取ってください。
- **効果文はカード下部の枠内に小さな文字で記載されています。**
  ここが最も読み取りを誤りやすい箇所です。拡大して1文字ずつ丁寧に読み、
  「召喚した」と「召喚された」、助詞（てにをは）、種族名（恐竜族など）の有無、
  数字・丸数字（①②③）を正確に書き起こしてください。推測で補完しないこと。
- 画像から読めない項目は空/null としてください
- 文字の読み取りに確信が持てない場合は confidence を下げてください

## 抽出ルール

1. ページに遊戯王OCGの**新カード情報**が掲載されている場合のみ抽出する。
   - カード名（name）が明確に記載されていないカードは出力しない
   - 既存カードの再録情報のみのページでは cards=[] を返す
   - カード情報がないページ（ニュース・イベント・店舗情報等）では cards=[] を返す

1-2. **ラッシュデュエルのカードの扱い（重要）**:
   このサービスの価格比較対象は遊戯王OCGのみで、ラッシュデュエル（ラッシュデュエル／
   RUSH DUEL）は対象外です。1ページにOCGとラッシュデュエルのカードが混在することが
   あるため、各カードごとに is_rush（true/false）を判定してください。
   - ラッシュデュエルのカードを見分ける主な視覚的手がかり:
     - カード枠の上部や枠デザインに「ラッシュデュエル」「RUSH DUEL」のロゴ・表記がある
     - レベルの★がカード上部に横並びで配置される（OCGとは枠・配置の様式が異なる）
     - 「MAXIMUM（マキシマム）」「LEGEND（レジェンド）」などラッシュ専用の枠・表記がある
     - 攻撃力/守備力の表記様式や効果欄の枠デザインがOCGと異なる
   - ラッシュデュエルだと判断したカードは is_rush=true とする（このカードは後段で除外される）
   - **確信が持てない場合は is_rush=false（OCG扱い）とすること。**
     正規のOCGカードを誤って除外しないよう、安全側に倒す。

2. 各フィールドの扱い:
   - name: 日本語正式カード名。必須。英語名しかない場合も記入する
   - reading: フリガナ。不明なら空文字
   - card_type: カード種別。カード画像右上の種類アイコンおよび「魔法カード」「罠カード」の記載を見て判定する。
     - モンスター: 効果モンスター/融合モンスター/シンクロモンスター/エクシーズモンスター/リンクモンスター/ペンデュラムモンスター/通常モンスター/儀式モンスター 等
     - 魔法: 種別アイコンを読み取り「通常魔法」「永続魔法」「速攻魔法」「装備魔法」「フィールド魔法」「儀式魔法」のいずれかを記入する。
       種別アイコンが読み取れない場合のみ「魔法」とする
     - 罠: 種別アイコンを読み取り「通常罠」「永続罠」「カウンター罠」のいずれかを記入する。
       種別アイコンが読み取れない場合のみ「罠」とする
   - attribute: 光/闇/炎/水/地/風/神。不明なら空文字
   - race: 戦士族/魔法使い族/ドラゴン族/機械族 等の種族。不明なら空文字
   - level: レベル（整数）。エクシーズはrank、リンクはlink_valを使う。不明ならnull
   - rank: エクシーズモンスターのランク（整数）。不明ならnull
   - link_val: リンクモンスターのリンク値（整数）。不明ならnull
   - atk: 攻撃力（整数）。「?」や不明の場合はnull
   - def: 守備力（文字列: 数値・「?」・「-」等）。不明なら空文字
   - effect_text: 効果テキストまたはフレーバーテキスト。改行は\\nで表現
   - product_name: 収録パック名（例: ANIMATION CHRONICLE 2026）。不明なら空文字
   - release_date: 発売予定日（YYYY-MM-DD形式）。不明・未定なら空文字
   - image_urls: 読み取り元の画像URL（提供された画像URLをそのまま記入）
   - image_url: このカードが写っている画像1枚のURL。
     各カードは1枚の画像に対応するので、そのカードを読み取った画像のURLを入れる。
     必ず渡された画像URLの中から正確にコピーして入れること（URLを創作・変形しない）。
     該当する画像URLが不明な場合は空文字にする。
   - is_rush: ラッシュデュエルのカードなら true、遊戯王OCGのカードなら false。
     判別がつかない場合は false（OCG扱い）とする。詳細は上記「1-2.」を参照。
   - confidence: 抽出の確信度
     - high: カード名・効果文・ステータスがすべて明確に読み取れる
     - medium: 一部情報が不明確または推測が含まれる
     - low: カード名以外ほとんど不明、または情報の信頼性が低い

3. confidence の判断基準:
   - 必須情報（card_typeまたはeffect_text）が欠落している場合は最大でもmedium
   - 画像からの読み取りに推測・補完が必要な場合はlowまたはmedium

4. 出力はJSONのみ。説明文は不要。
"""


# ──────────────────────────────────────────────
# カード画像URL抽出
# ──────────────────────────────────────────────

def _extract_card_image_urls(html: str, page_url: str) -> list[str]:
    """HTMLからカード画像URLを抽出する（dedupe・順序保持）。

    カード画像の判定基準:
      - パスが /images/news/ 配下
      - ファイル名が「<記事ID>_<14桁日時>_」で始まる
        （例: 2543_20260602093341_img_1_<hash>.jpg / 2542_20260602093009_<hash>.jpg）
        旧来は "_img_" 必須だったが、_img_ を含まない命名（Vジャンプ付録カード等）も
        カード画像なので、命名規則ベースに緩和した。

    menu/banner（news_1.jpg 等）/ヘッダーなどのUI画像は除外される。

    Args:
        html:     取得済みHTMLテキスト
        page_url: ページのURL（相対URLの絶対化に使用）

    Returns:
        カード画像URLリスト（重複なし・出現順）
    """
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    result: list[str] = []

    for img in soup.find_all("img"):
        src = img.get("src", "").strip()
        if not src:
            continue

        # 相対URLを絶対URLに変換
        abs_url = urljoin(page_url, src)

        # https化（http の場合は変換）
        if abs_url.startswith("http://"):
            abs_url = "https://" + abs_url[7:]

        # 重複除去
        if abs_url in seen:
            continue

        parsed = urlparse(abs_url)

        # /images/news/ 配下かつファイル名に _img_ を含むもののみ
        path = parsed.path
        if not path.startswith("/images/news/"):
            continue

        # カード画像の命名規則「<記事ID>_<14桁日時>_...」のみ採用。
        # バナー（news_1.jpg 等）や固定UI画像を除外する。
        filename = path.rsplit("/", 1)[-1]
        if not _CARD_IMAGE_NAME_RE.match(filename):
            continue

        seen.add(abs_url)
        result.append(abs_url)

    logger.debug(f"[Extractor] カード画像URL抽出: {len(result)}件 ({page_url!r})")
    return result


# ──────────────────────────────────────────────
# HTML前処理
# ──────────────────────────────────────────────

def _preprocess_html(html: str, page_url: str = "") -> str:
    """HTMLをClaudeへの入力テキストに変換する。

    処理内容:
      1. script/style/nav/footer/header タグを除去
      2. img タグを [IMG] src alt 形式の行に変換（カード画像URL抽出のため保持）
      3. テキスト化＋空白正規化
      4. _MAX_INPUT_CHARS で末尾切り捨て
    """
    soup = BeautifulSoup(html, "html.parser")

    # 不要タグを除去（テキスト量削減）
    for tag in soup.find_all(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    # img タグをプレースホルダ行に変換（除去前に処理）
    img_lines = []
    for img in soup.find_all("img"):
        src = img.get("src", "")
        alt = img.get("alt", "")
        if src:
            img_lines.append(f"[IMG] {src} {alt}")
        img.decompose()

    # テキスト化
    text = soup.get_text(separator="\n")

    # 連続空白・空行を正規化
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            lines.append(stripped)

    # img 行を末尾に付加
    lines.extend(img_lines)

    # ページURLをヒントとして先頭に追記
    header = f"[ページURL: {page_url}]\n\n" if page_url else ""
    result = header + "\n".join(lines)

    # 上限文字数で切り捨て
    if len(result) > _MAX_INPUT_CHARS:
        logger.warning(
            f"[Extractor] 入力テキストが上限を超えています "
            f"({len(result):,}文字 > {_MAX_INPUT_CHARS:,}文字)。末尾を切り捨てます。"
        )
        result = result[:_MAX_INPUT_CHARS]

    return result


# ──────────────────────────────────────────────
# Pydantic モデル定義（遅延ロード）
# ──────────────────────────────────────────────

def _get_pydantic_models():
    """Pydantic モデルを返す。初回呼び出し時にのみロード。"""
    try:
        from pydantic import BaseModel, Field, field_validator
        from typing import Literal
    except ImportError as e:
        raise ImportError(
            "pydantic が必要です。pip install pydantic でインストールしてください。"
        ) from e

    class ExtractedCard(BaseModel):
        name: str
        reading: str = ""
        card_type: str = ""
        attribute: str = ""
        race: str = ""
        level: int | None = None
        rank: int | None = None
        link_val: int | None = None
        atk: int | None = None
        def_: str = Field(default="", alias="def")
        effect_text: str = ""
        product_name: str = ""
        release_date: str = ""
        image_urls: list[str] = []
        # このカードのメイン画像1枚のURL（渡された画像URLのいずれかを正確にコピーする）
        image_url: str = ""
        # ラッシュデュエル（価格比較対象外）なら True。判別不能時は False（OCG扱い）。
        is_rush: bool = False
        confidence: Literal["high", "medium", "low"] = "medium"

        model_config = {"populate_by_name": True}

    class ExtractionResult(BaseModel):
        cards: list[ExtractedCard]

    return ExtractedCard, ExtractionResult


# ──────────────────────────────────────────────
# 抽出結果の検証・補正
# ──────────────────────────────────────────────

def _validate_and_fix(card: Any, page_url: str) -> dict | None:
    """抽出済みカードデータを検証・補正してdictを返す。
    無効なカード（name空等）は None を返す。

    Args:
        card: ExtractedCard Pydantic モデルインスタンス
        page_url: 抽出元ページURL

    Returns:
        unreleased_cards テーブル形式の dict、または None（除外対象）
    """
    # name が空なら無条件で除外
    name = (card.name or "").strip()
    if not name:
        logger.debug("[Extractor] name が空のカードをスキップします")
        return None

    confidence = card.confidence

    card_type = (card.card_type or "").strip()
    effect_text = (card.effect_text or "").strip()

    # 保険フィルタ: Claude の is_rush 判定をすり抜けても、ラッシュ専用語が
    # card_type / effect_text に現れたカードは OCG に存在しないため除外する。
    _haystack = f"{card_type} {effect_text}".upper()
    if any(term.upper() in _haystack for term in _RUSH_ONLY_TERMS):
        logger.warning(
            f"[Extractor] ラッシュ専用語を検出したため除外: {name!r} ({page_url!r})"
        )
        return None

    # 必須情報（card_type または effect_text）が両方欠落している場合は low に降格
    if not card_type and not effect_text:
        if confidence != "low":
            logger.debug(
                f"[Extractor] {name!r}: card_type/effect_text が両方空 → confidence を low に降格"
            )
            confidence = "low"

    # ドメイン取得
    source_domain = ""
    try:
        source_domain = urlparse(page_url).netloc
    except Exception:
        pass

    return {
        "name": name,
        "reading": (card.reading or "").strip(),
        "card_type": card_type,
        "attribute": (card.attribute or "").strip(),
        "race": (card.race or "").strip(),
        "level": card.level,
        "rank": card.rank,
        "link_val": card.link_val,
        "atk": card.atk,
        "def": (card.def_ or "").strip(),
        "effect_text": effect_text,
        "product_name": (card.product_name or "").strip(),
        "release_date": (card.release_date or "").strip() or None,
        "confidence": confidence,
        "source_url": page_url,
        "source_domain": source_domain,
        # このカードのメイン画像URL（空でも可）
        "image_url": (card.image_url or "").strip(),
        # extraction_raw はこの時点では未設定（呼び出し元で付加する）
    }


# ──────────────────────────────────────────────
# 画像ダウンロード・エンコード
# ──────────────────────────────────────────────

def _download_and_encode_images(image_urls: list[str]) -> list[dict]:
    """カード画像URLをダウンロードしてbase64エンコードする。

    fetch_guard.fetch_whitelisted を使用する（yu-gi-oh.jp配下のみ許可）。
    取得失敗・サイズ超過の画像は個別にスキップして続行する。

    Args:
        image_urls: カード画像URLリスト（上限適用済みを想定）

    Returns:
        Vision入力用 dict のリスト:
          {"url": str, "media_type": str, "data": str（base64）}
    """
    from fetch_guard import fetch_whitelisted, WhitelistViolation

    encoded: list[dict] = []

    for url in image_urls:
        try:
            # カード画像は同一サーバーの静的アセット。ページ巡回の5秒間隔は過剰なため
            # 短い間隔（0.5秒）で連続取得する（同時接続1は維持）。
            resp = fetch_whitelisted(url, timeout=30, min_interval=0.5)
        except WhitelistViolation as e:
            logger.warning(f"[Extractor] ホワイトリスト違反のためスキップ: {url!r} ({e})")
            continue
        except Exception as e:
            logger.warning(f"[Extractor] 画像取得失敗のためスキップ: {url!r} ({e})")
            continue

        if resp.status_code != 200:
            logger.warning(
                f"[Extractor] 画像HTTP {resp.status_code} のためスキップ: {url!r}"
            )
            continue

        content = resp.content
        if len(content) > _MAX_IMAGE_BYTES:
            logger.warning(
                f"[Extractor] 画像サイズ超過（{len(content):,}バイト > {_MAX_IMAGE_BYTES:,}バイト）"
                f"のためスキップ: {url!r}"
            )
            continue

        # media_type をContent-Typeヘッダから取得。取得できない場合は拡張子で判定
        content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type in ("image/jpeg", "image/png", "image/gif", "image/webp"):
            media_type = content_type
        else:
            # 拡張子から判定
            ext = url.rsplit(".", 1)[-1].lower() if "." in url else ""
            ext_map = {
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "png": "image/png",
                "gif": "image/gif",
                "webp": "image/webp",
            }
            media_type = ext_map.get(ext, "image/jpeg")

        encoded.append({
            "url": url,
            "media_type": media_type,
            "data": base64.standard_b64encode(content).decode("ascii"),
        })
        logger.debug(f"[Extractor] 画像取得成功: {url!r} ({len(content):,}バイト, {media_type})")

    return encoded


# ──────────────────────────────────────────────
# Vision メッセージ構築
# ──────────────────────────────────────────────

def _build_vision_message(processed_text: str, encoded_images: list[dict]) -> list[dict]:
    """Vision用のメッセージcontentを構築する。

    画像ブロック（type:image）とテキストブロック（type:text）を組み合わせる。
    画像を先頭に並べ、テキスト（記事コンテキスト）を末尾に付加する。

    Args:
        processed_text:  _preprocess_html の出力（記事テキスト）
        encoded_images:  _download_and_encode_images の出力

    Returns:
        Claude messages API の content リスト
    """
    content: list[dict] = []

    # 画像ブロック（各カード画像を順に追加）
    for img in encoded_images:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img["media_type"],
                "data": img["data"],
            },
        })
        # 画像直後に参照用のURL注記を付ける（Claudeが image_urls フィールドに使用）
        content.append({
            "type": "text",
            "text": f"[上記画像のURL: {img['url']}]",
        })

    # テキストブロック（記事コンテキスト＋スキーマ指示）
    schema_instruction = (
        "上記の画像群（カード画像）と以下の記事テキストから、"
        "遊戯王OCGの新カード情報を抽出してください。\n"
        "出力はJSON形式で、ExtractionResult スキーマに従ってください。\n\n"
        "ExtractionResult スキーマ:\n"
        "{\n"
        '  "cards": [\n'
        "    {\n"
        '      "name": "カード名（必須）",\n'
        '      "reading": "フリガナ",\n'
        '      "card_type": "カード種別（モンスターは「効果モンスター」等、魔法は「通常魔法/永続魔法/速攻魔法/装備魔法/フィールド魔法/儀式魔法」、罠は「通常罠/永続罠/カウンター罠」のいずれか。種別アイコンが読み取れない場合のみ「魔法」「罠」とする）",\n'
        '      "attribute": "属性",\n'
        '      "race": "種族",\n'
        '      "level": null,\n'
        '      "rank": null,\n'
        '      "link_val": null,\n'
        '      "atk": null,\n'
        '      "def": "",\n'
        '      "effect_text": "効果テキスト",\n'
        '      "product_name": "収録パック名",\n'
        '      "release_date": "YYYY-MM-DD または空文字",\n'
        '      "image_urls": ["読み取り元画像のURL"],\n'
        '      "image_url": "このカードの絵柄・カード名が写っている画像のURL（渡されたURLを正確にコピー。1枚のみ）",\n'
        '      "is_rush": false,\n'
        '      "confidence": "high|medium|low"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "--- 記事テキスト ---\n"
        f"{processed_text}"
    )
    content.append({"type": "text", "text": schema_instruction})

    return content


# ──────────────────────────────────────────────
# テキスト専用メッセージ構築（フォールバック用）
# ──────────────────────────────────────────────

def _build_text_message(processed_text: str) -> list[dict]:
    """テキスト専用（Vision不使用）のメッセージcontentを構築する。

    カード画像が0枚の場合のフォールバック。
    """
    content_text = (
        "以下のページから遊戯王OCGの新カード情報を抽出してください。\n"
        "出力はJSON形式で、ExtractionResult スキーマに従ってください。\n\n"
        "ExtractionResult スキーマ:\n"
        "{\n"
        '  "cards": [\n'
        "    {\n"
        '      "name": "カード名（必須）",\n'
        '      "reading": "フリガナ",\n'
        '      "card_type": "カード種別（モンスターは「効果モンスター」等、魔法は「通常魔法/永続魔法/速攻魔法/装備魔法/フィールド魔法/儀式魔法」、罠は「通常罠/永続罠/カウンター罠」のいずれか。種別が不明な場合のみ「魔法」「罠」とする）",\n'
        '      "attribute": "属性",\n'
        '      "race": "種族",\n'
        '      "level": null,\n'
        '      "rank": null,\n'
        '      "link_val": null,\n'
        '      "atk": null,\n'
        '      "def": "",\n'
        '      "effect_text": "効果テキスト",\n'
        '      "product_name": "収録パック名",\n'
        '      "release_date": "YYYY-MM-DD または空文字",\n'
        '      "image_urls": [],\n'
        '      "is_rush": false,\n'
        '      "confidence": "high|medium|low"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "--- ページ内容 ---\n"
        f"{processed_text}"
    )
    return [{"type": "text", "text": content_text}]


# ──────────────────────────────────────────────
# 公開関数
# ──────────────────────────────────────────────

def extract_cards_from_html(html: str, page_url: str = "") -> list[dict]:
    """HTMLページから遊戯王OCG新カード情報を抽出し、unreleased_cards 行形式の dict リストを返す。

    内部フロー（ハイブリッド方式）:
      1. HTMLからカード画像URL（/images/news/ 配下かつ _img_ 含む）を抽出
      2. カード画像が1枚以上ある場合:
         a. 画像をダウンロードしてbase64エンコード（最大 _MAX_IMAGES 枚）
         b. 画像＋テキストを Vision で Claude に渡して抽出
      3. カード画像が0枚の場合: テキストのみでフォールバック抽出
      4. JSON レスポンスをパース・バリデーション・補正して返す

    - HTTPリクエストはこの関数内では行わない（html 引数として受け取る）
      ※ 画像ダウンロードは fetch_guard.fetch_whitelisted 経由で内部的に行う
    - anthropic パッケージは遅延 import（関数呼び出し時に初めてロード）
    - pydantic は遅延 import（同上）

    Args:
        html:     取得済みHTMLテキスト
        page_url: ページのURL（ログ・source_url・image_urls正規化に使用）

    Returns:
        unreleased_cards テーブル形式の dict リスト。
        各 dict の "extraction_raw" キーに Claude 生出力・card_image_urls 等を含む。
        カード情報がないページでは空リストを返す。
    """
    # anthropic を遅延 import（ローカル環境で未インストールでも落ちないように）
    try:
        from anthropic import Anthropic
    except ImportError as e:
        raise ImportError(
            "anthropic パッケージが必要です。"
            "pip install anthropic でインストールしてください。"
        ) from e

    # Pydanticモデルを遅延ロード
    ExtractedCard, ExtractionResult = _get_pydantic_models()

    # HTML前処理（テキスト化）
    processed_text = _preprocess_html(html, page_url)
    logger.info(
        f"[Extractor] 前処理完了: {len(html):,}文字 → {len(processed_text):,}文字 ({page_url!r})"
    )

    # カード画像URLを抽出
    card_image_urls = _extract_card_image_urls(html, page_url)
    logger.info(f"[Extractor] カード画像URL: {len(card_image_urls)}件 ({page_url!r})")

    # 画像枚数上限チェック
    if len(card_image_urls) > _MAX_IMAGES:
        logger.warning(
            f"[Extractor] カード画像が上限を超えています "
            f"({len(card_image_urls)}枚 > {_MAX_IMAGES}枚)。先頭{_MAX_IMAGES}枚に切り捨てます。"
        )
        card_image_urls = card_image_urls[:_MAX_IMAGES]

    # Vision または テキストフォールバック を選択
    use_vision = len(card_image_urls) > 0
    encoded_images: list[dict] = []

    if use_vision:
        logger.info(
            f"[Extractor] Vision 抽出モード: {len(card_image_urls)}枚の画像を取得します"
        )
        encoded_images = _download_and_encode_images(card_image_urls)
        logger.info(f"[Extractor] 画像取得成功: {len(encoded_images)}件")

        if not encoded_images:
            # 全画像の取得に失敗した場合はテキストフォールバック
            logger.warning(
                f"[Extractor] 全画像の取得に失敗。テキスト抽出にフォールバックします: {page_url!r}"
            )
            use_vision = False
    else:
        logger.info(
            f"[Extractor] テキスト抽出モード（カード画像なし）: {page_url!r}"
        )

    # メッセージ構築
    if use_vision:
        message_content = _build_vision_message(processed_text, encoded_images)
    else:
        message_content = _build_text_message(processed_text)

    # Claude API クライアント
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError(
            "環境変数 ANTHROPIC_API_KEY が設定されていません。"
        )

    client = Anthropic(api_key=api_key)

    # 構造化抽出リクエスト
    mode_label = "Vision" if use_vision else "テキスト"
    logger.info(
        f"[Extractor] Claude API 呼び出し中 "
        f"(model={EXTRACTOR_MODEL}, mode={mode_label}): {page_url!r}"
    )
    try:
        message = client.messages.create(
            model=EXTRACTOR_MODEL,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": message_content,
                }
            ],
        )
    except Exception as e:
        logger.error(f"[Extractor] Claude API エラー: {e} ({page_url!r})")
        raise

    # レスポンスからテキストを取得
    raw_text = ""
    if message.content:
        for block in message.content:
            if hasattr(block, "text"):
                raw_text += block.text

    logger.info(
        f"[Extractor] Claude 応答受信: "
        f"入力={message.usage.input_tokens}tok, "
        f"出力={message.usage.output_tokens}tok"
    )

    # JSON を手動パース（messages.parse が pydantic 連携前提の場合の代替）
    import json

    # コードブロック記法（```json ... ```）を除去
    json_text = re.sub(r"```(?:json)?\s*", "", raw_text).strip()
    json_text = re.sub(r"```\s*$", "", json_text).strip()

    try:
        raw_data = json.loads(json_text)
    except json.JSONDecodeError as e:
        logger.error(
            f"[Extractor] JSON パースエラー: {e}\n"
            f"  生レスポンス（先頭200文字）: {raw_text[:200]!r}"
        )
        return []

    # Pydantic でバリデーション
    try:
        result = ExtractionResult.model_validate(raw_data)
    except Exception as e:
        logger.error(f"[Extractor] Pydantic バリデーションエラー: {e}")
        return []

    logger.info(f"[Extractor] 抽出カード数（生）: {len(result.cards)}件")

    # 使用した画像URLリスト（後段の画像取込処理でも参照される）
    used_image_urls = [img["url"] for img in encoded_images]

    # 検証・補正して返却
    output_rows = []
    rush_skipped = 0
    for card in result.cards:
        # ラッシュデュエル（価格比較対象外）と判定されたカードは除外する。
        # 判別不能時は is_rush=false（OCG扱い）になるため、誤って正規カードを落とさない。
        if getattr(card, "is_rush", False):
            rush_skipped += 1
            logger.info(
                f"[Extractor] ラッシュデュエルのため除外: {card.name!r} ({page_url!r})"
            )
            continue

        row = _validate_and_fix(card, page_url)
        if row is None:
            continue

        # このカード個別のメイン画像URLを取り出す。
        # image_url は unreleased_cards テーブルのカラムではないため、
        # トップレベルからは除去し extraction_raw 内にのみ保持する
        # （これを残すと insert 時に「カラムが存在しない」エラーになる）。
        individual_image_url = row.pop("image_url", "")

        # extraction_raw: Claude生出力 + card_image_urls（取得元URL）を格納
        row["extraction_raw"] = {
            "raw_response": raw_text,
            "image_urls": card.image_urls,
            # 記事全体のカード画像URL一覧（後方互換のため残す）
            "card_image_urls": used_image_urls,
            # このカード個別のメイン画像URL（新規追加）
            "card_image_url": individual_image_url,
            "model": EXTRACTOR_MODEL,
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
            "vision_mode": use_vision,
        }

        output_rows.append(row)

    if rush_skipped:
        logger.info(
            f"[Extractor] ラッシュデュエル除外: {rush_skipped}件 ({page_url!r})"
        )
    logger.info(f"[Extractor] 有効カード数（検証後）: {len(output_rows)}件 ({page_url!r})")
    return output_rows
