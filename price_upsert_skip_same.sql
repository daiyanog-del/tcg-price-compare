-- =============================================
-- 販売価格の書き込み関数 upsert_price_rows: 値が同じ行は書き直さない（2026-11-01）
-- =============================================
-- なぜ: Supabase 無料 Nano の Disk IO を使い切って DB が固まった（11-01 19:03〜20:20 JST。docs/decisions.md 同日）。
--   今までは同じ (カード, 店, レアリティ, 日) の行があると、値段が同じでも無条件に書き直していた。新弾の収集は
--   1日8回同じカードを取り直し、検索も同じ日の行を書き直す（再起動後の1時間で新規94行に対し上書き1,249行）。
--   書き直すたびに行の新しい版・WAL・死んだ行（autovacuum の掃除）が出る。
-- 何を: ON CONFLICT DO UPDATE に「min_price・min_price_any・code・url のどれかが違うときだけ」の条件を足す。
--   戻り値に changed_rows（実際に挿入・書き換えた行）を足す。saved_rows の意味は変えない
--   （上書き経路は受け付けた行。収集は saved_rows で成功を数え、0件を失敗として取り直すため）。
--   price_persist.upsert_price_rows が changed_rows をログに出す（効き目を測る）。
-- 変えないもの: 絵ごとの内訳（price_history_art）の消し直し。本表の値が同じでも絵ごとの値は変わりうるので、
--   上書き経路では今までどおり同じキーの行を消し、直後の upsert_price_art_rows で入れ直す。
-- 中身は price_history_art.sql 末尾の定義（11-01 本番の pg_get_functiondef と同じ）に上の2点を足しただけ。
-- 戻り値の型を変えるので CREATE OR REPLACE ではなく、1トランザクションで DROP → CREATE → 権限
--   （DDL はトランザクションに入るので、ほかの書き込みが関数の無い瞬間に当たらない）。
-- 適用順: このファイル → コードのデプロイ（古いコードは changed_rows を読まないだけで動く。新しいコードは
--   changed_rows が無ければ数えないだけで動く）。
-- 戻し方: price_history_art.sql 末尾の定義を、同じく DROP → CREATE で流し直す（戻り値の型が戻るため）。
--   新しく CREATE すると Supabase の既定で anon・authenticated に EXECUTE が付く。SECURITY DEFINER の書き込み関数なので
--   REVOKE は FROM PUBLIC, anon, authenticated まで（11-01 本番の proacl は postgres と service_role だけ）。
-- 測るときの注意（reviewer 中1）: ON CONFLICT の WHERE で書き直さなかった行にも行ロックはかかる（行の見出しを書き換え、
--   WAL に lock の記録が出る）。減るのは新しい版・索引の追加・死んだ行で、WAL とページの書き出しはゼロにならない。
--   効き目は n_tup_upd だけでなく pg_stat_statements のこの RPC の wal_bytes・shared_blks_dirtied でも見る。
BEGIN;
DROP FUNCTION IF EXISTS public.upsert_price_rows(jsonb, boolean);
CREATE FUNCTION public.upsert_price_rows(p_rows jsonb, p_ignore_duplicates boolean DEFAULT false)
RETURNS TABLE (saved_rows integer, changed_rows integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $function$
DECLARE
  n        integer := 0;
  n_in     integer := 0;
  n_valid  integer := 0;
  n_changed integer := 0;
BEGIN
  IF p_rows IS NULL OR jsonb_typeof(p_rows) <> 'array' THEN
    RAISE EXCEPTION 'upsert_price_rows: p_rows must be a JSON array';
  END IF;
  n_in := jsonb_array_length(p_rows);

  DROP TABLE IF EXISTS _upsert_rows;
  CREATE TEMP TABLE _upsert_rows ON COMMIT DROP AS
    SELECT DISTINCT ON (card_name, shop, rarity, recorded_at) *
    FROM (
      SELECT x->>'card_name'                AS card_name,
             x->>'shop'                     AS shop,
             x->>'rarity'                   AS rarity,
             (x->>'recorded_at')::date      AS recorded_at,
             (x->>'min_price')::integer     AS min_price,
             (x->>'min_price_any')::integer AS min_price_any,
             x->>'code'                     AS code,
             x->>'url'                      AS url,
             ord
      FROM jsonb_array_elements(p_rows) WITH ORDINALITY AS t(x, ord)
    ) s
    WHERE card_name IS NOT NULL AND shop IS NOT NULL AND rarity IS NOT NULL
      AND recorded_at IS NOT NULL AND min_price IS NOT NULL
    ORDER BY card_name, shop, rarity, recorded_at, ord DESC;   -- 同一キーは後勝ち
  SELECT count(*) INTO n_valid FROM _upsert_rows;
  IF n_valid < n_in THEN
    RAISE WARNING 'upsert_price_rows: %件を除外（必須列NULL または同一キーの重複）: 入力%件 → 有効%件',
      n_in - n_valid, n_in, n_valid;
  END IF;

  INSERT INTO public.card_dim (card_name)
    SELECT DISTINCT u.card_name FROM _upsert_rows u
    WHERE NOT EXISTS (SELECT 1 FROM public.card_dim d WHERE d.card_name = u.card_name)
    ON CONFLICT DO NOTHING;
  INSERT INTO public.shop_dim (shop)
    SELECT DISTINCT u.shop FROM _upsert_rows u
    WHERE NOT EXISTS (SELECT 1 FROM public.shop_dim d WHERE d.shop = u.shop)
    ON CONFLICT DO NOTHING;
  INSERT INTO public.rarity_dim (rarity)
    SELECT DISTINCT u.rarity FROM _upsert_rows u
    WHERE NOT EXISTS (SELECT 1 FROM public.rarity_dim d WHERE d.rarity = u.rarity)
    ON CONFLICT DO NOTHING;

  IF p_ignore_duplicates THEN
    INSERT INTO public.price_history_v2 (card_id, shop_id, rarity_id, recorded_at, min_price, min_price_any, code, url)
    SELECT c.card_id, s.shop_id, r.rarity_id, u.recorded_at, u.min_price, u.min_price_any, u.code, u.url
    FROM _upsert_rows u
    JOIN public.card_dim c ON c.card_name = u.card_name
    JOIN public.shop_dim s ON s.shop = u.shop
    JOIN public.rarity_dim r ON r.rarity = u.rarity
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT;
    n_changed := n;
  ELSE
    INSERT INTO public.price_history_v2 (card_id, shop_id, rarity_id, recorded_at, min_price, min_price_any, code, url)
    SELECT c.card_id, s.shop_id, r.rarity_id, u.recorded_at, u.min_price, u.min_price_any, u.code, u.url
    FROM _upsert_rows u
    JOIN public.card_dim c ON c.card_name = u.card_name
    JOIN public.shop_dim s ON s.shop = u.shop
    JOIN public.rarity_dim r ON r.rarity = u.rarity
    ON CONFLICT (card_id, shop_id, rarity_id, recorded_at) DO UPDATE
      SET min_price = EXCLUDED.min_price,
          min_price_any = EXCLUDED.min_price_any,
          code = EXCLUDED.code,
          url = EXCLUDED.url
      -- 値が同じ行は書き直さない（新しい版・WAL・死んだ行の掃除を出さない。2026-11-01）
      WHERE (price_history_v2.min_price, price_history_v2.min_price_any, price_history_v2.code, price_history_v2.url)
            IS DISTINCT FROM (EXCLUDED.min_price, EXCLUDED.min_price_any, EXCLUDED.code, EXCLUDED.url);
    GET DIAGNOSTICS n_changed = ROW_COUNT;
    -- saved_rows は今までどおり「受け付けた行」（同じ値で書き直さなかった行も含む）。収集はこの数で成功を数える
    n := n_valid;
    -- 同じキーの絵ごとの内訳を消す（このあと別表の書き込みで今の値を入れ直す）
    DELETE FROM public.price_history_art a
    USING _upsert_rows u
    JOIN public.card_dim c ON c.card_name = u.card_name
    JOIN public.shop_dim s ON s.shop = u.shop
    JOIN public.rarity_dim r ON r.rarity = u.rarity
    WHERE a.card_id = c.card_id AND a.rarity_id = r.rarity_id AND a.shop_id = s.shop_id
      AND a.recorded_at = u.recorded_at;
  END IF;
  RETURN QUERY SELECT n, n_changed;
END;
$function$;

REVOKE ALL ON FUNCTION public.upsert_price_rows(jsonb, boolean) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.upsert_price_rows(jsonb, boolean) TO service_role;
COMMIT;
-- PostgREST に関数の一覧（戻り値の型）を読み直させる（supabase_rpc_best_prices.sql と同じ）
NOTIFY pgrst, 'reload schema';
