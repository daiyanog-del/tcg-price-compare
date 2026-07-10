# activeContext — 今どこ・次何

> 更新: 2026-07-10

## 今どこ

- 前提監査＋リポジトリ棚卸しが完了し、**MAMB方式（多エージェント分業＋状態ファイル群）へ移行した直後**
- 状態ファイル新設: TASKS.md / docs/activeContext.md / docs/decisions.md（過去の主要決定を遡及起票済み）
- docs/ARCHITECTURE.md を実態（7店舗・Supabase・Render Cron・未発売パイプライン等）に全面改訂。旧版と失効文書は `..\..\_archive\docs_2026-03\`
- CLAUDE.md の正本を tcg-web/ 内（git管理下）に移し、旧「段階的分割」規律は廃止（decisions.md 参照）

## 次何

- **X投稿の新形式の実験設計が最優先**（現行の日次垂れ流しは廃止方向。実測: フォロワー2・105投稿でいいね0）。設計論点: X検索到達狙い（1カード1ポスト・フルカード名）／投稿条件（大変動のみ・該当なしの日は無投稿）／撤退ゲートの数値
- **GA4**: コード導入済み。ユーザーの GA4 プロパティ作成 → 測定IDを Render 環境変数 `GA_MEASUREMENT_ID` に設定すれば計測開始
- **movers 2系統併存の扱いをユーザーが判断**（X実験の結果次第でX側の系統が消えうるため、実験後の判断が合理的）。調査済みの選択肢:
  - 案1: 日次投稿のみRPC一本化（x_poster の get_price_movers を RPC呼び出しに置換。featured用の allowed_names 絞り込みは対象カードがRPCの top_n に乗らないためPython集計を残す＝部分一本化）
  - 案2: 現状維持＋days統一のみ（app.py の days=2 を x_poster と同じ 3 に揃えて欠測日に強くする。最小変更）
  - 案3: 完全現状維持（差異は decisions.md に記録済み、実害が出たら対応）
- x_poster 閾値変更（100→50円）の効果観察: 次回投稿（毎日 JST 20:07）から適用。数日分の投稿内容を見て過剰/過少を確認
- 本番は通常運用継続中（Render web + cron 3本 + GitHub Actions 6本 + ローカルWatcher）。特別な監視事項なし

## 申し送り・注意

- ルートの `自作画像など\` は tools/build_icons.py が参照する現役素材。削除禁止
- 毎週月曜に update-cardnames.yml が cardnames_ja.json を自動コミットする → **編集前 git pull 必須**
- ローカルでの動作確認の落とし穴（debug時 _estimate_cache 未ロード等）は Claude メモリ cardpricechecker-local-verify 参照
