# TASKS — CardPriceChecker (TCGYM)

> MAMB運用のタスクボード。セッション開始時にまずこれを読み、終了時に更新する。
> 現在地の詳細は [docs/activeContext.md](docs/activeContext.md)、決定の経緯は [docs/decisions.md](docs/decisions.md)。

## 進行中

- [ ] **値動き2ガードのSupabase適用＋本番確認** — supabase_rpc_movers.sql（DROP＋4引数版CREATE）をSQL Editorで実行 → /api/movers・/api/buyback-movers 実機確認。**適用まではWebは旧ロジックのまま。x_poster の手動実行は適用前に行わない（per_card 引数エラーになる）**

## バックログ（優先度順）

- [ ] **買取収集復旧の実地確認** — 2026-07-11朝（JST7:00台）の tcg-collect-buyback 実行ログで「収集対象」が300件前後に回復しているか確認する。翌7/12はさらに増えるはず（未登録hot 215枚が7/11実行のupsertで追加されるため）。販売側 tcg-collect-prices（JST5:00）の「新規追加」ログも併せて確認
- [ ] **/api/packs の発売前パック表示方針の確認** — 今回の修正で「Wikiにカードリスト未掲載のパック」は get_pack_list から除外されるようになった（pack_scraper.py:93 の元コメントどおりの挙動に戻した）。サイトの最新弾リストから発売前パックが消えるのが意図に反する場合は、表示用と収集用の分離を検討（reviewer指摘）
- [ ] **騰落率の閾値再校正（ガード適用後）** — 2026-07-10校正値（DAILY_MIN_DIFF=50、BIGMOVE±2,000円/+100%/-50%）は偽陽性98%の汚染分布由来。ガード後の2倍級通過は実測週0.12件しかないため、X再開判断とセットで再校正 or 無投稿容認を決める
- [ ] **X新形式（bigmove）の再開判断** — schedule停止中。ガード後は本物のみになるが週0.12件でネタがほぼ出ない前提で、続ける価値を判断（撤退ゲートの再設計含む）
- [ ] **フェーズ3（生成側の根本治療）の設計** — 書き込み時名寄せ（中黒・全半角）／型番(code)保持／rarity空文字の隔離／状態(condition)・セール価格の分離（トレコロのキズあり×0.8問題・カーナベル状態別価格）／観測店舗集合のDB記録。スキーマ変更を伴うためまとめて設計（監査詳細は decisions.md 2026-07-10 とClaudeメモリ cardpricechecker-movers-audit）
- [ ] **サイトにプライバシーポリシー表記を追加** — GA4（アクセス解析）利用の明示。小タスク
- [ ] **値動きランキング2系統の残り（featured系）** — post_big_movers はRPC一本化済み（2026-07-10）。featured/post_daily_movers のPython集計（get_price_movers Python版）が残存。featured の allowed_names はRPCの top_n に乗らないため完全一本化は不可。X実験の結果次第で系統ごと消える可能性があるため実験後に判断
- [ ] **稼働店舗2店未満の日のランキング無音** — ガード②の構造的制約（該当日は販売ランキングが空になる）。6店体制では実害小。店舗縮小時に問題化したら「データ不足」表示等を検討

## 完了

- [x] 2026-07-10 **値動き検出に2ガード導入（共通店舗＋複数店確認）** — RPC店舗粒度化＋per_card引数・notify.py 2ガード＋基準日改良・x_poster RPC部分一本化・app.py days=3統一。60日リプレイで偽陽性52件全滅/本物1件生存（53/53期待一致）・reviewer監査2巡・pytest 98件全緑。SQL適用は進行中タスク参照
- [x] 2026-07-10 **騰落率パイプラインの前提監査（別セッション）完了** — 偽陽性率98%確定（53件中本物1件。店舗欠測26/在庫入替25/系列分裂1）。トレコロCB買取混入疑いは白（キズありSKU×0.8）。買取側は25日分で本物0件・在庫入替型ゼロ。詳細は decisions.md とメモリ

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
