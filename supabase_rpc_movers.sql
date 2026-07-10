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
WITH
-- 店舗×カード×レアリティ×日付ごとの日次最安値を集計（10円以下の異常値を除外）
shop_daily AS (
    SELECT
        card_name,
        rarity,
        shop,
        recorded_at::date::text AS rec_date,
        MIN(min_price)          AS daily_price
    FROM price_history
    WHERE recorded_at >= cutoff_date::date
      AND min_price > 10
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
-- カード×レアリティごとに共通店舗内の最安値と、同方向に動いた店舗数を集計
agg AS (
    SELECT
        card_name,
        rarity,
        MIN(new_price)                                        AS today_price,
        MIN(old_price)                                        AS prev_price,
        COUNT(*) FILTER (WHERE new_price > old_price)          AS up_shops,
        COUNT(*) FILTER (WHERE new_price < old_price)          AS down_shops
    FROM shop_pairs
    GROUP BY card_name, rarity
),
-- カード×レアリティごとの前日比を計算。ガード②で単一店舗のみの変動を除外
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
      AND (
          (agg.today_price > agg.prev_price AND agg.up_shops   >= 2)
          OR
          (agg.today_price < agg.prev_price AND agg.down_shops >= 2)
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
