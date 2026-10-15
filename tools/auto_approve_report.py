"""
auto_approve_report.py — 自動承認判定（シャドーモード）の集計

extraction_raw.auto_approve_verdict が付いた承認済み/紐付け済み/却下のカードを集計し、
判定の閾値校正に必要な数字を出す。

見るところ:
  - 候補(eligible)なのに承認時に手直しされた（approval_snapshot.edited_fields が空でない）
  - 候補なのに却下された
  - 候補でないのに手直しなしで承認された（条件が厳しすぎる可能性。理由コード別）

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
    }


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
