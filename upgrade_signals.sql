-- 有料プランへ上げる合図の見張り（upgrade_signals.py）の1日の集計
-- =============================================
-- 見張りはサーバーの中で数えるが、再起動で消えるので、プロセスごとに1日1行を書く。
-- 書くのは30分に1回と日付が変わったとき（05〜10時 JST は書かない）。行は90日で消す。
-- 前日のまとめは JST 0:10 以降に、その日の全行を足して判定する。
-- upgrade_signal_summaries は「その日のまとめを決めた」印（複数プロセス・再起動で二重に送らないため）。
--
-- 書き込み元: app.py（web サービスのみ・service role キー）。テーブルが無くても見張りは止まらず、
-- すぐ知らせる通知だけ動く（ログに書き込み失敗が出る）。
--
-- Supabase 管理画面 > SQL Editor で実行してください。

CREATE TABLE IF NOT EXISTS upgrade_signal_daily (
    day          DATE        NOT NULL,              -- JST の日付
    instance_id  TEXT        NOT NULL,              -- プロセス（PID-起動時刻）
    stats        JSONB       NOT NULL,              -- DailyStats.to_json()
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (day, instance_id)
);

CREATE TABLE IF NOT EXISTS upgrade_signal_summaries (
    day          DATE        PRIMARY KEY,           -- まとめた日（JST）
    signals      INTEGER     NOT NULL,              -- 合図を超えた項目の数（0 なら送っていない）
    instance_id  TEXT,
    decided_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 公開キー（anon）からは読めない・書けないようにする（service role は RLS を通らない）
ALTER TABLE upgrade_signal_daily ENABLE ROW LEVEL SECURITY;
ALTER TABLE upgrade_signal_summaries ENABLE ROW LEVEL SECURITY;

-- DB 全体の大きさ（バイト）。見張りの1日のまとめで1回だけ呼ぶ（2026-10-31・無料枠 500MB の合図）。
-- pg_database_size はファイルの大きさを見るだけで、表を読まない（朝の収集中でも重くない）。
-- 呼べるのは service role だけ（公開キーから DB の大きさを見せない）。
CREATE OR REPLACE FUNCTION public.db_size_bytes()
RETURNS bigint
LANGUAGE sql
STABLE
SET search_path = ''
AS $$ SELECT pg_catalog.pg_database_size(pg_catalog.current_database()) $$;

REVOKE ALL ON FUNCTION public.db_size_bytes() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.db_size_bytes() TO service_role;
