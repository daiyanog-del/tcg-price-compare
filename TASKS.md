# TASKS — CardPriceChecker (TCGYM)

> MAMB運用のタスクボード。セッション開始時にまずこれを読み、終了時に更新する。
> 現在地の詳細は [docs/activeContext.md](docs/activeContext.md)、決定の経緯は [docs/decisions.md](docs/decisions.md)。

## 進行中

（なし）

## バックログ（優先度順・2026-07-10 前提監査より）

- [ ] **x_poster 投稿閾値の実データ校正** — DAILY_MIN_DIFF=100円 / DAILY_MIN_PCT=10% が `TODO: calibrate from data` のまま本番稼働中。price_history の日次変動分布を集計して置き直す（最小コストで効果大）
- [ ] **値動きランキングの2系統併存の解消検討** — Web表示=Supabase RPC / X投稿=Python集計で結果が食い違いうる。集計窓も days=2（app.py販売）と days=3（買取・X側）で不一致。RPC一本化の可否と影響範囲を先に列挙してから判断
- [ ] **柵（理由未記録の実装）の由来調査＋コメント追記** — git blame で辿り、判明分をコードコメント化: deck_image.py の `_v2` ディレクトリ名 / 画像有効判定 1KB閾値 / scraper.py の `time.sleep(2)` / rarity.py「レアレア」/ ygores_repository.py `_EX_PROP_IDS` の出所
- [ ] **fetch_guard.ALLOWED_PATH_PREFIXES（常に空）の要否判断** — 使われない検証ロジックを消すか、意図をコメント化するか
- [ ] **README.md の失効記述更新** — 「3店舗同時検索」等が旧構成のまま（実態は販売7店舗・デフォルト6店）。docs/ARCHITECTURE.md と整合させる（2026-07-10 reviewer指摘）

## 完了

- [x] 2026-07-10 前提監査（assumptions-check）＋棚卸し（inventory-audit）実施
- [x] 2026-07-10 未使用3ファイル削除（c859e17）・ルート空フォルダ削除・失効文書6枚を `_archive\docs_2026-03\` へ移動
- [x] 2026-07-10 test_fetch_guard の失効期待値を追従（67640e8）
- [x] 2026-07-10 MAMB方式へ移行（状態ファイル新設・ARCHITECTURE.md全面改訂・CLAUDE.md改訂）
