-- 公開リプレイ（みんなのリプレイ。公開ライブラリ化の段階2）
-- 利用者が共有時に選んだリプレイだけを is_public = true で保存し、一覧に出す。公開分は90日削除の対象外。
-- 手順: 本番に適用 → 列が反映されたか確認（されていなければ notify pgrst, 'reload schema';）→ デプロイ
-- 適用前にデプロイすると、公開の保存・一覧・90日掃除（is_public で絞る）が列なしで失敗するので、必ず先に適用する。

-- 公開フラグ。既定は false（既存の共有リプレイは公開しない）
alter table public.solitaire_replays add column if not exists is_public boolean not null default false;
-- 公開日時。公開でない行は null。一覧の並びと「続きを読む」の基準
alter table public.solitaire_replays add column if not exists published_at timestamptz;
-- 使ったカード名（トークン除く・重複なし・最大80件）。サーバーが names から作る。カード名での絞り込み用
alter table public.solitaire_replays add column if not exists card_names text[];
-- 手数（setup を除いたログの数）
alter table public.solitaire_replays add column if not exists steps integer;
-- 取り下げ用の鍵の sha256（16進）。鍵そのものは保存しない（公開したブラウザだけが持つ）
alter table public.solitaire_replays add column if not exists delete_token_hash text;
-- 運営が一覧から隠すためのフラグ（行は残す）
alter table public.solitaire_replays add column if not exists hidden boolean not null default false;

-- カード名での絞り込み（card_names @> ARRAY['名前']）
create index if not exists solitaire_replays_card_names_gin
    on public.solitaire_replays using gin (card_names);
-- 公開一覧（新しい順）。公開かつ非表示でない行だけ
create index if not exists solitaire_replays_public_published_at_idx
    on public.solitaire_replays (published_at desc)
    where is_public and not hidden;
