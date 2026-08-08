-- price_history 列追加（新弾フィーチャー 店舗×レアリティ価格マトリクス用）
-- =============================================
-- 対象: 「新弾発売日の店舗×レアリティ価格マトリクス」機能（2026-08-08）
-- price_persist.build_min_price_rows が min_price_any（状態を問わない本当の最安値）を
-- 決めた出品のURLをここに保存し、/featured のマトリクス表示から購入ページへ直接
-- 遷移できるようにする。
--
-- code 列（フェーズ3 P2、price_history_phase3_columns.sql）との役割分担は非対称:
--   code: min_price（通常品の最安値）側の出品の型番
--   url : min_price_any（本当の最安値）側の出品のURL
-- 通常品とany品が別出品の場合、code と url は別の出品を指すことになる（意図どおり。
-- 詳細は price_persist.build_min_price_rows のdocstring参照）。
--
-- 移行手順:
--   1. 本SQLで列追加（NULL許容・デフォルトなし → 即時完了、ロックなし）
--   2. price_persist.py（書き込み側）をデプロイ
--   3. バックフィルは行わない（90日で自然入替。旧行の url は NULL のまま）
--   4. ロールバックはコードを旧版に戻すだけ（列は残しても無害）
--
-- UNIQUE(card_name, shop, rarity, recorded_at) 制約は変更しない。
--
-- Supabase 管理画面 > SQL Editor で実行してください。
-- ※ 2026-08-08 時点で本番適用済み（列の存在確認済み）。本ファイルは体裁を
--   price_history_phase3_columns.sql に合わせて事後追加した記録用。

ALTER TABLE price_history
    ADD COLUMN IF NOT EXISTS url TEXT;

COMMENT ON COLUMN price_history.url IS 'min_price_any を決めた出品のURL（購入導線用）。code は min_price 側の型番という非対称は意図（price_persist.build_min_price_rows 参照）。NULLは出品にURLが無い、または本列追加前の旧行';
