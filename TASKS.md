# TASKS — CardPriceChecker (TCGYM)

> MAMB運用のタスクボード。セッション開始時にまずこれを読み、終了時に更新する。
> 現在地の詳細は [docs/activeContext.md](docs/activeContext.md)、決定の経緯は [docs/decisions.md](docs/decisions.md)。

## 進行中

（なし）

## バックログ（優先度順・2026-07-10 前提監査より）

- [ ] **X投稿の新形式の実験設計** — 現行の日次ランキング垂れ流しは廃止方向（フォロワー2・105投稿でエンゲージメントゼロの実測。docs/decisions.md 参照）。X検索到達を狙う形式（1カード1ポスト・フルカード名・大変動のみ等）を設計し、計測ゲート（何をいつまでに達成しなければ撤退か）とセットで決める
- [ ] **サイトにプライバシーポリシー表記を追加** — GA4（アクセス解析）利用の明示。小タスク
- [ ] **値動きランキングの2系統併存の解消検討** — Web表示=Supabase RPC / X投稿=Python集計で結果が食い違いうる。集計窓も days=2（app.py販売）と days=3（買取・X側）で不一致。調査済み（2026-07-10）: featured投稿の allowed_names 絞り込みはRPCのtop_nに乗らないため完全一本化は不可。選択肢はユーザー判断待ち（docs/activeContext.md 参照）。※X投稿形式の実験結果次第でX側の系統ごと消える可能性があるため、実験の後に判断が合理的

## 完了

- [x] 2026-07-10 GA4計測タグを導入し本番稼働開始（環境変数駆動・測定ID設定済み・本番HTMLでタグ出力を実機確認）

- [x] 2026-07-10 x_poster 投稿閾値を実データ校正（DAILY_MIN_DIFF 100→50円、60日8,854ペアの分布から導出。docs/decisions.md 参照）
- [x] 2026-07-10 柵5箇所の由来調査＋コメント追記（deck_image `_v2`/1KB閾値=8983439由来、scraper sleep(2)=初回アップロードから根拠なし、rarity「レアレア」=実データ非実在の投機的登録と確定、_EX_PROP_IDS=一次情報源の記録なし）
- [x] 2026-07-10 fetch_guard.ALLOWED_PATH_PREFIXES を削除（未使用の検証機構ごと除去、テスト追従）
- [x] 2026-07-10 README.md の失効記述更新（3店舗同時検索→販売7店舗・デフォルト6店、ファイル構成→docs/ARCHITECTURE.md誘導）
- [x] 2026-07-10 前提監査（assumptions-check）＋棚卸し（inventory-audit）実施
- [x] 2026-07-10 未使用3ファイル削除（c859e17）・ルート空フォルダ削除・失効文書6枚を `_archive\docs_2026-03\` へ移動
- [x] 2026-07-10 test_fetch_guard の失効期待値を追従（67640e8）
- [x] 2026-07-10 MAMB方式へ移行（状態ファイル新設・ARCHITECTURE.md全面改訂・CLAUDE.md改訂）
