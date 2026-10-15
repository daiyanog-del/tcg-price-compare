"""未発売カードの「自動承認してよいか」の判定（シャドーモード）。

2026-10-15 追加。この段階では承認は行わず、判定結果を記録するだけ。
  - 取込時（3経路）: extraction_raw.auto_approve_verdict と extracted_snapshot を保存
  - 承認時（単体/一括）: extraction_raw.approval_snapshot を保存
  - 集計: tools/auto_approve_report.py で「候補なのに手直し/却下された」件数を見て閾値を校正する

Flask / Supabase 接続に依存しない（Cron・自宅PC・管理画面のどこからでも import できる）。
"""

import logging
import math
from datetime import datetime, timezone

from name_normalize import fuzzy_key
from unreleased_quality import lacks_card_body

logger = logging.getLogger(__name__)

VERSION = 1

# ──────────────────────────────────────────────
# 閾値（実データでの校正が必要な仮置き値）
# ──────────────────────────────────────────────

# 切り抜き枠の面積（画像全体に対する割合）の下限。これ未満は別物を囲んでいる疑い。
# TODO: calibrate from data（tools/auto_approve_report.py の結果で見直す）
MIN_CROP_AREA = 0.10

# 切り抜き枠の縦横比（幅/高さ）の許容範囲。遊戯王カードは約0.686（card_crop は 0.60〜0.76 で候補化）。
# 少し広めに取り、極端に細長い/平たい枠だけ弾く。
# TODO: calibrate from data
CROP_ASPECT_MIN = 0.55
CROP_ASPECT_MAX = 0.80

# 画像全体を指す枠とみなす許容誤差（正規化座標）
_WHOLE_IMAGE_EPS = 1e-6

# ──────────────────────────────────────────────
# 理由コードと日本語ラベル
# ──────────────────────────────────────────────

REASON_LABELS = {
    "confidence_not_high": "抽出の信頼度が高ではない",
    "recognition_not_verified": "上部拡大での数値再読が確認済みではない",
    "release_date_not_verified": "発売日が公式情報で確認できていない",
    "crop_not_contour": "切り抜き枠が輪郭検出で確認できていない",
    "crop_invalid": "切り抜き枠の座標が不正",
    "crop_too_small": "切り抜き枠が小さすぎる",
    "crop_not_applied": "公式画像に切り抜きが必要だが承認時に適用されない",
    "lacks_card_body": "カード本体（効果・数値）を読めていない",
    "same_name_exists": "同名のカードが他にも登録されている",
    "same_name_unchecked": "同名カードの有無を確認できなかった",
    "evaluation_error": "判定中にエラーが起きた",
}

# 抽出時点で保存する値（承認時の手直し検出に使う）
SNAPSHOT_FIELDS = (
    "name", "card_type", "attribute", "race", "level", "rank", "link_val",
    "atk", "def", "effect_text", "pendulum_scale", "pendulum_effect",
    "product_name", "release_date",
)


def reason_label(code: str) -> str:
    """理由コードの日本語ラベル。未知コードはコードをそのまま返す。"""
    return REASON_LABELS.get(code, code)


# ──────────────────────────────────────────────
# 切り抜き条件
# ──────────────────────────────────────────────

def _box_from_suggestion(sug: dict) -> dict | None:
    """crop_suggestion から妥当な正規化座標だけを取り出す。不正なら None。"""
    box = {}
    for k in ("left", "top", "right", "bottom"):
        v = sug.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return None
        box[k] = float(v)
    if not (0 <= box["left"] < box["right"] <= 1 and 0 <= box["top"] < box["bottom"] <= 1):
        return None
    return box


def _is_whole_image(box: dict) -> bool:
    return (box["left"] <= _WHOLE_IMAGE_EPS and box["top"] <= _WHOLE_IMAGE_EPS
            and box["right"] >= 1 - _WHOLE_IMAGE_EPS and box["bottom"] >= 1 - _WHOLE_IMAGE_EPS)


def _crop_reason(raw: dict) -> str | None:
    """切り抜き条件を満たさない場合の理由コード。満たせば None。

    X経由（x_tweet）:
      取込時に ingest_x_card_image が画像へ自動クロップを適用する（method が vision でも適用）。
      自動承認では「切り抜き不要（候補なし）」か「輪郭検出（contour）で枠を裏付けられたもの」だけ通す。
      AIの目測だけの枠（vision）は、画像全体を指す場合も含め通さない（保守的）。
    公式サイト経由（official_html）:
      承認時の画像取込（_fetch_and_store_image）は切り抜きを一切行わず、公式のカード画像をそのまま保存する。
      そのため枠が「画像全体」（カード単体の画像）か候補なしなら問題なし。部分枠が付いている場合は、
      切り抜かれないまま公開されるので通さない。
    """
    sug = raw.get("crop_suggestion")
    if sug is None:
        return None  # 切り抜き不要
    if not isinstance(sug, dict):
        return "crop_invalid"
    box = _box_from_suggestion(sug)
    if box is None:
        return "crop_invalid"
    if raw.get("source") == "official_html":
        return None if _is_whole_image(box) else "crop_not_applied"
    if sug.get("method") != "contour":
        return "crop_not_contour"
    if (box["right"] - box["left"]) * (box["bottom"] - box["top"]) < MIN_CROP_AREA:
        return "crop_too_small"
    w, h = sug.get("width"), sug.get("height")
    if (isinstance(w, (int, float)) and isinstance(h, (int, float))
            and not isinstance(w, bool) and not isinstance(h, bool) and w > 0 and h > 0):
        aspect = ((box["right"] - box["left"]) * w) / ((box["bottom"] - box["top"]) * h)
        if not (CROP_ASPECT_MIN <= aspect <= CROP_ASPECT_MAX):
            return "crop_invalid"
    return None


# ──────────────────────────────────────────────
# 判定本体
# ──────────────────────────────────────────────

def evaluate(card: dict, *, other_names=None) -> dict:
    """1枚のカード行（unreleased_cards 形式の dict）が自動承認の条件を満たすか判定する。

    other_names: 同名判定の対象にする「他カード」の名前一覧（rejected を除く。自分自身は含めない）。
                 None のときは同名確認ができなかったものとして不適格にする。
    戻り値: {"eligible": bool, "reasons": [理由コード...], "version": 1, "at": ISO(UTC)}

    recognition_review の扱い:
      enrich_rows（unreleased_quality.py）は魔法・罠も含む全カードに上部拡大の再読をかけ、
      数値が全てnullであること・読めたこと(readable)を検証して verified にする。
      つまり「再読対象外」は存在せず、画像/枠が無い場合は needs_review が付く。
      よって verified 以外（未設定の旧データを含む）は全て不適格とする。
    """
    reasons: list[str] = []
    raw = card.get("extraction_raw") or {}

    if card.get("confidence") != "high":
        reasons.append("confidence_not_high")

    review = raw.get("recognition_review")
    if not (isinstance(review, dict) and review.get("status") == "verified"):
        reasons.append("recognition_not_verified")

    evidence = raw.get("release_date_evidence")
    if not (isinstance(evidence, dict) and evidence.get("status") == "verified"):
        reasons.append("release_date_not_verified")

    crop = _crop_reason(raw)
    if crop:
        reasons.append(crop)

    if lacks_card_body(card):
        reasons.append("lacks_card_body")

    if other_names is None:
        reasons.append("same_name_unchecked")
    else:
        key = fuzzy_key(card.get("name", ""))
        if key and any(fuzzy_key(n) == key for n in other_names if n):
            reasons.append("same_name_exists")

    return {
        "eligible": not reasons,
        "reasons": reasons,
        "version": VERSION,
        "at": datetime.now(timezone.utc).isoformat(),
    }


# ──────────────────────────────────────────────
# 取込時の記録
# ──────────────────────────────────────────────

def build_extracted_snapshot(row: dict) -> dict:
    """抽出時点の値のスナップショット（承認時の手直し検出用）。"""
    return {k: row.get(k) for k in SNAPSHOT_FIELDS}


def annotate_row(row: dict, other_names) -> dict:
    """挿入直前の行に auto_approve_verdict と extracted_snapshot を付ける。判定結果を返す。

    判定が例外で落ちても取込は止めない（evaluation_error として記録する）。
    """
    try:
        verdict = evaluate(row, other_names=other_names)
    except Exception as e:  # 取込本体を止めない
        logger.warning("[auto_approve] 判定失敗 (%s): %s", row.get("name", "?"), e)
        verdict = {"eligible": False, "reasons": ["evaluation_error"], "version": VERSION,
                   "at": datetime.now(timezone.utc).isoformat()}
    raw = row.get("extraction_raw")
    if not isinstance(raw, dict):
        raw = {}
        row["extraction_raw"] = raw
    # TODO: 将来の自動承認の差し込み位置。ここで verdict["eligible"] を見て status を approved にする
    #       （今回はシャドーモード＝記録のみ。実際に承認するスイッチは作っていない）。
    raw["auto_approve_verdict"] = verdict
    raw["extracted_snapshot"] = build_extracted_snapshot(row)
    return verdict


_NAME_PAGE = 1000


def load_active_names(sb) -> list[str] | None:
    """rejected 以外の全カード名を返す（同名判定用）。取得に失敗したら None。"""
    names: list[str] = []
    try:
        offset = 0
        while True:
            resp = (sb.table("unreleased_cards").select("name")
                    .neq("status", "rejected")
                    .range(offset, offset + _NAME_PAGE - 1).execute())
            rows = list(resp.data or [])
            names.extend(r["name"] for r in rows if r.get("name"))
            if len(rows) < _NAME_PAGE:
                return names
            offset += _NAME_PAGE
    except Exception as e:
        logger.warning("[auto_approve] 既存カード名の取得に失敗（同名確認なしで記録）: %s", e)
        return None


# ──────────────────────────────────────────────
# 承認時の記録
# ──────────────────────────────────────────────

def _norm(v) -> str:
    """比較用の正規化。None と空文字、1 と 1.0 を同一視する。"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def compute_edited_fields(snapshot, current: dict):
    """抽出時スナップショットと現在値の差を、変わったフィールド名のリストで返す。

    スナップショットが無い旧データは None（「手直しなし」と区別するため）。
    """
    if not isinstance(snapshot, dict):
        return None
    return [k for k in SNAPSHOT_FIELDS
            if k in snapshot and _norm(snapshot.get(k)) != _norm(current.get(k))]


def build_approval_snapshot(card: dict, via: str, now_iso: str | None = None) -> dict:
    """承認時の記録を作る。card は承認直前の行（extraction_raw と各フィールドを含む）。"""
    raw = card.get("extraction_raw") or {}
    verdict = raw.get("auto_approve_verdict")
    crop_state = raw.get("crop_state")
    return {
        "at": now_iso or datetime.now(timezone.utc).isoformat(),
        "via": via,
        "edited_fields": compute_edited_fields(raw.get("extracted_snapshot"), card),
        "crop_mode": crop_state.get("mode") if isinstance(crop_state, dict) else None,
        "verdict_eligible": verdict.get("eligible") if isinstance(verdict, dict) else None,
    }


_CHUNK = 100


def save_approval_snapshots(sb, entries: list[tuple[int, dict]]) -> int:
    """approval_snapshot を extraction_raw に保存する。保存できた件数を返す。失敗は警告ログのみ。

    他の書き込み（クロップ状態など）を消さないよう、100件ずつ最新の extraction_raw を
    まとめて読み直してから、1件ずつ書き戻す（読みは100件=1回、書きは1件=1回）。
    """
    saved = 0
    for start in range(0, len(entries), _CHUNK):
        chunk = entries[start:start + _CHUNK]
        ids = [cid for cid, _ in chunk]
        try:
            resp = sb.table("unreleased_cards").select("id, extraction_raw").in_("id", ids).execute()
            latest = {r["id"]: dict(r.get("extraction_raw") or {}) for r in (resp.data or [])}
        except Exception as e:
            logger.warning("[auto_approve] extraction_raw 読み直し失敗（承認記録を保存できず）: %s", e)
            continue
        for cid, snap in chunk:
            if cid not in latest:
                continue
            raw = latest[cid]
            raw["approval_snapshot"] = snap
            try:
                sb.table("unreleased_cards").update({"extraction_raw": raw}).eq("id", cid).execute()
                saved += 1
            except Exception as e:
                logger.warning("[auto_approve] 承認記録の保存失敗 card_id=%s: %s", cid, e)
    return saved
