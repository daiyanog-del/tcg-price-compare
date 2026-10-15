# TCG価格比較システム (TCGYM / カード相場) — アーキテクチャ

> 2026-07-10 全面改訂（前提監査に基づく実態反映）。旧版（2026-03-20、4店舗・Supabase導入前の姿）は `..\..\_archive\docs_2026-03\ARCHITECTURE_2026-03-20.md` 参照（ルートの _archive はgit管理外）。
> 変更したら日付を更新すること。重要な設計判断は [decisions.md](decisions.md) へ。

## 全体像

```
ブラウザ (templates/index.html SPA + PWA/Service Worker)
   │ HTTP / SSE
   ▼
Flask app.py (3,200行規模) ＋ solitaire_routes.py (Blueprint)
   ├── scraper.py ──────── 販売7店舗 / 買取5店舗 スクレイピング
   ├── meta_scraper.py ──── TCG PORTAL 環境Tier表（裏JSON API）
   ├── monitor.py ───────── ErrorTracker・Discord通知・ヘルスチェック
   ├── card_display.py 等 ─ カード情報・画像表示
   └── Supabase ─────────── 価格履歴・ログ・購読・未発売カード等の永続化
定期実行（サーバレス側）
   ├── Render Cron ──────── 販売価格収集 / 買取価格収集 / X未発売監視
   ├── GitHub Actions ───── カード名DB更新・X投稿・メトリクス・画像バックフィル・ygores同期
   └── ローカルPC ────────── yu-gi-oh.jp 未発売Watcher（DC IPブロック回避）
```

## 対応店舗

| 区分 | 店舗 | 備考 |
|---|---|---|
| 販売（scraper.py） | 遊々亭 / カードラッシュ / トレコロCB / カーナベル / カードラボ / まんぞく屋 / 駿河屋 | カーナベルのみ Elasticsearch API、他はHTML。デフォルト検索は駿河屋を除く6店 |
| 買取（scraper.py） | カードラッシュ / カーナベル / 遊々亭 / トレコロCB / カードラボ | カーナベルは ES API の sa_buying_price で403回避 |
| DB収集（collect_prices.py） | CI環境ではデータセンターIPブロックのため遊々亭・駿河屋・カードラボを除外（COLLECT_SKIP_SHOPS で上書き可） | |
| DB買取収集（collect_buyback.py） | 遊々亭を除外（BUYBACK_SKIP_SHOPS で上書き可） | |

## 主要モジュール

| ファイル | 責務 |
|---|---|
| app.py | 全エンドポイント（下表）。レートリミット3秒/IP、SSE配信 |
| solitaire_routes.py | 一人回しシミュレータ（`ENABLE_VISUAL_SOLO_PLAY` で kill-switch） |
| scraper.py | 店舗スクレイパー＋ファイルキャッシュ（.cache/ .cache_buy/、15分TTL） |
| collect_prices.py | 販売価格の日次収集（tracked_cards、hot/cold予算 DAILY_COLD_BUDGET=800枚・TIME_BUDGET_SEC=5h） |
| collect_buyback.py | 買取価格の日次収集 |
| watch_unreleased.py | yu-gi-oh.jp 監視→未発売カード抽出（Claude API使用、ローカル実行） |
| watch_x_unreleased.py | X(@YuGiOh_OCG_INFO) 監視→未発売カード抽出（Render Cron） |
| unreleased_extractor.py / admin_unreleased.py / unreleased_image_store.py / reconcile_unreleased.py | 未発売カードの抽出・承認画面・画像取込・発売済み照合 |
| x_poster.py + chart_renderer.py | 値動きランキングのX自動投稿＋グラフ生成 |
| unreleased_x_post.py / tools/post_unreleased_x.py | 承認された未発売カードのX自動投稿（予約テーブルで二重投稿防止、承認時＋3時間ごとの回収）。既定は無効 |
| unreleased_auto_approve.py / tools/auto_approve_report.py | 自動承認の判定（シャドーモード: 記録のみ）と集計。取込3経路が `extraction_raw.auto_approve_verdict` / `extracted_snapshot` を保存、承認時に `approval_snapshot` を保存 |
| unreleased_text_check.py / tools/build_text_check_dict.py / tools/text_check_calibrate.py / data/text_check_dict.json.gz | 未発売カードの効果テキストの誤字検証（決定的）。公式テキスト由来の辞書で3-gram・「」内の名前・書式を検査し、強/弱の疑義を返す。強い疑義は自動承認の条件（`text_suspect`）、結果は `extraction_raw.text_check`、管理画面に一覧と再検査ボタン |
| notify.py | Web Push 値下がり通知（-5% かつ -50円、同一購読20時間間隔） |
| meta_scraper.py | 環境Tier表（キャッシュ: Tier 3h / デッキ 6h、期限切れ後7日フォールバック、集計窓30→60日アダプティブ） |
| monitor.py | ErrorTracker（連続失敗をDiscord通知）・run_health_check |
| ygores_repository.py / sync_ygores.py | ygoresources カード情報のローカルキャッシュと差分同期 |
| rarity.py / name_normalize.py | レアリティ正規化（50種以上）・カード名正規化 |
| fetch_guard.py | 外部取得先ホワイトリスト（yu-gi-oh.jp ＋ pbs.twimg.com のみ許可） |
| deck_image.py / neuron_deck_parser.py | デッキ画像生成・ニューロン/PDF取込 |
| featured_pack.py | 新弾フィーチャー |

## API エンドポイント（グループ別）

| グループ | 主なパス |
|---|---|
| ページ | `/` `/card/<name>` `/buy/<name>` `/featured` `/solitaire` `/sitemap.xml` `/robots.txt` `/share-target` |
| 検索（SSE） | `/api/search` `/api/deck` `/api/deck-buy` `/api/buyback` `/api/status` |
| 検索補助 | `/api/suggest` `/api/validate` `/api/deck-estimate` `/api/card-rarities` `/api/wish-prices` `/api/wish-shop-totals` |
| 価格データ | `/api/price-history` `/api/trending` `/api/movers` `/api/buyback-movers` `/api/track` `/api/track-batch` |
| メタ・パック | `/api/meta` `/api/meta/deck` `/api/packs` `/api/packs/cards` `/api/featured` |
| カード情報・画像 | `/api/card-info` `/api/card-infos` `/api/card-image` `/api/card-images` `/api/card-types` `/api/card-image-proxy` |
| デッキ取込・出力 | `/api/import-neuron` `/api/parse-deck-pdf` `/api/deck-image` |
| Push・その他 | `/api/push/vapid-key` `/api/push/subscribe` `/api/push/unsubscribe` `/api/feedback` `/api/health` `/api/config` |
| 一人回し | `/api/solitaire/replay` (POST) `/api/solitaire/replay/<id>` (GET) |

※ `/api/deck` `/api/deck-buy` `/api/deck-estimate` は GET/POST 両対応（gunicorn**デフォルト**の `limit_request_line=4094` で長いGETが400になるため。明示設定ではない点と、ローカルFlask dev serverでは再現しない点に注意）

## データ永続化（Supabase）

スキーマ正本はリポジトリ内SQL: `featured_pack.sql` `unreleased_cards.sql` `ygores_cache.sql` `supabase_rpc_movers.sql`

| テーブル | 用途 |
|---|---|
| price_history / buyback_history | 販売・買取価格履歴（日次、保持90日 = constants.RETENTION_DAYS） |
| tracked_cards / pack_list | 収集対象カード・パック情報 |
| search_logs / deck_search_logs | 検索ログ（ランキングの永続化。旧「メモリ内のみ」から移行済み） |
| push_subscriptions | Web Push 購読 |
| feedback_reports | フィードバック |
| solitaire_replays | 一人回しリプレイ共有 |
| unreleased_cards / watched_pages / official_card_images / app_settings | 未発売カードパイプライン |
| ygores_cards / ygores_qa / ygores_blobs / ygores_sync_meta | ygoresources キャッシュ・同期メタ |
| tweet_log | X投稿ログ・インプレッション計測（x_poster.py / collect_x_metrics.py） |
| unreleased_x_posts | 未発売カードのX投稿状態（card_id 主キー。posting/posted/failed/rate_limited/unknown/skipped_*。投稿対象は unreleased_cards.approved_at の有無で判定）。スキーマは `unreleased_x_posts.sql` |
| featured_pack / pack_cards_cache | 新弾フィーチャー設定・パック収録キャッシュ（featured_pack.py） |

RPC（DB側集計関数）: `get_price_movers` / `get_buyback_movers`（値動きランキング）
※ 既知の課題: X投稿側（x_poster.py）はRPCを使わずPython側で別集計しており2系統併存。TASKS.md 参照

※ 2026-09-02 整数キー化: `price_history` の正本は `price_history_v2`（card_id/shop_id/rarity_id の整数キー、
card_dim/shop_dim/rarity_dim を辞書表として分離）。`price_history` は従来の列名で読める同名のビューとして残り、
既存の読み取り箇所・RPCは無変更。書き込みは RPC `upsert_price_rows(p_rows jsonb, p_ignore_duplicates boolean)`
に一本化（`price_persist.upsert_price_rows` 経由）。詳細・移行手順は `price_history_v2.sql` を参照。

## キャッシュ戦略

| 対象 | 保存先 | TTL |
|---|---|---|
| 販売検索結果 | `.cache/{md5}.json` | 15分 |
| 買取検索結果 | `.cache_buy/{md5}.json` | 15分 |
| 環境Tier表 / テーマデッキ | `.cache/meta/` | 3時間 / 6時間（期限切れ後7日フォールバック） |
| 検索ランキング / 見積 / movers / featured | メモリ | 5分 / 10分 / 1時間 / 10分 |
| カード情報 | メモリ（上限5,000件で全クリア） | なし |
| カード名DB | メモリ（起動時 data/cardnames_ja.json + cardnames_reading.json をロード） | なし |

## 定期実行ジョブ一覧（2026-07-10 時点）

| ジョブ | 実行基盤 | スケジュール |
|---|---|---|
| 販売価格収集 (collect_prices.py) | Render Cron `tcg-collect-prices` | 毎日 JST 5:00 |
| 新弾フィーチャー価格収集 (collect_featured.py) | Render Cron `tcg-collect-featured` | 毎日 JST 10:15（UTC 1:15）— 新弾（featured対象弾）収録カードの発売日朝価格を収集。対象弾が無い日は即終了 |
| 買取価格収集 (collect_buyback.py) | Render Cron `tcg-collect-buyback` | 毎日 JST 7:00 |
| X未発売監視 (watch_x_unreleased.py) | Render Cron `tcg-watch-x-unreleased` | 毎日 JST 23:00 |
| カード名DB更新＋発売済み照合 | GitHub Actions `update-cardnames.yml` | 毎週月曜 UTC 0:00（**cardnames_ja.json を自動コミット** → 編集前 git pull 必須の根拠） |
| X値動き投稿 (x_poster.py) | GitHub Actions `post-x.yml` | 毎日 JST 20:07 |
| 新弾フィーチャー投稿 | GitHub Actions `post-featured.yml` | 毎日 JST 21:07 |
| Xメトリクス収集 | GitHub Actions `collect-x-metrics.yml` | 15分ごと |
| 画像バックフィル＋未発売カードのX投稿回収 | GitHub Actions `backfill-images.yml` | 3時間ごと（画像回収→`tools/post_unreleased_x.py`。投稿は `UNRELEASED_X_POST_ENABLED=1` のときだけ） |
| ygores差分同期 | GitHub Actions `sync-ygores.yml` | 6時間ごと |
| yu-gi-oh.jp 未発売Watcher | ローカルPC（run_watcher_local.bat、タスクスケジューラ） | 日1回 |
| （停止済・手動フォールバックのみ） | GitHub Actions collect-prices.yml / collect-buyback.yml | Render Cron へ移行済み（理由は各yml冒頭コメント） |

※ Render Cron の3ジョブ名・時刻は 2026-07-10 に Render ダッシュボード実設定で確認（`tcg-collect-prices`=UTC20:00 / `tcg-collect-buyback`=UTC22:00 / `tcg-watch-x-unreleased`=UTC14:00）。リポジトリ内のコードには現れないため、Render側を変更したらこの表も更新すること。

## 環境変数（未発売カードのX自動投稿、2026-10-15〜）

| 変数 | 置き場所 | 内容 |
|---|---|---|
| `UNRELEASED_X_POST_ENABLED` | Render Web Service / GitHub Actions の Variables | `1` で有効。未設定=無効（既定） |
| `X_POST_DRY_RUN` | 同上（任意） | `1` で投稿せず本文をログに出すだけ（予約行も作らない） |
| `X_API_KEY` `X_API_SECRET` `X_ACCESS_TOKEN` `X_ACCESS_TOKEN_SECRET` | Render Web Service（承認時の投稿に必要）/ GitHub Actions secrets（回収。post-x.yml と同名） | X API v2 の OAuth1 認証情報 |
| `DISCORD_WEBHOOK_URL` | 既存 | 投稿失敗の通知 |

有効化の順序: ① `unreleased_x_posts.sql` を実行（`unreleased_cards.approved_at` 等の列追加。冪等。**コードのデプロイより先**。導入前の承認済みカードは approved_at が NULL のまま＝投稿対象外なので、有効化の直前に再実行する必要はない） ② コードをデプロイ ③ `X_POST_DRY_RUN=1` を付けて有効化しログで本文を確認 ④ DRY_RUN を外す。

## デプロイ構成

| 項目 | 値 |
|---|---|
| 本番 | Render Web Service `tcg-price-compare`（Docker、mainへのpushで自動デプロイ） |
| コンテナ | Python 3.12-slim、非rootユーザー |
| WSGI | gunicorn **1ワーカー × 8スレッド**（gthread、timeout 120秒、port 5000） |
| メモリ対策 | MALLOC_ARENA_MAX=2 |
| 通知 | Discord Webhook（エラー/復旧/フィードバック） |

## 技術スタック

Python 3.12 / Flask + flask-compress / gunicorn / requests + curl_cffi（WAF回避） / BeautifulSoup4 / supabase-py / tweepy（X API v2） / matplotlib + Pillow（グラフ・画像） / pywebpush / pdfplumber（デッキPDF） / anthropic（未発売カード抽出）
