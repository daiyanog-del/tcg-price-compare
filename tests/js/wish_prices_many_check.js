'use strict';
// 購入候補の価格: API の1回の上限（_WISH_API_MAX）を超える数のカードも、分けて取りに行って全部の価格を出すことの回帰検証
// （2026-10-31: サーバーが先頭50件しか受け取らず、後から入れたカードが「価格が見つかりませんでした」になっていた）。
// index-wish.js から価格の取得〜表示（_wishPrices の宣言〜_wishPriceHtml）だけを切り出して動かす。
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert/strict');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static/js/index-wish.js'), 'utf8');
const start = src.indexOf('let _wishPrices = null;');
const end = src.indexOf('// ── マイデッキ保存');
assert.ok(start >= 0 && end > start, '切り出す範囲が見つからない');

const store = new Map();
const sent = [];
const sb = {
  localStorage: { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) },
  wishGet: () => sb.__list,
  // サーバーの代わり: 受け取った名前を、表記を直した名前（末尾に「＊」）で送った順に返す
  fetch: (url, opt) => {
    const names = JSON.parse(opt.body).cards;
    sent.push(names.length);
    const cards = names.map((n, i) => ({ name: n + '＊', status: 'ready', latest: 100 + i }));
    return Promise.resolve({ ok: true, json: async () => ({ cards }) });
  },
  Date, setTimeout: () => 0, clearTimeout: () => {},
  document: { getElementById: () => null },
  renderWishlist: () => {}, wishUpdateBadge: () => {},
  Promise, console, Object, Set, JSON,
};
sb.globalThis = sb;
vm.createContext(sb);
vm.runInContext(src.slice(start, end) + '\n;globalThis.__html=n=>_wishPriceHtml(_wishPrices[n]);globalThis.__max=_WISH_API_MAX;', sb);

(async () => {
  const max = sb.__max;
  const total = max * 2 + 5;
  sb.__list = Array.from({ length: total }, (_, i) => ({ name: `カード${i}`, qty: 1 }));
  await vm.runInContext('loadWishPrices()', sb);
  assert.deepEqual(sent, [max, max, 5], `${max}件ずつに分けて送る`);
  // 最後に入れたカード（以前は切り捨てられていた）にも価格が出る。表記を直した名前でも送った名前でも引ける
  assert.ok(sb.__html(`カード${total - 1}`).includes('¥104'), '最後のカードの価格が出る');
  assert.ok(sb.__html(`カード${max}`).includes('¥100'), '2回目の先頭のカードの価格が出る');
  for (let i = 0; i < total; i++) assert.ok(!sb.__html(`カード${i}`).includes('見つかりませんでした'), `カード${i}`);
  console.log(`購入候補の価格: ${total}種を${max}件ずつ取りに行き、全部の価格が出ることを確認しました`);
})().catch(e => { console.error(e); process.exit(1); });
