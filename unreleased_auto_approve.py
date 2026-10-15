"""未発売カードの「自動承認してよいか」の判定と記録。

2026-10-15 追加。このモジュールは判定と記録だけを行う（承認そのものは unreleased_approve.py）。
  - 取込時（3経路）: extraction_raw.auto_approve_verdict と extracted_snapshot を保存
  - 承認時（単体/一括/自動）: extraction_raw.approval_snapshot を保存（via = single / bulk / auto）
  - 集計: tools/auto_approve_report.py で「候補なのに手直し/却下された」件数を見て閾値を校正する
  - 自動承認の実行: AUTO_APPROVE_ENABLED=1 のとき、取込3経路が挿入（と画像保存）の後に
    unreleased_approve.maybe_auto_approve を呼ぶ。判定が通ったものだけ approve_card(via="auto")

Flask / Supabase 接続に依存しない（Cron・自宅PC・管理画面のどこからでも import できる）。
"""

import logging
import math
import threading
import time
from datetime import datetime, timezone

import unreleased_text_check
from name_normalize import fuzzy_key
from unreleased_quality import lacks_card_body

logger = logging.getLogger(__name__)

# 2: 効果テキストの誤字検証（text_suspect / text_unchecked）を条件に追加（2026-10-15）
# 3: 誤字検証に言い回しの照合（unknown_phrase の強い疑義も text_suspect になる）を追加（2026-10-15）
VERSION = 3

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
    "text_suspect": "効果テキストに誤字・言い回しの誤りの強い疑いがある",
    "text_unchecked": "効果テキストの誤字を検証できなかった（辞書なし・フレーバーテキスト等）",
    "empty_text": "効果テキストが空（通常モンスターのフレーバーテキストも含め、読めていない）",
    "same_source_name_seen": "同じ投稿元で同名のカードを以前取り込み済み（却下・削除済みの再取込の可能性）",
    "no_visible_image": "公開用の画像がまだ無い（画像が揃うまで自動承認しない）",
    "text_too_long": "効果テキストが長すぎて言い回しを照合していない",
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

def compute_text_check(card: dict, other_names=None, text_context=None, *, context_has_self=False) -> dict:
    """効果テキストの誤字検証（unreleased_text_check.check_card）。例外は握りつぶさず「未検証」として返す。

    戻り値は extraction_raw.text_check にそのまま保存できる形（version つき）。
    context_has_self: text_context に検査対象カード自身が含まれているか（DB登録済みのカードの再検査は True）。
    """
    try:
        return unreleased_text_check.check_card(
            card, extra_names=[n for n in (other_names or []) if n], context=text_context,
            context_has_self=context_has_self)
    except Exception as e:  # 取込本体を止めない。検証できなかった事実は残す（疑義なし扱いにはしない）
        logger.warning("[auto_approve] 効果テキスト検証に失敗 (%s): %s", card.get("name", "?"), e)
        return {"version": unreleased_text_check.VERSION, "status": "unchecked", "reason": "error",
                "strong": 0, "weak": 0, "suspects": [], "unverified_fields": []}


def _text_reason(text_check) -> str | None:
    """text_check の結果から理由コードを返す。問題なければ None。

    強い疑義が無いことだけを条件にする（弱い疑義は参考表示で、承認条件に使わない）。
    n-gram 照合できなかった欄（通常モンスターのフレーバー等）がある場合は未検証として不適格にする。
    """
    if not isinstance(text_check, dict) or text_check.get("status") != "checked":
        return "text_unchecked"
    if text_check.get("unverified_fields"):
        return "text_unchecked"
    if text_check.get("strong"):
        return "text_suspect"
    return None


def evaluate(card: dict, *, other_names=None, text_check=None, text_context=None) -> dict:
    """1枚のカード行（unreleased_cards 形式の dict）が自動承認の条件を満たすか判定する。

    other_names: 同名判定の対象にする「他カード」の名前一覧（rejected を除く。自分自身は含めない）。
                 None のときは同名確認ができなかったものとして不適格にする。
    text_check: compute_text_check の結果（呼び出し側で計算済みのとき）。None なら内部で計算する。
    text_context: 内部で計算するときの TextContext（他の未発売カードの「」内語）。
    戻り値: {"eligible": bool, "reasons": [理由コード...], "version": VERSION, "at": ISO(UTC)}

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

    # 効果テキストが空のカードは（魔法・罠・モンスターを問わず）自動承認しない。
    # 通常モンスターのフレーバーテキストも effect_text に入るので、空なら読み落とし
    effect = card.get("effect_text")
    if not (isinstance(effect, str) and effect.strip()):
        reasons.append("empty_text")

    if text_check is None:
        text_check = compute_text_check(card, other_names, text_context)
    text_reason = _text_reason(text_check)
    if text_reason:
        reasons.append(text_reason)
    # 長すぎて言い回しの照合をスキップした文章は、検証できていないので自動承認しない
    if isinstance(text_check, dict) and any(
            s.get("kind") == "text_too_long" for s in (text_check.get("suspects") or [])):
        reasons.append("text_too_long")

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


def annotate_row(row: dict, other_names, text_context=None) -> dict:
    """挿入直前の行に auto_approve_verdict・extracted_snapshot・text_check を付ける。判定結果を返す。

    判定が例外で落ちても取込は止めない（evaluation_error として記録する）。
    text_context: load_text_context の結果（省略可）。渡した場合、この行の「」内語も文脈に足す。
    """
    text_check = compute_text_check(row, other_names, text_context)
    try:
        verdict = evaluate(row, other_names=other_names, text_check=text_check)
    except Exception as e:  # 取込本体を止めない
        logger.warning("[auto_approve] 判定失敗 (%s): %s", row.get("name", "?"), e)
        verdict = {"eligible": False, "reasons": ["evaluation_error"], "version": VERSION,
                   "at": datetime.now(timezone.utc).isoformat()}
    raw = row.get("extraction_raw")
    if not isinstance(raw, dict):
        raw = {}
        row["extraction_raw"] = raw
    # ここでは記録だけ。自動承認（AUTO_APPROVE_ENABLED=1）は、呼び出し側が挿入と画像の保存を終えた後に
    # unreleased_approve.maybe_auto_approve を呼んで行う（承認は画像が揃ってからでないと X 投稿に進めないため）
    raw["auto_approve_verdict"] = verdict
    raw["text_check"] = text_check
    # 記録の作成で取込を止めない（文字列でない値などが混ざっていても警告ログだけで続ける）
    try:
        raw["extracted_snapshot"] = build_extracted_snapshot(row)
    except Exception as e:
        logger.warning("[auto_approve] 抽出時スナップショット作成失敗 (%s): %s", row.get("name", "?"), e)
    if text_context is not None:
        try:
            text_context.add_card(row)
        except Exception as e:
            logger.warning("[auto_approve] 文脈への追加失敗 (%s): %s", row.get("name", "?"), e)
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


def load_text_context(sb):
    """rejected 以外の全カードの「」内語を集めた TextContext を返す。取得に失敗したら None。

    新弾の新カード名・テーマ名は同じ弾の複数のカードに出るので、②（「」内の名前）で既知扱いにする材料。
    """
    ctx = unreleased_text_check.TextContext()
    try:
        offset = 0
        while True:
            resp = (sb.table("unreleased_cards").select("effect_text, pendulum_effect")
                    .neq("status", "rejected")
                    .range(offset, offset + _NAME_PAGE - 1).execute())
            rows = list(resp.data or [])
            for r in rows:
                ctx.add_card(r)
            if len(rows) < _NAME_PAGE:
                return ctx
            offset += _NAME_PAGE
    except Exception as e:
        logger.warning("[auto_approve] 既存テキストの取得に失敗（「」内語の文脈なしで検証）: %s", e)
        return None


class CardContext:
    """全カード（rejected 以外）の名前と「」内語を id 別に持つ読み込み結果。

    編集保存・再検査では「このカード自身を除いた」他カードの材料が要る。id 別に持っておけば、
    自分を除く処理が正確になり（rejected など元から含まれていないカードの分を誤って引かない）、
    一度の全件読み込みを短時間キャッシュして使い回せる。
    """

    def __init__(self, names: dict, quotes: dict):
        self.names = names      # {id: name}
        self.quotes = quotes    # {id: 「」内語キーの集合}

    def excluding(self, card_id):
        """(自分を除いた他カードの名前一覧, 自分を除いた TextContext)。"""
        ctx = unreleased_text_check.TextContext()
        for cid, keys in self.quotes.items():
            if cid == card_id:
                continue
            for k in keys:
                ctx.word_df[k] = ctx.word_df.get(k, 0) + 1
        others = [n for cid, n in self.names.items() if cid != card_id and n]
        return others, ctx


def load_card_context(sb) -> "CardContext | None":
    """rejected 以外の全カードを1回だけ読んで CardContext にする（名前・「」内語を同時に）。失敗したら None。"""
    names: dict = {}
    quotes: dict = {}
    try:
        offset = 0
        while True:
            resp = (sb.table("unreleased_cards").select("id, name, effect_text, pendulum_effect")
                    .neq("status", "rejected")
                    .range(offset, offset + _NAME_PAGE - 1).execute())
            rows = list(resp.data or [])
            for r in rows:
                names[r["id"]] = r.get("name") or ""
                try:
                    quotes[r["id"]] = unreleased_text_check.TextContext.contrib(r)
                except Exception:
                    quotes[r["id"]] = set()
            if len(rows) < _NAME_PAGE:
                return CardContext(names, quotes)
            offset += _NAME_PAGE
    except Exception as e:
        logger.warning("[auto_approve] 既存カードの文脈取得に失敗: %s", e)
        return None


# 編集保存のたびに全件を読み直さないためのキャッシュ（秒）。他カードの変化は最大この秒数遅れて反映される。
# TODO: calibrate from data
CONTEXT_CACHE_TTL_SEC = 60
_context_cache = {"at": 0.0, "sb": None, "ctx": None}
_context_cache_lock = threading.Lock()


def get_card_context_cached(sb, *, ttl: float | None = None, clock=time.monotonic):
    """load_card_context の結果を ttl 秒キャッシュして返す。ttl=0 なら必ず読み直す。失敗時は None。"""
    ttl = CONTEXT_CACHE_TTL_SEC if ttl is None else ttl
    with _context_cache_lock:
        now = clock()
        if (ttl > 0 and _context_cache["ctx"] is not None and _context_cache["sb"] is sb
                and now - _context_cache["at"] < ttl):
            return _context_cache["ctx"]
        ctx = load_card_context(sb)
        if ctx is not None:
            _context_cache.update(at=now, sb=sb, ctx=ctx)
        return ctx


def recompute_card_checks(sb, card: dict) -> dict:
    """編集後のカード行で text_check と auto_approve_verdict を計算し直して返す（保存はしない）。

    card は編集後の全列を含む行（extraction_raw も）。文脈は短時間キャッシュ（自分自身は id で除く）。
    文脈が取れないときは同名確認不可（other_names=None）として不適格側に倒す。
    """
    ctx = get_card_context_cached(sb)
    if ctx is not None:
        others, tctx = ctx.excluding(card.get("id"))
    else:
        others, tctx = None, None
    text_check = compute_text_check(card, others, tctx, context_has_self=False)
    verdict = evaluate(card, other_names=others, text_check=text_check)
    return {"text_check": text_check, "auto_approve_verdict": verdict}


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
    # 承認時点（手直し後）の値での疑義数。「疑義ありだったが直さず承認」＝誤検知の数え上げに使う。
    # 辞書だけで数える（取込時の文脈は使わない）ため、取込時の text_check と数が違うことがある
    now_check = compute_text_check(card, None, None)
    return {
        "at": now_iso or datetime.now(timezone.utc).isoformat(),
        "via": via,
        "edited_fields": compute_edited_fields(raw.get("extracted_snapshot"), card),
        "crop_mode": crop_state.get("mode") if isinstance(crop_state, dict) else None,
        "verdict_eligible": verdict.get("eligible") if isinstance(verdict, dict) else None,
        "text_check": {
            "version": now_check.get("version"),
            "status": now_check.get("status"),
            "strong": now_check.get("strong", 0),
            "weak": now_check.get("weak", 0),
            # 種別ごとの強の数。承認時は他カード名の文脈が無く unknown_name が増えるので、
            # 集計では unknown_ngram / unknown_phrase / format を主に見る
            "strong_by_kind": _count_by_kind(now_check, "strong"),
        },
    }


def _count_by_kind(text_check: dict, severity: str) -> dict:
    out: dict = {}
    for sus in text_check.get("suspects") or []:
        if sus.get("severity") == severity:
            out[sus.get("kind")] = out.get(sus.get("kind"), 0) + 1
    return out


def build_approval_snapshot_safe(card: dict, via: str) -> dict:
    """build_approval_snapshot が例外でも、最低限の記録 {"at","via","error"} を返す。

    X 自動投稿は approval_snapshot があるカードだけを対象にする（このデプロイ以降の承認の印）ため、
    記録の作成失敗で承認済みなのに永久に投稿されなくなることを避ける。詳細が無いだけで印は残る。
    """
    try:
        return build_approval_snapshot(card, via)
    except Exception as e:
        logger.warning("[auto_approve] 承認記録の作成に失敗（最小記録で代替）card_id=%s: %s", card.get("id"), e)
        return {"at": datetime.now(timezone.utc).isoformat(), "via": via, "error": f"{type(e).__name__}: {e}"[:300]}


def record_approval_snapshots(sb, cards: list[dict], via: str, failed_ids: list | None = None) -> int:
    """承認したカードの承認記録を作って保存する。例外は投げない（承認・画像取込・投稿を止めない）。

    保存できた件数を返す。承認記録は計測用で、X 投稿の条件ではない（条件は approved_at）。
    failed_ids を渡すと、保存できなかったカードの id がそこに追加される（欠落の通知用）。
    """
    try:
        entries = [(c["id"], build_approval_snapshot_safe(c, via)) for c in cards]
        return save_approval_snapshots(sb, entries, failed_ids)
    except Exception as e:
        logger.warning("[auto_approve] 承認記録の保存でエラー（承認は成立済み）: %s", e)
        if failed_ids is not None:
            failed_ids.extend(c["id"] for c in cards if c["id"] not in failed_ids)
        return 0


_CHUNK = 100


def save_approval_snapshots(sb, entries: list[tuple[int, dict]], failed_ids: list | None = None) -> int:
    """approval_snapshot を extraction_raw に保存する。保存できた件数を返す。失敗は警告ログのみ。

    他の書き込み（クロップ状態など）を消さないよう、100件ずつ最新の extraction_raw を
    まとめて読み直してから、1件ずつ書き戻す（読みは100件=1回、書きは1件=1回）。
    保存できなかったカード id は failed_ids（渡されたとき）に追加する。
    """
    def _fail(*cids):
        if failed_ids is not None:
            failed_ids.extend(c for c in cids if c not in failed_ids)

    saved = 0
    for start in range(0, len(entries), _CHUNK):
        chunk = entries[start:start + _CHUNK]
        ids = [cid for cid, _ in chunk]
        try:
            resp = sb.table("unreleased_cards").select("id, extraction_raw").in_("id", ids).execute()
            latest = {r["id"]: dict(r.get("extraction_raw") or {}) for r in (resp.data or [])}
        except Exception as e:
            logger.warning("[auto_approve] extraction_raw 読み直し失敗（承認記録を保存できず）: %s", e)
            _fail(*ids)
            continue
        for cid, snap in chunk:
            if cid not in latest:
                _fail(cid)
                continue
            raw = latest[cid]
            raw["approval_snapshot"] = snap
            try:
                sb.table("unreleased_cards").update({"extraction_raw": raw}).eq("id", cid).execute()
                saved += 1
            except Exception as e:
                logger.warning("[auto_approve] 承認記録の保存失敗 card_id=%s: %s", cid, e)
                _fail(cid)
    return saved
