# TASKS — CardPriceChecker (TCGYM)

> MAMB運用のタスクボード。セッション開始時にまずこれを読み、終了時に更新する。
> 現在地の詳細は [docs/activeContext.md](docs/activeContext.md)、決定の経緯は [docs/decisions.md](docs/decisions.md)。

## 進行中

（なし）

## バックログ（優先度順・2026-07-10 前提監査より）

- [ ] **買取収集復旧の実地確認** — 2026-07-11朝（JST7:00台）の tcg-collect-buyback 実行ログで「収集対象」が300件前後に回復しているか確認する。翌7/12はさらに増えるはず（未登録hot 215枚が7/11実行のupsertで追加されるため）。販売側 tcg-collect-prices（JST5:00）の「新規追加」ログも併せて確認
- [ ] **/api/packs の発売前パック表示方針の確認** — 今回の修正で「Wikiにカードリスト未掲載のパック」は get_pack_list から除外されるようになった（pack_scraper.py:93 の元コメントどおりの挙動に戻した）。サイトの最新弾リストから発売前パックが消えるのが意図に反する場合は、表示用と収集用の分離を検討（reviewer指摘）
- [ ] **騰落率パイプラインの前提監査（別セッションで実施）** — 偽陽性確定（ブラックマジシャン+489%=表記ゆれ系列分裂＋単一店舗在庫入替）を受けた assumptions-check。監査完了後に対策設計→X投稿再開判断
- [ ] **X新形式（bigmove）の再開判断** — schedule停止中。上の監査＋対策実装後に再開し、撤退ゲート（インプ中央値100、期限は再開から8〜10週）を再設定
- [ ] **サイトにプライバシーポリシー表記を追加** — GA4（アクセス解析）利用の明示。小タスク
- [ ] **値動きランキングの2系統併存の解消検討** — Web表示=Supabase RPC / X投稿=Python集計で結果が食い違いうる。集計窓も days=2（app.py販売）と days=3（買取・X側）で不一致。調査済み（2026-07-10）: featured投稿の allowed_names 絞り込みはRPCのtop_nに乗らないため完全一本化は不可。選択肢はユーザー判断待ち（docs/activeContext.md 参照）。※X投稿形式の実験結果次第でX側の系統ごと消える可能性があるため、実験の後に判断が合理的

## 完了

- [x] 2026-07-10 **買取収集激減（7/7以降 1,185→50行/日）の根治4点** — ①TCG PORTAL deck-guides のHTML改修（h3→p）に fetch_deck_cards を追従 ②tracked_cards 1,243件の PostgREST 1000行上限を分割取得（order付きrange）で根治＋ _insert_new_cards を upsert(ignore_duplicates) 化 ③Wikiカードリスト未掲載パックに get_pack_list の枠を消費させない ④sync_regulation の取得元を公式カードDB（forbidden_limited.action）へ移行。ドライラン検証: hot候補562件・収集対象347件（障害前206件を上回る）・規制197枚復活・テスト75件全緑・reviewer監査済（中指摘のorder付与を反映）
- [x] 2026-07-10 X投稿を大変動単発形式（post_big_movers）へ切替（閾値は60日実測校正・reviewer監査で繰り上げ不一致を検出し修正・テスト75件全緑）
- [x] 2026-07-10 GA4計測タグを導入し本番稼働開始（環境変数駆動・測定ID設定済み・本番HTMLでタグ出力を実機確認）

- [x] 2026-07-10 x_poster 投稿閾値を実データ校正（DAILY_MIN_DIFF 100→50円、60日8,854ペアの分布から導出。docs/decisions.md 参照）
- [x] 2026-07-10 柵5箇所の由来調査＋コメント追記（deck_image `_v2`/1KB閾値=8983439由来、scraper sleep(2)=初回アップロードから根拠なし、rarity「レアレア」=実データ非実在の投機的登録と確定、_EX_PROP_IDS=一次情報源の記録なし）
- [x] 2026-07-10 fetch_guard.ALLOWED_PATH_PREFIXES を削除（未使用の検証機構ごと除去、テスト追従）
- [x] 2026-07-10 README.md の失効記述更新（3店舗同時検索→販売7店舗・デフォルト6店、ファイル構成→docs/ARCHITECTURE.md誘導）
- [x] 2026-07-10 前提監査（assumptions-check）＋棚卸し（inventory-audit）実施
- [x] 2026-07-10 未使用3ファイル削除（c859e17）・ルート空フォルダ削除・失効文書6枚を `_archive\docs_2026-03\` へ移動
- [x] 2026-07-10 test_fetch_guard の失効期待値を追従（67640e8）
- [x] 2026-07-10 MAMB方式へ移行（状態ファイル新設・ARCHITECTURE.md全面改訂・CLAUDE.md改訂）
