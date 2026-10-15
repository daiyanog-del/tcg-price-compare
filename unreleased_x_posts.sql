-- 未発売カードのX自動投稿 二重投稿防止テーブル
-- =============================================
-- 2026-10-15 追加。unreleased_x_post.py が投稿前に「予約」行を insert し、
-- 主キー（card_id）の衝突で二重投稿を防ぐ。
--
-- status:
--   posting          → 投稿処理中（予約済み）。1時間以上残った場合は回収ジョブが Discord に通知（投稿済みか X で確認して posted/削除）
--   posted           → 投稿済み（tweet_id に X の投稿ID）
--   failed           → 送信前の失敗 / X が明確に拒否(4xx)。attempts < 3 の間は回収ジョブが再試行する
--   rate_limited     → 429（レート制限）。attempts を増やさず、回収ジョブが再挑戦する
--   unknown          → 結果不明（応答なし・タイムアウト・5xx）。投稿されたかもしれないので自動再試行しない。
--                      Discord 通知。X を見て、投稿済みなら posted（tweet_id も）に、未投稿なら行を削除
--   skipped_duplicate→ 同じカード名で投稿済みのカードがあるため投稿しない
--   skipped_legacy   → 旧版が導入前の承認済みカードに付けた印。現行は approved_at で除外するので新規には作らない（既存行は残る）
--
-- 投稿対象は unreleased_cards.approved_at（承認時刻）が入っているカードだけ。
--   approved_at は単体承認・一括承認で status='approved' にする UPDATE と同時に入る。
--   このSQLで列を追加した時点の既存行は NULL のまま＝導入前の承認済みカードは投稿対象外（過去分が流れない）。
--   （旧版にあった skipped_legacy の一括 INSERT は不要になったため削除した。status 値と CHECK は既存行のため残す）
--   extraction_raw.approval_snapshot は計測用で、投稿条件ではない。
--
-- ★適用順序（本番）: ① このSQLを先に実行 → ② コードをデプロイ → ③ UNRELEASED_X_POST_ENABLED=1
--   コードを先にデプロイしても承認は壊れない（approved_at 列が無いエラーを検知したら approved_at 抜きで
--   承認を再実行して警告ログを出す）が、その間に承認したカードは approved_at が NULL のため投稿されない。
--   その場合は SQL 適用後に該当カードを unpublish → 再承認するか、approved_at を直接 UPDATE する。

-- Supabase 管理画面 > SQL Editor で実行してください（冪等）。

CREATE TABLE IF NOT EXISTS unreleased_x_posts (
    card_id     BIGINT      PRIMARY KEY REFERENCES unreleased_cards(id) ON DELETE CASCADE,
    status      TEXT        NOT NULL CHECK (status IN ('posting', 'posted', 'failed', 'rate_limited', 'unknown', 'skipped_duplicate', 'skipped_legacy')),
    tweet_id    TEXT,
    attempts    INTEGER     NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  unreleased_x_posts            IS '未発売カードのX自動投稿の状態。card_id 主キーで二重投稿を防ぐ（unreleased_x_post.py）';
COMMENT ON COLUMN unreleased_x_posts.card_id    IS 'unreleased_cards.id。主キー衝突＝他が処理中か投稿済み';
COMMENT ON COLUMN unreleased_x_posts.status     IS 'posting=処理中 / posted=投稿済み / failed=失敗（attempts<3で再試行）/ rate_limited=429（再挑戦）/ unknown=結果不明（自動再試行しない）/ skipped_duplicate=同名で投稿済み / skipped_legacy=導入前の承認済み';
COMMENT ON COLUMN unreleased_x_posts.tweet_id   IS '投稿成功時のXの投稿ID';
COMMENT ON COLUMN unreleased_x_posts.attempts   IS '失敗回数（失敗のたびに+1）';
COMMENT ON COLUMN unreleased_x_posts.last_error IS '直近の失敗理由';

-- RLSは既存テーブル（ygores_cache.sql / sync_accounts.sql）と同様に有効化のみ
-- （ポリシーなし＝anonアクセス遮断、サーバーはservice_role keyでアクセス）
ALTER TABLE unreleased_x_posts ENABLE ROW LEVEL SECURITY;

-- 既に旧版の CREATE TABLE を実行済みの場合に備え、status の CHECK 制約を作り直す（冪等）。
ALTER TABLE unreleased_x_posts DROP CONSTRAINT IF EXISTS unreleased_x_posts_status_check;
ALTER TABLE unreleased_x_posts ADD CONSTRAINT unreleased_x_posts_status_check
    CHECK (status IN ('posting', 'posted', 'failed', 'rate_limited', 'unknown', 'skipped_duplicate', 'skipped_legacy'));

-- 承認時刻の列（冪等）。既存行は NULL のまま。
ALTER TABLE unreleased_cards ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ NULL;
COMMENT ON COLUMN unreleased_cards.approved_at IS '承認した時刻。X自動投稿の対象判定に使う（NULL=導入前の承認済み or 未承認は投稿しない）';

-- 429 の再挑戦回数と、取り残し通知の最終時刻（冪等）
ALTER TABLE unreleased_x_posts ADD COLUMN IF NOT EXISTS rate_limit_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE unreleased_x_posts ADD COLUMN IF NOT EXISTS stuck_notified_at TIMESTAMPTZ NULL;
COMMENT ON COLUMN unreleased_x_posts.rate_limit_count   IS '429 に当たった累計回数。上限（初回から24時間 or 8回）を超えたら failed にする';
COMMENT ON COLUMN unreleased_x_posts.stuck_notified_at  IS 'posting/unknown の取り残し通知を最後に出した時刻（同じ行は1日1回まで）';
