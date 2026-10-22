-- ⚠ 2026-09-01: 値動きの定義がこのプロジェクトには**2つ**ある。
--   ・この get_price_movers … X投稿（x_poster.py の post_big_movers / post_featured_movers）が使う
--   ・supabase_rpc_top_movers.sql の get_top_movers … サイトのトップに出すランキングが使う
--   両者はパラメータ違いではなく**設計が別物**（統合しなかった理由は docs/decisions.md 2026-09-01）:
--     比較する2日   : こちら=窓の中の直近2日（可変） / 向こう=N日前と当日（固定）
--     レアリティ     : こちら=レアリティ別（per_card で制御） / 向こう=代表1つに畳む
--     異常値         : こちら=¥10以下を除外 / 向こう=除外なし
--     偽陽性ガード   : こちら=共通店舗＋**複数店同方向** / 向こう=共通店舗＋**定着チェック**
--   13日リプレイの実測偽陽性率: 複数店同方向 11.6% / 定着 15.3% / 両方 11.9%（どれも実用範囲）
--   → 「サイトに出ているのにXが投稿しない」のは仕様。どちらかが壊れているわけではない。
--
-- 値動きランキング用 RPC（販売価格版）
-- Supabase の SQL Editor で実行してください
--
-- 引数:
--   cutoff_date text  比較対象の開始日（YYYY-MM-DD形式）例: '2026-06-02'
--   min_diff    int   最小変動額のフィルタ（デフォルト50円）
--   top_n       int   上位何件を返すか（デフォルト20件）
--   per_card    int   同一カード名・同方向で返す最大レアリティ数（デフォルト1件=
--                      現行Web表示どおり代表レアリティのみ。X投稿等で全レアリティを
--                      候補として受け取りたい場合は増やす。2026-07-10追加）
--
-- 戻り値 (行ごと):
--   card_name     text   カード名
--   rarity        text   レアリティ
--   today_price   int    直近日の最安値
--   prev_price    int    前日の最安値
--   diff          int    価格差 (today - prev)
--   pct           numeric 変動率 (%)
--   date_new      text   直近日 (YYYY-MM-DD)
--   date_old      text   前日   (YYYY-MM-DD)
--   direction     text   'up' | 'down'
--
-- 偽陽性対策のガード（2026-07-10導入。店舗粒度で集計することで表記ゆれ系列分裂や
-- 単一店舗の在庫入れ替わりによる見かけ上の急騰・急落を除外する）:
--   ガード①（共通店舗）: 前日比の計算を、比較する2日の両方に記録がある店舗
--     （共通店舗）だけで行う。片方の日にしか行が無い店舗は集計から除外する
--   ガード②（複数店確認・販売のみ）: 集計値の変動方向と同方向に動いた共通店舗が
--     2店以上ある系列のみ採用する。単一店舗のみの変動は偽陽性の疑いが強いため除外
--
-- 引数構成が異なる関数はPostgreSQLがオーバーロードとして別関数扱いで共存させてしまい、
-- PostgRESTのRPC呼び出しが「関数が一意に定まらない」エラーになる。per_card追加前の
-- 旧シグネチャ (text, int, int) を明示的にDROPしてから再作成する。
DROP FUNCTION IF EXISTS get_price_movers(text, int, int);

CREATE OR REPLACE FUNCTION get_price_movers(
    cutoff_date text DEFAULT (CURRENT_DATE - INTERVAL '2 days')::text,
    min_diff    int  DEFAULT 50,
    top_n       int  DEFAULT 20,
    per_card    int  DEFAULT 1
)
RETURNS TABLE (
    card_name   text,
    rarity      text,
    today_price int,
    prev_price  int,
    diff        int,
    pct         numeric,
    date_new    text,
    date_old    text,
    direction   text
)
LANGUAGE sql
STABLE
AS $$
-- 【2026-10-22 読み方の書き換え（結果は同じ）】
-- 旧版は窓（cutoff_date 以降。呼び出し側は今日-3日＝4日分・約13.5万行）の全行をビュー
-- price_history 経由で名前つきにしてから日別に集計し、そのうち直近2日だけを使っていた。
-- 本番 EXPLAIN ANALYZE（10-22）で 10.4秒（索引読み 4.6秒＋集計がメモリに収まらずディスクへ
-- 3秒＋外部並べ替え）。起動直後に statement timeout（57014・service_role は8秒）で落ちた。
-- 新版: (1) 比較する2日を索引 (recorded_at, …) の後ろ向き読みで先に決め、
-- (2) その2日だけを日付の等号で読み（各日 約2〜4万行）、(3) カード番号・店番号・
-- レアリティ番号のまま突き合わせて集計し、(4) 名前は残った数十行にだけ付ける。
-- 同じ時刻の実測で 10.4秒 → 1.15秒（可視性マップが空の状態）。
-- 2日の決め方は旧版と同じ条件（¥10以下と正体不明レアリティを除いた行がある日）。
-- price_history_v2 は (card_id, shop_id, rarity_id, recorded_at) が主キーなので、旧版の
-- 「店舗×カード×レアリティ×日付の MIN(min_price)」は1行そのものと同じ。名前は card_dim・
-- shop_dim・rarity_dim でそれぞれ一意（UNIQUE 制約）なので、番号で束ねても名前で束ねても同じ。
-- MATERIALIZED は、日付が実行時に決まるため計画時に件数が読めず、主キーで1系列ずつ全期間を
-- 引く計画（実測 18秒）に倒れるのを防ぐため。
WITH
unk AS (
    -- ガード③（正体不明レアリティの除外）の対象。旧版の COALESCE(rarity,'') NOT IN と同じ条件
    SELECT rd.rarity_id FROM rarity_dim rd
    WHERE COALESCE(rd.rarity, '') IN ('(不明)', '')
),
d_new AS MATERIALIZED (
    SELECT p.recorded_at AS d
    FROM price_history_v2 p
    WHERE p.recorded_at >= cutoff_date::date
      AND p.min_price > 10
      AND p.rarity_id NOT IN (SELECT rarity_id FROM unk)
    ORDER BY p.recorded_at DESC
    LIMIT 1
),
d_old AS MATERIALIZED (
    SELECT p.recorded_at AS d
    FROM price_history_v2 p
    WHERE p.recorded_at >= cutoff_date::date
      AND p.recorded_at < (SELECT d FROM d_new)
      AND p.min_price > 10
      AND p.rarity_id NOT IN (SELECT rarity_id FROM unk)
    ORDER BY p.recorded_at DESC
    LIMIT 1
),
s_new AS MATERIALIZED (
    SELECT p.card_id, p.shop_id, p.rarity_id, p.min_price
    FROM price_history_v2 p
    WHERE p.recorded_at = (SELECT d FROM d_new)
      AND p.min_price > 10
      AND p.rarity_id NOT IN (SELECT rarity_id FROM unk)
),
s_old AS MATERIALIZED (
    SELECT p.card_id, p.shop_id, p.rarity_id, p.min_price
    FROM price_history_v2 p
    WHERE p.recorded_at = (SELECT d FROM d_old)
      AND p.min_price > 10
      AND p.rarity_id NOT IN (SELECT rarity_id FROM unk)
),
-- ガード①（共通店舗）: 比較2日の両方に記録がある店舗だけを突き合わせ、
-- カード×レアリティごとに共通店舗内の最安値と、同方向に動いた店舗数を集計
agg AS (
    SELECT
        n.card_id,
        n.rarity_id,
        MIN(n.min_price)                                     AS today_price,
        MIN(o.min_price)                                     AS prev_price,
        COUNT(*) FILTER (WHERE n.min_price > o.min_price)    AS up_shops,
        COUNT(*) FILTER (WHERE n.min_price < o.min_price)    AS down_shops
    FROM s_new n
    JOIN s_old o USING (card_id, shop_id, rarity_id)
    GROUP BY n.card_id, n.rarity_id
),
-- 前日比を計算。ガード②で単一店舗のみの変動を除外し、ここで初めて名前を付ける
movers AS (
    SELECT
        c.card_name,
        r.rarity,
        a.today_price,
        a.prev_price,
        a.today_price - a.prev_price                              AS diff,
        ROUND(
            (a.today_price - a.prev_price)::numeric
            / a.prev_price * 100, 1
        )                                                           AS pct,
        (SELECT d::text FROM d_new)                                 AS date_new,
        (SELECT d::text FROM d_old)                                 AS date_old
    FROM agg a
    JOIN card_dim   c ON c.card_id   = a.card_id
    JOIN rarity_dim r ON r.rarity_id = a.rarity_id
    WHERE a.prev_price > 0
      AND a.today_price != a.prev_price
      AND (
          (a.today_price > a.prev_price AND a.up_shops   >= 2)
          OR
          (a.today_price < a.prev_price AND a.down_shops >= 2)
      )
),
-- 変動幅フィルタ + 同名カード内の代表レアリティを選出
ranked AS (
    SELECT
        card_name, rarity, today_price, prev_price, diff, pct, date_new, date_old,
        CASE WHEN diff > 0 THEN 'up' ELSE 'down' END AS direction,
        ROW_NUMBER() OVER (
            PARTITION BY card_name, CASE WHEN diff > 0 THEN 'up' ELSE 'down' END
            ORDER BY ABS(pct) DESC, ABS(diff) DESC, today_price DESC, rarity
        ) AS card_rn
    FROM movers
    WHERE ABS(diff) >= min_diff
),
-- 同名カード・同方向を per_card 枠まで絞った後、方向別に上位N件を抽出
top_ranked AS (
    SELECT
        card_name, rarity, today_price, prev_price, diff, pct, date_new, date_old, direction,
        ROW_NUMBER() OVER (
            PARTITION BY direction
            ORDER BY ABS(pct) DESC, ABS(diff) DESC, card_name, rarity
        ) AS rn
    FROM ranked
    WHERE card_rn <= per_card
)
SELECT card_name, rarity, today_price, prev_price, diff, pct, date_new, date_old, direction
FROM top_ranked
WHERE rn <= top_n
ORDER BY direction, ABS(pct) DESC;
$$;

-- 買取値動きランキング用 RPC（buyback_history テーブル）
-- 引数・戻り値は get_price_movers と同じ構造（per_card含む）。
-- price_history.min_price の代わりに buyback_history.max_price を使用。
--
-- ガード①（共通店舗）のみ適用する（買取は元々店舗数が少なく、ガード②の
-- 「同方向2店以上」を課すと該当ゼロになりやすいため対象外）。
-- up_shops/down_shops は将来のガード②導入に備えて集計だけ残し、WHEREには使わない。
DROP FUNCTION IF EXISTS get_buyback_movers(text, int, int);

CREATE OR REPLACE FUNCTION get_buyback_movers(
    cutoff_date text DEFAULT (CURRENT_DATE - INTERVAL '3 days')::text,
    min_diff    int  DEFAULT 50,
    top_n       int  DEFAULT 20,
    per_card    int  DEFAULT 1
)
RETURNS TABLE (
    card_name   text,
    rarity      text,
    today_price int,
    prev_price  int,
    diff        int,
    pct         numeric,
    date_new    text,
    date_old    text,
    direction   text
)
LANGUAGE sql
STABLE
AS $$
WITH
-- 店舗×カード×レアリティ×日付ごとの日次最高買取額を集計（10円以下の異常値を除外）
-- ガード③（正体不明レアリティの除外）: 販売版と同じ理由。買取側はガード②が無いぶん
-- 単店の段差がそのまま通るため、正体不明系列を残す危険は販売より大きい。
-- なお buyback_history の空文字 rarity は 2026-08-03 時点でも書き込みが続いている
-- （カードラボ・トレコロCBの2店）。
shop_daily AS (
    SELECT
        card_name,
        rarity,
        shop,
        recorded_at::date::text AS rec_date,
        MAX(max_price)          AS daily_price
    FROM buyback_history
    WHERE recorded_at >= cutoff_date::date
      AND max_price > 10
      AND COALESCE(rarity, '') NOT IN ('(不明)', '')
    GROUP BY card_name, rarity, shop, recorded_at::date::text
),
-- 比較に使う直近2日を特定
dates AS (
    SELECT
        MAX(rec_date) AS date_new,
        MIN(rec_date) AS date_old
    FROM (
        SELECT DISTINCT rec_date
        FROM shop_daily
        ORDER BY rec_date DESC
        LIMIT 2
    ) t
),
-- ガード①: 比較2日の両方に記録がある店舗（共通店舗）だけを横持ちで抽出
shop_pairs AS (
    SELECT
        s_new.card_name,
        s_new.rarity,
        s_new.shop,
        s_new.daily_price AS new_price,
        s_old.daily_price AS old_price
    FROM       shop_daily s_new
    JOIN       shop_daily s_old  ON s_new.card_name = s_old.card_name
                                 AND s_new.rarity    = s_old.rarity
                                 AND s_new.shop      = s_old.shop
    CROSS JOIN dates
    WHERE s_new.rec_date = dates.date_new
      AND s_old.rec_date = dates.date_old
),
-- カード×レアリティごとに共通店舗内の最高値と、同方向に動いた店舗数を集計
-- （up_shops/down_shopsは将来のガード②導入用。買取側のWHEREには使用しない）
agg AS (
    SELECT
        card_name,
        rarity,
        MAX(new_price)                                        AS today_price,
        MAX(old_price)                                        AS prev_price,
        COUNT(*) FILTER (WHERE new_price > old_price)          AS up_shops,
        COUNT(*) FILTER (WHERE new_price < old_price)          AS down_shops
    FROM shop_pairs
    GROUP BY card_name, rarity
),
-- カード×レアリティごとの前日比を計算
movers AS (
    SELECT
        agg.card_name,
        agg.rarity,
        agg.today_price,
        agg.prev_price,
        agg.today_price - agg.prev_price                          AS diff,
        ROUND(
            (agg.today_price - agg.prev_price)::numeric
            / agg.prev_price * 100, 1
        )                                                           AS pct,
        dates.date_new,
        dates.date_old
    FROM       agg
    CROSS JOIN dates
    WHERE agg.prev_price > 0
      AND agg.today_price != agg.prev_price
),
-- 変動幅フィルタ + 同名カード内の代表レアリティを選出
ranked AS (
    SELECT
        card_name, rarity, today_price, prev_price, diff, pct, date_new, date_old,
        CASE WHEN diff > 0 THEN 'up' ELSE 'down' END AS direction,
        ROW_NUMBER() OVER (
            PARTITION BY card_name, CASE WHEN diff > 0 THEN 'up' ELSE 'down' END
            ORDER BY ABS(pct) DESC, ABS(diff) DESC, today_price DESC, rarity
        ) AS card_rn
    FROM movers
    WHERE ABS(diff) >= min_diff
),
-- 同名カード・同方向を per_card 枠まで絞った後、方向別に上位N件を抽出
top_ranked AS (
    SELECT
        card_name, rarity, today_price, prev_price, diff, pct, date_new, date_old, direction,
        ROW_NUMBER() OVER (
            PARTITION BY direction
            ORDER BY ABS(pct) DESC, ABS(diff) DESC, card_name, rarity
        ) AS rn
    FROM ranked
    WHERE card_rn <= per_card
)
SELECT card_name, rarity, today_price, prev_price, diff, pct, date_new, date_old, direction
FROM top_ranked
WHERE rn <= top_n
ORDER BY direction, ABS(pct) DESC;
$$;
