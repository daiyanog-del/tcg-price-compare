-- 汎用カードのリスト（2026-10-21）
-- staple_cards.py --update（update-staple-cards.yml・週1回）が作り直し、
-- staple_post.py（post-staple-movers.yml）が読む。
-- 書き込みは GitHub Actions の SUPABASE_KEY（service role）のみ。公開APIからは読ませない。

CREATE TABLE IF NOT EXISTS staple_cards (
    card_name     text PRIMARY KEY,
    themes        int  NOT NULL,          -- 採用していた異なるテーマの数
    decks         int  NOT NULL,          -- 採用していたレシピの数
    is_ex         boolean NOT NULL DEFAULT false,
    sample_decks  int  NOT NULL,          -- 集計に使ったレシピの総数
    window_from   date NOT NULL,
    window_to     date NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE staple_cards ENABLE ROW LEVEL SECURITY;
