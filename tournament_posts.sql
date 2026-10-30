-- 店舗・CS の大会結果ポスト（2026-10-30）
-- collect_tournament_posts.py（collect-tournament-posts.yml・1日1回）が X の検索で集めて upsert し、
-- テーマ単位の入賞数・分布の集計（採用急増の型・使用率分布の速報）が読む。まだ投稿には使わない。
-- 本文（text）を残すので、抜き出し方（adoption_parse.py）を直したら --reparse で読み直せる。
-- 書き込みは GitHub Actions の SUPABASE_KEY（service role）のみ。公開APIからは読ませない。

CREATE TABLE IF NOT EXISTS tournament_posts (
    tweet_id        text PRIMARY KEY,
    author          text NOT NULL,                 -- X のユーザー名（小文字）
    source_kind     text NOT NULL CHECK (source_kind IN ('shop', 'cs', 'cs_search')),
                                                   -- shop=店のアカウント／cs=CS主催者のアカウント／cs_search=語の検索で見つけたもの
    posted_at       timestamptz NOT NULL,
    event_date      date NOT NULL,                 -- 本文の「10/25開催」など。無ければ投稿日（JST）
    text            text NOT NULL,
    is_ocg          boolean NOT NULL,              -- 遊戯王OCGの結果か（他TCG・ラッシュデュエル・昔のルールの大会は false）
    participants    int,                           -- 参加人数（書いていなければ NULL）
    placements      jsonb NOT NULL DEFAULT '[]',   -- [{"rank":1,"theme_raw":"...","theme":"..."}]
    distribution    jsonb NOT NULL DEFAULT '[]',   -- [{"theme_raw":"...","theme":"...","count":3|null}]
    parser_version  smallint NOT NULL,
    fetched_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS tournament_posts_event_date_idx
    ON tournament_posts (event_date) WHERE is_ocg;

ALTER TABLE tournament_posts ENABLE ROW LEVEL SECURITY;
