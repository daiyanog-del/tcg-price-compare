-- get_card_best_prices — /api/deck-estimate（_estimate_cache）と /api/featured の唯一の値源
-- =============================================
-- 監査F3（docs/audit-price-logic-2026-08-06.md）で「定義が本番DBにしか無い」ことが判明したため
-- リポジトリに収載。変更時は必ずこのファイルを正本として更新すること。
--
-- 【2026-08-07 定義変更（適用はSupabase SQL Editorで実行）】
-- 旧定義は「7日窓の全店舗・全レアリティ・全日付の最安1行」＝7日間の底値。
-- 実測で61%のカードが3日以上前の観測値を「現在の最安」として返しており、
-- 見積もり合計が系統的に安値へ偏っていた。
-- 新定義: 各(店舗,レアリティ)系列の「窓内で最新の観測日」の値だけを現在価格の候補にし、
-- その最安を返す。窓(cutoff)は欠測補完（今日収集が無かった店舗の直近値を使う）のためだけに使う。
-- 併せて ¥10以下の異常値ガードを他経路（RPC movers / aggregations / notify / x_poster）と統一。
-- 事前計測(2026-08-07): 2,690枚中194枚(7.2%)が上昇・中央値+20%・カバレッジ喪失0枚。
-- 注: rarity="(不明)" は除外しない（購入可能な実在出品であり「最安で買う」目的には有効なため）。
--
-- 【2026-09-02 起動時57014（statement timeout）と3秒タイムアウト対策】
-- 本番実測: 7日分27万行のDISTINCT ONの並べ替えがwork_mem不足でディスクに溢れ6.9秒
-- （VACUUM後2.1秒）かかっていた。set local work_mem='96MB'を与えると0.8秒まで縮む
-- （並べ替え自体の実測必要量は35MB）。57014はこのwork_mem不足→ディスク並べ替え→
-- 遅延の結果としてstatement timeout（3秒）を超過して出ていたもの
-- （メモリ不足そのものの警告ではない）。さらにapp.pyの_load_estimate_cache_innerは
-- PostgRESTの1000行上限のためこのRPCをrangeで最大4ページ分＝4回実行していた。
-- 対策: (1) 本関数にwork_mem='64MB'を固定設定する（毎回のset local相当。
-- CREATE OR REPLACE 本体に内包する。外付けの ALTER FUNCTION ... SET は
-- 次回の CREATE OR REPLACE で無言で既定に戻ってしまうため使わない）、
-- (2) 結果をjsonbに一括集約して1回のRPC呼び出しで返す get_card_best_prices_json を
-- 新設し、app.py側はこちらを優先経路にしてページング呼び出しの4回実行を解消する
-- （本関数未適用の環境向けに、旧ページング経路はapp.py側にフォールバックとして残す）。

CREATE OR REPLACE FUNCTION public.get_card_best_prices(cutoff_date text)
 RETURNS TABLE(card_name text, shop text, rarity text, min_price integer, recorded_at text)
 LANGUAGE sql
 STABLE
 SECURITY DEFINER
 SET work_mem = '64MB'
 SET search_path = public, pg_temp
AS $function$
  WITH latest AS (
    SELECT DISTINCT ON (ph.card_name, ph.shop, ph.rarity)
      ph.card_name, ph.shop, ph.rarity, ph.min_price, ph.recorded_at
    FROM price_history ph
    WHERE ph.recorded_at >= cutoff_date::date
      AND ph.min_price IS NOT NULL AND ph.min_price > 10
    ORDER BY ph.card_name, ph.shop, ph.rarity, ph.recorded_at DESC
  )
  SELECT DISTINCT ON (l.card_name)
    l.card_name, l.shop, l.rarity, l.min_price, l.recorded_at::text
  FROM latest l
  ORDER BY l.card_name, l.min_price ASC;
$function$;

-- get_card_best_prices の結果を1回のRPC呼び出しでjsonbに一括集約して返す。
-- app.py の PostgREST 1000行上限に伴う range 分割ページング（最大4回のRPC実行）を
-- 1回にまとめるための経路（2026-09-02追加）。SECURITY DEFINERは付けない
-- （内側の get_card_best_prices が既に SECURITY DEFINER であり、二重に付ける必要が無い）。
CREATE OR REPLACE FUNCTION public.get_card_best_prices_json(cutoff_date text)
 RETURNS jsonb
 LANGUAGE sql
 STABLE
 SET work_mem = '64MB'
 SET search_path = public, pg_temp
AS $function$
  SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY t.card_name), '[]'::jsonb)
  FROM public.get_card_best_prices(cutoff_date) t;
$function$;

-- 【ロールバック用: 2026-08-07以前の旧定義】
-- CREATE OR REPLACE FUNCTION public.get_card_best_prices(cutoff_date text)
--  RETURNS TABLE(card_name text, shop text, rarity text, min_price integer, recorded_at text)
--  LANGUAGE sql
--  SECURITY DEFINER
-- AS $function$
--     SELECT DISTINCT ON (ph.card_name)
--       ph.card_name, ph.shop, ph.rarity, ph.min_price, ph.recorded_at::text
--     FROM price_history ph
--     WHERE ph.recorded_at >= cutoff_date::date
--       AND ph.min_price IS NOT NULL AND ph.min_price > 0
--     ORDER BY ph.card_name, ph.min_price ASC;
-- $function$;
