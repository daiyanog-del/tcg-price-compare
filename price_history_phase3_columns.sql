-- price_history 列追加（フェーズ3 P4 + P2）
-- =============================================
-- 対象: docs/design-phase3-generation-side.md
--   「P4: 状態（キズあり・状態別・セール）混入」 推奨案B（2値記録・キー不変）
--   「P2: 型番(code)の破棄」                    推奨案B（属性列として保存・キー不変）
-- 同一マイグレーションに同乗させる理由: どちらも price_persist.build_min_price_rows の
-- 同じ箇所（(shop, rarity) 単位の min 集計）を触るため（design §3 ステップ4/5）。
--
-- 既存 min_price の意味は変わる（P4-(b)参照）:
--   旧: sold_out/10円以下を除いた (shop, rarity) の単純最安値（状態混合）
--   新: 状態を揃えた「通常品」の最安値（変動検出・グラフ用）
--   新設の min_price_any が状態を問わない本当の最安値（表示・簡易見積り用）を持つ。
--   通常品が1件も無い (shop, rarity) では min_price は min_price_any にフォールバックする
--   （price_persist.build_min_price_rows 参照。行を欠測させないための実装判断）。
--
-- 移行手順（design §4 に従う）:
--   1. 本SQLで列追加（NULL許容・デフォルトなし → 即時完了、ロックなし）
--   2. price_persist.py（書き込み側）をデプロイ
--   3. 切替日を decisions.md に記録
--   4. バックフィルは行わない（90日で自然入替。旧行の min_price_any は NULL のまま）
--   5. ロールバックはコードを旧版に戻すだけ（列は残しても無害）
--
-- UNIQUE(card_name, shop, rarity, recorded_at) 制約は変更しない
-- （P2-A/P4-A＝キー昇格は不採用。属性列として保存する推奨案Bのみ適用）。
--
-- Supabase 管理画面 > SQL Editor で実行してください。

ALTER TABLE price_history
    ADD COLUMN IF NOT EXISTS min_price_any INTEGER,
    ADD COLUMN IF NOT EXISTS code           TEXT;

COMMENT ON COLUMN price_history.min_price     IS 'フェーズ3以降: 状態を揃えた「通常品」の最安値（変動検出・グラフ用）。通常品が無い場合は min_price_any と同値（price_persist.build_min_price_rows のフォールバック）。フェーズ3以前の行は状態混合の単純最安値';
COMMENT ON COLUMN price_history.min_price_any IS 'フェーズ3 P2/P4で追加。状態（キズあり・状態別・セール）を問わない本当の最安値。NULLはフェーズ3切替前の旧行（読み出し規約: NULLはmin_priceと同値とみなす）';
COMMENT ON COLUMN price_history.code          IS 'フェーズ3 P2で追加。min_priceを決めた出品の型番（店舗ごとに表記形式が異なるため店舗横断キーとしては使えない属性・監査情報）。NULLはフェーズ3切替前の旧行';
