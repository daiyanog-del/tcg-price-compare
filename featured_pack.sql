-- 新弾フィーチャー 手動オーバーライドテーブル
-- =============================================
-- このテーブルは任意です。作成しなくても自動判定で動作します。
-- 例外運用（発売日とは別の日に開始したい、期間を延ばしたい等）の時のみ使用。
--
-- Supabase 管理画面 > SQL Editor で実行してください。
--
-- 使い方:
--   1. 下の CREATE TABLE を実行してテーブルを作成
--   2. INSERT 例を参考に、フィーチャーしたい弾を1行追加する
--   3. 期間が終わったら active を false に更新するか行を削除する
--
-- テーブルが存在しない / active=true 行がない場合は
-- pack_list.release_date ベースの自動判定にフォールバックします。

CREATE TABLE IF NOT EXISTS featured_pack (
    id          BIGSERIAL PRIMARY KEY,
    pack_name   TEXT        NOT NULL,              -- パック名（pack_list.name と一致させる）
    wiki_page   TEXT        NOT NULL DEFAULT '',   -- 遊戯王Wiki ページ名（空でも可）
    tcg_name    TEXT        NOT NULL DEFAULT '',   -- YGOPRODeck API 用 TCG セット名（空でも可）
    start_date  DATE        NOT NULL,              -- 運用開始日（通常は発売日）
    window_days INTEGER     NOT NULL DEFAULT 7,    -- 運用日数（既定7日）
    active      BOOLEAN     NOT NULL DEFAULT TRUE, -- TRUE の行のみ有効（最新1行が使われる）
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- INSERT 例（実際のパック名に変えて使用してください）:
-- INSERT INTO featured_pack (pack_name, wiki_page, tcg_name, start_date, window_days)
-- VALUES (
--     'パック名',                  -- 例: 'LIMIT OVER COLLECTION −THE RIVALS−'
--     'Wikiページ名',              -- 例: 'LIMIT OVER COLLECTION -THE RIVALS-'
--     'TCGセット名',               -- YGOPRODeck用、不要なら ''
--     '2026-05-30',               -- 発売日
--     7                           -- 投稿を続ける日数
-- );

-- 次の弾に切り替える時は既存行を無効化してから新しい行を入れる:
-- UPDATE featured_pack SET active = false WHERE active = true;
