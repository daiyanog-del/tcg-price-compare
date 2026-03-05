# TCG 価格比較 Web

遊々亭・カードラッシュ・トレコロCBの3店舗を同時検索し、カード価格を比較するWebアプリです。

## クイックスタート

```bash
pip install -r requirements.txt
python app.py
# → http://localhost:5000
```

## Docker でデプロイ

```bash
# ビルド & 起動
docker compose up -d

# ログ確認
docker compose logs -f

# 停止
docker compose down
```

## 機能一覧

| 機能 | 説明 |
|------|------|
| 🔍 3店舗同時検索 | 遊々亭・カードラッシュ・トレコロCBを同時スクレイピング |
| 📊 レアリティ別比較 | 店舗間でレアリティ表記を統一して比較 |
| 🔗 商品ページリンク | カード名クリックで各店舗の商品ページへ直接遷移 |
| 🚫 売り切れ表示 | 売切商品をグレーアウト+取消線で表示 (トグルで表示/非表示) |
| ⚡ リアルタイム進捗 | Server-Sent Events で検索状況をライブ表示 |
| 💾 キャッシュ | 15分間のキャッシュでサーバー負荷を軽減 |
| 🛡️ レートリミット | 連続検索を3秒間隔に制限 |
| 🗂️ ソート | 各列ヘッダークリックでソート切替 |
| 📱 レスポンシブ | スマートフォン対応 |

## ファイル構成

```
tcg-web/
├── app.py               # Flask サーバー (SSE / キャッシュ / レートリミット)
├── scraper.py            # スクレイピングエンジン
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── templates/
│   └── index.html        # フロントエンド (Single HTML)
└── .cache/               # 検索結果キャッシュ (自動生成)
```

## 設定

### scraper.py

| 変数 | 説明 | デフォルト |
|------|------|-----------|
| `DEBUG_DUMP_HTML` | 取得HTMLをファイル保存 | `False` |
| `STRICT_NAME_FILTER` | 名前フィルタ有効 | `True` |
| `EXCLUDE_SUPPLY` | サプライ品を除外 | `True` |
| `CACHE_ENABLED` | キャッシュ有効 | `True` |
| `CACHE_TTL_MINUTES` | キャッシュ有効期間(分) | `15` |

### 環境変数

| 変数 | 説明 | デフォルト |
|------|------|-----------|
| `PORT` | リッスンポート | `5000` |
| `FLASK_DEBUG` | デバッグモード | `1` |

## デプロイ先の例

### Render / Railway / Fly.io

いずれも Dockerfile によるデプロイに対応しています。

```bash
# Render の場合
# → Dashboard > New Web Service > Docker を選択
# → PORT=5000 を環境変数に設定

# Fly.io の場合
fly launch
fly deploy
```

### VPS (Ubuntu)

```bash
# サーバーに Docker をインストール後:
git clone <repo-url> && cd tcg-web
docker compose up -d

# リバースプロキシ (nginx) 設定例:
# server {
#     listen 80;
#     server_name tcg.example.com;
#     location / {
#         proxy_pass http://127.0.0.1:5000;
#         proxy_set_header Host $host;
#         proxy_set_header X-Real-IP $remote_addr;
#         proxy_buffering off;              # SSE に必要
#         proxy_read_timeout 120s;
#     }
# }
```

## 注意事項

- 各店舗のサーバーに負荷をかけないよう、リクエスト間隔を1秒以上空けています
- キャッシュにより同一検索は15分間再リクエストしません
- サイトのHTML構造が変更された場合、`scraper.py` のセレクタ更新が必要です
- `DEBUG_DUMP_HTML = True` でHTMLファイルを保存してデバッグできます
