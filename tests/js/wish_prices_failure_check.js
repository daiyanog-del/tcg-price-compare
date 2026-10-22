'use strict';
// 購入候補の価格: 読み込みの失敗を「値段なし」と別に見せ、失敗は長くとっておかずに取り直すことの回帰検証
// （2026-10-22: DB の読み込み失敗で31枚すべてが「価格が見つかりませんでした」になり、端末が10分その結果をとっておいた）。
// index-wish.js から価格の取得〜表示（_wishPrices の宣言〜_wishPriceHtml）だけを切り出して動かす。
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert/strict');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static/js/index-wish.js'), 'utf8');
const start = src.indexOf('let _wishPrices = null;');
const end = src.indexOf('// ── マイデッキ保存');
assert.ok(start >= 0 && end > start, '切り出す範囲が見つからない');

const NOT_FOUND = '価格が見つかりませんでした';
const FAILED = '価格を読み込めませんでした';

function setup(store) {
  let clock = 1_000_000;
  const timers = [];
  const sb = {
    localStorage: { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) },
    __list: [{ name: '灰流うらら', qty: 3 }, { name: '増殖するG', qty: 2 }],
    wishGet: () => sb.__list,
    __respond: null, // () => Promise<Response 相当>
    fetches: 0, renders: 0,
    fetch: () => { sb.fetches++; return sb.__respond(); },
    Date: { now: () => clock },
    setTimeout: (fn, ms) => { timers.push({ fn, at: clock + ms }); return timers.length; },
    clearTimeout: () => {},
    document: { getElementById: id => (id === 'wishList' ? { offsetParent: {} } : null) },
    Promise, console, Object, Set, JSON,
  };
  sb.renderWishlist = () => { sb.renders++; vm.runInContext('_wishEnsurePrices()', sb); };
  sb.wishUpdateBadge = () => {};
  sb.globalThis = sb;
  vm.createContext(sb);
  vm.runInContext(src.slice(start, end) + '\n;globalThis.__html=n=>_wishPriceHtml(_wishPrices[n]);', sb);
  sb.advance = ms => { clock += ms; for (const t of timers.splice(0)) { if (t.at <= clock) t.fn(); else timers.push(t); } };
  return sb;
}
const ok = cards => () => Promise.resolve({ ok: true, json: async () => ({ cards }) });
const tick = () => new Promise(r => setTimeout(r, 0));

(async () => {
  // 1. サーバーが一部のカードを status "error" で返した: 失敗と値段なしは別の文。失敗は端末に保存しない
  {
    const store = new Map();
    const sb = setup(store);
    sb.__respond = ok([
      { name: '灰流うらら', status: 'error', latest: null },
      { name: '増殖するG', status: 'ready', latest: null },
    ]);
    await vm.runInContext('loadWishPrices()', sb);
    assert.ok(sb.__html('灰流うらら').includes(FAILED), '失敗したカードは「読み込めませんでした」');
    assert.ok(sb.__html('増殖するG').includes(NOT_FOUND), '値段の無いカードは「見つかりませんでした」');
    const saved = JSON.parse(store.get('cardprice_wish_prices_v1'));
    assert.ok(!saved.data['灰流うらら'], '失敗の結果を端末に保存しない');
    assert.ok(!saved.names.includes('灰流うらら'), '失敗したカードは取りに行った名前からも外す（次に開いたら取り直す）');
    assert.ok(saved.data['増殖するG'], '値段なしの結果は保存する');

    // 30秒たてば（一覧が見えていれば）自分で取り直す。10分待たない
    sb.__respond = ok([
      { name: '灰流うらら', status: 'ready', latest: 180 },
      { name: '増殖するG', status: 'ready', latest: null },
    ]);
    sb.advance(10 * 1000); await tick();
    assert.equal(sb.fetches, 1, '30秒より前には取り直さない');
    sb.advance(25 * 1000); await tick(); await tick();
    assert.equal(sb.fetches, 2, '失敗のあと30秒ほどで取り直す');
    assert.ok(sb.__html('灰流うらら').includes('¥180'));

    // 取り直しが成功したら10分は取りに行かない
    sb.advance(60 * 1000); sb.renderWishlist(); await tick();
    assert.equal(sb.fetches, 2, '成功の結果は10分とっておく');
  }

  // 2. 次に開いたとき: 保存された結果から失敗のカードは消えているので、すぐ取り直す
  {
    const store = new Map();
    store.set('cardprice_wish_prices_v1', JSON.stringify({
      data: { '増殖するG': { name: '増殖するG', status: 'ready', latest: 300 } },
      time: 1_000_000, names: ['増殖するG'],
    }));
    const sb = setup(store);
    sb.__respond = ok([
      { name: '灰流うらら', status: 'ready', latest: 180 },
      { name: '増殖するG', status: 'ready', latest: 300 },
    ]);
    sb.renderWishlist(); await tick(); await tick();
    assert.equal(sb.fetches, 1, '保存に無いカードがあれば開いたときに取り直す');
  }

  // 3. 通信・応答そのものの失敗: 全カード「読み込めませんでした」、30秒で取り直す
  {
    const sb = setup(new Map());
    sb.__respond = () => Promise.resolve({ ok: false, status: 503, json: async () => ({}) });
    await vm.runInContext('loadWishPrices()', sb);
    assert.ok(vm.runInContext('_wishPriceHtml(undefined)', sb).includes(FAILED));
    sb.__respond = ok([{ name: '灰流うらら', status: 'ready', latest: 180 }, { name: '増殖するG', status: 'ready', latest: 300 }]);
    sb.advance(31 * 1000); await tick(); await tick();
    assert.equal(sb.fetches, 2, '通信の失敗のあとも30秒ほどで取り直す');
    assert.ok(sb.__html('増殖するG').includes('¥300'));
  }
  // 4. 失敗が続くとき: 30秒→60秒→120秒…と間隔を延ばし、10分で止める（毎回時間切れする重い読み込みを30秒ごとに投げない。reviewer 高1）
  {
    const sb = setup(new Map());
    sb.__respond = ok([{ name: '灰流うらら', status: 'error', latest: null }, { name: '増殖するG', status: 'error', latest: null }]);
    await vm.runInContext('loadWishPrices()', sb);
    const step = async ms => { sb.advance(ms); await tick(); await tick(); };
    await step(31 * 1000); assert.equal(sb.fetches, 2, '1回目の取り直しは30秒');
    await step(31 * 1000); assert.equal(sb.fetches, 2, '2回目は30秒では取り直さない');
    await step(30 * 1000); assert.equal(sb.fetches, 3, '2回目の取り直しは60秒');
    await step(121 * 1000); assert.equal(sb.fetches, 4, '3回目は120秒');
    for (let i = 0; i < 10; i++) await step(10 * 60 * 1000 + 1000);
    const n = sb.fetches;
    await step(9 * 60 * 1000); assert.equal(sb.fetches, n, '間隔は10分で止まる（10分より短くならない）');
    await step(61 * 1000); assert.equal(sb.fetches, n + 1);
    // 成功したら間隔を30秒に戻す
    sb.__respond = ok([{ name: '灰流うらら', status: 'ready', latest: 180 }, { name: '増殖するG', status: 'ready', latest: 300 }]);
    await step(10 * 60 * 1000 + 1000);
    sb.__respond = ok([{ name: '灰流うらら', status: 'error', latest: null }, { name: '増殖するG', status: 'ready', latest: 300 }]);
    vm.runInContext('_wishPrices=null', sb); sb.renderWishlist(); await tick(); await tick();
    const m = sb.fetches;
    await step(31 * 1000); assert.equal(sb.fetches, m + 1, '成功のあとの失敗は、また30秒で取り直す');
  }
  console.log('購入候補の価格: 読み込みの失敗と値段なしを分けて見せ、失敗は30秒で取り直すことを確認しました');
})().catch(e => { console.error(e); process.exit(1); });
