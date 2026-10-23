-- TCG先行カード（英語版で先に出て、日本のOCGにまだ無いカード）の取り込み先
-- =============================================
-- 2026-10-23 追加。tools/tcg_preview_watch.py が週1回、YGOResources のミラーから「英語にあって日本語に無い」
-- カードを集め、効果文を機械訳（tcg_translate.py）して保存する。今回は取り込みだけで、画面にはまだ出さない。
--
-- Supabase 管理画面 > SQL Editor で実行してください（適用は司令塔が行う。ツール側は書き込むだけ）。
--
-- 日本語の語彙は unreleased_cards と揃える:
--   card_type = 「効果モンスター」「シンクロ・チューナー・効果モンスター」「速攻魔法」「通常罠」など
--   attribute = 「光」「闇」のように「属性」を付けない1字
--   race      = 「雷族」のように「族」付き
--
-- status の遷移:
--   active      → 日本のOCGにまだ無い（収集対象）
--   ocg_linked  → 日本に来たカード（ocg_name に日本語の正式名。今回は列を作るだけで、遷移の処理は後の段階）
--
-- translation_status の遷移:
--   pending → 未翻訳 / done → 翻訳済み / failed → 失敗（translation_error に理由。次回の実行で取り直す）
--   skipped → 訳さない（通常モンスターの説明文）。effect_text_ja は空のまま
--   訳し直しの判定は「今の英文 ≠ translated_source_en / translated_source_pend_en」または status が done 以外
--
-- first_seen_at はツールが payload に含めない（初回 INSERT の DEFAULT だけが入り、upsert の更新では書き換わらない）。

CREATE TABLE IF NOT EXISTS tcg_preview_cards (
    konami_id            INTEGER     PRIMARY KEY,                 -- YGOResources / 公式DB の cid

    -- カード識別・カードデータ
    name_en              TEXT        NOT NULL,                    -- 英語名
    card_type            TEXT        NOT NULL DEFAULT '',         -- カード種別（日本語）
    attribute            TEXT        NOT NULL DEFAULT '',         -- 属性（光/闇/炎/水/地/風/神）
    race                 TEXT        NOT NULL DEFAULT '',         -- 種族（雷族等）
    level                INTEGER,                                 -- レベル
    rank                 INTEGER,                                 -- ランク（エクシーズ）
    link_val             INTEGER,                                 -- リンク値
    atk                  INTEGER,                                 -- 攻撃力（NULL=?や魔法罠）
    def                  TEXT        NOT NULL DEFAULT '',         -- 守備力（文字列）
    pendulum_scale       INTEGER,                                 -- ペンデュラムスケール（NULL=非ペンデュラム）
    link_arrows          TEXT        NOT NULL DEFAULT '',         -- リンク矢印（公式の番号をカンマ区切り。例: 1,2,3）

    -- 効果文（英語の原文と機械訳）
    effect_text_en       TEXT        NOT NULL DEFAULT '',
    effect_text_ja       TEXT        NOT NULL DEFAULT '',
    pendulum_effect_en   TEXT        NOT NULL DEFAULT '',
    pendulum_effect_ja   TEXT        NOT NULL DEFAULT '',

    -- 翻訳の記録
    translation_model    TEXT        NOT NULL DEFAULT '',
    translation_warnings JSONB       NOT NULL DEFAULT '[]'::jsonb,
    translated_at        TIMESTAMPTZ,
    translation_status   TEXT        NOT NULL DEFAULT 'pending'
                         CHECK (translation_status IN ('pending', 'done', 'failed', 'skipped')),
    translation_error    TEXT,
    translated_source_en      TEXT   NOT NULL DEFAULT '',         -- 訳したときの英文（効果）
    translated_source_pend_en TEXT   NOT NULL DEFAULT '',         -- 訳したときの英文（ペンデュラム効果）

    -- 商品・収録情報（公式データベースの英語ページ）
    product_name_en      TEXT        NOT NULL DEFAULT '',
    set_code             TEXT        NOT NULL DEFAULT '',
    tcg_release_date     DATE,
    official_fetch_status TEXT       NOT NULL DEFAULT '',         -- 公式ページ取得の結果: ok / empty(商品0件) / failed
    official_fetched_at  TIMESTAMPTZ,                             -- 公式ページを最後に取得した日時

    -- 画像（公式データベース → Storage official-card-images/tcg_preview/<konami_id>.<拡張子>）
    image_storage_path   TEXT,
    image_url            TEXT,
    image_source_url     TEXT,
    image_page_url       TEXT,

    -- 管理
    status               TEXT        NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'ocg_linked')),
    hidden               BOOLEAN     NOT NULL DEFAULT FALSE,
    ocg_name             TEXT,
    ocg_linked_at        TIMESTAMPTZ,
    first_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  tcg_preview_cards                      IS 'TCG先行カード（英語版で先に出て日本のOCGにまだ無いカード）。週1回の収集ツールが書く';
COMMENT ON COLUMN tcg_preview_cards.konami_id            IS 'YGOResources の id = 公式データベースの cid（主キー）';
COMMENT ON COLUMN tcg_preview_cards.name_en              IS '英語のカード名';
COMMENT ON COLUMN tcg_preview_cards.card_type            IS 'カード種別（日本語。unreleased_cards と同じ語彙）';
COMMENT ON COLUMN tcg_preview_cards.attribute            IS '属性（光/闇/炎/水/地/風/神。「属性」は付けない）';
COMMENT ON COLUMN tcg_preview_cards.race                 IS '種族（「雷族」のように「族」付き）';
COMMENT ON COLUMN tcg_preview_cards.level                IS 'レベル';
COMMENT ON COLUMN tcg_preview_cards.rank                 IS 'ランク（エクシーズ）';
COMMENT ON COLUMN tcg_preview_cards.link_val             IS 'リンク値';
COMMENT ON COLUMN tcg_preview_cards.atk                  IS '攻撃力。NULLは「?」または魔法罠';
COMMENT ON COLUMN tcg_preview_cards.def                  IS '守備力（文字列。無ければ空文字）';
COMMENT ON COLUMN tcg_preview_cards.pendulum_scale       IS 'ペンデュラムスケール。NULL=非ペンデュラム';
COMMENT ON COLUMN tcg_preview_cards.link_arrows          IS 'リンク矢印。公式の番号（1〜9）のカンマ区切り';
COMMENT ON COLUMN tcg_preview_cards.effect_text_en       IS '英語の効果文（原文）';
COMMENT ON COLUMN tcg_preview_cards.effect_text_ja       IS '効果文の機械訳（tcg_translate.py）。公式の日本語ではない';
COMMENT ON COLUMN tcg_preview_cards.pendulum_effect_en   IS '英語のペンデュラム効果（原文）';
COMMENT ON COLUMN tcg_preview_cards.pendulum_effect_ja   IS 'ペンデュラム効果の機械訳';
COMMENT ON COLUMN tcg_preview_cards.translation_model    IS '翻訳に使ったモデル名';
COMMENT ON COLUMN tcg_preview_cards.translation_warnings IS '訳の自動点検の警告（数値・名前の食い違い）のJSON配列';
COMMENT ON COLUMN tcg_preview_cards.translated_at        IS '機械訳した日時';
COMMENT ON COLUMN tcg_preview_cards.translation_status   IS '翻訳の状態: pending=未/done=済/failed=失敗（次回取り直す）/skipped=訳さない（通常モンスター）';
COMMENT ON COLUMN tcg_preview_cards.translation_error    IS 'translation_status=failed のときの理由';
COMMENT ON COLUMN tcg_preview_cards.translated_source_en      IS '機械訳したときの英文（効果）。今の英文と違えば訳し直す';
COMMENT ON COLUMN tcg_preview_cards.translated_source_pend_en IS '機械訳したときの英文（ペンデュラム効果）。今の英文と違えば訳し直す';
COMMENT ON COLUMN tcg_preview_cards.official_fetch_status IS '公式ページ取得の結果: ok=商品あり/empty=ページは取れたが商品0件/failed=取得失敗・目印なし';
COMMENT ON COLUMN tcg_preview_cards.official_fetched_at  IS '公式ページを最後に取得した日時';
COMMENT ON COLUMN tcg_preview_cards.product_name_en      IS '収録商品名（英語）。複数あれば最も古い発売日の行';
COMMENT ON COLUMN tcg_preview_cards.set_code             IS '収録番号（例: BETB-EN081）';
COMMENT ON COLUMN tcg_preview_cards.tcg_release_date     IS 'TCGでの発売日（最も古い行）';
COMMENT ON COLUMN tcg_preview_cards.image_storage_path   IS 'Storage(official-card-images)内のパス（tcg_preview/<konami_id>.<拡張子>）。NULL=未取得';
COMMENT ON COLUMN tcg_preview_cards.image_url            IS '画像の公開URL。NULL=未取得（次回取り直す）';
COMMENT ON COLUMN tcg_preview_cards.image_source_url     IS '取得元の画像URL（来歴）';
COMMENT ON COLUMN tcg_preview_cards.image_page_url       IS '画像が掲載されていた公式データベースのページURL（来歴）';
COMMENT ON COLUMN tcg_preview_cards.status               IS 'active=日本に未発売/ocg_linked=日本に来た';
COMMENT ON COLUMN tcg_preview_cards.hidden               IS 'TRUE=個別非表示';
COMMENT ON COLUMN tcg_preview_cards.ocg_name             IS '日本に来た時の日本語の正式名（今回は列のみ）';
COMMENT ON COLUMN tcg_preview_cards.ocg_linked_at        IS 'status=ocg_linked になった日時（今回は列のみ）';
COMMENT ON COLUMN tcg_preview_cards.first_seen_at        IS '初めて収集した日時（初回のみ。更新で書き換えない）';
COMMENT ON COLUMN tcg_preview_cards.updated_at           IS '最終更新日時（収集のたびに更新）';

-- 表示用クエリ（status, hidden で絞る）の高速化
CREATE INDEX IF NOT EXISTS idx_tcg_preview_cards_status_hidden
    ON tcg_preview_cards (status, hidden);

-- RLS 有効・ポリシーなし＝anon からは直接読めない（サーバー側の service key だけが読み書きする）
ALTER TABLE tcg_preview_cards ENABLE ROW LEVEL SECURITY;
