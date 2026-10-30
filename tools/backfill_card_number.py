"""
backfill_card_number.py — 型番（card_number）が空の未発売カードを埋める一回限りのスクリプト（2026-10-30）

背景:
  未発売カード一覧の「型番順」のため unreleased_cards.card_number を足した。これから取り込むカードは
  抽出時に入る（unreleased_extractor / ocgtimes_reveal）が、足す前に取り込んだカードは空のまま。

埋め方（unreleased_card_number と同じ優先順）:
  1. 公式サイト由来: extraction_raw の画像URL（card_image_url / source_image_url）から型番を復号する（AI 不要・確実）
  2. それ以外（X・YouTube・画像アップロード）: --ai のときだけ、保存済みのカード画像（official_card_images の
     切り抜き後の画像）を AI（QUALITY_MODEL）に見せて型番を読ませる。形の合わない読み・空は埋めない

対象:
  status IN ('pending','approved') かつ card_number IS NULL（linked は一覧から外れるので対象外）

冪等性:
  card_number が空の行だけを対象にするので、再実行しても埋めた行には触らない。

実行:
  python tools/backfill_card_number.py --dry-run        # URL から取れる分と、AI で読む対象の一覧だけ
  python tools/backfill_card_number.py                  # URL から取れる分だけ書き込む
  python tools/backfill_card_number.py --ai             # URL の分＋AI で読んだ分を書き込む
  python tools/backfill_card_number.py --ai --dry-run   # AI で読むが書き込まない（読みの確認）

認証:
  SUPABASE_URL / SUPABASE_KEY / ANTHROPIC_API_KEY。未設定時は ~/.cardsouba/watcher.env から補完する。
"""

import base64
import io
import os
import sys
import logging

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
except Exception:
    pass

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from unreleased_card_number import card_number_from_image_url, normalize_card_number  # noqa: E402


def _load_env_fallback() -> None:
    """未設定の環境変数を ~/.cardsouba/watcher.env から補完する（既にある値は上書きしない）。"""
    env_path = os.path.join(os.path.expanduser("~"), ".cardsouba", "watcher.env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if key and key not in os.environ:
                os.environ[key] = val


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("backfill_card_number")

_READ_SYSTEM = (
    "あなたは遊戯王OCGのカード画像から型番を書き写すアシスタントです。"
    "型番はカードの絵の右下（効果文の枠のすぐ上）に小さく印刷されている（例 IMPH-JP012・IMPH-JPS13）。"
    "小さくて読み切れない字が1つでもあれば推測せず card_number を空文字にする。"
)
_READ_SCHEMA = {"type": "object", "properties": {"card_number": {"type": "string"}}, "required": ["card_number"]}


def _targets(sb) -> list[dict]:
    resp = (
        sb.table("unreleased_cards")
        .select("id, name, product_name, source_domain, status, extraction_raw")
        .in_("status", ["pending", "approved"])
        .is_("card_number", "null")
        .order("id")
        .execute()
    )
    return resp.data or []


def _stored_images(sb, ids: list[int]) -> dict[int, str]:
    """カードID → 保存済みの表示用画像（切り抜き後）の URL。"""
    if not ids:
        return {}
    resp = (
        sb.table("official_card_images")
        .select("unreleased_card_id, public_url")
        .in_("unreleased_card_id", ids)
        .eq("hidden", False)
        .is_("deleted_at", "null")
        .execute()
    )
    return {r["unreleased_card_id"]: r["public_url"] for r in (resp.data or []) if r.get("public_url")}


def _read_by_ai(client, model: str, url: str) -> str:
    import requests
    from unreleased_quality import structured_request
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    media_type = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                     "data": base64.b64encode(resp.content).decode("ascii")}},
        {"type": "text", "text": "このカードの型番を書き写してください。"},
    ]
    data, _, _ = structured_request(client, model=model, system=_READ_SYSTEM, content=content,
                                    schema=_READ_SCHEMA, max_tokens=2000)
    return normalize_card_number(data.get("card_number"))


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    use_ai = "--ai" in sys.argv
    _load_env_fallback()
    from supabase import create_client
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

    rows = _targets(sb)
    logger.info("型番が空の行: %d件", len(rows))
    decided: list[tuple[dict, str, str]] = []   # (行, 型番, 出どころ)
    rest: list[dict] = []
    for row in rows:
        raw = row.get("extraction_raw") or {}
        number = card_number_from_image_url(raw.get("card_image_url") or "") \
            or card_number_from_image_url(raw.get("source_image_url") or "")
        if number:
            decided.append((row, number, "image_url"))
        else:
            rest.append(row)
    logger.info("画像URLから取れた: %d件 / AI で読む対象: %d件", len(decided), len(rest))

    unread: list[dict] = []
    if use_ai and rest:
        from anthropic import Anthropic
        from unreleased_extractor import QUALITY_MODEL
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], timeout=120.0, max_retries=1)
        images = _stored_images(sb, [r["id"] for r in rest])
        for row in rest:
            url = images.get(row["id"])
            if not url:
                logger.warning("保存済みの画像が無いので読めません: id=%s %s", row["id"], row["name"])
                unread.append(row)
                continue
            try:
                number = _read_by_ai(client, QUALITY_MODEL, url)
            except Exception as e:  # 1枚の失敗で全体を止めない（次回の実行で読み直せる）
                logger.warning("AI の読み取りに失敗: id=%s %s (%s)", row["id"], row["name"], e)
                unread.append(row)
                continue
            if number:
                decided.append((row, number, "ai"))
            else:
                unread.append(row)
    else:
        unread = rest

    for row, number, source in decided:
        print(f"{row['id']}\t{number}\t{source}\t{row.get('product_name') or ''}\t{row['name']}")
    for row in unread:
        print(f"{row['id']}\t(空)\t-\t{row.get('product_name') or ''}\t{row['name']}")

    if dry_run:
        logger.info("--dry-run のため書き込みません（埋まる予定 %d件・空のまま %d件）", len(decided), len(unread))
        return 0
    written = 0
    for row, number, _ in decided:
        # 途中で管理画面から入れた値を上書きしないよう、空の行だけを更新する
        sb.table("unreleased_cards").update({"card_number": number}).eq("id", row["id"]).is_("card_number", "null").execute()
        written += 1
    logger.info("書き込み: %d件・空のまま: %d件", written, len(unread))
    return 0


if __name__ == "__main__":
    sys.exit(main())
