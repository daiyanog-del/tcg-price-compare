"""
check_collection.py — 毎日の価格収集が抜けていないかを見張る（2026-10-21）
=====================================================================
Render の定期実行（tcg-collect-prices・tcg-collect-buyback）が起動しなかった日・ほとんど取れなかった日に気づくため、
Render とは別の GitHub Actions（.github/workflows/check-collection.yml・毎日 JST 13:00）から、
今日の price_history / buyback_history の行数を数えて Discord へ知らせる。

経緯: 2026-09-17〜21 の5日間、tcg-collect-buyback が起動せず（ログに起動の痕跡なし・エラーなし）、
buyback_history が欠けて買取の値動きランキングが 0 件になった。気づくまで5日かかった。

判定（evaluate）:
  - 今日の行数が 0 → 「無い」。collection_runs に今日の実行記録があるかで「起動しなかった」「動いたが0件」を分ける
  - 直前14日の中央値（0件の日は除く）の LOW_RATIO 未満 → 「少ない」
  - それ以外 → 問題なし
知らせたら終了コード 1 で終わる（GitHub の失敗のメールでも気づけるように）。読むだけで、DB には書かない。
"""

from __future__ import annotations

import os
import statistics
import sys
from datetime import date, datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

# 直前の何日と比べるか・どこまで少なければ知らせるか
HISTORY_DAYS = 14
# 2026-09-06〜10-21 の実測: 普段の日は中央値の 0.58 倍以上（買取 10-11・10-13 が最小）、
# ほとんど取れなかった 10-11 の販売は 0.28 倍。その間を取った仮置き。TODO: calibrate from data
LOW_RATIO = 0.5

TARGETS = [
    # (表示名, テーブル, collection_runs.run_type, Render の定期実行の名前)
    ("販売", "price_history", "prices", "tcg-collect-prices"),
    ("買取", "buyback_history", "buyback", "tcg-collect-buyback"),
]


def evaluate(label: str, today_rows: int, history: list[int], runs_today: list[dict], cron_name: str) -> str | None:
    """問題があれば知らせる文を、無ければ None を返す。

    history: 直前 HISTORY_DAYS 日の行数（0 の日を含んでよい。中央値は 0 を除いて取る）
    runs_today: 今日の collection_runs の行（started_at・finished_at・skipped_shops を持つ dict）
    """
    if today_rows == 0:
        if not runs_today:
            return (f"⚠ {label}の価格収集が今日（JST）まだ1件もありません。実行の記録（collection_runs）も無いので、"
                    f"Render の定期実行 `{cron_name}` が起動していない可能性があります。Render のダッシュボードで実行履歴を確認してください。")
        unfinished = [r for r in runs_today if not r.get("finished_at")]
        state = "実行中のまま終わっていない" if unfinished else "実行は終わったが0件"
        return (f"⚠ {label}の価格収集が今日（JST）0件です（{state}・実行 {len(runs_today)} 回）。"
                f"{_skipped_text(runs_today)}`{cron_name}` のログを確認してください。")
    usual = [n for n in history if n > 0]
    if not usual:
        return None  # 比べる日が無い（始めたばかりなど）
    median = statistics.median(usual)
    if today_rows < median * LOW_RATIO:
        return (f"⚠ {label}の価格収集が今日（JST）{today_rows:,}行で、普段（直前{HISTORY_DAYS}日の中央値 {int(median):,}行）の"
                f"{today_rows / median:.0%} しかありません。{_skipped_text(runs_today)}店のブロックや取得失敗が無いか確認してください。")
    return None


def _skipped_text(runs_today: list[dict]) -> str:
    skipped = {}
    for r in runs_today:
        s = r.get("skipped_shops") or {}
        if isinstance(s, dict):
            skipped.update(s)
    if not skipped:
        return ""
    return "飛ばした店: " + "、".join(f"{shop}（{why}）" for shop, why in skipped.items()) + "。"


def _count(client, table: str, day: date) -> int:
    resp = client.table(table).select("recorded_at", count="exact").eq("recorded_at", day.isoformat()).limit(1).execute()
    if not isinstance(resp.count, int):
        raise RuntimeError(f"{table} {day} の件数を読めませんでした")
    return resp.count


def main() -> int:
    from supabase import create_client  # GitHub Actions でだけ入れる（テストでは読まない）
    from discord_notify import send_discord_message

    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
    if not url or not key:
        print("SUPABASE_URL / SUPABASE_KEY がありません")
        return 2
    client = create_client(url, key)
    today = datetime.now(JST).date()
    start_utc = datetime(today.year, today.month, today.day, tzinfo=JST).astimezone(timezone.utc)

    alerts, lines = [], []
    for label, table, run_type, cron_name in TARGETS:
        today_rows = _count(client, table, today)
        history = [_count(client, table, today - timedelta(days=i)) for i in range(1, HISTORY_DAYS + 1)]
        runs = (client.table("collection_runs").select("started_at,finished_at,skipped_shops")
                .eq("run_type", run_type).gte("started_at", start_utc.isoformat()).execute().data) or []
        msg = evaluate(label, today_rows, history, runs, cron_name)
        lines.append(f"{label}: 今日 {today_rows:,}行・直前{HISTORY_DAYS}日 {history}・実行 {len(runs)} 回 → {'要確認' if msg else 'OK'}")
        if msg:
            alerts.append(msg)

    print("\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"## 価格収集の見張り（{today}）\n\n" + "\n".join(f"- {l}" for l in lines + alerts) + "\n")
    if alerts:
        send_discord_message(f"【価格収集の見張り {today}】\n" + "\n".join(alerts))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
