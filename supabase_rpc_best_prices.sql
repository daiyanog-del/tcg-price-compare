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

-- 【2026-10-22 休止中の店を外す引数 excluded_shops を追加】
-- カーナベルが PHP の脆弱性でサイトを止めた（10-21 20:25〜）あとも、7日窓に残る休止前の観測が
-- 「最安 カーナベル」として見積もりに出続けるため、環境変数 SUSPENDED_SHOPS の店を外せるようにした
-- （shop_status.py）。既定は空配列で、引数を渡さない呼び出しは従来と同じ結果。
-- 引数を足すと別の関数（多重定義）になり PostgREST が呼び分けられなくなるので、旧い1引数版は DROP する。
-- 実行権限は旧版と同じ（anon / authenticated / service_role）を付け直す。
-- 適用は1つのトランザクションで行う（DROP と CREATE の間に呼び出しが失敗しないように）。
BEGIN;

DROP FUNCTION IF EXISTS public.get_card_best_prices_json(text);
DROP FUNCTION IF EXISTS public.get_card_best_prices(text);

CREATE OR REPLACE FUNCTION public.get_card_best_prices(cutoff_date text, excluded_shops text[] DEFAULT '{}')
 RETURNS TABLE(card_name text, shop text, rarity text, min_price integer, recorded_at text)
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET work_mem TO '64MB'
 SET search_path TO 'public', 'pg_temp'
AS $function$
  -- 【2026-10-22 読み方の書き換え（最安値の定義は同じ・同じ値段のときの選び方を新たに決めた）】旧版はビュー price_history 経由で
  -- 7日窓の全行（約28.6万行）に名前を付けてから、名前の文字列で並べ替えていた（37MB）。
  -- 新版はカード番号・店番号・レアリティ番号のまま「系列ごとの最新日」→「カードごとの
  -- 最安」を選び、カード名は残った1カード1行（約6,300行）にだけ付ける。名前は card_dim・
  -- shop_dim・rarity_dim でそれぞれ一意（UNIQUE 制約）なので、番号で束ねても名前で束ねても同じ。
  -- 同じ時刻の本番実測（10-22）で 1.0秒 → 0.45秒、並べ替え 37MB → 21MB。
  -- win を MATERIALIZED にするのは、日付の索引 (recorded_at, …) INCLUDE (min_price) だけで
  -- 窓を読ませるため（外すと主キーを全件なめる計画になり、実測 11秒）。
  WITH win AS MATERIALIZED (
    SELECT p.card_id, p.shop_id, p.rarity_id, p.min_price, p.recorded_at
    FROM price_history_v2 p
    WHERE p.recorded_at >= cutoff_date::date
      AND p.min_price IS NOT NULL AND p.min_price > 10
  ),
  latest AS (
    SELECT DISTINCT ON (w.card_id, w.shop_id, w.rarity_id)
      w.card_id, w.shop_id, w.rarity_id, w.min_price, w.recorded_at
    FROM win w
    ORDER BY w.card_id, w.shop_id, w.rarity_id, w.recorded_at DESC
  ),
  -- 同じ最安値の系列が複数あるとき（10-22 実測 6,328枚中 1,850枚）、旧版は並べ替えの
  -- 都合で決まる1つを返していた（決まった規則なし）。新版は観測日が新しい順→店名→
  -- レアリティ名で1つに決める（同じ値段なら、より最近確かめた店を出す）。
  -- 休止中の店（excluded_shops）は最安の候補にしない（10-22 追加）。
  best AS (
    SELECT DISTINCT ON (l.card_id)
      l.card_id, s.shop, r.rarity, l.min_price, l.recorded_at
    FROM latest l
    JOIN shop_dim   s ON s.shop_id   = l.shop_id
    JOIN rarity_dim r ON r.rarity_id = l.rarity_id
    WHERE s.shop <> ALL (coalesce(excluded_shops, '{}'))
    ORDER BY l.card_id, l.min_price ASC, l.recorded_at DESC, s.shop, r.rarity
  )
  SELECT c.card_name, b.shop, b.rarity, b.min_price, b.recorded_at::text
  FROM best b
  JOIN card_dim c ON c.card_id = b.card_id;
$function$;

-- get_card_best_prices の結果を1回のRPC呼び出しでjsonbに一括集約して返す。
-- SECURITY DEFINER は付けない（内側の get_card_best_prices が既に SECURITY DEFINER）。
CREATE OR REPLACE FUNCTION public.get_card_best_prices_json(cutoff_date text, excluded_shops text[] DEFAULT '{}')
 RETURNS jsonb
 LANGUAGE sql
 STABLE
 SET work_mem TO '64MB'
 SET search_path TO 'public', 'pg_temp'
AS $function$
  SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY t.card_name), '[]'::jsonb)
  FROM public.get_card_best_prices(cutoff_date, excluded_shops) t;
$function$;

GRANT EXECUTE ON FUNCTION public.get_card_best_prices(text, text[]) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.get_card_best_prices_json(text, text[]) TO anon, authenticated, service_role;

COMMIT;

-- PostgREST に関数の入れ替えを知らせる（Supabase 標準のイベントトリガーがあれば不要だが、念のため）
NOTIFY pgrst, 'reload schema';

-- 【ロールバックの注意（2026-10-22〜）】
-- 下の旧定義や10-22以前の1引数版を CREATE OR REPLACE で戻すときは、先に2引数版を消すこと。
-- 消さないと同名の関数が2つ並び、PostgREST が呼び分けられず（PGRST203）相場キャッシュが更新されなくなる。
--   DROP FUNCTION public.get_card_best_prices_json(text, text[]);
--   DROP FUNCTION public.get_card_best_prices(text, text[]);
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
