# TCGYM アイコン修正（2026-10-14）

## 改善したこと

- faviconの元画像を128pxから1024pxへ更新。SVGの中身は引き続き画像であり、ベクター化ではない。PWAではこのSVGを使わず、サイズを明示したPNGを選ぶ。
- favicon・通常PWAの紺色の四角を透明化。白い背景でも絵を見分けられるよう、図柄に濃色の輪郭を追加した。
- 原画の比率を保って縮小し、周囲に余白を設けた。元画像の黒に混ざる微小な明るさ（四隅で0〜3/255）を実測して取り除き、半透明の四角が残るのを防いだ。
- Apple用は端まで背景色を塗った180px PNG。Android用は192/512pxの専用maskable画像を追加し、背景は現在のサイトと同じ#101626に統一。
- maskableは中央の「直径80%の円」が安全領域。原画の図柄から最大半径を測り、再サンプリングの縁を含めて円内に収めた。
- ヘッダー画像は既存の透過原画と同じ絵を使い、144×176から288×352へ更新。WebPは可逆圧縮。画面上の大きさは維持した。
- 各参照URLに内容ハッシュを付け、古い画像が残る原因を減らした。SWのキャッシュ名は維持し、カード画像を削除せず旧アイコンだけ入れ替える。

16pxでは手や線の細部の表現に限界がある。今回は全サイズで元の絵を保持し、簡略版のデザイン変更はしていない。

## 変更ファイル

| ファイル | 変更理由 |
| --- | --- |
| .gitattributes | 生成SVG・manifest・SWの改行をLFに固定し、Windows/Linux間のハッシュ不一致を防ぐ |
| tools/build_icons.py | 比率維持、ノイズ除去、透明背景/全面背景/安全円の作り分け、輪郭、ヘッダー生成、参照ハッシュ生成。出力先のフォルダ名依存を解消。OG再生成を`--og`に分離 |
| static/favicon-16.png、favicon-32.png | タブ用の透過画像へ再生成 |
| static/favicon.svg、tcgym-icon-base.png | 1024pxの透過素材へ更新 |
| static/apple-touch-icon.png | 180px、余白付き・全面背景へ再生成 |
| static/icon-192.png、icon-512.png | 通常PWA用、透過＋輪郭へ再生成 |
| static/icon-maskable-192.png、icon-maskable-512.png | Android用の全面背景・安全円対応画像を追加 |
| static/tcgym-icon-small.png、tcgym-icon-small.webp | 元素材から288×352へ再生成 |
| static/manifest.json | sizes:anyのSVGを除去、any/maskableを分離、テーマ色と参照ハッシュを更新 |
| static/sw.js | 生成されたURLで事前キャッシュ・通知表示。旧アイコンのみ削除 |
| app.py | FAVICON_FILESにmaskable 2ファイルを追加 |
| templates/index.html | ヘッダー画像をstatic_url化。theme-colorを現在の背景色へ合わせる |
| templates/solitaire.html | ヘッダー画像とmanifestをstatic_url化。画像の縦横属性を指定 |
| templates/error.html、solo_disabled.html | 固定の?v=3をstatic_urlに置き換え |
| tests/test_icons.py | 画像サイズ・透明度・安全円・比率・参照ハッシュ・配信キャッシュを検証 |
| tests/js/icon_cache_check.js | SW更新でカード画像を保持し、旧アイコンだけ置換することを検証 |
| TASKS.md、docs/activeContext.md、docs/decisions.md、この文書 | 作業状態と判断・検証記録 |

`requirements.txt`の既存追記と未追跡`delete_all_tweets.py`は今回のコミット対象外。原画、tcgym-icon.png、logo_text.png、og-image.pngは変更していない。

## 再生成と確認

```powershell
python tools/build_icons.py
python -m pytest tests/test_icons.py tests/test_error_handlers.py -q
node tests/js/icon_cache_check.js
```

- 関連pytest 27件成功。既存依存パッケージの警告は残る。
- SWの旧アイコン削除・カード画像保持・通知URLの確認成功。
- 連続再生成で同一出力になることを確認。
- ローカル起動してトップ・一人回し・404を確認。ヘッダーの実表示はトップ33×40、一人回し29×36、読み込み元は288×352。全ページのfaviconは内容ハッシュ付きSVGを参照し、画像の読み込み成功を確認。
- 白黒比較と円形/角丸の切抜きプレビューを撮影。Maskable.appも開いたが、ブラウザツールでファイル選択が反応しなかったため、同等の安全円を持つローカルプレビューと全画素検査で代替した。
- ローカルは本番DBを設定せず、アイコン・HTMLの表示を確認。ランキング・新着カードの取得失敗はこの検証環境によるもので、データ機能の動作確認には含めない。

スクリーンショットはローカルの`.cache/icon-review/`に保存（Git管理外）：

- comparison-top.jpg / comparison-bottom.jpg：全生成サイズの白黒比較
- maskable-shapes.jpg：安全円・丸形・角丸
- top.jpg / solitaire.jpg / error.jpg：実ページ
- pages.jpg：実ページの埋め込みと、そのHTMLに指定されたfaviconを並べた確認画面

## 制限

- iPhone/Android実機へのインストールは未確認。インストール済みアイコンの再取得時期はOSやブラウザに依存する。
- 透過版は輪郭を追加したぶん、白背景では従来の白線画より濃く見える。ヘッダーは既存の白いデザインを維持し、暗色のサイト上で使う。
- 高解像度化でSVGとヘッダー画像の容量は増える。内容ハッシュによる長期キャッシュを利用する。
- 公開PR: https://github.com/daiyanog-del/tcg-price-compare/pull/35 。本番反映の結果は最終チャットで報告する。
