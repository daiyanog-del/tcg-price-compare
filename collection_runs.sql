-- 収集ラン記録テーブル
-- =============================================
-- フェーズ3 P5（docs/design-phase3-generation-side.md 「P5: 観測店舗集合の非記録」）。
-- 「未観測」と「在庫0」を区別するため、収集の1実行（ラン）につき1行を記録する。
-- price_history / buyback_history 側は無変更・完全可逆（追加のみ）。
--
-- 用途:
--   その日その店舗は収集を試行したのか（attempted_shops）、実際に使えたのか
--   （available_shops）、ヘルスチェック失敗やAPIキー未設定でスキップしたのは
--   どの店舗でなぜか（skipped_shops）を復元できるようにする。
--   今後の P1〜P4 の効果測定・ガードのチューニング・閾値再校正の検証土台。
--
-- 書き込み元: collect_prices.py（run_type='prices'）/ collect_buyback.py（run_type='buyback'）
-- 書き込み失敗（テーブル未作成含む）は収集本体を止めない（collection_run.py 参照）。
--
-- Supabase 管理画面 > SQL Editor で実行してください。

CREATE TABLE IF NOT EXISTS collection_runs (
    id               BIGSERIAL   PRIMARY KEY,

    -- ラン種別
    run_type         TEXT        NOT NULL CHECK (run_type IN ('prices', 'buyback', 'instant')),

    -- 実行区間
    started_at       TIMESTAMPTZ NOT NULL,              -- ラン開始日時
    finished_at      TIMESTAMPTZ,                       -- ラン終了日時（NULL=異常終了で未記録）

    -- 観測店舗集合（「未観測」と「在庫0」を区別するための本体）
    attempted_shops  TEXT[]      NOT NULL DEFAULT '{}', -- 今回の実行で問い合わせ対象とした店舗
    available_shops  TEXT[]      NOT NULL DEFAULT '{}', -- ヘルスチェック等を通過し実際に収集に使った店舗
    skipped_shops    JSONB       NOT NULL DEFAULT '{}'::jsonb, -- {店舗名: スキップ理由}（例: health_check_failed / api_key_missing）

    -- 店舗別集計（カード単位の 取得成功/0件/エラー を店舗別に積算したもの）
    shop_stats       JSONB       NOT NULL DEFAULT '{}'::jsonb, -- {店舗名: {"ok": n, "empty": n, "error": n}}

    -- 収集結果サマリ
    success_count    INTEGER     NOT NULL DEFAULT 0,    -- 保存できたカード数
    fail_count       INTEGER     NOT NULL DEFAULT 0,    -- 保存できなかったカード数
    cutoff_count     INTEGER     NOT NULL DEFAULT 0,    -- 時間予算超過で打ち切った周辺カード数
    saved_rows       INTEGER     NOT NULL DEFAULT 0,    -- price_history / buyback_history への保存行数

    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  collection_runs                  IS '収集の1実行（ラン）につき1行。観測店舗集合を記録し「未観測」と「在庫0」を区別する（フェーズ3 P5）';
COMMENT ON COLUMN collection_runs.run_type         IS 'ラン種別: prices=夜間販売価格収集 / buyback=夜間買取価格収集 / instant=ユーザー検索由来の即時反映';
COMMENT ON COLUMN collection_runs.started_at       IS 'ラン開始日時';
COMMENT ON COLUMN collection_runs.finished_at      IS 'ラン終了日時。NULLは異常終了等で未記録';
COMMENT ON COLUMN collection_runs.attempted_shops  IS '今回の実行で問い合わせ対象とした店舗一覧（ヘルスチェック前の候補集合）';
COMMENT ON COLUMN collection_runs.available_shops  IS 'ヘルスチェック・APIキー確認を通過し実際に収集に使った店舗一覧';
COMMENT ON COLUMN collection_runs.skipped_shops    IS 'スキップした店舗と理由（例: {"カーナベル": "api_key_missing"}）';
COMMENT ON COLUMN collection_runs.shop_stats       IS 'カード単位の取得成功/0件/エラーを店舗別に積算したもの（JSONB）';
COMMENT ON COLUMN collection_runs.success_count    IS '保存できたカード数';
COMMENT ON COLUMN collection_runs.fail_count       IS '保存できなかったカード数';
COMMENT ON COLUMN collection_runs.cutoff_count     IS '時間予算超過で打ち切った周辺カード数（collect_prices.py のみ使用。他は常に0）';
COMMENT ON COLUMN collection_runs.saved_rows       IS 'price_history / buyback_history への保存行数の合計';
COMMENT ON COLUMN collection_runs.created_at       IS '行の作成日時（DB書き込み時刻）';

-- 検証クエリ（ラン種別・期間での絞り込み）を高速化
CREATE INDEX IF NOT EXISTS idx_collection_runs_run_type_started
    ON collection_runs (run_type, started_at DESC);
