-- 海外先行の予告カード（英語版の新弾で、公式データベースにまだ載っていないカード）の取り込み先
-- =============================================
-- 2026-10-29 追加。tools/tcg_announce_watch.py が1時間ごとに Yugipedia を見て、新しい英語版の弾と
-- その新規カードを集め、宣伝文・英語テキストを日本語の概要にして保存する。/tcg-preview/<slug> が弾ごとに表示する。
-- 公式データベースに載ったカードは tcg_preview_cards（週1回・konami_id が鍵）へ移るので、ここでは status='in_db' にして引退させる。
--
-- Supabase 管理画面 > SQL Editor で実行してください（適用は司令塔が行う。ツール側は書き込むだけ）。
-- 語彙（card_type・attribute・race）は tcg_preview_cards と同じ。

-- ── 弾 ──────────────────────────────────────
-- status: active＝追いかけ中 / closed＝英語版の発売から日数が経ち、追いかけるのをやめた
CREATE TABLE IF NOT EXISTS tcg_announce_sets (
    prefix           TEXT        PRIMARY KEY,                  -- 収録番号の頭（MZMC）
    slug             TEXT        NOT NULL UNIQUE,              -- URL 用（maze-of-machines）
    name_en          TEXT        NOT NULL,
    list_title       TEXT        NOT NULL,                     -- Yugipedia の「Set Card Lists:… (TCG-EN)」
    list_page_id     INTEGER,
    list_revid       BIGINT,                                   -- 最後に読んだ一覧の版（変わっていなければカードを読み直さない）
    set_title        TEXT        NOT NULL DEFAULT '',          -- 弾のページ名
    size             INTEGER,                                  -- 収録枚数（分かれば）
    tcg_release_date DATE,                                     -- 英語版の発売日（北米）
    status           TEXT        NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'closed')),
    hidden           BOOLEAN     NOT NULL DEFAULT FALSE,
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),       -- payload に含めない（初回 INSERT だけ）
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── カード ──────────────────────────────────
-- 鍵は Yugipedia のページ番号（予告名から正式名へページが移動しても変わらない。収録番号は未定「MZMC-EN0??」のことがある）
-- lore_kind: blurb＝販売店などの宣伝文（非公式）/ official＝カードの英語テキスト / none＝効果は未公開
-- summary_status: pending / done / failed（次回取り直す）/ skipped（効果が未公開で要約するものが無い）
-- status: announced＝予告中 / in_db＝公式DBに載った（tcg_preview_cards が引き継ぐ）/
--         dropped＝一覧の「New」から外れた（ページの作り直し・誤記の訂正など。戻ってきたら announced に戻す）
CREATE TABLE IF NOT EXISTS tcg_announce_cards (
    page_id            INTEGER     PRIMARY KEY,
    set_prefix         TEXT        NOT NULL REFERENCES tcg_announce_sets(prefix),
    set_code           TEXT        NOT NULL DEFAULT '',        -- MZMC-EN005（未定なら MZMC-EN0??）
    list_order         INTEGER     NOT NULL DEFAULT 0,         -- 一覧での並び
    page_title         TEXT        NOT NULL,
    page_revid         BIGINT,                                 -- 最後に読んだカードのページの版
    name_en            TEXT        NOT NULL,
    unofficial_name    BOOLEAN     NOT NULL DEFAULT FALSE,     -- 名前が仮のもの（{{Unofficial name}}）

    card_type          TEXT        NOT NULL DEFAULT '',
    attribute          TEXT        NOT NULL DEFAULT '',
    race               TEXT        NOT NULL DEFAULT '',
    level              INTEGER,
    rank               INTEGER,
    link_val           INTEGER,
    atk                TEXT        NOT NULL DEFAULT '',        -- 「?」「???」も入るので文字列
    def                TEXT        NOT NULL DEFAULT '',
    pendulum_scale     INTEGER,
    link_arrows        TEXT        NOT NULL DEFAULT '',

    lore_kind          TEXT        NOT NULL DEFAULT 'none' CHECK (lore_kind IN ('blurb', 'official', 'none')),
    lore_en            TEXT        NOT NULL DEFAULT '',
    lore_source_url    TEXT        NOT NULL DEFAULT '',        -- 宣伝文の出どころ（southernhobby.com など）
    pendulum_lore_en   TEXT        NOT NULL DEFAULT '',

    summary_ja         JSONB       NOT NULL DEFAULT '[]'::jsonb, -- 効果の概要（短い文の配列）
    summary_short_ja   TEXT        NOT NULL DEFAULT '',        -- 投稿用の1〜2文
    related_ja         JSONB       NOT NULL DEFAULT '[]'::jsonb, -- 宣伝文がリンクしている既存カードの日本語の正式名
    effect_text_ja     TEXT        NOT NULL DEFAULT '',        -- lore_kind=official のときの全文の機械訳
    pendulum_effect_ja TEXT        NOT NULL DEFAULT '',
    summary_status     TEXT        NOT NULL DEFAULT 'pending' CHECK (summary_status IN ('pending', 'done', 'failed', 'skipped')),
    summary_error      TEXT,
    summary_warnings   JSONB       NOT NULL DEFAULT '[]'::jsonb, -- 要約に英文に無い数字（あれば自動投稿しない）
    effect_warnings    JSONB       NOT NULL DEFAULT '[]'::jsonb, -- 全文の機械訳の点検（名前・数字の食い違い。投稿は止めない）
    summary_attempts   INTEGER     NOT NULL DEFAULT 0,         -- 今の英文で要約を試した回数（失敗・見送りは3回まで取り直す）
    summary_source_seen TEXT       NOT NULL DEFAULT '',        -- summary_attempts を数えている英文
    summary_model      TEXT        NOT NULL DEFAULT '',
    summarized_source  TEXT        NOT NULL DEFAULT '',        -- 要約したときの英文（変われば作り直す）
    summarized_at      TIMESTAMPTZ,
    official_at        TIMESTAMPTZ,                            -- lore_kind が official になった日時（「全文判明」の続報に使う）

    status             TEXT        NOT NULL DEFAULT 'announced' CHECK (status IN ('announced', 'in_db', 'dropped')),
    hidden             BOOLEAN     NOT NULL DEFAULT FALSE,
    first_seen_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),     -- payload に含めない（初回 INSERT だけ）
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tcg_announce_cards_set ON tcg_announce_cards (set_prefix, status, hidden);

-- RLS 有効・ポリシーなし＝anon からは直接読めない（サーバー側の service key だけが読み書きする）
ALTER TABLE tcg_announce_sets  ENABLE ROW LEVEL SECURITY;
ALTER TABLE tcg_announce_cards ENABLE ROW LEVEL SECURITY;

-- ── X 投稿の記録 ────────────────────────────
-- tcg_announce_post.py が投稿の前に予約の行を入れ（重複は一意の索引で止まる）、結果で更新する。
-- kind: set_first＝初報（弾を見つけて最初の概要ができたとき）/ set_update＝続報（判明が増えたとき）/
--       card＝カードごとの連載（1日1枚）/ card_official＝連載で出したカードの英語テキストが出たとき
-- status: reserved＝送信中 / posted＝投稿済み / unknown＝結果不明（投稿されたかもしれない。自動で出し直さない）/
--         failed＝X に明確に拒否された（同じ本文の重複など。自動で出し直さない）
-- unknown・failed・30分以上 reserved の行は Discord に1回知らせる。X で確かめたら、出ていれば status='posted'、
-- 出ていなければ行を消す（次の回に出し直す）
CREATE TABLE IF NOT EXISTS tcg_announce_posts (
    id           BIGSERIAL   PRIMARY KEY,
    kind         TEXT        NOT NULL CHECK (kind IN ('set_first', 'set_update', 'card', 'card_official')),
    set_prefix   TEXT        NOT NULL REFERENCES tcg_announce_sets(prefix),
    page_id      INTEGER,                                      -- カードの投稿のとき
    name_en      TEXT,                                         -- カードの投稿のときのカード名（ページの作り直しで番号が変わっても同じカードを二度出さない）
    known_count  INTEGER,                                      -- 弾の投稿のときの判明枚数（概要のあるカード）
    status       TEXT        NOT NULL DEFAULT 'reserved' CHECK (status IN ('reserved', 'posted', 'unknown', 'failed')),
    tweet_id     TEXT,
    text         TEXT        NOT NULL DEFAULT '',
    error        TEXT,
    notified_at  TIMESTAMPTZ,                                  -- unknown・failed・残った reserved を知らせた日時（1回だけ知らせる）
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    posted_at    TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tcg_announce_posts_set_first ON tcg_announce_posts (set_prefix) WHERE kind = 'set_first';
CREATE UNIQUE INDEX IF NOT EXISTS uq_tcg_announce_posts_set_update ON tcg_announce_posts (set_prefix, known_count) WHERE kind = 'set_update';
CREATE UNIQUE INDEX IF NOT EXISTS uq_tcg_announce_posts_card ON tcg_announce_posts (kind, page_id) WHERE kind IN ('card', 'card_official');
ALTER TABLE tcg_announce_posts ENABLE ROW LEVEL SECURITY;
