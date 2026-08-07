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

CREATE OR REPLACE FUNCTION public.get_card_best_prices(cutoff_date text)
 RETURNS TABLE(card_name text, shop text, rarity text, min_price integer, recorded_at text)
 LANGUAGE sql
 SECURITY DEFINER
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
