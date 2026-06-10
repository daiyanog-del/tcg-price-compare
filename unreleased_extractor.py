"""
unreleased_extractor.py — Claude API を使った未発売カード情報抽出モジュール

HTML から遊戯王OCGの未発売カード情報を抽出し、unreleased_cards テーブル用の
dict リストを返す。

依存パッケージ:
  - anthropic: GitHub Actions ワークフロー内でのみ pip install（ローカルでも動作するが
    ANTHROPIC_API_KEY が不要なため、import は起動時でなく関数呼び出し時に遅延実行）
  - beautifulsoup4: requirements.txt に含まれるため常に利用可能

使い方:
  from unreleased_extractor import extract_cards_from_html
  rows = extract_cards_from_html(html_str, page_url)
"""

import logging
import os
import re
from typing import Any
from urllib.parse import urlparse

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

# システムプロンプト（固定・日本語）
_SYSTEM_PROMPT = """\
あなたは遊戯王OCGの新カード情報を公式ページから正確に抽出するアシスタントです。

## 抽出ルール

1. ページに遊戯王OCGの**新カード情報**が掲載されている場合のみ抽出する。
   - カード名（name）が明確に記載されていないカードは出力しない
   - 既存カードの再録情報のみのページでは cards=[] を返す
   - カード情報がないページ（ニュース・イベント・店舗情報等）では cards=[] を返す

2. 各フィールドの扱い:
   - name: 日本語正式カード名。必須。英語名しかない場合も記入する
   - reading: フリガナ。不明なら空文字
   - card_type: モンスター/効果モンスター/融合モンスター/シンクロモンスター/エクシーズモンスター/リンクモンスター/ペンデュラムモンスター/通常モンスター/魔法/罠 など
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
   - image_urls: ページ内に [IMG] タグとして記載されているカード画像のURL候補リスト
   - confidence: 抽出の確信度
     - high: カード名・効果文・ステータスがすべて明確に記載されている
     - medium: 一部情報が不明確または推測が含まれる
     - low: カード名以外ほとんど不明、または情報の信頼性が低い

3. confidence の判断基準:
   - 必須情報（card_typeまたはeffect_text）が欠落している場合は最大でもmedium
   - ページからの読み取りに推測・補完が必要な場合はlowまたはmedium

4. 出力はJSONのみ。説明文は不要。
"""


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

    # 必須情報（card_type または effect_text）が両方欠落している場合は low に降格
    card_type = (card.card_type or "").strip()
    effect_text = (card.effect_text or "").strip()
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
        # extraction_raw はこの時点では未設定（呼び出し元で付加する）
    }


# ──────────────────────────────────────────────
# 公開関数
# ──────────────────────────────────────────────

def extract_cards_from_html(html: str, page_url: str = "") -> list[dict]:
    """HTMLページから遊戯王OCG新カード情報を抽出し、unreleased_cards 行形式の dict リストを返す。

    - HTTPリクエストはこの関数内では行わない（html 引数として受け取る）
    - anthropic パッケージは遅延 import（関数呼び出し時に初めてロード）
    - pydantic は遅延 import（同上）

    Args:
        html:     取得済みHTMLテキスト
        page_url: ページのURL（ログ・source_url・image_urls正規化に使用）

    Returns:
        unreleased_cards テーブル形式の dict リスト。
        各 dict の "extraction_raw" キーに Claude 生出力と image_urls を含む。
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

    # anthropic も型チェック用に遅延import
    try:
        from pydantic import BaseModel
    except ImportError:
        pass

    # HTML前処理
    processed_text = _preprocess_html(html, page_url)
    logger.info(
        f"[Extractor] 前処理完了: {len(html):,}文字 → {len(processed_text):,}文字 ({page_url!r})"
    )

    # Claude API クライアント
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError(
            "環境変数 ANTHROPIC_API_KEY が設定されていません。"
        )

    client = Anthropic(api_key=api_key)

    # 構造化抽出リクエスト
    logger.info(f"[Extractor] Claude API 呼び出し中 (model={EXTRACTOR_MODEL}): {page_url!r}")
    try:
        message = client.messages.create(
            model=EXTRACTOR_MODEL,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "以下のページから遊戯王OCGの新カード情報を抽出してください。\n"
                        "出力はJSON形式で、ExtractionResult スキーマに従ってください。\n\n"
                        "ExtractionResult スキーマ:\n"
                        "{\n"
                        '  "cards": [\n'
                        "    {\n"
                        '      "name": "カード名（必須）",\n'
                        '      "reading": "フリガナ",\n'
                        '      "card_type": "カード種別",\n'
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
                        '      "confidence": "high|medium|low"\n'
                        "    }\n"
                        "  ]\n"
                        "}\n\n"
                        "--- ページ内容 ---\n"
                        f"{processed_text}"
                    ),
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

    # 検証・補正して返却
    output_rows = []
    for card in result.cards:
        row = _validate_and_fix(card, page_url)
        if row is None:
            continue

        # extraction_raw: Claude生出力 + image_urls を格納
        row["extraction_raw"] = {
            "raw_response": raw_text,
            "image_urls": card.image_urls,
            "model": EXTRACTOR_MODEL,
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
        }

        output_rows.append(row)

    logger.info(f"[Extractor] 有効カード数（検証後）: {len(output_rows)}件 ({page_url!r})")
    return output_rows
