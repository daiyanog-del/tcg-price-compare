-- 2026-10-22 get_price_movers・get_card_best_prices 書き換えの新旧照合（読み取りのみ）
-- 使い方: 本番に新版を適用する「前」に、SQL Editor か MCP の execute_sql で1回に実行する。
-- pg_temp の関数は接続を閉じると消えるので、本番のスキーマは変わらない。
-- 期待: old_only・new_only がすべて 0。best は price_old_only・price_new_only が 0
-- （同じ最安値の系列があるカードは店・レアリティ・日付が変わりうるので fullrow_diff は 0 でなくてよい）。
-- 10-22 の結果: movers 6パターン＋上限つき5パターン一致・集計 2,405/2,721/4,054 系列一致・best 最安値 6,328枚一致。
-- 新版の本体は supabase_rpc_movers.sql / supabase_rpc_best_prices.sql からそのまま写して pg_temp.m_new / pg_temp.b_new にする。

CREATE FUNCTION pg_temp.m_old(cutoff_date text, min_diff int, top_n int, per_card int) RETURNS TABLE (card_name text, rarity text, today_price int, prev_price int, diff int, pct numeric, date_new text, date_old text, direction text) LANGUAGE sql STABLE AS $f$
WITH
shop_daily AS (
 SELECT
 card_name,
 rarity,
 shop,
 recorded_at::date::text AS rec_date,
 MIN(min_price) AS daily_price
 FROM price_history
 WHERE recorded_at >= cutoff_date::date
 AND min_price > 10
 AND COALESCE(rarity, '') NOT IN ('(不明)', '')
 GROUP BY card_name, rarity, shop, recorded_at::date::text
),
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
shop_pairs AS (
 SELECT
 s_new.card_name,
 s_new.rarity,
 s_new.shop,
 s_new.daily_price AS new_price,
 s_old.daily_price AS old_price
 FROM shop_daily s_new
 JOIN shop_daily s_old ON s_new.card_name = s_old.card_name
 AND s_new.rarity = s_old.rarity
 AND s_new.shop = s_old.shop
 CROSS JOIN dates
 WHERE s_new.rec_date = dates.date_new
 AND s_old.rec_date = dates.date_old
),
agg AS (
 SELECT
 card_name,
 rarity,
 MIN(new_price) AS today_price,
 MIN(old_price) AS prev_price,
 COUNT(*) FILTER (WHERE new_price > old_price) AS up_shops,
 COUNT(*) FILTER (WHERE new_price < old_price) AS down_shops
 FROM shop_pairs
 GROUP BY card_name, rarity
),
movers AS (
 SELECT
 agg.card_name,
 agg.rarity,
 agg.today_price,
 agg.prev_price,
 agg.today_price - agg.prev_price AS diff,
 ROUND(
 (agg.today_price - agg.prev_price)::numeric
 / agg.prev_price * 100, 1
 ) AS pct,
 dates.date_new,
 dates.date_old
 FROM agg
 CROSS JOIN dates
 WHERE agg.prev_price > 0
 AND agg.today_price != agg.prev_price
 AND (
 (agg.today_price > agg.prev_price AND agg.up_shops >= 2)
 OR
 (agg.today_price < agg.prev_price AND agg.down_shops >= 2)
 )
),
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
$f$;
CREATE FUNCTION pg_temp.m_new(cutoff_date text, min_diff int, top_n int, per_card int) RETURNS TABLE (card_name text, rarity text, today_price int, prev_price int, diff int, pct numeric, date_new text, date_old text, direction text) LANGUAGE sql STABLE AS $f$
WITH
unk AS (
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
agg AS (
 SELECT
 n.card_id,
 n.rarity_id,
 MIN(n.min_price) AS today_price,
 MIN(o.min_price) AS prev_price,
 COUNT(*) FILTER (WHERE n.min_price > o.min_price) AS up_shops,
 COUNT(*) FILTER (WHERE n.min_price < o.min_price) AS down_shops
 FROM s_new n
 JOIN s_old o USING (card_id, shop_id, rarity_id)
 GROUP BY n.card_id, n.rarity_id
),
movers AS (
 SELECT
 c.card_name,
 r.rarity,
 a.today_price,
 a.prev_price,
 a.today_price - a.prev_price AS diff,
 ROUND(
 (a.today_price - a.prev_price)::numeric
 / a.prev_price * 100, 1
 ) AS pct,
 (SELECT d::text FROM d_new) AS date_new,
 (SELECT d::text FROM d_old) AS date_old
 FROM agg a
 JOIN card_dim c ON c.card_id = a.card_id
 JOIN rarity_dim r ON r.rarity_id = a.rarity_id
 WHERE a.prev_price > 0
 AND a.today_price != a.prev_price
 AND (
 (a.today_price > a.prev_price AND a.up_shops >= 2)
 OR
 (a.today_price < a.prev_price AND a.down_shops >= 2)
 )
),
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
$f$;
select k, (select count(*) from pg_temp.m_old(c,m,t,p)) old_n, (select count(*) from pg_temp.m_new(c,m,t,p)) new_n,
 (select count(*) from (select * from pg_temp.m_old(c,m,t,p) except all select * from pg_temp.m_new(c,m,t,p)) a) old_only,
 (select count(*) from (select * from pg_temp.m_new(c,m,t,p) except all select * from pg_temp.m_old(c,m,t,p)) b) new_only
from (values (1,'2026-10-19',50,20,1),(2,'2026-10-19',50,1000,999),(3,'2026-10-19',10,1000,999),(4,'2026-10-21',1,5000,999)) v(k,c,m,t,p);

-- ── get_card_best_prices（pg_temp.b_new に新版の本体を写してから実行） ──
-- with v(k) as (values ('2026-10-15'),('2026-10-21'),('2026-10-01')),
-- o as materialized (select v.k, t.* from v, lateral public.get_card_best_prices(v.k) t),
-- n as materialized (select v.k, t.* from v, lateral pg_temp.b_new(v.k) t)
-- select k,
--  (select count(*) from o where o.k=v.k) old_n, (select count(*) from n where n.k=v.k) new_n,
--  (select count(*) from (select card_name,min_price from o where o.k=v.k except all select card_name,min_price from n where n.k=v.k) a) price_old_only,
--  (select count(*) from (select card_name,min_price from n where n.k=v.k except all select card_name,min_price from o where o.k=v.k) a) price_new_only
-- from v;
