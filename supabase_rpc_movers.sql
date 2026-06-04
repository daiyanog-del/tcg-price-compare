-- 値動きランキング用 RPC（販売価格版）
-- Supabase の SQL Editor で実行してください
--
-- 引数:
--   cutoff_date text  比較対象の開始日（YYYY-MM-DD形式）例: '2026-06-02'
--   min_diff    int   最小変動額のフィルタ（デフォルト50円）
--   top_n       int   上位何件を返すか（デフォルト20件）
--
-- 戻り値 (行ごと):
--   card_name     text   カード名
--   today_price   int    直近日の最安値
--   prev_price    int    前日の最安値
--   diff          int    価格差 (today - prev)
--   pct           numeric 変動率 (%)
--   date_new      text   直近日 (YYYY-MM-DD)
--   date_old      text   前日   (YYYY-MM-DD)
--   direction     text   'up' | 'down'

CREATE OR REPLACE FUNCTION get_price_movers(
    cutoff_date text DEFAULT (CURRENT_DATE - INTERVAL '2 days')::text,
    min_diff    int  DEFAULT 50,
    top_n       int  DEFAULT 20
)
RETURNS TABLE (
    card_name   text,
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
-- 直近2日の日次最安値をカードごとに集計（10円以下の異常値を除外）
daily_min AS (
    SELECT
        card_name,
        recorded_at::date::text AS rec_date,
        MIN(min_price)          AS daily_price
    FROM price_history
    WHERE recorded_at >= cutoff_date::date
      AND min_price > 10
    GROUP BY card_name, recorded_at::date::text
),
-- 比較に使う直近2日を特定
dates AS (
    SELECT
        MAX(rec_date) AS date_new,
        MIN(rec_date) AS date_old
    FROM (
        SELECT DISTINCT rec_date
        FROM daily_min
        ORDER BY rec_date DESC
        LIMIT 2
    ) t
),
-- カードごとの前日比を計算
movers AS (
    SELECT
        d_new.card_name,
        d_new.daily_price                                          AS today_price,
        d_old.daily_price                                          AS prev_price,
        d_new.daily_price - d_old.daily_price                     AS diff,
        ROUND(
            (d_new.daily_price - d_old.daily_price)::numeric
            / d_old.daily_price * 100, 1
        )                                                           AS pct,
        dates.date_new,
        dates.date_old
    FROM       daily_min d_new
    JOIN       daily_min d_old  ON d_new.card_name = d_old.card_name
    CROSS JOIN dates
    WHERE d_new.rec_date = dates.date_new
      AND d_old.rec_date = dates.date_old
      AND d_old.daily_price > 0
      AND d_new.daily_price != d_old.daily_price
),
-- 変動幅フィルタ + 上位N件ずつ抽出
ranked AS (
    SELECT
        card_name, today_price, prev_price, diff, pct, date_new, date_old,
        CASE WHEN diff > 0 THEN 'up' ELSE 'down' END AS direction,
        ROW_NUMBER() OVER (
            PARTITION BY CASE WHEN diff > 0 THEN 'up' ELSE 'down' END
            ORDER BY ABS(pct) DESC
        ) AS rn
    FROM movers
    WHERE ABS(diff) >= min_diff
)
SELECT card_name, today_price, prev_price, diff, pct, date_new, date_old, direction
FROM ranked
WHERE rn <= top_n
ORDER BY direction, ABS(pct) DESC;
$$;

-- 買取値動きランキング用 RPC（buyback_history テーブル）
-- 引数・戻り値は get_price_movers と同じ構造。
-- price_history.min_price の代わりに buyback_history.max_price を使用。

CREATE OR REPLACE FUNCTION get_buyback_movers(
    cutoff_date text DEFAULT (CURRENT_DATE - INTERVAL '3 days')::text,
    min_diff    int  DEFAULT 50,
    top_n       int  DEFAULT 20
)
RETURNS TABLE (
    card_name   text,
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
-- 直近の日次最高買取額をカードごとに集計（10円以下の異常値を除外）
daily_max AS (
    SELECT
        card_name,
        recorded_at::date::text AS rec_date,
        MAX(max_price)          AS daily_price
    FROM buyback_history
    WHERE recorded_at >= cutoff_date::date
      AND max_price > 10
    GROUP BY card_name, recorded_at::date::text
),
-- 比較に使う直近2日を特定
dates AS (
    SELECT
        MAX(rec_date) AS date_new,
        MIN(rec_date) AS date_old
    FROM (
        SELECT DISTINCT rec_date
        FROM daily_max
        ORDER BY rec_date DESC
        LIMIT 2
    ) t
),
-- カードごとの前日比を計算
movers AS (
    SELECT
        d_new.card_name,
        d_new.daily_price                                          AS today_price,
        d_old.daily_price                                          AS prev_price,
        d_new.daily_price - d_old.daily_price                     AS diff,
        ROUND(
            (d_new.daily_price - d_old.daily_price)::numeric
            / d_old.daily_price * 100, 1
        )                                                           AS pct,
        dates.date_new,
        dates.date_old
    FROM       daily_max d_new
    JOIN       daily_max d_old  ON d_new.card_name = d_old.card_name
    CROSS JOIN dates
    WHERE d_new.rec_date = dates.date_new
      AND d_old.rec_date = dates.date_old
      AND d_old.daily_price > 0
      AND d_new.daily_price != d_old.daily_price
),
-- 変動幅フィルタ + 上位N件ずつ抽出
ranked AS (
    SELECT
        card_name, today_price, prev_price, diff, pct, date_new, date_old,
        CASE WHEN diff > 0 THEN 'up' ELSE 'down' END AS direction,
        ROW_NUMBER() OVER (
            PARTITION BY CASE WHEN diff > 0 THEN 'up' ELSE 'down' END
            ORDER BY ABS(pct) DESC
        ) AS rn
    FROM movers
    WHERE ABS(diff) >= min_diff
)
SELECT card_name, today_price, prev_price, diff, pct, date_new, date_old, direction
FROM ranked
WHERE rn <= top_n
ORDER BY direction, ABS(pct) DESC;
$$;
