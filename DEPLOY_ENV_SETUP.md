# 環境変数セットアップ手順

APIキーをソースコードから環境変数に移行したことに伴う、デプロイ時の設定手順です。

---

## 1. 環境変数の値を確認

以下の3つの環境変数を登録します。

| 環境変数名 | 説明 |
|---|---|
| `KANABELL_CLOUD_ID` | カーナベル Elasticsearch の接続先識別子 |
| `KANABELL_API_KEY` | カーナベル Elasticsearch の認証キー |
| `KANABELL_INDEX` | カーナベル Elasticsearch のインデックス名（通常は `ec-cards`） |

値は `.env` ファイルまたはチーム内の秘密情報管理ツールから取得してください。

---

## 2. Render.com での登録手順

1. [Render.com ダッシュボード](https://dashboard.render.com/) にログイン
2. 対象のサービス（tcg-web）をクリック
3. 左メニューの **「Environment」** を選択
4. **「Add Environment Variable」** ボタンで以下を1つずつ追加:

```
Key:   KANABELL_CLOUD_ID
Value: （実際の値を貼り付け）

Key:   KANABELL_API_KEY
Value: （実際の値を貼り付け）

Key:   KANABELL_INDEX
Value: ec-cards
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
- Render.com のログ画面で `⚠️ カーナベル: KANABELL_CLOUD_ID / KANABELL_API_KEY が未設定です` というメッセージが出ていないか確認

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

## 補足: Git履歴にAPIキーが残る問題

今回の修正でコード上からはAPIキーを削除しましたが、Gitの過去のコミット履歴には値が残っています。

リポジトリが **公開（public）** の場合は、以下のいずれかの対応が必要です:

1. **APIキーを無効化して再発行する**（最も安全）
2. **`git filter-branch` や `BFG Repo-Cleaner` で履歴からキーを削除する**
3. **リポジトリを非公開（private）にする**

リポジトリが **非公開（private）** であれば、すぐに対応は不要ですが、将来公開する際には事前に対処してください。
