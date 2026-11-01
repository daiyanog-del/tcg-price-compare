-- お店ごとの1日の取得回数（shop_request_log.py・2026-11-01）
-- =============================================
-- まんぞく屋が本番の接続元を止めた（11-01）のは、機能ごとには正しい取得の合計を誰も見ていなかったのが根。
-- 出どころ（prices=毎朝の収集 / buyback=買取 / featured=新弾の収集 / web=サイトの検索）ごと・プロセスごとに
-- その日の累計を1行ずつ書く（何度書いても同じ upsert）。読むときに day・host で足し合わせる。
--
-- 書き込み元: collect_prices.py / collect_buyback.py / collect_featured.py（終了時）・app.py（30分ごと・終了時）。
-- service role キーだけで読み書きする。表が無くても収集・サイトは止まらない（ログに書き込み失敗が出る）。
-- 400日より古い行は毎朝の収集が消す。1日数十行。
--
-- Supabase 管理画面 > SQL Editor で実行してください。

CREATE TABLE IF NOT EXISTS shop_request_daily (
    day          DATE        NOT NULL,              -- JST の日付
    source       TEXT        NOT NULL,              -- prices / buyback / featured / web
    instance_id  TEXT        NOT NULL,              -- プロセス（PID-起動時刻）
    host         TEXT        NOT NULL,              -- 取得先のホスト（例 shopmanzokuya.com。カーナベルの検索は ES の …cloud.es.io:443 と www.ka-nabell.com の2つ）
    requests     INTEGER     NOT NULL DEFAULT 0,    -- 実際に送った回数（再試行も1回）
    skipped      INTEGER     NOT NULL DEFAULT 0,    -- ブレーカで送らずに飛ばした回数
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (day, source, instance_id, host)
);

-- 公開キー（anon）からは読めない・書けないようにする（service role は RLS を通らない）
ALTER TABLE shop_request_daily ENABLE ROW LEVEL SECURITY;

-- 例: 日ごと・ホストごとの合計
--   SELECT day, host, source, sum(requests) AS requests, sum(skipped) AS skipped
--     FROM shop_request_daily WHERE day >= current_date - 14
--    GROUP BY day, host, source ORDER BY day DESC, host, source;
