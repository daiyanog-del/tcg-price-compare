-- ygoresources ローカルキャッシュ用テーブル
-- 適用済み（2026-06-10、MCP経由でマイグレーション実行）。再構築時はSupabase SQL Editorで実行する。
--
-- 設計方針:
--   - raw カラムにAPIの生JSONをそのまま保存する（上流のスキーマ変更時に再フェッチなしで再パースできるようにするため）
--   - path は manifest の変更パス表記（例: card/4007, idx/card/name/ja）と一致させ、差分同期の突合キーにする
--   - RLSは既存テーブルと同様に有効化のみ（ポリシーなし＝anonアクセス遮断、サーバーはservice keyでアクセス）

-- カードデータ（/data/card/<konami_id> の生レスポンス）
create table if not exists ygores_cards (
    konami_id  bigint primary key,
    name       text,                                  -- 日本語名（検索・デバッグ用に正規化して保持）
    card_type  text,                                  -- monster / spell / trap
    raw        jsonb not null,                        -- APIレスポンス全体
    fetched_at timestamptz not null default now()
);
create index if not exists idx_ygores_cards_name on ygores_cards (name);

-- Q&A（裁定）データ（/data/qa/<id>。一人回しツール基盤用に先行整備、現時点で呼び出し元なし）
create table if not exists ygores_qa (
    qa_id      bigint primary key,
    raw        jsonb not null,
    fetched_at timestamptz not null default now()
);

-- 索引系・その他のJSON blob（/data/idx/card/name/ja 等、IDを持たないエンドポイント）
create table if not exists ygores_blobs (
    path       text primary key,                      -- 例: 'idx/card/name/ja'
    raw        jsonb not null,
    fetched_at timestamptz not null default now()
);

-- 同期メタデータ（最終確認 X-Cache-Revision 等）
create table if not exists ygores_sync_meta (
    key        text primary key,                      -- 'last_revision' / 'last_sync_at'
    value      text,
    updated_at timestamptz not null default now()
);

alter table ygores_cards enable row level security;
alter table ygores_qa enable row level security;
alter table ygores_blobs enable row level security;
alter table ygores_sync_meta enable row level security;
