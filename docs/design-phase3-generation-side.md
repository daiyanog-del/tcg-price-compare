# フェーズ3設計ドラフト: 価格データ生成側の根本治療

> 作成: 2026-07-10（設計担当エージェント、コード読解のみ・実装なし）
> 前提資料: docs/decisions.md 2026-07-10 エントリ群、Claudeメモリ cardpricechecker-movers-audit
> 対象コード（読解済み）: scraper.py / price_persist.py / collect_prices.py / collect_buyback.py /
> app.py / name_normalize.py / rarity.py / supabase_rpc_movers.sql / aggregations.py / notify.py / x_poster.py

---

## 0. 位置づけと設計原則

**フェーズ3は緊急対応ではない。** 消費側の2ガード（共通店舗＋複数店同方向）が本番稼働済みで、
偽陽性の98%は既に抑止されている。よってこれは「データ資産の質を上げる恒久投資」であり、
以下の原則で優先順位をつける:

1. **安い・可逆なものから**（列追加・新テーブルは可逆、UNIQUE制約変更は重い）
2. **保持90日を味方につける**（price_history / buyback_history は90日で自然入替するため、
   書き込み側だけ直せばバックフィルなしで90日後に全量が新品質になる。バックフィルは原則やらない）
3. **マスタ（tracked_cards）の汚染だけは自然消滅しない**（ここだけはバックフィル必須）
4. **表示用と変動検出用の要求分離**を設計に織り込む
   （表示=キズあり含む本当の最安、変動検出=状態を揃えた同一商品比較）

---

## 1. 現状のデータフロー（コード確認済みの事実）

```
━━━ 経路A: 夜間収集（Render Cron・毎日） ━━━━━━━━━━━━━━━━━━━━━━━━

  collect_prices.py main()
    │
    ├─ sync_latest_packs / sync_meta_decks / sync_regulation / sync_searched_cards
    │  sync_recipe_decks / sync_theme_wiki_cards / sync_remake_themes / sync_trending_cards
    │      │  カード名は normalize_card_name() のみ通す
    │      │  （ダッシュ統一＋全角英数半角化＋．！のみ。中黒・cardnames照合なし）★P1の根
    │      ▼
    │  tracked_cards (UNIQUE card_name, active, last_collected_at)
    │      ↑ app.py の _track_card_async / _track_cards_async も書く
    │        （こちらは _correct_cardname 済みの名前 → 二重系列の温床）
    │
    ├─ check_shop_availability() … 結果は print と Discord レポートのみ。DB非記録 ★P5
    ├─ カーナベルAPIキー未設定時の当日除外 … 同じく非記録 ★P5
    │
    ├─ カードごとに compare_prices(card_name, available_shops)
    │      │  scraper.py 各店舗が返す item:
    │      │  {shop, name, rarity, code, condition, price, stock, sold_out, url, image}
    │      │   - トレコロCB:  condition="中古キズあり"（正価×0.8のSKU）
    │      │   - カーナベル:  condition="状態:SA/B/C/D"（状態別に複数行）
    │      │   - 遊々亭:      condition="セール"（saleクラス検出時）
    │      │   - カードラッシュ: condition="〔状態…〕"抽出
    │      ▼
    │  price_persist.build_min_price_rows(card_name, results, today)
    │      │  (shop, rarity) キーで min(price) に畳む
    │      │  → code / condition / name(商品名) をここで破棄 ★P2 ★P4
    │      │  → rarity 抽出失敗行は rarity="" バケットに合流 ★P3
    │      │  → sold_out / price<=10 は除外
    │      ▼
    │  price_persist.upsert_price_rows()
    │      UNIQUE(card_name, shop, rarity, recorded_at) ON CONFLICT 全列上書き
    │      = last-write-wins ★P6b
    │      ▼
    │  price_history {card_name, shop, rarity, min_price, recorded_at}  保持90日
    │
    └─ cleanup_old_data() … 90日超を削除

━━━ 経路B: ユーザー検索の即時反映（Flask・fire-and-forget） ━━━━━━━━━

  /api/search ──┐  card_name は _correct_cardname() で補正済み
  /api/deck  ──┤  （fuzzy衝突時は候補リスト[0]決め打ち ★P6a。
                │    /api/deck はトレコロ・カーナベルを max_pages=1 に制限
                │    → 網羅性の低い min が夜間の網羅的 min を上書きしうる ★P6b）
                ▼
  _persist_scrape_async() → 経路Aと同じ build_min_price_rows → upsert_price_rows
                            （観測店舗集合はユーザー選択店舗のみ。これも非記録 ★P5）

━━━ 買取（参考: 販売と相似だが別系） ━━━━━━━━━━━━━━━━━━━━━━━━━━

  collect_buyback.py … (shop, rarity) で max(price) に畳む。code/condition 同様に破棄。
  ※ upsert ではなく素の insert（UNIQUE制約の有無はスキーマ未確認。
     同日再実行で重複行が入る可能性あり — フェーズ3実装時に要確認）

━━━ 読み出し（消費側） ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  /api/price-history … card_name 完全一致（補正後）。rarity は読み出し時 normalize_rarity
  get_price_movers / get_buyback_movers RPC … 2ガード稼働済み（店舗粒度）
  notify.py get_price_drops … 2ガード稼働済み
  x_poster select_big_movers … fuzzy_key による読み出し側 dedup（変動率大を残す=危険側）
  aggregations.daily_min_by_lowest_rarity … 代表レアリティ選定。rarity="" も候補に参加
```

---

## 2. 問題別の対策オプション

凡例 — 工数: **S**=半日以内 / **M**=1〜2日 / **L**=3日以上（テスト・検証込みの感覚値）

### P1: 書き込み時名寄せゼロ（表記分裂 6.4%）

**根本原因（コードで確認）**: 分裂の発生源は price_history への書き込み時ではなく
**tracked_cards への登録時**。経路Bは `_correct_cardname` 済みだが、経路Aの sync 系
（特に trending_scraper 由来＝店舗サイトの表記そのまま、Wiki・規制DB由来）は
`normalize_card_name`（ダッシュ・全半角のみ）しか通らない。tracked_cards に
「ブラックマジシャン」と「ブラック・マジシャン」が別行で入り、夜間収集がそれぞれを
毎日収集して price_history に別系列を生む。

| 案 | 内容 |
|---|---|
| **A1（推奨候補）** | **canonicalize 共通関数の新設＋tracked_cards 入口全面適用＋tracked_cards バックフィル**。`name_normalize.py` に「cardnames_ja.json 照合つき正規化」（app.py の `_correct_cardname` 相当を Flask 非依存で切り出し）を追加し、collect_prices.py の全 sync 系と `_insert_new_cards` の直前、app.py の `_track_card_async` 系に適用。既存 tracked_cards の分裂行は fuzzy_key でグルーピングして正式名に統合（active は OR、last_collected_at は MAX）。 |
| A2 | A1 ＋ **price_history 過去90日分のバックフィル rename**。UNIQUE 衝突時（正規名側に同日行が既にある）は min(min_price) にマージ。系列連続性を即日回復。 |
| A3 | 何もしない（読み出し側 fuzzy 集約に将来寄せる）。 |

- **(a) 変更範囲**: A1 = name_normalize.py（関数追加・cardnames_ja.json の読込を Flask 外でも可能に）、collect_prices.py（sync 系 8箇所）、app.py（切り出した関数への差し替え）、tracked_cards への一回きりの統合スクリプト（tools/ 配下）。**price_history のスキーマ変更なし**。A2 はさらに price_history 更新バッチ。
- **(b) 過去データ互換**: A1 = 切替日以降、新規書き込みは正規名系列に合流。旧・非正規系列は追記が止まり90日で自然消滅。**移行期間中（最大90日）はグラフ上で旧系列が「途切れた線」として残る**（/api/price-history は card_name 完全一致なので、非正規名系列は正規名での照会に出てこない＝見た目はむしろ現状と同じ）。A2 は移行期間ゼロだが衝突マージのセマンティクス（min合成）が「当時の実観測」と厳密には異なる行を作る。
- **(c) リスク**: canonicalize の誤写像。**fuzzy_key は中黒とハイフンを両方剥がすため、実在の別カード（例: E・HERO と E-HERO は別カード群）が同一キーに衝突する**。→ P6a（衝突時は補正しない）を先に直すことが前提条件。cardnames_ja.json は週次更新のため、発売直後カードは照合に載らず素通しになる（現状と同じ挙動なので悪化はしない）。
- **(d) 工数**: A1 = **M**。A2 = A1 + M（バックフィルは90日ぶんを日付分割で流す。Render無料相当なのでローカル実行推奨）。A3 = 0。
- **やらない場合の残害**: movers への影響は2ガードでほぼ抑止済み（監査で系列分裂型は53件中1件）。残るのは (1) 価格グラフの系列が薄く分裂したまま（同一カードのデータが2系列に割れて日次点が疎になる）、(2) 簡易見積り(DB相場)のカバー率低下、(3) tracked_cards の収集予算（DAILY_COLD_BUDGET 800枚）を重複カードが二重消費。6.4% という規模を考えると**恒久投資としては本命**。A2 の即日回復は、A1 だけでも90日で同じ状態に到達するため費用対効果が薄い。

### P2: 型番(code)の破棄（別イラスト・別収録の混合）

| 案 | 内容 |
|---|---|
| A | **系列キーに code を昇格**。price_history に code 列追加、UNIQUE を (card_name, shop, rarity, **code**, recorded_at) に変更、build_min_price_rows の畳みキーに code を追加。 |
| **B（推奨候補）** | **属性列として保存（キー不変）**。code 列を追加するが UNIQUE と畳みキーは現状のまま。(shop, rarity) の min を作った item の code を行に添えて保存する（「その日その店のそのレアリティの最安は、どの収録だったか」の監査情報）。 |
| C | 何もしない。 |

- **(a) 変更範囲**: A = スキーマ（列追加＋**UNIQUE 制約作り直し**）、price_persist.py、読み出し側は当面無変更でも互換（後述）。B = 列追加のみ＋price_persist.py の数行。C = なし。
- **(b) 過去データ互換**: A = 旧行は code=NULL。movers RPC は (card_name, rarity, shop) 粒度で MIN するため、**code で行が分かれても日次最安の集計結果は不変**（読み出し互換は保たれる）。ただしガード①の店舗ペア照合は shop_daily の MIN 後なので影響なし。B = 完全互換（読み出しは code 列を無視すればよい）。
- **(c) リスク**: A = **行数増加**。同一 (shop, rarity) 内に複数収録があるカード（人気の再録カード）で行が数倍化し、Supabase 無料枠の容量と PostgREST 1000行分割取得の負荷が増える。増加率はやってみないと不明（**未検証の前提**: 監査データから (shop, rarity) 内の code 多様度を事前集計可能）。さらに code の表記も店舗間でゆれる（トレコロはURL断片、カーナベルはカテゴリ略称 category3_abbr、他店は型番そのもの）ため、**code は店舗横断キーとして使えない**——A案でも実質「店舗内の系列分離」にしかならない。B = min の代表 code だけなので混合自体は解消しない。
- **(d) 工数**: A = **L**（UNIQUE 変更・on_conflict 文字列の同期切替・全読み出しの回帰確認）。B = **S**。
- **やらない場合の残害**: 別収録の在庫入替による日次最安の飛びは残るが、これはまさに2ガードが抑止している「在庫入替型」（監査53件中25件→ガードで全滅）。表示用最安値の要件（一番安い出品を見せる）にはむしろ現状の混合 min が合致。**A案は費用対効果が悪い**。B案は将来の監査・調査（「この急変はどの収録の入替か」）を1クエリで答えられるようにする安価な保険。

### P3: rarity 空文字バケット

| 案 | 内容 |
|---|---|
| **A（推奨候補）** | **書き込み時に rarity="" を "(不明)" ラベルへ明示**＋読み出しの代表レアリティ選定（aggregations.daily_min_by_lowest_rarity / notify._representative_rarity）で "(不明)" を候補から除外（他に候補がない場合のみフォールバック採用）。movers RPC は変更不要（"(不明)" 同士でしか比較されないため誤比較は起きない — 現状の "" も同じ性質。問題は代表選定で "" が最安代表に選ばれ、正体不明の疑似系列がグラフ・通知の顔になること）。 |
| B | 上流改善のみ: rarity.py の未知表記 WARN ログを定期レビューし aliases を追補、トレコロ買取の rarity 空を商品名から推定。 |
| C | 何もしない。 |

- **(a) 変更範囲**: A = price_persist.py（1行）＋aggregations.py＋notify.py（選定ロジックに除外条件）。スキーマ変更なし。B = rarity.py のみ、継続運用タスク。
- **(b) 過去データ互換**: A = 旧 "" 行と新 "(不明)" 行が90日間併存し、**"" 系列と "(不明)" 系列が切替日で分断される**。ただし分断されるのは元々正体不明のバケットなので実害は小さい。気になるなら `UPDATE price_history SET rarity='(不明)' WHERE rarity=''` の一回きりバックフィルは UNIQUE 衝突の可能性が同日 "" と "(不明)" の共存時のみで、切替直後に流せばほぼ衝突しない（要衝突時 min マージ）。
- **(c) リスク**: 低。表示側（index.html のレアリティバッジ・色）が "(不明)" を未知スラグとして受けるがグレー表示にフォールバックする設計（rarity.color_of は未知=グレー）。
- **(d) 工数**: **S**。
- **やらない場合の残害**: "" バケットに別商品が合流した疑似系列が、代表レアリティ選定で「最安」として選ばれ続ける（グラフと push 通知の基準系列になる）。movers は2ガードで大半抑止済み。

### P4: 状態（キズあり・状態別・セール）混入

**要求の分離が核心**: 表示用の「最安値」はキズあり込みの本当の最安が正しい。
変動検出用の系列は状態を揃えないと「キズあり在庫の出現・消滅」が価格変動に見える。
監査でトレコロの ×0.8 SKU 混入は「白（仕様問題）」と判定済みだが、系統的バイアスとして残存。

| 案 | 内容 |
|---|---|
| A | **系列キーに condition を昇格**。condition 列追加＋UNIQUE を (card_name, shop, rarity, condition, recorded_at) に変更。movers/notify は condition='通常' 系列のみ、表示は全 condition の min。 |
| **B（推奨候補）** | **2値記録（キー不変）**。列を1本追加し、`min_price` = **通常品（condition が "-" / セール除外可否は分岐点④）の最安**、新列 `min_price_any` = **キズあり・状態難含む本当の最安**とする。build_min_price_rows で condition を見て2つの min を並行集計。movers・notify・グラフは min_price（状態を揃えた系列＝自動的に品質向上）、簡易見積り等「買うならいくら」の文脈は min_price_any。 |
| C | 書き込み時にキズあり・状態C/D を min 候補から単純除外（列追加なし）。 |

- **(a) 変更範囲**: A = スキーマ（UNIQUE 変更）＋price_persist＋movers RPC（WHERE 追加）＋aggregations/notify＋表示系。B = **列追加のみ**＋price_persist.py＋（任意で）見積り系の読み出し1箇所。C = price_persist.py のみ。
- **(b) 過去データ互換**: B = **min_price の意味が切替日に変わる**。トレコロのキズあり SKU が最安だったカードは切替日に見かけ+25%（×0.8 の解除）の段差が出る。ガード①は「両日とも記録がある店舗」の同一列比較なので、**切替日をまたぐ movers 窓で偽の急騰が出うる**。対策: 切替日を decisions.md に記録し、切替後2日間は movers/X投稿/push通知を目視確認 or 一時停止（movers 窓は直近2 distinct 日なので影響は最大2日で抜ける）。旧行の min_price_any は NULL（=min_price と同値とみなす読み出し規約）。A も同種の段差＋移行がさらに重い。C は段差に加えて「本当の最安」を恒久喪失。
- **(c) リスク**: condition の判定精度。カーナベルは SA を「通常」とみなすか（SA=傷なし相当。SA のみ通常扱いが妥当）、遊々亭「セール」を通常に含めるか（セールは実在の販売価格であり、変動検出的にも本物の値下げ。**除外しない方が妥当**だが分岐点として提示）。カードラッシュの〔状態…〕表記の網羅性は未検証。判定は scraper が既に採取済みの condition 文字列を price_persist 側で分類するだけなので、分類表を1箇所（constants か rarity.py 同様の小モジュール）に置く。
- **(d) 工数**: A = **L**、B = **M**（分類表＋2値集計＋テスト。読み出しは当面無変更でも成立）、C = **S**。
- **やらない場合の残害**: 系統的バイアス（トレコロ×0.8、カーナベル B/C/D、状態難の安値出品）が min_price に乗り続ける。movers の偽陽性は2ガードで大半抑止済みだが、**「状態難あり品の安値出品を急落と誤認するリスク」は bigmove 実験で既知として残置中**（decisions.md 2026-07-10）。また簡易見積り・グラフの水準が本来の相場より低めに歪む。X再開・通知品質を上げたいなら価値が高い。

### P5: 観測店舗集合の非記録（「未観測」と「在庫0」の混同）

| 案 | 内容 |
|---|---|
| **A（推奨候補・2026-07-10実装済み）** | **collection_runs テーブル新設（ラン粒度）**。1回の収集実行につき1行: {run_type('prices'/'buyback'/'instant')、attempted_shops[]、available_shops[]、skipped_shops（店舗→理由のJSONB）、shop_stats（per-shop ok/empty/error JSONB）、started_at、finished_at、success/fail/cutoff_count、saved_rows}。collect_prices / collect_buyback の終了時に書く。※実装時判断: 経路B（即時upsert `_persist_scrape_async`）への記録は、検索1件ごとの呼び出しで「ラン粒度なら年間千行未満」の前提が成立せず、観測集合も「ユーザー選択店舗のみ」で検証価値が低いため**見送り**（'instant' はCHECK制約への前方互換予約のみ）。 |
| B | カード×店舗×日の試行結果を全記録。 |
| C | 現状維持（Discord レポートのみ）。 |

- **(a) 変更範囲**: A = 新テーブル1つ（既存テーブル無変更・完全可逆）、collect_prices.py / collect_buyback.py / app.py に書き込み数行。消費側は当面変更なし（将来 movers の「無音日はデータ不足と表示」等に使える）。
- **(b) 過去データ互換**: 完全互換（追加のみ）。過去の欠測は復元不能だがそれは現状と同じ。
- **(c) リスク**: ほぼなし。書き込み失敗は warning で握る（収集本体を止めない）。行数はラン粒度なら年間千行未満。B 案は行数爆発（1,200カード×6店×365日）で無料枠に不適・却下推奨。
- **(d) 工数**: A = **S**。
- **やらない場合の残害**: 今後のあらゆる検証（P1〜P4 の効果測定、ガードのチューニング、閾値再校正）で「その日その店は観測されていたのか」を毎回 Render ログ発掘で復元することになる。**他の全対策の検証土台**なので、やらない選択は事実上フェーズ3全体の検証を高コスト化する。

### P6a: _correct_cardname の fuzzy 衝突時 [0] 決め打ち

- **対策（実質1案）**: `_cardnames_fuzzy[q_fuzzy]` が複数候補のとき補正しない（原名をそのまま返す）＋ WARN ログ。完全一致（`name in _cardnames_set`）は従来どおり素通し。
- **(a) 変更範囲**: app.py の `_correct_cardname` 数行（P1 で name_normalize.py に切り出すならそこ）。
- **(b) 互換**: 挙動が変わるのは「衝突キーに fuzzy でしか一致しない入力」のみ。誤補正が消える代わりに補正されないケースが増える（検索は confirmed フラグ経路で通せる。現状と同じ 404 UX）。
- **(c) リスク**: 低。E・HERO/E-HERO 型の実在衝突で誤ったカードの系列に書き込む事故を塞ぐ。
- **(d) 工数**: **S**。**P1 の前提条件**（canonicalize を書き込み経路に使う以上、衝突時の安全側動作が必須）。
- **やらない場合の残害**: 低頻度だが「別カードへの誤合流」という最悪種の汚染（分裂より修復困難）が残る。

### P6b: 即時 upsert と夜間収集の last-write-wins 競合

- **問題の実体（コードで確認)**: /api/deck はトレコロ・カーナベルを max_pages=1 に制限して
  スクレイプするため最安を取り逃がした高めの min を作ることがあり、それが夜間の網羅的な
  min を同日上書きしうる。/api/search はユーザー選択店舗のみ＝店舗網羅性が狭い。

| 案 | 内容 |
|---|---|
| A | 即時 upsert を `ignore_duplicates=True`（既存行があれば書かない）に変更。夜間収集を正とし、即時系は「その日まだ行がないときだけ」埋める。 |
| **B（推奨候補）** | 経路別に分ける: **/api/deck（1ページ制限あり＝品質劣位）のみ ignore_duplicates 化**、/api/search（全ページ・ただし選択店舗のみ）は現状維持。 |
| C | 現状維持。 |

- **(a) 変更範囲**: price_persist.upsert_price_rows に mode 引数を足し、app.py の呼び出し2箇所で出し分け。スキーマ変更なし。
- **(b) 互換**: 完全互換（書き込みポリシーのみ）。
- **(c) リスク**: A は「夜間収集後に実際に値下がりした最新価格」を捨てる（鮮度低下）。B はその損失を品質劣位経路に限定。日次 min 系列という設計上、日内の鮮度は元々売りではない。
- **(d) 工数**: **S**。
- **やらない場合の残害**: deck 検索由来の高め min が日次値をたまに汚す程度。movers はガードで、グラフは日単位ノイズ1点で実害小。**優先度最低で可**。

---

## 3. 依存関係と実施順序の推奨

```
ステップ0  P5-A  collection_runs 新設          … 全対策の検証土台。最安・完全可逆
ステップ1  P6a   fuzzy衝突の安全側化           … P1 の前提条件
ステップ2  P1-A1 canonicalize＋tracked_cardsバックフィル … 本命。効果測定にP5を使う
ステップ3  P3-A  rarity "(不明)" 隔離           … 小粒。P1 と同一デプロイでも可
ステップ4  P4-B  condition 2値記録              … スキーマ列追加。切替日の段差管理が必要
ステップ5  P2-B  code 属性列                    … P4 と同じマイグレーションに同乗させると1回で済む
ステップ6  P6b   deck経路の ignore_duplicates   … 任意。いつでも
```

依存の根拠:
- **P5 が先**: P1〜P4 の効果測定（後述 §5）は「観測店舗集合を固定した比較」が必要。
  記録がないと検証のたびに今回の監査と同じ手作業になる。
- **P6a → P1**: canonicalize を書き込み経路（毎晩1,000枚超が通る）に乗せる前に、
  衝突時 [0] 決め打ちを塞がないと、低頻度の誤合流を大量自動生産する。
- **P4 と P2 の列追加は1回のマイグレーションに同乗**: どちらも price_persist.build_min_price_rows
  の同じ箇所を触る。別々にやると同一関数の2回改修＋2回の列追加になる。
- **P1 と P4 は独立**（名寄せはカード名軸、状態は行の中身）なので並行可能だが、
  効果測定を切り分けるため**切替日はずらす**ことを推奨（グローバルルールの
  「独立した最適化は全部入れてから計測」の例外: これは最適化ではなくデータ生成の
  意味論変更であり、切替日の段差がそれぞれ movers に出るため、監査可能性を優先）。

---

## 4. スキーマ変更を伴うものの移行戦略

対象: P4-B（min_price_any 列）、P2-B（code 列）、P5-A（新テーブル）。
※ 推奨案の範囲では **UNIQUE 制約の変更は発生しない**（P2-A / P4-A を選んだ場合のみ発生。
その場合は「新 UNIQUE index を CONCURRENTLY で作成 → on_conflict 文字列をコード側で切替 →
旧制約 DROP」の3段階＋upsert 呼び出し2箇所（price_persist 経由に集約済み）の同期切替が必要）。

手順（列追加系）:
1. **列追加が先、コードデプロイが後**（PostgREST は存在しない列への書き込みでエラーになるため。
   逆順だと夜間収集が全滅する。買取激減障害の教訓と同型）。
   `ALTER TABLE price_history ADD COLUMN min_price_any int, ADD COLUMN code text;` は
   NULL許容・デフォルトなしなら即時完了（テーブル90日分・数十万行想定でもロックは瞬時）。
2. 書き込み側（price_persist.py）を新列対応でデプロイ。**読み出し側は触らない**
   （新列は select していないので無風）。
3. 切替日を decisions.md に記録。movers/notify/X は切替日から2日間、目視 or 一時停止（P4のみ。
   §2 P4-(b) の段差問題）。
4. **バックフィルは行わない**（90日で自然入替）。読み出し規約として
   「min_price_any IS NULL → min_price と同値」を、min_price_any を使い始める時点のコードに書く。
5. ロールバック: コードを旧版に戻すだけ（列は残しても無害。NULL のまま）。

tracked_cards バックフィル（P1、これだけは必須のデータ移行）:
1. ドライラン: fuzzy_key グルーピングで統合候補一覧を出力し、**ユーザーが目視承認**
   （衝突キー＝複数正式名にまたがるグループは統合対象から除外して一覧報告）。
2. 統合実行: 残す行=正式名。消す行の active/last_collected_at をマージ。
   price_history の過去行は触らない（90日で消える）。
3. 実行はローカルから（Render cron の時間帯 JST5:00/7:00 を避ける）。

---

## 5. 検証方法（対策ごと）

| 対策 | 測定方法 |
|---|---|
| P5 | デプロイ翌日から collection_runs に行が入ること。Discord レポートと突合して一致。以降は他対策の分母として使用 |
| P6a | 衝突キー一覧（cardnames_ja.json 全件の fuzzy_key 衝突集計、ローカルで1回）を出し、WARN ログで実際に踏んだ件数を観測 |
| P1 | 監査と同じ手法で分裂率を再測定: price_history の distinct card_name を fuzzy_key でグルーピングし、`recorded_at >= 切替日` の行だけで分裂率を出す → **6.4% → ほぼ0%** を確認。tracked_cards 統合の前後件数（1,243件 → 統合後）を記録 |
| P3 | `rarity='' AND recorded_at >= 切替日` の新規流入が0件。"(不明)" 系列が代表レアリティに選ばれた件数の減少（aggregations にログ仕込み or SQL 集計） |
| P4 | 切替前後7日で、トレコロ「キズあり由来 min」混入率（監査スクリプト再利用: 正価×0.8 パターン検出）とカーナベル B/C/D 由来 min の比率を before/after 比較。movers 60日リプレイ（既存の監査リプレイ資産を流用）で偽陽性が増えていないこと |
| P2 | code 列の非NULL率（店舗別）。将来の急変調査で「同一 (shop,rarity) の code 切替と価格飛びの同時発生」を1クエリで判定できることをサンプル1件で実証 |
| P6b | deck 検索が多い日の price_history 日内上書き回数（last-write-wins の発生率）を before で1回測ってから判断してもよい（発生が稀なら C 案=現状維持で確定） |

共通: 各切替日を decisions.md に記録し、pytest（現98件）に price_persist の新ロジックの
ユニットテストを追加。movers の60日リプレイ環境は今回の監査で作った資産を再利用する。

---## 6. ユーザーが決めるべき分岐点

1. **P1 の A1 か A2 か**: 90日待てるなら A1（推奨）。グラフの系列連続性を即日回復したいなら
   A2（＋バックフィル工数 M と min 合成の意味論を受け入れる）。
2. **P2 をやるか**: 推奨は B（属性列・S工数）まで。A（キー昇格・L工数）は2ガード稼働後の
   残存リスクに対して過剰投資の疑いが強い。**C（やらない）も十分合理的**。
3. **P4 をやるか、やるなら B か**: X再開・通知品質を重視するなら B。当面 X 凍結・通知利用者が
   少ないなら後回しも可（ただし90日ルールにより、早く始めるほど早くデータ資産が揃う）。
4. **P4-B の「通常品」の定義**: カーナベル SA を通常に含めるか（推奨: 含める）、
   遊々亭セールを通常に含めるか（推奨: 含める＝セールは本物の価格変動）。
5. **P4 切替日の movers/通知の扱い**: 2日間の一時停止か、目視監視か。
6. **tracked_cards 統合の承認方式**: ドライラン一覧の全件目視か、衝突なしグループの自動承認か。
7. **P6b**: 発生率を測ってから決めるか、S工数なので測らず B 案を入れるか。

## 7. 概算工数の合計（推奨構成: P5-A + P6a + P1-A1 + P3-A + P4-B + P2-B）

S×4 + M×2 ≒ 実働4〜6日相当。分割デプロイ5回（ステップ0/1 は同日可、2/3 は同日可、4/5 は同日可）。
最小構成（P5-A + P6a + P1-A1 のみ）なら実働2〜3日で、監査で確定した被害の最大要因
（系列分裂の生産ラインと誤合流リスク）は止まる。
