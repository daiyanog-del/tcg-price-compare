-- =============================================
-- パックの版ごとの価格（新弾用の小さな表。2026-11-01）
-- =============================================
-- 背景: 価格の記録（price_history_v2）はカード×店×レアリティ×日で1行で、同じレアリティに複数の版（再録）があると
--   一番安い版の1行しか残らない。再録カードは、昔の版のほうが安いと「このパックの版」の値が記録に無く、
--   新弾情報ページの店舗×レアの表が「このパックの版は見つかりませんでした」になっていた。
-- 方針（ユーザー選択「新弾用の小さな表」）: 本表はそのまま。新弾の収集と新弾情報ページの「価格を調べる」が、
--   そのパックの型番の頭（例 IMPH）の出品だけを、カード×店×レアリティ×パックの版×日ごとにこの表へ書く。
--   表（/api/packs/matrix）は本表の行にこの表の行を重ねて出す。30日を過ぎた行は毎晩の収集が消す（畳まない）。
-- 見積もり: 新弾100種×6店×レアリティ3×1日数回の上書き＝1日2,000行以下×30日＝6万行・10MB 程度（多めに置いた値）
-- 書き手: price_persist.upsert_price_pack_rows（RPC）。読み手: featured_matrix.get_pack_matrix（price_history_pack_v）
-- 適用順: このファイルを先に適用 → コードをデプロイ（コードが先でも、RPC・ビューが無いとプロセスごとに1回警告して書かない／重ねないだけ）
-- 戻し方: コードを戻す → DROP FUNCTION upsert_price_pack_rows → DROP VIEW price_history_pack_v → DROP TABLE price_history_pack

CREATE TABLE IF NOT EXISTS public.price_history_pack (
    pack_code     text     NOT NULL,          -- パックの型番の頭（大文字。例 "IMPH"）
    recorded_at   date     NOT NULL,          -- JST日付（price_history_v2 と同じ）
    card_id       integer  NOT NULL REFERENCES public.card_dim(card_id),
    shop_id       smallint NOT NULL REFERENCES public.shop_dim(shop_id),
    rarity_id     smallint NOT NULL REFERENCES public.rarity_dim(rarity_id),
    min_price     integer  NOT NULL,          -- 通常品の最安（price_history_v2 と同じ定義）
    min_price_any integer,                    -- 状態を問わない最安
    code          text,                       -- min_price を決めた出品の型番
    url           text,                       -- min_price_any を決めた出品の URL
    PRIMARY KEY (pack_code, recorded_at, card_id, shop_id, rarity_id)  -- 読み手はパック×日付×カードで引く
);
ALTER TABLE public.price_history_pack ENABLE ROW LEVEL SECURITY;
-- 毎晩の30日超の削除用（主キーは pack_code 先頭）。日付順に入るので BRIN で足りる
CREATE INDEX IF NOT EXISTS idx_price_history_pack_date ON public.price_history_pack USING brin (recorded_at);

-- 読み出し用（price_history と同じく名前つき。security_invoker 無し＝公開読み取りデータとして意図的）
CREATE OR REPLACE VIEW public.price_history_pack_v AS
  SELECT p.pack_code, p.recorded_at, p.card_id, p.shop_id, p.rarity_id,
         c.card_name, s.shop, r.rarity,
         p.min_price, p.min_price_any, p.code, p.url
  FROM public.price_history_pack p
  JOIN public.card_dim   c ON c.card_id   = p.card_id
  JOIN public.shop_dim   s ON s.shop_id   = p.shop_id
  JOIN public.rarity_dim r ON r.rarity_id = p.rarity_id;
GRANT SELECT ON public.price_history_pack_v TO anon, authenticated, service_role;

-- p_rows: [{card_name, shop, rarity, pack_code, recorded_at, min_price, min_price_any, code, url}, ...]
-- 同じ日の書き直しは上書き（新弾の収集は1日に何度も動き、そのたびに今の値で書き直す）。辞書の追加は upsert_price_rows と同じ
CREATE OR REPLACE FUNCTION public.upsert_price_pack_rows(p_rows jsonb)
RETURNS TABLE (saved_rows integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $function$
DECLARE
  n integer := 0;
BEGIN
  IF p_rows IS NULL OR jsonb_typeof(p_rows) <> 'array' THEN
    RAISE EXCEPTION 'upsert_price_pack_rows: p_rows must be a JSON array';
  END IF;

  DROP TABLE IF EXISTS _upsert_pack_rows;
  CREATE TEMP TABLE _upsert_pack_rows ON COMMIT DROP AS
    SELECT DISTINCT ON (pack_code, recorded_at, card_name, shop, rarity) *
    FROM (
      SELECT upper(x->>'pack_code')         AS pack_code,
             (x->>'recorded_at')::date      AS recorded_at,
             x->>'card_name'                AS card_name,
             x->>'shop'                     AS shop,
             x->>'rarity'                   AS rarity,
             (x->>'min_price')::integer     AS min_price,
             (x->>'min_price_any')::integer AS min_price_any,
             x->>'code'                     AS code,
             x->>'url'                      AS url,
             ord
      FROM jsonb_array_elements(p_rows) WITH ORDINALITY AS t(x, ord)
    ) s
    WHERE pack_code IS NOT NULL AND pack_code <> '' AND card_name IS NOT NULL AND shop IS NOT NULL
      AND rarity IS NOT NULL AND recorded_at IS NOT NULL AND min_price IS NOT NULL
    ORDER BY pack_code, recorded_at, card_name, shop, rarity, ord DESC;   -- 同一キーは後勝ち

  -- 辞書の追加。既存値で nextval を消費しないよう NOT EXISTS で絞る（price_history_v2.sql と同じ）
  INSERT INTO public.card_dim (card_name)
    SELECT DISTINCT u.card_name FROM _upsert_pack_rows u
    WHERE NOT EXISTS (SELECT 1 FROM public.card_dim d WHERE d.card_name = u.card_name)
    ON CONFLICT DO NOTHING;
  INSERT INTO public.shop_dim (shop)
    SELECT DISTINCT u.shop FROM _upsert_pack_rows u
    WHERE NOT EXISTS (SELECT 1 FROM public.shop_dim d WHERE d.shop = u.shop)
    ON CONFLICT DO NOTHING;
  INSERT INTO public.rarity_dim (rarity)
    SELECT DISTINCT u.rarity FROM _upsert_pack_rows u
    WHERE NOT EXISTS (SELECT 1 FROM public.rarity_dim d WHERE d.rarity = u.rarity)
    ON CONFLICT DO NOTHING;

  INSERT INTO public.price_history_pack (pack_code, recorded_at, card_id, shop_id, rarity_id, min_price, min_price_any, code, url)
  SELECT u.pack_code, u.recorded_at, c.card_id, s.shop_id, r.rarity_id, u.min_price, u.min_price_any, u.code, u.url
  FROM _upsert_pack_rows u
  JOIN public.card_dim c   ON c.card_name = u.card_name
  JOIN public.shop_dim s   ON s.shop = u.shop
  JOIN public.rarity_dim r ON r.rarity = u.rarity
  ON CONFLICT (pack_code, recorded_at, card_id, shop_id, rarity_id) DO UPDATE
    SET min_price = EXCLUDED.min_price,
        min_price_any = EXCLUDED.min_price_any,
        code = EXCLUDED.code,
        url = EXCLUDED.url;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN QUERY SELECT n;
END;
$function$;
REVOKE ALL ON FUNCTION public.upsert_price_pack_rows(jsonb) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.upsert_price_pack_rows(jsonb) TO service_role;
