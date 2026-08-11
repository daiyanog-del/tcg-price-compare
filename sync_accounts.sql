-- 購入候補・保存デッキの端末間同期用テーブル
-- 未適用（作成のみ。DDLの適用はSupabase管理API/SQL Editorで別途行う）。
--
-- 設計方針:
--   - docs/design-sync-2026-08-09.md（第2版）「番号札方式（リビジョン制御）」に対応する
--   - sync_id はログイン不要の匿名識別子（UUIDv4）。会員登録は作らない
--   - wishlist（購入候補）と decks（保存デッキ）でリビジョンを分ける。
--     片方の編集がもう片方の競合を誘発しないため
--   - RLSは既存テーブル（ygores_cache.sql等）と同様に有効化のみ
--     （ポリシーなし＝anonアクセス遮断、サーバーはservice_role keyでアクセス）
--   - P1（本ファイル作成時点）で実際に読み書きするのは wishlist 系カラムのみ。
--     decks 系・sync_link_tokens は P2/P3 向けに先行して定義しておく

-- 同期アカウント（1端末グループにつき1行）
create table if not exists sync_accounts (
    sync_id       uuid primary key default gen_random_uuid(),
    wishlist      jsonb  not null default '[]'::jsonb,   -- 購入候補（既存 localStorage 形式と同一）
    wishlist_rev  bigint not null default 0,              -- 購入候補の単調増加リビジョン
    decks         jsonb  not null default '[]'::jsonb,    -- 保存デッキ（P2で使用開始）
    decks_rev     bigint not null default 0,               -- 保存デッキの単調増加リビジョン（P2）
    created_at    timestamptz not null default now(),
    last_seen_at  timestamptz not null default now()
);
create index if not exists sync_accounts_last_seen_idx on sync_accounts (last_seen_at);

-- 端末を紐づけるためのワンタイムリンクトークン（P3で使用開始）
create table if not exists sync_link_tokens (
    token       text primary key,
    sync_id     uuid not null references sync_accounts(sync_id) on delete cascade,
    created_at  timestamptz not null default now(),
    expires_at  timestamptz not null,   -- 発行から10分
    used_at     timestamptz              -- 1回使用で失効（redeem時に条件付き更新でセット）
);
create index if not exists sync_link_tokens_expires_idx on sync_link_tokens (expires_at);

alter table sync_accounts enable row level security;
alter table sync_link_tokens enable row level security;
