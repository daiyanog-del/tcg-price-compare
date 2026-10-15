-- 自動承認の開始に伴う DB 変更（冪等）
-- ======================================
-- 2026-10-15 追加。Supabase 管理画面 > SQL Editor で実行してください。何度実行しても安全です。
--
-- ★適用順序（本番）: ① このSQLを先に実行 → ② コードをデプロイ → ③ 環境変数（AUTO_APPROVE_ENABLED=1 は最後）
--   コードを先にデプロイしても壊れないように作ってある:
--     - 誤り報告テーブルが無い間は、報告の受付は500・管理画面の件数は0として動く（警告ログ）
--     - delete_failed を書く UPDATE は CHECK 制約違反になるが、削除失敗の記録に失敗するだけで承認・非公開は成立する
--
-- 含むもの:
--   1. unreleased_text_reports  ユーザーからの「テキストの誤り報告」（unreleased_text_report.py）
--   2. unreleased_x_posts.status に 'delete_failed' を追加（非公開時の X 投稿削除に失敗した行の印）

-- ──────────────────────────────────────────────
-- 1. 誤り報告
-- ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS unreleased_text_reports (
    id                  BIGSERIAL   PRIMARY KEY,
    unreleased_card_id  BIGINT      NOT NULL REFERENCES unreleased_cards(id) ON DELETE CASCADE,
    card_name           TEXT        NOT NULL,
    field               TEXT        NOT NULL CHECK (field IN ('effect_text', 'pendulum_effect', 'card_name', 'stats', 'other')),
    note                TEXT        NOT NULL DEFAULT '' CHECK (char_length(note) <= 500),
    ip_hash             TEXT        NOT NULL,
    status              TEXT        NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at         TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS unreleased_text_reports_card_status_idx
    ON unreleased_text_reports (unreleased_card_id, status);

COMMENT ON TABLE  unreleased_text_reports            IS '未発売カードのテキスト誤り報告（利用者から）。報告だけではカードを非公開にしない';
COMMENT ON COLUMN unreleased_text_reports.field      IS '誤りのある欄: effect_text / pendulum_effect / card_name / stats / other';
COMMENT ON COLUMN unreleased_text_reports.ip_hash    IS '生IPは保存しない。ソルト付き SHA-256（unreleased_text_report.hash_ip）';
COMMENT ON COLUMN unreleased_text_reports.status     IS 'open=未対応 / resolved=対応済み（管理画面の「対応済みにする」）';

-- RLS は既存テーブルと同様に有効化のみ（ポリシーなし＝anon アクセス遮断、サーバーは service_role key）
ALTER TABLE unreleased_text_reports ENABLE ROW LEVEL SECURITY;

-- ──────────────────────────────────────────────
-- 2. X 投稿の削除失敗の印（unreleased_x_posts）は機能3のコミットで追記
-- ──────────────────────────────────────────────
