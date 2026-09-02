-- buyback_history に UNIQUE 制約を追加する（2026-09-02 総点検）
-- =============================================
-- 背景:
--   collect_buyback.py:collect_and_save_buyback は元々 sb.table("buyback_history").insert(rows)
--   で書き込んでおり、UNIQUE 制約が無かったため再実行のたびに重複行が溜まっていた。
--   2026-06-16〜17 の Render 移行時の再実行が主因で、本番に 2,664 行の重複が蓄積しているのを
--   確認（本日削除済み）。
--   合わせて collect_buyback.py / price_persist.py 側を upsert_buyback_rows
--   （on_conflict="card_name,shop,rarity,recorded_at" の last-write-wins）に揃え、
--   販売側の price_history と同じ冪等な書き込み経路にした。
-- 適用: Supabase の SQL Editor で実行（本番適用は 2026-09-02 に MCP 経由で実施済み）。
-- 戻し方: DROP INDEX IF EXISTS public.buyback_history_uniq;

CREATE UNIQUE INDEX IF NOT EXISTS buyback_history_uniq
    ON public.buyback_history (card_name, shop, rarity, recorded_at);
