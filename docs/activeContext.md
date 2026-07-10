# activeContext — 今どこ・次何

> 更新: 2026-07-10

## 今どこ

- 前提監査＋リポジトリ棚卸しが完了し、**MAMB方式（多エージェント分業＋状態ファイル群）へ移行した直後**
- 状態ファイル新設: TASKS.md / docs/activeContext.md / docs/decisions.md（過去の主要決定を遡及起票済み）
- docs/ARCHITECTURE.md を実態（7店舗・Supabase・Render Cron・未発売パイプライン等）に全面改訂。旧版と失効文書は `..\..\_archive\docs_2026-03\`
- CLAUDE.md の正本を tcg-web/ 内（git管理下）に移し、旧「段階的分割」規律は廃止（decisions.md 参照）

## 次何

- TASKS.md バックログの着手判断。推奨順: x_poster閾値校正 → 柵の由来調査 → movers一本化検討
- 本番は通常運用継続中（Render web + cron 3本 + GitHub Actions 6本 + ローカルWatcher）。特別な監視事項なし

## 申し送り・注意

- ルートの `自作画像など\` は tools/build_icons.py が参照する現役素材。削除禁止
- 毎週月曜に update-cardnames.yml が cardnames_ja.json を自動コミットする → **編集前 git pull 必須**
- ローカルでの動作確認の落とし穴（debug時 _estimate_cache 未ロード等）は Claude メモリ cardpricechecker-local-verify 参照
