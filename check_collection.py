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

店ごとの見張り（evaluate_shops・2026-10-30 追加）:
  合計の行数だけでは、1店が丸ごと抜けても他の店で埋まって気づけなかった（10月に買取のカードラッシュが9日、
  開始時の確認で当日スキップ・10-29 のトレコロCB は半分で打ち切り）。collection_runs の shop_stats（店ごとの
  取得成功 ok・0件 empty・エラー error・ブレーカーで飛ばした skipped の数）と skipped_shops を使う。
  ok の数は注目カードの数とともに日々動くので、店ごとは「取れた割合」と「エラーの割合」で見る。
  - 休止中（suspended）以外の理由で当日スキップした店
  - エラーの割合が ERROR_RATIO 以上、またはブレーカーで飛ばしたカードがある店
  - 取れた割合が、その店の直前の中央値の OK_RATE_RATIO 未満の店（10-13・14 のトレコロは WAF で全件0件）
  - 集めに行ったカードの数が直前の中央値の ATTEMPTED_RATIO 未満（10-27 の約4割減＝環境デッキが空で注目カードが減った）
見慣れないレアリティ（find_unknown_rarities）:
  rarity.py の正規名と KNOWN_ODD_RARITIES に無い表記が今日の記録にあれば知らせる（10-30 にトレコロの
  「ステンレス製」が入っていた）。店の新しい表記を正規名に寄せたら rarity.py に、別のレアリティとして
  残すと決めたら KNOWN_ODD_RARITIES に足す。
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

# 店ごとの見張りの線。2026-10-07〜30 の collection_runs の実測から決めた（docs/decisions.md 10-30）。
# エラー: 普段は 0〜0.5%（カードラッシュの販売で最大 20/3800）。問題の日は 4.5%（10-12 トレコロ販売）〜56%。
ERROR_RATIO = 0.03
# 取れた割合: 普段は店ごとにほぼ一定（まんぞく屋の販売は約55%・カードラボの買取は約40%）。問題の日は 0%・約46%。
OK_RATE_RATIO = 0.5
# 集めに行ったカードの数: 普段の揺れは中央値の 0.78 倍まで（買取 10-20）。10-27 の買取は 0.61 倍。
ATTEMPTED_RATIO = 0.7
# 比べる直前の日に、その店の記録が何日あれば比べるか
MIN_SHOP_HISTORY = 3
# 見慣れないレアリティを知らせる文に並べる数（Discord は1通1900字で切れる）
MAX_RARITIES_IN_MESSAGE = 10

# collection_runs.skipped_shops の理由（collect_prices.py / collect_buyback.py が書く）
SKIP_REASONS = {
    "health_check_failed": "開始時の確認で店のサイトが応答しなかった",
    "api_key_missing": "API キーが未設定",
}

# rarity.py の正規名ではないが、記録に残っている既知の表記（2026-10-30 の price_history の rarity_dim と
# buyback_history から）。MANGA 系・GREEN/PINK・SPECIAL 系は絵が違うので、意図して別のレアリティとして残している。
# 旧い略号（KC-N・M-UR など）は過去の記録の rarity_dim に残っているもの。rarity._SEPARATE_RARITIES（WARN を止める別扱いの表記）は
# ここにも入れる（tests/test_check_collection.py で一致を確かめる）。「ステンレス製」は入れない（紙のカードではない）
KNOWN_ODD_RARITIES = frozenset({
    "", "(不明)", "-", "BSE", "ES", "KC-N", "KC-R", "KC-UR", "KCウルトラレア", "M-GR", "M-SE", "M-SR", "M-UR",
    "MANGAアル", "MANGAウル", "MANGAシク", "N-P", "NR", "Oラッシュ", "P-EXSE", "PN", "QCシク", "RE", "SR-P", "UR-P",
    "WPP7", "イベント品", "ウルトラSPECIALILLUSTVer.", "ウルトラSPECIALPURPLEVer.", "ウルトラシークレット",
    "エクストラシークレットパラレルレア", "オバフレウル", "オバフレシク",
    "オバフレプリシク", "クォーターセンチュリーシークレット(SPECIAL PINK ver.)",
    "クォーターセンチュリーシークレットGREEN Ver.", "ゴル", "シークレットBLUEVer.", "シークレットSPECIAL RED Ver.",
    "シークレットSPECIALREDVer.", "ノーマル(パラレル仕様)", "ファラオズシークレット", "ミレニアムウルトラレア", "ミレニアムゴールドレア",
    "ミレニアムシークレットレア", "ミレニアムスーパーレア", "レアパラ",
})

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


def _shop_numbers(stats: dict) -> dict[str, dict]:
    """shop_stats から店ごとの数を取り出す（先頭が _ の鍵は打ち切りの記録など店ではない）。"""
    out = {}
    for shop, v in (stats.items() if isinstance(stats, dict) else []):
        if shop.startswith("_") or not isinstance(v, dict):
            continue
        ok, empty, error = (int(v.get(k) or 0) for k in ("ok", "empty", "error"))
        out[shop] = {"ok": ok, "error": error, "skipped": int(v.get("skipped") or 0),
                     "attempted": ok + empty + error}
    return out


def evaluate_shops(label: str, today_run: dict | None, history_runs: list[dict]) -> list[str]:
    """店ごとの問題を知らせる文のリストを返す（無ければ空）。

    today_run: 今日の collection_runs の行（shop_stats・skipped_shops）。無ければ見ない（evaluate が知らせる）
    history_runs: 直前の日の行（1日1行）
    """
    if not today_run:
        return []
    alerts = []
    skipped = today_run.get("skipped_shops")
    for shop, why in (skipped.items() if isinstance(skipped, dict) else []):
        if why != "suspended":
            alerts.append(f"⚠ {label}: {shop} を今日まるごと取っていません（{SKIP_REASONS.get(why, why)}）。")
    aborted = (today_run.get("shop_stats") or {}).get("_aborted") if isinstance(today_run.get("shop_stats"), dict) else None
    if isinstance(aborted, dict):
        alerts.append(f"⚠ {label}: 今日の収集は途中で打ち切られています（処理 {aborted.get('processed', '?')}/"
                      f"{aborted.get('total', '?')}枚）。下の店ごとの数は打ち切りまでの分です。")

    today = _shop_numbers(today_run.get("shop_stats"))
    past = [_shop_numbers(r.get("shop_stats")) for r in history_runs]
    for shop, t in today.items():
        if t["attempted"] == 0:
            continue
        err = t["error"] / t["attempted"]
        if err >= ERROR_RATIO or t["skipped"] > 0:
            extra = f"・連続失敗のため店への問い合わせを {t['skipped']:,}回止めました" if t["skipped"] else ""
            alerts.append(f"⚠ {label}: {shop} のエラーが {t['error']:,}/{t['attempted']:,}件（{err:.0%}）{extra}。"
                          "店のサイトの遅れ・ブロックが無いか確認してください。")
        rates = [p[shop]["ok"] / p[shop]["attempted"] for p in past if p.get(shop, {}).get("attempted")]
        if len(rates) >= MIN_SHOP_HISTORY:
            usual = statistics.median(rates)
            rate = t["ok"] / t["attempted"]
            if usual > 0 and rate < usual * OK_RATE_RATIO:
                alerts.append(f"⚠ {label}: {shop} で取れたカードが {rate:.0%}（普段 {usual:.0%}）しかありません。"
                              "店のサイトの作りが変わったか、ブロックされていないか確認してください。")

    attempted_today = max((t["attempted"] for t in today.values()), default=0)
    attempted_past = [m for m in (max((t["attempted"] for t in p.values()), default=0) for p in past) if m > 0]
    # 打ち切った日は集めに行った数が減って当然なので比べない（打ち切りは上で知らせている）
    if attempted_today and not isinstance(aborted, dict) and len(attempted_past) >= MIN_SHOP_HISTORY:
        usual = statistics.median(attempted_past)
        if attempted_today < usual * ATTEMPTED_RATIO:
            alerts.append(f"⚠ {label}: 集めに行ったカードが {attempted_today:,}枚で、普段（{int(usual):,}枚）の"
                          f"{attempted_today / usual:.0%} です。注目カード（環境デッキ・新弾・規制）の取り込みが"
                          "空振りしていないか確認してください。")
    return alerts


def find_unknown_rarities(rows: list[dict], canonical: set[str]) -> dict[str, set[str]]:
    """今日の行（shop・rarity）から、正規名でも既知の表記でもないレアリティを {表記: 店の集合} で返す。"""
    found: dict[str, set[str]] = {}
    for r in rows:
        rarity = r.get("rarity") or ""
        if rarity in canonical or rarity in KNOWN_ODD_RARITIES:
            continue
        found.setdefault(rarity, set()).add(r.get("shop") or "?")
    return found


def unknown_rarity_text(label: str, found: dict[str, set[str]]) -> str | None:
    if not found:
        return None
    listed = sorted(found.items())[:MAX_RARITIES_IN_MESSAGE]
    items = "、".join(f"「{r}」（{'・'.join(sorted(shops))}）" for r, shops in listed)
    if len(found) > len(listed):
        items += f" ほか {len(found) - len(listed)}件"
    return (f"⚠ {label}: 見慣れないレアリティの表記があります: {items}。正規名に寄せるなら rarity.py、"
            "別のレアリティとして残すなら check_collection.py の KNOWN_ODD_RARITIES に足してください。"
            "紙のカードでない品なら取得の除外に足してください。")


def _run_day(r: dict) -> date:
    """started_at（DB の timestamptz の文字列）の JST の日付。小数秒の桁数は DB が詰めて返すことがあり
    （例 .11532）、Python 3.10 の fromisoformat は6桁でないと読めないので秒までで切る。"""
    text = str(r["started_at"]).replace("Z", "+00:00")
    head, _, rest = text.partition(".")
    if rest:
        offset = rest[next((i for i, ch in enumerate(rest) if ch in "+-"), len(rest)):]
        text = head + offset
    return datetime.fromisoformat(text).astimezone(JST).date()


def daily_runs(rows: list[dict]) -> dict[date, dict]:
    """collection_runs の行を JST の日ごとに1行にまとめる。終わった実行（finished_at あり）の最後を選び、
    その日に終わった実行が無ければ最後に始まった行を選ぶ（途中で打ち切った実行や手で回し直した実行が混ざるため）。"""
    by_day: dict[date, dict] = {}
    for r in sorted((r for r in rows if r.get("started_at")),
                    key=lambda r: (bool(r.get("finished_at")), str(r.get("started_at")))):
        by_day[_run_day(r)] = r
    return by_day


def _today_rarity_rows(client, table: str, today: date, canonical: set[str]) -> list[dict]:
    """今日の行の店とレアリティ。販売は1日約4万行あるので、レアリティの表（rarity_dim）から
    見慣れない表記の番号だけを引き、その番号の行だけ読む。買取（1日約7千行）は全部読む。"""
    if table == "price_history":
        dim = client.table("rarity_dim").select("rarity_id,rarity").order("rarity_id").limit(1000).execute().data or []
        if len(dim) >= 1000:
            raise RuntimeError("rarity_dim が1000行を超えたので、ページごとに読む形に直してください")
        ids = [d["rarity_id"] for d in dim
               if (d.get("rarity") or "") not in canonical and (d.get("rarity") or "") not in KNOWN_ODD_RARITIES]
        if not ids:
            return []
        return (client.table(table).select("shop,rarity").eq("recorded_at", today.isoformat())
                .in_("rarity_id", ids).limit(1000).execute().data) or []
    rows, start = [], 0
    while True:
        page = (client.table(table).select("shop,rarity").eq("recorded_at", today.isoformat())
                .order("id").range(start, start + 999).execute().data) or []
        rows += page
        if len(page) < 1000:
            return rows
        start += 1000


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

    import rarity as rarity_mod
    canonical = {e["canonical"] for e in rarity_mod.RARITIES}
    history_start_utc = start_utc - timedelta(days=HISTORY_DAYS)

    alerts, lines = [], []
    for label, table, run_type, cron_name in TARGETS:
        today_rows = _count(client, table, today)
        history = [_count(client, table, today - timedelta(days=i)) for i in range(1, HISTORY_DAYS + 1)]
        recent = (client.table("collection_runs").select("started_at,finished_at,skipped_shops,shop_stats")
                  .eq("run_type", run_type).gte("started_at", history_start_utc.isoformat()).execute().data) or []
        runs = [r for r in recent if r.get("started_at") and _run_day(r) == today]
        msg = evaluate(label, today_rows, history, runs, cron_name)
        # 店ごと・レアリティの見張りが落ちても、上の行数の知らせは必ず送る
        try:
            daily = daily_runs(recent)
            shop_alerts = evaluate_shops(label, daily.get(today), [r for d, r in daily.items() if d < today])
        except Exception as e:  # noqa: BLE001
            shop_alerts = [f"⚠ {label}: 店ごとの見張りを読めませんでした（{type(e).__name__}: {e}）。"]
        try:
            found = find_unknown_rarities(_today_rarity_rows(client, table, today, canonical), canonical) if today_rows else {}
            rarity_msg = unknown_rarity_text(label, found)
        except Exception as e:  # noqa: BLE001
            found = {}
            rarity_msg = f"⚠ {label}: 見慣れないレアリティの確認を読めませんでした（{type(e).__name__}: {e}）。"
        problems = ([msg] if msg else []) + shop_alerts + ([rarity_msg] if rarity_msg else [])
        lines.append(f"{label}: 今日 {today_rows:,}行・直前{HISTORY_DAYS}日 {history}・実行 {len(runs)} 回・"
                     f"店ごと {len(shop_alerts)}件・見慣れない表記 {len(found)}件 → {'要確認' if problems else 'OK'}")
        alerts += problems

    print("\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"## 価格収集の見張り（{today}）\n\n" + "\n".join(f"- {l}" for l in lines + alerts) + "\n")
    if alerts:
        # 店の表記（レアリティ）を載せるので、本文に @ があってもメンションを飛ばさない
        send_discord_message(f"【価格収集の見張り {today}】\n" + "\n".join(alerts), no_mentions=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
