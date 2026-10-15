"""
auto_approve_report.py — 自動承認判定（シャドーモード）の集計

extraction_raw.auto_approve_verdict が付いた承認済み/紐付け済み/却下のカードを集計し、
判定の閾値校正に必要な数字を出す。

見るところ:
  - 候補(eligible)なのに承認時に手直しされた（approval_snapshot.edited_fields が空でない）
  - 候補なのに却下された
  - 候補でないのに手直しなしで承認された（条件が厳しすぎる可能性。理由コード別）
  - 効果テキストの誤字検証（extraction_raw.text_check）: 抽出時に強い疑義が付いたカードを、
    承認時に本文を手直ししたか（検出が役立った）／手直しせず承認したか（誤検知、または人の見落とし）で数える

  - confidence × uncertain_fields（抽出AIが「不確か」とした欄）: 信頼度別の最終結果（手直しなし承認／手直しあり承認／却下）と、
    medium で不確かとされた欄ごとに「その欄が承認時に手直しされたか」（自己申告の当たり率）

実行:
  python tools/auto_approve_report.py            # 本番Supabaseを読むだけ（書き込みなし）

認証: SUPABASE_URL / SUPABASE_KEY（未設定なら ~/.cardsouba/watcher.env から補完）
"""

import io
import os
import sys
from collections import Counter

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_PAGE = 200


def _load_env_fallback() -> None:
    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY"):
        return
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


def summarize(cards: list[dict]) -> dict:
    """カード行（id, name, status, extraction_raw）から集計結果を作る。純粋関数。

    対象は verdict が付いていて status が approved/linked/rejected のもののみ。
    """
    total = Counter()
    eligible_edited: list[dict] = []
    eligible_rejected: list[dict] = []
    ineligible_clean_reasons = Counter()
    ineligible_clean = 0
    eligible_approved = 0
    text = {"checked": 0, "flagged": [], "flagged_fixed": [], "flagged_unfixed": [], "weak_only": 0,
            "approval_flagged_unedited": []}
    conf = {"by_confidence": {}, "by_field": {}, "medium_without_fields": 0}
    for c in cards:
        raw = c.get("extraction_raw") or {}
        verdict = raw.get("auto_approve_verdict")
        status = c.get("status")
        if not isinstance(verdict, dict) or status not in ("approved", "linked", "rejected"):
            continue
        eligible = bool(verdict.get("eligible"))
        total[(eligible, "rejected" if status == "rejected" else "approved")] += 1
        snap = raw.get("approval_snapshot") if isinstance(raw.get("approval_snapshot"), dict) else {}
        edited = snap.get("edited_fields")  # None=記録なし（承認記録の導入前など）
        _tally_confidence(conf, c, raw, status, edited)
        if status != "rejected":
            _tally_text_check(text, c, raw, snap, edited)
        if status == "rejected":
            if eligible:
                eligible_rejected.append({"id": c.get("id"), "name": c.get("name")})
            continue
        if eligible:
            eligible_approved += 1
            if edited:
                eligible_edited.append({"id": c.get("id"), "name": c.get("name"), "edited_fields": edited})
        elif edited == []:
            ineligible_clean += 1
            ineligible_clean_reasons.update(verdict.get("reasons") or [])
    return {
        "total": dict(total),
        "eligible_approved": eligible_approved,
        "eligible_edited": eligible_edited,
        "eligible_rejected": eligible_rejected,
        "ineligible_clean": ineligible_clean,
        "ineligible_clean_reasons": dict(ineligible_clean_reasons),
        "text_check": text,
        "confidence": {
            "by_confidence": {k: dict(v) for k, v in conf["by_confidence"].items()},
            "by_field": {k: dict(v) for k, v in conf["by_field"].items()},
            "medium_without_fields": conf["medium_without_fields"],
        },
    }


_TEXT_FIELDS = ("effect_text", "pendulum_effect")

# uncertain_fields の値 → 承認時の手直し検出（SNAPSHOT_FIELDS）で対応する列
_UNCERTAIN_TO_SNAPSHOT = {
    "level_rank_link": ("level", "rank", "link_val"),
}


def _tally_confidence(conf: dict, card: dict, raw: dict, status: str, edited) -> None:
    """confidence 別の最終結果と、medium の uncertain_fields ごとの手直し率を数える。"""
    c = card.get("confidence") or "(なし)"
    if status == "rejected":
        outcome = "rejected"
    elif edited is None:
        outcome = "approved_unknown"   # 承認記録の導入前
    elif edited:
        outcome = "approved_edited"
    else:
        outcome = "approved_clean"
    conf["by_confidence"].setdefault(c, Counter())[outcome] += 1
    if c != "medium":
        return
    fields = raw.get("uncertain_fields")
    if not isinstance(fields, list) or not fields:
        conf["medium_without_fields"] += 1
        return
    for f in dict.fromkeys(fields):
        row = conf["by_field"].setdefault(f, Counter())
        row["n"] += 1
        if status == "rejected":
            row["rejected"] += 1
        elif edited is not None:
            row["judged"] += 1
            if any(k in edited for k in _UNCERTAIN_TO_SNAPSHOT.get(f, (f,))):
                row["edited_that_field"] += 1


def _tally_text_check(text: dict, card: dict, raw: dict, snap: dict, edited) -> None:
    """効果テキスト検証の集計。承認済み/紐付け済みで、抽出時の text_check があるものが対象。"""
    tc = raw.get("text_check")
    if not isinstance(tc, dict) or tc.get("status") != "checked":
        return
    text["checked"] += 1
    item = {"id": card.get("id"), "name": card.get("name"),
            "examples": [s.get("text") for s in (tc.get("suspects") or []) if s.get("severity") == "strong"][:3]}
    if tc.get("strong"):
        text["flagged"].append(item)
        if edited is None:
            return  # 承認時の記録が無い旧データは手直しの有無を判断できない
        if any(f in _TEXT_FIELDS for f in edited):
            text["flagged_fixed"].append(item)
        else:
            text["flagged_unfixed"].append(item)
    elif tc.get("weak"):
        text["weak_only"] += 1
    # 承認時点でも強い疑義が残っているのに本文を直さず承認したもの
    now = snap.get("text_check") if isinstance(snap.get("text_check"), dict) else {}
    by_kind = now.get("strong_by_kind") or {}
    reliable = by_kind.get("unknown_ngram", 0) + by_kind.get("unknown_phrase", 0) + by_kind.get("format", 0)
    if reliable and edited is not None and not any(f in _TEXT_FIELDS for f in edited):
        text["approval_flagged_unedited"].append(item)


def _fetch_cards(sb) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        batch = (sb.table("unreleased_cards").select("id, name, status, extraction_raw")
                 .in_("status", ["approved", "linked", "rejected"])
                 .order("id").range(offset, offset + _PAGE - 1).execute().data or [])
        rows.extend(batch)
        if len(batch) < _PAGE:
            return rows
        offset += _PAGE


def print_report(result: dict) -> None:
    from unreleased_auto_approve import reason_label
    t = result["total"]
    print("=== 自動承認判定の集計（シャドーモード）===")
    print(f"判定あり・承認済み: 候補 {t.get((True, 'approved'), 0)} 件 / 候補でない {t.get((False, 'approved'), 0)} 件")
    print(f"判定あり・却下:     候補 {t.get((True, 'rejected'), 0)} 件 / 候補でない {t.get((False, 'rejected'), 0)} 件")
    print()
    print(f"候補なのに承認時に手直しされた: {len(result['eligible_edited'])} / 候補で承認 {result['eligible_approved']} 件")
    for r in result["eligible_edited"]:
        print(f"  - id={r['id']} {r['name']}: {', '.join(r['edited_fields'])}")
    print()
    print(f"候補なのに却下された: {len(result['eligible_rejected'])} 件")
    for r in result["eligible_rejected"]:
        print(f"  - id={r['id']} {r['name']}")
    print()
    print(f"候補でないが手直しなしで承認された: {result['ineligible_clean']} 件（条件が厳しすぎる可能性。理由の内訳）")
    for code, n in sorted(result["ineligible_clean_reasons"].items(), key=lambda kv: -kv[1]):
        print(f"  - {reason_label(code)} ({code}): {n}")
    print()
    print("※ 手直しの記録(approval_snapshot)が無い旧データは「手直しなし」に数えていません。")
    cf = result.get("confidence")
    if cf and cf["by_confidence"]:
        print()
        print("=== 信頼度 × AIが不確かとした欄（判定あり・承認済み/紐付け済み/却下）===")
        for k in ("high", "medium", "low"):
            v = cf["by_confidence"].get(k)
            if v:
                print(f"  {k}: 手直しなし承認 {v.get('approved_clean', 0)} / 手直しあり承認 {v.get('approved_edited', 0)}"
                      f" / 却下 {v.get('rejected', 0)} / 記録なし承認 {v.get('approved_unknown', 0)}")
        print(f"  medium で uncertain_fields が無い（旧データ・AIが出さなかった）: {cf['medium_without_fields']} 件")
        for f, v in sorted(cf["by_field"].items(), key=lambda kv: -kv[1].get("n", 0)):
            print(f"  - 不確か欄 {f}: {v.get('n', 0)} 件 / うち承認でその欄を手直し {v.get('edited_that_field', 0)}"
                  f"（判定可能 {v.get('judged', 0)}）/ 却下 {v.get('rejected', 0)}")
    tx = result.get("text_check")
    if tx:
        print()
        print("=== 効果テキストの誤字検証（承認済み/紐付け済み・抽出時の検査記録があるもの）===")
        print(f"検査済み: {tx['checked']} 件 / 抽出時に強い疑義: {len(tx['flagged'])} 件 / 弱い疑義のみ: {tx['weak_only']} 件")
        print(f"  強い疑義が付き、承認時に本文を手直しした（検出が役立った）: {len(tx['flagged_fixed'])} 件")
        for r in tx["flagged_fixed"]:
            print(f"    - id={r['id']} {r['name']}: {', '.join(map(str, r['examples']))}")
        print(f"  強い疑義が付いたが、本文を手直しせず承認した（誤検知、または人の見落とし）: {len(tx['flagged_unfixed'])} 件")
        for r in tx["flagged_unfixed"]:
            print(f"    - id={r['id']} {r['name']}: {', '.join(map(str, r['examples']))}")
        print(f"  承認時点でも n-gram/言い回し/書式の強い疑義が残り、本文を直さず承認した: {len(tx['approval_flagged_unedited'])} 件")
        print("  ※「直さず承認」が誤検知かは目視で判定が必要。多ければ tools/text_check_calibrate.py で閾値を見直す。")


def main() -> int:
    _load_env_fallback()
    if not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY")):
        print("SUPABASE_URL / SUPABASE_KEY が未設定です")
        return 1
    from supabase import create_client
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    print_report(summarize(_fetch_cards(sb)))
    return 0


if __name__ == "__main__":
    # 日本語出力のため stdout を UTF-8 化（import 時には触らない: pytest の出力捕捉を壊すため）
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
