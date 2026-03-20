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

## 補足: Git履歴にAPIキーが残る問題

今回の修正でコード上からはAPIキーを削除しましたが、Gitの過去のコミット履歴には値が残っています。

リポジトリが **公開（public）** の場合は、以下のいずれかの対応が必要です:

1. **APIキーを無効化して再発行する**（最も安全）
2. **`git filter-branch` や `BFG Repo-Cleaner` で履歴からキーを削除する**
3. **リポジトリを非公開（private）にする**

リポジトリが **非公開（private）** であれば、すぐに対応は不要ですが、将来公開する際には事前に対処してください。
