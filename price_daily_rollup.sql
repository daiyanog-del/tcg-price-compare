-- =============================================
-- 販売価格の「日ごとの全店最安」表（2026-10-22 DB容量対策・案D）
-- =============================================
-- 背景: Supabase 無料枠（500MB）に対し DB 846MB。price_history_v2 だけで 608MB（90日・約317万行）。
--   90日分を読むのは相場グラフ（/api/price-history → price-trend.js）と汎用カードX投稿
--   （staple_post.fetch_history）だけで、どちらも「日×レアリティごとの全店最安」しか使わない。
--   店舗別の値を読む機能（値動き・購入候補・新弾マトリクス等）は最長30日。
-- 方針: 店舗別の行（price_history_v2）は30日、31〜100日は「日×カード×レアリティの最安」だけを
--   price_daily_min に畳んで残す（実測 10-20: 店舗別 35,974行 → 9,730組＝約1/3.7）。
--   毎晩 collect_prices.rollup_and_delete_old_prices が、30日より前に残っている日を古い順に1日ずつ
--   rollup_price_daily で畳み、成功した日だけ price_history_v2 から消す（1晩3日まで。畳む前に消すと
--   履歴が失われるため）。失敗や追いつけないときは Discord に知らせる。
-- 読み手: price_daily ビュー（card_name/rarity/shop の文字列で price_history と同じ形）。
--   min_shop_ids を unnest するので、同額の店が複数あれば同じ日に複数行になる（グラフの吹き出しの店名用）。
-- 2つの最安を持つ理由: グラフは 0円超を、汎用X投稿は 10円以下を捨てた値を使う（既存の慣例）。
--   片方だけだと、もう片方の結果が畳む前と変わる。
--
-- 適用順:
--   §1（表・ビュー・関数の作成。既存データに触れない）→ コードをデプロイ
--   → §2 初回の畳み込みと照合（SQL Editor で1行ずつ）→ §3 古い行の削除 → §4 ygores の縮小 → §5 詰め直し
--   コードが先に出ても壊れない: rollup が失敗すると price_history_v2 を消さない（Discord に通知が出る）。
--   §2 の前に夜間が走っても、畳むのは古い3日分だけ（1回の処理は1日分）。
--   /api/price-history・staple_post はビューが無ければ店舗別の行だけで返す。

-- ============ §1 作成 ============
CREATE TABLE IF NOT EXISTS public.price_daily_min (
    card_id          integer  NOT NULL REFERENCES public.card_dim(card_id),
    rarity_id        smallint NOT NULL REFERENCES public.rarity_dim(rarity_id),
    recorded_at      date     NOT NULL,           -- JST日付（price_history_v2 と同じ）
    min_price        integer  NOT NULL,           -- 全店最安（0円以下の行は除いて計算）
    min_shop_ids     smallint[] NOT NULL,         -- min_price を付けた店（同額なら複数・shop_id 昇順）
    min_price_over10 integer,                     -- 10円以下の行を除いた全店最安（汎用X投稿用。無ければ NULL）
    PRIMARY KEY (card_id, rarity_id, recorded_at)
);
ALTER TABLE public.price_daily_min ENABLE ROW LEVEL SECURITY;  -- ポリシーなし＝anon からは表を直接読めない
-- 毎晩の100日超の削除用（主キーは card_id 先頭で日付の範囲を引けない）。日付順に入るので BRIN で足りる
CREATE INDEX IF NOT EXISTS idx_price_daily_min_date ON public.price_daily_min USING brin (recorded_at);

CREATE OR REPLACE VIEW public.price_daily AS
SELECT c.card_name, r.rarity, s.shop,
       d.min_price, d.min_price_over10, d.recorded_at
FROM public.price_daily_min d
JOIN public.card_dim   c ON c.card_id   = d.card_id
JOIN public.rarity_dim r ON r.rarity_id = d.rarity_id
CROSS JOIN LATERAL unnest(d.min_shop_ids) AS u(shop_id)
JOIN public.shop_dim   s ON s.shop_id   = u.shop_id;
GRANT SELECT ON public.price_daily TO anon, authenticated, service_role;

-- p_before より前（p_from 指定時は p_from 以上も条件）の price_history_v2 を畳んで price_daily_min へ入れる。
-- 同じ日を2回畳んでも上書きになるだけ（冪等）。返り値は入れた（上書きした）組の数。
-- 夜間は p_from なし（消し残りの日があっても全部畳む）。初回は p_from で区切って SQL Editor から呼ぶ。
CREATE OR REPLACE FUNCTION public.rollup_price_daily(p_before date, p_from date DEFAULT NULL)
RETURNS TABLE(rolled_rows bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  n bigint;
BEGIN
  INSERT INTO public.price_daily_min
    (card_id, rarity_id, recorded_at, min_price, min_shop_ids, min_price_over10)
  SELECT card_id, rarity_id, recorded_at,
         min(day_min),
         array_agg(DISTINCT shop_id ORDER BY shop_id) FILTER (WHERE min_price = day_min),
         min(min_price) FILTER (WHERE min_price > 10)
  FROM (
    SELECT card_id, rarity_id, recorded_at, shop_id, min_price,
           min(min_price) OVER (PARTITION BY card_id, rarity_id, recorded_at) AS day_min
    FROM public.price_history_v2
    WHERE recorded_at < p_before
      AND (p_from IS NULL OR recorded_at >= p_from)
      AND min_price > 0
  ) src
  GROUP BY card_id, rarity_id, recorded_at
  ON CONFLICT (card_id, rarity_id, recorded_at) DO UPDATE
    SET min_price        = EXCLUDED.min_price,
        min_shop_ids     = EXCLUDED.min_shop_ids,
        min_price_over10 = EXCLUDED.min_price_over10;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN QUERY SELECT n;
END;
$$;
-- 関数は作成時に暗黙で PUBLIC に EXECUTE が付く（anon からの REVOKE だけでは消えない）
REVOKE ALL ON FUNCTION public.rollup_price_daily(date, date) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rollup_price_daily(date, date) TO service_role;

-- ============ §2 初回の畳み込み（デプロイ後・収集の時間帯 JST 05:00〜10:30 を外して1行ずつ） ============
-- 9日ずつ（約30万行ずつ）に分けて、1回の並べ替えと書き込みを小さくする。日付は <実行日-30日> まで。
-- SELECT * FROM public.rollup_price_daily('2026-08-01', NULL);
-- SELECT * FROM public.rollup_price_daily('2026-08-10', '2026-08-01');
-- ...（9日刻み）...
-- SELECT * FROM public.rollup_price_daily('<実行日-30日>', '<直前の区切り>');
-- 確認1: SELECT min(recorded_at), max(recorded_at), count(*) FROM public.price_daily_min;
-- 確認2（全カードで照合・0行なら一致。9日ずつ区切って流す）: 店舗別の行から同じ定義で計算し直した値と突き合わせる
-- WITH s AS (SELECT card_id, rarity_id, recorded_at, shop_id, min_price,
--                   min(min_price) OVER (PARTITION BY card_id, rarity_id, recorded_at) AS m
--            FROM public.price_history_v2
--            WHERE recorded_at >= '2026-08-01' AND recorded_at < '2026-08-10' AND min_price > 0),
--      e AS (SELECT card_id, rarity_id, recorded_at, min(m) AS min_price,
--                   array_agg(DISTINCT shop_id ORDER BY shop_id) FILTER (WHERE min_price = m) AS min_shop_ids,
--                   min(min_price) FILTER (WHERE min_price > 10) AS min_price_over10
--            FROM s GROUP BY 1, 2, 3)
-- (SELECT * FROM e EXCEPT SELECT card_id, rarity_id, recorded_at, min_price, min_shop_ids, min_price_over10
--   FROM public.price_daily_min WHERE recorded_at >= '2026-08-01' AND recorded_at < '2026-08-10')
-- UNION ALL
-- (SELECT card_id, rarity_id, recorded_at, min_price, min_shop_ids, min_price_over10
--   FROM public.price_daily_min WHERE recorded_at >= '2026-08-01' AND recorded_at < '2026-08-10' EXCEPT SELECT * FROM e);

-- ============ §3 古い店舗別の行の削除（§2 の確認後。区切りは §2 と同じ） ============
-- DELETE FROM public.price_history_v2 WHERE recorded_at < '2026-08-01';
-- ...（9日刻み）...
-- DELETE FROM public.price_history_v2 WHERE recorded_at < '<実行日-30日>';
-- 買取（30日保持。読むのは3日と15日だけ）
-- DELETE FROM public.buyback_history WHERE recorded_at < '<実行日-30日>';

-- ============ §4 ygores_cards の raw を日本語だけにする（使っていない9言語を捨てる） ============
-- 見積もり: 5%抜き取りで1行 3,762→2,475バイト（圧縮後）＝全体で約−18MB。faqData（約1.5KB・未使用）は残す
-- 保存時は ygores_repository._slim_raw が同じことをする。既存行はここで一度だけ縮める。
-- konami_id で区切って数回に分ける（1回 5,000行程度）
-- UPDATE public.ygores_cards
--    SET raw = jsonb_set(raw, '{cardData}', jsonb_build_object('ja', raw->'cardData'->'ja'))
--  WHERE konami_id < 10000 AND raw->'cardData' ? 'ja'
--    AND (SELECT count(*) FROM jsonb_object_keys(raw->'cardData')) > 1;
-- （konami_id >= 10000 AND < 15000、>= 15000 … と続ける）

-- ============ §5 詰め直し（ファイルを縮めて空きを返す。実行中はその表の読み書きが止まる） ============
-- VACUUM FULL は作り直した大きさぶんの空きを一時的に使うので、小さい表から順に空ける。
-- DROP TABLE public._migration_backup_20260906_parallel_ph;
-- DROP TABLE public._migration_backup_20260906_parallel_bh;
-- VACUUM FULL public.ygores_cards;
-- VACUUM FULL public.buyback_history;
-- VACUUM FULL public.price_history_v2;
-- ANALYZE public.ygores_cards;
-- ANALYZE public.buyback_history;
-- ANALYZE public.price_history_v2;
-- ANALYZE public.price_daily_min;
-- SELECT pg_size_pretty(pg_database_size(current_database()));
