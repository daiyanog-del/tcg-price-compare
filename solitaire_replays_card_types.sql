-- 共有リプレイのカード種類（{cardId: 'monster'|'spell'|'trap'}）。再生・復元で data-card-type を付け直すため。
-- null 可（旧リプレイ）。未適用でも共有・読み込みは動く（サーバーが列なしで再試行する）が、カード種類は失われる。
alter table public.solitaire_replays add column if not exists card_types jsonb;
