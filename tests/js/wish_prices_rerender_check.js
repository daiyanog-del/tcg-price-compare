'use strict';
// 購入候補の価格: 取得の途中で一覧を描いても、届いたら描き直すことの回帰検証
// （2026-10-21: 起動時の取得の途中に購入候補を開くと、届いても「相場データ収集中」のまま止まっていた）。
// index-wish.js から価格の取得まわり（_wishPrices の宣言〜_wishEnsurePrices）だけを切り出して動かす。
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert/strict');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static/js/index-wish.js'), 'utf8');
const start = src.indexOf('let _wishPrices = null;');
const end = src.indexOf('// 価格情報の表示 HTML を生成する');
assert.ok(start >= 0 && end > start, '切り出す範囲が見つからない');

(async () => {
  const store = new Map();
  let fetches = 0;
  let resolveFetch = null;
  const sb = {
    localStorage: { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) },
    wishGet: () => [{ name: '灰流うらら', qty: 3 }],
    renders: 0, badges: 0,
    fetch: () => { fetches++; return new Promise(r => { resolveFetch = () => r({ json: async () => ({ cards: [{ name: '灰流うらら', status: 'ready', latest: 180 }] }) }); }); },
    Date, Promise, console,
  };
  sb.renderWishlist = () => { sb.renders++; vm.runInContext('_wishEnsurePrices()', sb); };
  sb.wishUpdateBadge = () => { sb.badges++; };
  vm.createContext(sb);
  vm.runInContext(src.slice(start, end) + '\n;globalThis.__get=()=>({p:_wishPrices,loading:_wishPricesLoading});', sb);
  const tick = () => new Promise(r => setTimeout(r, 0));

  // 1. 起動時の取得（index-wish.js 末尾と同じ呼び方）の途中で一覧を開く
  vm.runInContext('loadWishPrices().then(wishUpdateBadge)', sb);
  sb.renderWishlist();
  sb.renderWishlist(); // 取得中に何度描いても、取りに行くのは1回・描き直しの予約も1回
  assert.equal(fetches, 1, '取得中に二重に取りに行った');
  assert.equal(sb.renders, 2);
  resolveFetch(); await tick(); await tick();
  assert.equal(sb.renders, 3, '届いたあとに一覧を1回だけ描き直す');
  assert.equal(sb.__get().p['灰流うらら'].latest, 180);
  assert.equal(sb.__get().loading, null, '届いたら取得中の印を外す');

  // 2. 新しいうちは描いても取りに行かない
  sb.renderWishlist(); await tick();
  assert.equal(fetches, 1);
  assert.equal(sb.renders, 4);

  // 3. 取得中にカードが足されて価格が捨てられた（null）ら、古い結果を残さず、描き直しで取り直す
  vm.runInContext('_wishPrices=null', sb);
  sb.renderWishlist();
  assert.equal(fetches, 2);
  vm.runInContext('_wishPrices=null', sb); // 取得中にもう一度捨てられた
  resolveFetch(); await tick(); await tick();
  assert.equal(fetches, 3, '捨てられていたら描き直しで取り直す');
  resolveFetch(); await tick(); await tick();
  assert.equal(sb.__get().p['灰流うらら'].latest, 180);

  console.log('購入候補の価格: 取得中に描いた一覧を届いたら描き直すことを確認しました');
})().catch(e => { console.error(e); process.exit(1); });
