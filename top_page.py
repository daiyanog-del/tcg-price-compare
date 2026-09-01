"""
top_page.py — 最初の画面（販売価格タブの初期状態）向けランキングの集計ロジック

app.py の /api/top-decks・/api/top-movers から呼ばれる純粋関数を集約する
（Supabase・ネットワーク呼び出しを含まない。テストしやすくするため）。

背景（2026-08-31）:
  最初の画面は「カード名を入力して検索」の1行のみで空だった。既存の /api/movers は
  7月導入の「複数店同方向ガード」により構造的にほぼ毎日0件になることが判明したため、
  この画面には別の素材（環境デッキの合計金額・共通店舗ガードのみの価格推移）を使う。
"""

from __future__ import annotations

from aggregations import pick_representative_rarity


# ── デッキランキング（N-1） ──

# 表示するデッキ数の上限。デッキ1件ごとにTCG PORTALのスクレイピングが発生するため
# （meta_scraper.fetch_deck_cards）、全デッキを毎回叩かないための上限。
# TODO: calibrate from data（表示件数の妥当性・体感速度は未検証）
TOP_DECKS_LIMIT = 8

# app.py 側キャッシュのTTL。meta_scraper側キャッシュ（Tier表3h・デッキ6h）と
# 相場キャッシュ（_ESTIMATE_CACHE_SEC=600秒）の間を仮置き。
# TODO: calibrate from data
TOP_DECKS_CACHE_SEC = 1800


def summarize_deck(card_entries: list[dict], price_lookup: dict) -> dict:
    """デッキの主要カードリストと相場（_estimate_cache 相当の name -> {"price":...}）から、
    「いま組むといくら」の合計と、価格が取れた/取れなかった枚数を計算する。

    card_entries: [{"name": str, "qty": int}, ...]（app._parse_deck_entries の出力形式）
    price_lookup: {カード名: {"price": int, ...}}

    合計金額は取れた分だけの金額であり、正確な総額ではない
    （missing_count が1枚でもあれば「一部未取得」として扱うこと）。
    """
    total = 0
    priced_count = 0
    missing_count = 0
    for entry in card_entries:
        name = entry.get("name", "")
        qty = entry.get("qty", 1)
        best = price_lookup.get(name)
        if best and best.get("price"):
            total += best["price"] * qty
            priced_count += 1
        else:
            missing_count += 1
    return {"total": total, "priced_count": priced_count, "missing_count": missing_count}


def rank_decks(decks: list[dict], sort: str = "price") -> list[dict]:
    """デッキランキングの並び替え。

    sort="price"（既定・安い順）: total 昇順。ただし total は「取れた分だけの
        合計」であり、未取得（missing_count>0）が多いデッキほど不当に安く
        見えてしまう（2026-08-31 reviewer指摘 Q-6）。そのため:
          1. 全カード取得済み（missing_count==0 かつ total>0）を優先し、total 昇順
          2. 一部未取得（missing_count>0 かつ total>0）はそれより後方、total 昇順
          3. 価格情報なし（total<=0）は必ず末尾
    sort="tier"（強い順）: tier 昇順（Tier未設定=0は最下位扱い）、同Tier内は share 降順
    """
    if sort == "tier":
        return sorted(
            decks,
            key=lambda d: ((d.get("tier") or 99), -(d.get("share") or 0)),
        )

    def _price_sort_key(d: dict):
        total = d.get("total", 0)
        missing = d.get("missing_count", 0)
        if total <= 0:
            reliability = 2  # 価格情報なし・必ず末尾
        elif missing > 0:
            reliability = 1  # 一部未取得・下限にすぎないため後方へ
        else:
            reliability = 0  # 全カード取得済み・最も信頼できる
        return (reliability, total)

    return sorted(decks, key=_price_sort_key)


# ── 価格推移ランキング（N-2） ──

# 対象を「最安 ¥1,000 以上」に絞る（ユーザー決定、2026-08-31）。
# 安価なカードは僅かな価格変動でも変動率(%)が跳ねやすく、ランキングの上位を
# 埋め尽くしてしまうため。
# TODO: calibrate from data（この閾値そのものの妥当性は未校正の仮置き）
MOVERS_MIN_PRICE = 1000

# app.py 側キャッシュのTTL。既存の買取movers（_BUYBACK_MOVERS_CACHE_SEC）に倣う。
TOP_MOVERS_CACHE_SEC = 3600

# 10円以下は異常値として除外する（price_history 全体で使われている既存の慣例。
# supabase_rpc_movers.sql の shop_daily と同じ基準）。
_MIN_VALID_PRICE = 10

# 定着（在庫入替の一時的な変動を除外）判定に使う日数。「当日」の何日前を
# 「前日」として比較するか。司令塔の本番実測（2026-08-31）: これにより
# 18件→15件（3件除外）で、除外された3件が在庫入替の疑いが強いものだった。
# TODO: calibrate from data（1日で十分か、収集間隔が空いた日の扱い等は未校正）
MOVERS_STABILITY_DAYS = 1

# RPC(get_top_movers)に渡す p_limit。RPCは「up/down合算・変化率絶対値降順」で
# 上位N件を返し、app.py側でpctの符号からup/downへ振り分けるため、up/down片方に
# 偏った相場でも両方向とも API側の上限（_parse_limit_param(10, 20)の最大20件）を
# 満たせるよう、その最大値の数倍を要求しておく。
# TODO: calibrate from data（×4は仮置き。実際の up/down 偏りは未計測）
TOP_MOVERS_RPC_LIMIT = 20 * 4


def _group_by_card(rows: list[dict]) -> dict:
    """price_history 行リストを card_name ごとにグループ化する"""
    grouped: dict = {}
    for row in rows:
        name = row.get("card_name") or ""
        if not name:
            continue
        grouped.setdefault(name, []).append(row)
    return grouped


def _shop_min(rows: list[dict]) -> dict:
    """1カード・1レアリティ分の行リストから shop -> その日の最安値 を求める"""
    result: dict = {}
    for row in rows:
        shop = row.get("shop") or ""
        price = row.get("min_price")
        if not shop or price is None or price <= _MIN_VALID_PRICE:
            continue
        if shop not in result or price < result[shop]:
            result[shop] = price
    return result


def _price_by_rarity(rows: list[dict]) -> dict:
    """1カード分の行リスト（1日分）から rarity -> その日の最安値 を求める
    （代表レアリティ選定の入力。店舗を跨いだ最安値であることに注意）"""
    result: dict = {}
    for row in rows:
        rarity = row.get("rarity") or ""
        price = row.get("min_price")
        if price is None or price <= _MIN_VALID_PRICE:
            continue
        if rarity not in result or price < result[rarity]:
            result[rarity] = price
    return result


def _compute_card_movement(name: str, rarity: str, rows_new: list[dict], rows_old: list[dict],
                            rows_prev: list[dict] | None, min_price: int,
                            stability_checked: bool) -> dict | None:
    """代表レアリティに絞った1カード分の行から、共通店舗ガード・閾値・定着チェックを
    適用して1件のエントリ（または対象外なら None）を返す。"""
    new_shop = _shop_min(rows_new)
    old_shop = _shop_min(rows_old)

    # ガード①: 当日・7日前の両方に記録がある店舗（共通店舗）だけを対象にする
    common_shops = [s for s in new_shop if s in old_shop]
    if not common_shops:
        return None

    price_new = min(new_shop[s] for s in common_shops)
    price_old = min(old_shop[s] for s in common_shops)
    if price_new < min_price or price_old < min_price or price_new == price_old:
        return None

    if stability_checked:
        # Q-7修正: 「前日データがある共通店舗」の集合Sを先に確定し、
        # 当日側のminもSに限定して比較する（片側だけ店舗集合を絞ると
        # 偽陽性/偽陰性の両方が起きるため、両側とも同じ店舗集合で比較する）
        prev_shop = _shop_min(rows_prev or [])
        stable_shops = [s for s in common_shops if s in prev_shop]
        if not stable_shops:
            return None  # 前日データがこのカードの共通店舗に無い→検証不能
        price_new_stable = min(new_shop[s] for s in stable_shops)
        price_prev_stable = min(prev_shop[s] for s in stable_shops)
        if price_new_stable != price_prev_stable:
            return None  # 前日と一致しない（在庫入替の疑い）

    diff = price_new - price_old
    pct = round(diff / price_old * 100, 1)
    return {"name": name, "rarity": rarity, "today": price_new, "yesterday": price_old,
            "diff": diff, "pct": pct}


def aggregate_common_shop_movers(rows_new: list[dict], rows_old: list[dict],
                                  rows_prev: list[dict] | None = None,
                                  min_price: int = MOVERS_MIN_PRICE) -> dict:
    """価格推移ランキング（代表レアリティ固定＋共通店舗ガード＋定着チェック）を計算する。

    注意（2026-09-01）: 本番経路（/api/top-movers）はDB側RPC（get_top_movers）に
    切り替わっており、この関数はもう本番からは呼ばれない。ただしこの関数と
    そのテスト（tests/test_top_page.py）は「集計の規約」の実行可能な仕様書として
    残す。SQL側の意味とここのPython実装が食い違えばテストで気づける。

    rows_new / rows_old: price_history の行リスト（当日 / 7日前）。
        各行は {"card_name": str, "shop": str, "rarity": str, "min_price": int} を
        持てばよい（余分なキーがあっても無視する）。
    rows_prev: price_history の行リスト（前日 = 当日 - MOVERS_STABILITY_DAYS日）。
        None または空リストなら「前日データが取れない」とみなし、定着チェックを
        スキップして通す（欠測でランキングが空になる方が害が大きいため）。
    min_price: カード単位の最安値の下限。**当日・7日前の両方**に適用する
        （2026-08-31 P-1修正: 従来は当日にしか掛けておらず、7日前は安かった
        カードの一時的な倍増が1位に出る不具合があった。例: 次元融合 ¥540→¥1,080）。

    代表レアリティの固定（2026-08-31 Q-1修正・reviewer指摘）:
      本番実測で、同じ(カード×店)に複数レアリティの行がある組が64%と多数派であり、
      レアリティを跨いで最安を取ると「安いレアリティの在庫切れ→高いレアリティに
      繰り上がる」だけの見かけの値上がりが混入する（実例: 滅びの黒魔術師の
      偽の+33.6%）。このプロジェクトの既存3経路（aggregations.daily_min_by_lowest_rarity・
      aggregations._common_shop_change_for_card・supabase_rpc_movers.sqlのガード③）は
      いずれもレアリティを揃えており、ここだけレアリティ盲目だったのを揃える。
      代表レアリティは「当日(rows_new)に存在するレアリティのうち最安のもの」
      （"(不明)"・空文字は他に候補がある限り除外。aggregations.pick_representative_rarity
      に集約、ガード③相当）。以降の共通店舗ガード・閾値・定着チェックは、
      カードごとに選んだこの代表レアリティの行だけに絞って行う。

    ガード①（共通店舗）: 当日と7日前の両方に記録がある店舗（共通店舗）だけを使い、
    カードごとに当日の最安値・7日前の最安値を求める（代表レアリティに絞った上で）。
    7月導入の「複数店同方向ガード」（get_price_movers のガード②）はここでは
    適用しない。ガード②が本番でほぼ毎日0件を生んでいたことが今回の切り替えの
    理由そのものであり、この画面ではガード①＋定着チェックで誤検知対策とする。

    定着チェック（2026-08-31 P-2追加、Q-7で店舗集合の非対称を修正）: 在庫入替
    （最安の1枚が売れて次点の価格に繰り上がる）は一時的な変動であり、複数店同方向
    ガードの代替として「当日の価格が前日も同じだったか」で見分ける。判定には、
    「共通店舗のうち前日データもある店舗」の集合Sを先に確定し、当日側の価格も
    このSに限定して比較する（当日側だけ全共通店舗のminを使うと、店舗ごとの
    値上げ・値下げを見逃す/誤検知する非対称が生じるため）。

    変化が無かったカード（当日=7日前）は結果に含めない。

    戻り値: {"up": [...], "down": [...], "stability_checked": bool}（各要素は
        {"name","rarity","today","yesterday","diff","pct"}、abs(pct) 降順。
        stability_checked は定着チェックを実際に適用したかどうか）
    """
    stability_checked = bool(rows_prev)
    by_card_new = _group_by_card(rows_new)
    by_card_old = _group_by_card(rows_old)
    by_card_prev = _group_by_card(rows_prev) if rows_prev else {}

    up: list[dict] = []
    down: list[dict] = []
    for name, card_rows_new in by_card_new.items():
        rarity = pick_representative_rarity(_price_by_rarity(card_rows_new))
        if rarity is None:
            continue

        rows_new_r = [r for r in card_rows_new if (r.get("rarity") or "") == rarity]
        rows_old_r = [r for r in by_card_old.get(name, []) if (r.get("rarity") or "") == rarity]
        rows_prev_r = [r for r in by_card_prev.get(name, []) if (r.get("rarity") or "") == rarity]

        entry = _compute_card_movement(name, rarity, rows_new_r, rows_old_r, rows_prev_r,
                                        min_price, stability_checked)
        if entry is None:
            continue
        (up if entry["diff"] > 0 else down).append(entry)

    up.sort(key=lambda e: abs(e["pct"]), reverse=True)
    down.sort(key=lambda e: abs(e["pct"]), reverse=True)
    return {"up": up, "down": down, "stability_checked": stability_checked}
