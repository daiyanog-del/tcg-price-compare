# 環境変数セットアップ手順

APIキーをソースコードから環境変数に移行したことに伴う、デプロイ時の設定手順です。

---

## 1. 環境変数の値を確認

以下の2つの環境変数を登録します。

| 環境変数名 | 説明 |
|---|---|
| `KANABELL_ES_ENDPOINT` | カーナベル Elasticsearch の接続先 URL（`https://…cloud.es.io`） |
| `KANABELL_ES_API_KEY` | カーナベル Elasticsearch の閲覧用キー |

値はカーナベルの販売サイト（`https://www.ka-nabell.com/ec/sell/yugioh`）がブラウザに配っている公開設定の
`TCG_PLATFORM_ES_ENDPOINT` / `TCG_PLATFORM_ES_API_KEY` と同じものです。サイトの作り直しで変わることがあります。
索引名（`yugioh-single-products`）はコードに固定しています。

> 2026-10-24 の新サイト移行までは `KANABELL_CLOUD_ID` / `KANABELL_API_KEY` / `KANABELL_INDEX`（旧サイトの `ec-cards`）を使っていました。今は読みません。

---

## 2. Render.com での登録手順

1. [Render.com ダッシュボード](https://dashboard.render.com/) にログイン
2. 対象のサービス（tcg-web）をクリック
3. 左メニューの **「Environment」** を選択
4. **「Add Environment Variable」** ボタンで以下を1つずつ追加:

```
Key:   KANABELL_ES_ENDPOINT
Value: （実際の値を貼り付け）

Key:   KANABELL_ES_API_KEY
Value: （実際の値を貼り付け）
```

5. **「Save Changes」** をクリック

> 既に登録済みの `HEALTH_CHECK_KEY` や `DISCORD_WEBHOOK_URL` はそのまま触らないでください。

---

## 3. デプロイ

環境変数を保存すると、Render.com が自動で再デプロイを開始します。

自動デプロイが無効の場合は、手動でデプロイしてください:
- **「Manual Deploy」** → **「Deploy latest commit」**

---

## 4. 動作確認

デプロイ完了後、ヘルスチェックAPIで全店舗が正常に動作しているか確認します。

```
https://あなたのドメイン/api/health?key=あなたのHEALTH_CHECK_KEY
```

確認ポイント:
- レスポンス全体の `"status"` が `"ok"` であること
- `"checks"` 内の各店舗（特にカーナベル）が `"status": "ok"` であること

エラーが出る場合:
- 環境変数の値が正しくコピーされているか確認
- Render.com のログ画面で `⚠️ カーナベル: KANABELL_ES_ENDPOINT / KANABELL_ES_API_KEY が未設定です` というメッセージが出ていないか確認

---

## ローカル開発環境の場合

```bash
cd tcg-web
cp .env.example .env
```

`.env` ファイルを開き、実際の値を記入してから起動してください:

```bash
python app.py
```

Docker を使う場合は `docker-compose.yml` が `.env` を自動で読み込みます:

```bash
docker compose up
```

> `.env` ファイルはGitにコミットしないでください。`.dockerignore` で除外済みです。

---

## 一人回し(solo play) kill-switch — `ENABLE_VISUAL_SOLO_PLAY`

一人回し機能は公式カード画像を使うため IP（知的財産）停止要請のリスクを抱えています。
要請が来た際にこの機能だけを即座に止め、相場・デッキ等のコア機能は無傷で動かし続けるための
運用スイッチが `ENABLE_VISUAL_SOLO_PLAY` です。

| 環境変数名 | 既定値 | 説明 |
|---|---|---|
| `ENABLE_VISUAL_SOLO_PLAY` | `1`（未設定時も ON） | `0` で一人回しを停止。それ以外（`1` 等）で有効 |

### 停止手順（kill-switch を引く）

1. Render.com → 対象サービス（tcg-web）→ **「Environment」**
2. `ENABLE_VISUAL_SOLO_PLAY` を **`0`** に設定（無ければ Add Environment Variable で追加）
3. **「Save Changes」** → 自動再デプロイ。即時反映したい場合は **「Manual Deploy」**

### 再開手順

- `ENABLE_VISUAL_SOLO_PLAY` を **`1`** に戻して Save。

### OFF（`0`）時の挙動

- `GET /solitaire` → **503** +「現在ご利用いただけません」案内ページ
- `POST /api/solitaire/replay` / `GET /api/solitaire/replay/<id>` → **404**
- トップページのナビから「一人回し」リンクが**非表示**

### OFF にしても影響しないもの（コアは無傷）

- 販売価格・買取価格の比較（`/`, `/card/<name>`, `/buy/`, `/api/deck`, `/api/search` 等）
- 価格推移・ランキング（`/api/price-history`, `/api/trending`, `/api/movers`）
- デッキPDF取り込み（`/api/parse-deck-pdf`）やカード画像/情報の各 API（`/api/card-image(s)`,
  `/api/card-info(s)`, `/api/meta` 等）— これらは一人回しとメインページの共用コアであり停止しません。

### スコープの境界（重要）

- このフラグは**一人回しのサーフェス（導線・ルート）のみ**を止めます。
- メインページのカード詳細・検索結果が表示する**公式カードアート画像は別管轄**です。
  公式画像の表示可否は別フラグ `OFFICIAL_IMAGE_DISPLAY`（app_settings / 管理画面トグル）で
  制御します。公式画像 IP の全面停止が必要な場合はそちらも併せて操作してください。

### 担保テスト

ON/OFF 両状態の挙動は `tests/test_solo_flag.py` で自動検証しています:

```bash
cd tcg-web
python -m pytest tests/test_solo_flag.py -v
```

---

## メルカリアンバサダー — `MERCARI_AFID`

- 「メルカリで見る」リンクにアンバサダー用の識別子（`afid=<値>`）を付ける。値はメルカリアンバサダーのリンク発行画面で確認できる共通ID（数字のみ）。
- 未設定・空: afid なし（従来のリンク・文言「メルカリで見る」）。設定あり: 文言「メルカリで見る（PR）」＋ `rel="sponsored noopener"`。
- 数字以外の値はWARNログを1回出して無視する（アフィリエイトなしで動作）。
- Render の環境変数に登録後、再デプロイ（再起動）で反映。実IDはこのドキュメントには書かない。

## 店舗検索キャッシュの置き場 — `SHOP_CACHE_REDIS_URL`

| 環境変数名 | 既定値 | 説明 |
|---|---|---|
| `SHOP_CACHE_REDIS_URL` | 未設定（ファイル） | Render Key Value の内部 URL（同じリージョン・オレゴン）。設定すると店舗検索の結果をそこに置く（デプロイで消えない）。営業メールの sales-mail-redis と相乗りするので、末尾はデータベース番号 `/1`（営業メールは `/0`）。例 `redis://red-d86rkfojo89c73d0artg:6379/1` |
| `SHOP_CACHE_KV_MAX_BYTES` | `12582912`（12MB） | Key Value の使用量（used_memory）がこれを超えたら、書き込みはファイルに戻す。相乗り先のキーを追い出さないための上限 |

- 設定するのは Web サービス（tcg-price-compare）だけ。定期実行（収集）は店舗検索キャッシュを使わない
- 未設定・届かない・上限超えでも検索は止まらず、従来のファイル（.cache・.cache_buy）で動く。ログに `[shop-cache-kv]` の警告が出る
- 外すときは環境変数を消して再デプロイするだけ
- ログ `[shop-cache-kv] 使用量 …・キーの数 db0=… db1=…` が10分ごと（書き込みがあるとき）に出る。db0 が営業メール・db1 がラボ。キーの中身は読まない

## 補足: Git履歴にAPIキーが残る問題

今回の修正でコード上からはAPIキーを削除しましたが、Gitの過去のコミット履歴には値が残っています。

リポジトリが **公開（public）** の場合は、以下のいずれかの対応が必要です:

1. **APIキーを無効化して再発行する**（最も安全）
2. **`git filter-branch` や `BFG Repo-Cleaner` で履歴からキーを削除する**
3. **リポジトリを非公開（private）にする**

リポジトリが **非公開（private）** であれば、すぐに対応は不要ですが、将来公開する際には事前に対処してください。
