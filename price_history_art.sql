-- =============================================
-- 絵ごとの内訳の表（2026-10-30 イラスト違いの段2・案B の段2a-1）
-- =============================================
-- 背景: 価格の記録（price_history_v2・buyback_history）はカード×店×レアリティの単位で、同じレアリティの中の
--   元のイラスト／イラスト違い／漫画／GREEN／言語版の値段が混ざる（本表の 0.75% は最安の型番が違う絵の版。10-30 実測）。
-- 方針（docs/design-art-variant-stage2-2026-10-30.md・ユーザー選択 案B）:
--   本表はそのまま。絵が分かれるカード×レアリティ（art_variant.needs_art_rows）だけ、店×レアリティ×絵ごとの値を
--   この表に書く（元のイラスト・判別できない行も含めて全部）。30日を過ぎた行は price_daily_art（全店最安・100日）へ畳む。
--   段2a-2 で本表を「代表の絵」に切り替えるときは、この表がある日の本表の行を書き直す。
-- 見積もり: price_history_art 1日2,000行以下×30日＝約13MB・price_daily_art 約6MB・buyback_art 約3.5MB（多めに置いた値）
-- 書き手: price_persist.upsert_price_art_rows／upsert_buyback_art_rows（RPC）。読み手は段2b 以降（価格推移・購入候補・買取の欄）
-- 適用順: このファイルを先に適用 → コードをデプロイ（コードが先でも、RPC が無いとプロセスごとに1回警告して書かないだけ）
-- 戻し方: コードを戻す → DROP FUNCTION 3つ → DROP TABLE price_daily_art, price_history_art, buyback_art, art_dim

-- ============ 辞書（rarity_dim と同じ作り） ============
CREATE TABLE IF NOT EXISTS public.art_dim (
    art_id smallserial PRIMARY KEY,
    art    text NOT NULL UNIQUE          -- "元のイラスト" / "イラスト違い" / "イラスト違い#4" / "漫画カラー" / "アジア版" / "(判別できない)" など
);
ALTER TABLE public.art_dim ENABLE ROW LEVEL SECURITY;  -- ポリシーなし＝anon からは読めない（読み出しの口を作る段2b で決める）

-- ============ 販売（店舗別・30日） ============
CREATE TABLE IF NOT EXISTS public.price_history_art (
    card_id       integer  NOT NULL REFERENCES public.card_dim(card_id),
    rarity_id     smallint NOT NULL REFERENCES public.rarity_dim(rarity_id),
    art_id        smallint NOT NULL REFERENCES public.art_dim(art_id),
    shop_id       smallint NOT NULL REFERENCES public.shop_dim(shop_id),
    recorded_at   date     NOT NULL,          -- JST日付（price_history_v2 と同じ）
    min_price     integer  NOT NULL,          -- 通常品の最安（price_history_v2 と同じ定義）
    min_price_any integer,                    -- 状態を問わない最安
    code          text,                       -- min_price を決めた出品の型番
    url           text,                       -- min_price_any を決めた出品の URL
    PRIMARY KEY (card_id, rarity_id, art_id, shop_id, recorded_at)  -- 読み手はカード単位で引く
);
ALTER TABLE public.price_history_art ENABLE ROW LEVEL SECURITY;
-- 毎晩の30日超の畳み込み・削除用（主キーは card_id 先頭で日付の範囲を引けない）。日付順に入るので BRIN で足りる
CREATE INDEX IF NOT EXISTS idx_price_history_art_date ON public.price_history_art USING brin (recorded_at);

-- ============ 販売（日ごとの全店最安・100日。price_daily_min と同じ形に art_id を足す） ============
CREATE TABLE IF NOT EXISTS public.price_daily_art (
    card_id          integer  NOT NULL REFERENCES public.card_dim(card_id),
    rarity_id        smallint NOT NULL REFERENCES public.rarity_dim(rarity_id),
    art_id           smallint NOT NULL REFERENCES public.art_dim(art_id),
    recorded_at      date     NOT NULL,
    min_price        integer  NOT NULL,
    min_shop_ids     smallint[] NOT NULL,
    min_price_over10 integer,                 -- 10円未満を除いた全店最安（price_daily_min と同じ名前のまま）
    PRIMARY KEY (card_id, rarity_id, art_id, recorded_at)
);
ALTER TABLE public.price_daily_art ENABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_price_daily_art_date ON public.price_daily_art USING brin (recorded_at);

-- ============ 買取（店舗別・30日） ============
CREATE TABLE IF NOT EXISTS public.buyback_art (
    card_id     integer  NOT NULL REFERENCES public.card_dim(card_id),
    rarity_id   smallint NOT NULL REFERENCES public.rarity_dim(rarity_id),
    art_id      smallint NOT NULL REFERENCES public.art_dim(art_id),
    shop_id     smallint NOT NULL REFERENCES public.shop_dim(shop_id),
    recorded_at date     NOT NULL,
    max_price   integer  NOT NULL,            -- 未開封・付属品つきを除いた最高額（buyback_history と同じ定義）
    PRIMARY KEY (card_id, rarity_id, art_id, shop_id, recorded_at)
);
ALTER TABLE public.buyback_art ENABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_buyback_art_date ON public.buyback_art USING brin (recorded_at);

-- ============ 書き込み（販売） ============
-- p_rows: [{card_name, shop, rarity, art, recorded_at, min_price, min_price_any, code, url}, ...]
-- 辞書の追加と上書きの決まりは upsert_price_rows と同じ（p_ignore_duplicates=true は既存行を上書きしない）
CREATE OR REPLACE FUNCTION public.upsert_price_art_rows(p_rows jsonb, p_ignore_duplicates boolean DEFAULT false)
RETURNS TABLE (saved_rows integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $function$
DECLARE
  n integer := 0;
BEGIN
  IF p_rows IS NULL OR jsonb_typeof(p_rows) <> 'array' THEN
    RAISE EXCEPTION 'upsert_price_art_rows: p_rows must be a JSON array';
  END IF;

  DROP TABLE IF EXISTS _upsert_art_rows;
  CREATE TEMP TABLE _upsert_art_rows ON COMMIT DROP AS
    SELECT DISTINCT ON (card_name, shop, rarity, art, recorded_at) *
    FROM (
      SELECT x->>'card_name'                AS card_name,
             x->>'shop'                     AS shop,
             x->>'rarity'                   AS rarity,
             x->>'art'                      AS art,
             (x->>'recorded_at')::date      AS recorded_at,
             (x->>'min_price')::integer     AS min_price,
             (x->>'min_price_any')::integer AS min_price_any,
             x->>'code'                     AS code,
             x->>'url'                      AS url,
             ord
      FROM jsonb_array_elements(p_rows) WITH ORDINALITY AS t(x, ord)
    ) s
    WHERE card_name IS NOT NULL AND shop IS NOT NULL AND rarity IS NOT NULL AND art IS NOT NULL
      AND recorded_at IS NOT NULL AND min_price IS NOT NULL
    ORDER BY card_name, shop, rarity, art, recorded_at, ord DESC;   -- 同一キーは後勝ち

  -- 辞書の追加。既存値で nextval を消費しないよう NOT EXISTS で絞る（smallserial 枯渇対策・price_history_v2.sql と同じ）
  INSERT INTO public.card_dim (card_name)
    SELECT DISTINCT u.card_name FROM _upsert_art_rows u
    WHERE NOT EXISTS (SELECT 1 FROM public.card_dim d WHERE d.card_name = u.card_name)
    ON CONFLICT DO NOTHING;
  INSERT INTO public.shop_dim (shop)
    SELECT DISTINCT u.shop FROM _upsert_art_rows u
    WHERE NOT EXISTS (SELECT 1 FROM public.shop_dim d WHERE d.shop = u.shop)
    ON CONFLICT DO NOTHING;
  INSERT INTO public.rarity_dim (rarity)
    SELECT DISTINCT u.rarity FROM _upsert_art_rows u
    WHERE NOT EXISTS (SELECT 1 FROM public.rarity_dim d WHERE d.rarity = u.rarity)
    ON CONFLICT DO NOTHING;
  INSERT INTO public.art_dim (art)
    SELECT DISTINCT u.art FROM _upsert_art_rows u
    WHERE NOT EXISTS (SELECT 1 FROM public.art_dim d WHERE d.art = u.art)
    ON CONFLICT DO NOTHING;

  IF p_ignore_duplicates THEN
    INSERT INTO public.price_history_art (card_id, rarity_id, art_id, shop_id, recorded_at, min_price, min_price_any, code, url)
    SELECT c.card_id, r.rarity_id, a.art_id, s.shop_id, u.recorded_at, u.min_price, u.min_price_any, u.code, u.url
    FROM _upsert_art_rows u
    JOIN public.card_dim c   ON c.card_name = u.card_name
    JOIN public.shop_dim s   ON s.shop = u.shop
    JOIN public.rarity_dim r ON r.rarity = u.rarity
    JOIN public.art_dim a    ON a.art = u.art
    ON CONFLICT DO NOTHING;
  ELSE
    INSERT INTO public.price_history_art (card_id, rarity_id, art_id, shop_id, recorded_at, min_price, min_price_any, code, url)
    SELECT c.card_id, r.rarity_id, a.art_id, s.shop_id, u.recorded_at, u.min_price, u.min_price_any, u.code, u.url
    FROM _upsert_art_rows u
    JOIN public.card_dim c   ON c.card_name = u.card_name
    JOIN public.shop_dim s   ON s.shop = u.shop
    JOIN public.rarity_dim r ON r.rarity = u.rarity
    JOIN public.art_dim a    ON a.art = u.art
    ON CONFLICT (card_id, rarity_id, art_id, shop_id, recorded_at) DO UPDATE
      SET min_price = EXCLUDED.min_price,
          min_price_any = EXCLUDED.min_price_any,
          code = EXCLUDED.code,
          url = EXCLUDED.url;
  END IF;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN QUERY SELECT n;
END;
$function$;
REVOKE ALL ON FUNCTION public.upsert_price_art_rows(jsonb, boolean) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.upsert_price_art_rows(jsonb, boolean) TO service_role;

-- ============ 書き込み（買取） ============
-- p_rows: [{card_name, shop, rarity, art, recorded_at, max_price}, ...]。同じ日の再実行は上書き（buyback_history と同じ）
CREATE OR REPLACE FUNCTION public.upsert_buyback_art_rows(p_rows jsonb)
RETURNS TABLE (saved_rows integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $function$
DECLARE
  n integer := 0;
BEGIN
  IF p_rows IS NULL OR jsonb_typeof(p_rows) <> 'array' THEN
    RAISE EXCEPTION 'upsert_buyback_art_rows: p_rows must be a JSON array';
  END IF;

  DROP TABLE IF EXISTS _upsert_bb_art_rows;
  CREATE TEMP TABLE _upsert_bb_art_rows ON COMMIT DROP AS
    SELECT DISTINCT ON (card_name, shop, rarity, art, recorded_at) *
    FROM (
      SELECT x->>'card_name'            AS card_name,
             x->>'shop'                 AS shop,
             x->>'rarity'               AS rarity,
             x->>'art'                  AS art,
             (x->>'recorded_at')::date  AS recorded_at,
             (x->>'max_price')::integer AS max_price,
             ord
      FROM jsonb_array_elements(p_rows) WITH ORDINALITY AS t(x, ord)
    ) s
    WHERE card_name IS NOT NULL AND shop IS NOT NULL AND rarity IS NOT NULL AND art IS NOT NULL
      AND recorded_at IS NOT NULL AND max_price IS NOT NULL
    ORDER BY card_name, shop, rarity, art, recorded_at, ord DESC;

  INSERT INTO public.card_dim (card_name)
    SELECT DISTINCT u.card_name FROM _upsert_bb_art_rows u
    WHERE NOT EXISTS (SELECT 1 FROM public.card_dim d WHERE d.card_name = u.card_name)
    ON CONFLICT DO NOTHING;
  INSERT INTO public.shop_dim (shop)
    SELECT DISTINCT u.shop FROM _upsert_bb_art_rows u
    WHERE NOT EXISTS (SELECT 1 FROM public.shop_dim d WHERE d.shop = u.shop)
    ON CONFLICT DO NOTHING;
  INSERT INTO public.rarity_dim (rarity)
    SELECT DISTINCT u.rarity FROM _upsert_bb_art_rows u
    WHERE NOT EXISTS (SELECT 1 FROM public.rarity_dim d WHERE d.rarity = u.rarity)
    ON CONFLICT DO NOTHING;
  INSERT INTO public.art_dim (art)
    SELECT DISTINCT u.art FROM _upsert_bb_art_rows u
    WHERE NOT EXISTS (SELECT 1 FROM public.art_dim d WHERE d.art = u.art)
    ON CONFLICT DO NOTHING;

  INSERT INTO public.buyback_art (card_id, rarity_id, art_id, shop_id, recorded_at, max_price)
  SELECT c.card_id, r.rarity_id, a.art_id, s.shop_id, u.recorded_at, u.max_price
  FROM _upsert_bb_art_rows u
  JOIN public.card_dim c   ON c.card_name = u.card_name
  JOIN public.shop_dim s   ON s.shop = u.shop
  JOIN public.rarity_dim r ON r.rarity = u.rarity
  JOIN public.art_dim a    ON a.art = u.art
  ON CONFLICT (card_id, rarity_id, art_id, shop_id, recorded_at) DO UPDATE
    SET max_price = EXCLUDED.max_price;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN QUERY SELECT n;
END;
$function$;
REVOKE ALL ON FUNCTION public.upsert_buyback_art_rows(jsonb) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.upsert_buyback_art_rows(jsonb) TO service_role;

-- ============ 畳み込み（30日を過ぎた販売の行 → price_daily_art。rollup_price_daily と同じ定義に art_id を足す） ============
-- 夜間は collect_prices.rollup_and_delete_old_art_prices が1日ずつ呼び、成功した日だけ price_history_art から消す
CREATE OR REPLACE FUNCTION public.rollup_price_daily_art(p_before date, p_from date DEFAULT NULL)
RETURNS TABLE(rolled_rows bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
SET statement_timeout = '16s'
AS $$
DECLARE
  n bigint;
BEGIN
  INSERT INTO public.price_daily_art
    (card_id, rarity_id, art_id, recorded_at, min_price, min_shop_ids, min_price_over10)
  SELECT card_id, rarity_id, art_id, recorded_at,
         min(day_min),
         array_agg(DISTINCT shop_id ORDER BY shop_id) FILTER (WHERE min_price = day_min),
         min(min_price) FILTER (WHERE min_price >= 10)
  FROM (
    SELECT card_id, rarity_id, art_id, recorded_at, shop_id, min_price,
           min(min_price) OVER (PARTITION BY card_id, rarity_id, art_id, recorded_at) AS day_min
    FROM public.price_history_art
    WHERE recorded_at < p_before
      AND (p_from IS NULL OR recorded_at >= p_from)
      AND min_price > 0
  ) src
  GROUP BY card_id, rarity_id, art_id, recorded_at
  ON CONFLICT (card_id, rarity_id, art_id, recorded_at) DO UPDATE
    SET min_price        = EXCLUDED.min_price,
        min_shop_ids     = EXCLUDED.min_shop_ids,
        min_price_over10 = EXCLUDED.min_price_over10;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN QUERY SELECT n;
END;
$$;
REVOKE ALL ON FUNCTION public.rollup_price_daily_art(date, date) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rollup_price_daily_art(date, date) TO service_role;

-- ============ 本表の書き込み関数の差し替え（同じ日の上書きで、もう出ない絵の行を別表に残さない） ============
-- ⚠ 2026-11-01 から本番の定義は price_upsert_skip_same.sql（値が同じ行は書き直さない・戻り値に changed_rows）。下は戻し方の元
-- 10-30 reviewer 指摘: 夜に「イラスト違い 1,500円」と「元のイラスト 800円」を書いたあと、昼の検索で違う絵が売り切れていると、
--   別表の「元のイラスト」だけが上書きされ、夜の違う絵の行がその日の値として残る（検索にカーナベルが無く「分かれない」と
--   判定されたときは別表に1行も書かれず、夜の行が全部残る）。
-- 本表を上書きする（p_ignore_duplicates=false）ときは、同じ (カード, 店, レアリティ, 日) の別表の行を消す。別表の行は
--   この直後に price_persist が upsert_price_art_rows で書き直す（どの書き込み口も本表→別表の順）。
-- 中身は price_history_v2.sql §4a の定義（2026-10-30 本番の pg_get_functiondef と同じ）に DELETE を1つ足しただけ。
--   p_ignore_duplicates=true（既存を上書きしない）のときは本表も別表も触らない。
CREATE OR REPLACE FUNCTION public.upsert_price_rows(p_rows jsonb, p_ignore_duplicates boolean DEFAULT false)
RETURNS TABLE (saved_rows integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $function$
DECLARE
  n        integer := 0;
  n_in     integer := 0;
  n_valid  integer := 0;
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
          url = EXCLUDED.url;
    GET DIAGNOSTICS n = ROW_COUNT;
    -- 同じキーの絵ごとの内訳を消す（このあと別表の書き込みで今の値を入れ直す）
    DELETE FROM public.price_history_art a
    USING _upsert_rows u
    JOIN public.card_dim c ON c.card_name = u.card_name
    JOIN public.shop_dim s ON s.shop = u.shop
    JOIN public.rarity_dim r ON r.rarity = u.rarity
    WHERE a.card_id = c.card_id AND a.rarity_id = r.rarity_id AND a.shop_id = s.shop_id
      AND a.recorded_at = u.recorded_at;
  END IF;
  RETURN QUERY SELECT n;
END;
$function$;
REVOKE ALL ON FUNCTION public.upsert_price_rows(jsonb, boolean) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.upsert_price_rows(jsonb, boolean) TO service_role;
