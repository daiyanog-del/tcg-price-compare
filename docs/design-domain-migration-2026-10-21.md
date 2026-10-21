# 独自ドメイン移行の設計（greedy-lab.com）— 2026-10-21 初版

旧: `https://tcg-price-compare.onrender.com`（以下「旧」）
新: `https://greedy-lab.com`（以下「新」。www は Render で apex へ 301）

## 0. 現状（10-21 確認）

- 新は開通済み・未告知。Render のカスタムドメイン、Cloudflare DNS は CNAME（DNS only）、証明書は `*.greedy-lab.com` 込みで発行済み
- 同期アカウント（sync_accounts）25件、プッシュ購読（push_subscriptions）**0件**
- 利用者データはブラウザの localStorage（origin ごとに分かれる）にある:
  `cardprice_saved_decks`・購入候補・`cardprice_sync_state`（sync_id）・`cardprice_deck_draft`（下書き。同期対象外）
- 端末間同期とワンタイムリンク（`/api/sync/link/create` → `/sync?t=` → `redeem`。10分・1回限り・和集合マージ）が既にある
- ニューロン連携拡張 v1.3.0 は旧を直書き。デッキ取り込みは `旧/?tcgym_import=1` を開き、content script が**旧の localStorage に直接書く**

## 1. 目標

- 利用者は何も操作しない。旧のブックマーク・X の古いリンク・拡張機能のボタンから来ても、データごと新に着く
- 旧のデータは消さない（失敗しても旧に残っている）
- 拡張機能の権限は増やさない（再承認で拡張が止まるのを避ける）

## 2. 段階

### 段階1: 新を正にする準備（利用者影響なし・旧の動きは不変）

- 正規 URL（canonical・OGP・sitemap・robots の Sitemap 行）を環境変数 `CANONICAL_BASE_URL=https://greedy-lab.com` に固定（`site_urls.py`・`app.py` の `_canonical_base_url()`）
  - `_public_base_url()`（リクエストの host 由来）は同期リンクの発行に使っているので**そのまま残す**。canonical 用に別関数を足す
- 直書きの差し替え: `x_poster.py` の `SITE_URL`、`unreleased_approve.py` の `ADMIN_URL`、プライバシーポリシー・利用規約の URL 表記
- `scraper.py` の User-Agent は**変えない**（トレコロ WAF は名乗りで通している。変えるなら別作業で監視つき）
- `_same_origin_ok()` は変えない（旧のページは旧の API、新のページは新の API を叩くので、どちらも同一 origin）

### 段階2: 引き継ぎつき転送（切り替え日＝環境変数を設定した日）

旧の host（`LEGACY_HOSTS`。既定 `tcg-price-compare.onrender.com`）で:

| 要求 | 応答 |
|---|---|
| 画面（`/`・`/card/`・`/buy/`・`/featured`・`/new-cards`・`/solitaire`・`/solitaire/replays`・`/admin`・`/share-target`）の GET | **引き継ぎページ**（`templates/handoff.html`・GA 無し・canonical は新の同じパス） |
| それ以外（`/api/*`・`/static/*`・`/sync`・robots・sitemap・OG 画像など） | 今までどおり（更新前の拡張・旧の service worker・途中の同期リンクのため） |

**運び方（10-21 実装時に変更）**: 初版の「同期のワンタイムリンクで運ぶ」をやめ、**ブラウザ保存領域を丸ごと URL の `#` 以降で運ぶ**（`static/shared/domain-handoff.js`）。

- 理由: シミュレーターのリプレイ・下書きなど同期の対象外のデータも運べる。同期 API の5秒制限・DB の状態に左右されない。`#` 以降はサーバーにも GA にも送られない
- 圧縮（deflate-raw）して base64url。長さが 1,000,000 文字を超えたら自動では運ばず案内を出す（データは旧に残る）

送る側（旧）:

1. `?tcgym_import=1` 付きなら、更新前の拡張（v1.3）の取り込み画面（`.tdi-overlay`）が閉じるまで待つ。拡張が無ければ5秒で打ち切る
2. 1回目: 保存領域を丸ごと（管理画面の鍵・プッシュ購読済みの印・価格の一時キャッシュは除く）。送った時刻を `gl_handoff_at` に記録
3. 2回目以降: `updated` が `gl_handoff_at` より後のデッキだけ（丸ごと送り直すと、新で消したデッキが旧の古い写しから復活する）。無ければ運ばずに移動
4. `新/handoff?next=<パス>#<中身>` へ移る（運ぶものが無ければ `新<パス>` へ直接）

受け取る側（新の `/handoff`）:

- まず `#` を URL から消す
- `document.referrer` の origin が旧 → 受け取る
  - 新が空（デッキ・購入候補・リプレイ・同期の状態が無い）: 丸ごと写す。同期の状態も写るので、同じ同期アカウントを使い続ける
  - 新が空でない: デッキ・購入候補は `gl_handoff_pending` に置き、トップで `static/shared/handoff-apply.js` が保存の関数（`savedDecksSet`・`wishSave`）を通して合流させる（同期にも載る）。同期の状態は新側を残す。それ以外のキーは新に無いものだけ足す
- referrer が旧以外のサイト → 受け取らない（細工されたリンクで他人の sync_id を書き込まれ、以後の保存を読まれるのを防ぐ）
- referrer が無い（ブラウザの設定など）→ 「引き継ぎますか？」のボタンを出し、押されたら受け取る。このときは同期の状態は写さない
- 失敗したら「もう一度引き継ぐ」（旧の `?gl_retry=1`。印を消して丸ごと送り直す）

### 段階3: 片付け（転送開始から約60日後、または旧の HTML アクセスがほぼ0になったら）

- 旧の画面の GET は単純な 301 に変更（更新前の拡張は自動更新で消えているはず）

## 3. 拡張機能（v1.4.0・10-21 裁定変更）

- 4か所の URL（`content.js`・`deck-content.js`・`wiki-content.js` の `TCGYM_BASE`、`manifest.json` の取り込みの content script）を新に変える
- 新しい host を足すので、更新時に Chrome が権限の再承認を求める見込み（未検証）。利用者は約7人なので受け入れる
  - 初版の「取り込みは旧経由のまま」は、シンガポール移設で旧の onrender の住所が無くなるとぶつかるので却下
  - 「取り込み画面をサイト側に移して権限を増やさない」案は、7人のためには作業が見合わないので却下
- ストアへの提出は切り替え日の後（先に出すと、v1.4 のボタンが引き継ぎ前の空の新ドメインを開く）
- 自動更新されるまでの旧版（v1.3）は、旧の `?tcgym_import=1` を引き継ぎページが待って運ぶ

## 4. 外部の登録先

- Search Console: 新のプロパティを登録。旧（URL プレフィックス）の所有確認用の meta を引き継ぎページにも置き、アドレス変更ツールを試す（onrender のサブドメインで使えるかは未確認）
- GA4 のデータストリーム URL、X のプロフィール、Chrome ウェブストアの掲載ページ、メルカリアンバサダーの登録サイト（あれば）

## 5. ホーム画面に追加している人・プッシュ通知

- 旧の origin で入れたアイコンは、開くたびに旧→新へ移る（未検証。アプリの枠の外に出る見込み）。案内は出さない（今の規模では少数）。必要になったら別途
- プッシュ通知の購読は 10-21 時点で0件。通知の飛び先は相対パスのままで、旧の service worker から開かれても引き継ぎページ経由で新に着く

## 6. Cloudflare の中継（CDN）

- 移行が落ち着くまでオフ（DNS only）のまま
- Render 自体がすでに Cloudflare の後ろにある（応答に `Server: cloudflare`・`cf-cache-status: DYNAMIC`）ので、DDoS 対策や TLS の面で今すぐの差は無い。速くなるのはキャッシュの規則を書いてから
- オンにするときの確認: レート制限は `CF-Connecting-IP` で利用者の IP を取っている（`client_ip.py`）。Cloudflare を二重に通しても元の IP が届くか（未検証）、`/sync`・`/api`・管理画面をキャッシュしないこと

## 7. 検証

- 単体: host 別の振り分け（旧の HTML→引き継ぎページ、旧の `/api`→通常、新→通常）
- 単体: `tests/test_domain_handoff.py`
- 結合（10-21 実施・28/28）: ローカルで `localhost:5099`（旧）と `127.0.0.1:5099`（新）を別の origin に見立て、Playwright で 1回目の丸ごと／2回目の素通り／新で消したデッキが復活しない／新を先に使っていた人の合流／他サイト発の拒否／referrer 無しの確認／旧版拡張の取り込み待ち／拡張なしの打ち切り
- 本番: 段階2の反映直後に、自分の端末で旧のデータが新に着くこと、拡張の取り込みが新に着くことを確かめる

## 8. 決まったこと（10-21 ユーザー裁定）

1. 自動で移動する（ボタンは押させない）
2. 段階3は約60日後
3. 拡張は新ドメインを足して再承認を受け入れる（v1.4.0）

## 9. 切り替えの手順

1. この変更を main に取り込む（環境変数が無いあいだは動きが変わらない）
2. Render の web（tcg-price-compare）と定期実行4つに `CANONICAL_BASE_URL=https://greedy-lab.com` を設定（X 投稿・通知の URL も切り替わる）
3. 本番で確認: 旧の画面→新へ移る・自分のデータが届く・新の canonical
4. 拡張 v1.4.0 をストアへ提出（`greedy-lab-extension-v1.4.0.zip`・リポジトリ外の `..\`）
5. Search Console・GA4・X のプロフィール・ストアの掲載ページ・プライバシーポリシーの表記
