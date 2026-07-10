# TASKS — CardPriceChecker (TCGYM)

> MAMB運用のタスクボード。セッション開始時にまずこれを読み、終了時に更新する。
> 現在地の詳細は [docs/activeContext.md](docs/activeContext.md)、決定の経緯は [docs/decisions.md](docs/decisions.md)。

## 進行中

（なし）

## バックログ（優先度順）

- [ ] **フェーズ3切替日の目視監視（2026-07-11〜12）** — min_price の意味変更（通常品最安化）初日は系列が上方シフトしうる。7/11・7/12 の /api/movers 上位とX投稿候補（JST20:07）に状態シフト由来の偽急騰が混ざっていないか確認。collection_runs への初回記録（7/11 JST5:00/7:00）も確認
- [ ] **買取収集復旧の実地確認** — 2026-07-11朝（JST7:00台）の tcg-collect-buyback 実行ログで「収集対象」が300件前後に回復しているか確認する。翌7/12はさらに増えるはず（未登録hot 215枚が7/11実行のupsertで追加されるため）。販売側 tcg-collect-prices（JST5:00）の「新規追加」ログも併せて確認
- [ ] **X再開後の初回投稿確認＋撤退ゲート判定（2026-09-15頃）** — schedule再開済み（毎日JST20:07）。初回自動実行のログと投稿内容を確認。ゲート: bigmoveインプ中央値100未満かつGA4でX流入なしなら凍結
- [ ] **値動きランキング2系統の残り（featured系）** — post_big_movers はRPC一本化済み（2026-07-10）。featured/post_daily_movers のPython集計（get_price_movers Python版）が残存。featured の allowed_names はRPCの top_n に乗らないため完全一本化は不可。X実験の結果次第で系統ごと消える可能性があるため実験後に判断
- [ ] **稼働店舗2店未満の日のランキング無音** — ガード②の構造的制約（該当日は販売ランキングが空になる）。6店体制では実害小。店舗縮小時に問題化したら「データ不足」表示等を検討

## 完了

- [x] 2026-07-11 **フェーズ3（生成側の根本治療）フル構成 完了** — P5観測店舗記録（collection_runs）/P6a誤補正停止/P1名寄せ＋tracked_cardsバックフィル（統合65G・改名58行・再ドライラン0件）/P3 rarity"(不明)"隔離/P4状態2値（min_price=通常品最安・min_price_any新列）＋P2 code列/P6b deck経路ignore_duplicates。DDL2本適用済み・pytest166件全緑・reviewer3巡。詳細 decisions.md 2026-07-11
- [x] 2026-07-10 **X自動投稿を再開（閾値±500円/±30%に再校正、候補A採用）** — ガード後60日実測で校正、テスト境界値追従、schedule復活。撤退ゲート2026-09-15頃
- [x] 2026-07-10 **プライバシーポリシーをGA4実態に追従** — 既存モーダルの「アクセス解析ツールを使用していません」を GA4利用の明示＋オプトアウト案内に更新。Web Push・フィードバックの項を新設、改定日追記
- [x] 2026-07-10 **パックリストの表示用/収集用を分離（include_emptyフラグ）** — 発売前パックがサイトから消える副作用を解消、キャッシュキーも用途別分離
- [x] 2026-07-10 **フェーズ3設計文書を作成** — docs/design-phase3-generation-side.md（名寄せの真の発生源=tracked_cards登録経路と判明、code横断キー不可の確認含む）
- [x] 2026-07-10 **値動き検出に2ガード導入（共通店舗＋複数店確認）＋Supabase適用・本番検証済み** — RPC店舗粒度化＋per_card引数・notify.py 2ガード＋基準日改良・x_poster RPC部分一本化・app.py days=3統一。60日リプレイで偽陽性52件全滅/本物1件生存（53/53期待一致）・reviewer監査2巡・pytest 98件全緑。SQLは管理API経由で適用し（旧3引数版DROP・4引数版のみ登録を確認）、本番RPC出力11行を生データ再計算と突き合わせ全件一致（全行が共通2店以上・同方向2店以上）
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
