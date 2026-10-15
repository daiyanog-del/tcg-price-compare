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
--   skipped_legacy   → 導入前から承認済みだったカード。過去分を流さないための印（保険）
--
-- 投稿対象は extraction_raw.approval_snapshot があるカード（＝このデプロイ以降に承認されたカード）に限られる。
-- そのため SQL の実行と有効化の間にずれがあっても過去分は流れない。skipped_legacy の INSERT は二重の保険。
--
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

-- 導入前から承認済み/紐付け済みのカードを全件 skipped_legacy で登録し、過去分が流れないようにする。
-- 冪等（既にあれば何もしない）。有効化（UNRELEASED_X_POST_ENABLED=1）より前に必ず実行すること。
INSERT INTO unreleased_x_posts (card_id, status)
SELECT id, 'skipped_legacy'
FROM unreleased_cards
WHERE status IN ('approved', 'linked')
ON CONFLICT (card_id) DO NOTHING;
