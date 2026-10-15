-- 共有リプレイの最後の盤面（リンク画像用）。null 可（旧リプレイ・検証に通らなかった場合）
alter table public.solitaire_replays add column if not exists board jsonb;
