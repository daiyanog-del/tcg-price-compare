-- 共有リプレイのカード種類（{cardId: 'monster'|'spell'|'trap'}）。再生・復元で data-card-type を付け直すため。
-- null 可（旧リプレイ）。board 列（solitaire_replays_board.sql）と同じく、デプロイの前に適用すること。
-- 手順: 本番に適用 → 列が反映されたか確認（されていなければ notify pgrst, 'reload schema';）→ デプロイ
alter table public.solitaire_replays add column if not exists card_types jsonb;
