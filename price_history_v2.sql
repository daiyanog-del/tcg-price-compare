-- price_history の整数キー化（2026-09-02 総点検 → Supabase 無料枠 500MB 超過への対応）
-- =============================================
-- 背景（本番実測 2026-09-02）:
--   DB 1,095MB のうち price_history が 1,006MB。本体 289MB に対し索引が 717MB（7割）。
--   card_name(平均30B)/shop(16B)/rarity(16B) の文字列キーを3本の索引に重複保持していたため。
--   35日より古い行を読む機能は現存しない（/api/price-history のみ90日読むが画面から未参照）。
-- 方針:
--   文字列キーを辞書表（card_dim / shop_dim / rarity_dim）の整数IDに置き換えた
--   price_history_v2 を正本にし、既存の読み取り11箇所とRPC（best_prices / movers）は同名ビュー
--   price_history（辞書表を結合して従来の列名で返す）で無変更のまま動かす。
--   例外は get_top_movers の max(recorded_at)：INNER JOIN のビュー経由だと MIN/MAX 最適化が
--   効かず209万行のフルスキャン（実測5.5秒）になるため price_history_v2 を直接読む。
--   書き込みは price_persist.upsert_price_rows → RPC upsert_price_rows(jsonb) に一本化。
--   削除（90日保持）は price_history_v2 を直接 delete する。
-- 実測（切替前・2026-09-02）: v2 本体 166MB + 主キー 88MB + 被覆索引 63MB = 318MB。
--   ビュー経由の get_card_best_prices 0.9秒（旧0.8秒）、featured_pack の1ページ 0.1秒。
-- 適用状況: 2026-09-02 に §1〜§6 すべて適用済み（§6 の DROP TABLE はユーザーが SQL Editor で実行）。削除後の DB 全体 433MB。
-- 適用手順（本番）: §1〜§3 を先に適用（無停止）→ §4a RPC作成＋疎通確認 → §4b 切替（1トランザクション・
--   差分再同期込み）→ アプリをデプロイ → §5 旧索引削除 → 翌朝の夜間収集を確認後 §6 旧テーブル削除。
-- 戻し方（§4b の後、§6 の前）: BEGIN; DROP VIEW price_history; ALTER TABLE price_history_old RENAME TO price_history;
--   COMMIT; のうえで、切替後に v2 にだけ入った行を旧テーブルへ upsert し直す（§4b の逆方向）。
--   §5 の後は索引の再作成が必要（supabase_rpc_top_movers.sql 末尾の索引定義を参照）。
-- 設計上の注意（レビュー班 2026-09-02）:
--   - ビューは security_invoker 無し（definer 相当）。公開読み取りデータなので意図的。Advisor の
--     security_definer_view 警告は既知として扱う
--   - shop_dim / rarity_dim は smallserial（上限 32,767）。既存値で nextval を消費しないよう
--     辞書追加は WHERE NOT EXISTS で絞る（ON CONFLICT だけだと1晩で数千消費して2週間で枯渇する）
--   - RPC の戻り値は 1行のテーブル（saved_rows）。スカラー返却だと supabase-py の APIResponse が
--     list 以外を弾いて HTTP 200 なのに例外になる（実測）

-- ============ §1 辞書表と新テーブル（本番適用済み 2026-09-02） ============
CREATE TABLE IF NOT EXISTS public.card_dim (
    card_id   serial PRIMARY KEY,
    card_name text NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS public.shop_dim (
    shop_id smallserial PRIMARY KEY,
    shop    text NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS public.rarity_dim (
    rarity_id smallserial PRIMARY KEY,
    rarity    text NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS public.price_history_v2 (
    card_id       integer  NOT NULL REFERENCES public.card_dim(card_id),
    shop_id       smallint NOT NULL REFERENCES public.shop_dim(shop_id),
    rarity_id     smallint NOT NULL REFERENCES public.rarity_dim(rarity_id),
    recorded_at   date     NOT NULL,          -- JST日付（従来どおり）
    min_price     integer  NOT NULL,          -- 通常品の最安（フェーズ3の定義）
    min_price_any integer,                    -- 状態を問わない最安
    code          text,                       -- 型番（店由来 or 補完）
    url           text,                       -- 商品ページURL（新弾マトリクスが参照）
    PRIMARY KEY (card_id, shop_id, rarity_id, recorded_at)
);
ALTER TABLE public.price_history_v2 SET (
    autovacuum_vacuum_insert_scale_factor = 0.02,
    autovacuum_vacuum_insert_threshold = 10000
);
ALTER TABLE public.card_dim         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.shop_dim         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.rarity_dim       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.price_history_v2 ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename='card_dim' AND policyname='allow_read_all') THEN
    CREATE POLICY allow_read_all ON public.card_dim FOR SELECT TO anon USING (true); END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename='shop_dim' AND policyname='allow_read_all') THEN
    CREATE POLICY allow_read_all ON public.shop_dim FOR SELECT TO anon USING (true); END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename='rarity_dim' AND policyname='allow_read_all') THEN
    CREATE POLICY allow_read_all ON public.rarity_dim FOR SELECT TO anon USING (true); END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename='price_history_v2' AND policyname='allow_read_all') THEN
    CREATE POLICY allow_read_all ON public.price_history_v2 FOR SELECT TO anon USING (true); END IF;
END $$;

-- ============ §2 現行データの流し込み（本番適用済み。日付範囲で5分割して実行した） ============
INSERT INTO public.card_dim (card_name) SELECT DISTINCT card_name FROM public.price_history ON CONFLICT DO NOTHING;
INSERT INTO public.shop_dim (shop)      SELECT DISTINCT shop      FROM public.price_history ON CONFLICT DO NOTHING;
INSERT INTO public.rarity_dim (rarity)  SELECT DISTINCT rarity    FROM public.price_history ON CONFLICT DO NOTHING;
INSERT INTO public.price_history_v2 (card_id, shop_id, rarity_id, recorded_at, min_price, min_price_any, code, url)
SELECT c.card_id, s.shop_id, r.rarity_id, p.recorded_at, p.min_price, p.min_price_any, p.code, p.url
FROM public.price_history p
JOIN public.card_dim   c ON c.card_name = p.card_name
JOIN public.shop_dim   s ON s.shop      = p.shop
JOIN public.rarity_dim r ON r.rarity    = p.rarity
ON CONFLICT DO NOTHING;

-- ============ §3 索引（本番適用済み。主キーに加えて日付先頭の被覆索引1本のみ） ============
CREATE INDEX IF NOT EXISTS idx_ph2_date_card_shop_rarity
    ON public.price_history_v2 (recorded_at, card_id, shop_id, rarity_id) INCLUDE (min_price);
ANALYZE public.price_history_v2;

-- ============ §4a 書き込みRPC（ビューに依存しないので切替前に作成し、Python から疎通確認する） ============
-- p_rows: [{card_name, shop, rarity, recorded_at, min_price, min_price_any, code, url}, ...]
-- p_ignore_duplicates=true は既存行を上書きしない（/api/deck の即時保存が夜間収集の
-- 網羅的な最安値を同日上書きしないための切替。price_persist の docstring 参照）
-- 戻り値: 1行 (saved_rows) = 実際に挿入/更新した行数。DO NOTHING で読み飛ばした行は数えない
-- 必須列が NULL の行は除外し、除外件数を WARNING で出す（無言で捨てない）
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

  -- 辞書の追加。既存値で nextval を消費しないよう NOT EXISTS で絞る（smallserial 枯渇対策）
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
  END IF;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN QUERY SELECT n;
END;
$function$;
-- SECURITY DEFINER の書き込み関数は service_role だけに限定する
-- （関数は作成時に暗黙で PUBLIC に EXECUTE が付くため、anon からの REVOKE では消えない）
REVOKE ALL ON FUNCTION public.upsert_price_rows(jsonb, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.upsert_price_rows(jsonb, boolean) TO service_role;

-- ============ §4b 切替（1トランザクション。§2 以降に旧テーブルへ入った行を再同期してから改名） ============
-- BEGIN;
-- LOCK TABLE public.price_history IN EXCLUSIVE MODE;   -- 書き込みを止めてから差分を取る（読み取りは可）
-- INSERT INTO public.card_dim (card_name)
--   SELECT DISTINCT p.card_name FROM public.price_history p WHERE p.recorded_at >= '<§2実行日-1>'
--   AND NOT EXISTS (SELECT 1 FROM public.card_dim d WHERE d.card_name = p.card_name) ON CONFLICT DO NOTHING;
-- （shop_dim / rarity_dim も同様）
-- INSERT INTO public.price_history_v2 (card_id, shop_id, rarity_id, recorded_at, min_price, min_price_any, code, url)
-- SELECT c.card_id, s.shop_id, r.rarity_id, p.recorded_at, p.min_price, p.min_price_any, p.code, p.url
-- FROM public.price_history p JOIN card_dim c ON c.card_name=p.card_name JOIN shop_dim s ON s.shop=p.shop
--   JOIN rarity_dim r ON r.rarity=p.rarity
-- WHERE p.recorded_at >= '<§2実行日-1>'
-- ON CONFLICT (card_id, shop_id, rarity_id, recorded_at) DO UPDATE
--   SET min_price = EXCLUDED.min_price, min_price_any = EXCLUDED.min_price_any, code = EXCLUDED.code, url = EXCLUDED.url;
-- ALTER TABLE public.price_history RENAME TO price_history_old;
-- CREATE VIEW public.price_history AS
--   SELECT p.card_id, p.shop_id, p.rarity_id,
--          c.card_name, s.shop, r.rarity,
--          p.min_price, p.recorded_at, p.min_price_any, p.code, p.url
--   FROM public.price_history_v2 p
--   JOIN public.card_dim   c ON c.card_id   = p.card_id
--   JOIN public.shop_dim   s ON s.shop_id   = p.shop_id
--   JOIN public.rarity_dim r ON r.rarity_id = p.rarity_id;
-- GRANT SELECT ON public.price_history TO anon, authenticated, service_role;
-- COMMIT;
-- ANALYZE public.price_history_v2;
-- 切替後: get_top_movers を supabase_rpc_top_movers.sql の最新版（max(recorded_at) を v2 直参照）で再作成する

-- ============ §5 旧テーブルの索引削除（切替とデプロイ確認後。容量 717MB を即時回収） ============
-- price_history_uniq は UNIQUE 制約なので DROP INDEX ではなく DROP CONSTRAINT
-- ALTER TABLE public.price_history_old DROP CONSTRAINT price_history_uniq;
-- DROP INDEX IF EXISTS idx_price_history_date_card_shop_rarity, idx_price_history_card_date_price,
--   idx_price_history_card_date, idx_price_history_date;
-- （price_history_pkey は旧テーブル本体と一緒に §6 で消える）

-- ============ §6 旧テーブル削除（翌朝の夜間収集を確認後） ============
-- DROP TABLE public.price_history_old;
