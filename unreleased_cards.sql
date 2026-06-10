-- 未発売カード管理テーブル群
-- =============================================
-- 未発売カードの表示・管理・画像保存に必要なテーブルをまとめたDDL。
--
-- Supabase 管理画面 > SQL Editor で実行してください。
--
-- テーブル構成:
--   app_settings           — サイト全体の設定キー/値ストア（OFFICIAL_IMAGE_DISPLAY等）
--   watched_pages          — Watcherによる差分検知対象URL
--   unreleased_cards       — 未発売カードのレビューキュー兼マスタ
--   official_card_images   — 未発売カードの公式画像（来歴必須）
--
-- Storageバケット:
--   official-card-images   — public read バケット（末尾のINSERTで作成）

-- ──────────────────────────────────────────────
-- 1. app_settings（サイト設定キー/値ストア）
-- ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT        PRIMARY KEY,                  -- 設定キー（例: OFFICIAL_IMAGE_DISPLAY）
    value       JSONB       NOT NULL,                     -- 設定値（JSONBで型を柔軟に保持）
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()        -- 最終更新日時
);

-- 初期設定: 公式画像表示は有効（disabled にするとサイト全体がプロキシ表示へ縮退）
INSERT INTO app_settings (key, value)
VALUES ('OFFICIAL_IMAGE_DISPLAY', '{"enabled": true}'::jsonb)
ON CONFLICT (key) DO NOTHING;

COMMENT ON TABLE  app_settings              IS 'サイト全体の設定キー/値ストア';
COMMENT ON COLUMN app_settings.key         IS '設定キー名（例: OFFICIAL_IMAGE_DISPLAY）';
COMMENT ON COLUMN app_settings.value       IS '設定値（JSONBで型を柔軟に保持）';
COMMENT ON COLUMN app_settings.updated_at  IS '最終更新日時';

-- ──────────────────────────────────────────────
-- 2. watched_pages（Watcher差分検知対象URL）
-- ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS watched_pages (
    url                TEXT        PRIMARY KEY,           -- 監視対象URL（PK）
    content_hash       TEXT        NOT NULL DEFAULT '',   -- 最終確認時のコンテンツハッシュ（差分検知用）
    last_checked_at    TIMESTAMPTZ,                       -- 最終確認日時
    last_changed_at    TIMESTAMPTZ,                       -- コンテンツ変化を検知した日時
    last_extracted_at  TIMESTAMPTZ,                       -- Extractorが最後に処理した日時
    enabled            BOOLEAN     NOT NULL DEFAULT TRUE  -- FALSE にすると巡回スキップ
);

COMMENT ON TABLE  watched_pages                  IS 'Watcherによる差分検知対象URLの管理';
COMMENT ON COLUMN watched_pages.url              IS '監視対象URL（主キー）';
COMMENT ON COLUMN watched_pages.content_hash     IS '最終確認時コンテンツのSHA256ハッシュ（差分検知用）';
COMMENT ON COLUMN watched_pages.last_checked_at  IS '最終確認日時（変化がなくても更新）';
COMMENT ON COLUMN watched_pages.last_changed_at  IS 'コンテンツ変化を検知した日時';
COMMENT ON COLUMN watched_pages.last_extracted_at IS 'Extractorが処理した最終日時';
COMMENT ON COLUMN watched_pages.enabled          IS 'FALSEにすると巡回対象から除外';

-- ──────────────────────────────────────────────
-- 3. unreleased_cards（未発売カードのレビューキュー兼マスタ）
-- ──────────────────────────────────────────────
-- status の遷移:
--   pending       → 抽出済みだがまだ未レビュー
--   approved      → 手動承認済み（サイトに表示可）
--   rejected      → 否認（サイトに出さない）
--   linked        → 発売後にygoresourcesのkonami_idと紐付け済み
--   needs_review  → Reconcilerが複数候補などで自動判定できず要確認
--
-- pending / rejected は表示解決ロジックが絶対にサイトへ出さない。
CREATE TABLE IF NOT EXISTS unreleased_cards (
    id              BIGSERIAL   PRIMARY KEY,

    -- カード識別
    name            TEXT        NOT NULL,                 -- カード名（日本語正式名称）
    reading         TEXT        NOT NULL DEFAULT '',      -- 読み仮名（フリガナ）

    -- カードデータ（プロキシ描画に使用）
    card_type       TEXT        NOT NULL DEFAULT '',      -- カード種別（モンスター/魔法/罠等）
    attribute       TEXT        NOT NULL DEFAULT '',      -- 属性（光/闇/炎/水/地/風/神）
    race            TEXT        NOT NULL DEFAULT '',      -- 種族
    level           INTEGER,                              -- レベル（通常モンスター/効果モンスター用）
    rank            INTEGER,                              -- ランク（エクシーズ用）
    link_val        INTEGER,                              -- リンク値（リンクモンスター用）
    atk             INTEGER,                              -- 攻撃力（NULL=?扱い）
    def             TEXT        NOT NULL DEFAULT '',      -- 守備力（「?」「-」等の文字列にも対応）
    effect_text     TEXT        NOT NULL DEFAULT '',      -- 効果テキスト / フレーバーテキスト

    -- 商品・収録情報
    product_name    TEXT        NOT NULL DEFAULT '',      -- 収録パック名
    release_date    DATE,                                 -- 発売予定日（NULL=未定）

    -- 抽出メタデータ
    confidence      TEXT        NOT NULL DEFAULT 'medium' CHECK (confidence IN ('high', 'medium', 'low')),
    source_url      TEXT        NOT NULL DEFAULT '',      -- 抽出元URL
    source_domain   TEXT        NOT NULL DEFAULT '',      -- 抽出元ドメイン（ドメイン単位削除のキー）
    extracted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),   -- 抽出日時
    extraction_raw  JSONB,                                -- Extractor生の出力（デバッグ・再処理用）

    -- 承認・表示管理
    status          TEXT        NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected', 'linked', 'needs_review')),
    hidden          BOOLEAN     NOT NULL DEFAULT FALSE,   -- TRUE=個別非表示（承認済みでも隠す）
    konami_id       INTEGER,                              -- 発売後に紐付けるYGOResources konami_id

    -- 重複防止（同一カード名・同一パックは1行）
    UNIQUE (name, product_name)
);

COMMENT ON TABLE  unreleased_cards               IS '未発売カードのレビューキュー兼マスタ。pending/rejectedはサイトに出さない';
COMMENT ON COLUMN unreleased_cards.name          IS 'カード名（日本語正式名称）';
COMMENT ON COLUMN unreleased_cards.reading       IS '読み仮名（フリガナ）';
COMMENT ON COLUMN unreleased_cards.card_type     IS 'カード種別（モンスター/効果モンスター/魔法/罠等）';
COMMENT ON COLUMN unreleased_cards.attribute     IS '属性（光/闇/炎/水/地/風/神）';
COMMENT ON COLUMN unreleased_cards.race          IS '種族（戦士族/魔法使い族等）';
COMMENT ON COLUMN unreleased_cards.level         IS 'レベル。エクシーズはrank、リンクはlink_valを使う';
COMMENT ON COLUMN unreleased_cards.rank          IS 'ランク（エクシーズモンスター用）';
COMMENT ON COLUMN unreleased_cards.link_val      IS 'リンク値（リンクモンスター用）';
COMMENT ON COLUMN unreleased_cards.atk           IS '攻撃力。NULLは「?」として扱う';
COMMENT ON COLUMN unreleased_cards.def           IS '守備力（テキスト型: 数値・「?」・「-」等に対応）';
COMMENT ON COLUMN unreleased_cards.effect_text   IS '効果テキストまたはフレーバーテキスト';
COMMENT ON COLUMN unreleased_cards.product_name  IS '収録パック名（例: ANIMATION CHRONICLE 2026）';
COMMENT ON COLUMN unreleased_cards.release_date  IS '発売予定日。NULL=未定';
COMMENT ON COLUMN unreleased_cards.confidence    IS '抽出精度（high/medium/low）。管理画面での優先確認に使用';
COMMENT ON COLUMN unreleased_cards.source_url    IS '抽出元ページURL';
COMMENT ON COLUMN unreleased_cards.source_domain IS '抽出元ドメイン（yu-gi-oh.jp等）。ドメイン単位削除のキー';
COMMENT ON COLUMN unreleased_cards.extraction_raw IS 'Extractor生の出力JSON（デバッグ・再処理用）';
COMMENT ON COLUMN unreleased_cards.status        IS 'ステータス: pending=未レビュー/approved=承認済/rejected=否認/linked=発売後紐付済/needs_review=要確認';
COMMENT ON COLUMN unreleased_cards.hidden        IS 'TRUE=個別非表示。承認済みでもこのフラグでサイトから隠せる';
COMMENT ON COLUMN unreleased_cards.konami_id     IS '発売後にReconcilerが設定するYGOResources konami_id';

-- インデックス（表示解決クエリの高速化）
CREATE INDEX IF NOT EXISTS idx_unreleased_cards_status_hidden
    ON unreleased_cards (status, hidden)
    WHERE status IN ('approved', 'linked');

-- ──────────────────────────────────────────────
-- 4. official_card_images（公式画像。来歴必須）
-- ──────────────────────────────────────────────
-- ドメイン一括削除の2段階フロー:
--   ① hidden=true 更新 → サーバー側キャッシュ無効化で即時非表示
--   ② Storage物理削除 + deleted_at 記録
CREATE TABLE IF NOT EXISTS official_card_images (
    id                  BIGSERIAL   PRIMARY KEY,
    unreleased_card_id  BIGINT      NOT NULL REFERENCES unreleased_cards (id) ON DELETE CASCADE,

    -- Storage情報
    storage_path        TEXT        NOT NULL,             -- Supabase Storage内のオブジェクトパス
    public_url          TEXT        NOT NULL,             -- Storage public URL

    -- 来歴（必須。出所不明画像はINSERT不可）
    source_image_url    TEXT        NOT NULL,             -- 取得元画像URL
    source_page_url     TEXT        NOT NULL,             -- 画像が掲載されていたページURL
    source_domain       TEXT        NOT NULL,             -- ドメイン単位削除のキー（例: yu-gi-oh.jp）

    -- 管理
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(), -- 取得・保存日時
    hidden              BOOLEAN     NOT NULL DEFAULT FALSE,  -- TRUE=即時非表示（物理削除前の第1段階）
    deleted_at          TIMESTAMPTZ                          -- Storage物理削除日時（NULL=未削除）
);

COMMENT ON TABLE  official_card_images                    IS '未発売カードの公式画像。来歴（source_*）は必須';
COMMENT ON COLUMN official_card_images.unreleased_card_id IS '参照先 unreleased_cards.id';
COMMENT ON COLUMN official_card_images.storage_path       IS 'Supabase Storage内のオブジェクトパス';
COMMENT ON COLUMN official_card_images.public_url         IS 'Storage public URL（表示に使用）';
COMMENT ON COLUMN official_card_images.source_image_url   IS '取得元の画像直URL';
COMMENT ON COLUMN official_card_images.source_page_url    IS '画像が掲載されていたページURL';
COMMENT ON COLUMN official_card_images.source_domain      IS 'ドメイン単位削除のキー（yu-gi-oh.jp等）';
COMMENT ON COLUMN official_card_images.fetched_at         IS '画像をStorageに保存した日時';
COMMENT ON COLUMN official_card_images.hidden             IS 'TRUE=即時非表示（ドメイン一括削除の第1段階）';
COMMENT ON COLUMN official_card_images.deleted_at         IS 'Storage物理削除日時。NULLなら未削除';

-- インデックス（表示解決クエリの高速化）
CREATE INDEX IF NOT EXISTS idx_official_card_images_card_hidden
    ON official_card_images (unreleased_card_id, hidden)
    WHERE hidden = false AND deleted_at IS NULL;

-- ──────────────────────────────────────────────
-- Supabase Storage バケット作成（public read）
-- ──────────────────────────────────────────────
-- バケットが存在しない場合のみ作成する（冪等）
INSERT INTO storage.buckets (id, name, public)
VALUES ('official-card-images', 'official-card-images', true)
ON CONFLICT (id) DO NOTHING;
