-- price_history の autovacuum を挿入量ベースで早めに回す（2026-09-02 総点検 A-3）
-- 2026-09-02 追記: 同日中に price_history_v2 へ移行（price_history_v2.sql）。この設定は旧テーブル用で、
-- 旧テーブルは同 §6 で削除される。v2 側には price_history_v2.sql の §1 で同じ設定を入れている。
-- =============================================
-- 背景（本番実測）:
--   price_history は毎晩約35,000行を追記するだけで UPDATE/DELETE はほぼ無い（90日保持の削除のみ）。
--   PostgreSQL の autovacuum は既定では「挿入 1,000行 + テーブルの20%」（本テーブルでは約42万行）
--   で初めて走るため、約11日ごとにしか走らず、その間に追記されたページは可視性マップ
--   （visibility map）が未設定のままになる。
--   その結果、get_card_best_prices の Index Only Scan が「Heap Fetches: 199,204」
--   （27万行中）を起こし、実行が 6.9秒に達していた（VACUUM 直後は Heap Fetches 6,000・2.1秒）。
-- 対策:
--   挿入ベースの autovacuum 閾値を「10,000行 + 2%（約42,000行）」に下げ、毎晩の収集後に
--   自動で VACUUM が走るようにする。VACUUM は可視性マップを更新するだけで、行の削除や
--   ロックの長期保持は無い（ShareUpdateExclusive。通常の読み書きは妨げない）。
-- 適用: Supabase の SQL Editor で実行（2026-09-02 に MCP 経由で本番適用済み）。
-- 戻し方: ALTER TABLE public.price_history RESET (autovacuum_vacuum_insert_scale_factor, autovacuum_vacuum_insert_threshold);

ALTER TABLE public.price_history SET (
    autovacuum_vacuum_insert_scale_factor = 0.02,
    autovacuum_vacuum_insert_threshold = 10000
);
