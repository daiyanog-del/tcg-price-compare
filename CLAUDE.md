# CardPriceChecker プロジェクトルール（正本）

## MAMB運用（状態ファイルの儀式）
- セッション開始時: `TASKS.md` → `docs/activeContext.md` の順で読む
- セッション終了時: 両ファイルを更新する
- 重要な設計・運用の決定は `docs/decisions.md` に「いつ・何を・なぜ」で追記する
- 分業: 大きいバッチは implementer に委譲し reviewer が別視点で監査。小さい修正は司令塔が直接実装してよい
- ルートの `..\CLAUDE.md` と `..\AGENTS.md` はこのファイルへのポインタ（ルールの変更は必ずこのファイルに対して行う）

## リポジトリ運用
- 作業ディレクトリ = Gitリポジトリ = `tcg-web/`（2026-06-10に一本化。ルートはgit管理外なので恒久的な文書はここに置かない）
- 編集前に必ず `git pull` する（update-cardnames.yml が毎週月曜に cardnames_ja.json を自動コミットするため）
- コード変更後は コミット → push → Renderデプロイ確認 まで完了させる

## 巨大ファイルの扱い（2026-07-10 改訂）
- app.py（約3,200行）と templates/index.html（約216KB）は肥大化している。**新機能のロジックは可能な限り新規モジュール／static配下に書き、app.py・index.htmlには配線だけを足す**ことを検討する
- 切り出しを行う場合はコードの中身を変えない（移動と分割のみ。「ついでの改善」をしない）
- 旧「段階的分割（ついで方式）」規律は発動実績が乏しかったため廃止（経緯は docs/decisions.md）

## データ品質
- スクレイパーの「0件」と「取得失敗」は区別して扱う（compare_prices の status_out 参照）
- 価格履歴 recorded_at はJST日付。日付比較は必ず datetime.now(JST) 基準

## パラメータ
- チューニング値は実データから導出する。仮置きには `# TODO: calibrate from data` を付け、TASKS.md にも校正タスクを載せる
