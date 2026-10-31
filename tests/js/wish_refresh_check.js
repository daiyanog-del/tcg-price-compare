'use strict';
// 購入候補の「価格を更新」（wishRefreshPrices。2026-10-31）の検証。
// - /api/deck に fresh=1 で送り、80種ごとに分ける
// - 未発売のカードは送らない
// - 終わったら端末の価格を捨てて DB を読み直す（renderWishlist）。開いている「どこで買うと安いか」も調べ直す
// - 取れなかった店・途中で止まった場合を知らせる
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert/strict');

const root = path.join(__dirname, '..', '..');
const src = fs.readFileSync(path.join(root, 'static/js/index-wish.js'), 'utf8');
function slice(from, to) {
  const s = src.indexOf(from), e = src.indexOf(to, s);
  assert.ok(s >= 0 && e > s, `切り出す範囲が見つからない: ${from}`);
  return src.slice(s, e);
}
const code = slice('// ── 「価格を更新」', '// まだ店に並ばないカードの言い方');

// SSE の本文（card_done を names の数だけ＋最後の done。noDone なら done を送らずに閉じる）
function sseBody(names, failed, noDone) {
  const text = names.map((n, i) => `data: ${JSON.stringify({ type: 'card_done', index: i, name: n, failed_shops: failed || [] })}\n\n`).join('')
    + (noDone ? '' : `data: ${JSON.stringify({ type: 'done' })}\n\n`);
  const bytes = Buffer.from(text, 'utf8');
  let sent = false;
  return { getReader: () => ({ read: async () => (sent ? { done: true } : (sent = true, { done: false, value: bytes })) }) };
}

function run(list, opts = {}) {
  const posts = [], toasts = [], store = new Map();
  let rendered = 0, cheapest = 0, loads = 0;
  const btn = { textContent: '価格を更新', disabled: false, setAttribute() {}, removeAttribute() {} };
  const wsr = { classList: { contains: c => c === 'hidden' ? !opts.rankingOpen : false } };
  const sb = {
    window: {}, console, Map, Set, Object, JSON, Promise, Date, Number, String, URLSearchParams, TextDecoder,
    wishGet: () => list,
    _ga: () => {},
    _wishPrices: opts.prices === undefined ? {} : opts.prices,
    _wishPricesNames: new Set(),
    _wishPricesLoading: null,
    loadWishPrices: async () => { loads++; sb._wishPrices = opts.loadedPrices || {}; },
    _clearWishPricesCache: () => { store.set('cleared', true); },
    renderWishlist: () => { rendered++; },
    wishCheapestStores: () => { cheapest++; },
    localStorage: { getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) },
    document: { getElementById: id => (id === 'wishShopRanking' ? wsr : null) },
    DeckHead: { toast: m => toasts.push(m) },
    ShopBusy: {
      sleep: async () => {},
      fetch: async (url, init) => {
        const p = new URLSearchParams(init.body.toString());
        const names = p.get('cards').split('|');
        posts.push({ url, fresh: p.get('fresh'), names });
        if (opts.failAt === posts.length) throw new Error('切断');
        return { ok: true, status: 200, body: sseBody(names, opts.failedShops, opts.silentCutAt === posts.length) };
      },
    },
  };
  sb.window.DeckHead = sb.DeckHead;
  vm.createContext(sb);
  vm.runInContext(code, sb);
  return vm.runInContext('wishRefreshPrices(btnArg)', Object.assign(sb, { btnArg: btn }))
    .then(() => ({ posts, toasts, rendered, cheapest, btn, sb, store, loads }));
}

(async () => {
  assert.ok(code.includes('function wishRefreshPrices'), '切り出しに wishRefreshPrices が無い');

  // 1. 170種 → 80・80・10 に分けて fresh=1 で送る。終わったら DB を読み直す
  {
    const list = Array.from({ length: 170 }, (_, i) => ({ name: `カード${i}`, qty: 1 }));
    const r = await run(list);
    assert.deepEqual(r.posts.map(p => p.names.length), [80, 80, 10]);
    assert.ok(r.posts.every(p => p.url === '/api/deck' && p.fresh === '1'));
    assert.equal(r.rendered, 1);
    assert.equal(r.sb._wishPrices, null, '端末の価格を捨てて取り直す');
    assert.ok(r.store.get('cleared'));
    assert.ok(r.store.get('cardprice_wish_refreshed_v1'), '更新した時刻を覚える');
    assert.equal(r.btn.textContent, '価格を更新', 'ボタンの文字を戻す');
    assert.equal(r.btn.disabled, false);
    assert.deepEqual(r.toasts, ['価格を更新しました']);
    assert.equal(r.cheapest, 0, '「どこで買うと安いか」を開いていなければ調べない');
  }

  // 2. 未発売は送らない・同じ名前（レアリティ違い）は1回だけ
  {
    const r = await run(
      [{ name: 'A', qty: 1 }, { name: 'A', qty: 1, rarity: 'ウルトラ' }, { name: '未来のカード', qty: 1 }],
      { prices: { '未来のカード': { status: 'unreleased' } }, rankingOpen: true });
    assert.deepEqual(r.posts.map(p => p.names), [['A']]);
    assert.equal(r.cheapest, 1, '開いている「どこで買うと安いか」は調べ直す');
  }

  // 3. 取れなかった店を知らせる
  {
    const r = await run([{ name: 'A', qty: 1 }], { failedShops: ['カーナベル'] });
    assert.match(r.toasts[0], /カーナベルは取得できず/);
  }

  // 4. 2回目で切れたら、そこまでの分は読み直して「途中で止まりました」
  {
    const list = Array.from({ length: 100 }, (_, i) => ({ name: `カード${i}`, qty: 1 }));
    const r = await run(list, { failAt: 2 });
    assert.equal(r.rendered, 1);
    assert.match(r.toasts[0], /80\/100種まで更新しました/);
    assert.equal(r.store.get('cardprice_wish_refreshed_v1'), undefined, '途中で止まったら「〇時〇分に更新」を出さない');
  }

  // 4b. 最後の合図（done）が届かずに静かに閉じたら途中で切れた扱い。残りは送らない
  {
    const list = Array.from({ length: 100 }, (_, i) => ({ name: `カード${i}`, qty: 1 }));
    const r = await run(list, { silentCutAt: 1 });
    assert.equal(r.posts.length, 1);
    assert.match(r.toasts[0], /80\/100種まで更新しました/);
  }

  // 4c. 価格をまだ読んでいなければ先に読んでから未発売を外す
  {
    const r = await run([{ name: 'A', qty: 1 }, { name: '未来のカード', qty: 1 }],
      { prices: null, loadedPrices: { '未来のカード': { status: 'unreleased' } } });
    assert.equal(r.loads, 1);
    assert.deepEqual(r.posts.map(p => p.names), [['A']]);
  }

  // 5. 1件も調べられなければ読み直さない
  {
    const r = await run([{ name: 'A', qty: 1 }], { failAt: 1 });
    assert.equal(r.rendered, 0);
    assert.match(r.toasts[0], /更新できませんでした/);
  }

  // 6. 見出しの「14:05に更新」は今日のうちだけ
  {
    const sb = { localStorage: { getItem: () => JSON.stringify({ time: Date.now() - 2 * 86400000 }) }, JSON, Number, Date, String };
    vm.createContext(sb);
    vm.runInContext(code.replace(/async function wishRefreshPrices[\s\S]*$/, ''), sb);
    assert.equal(vm.runInContext('_wishRefreshedHtml()', sb), '');
    sb.localStorage.getItem = () => JSON.stringify({ time: Date.now() });
    assert.match(vm.runInContext('_wishRefreshedHtml()', sb), /\d+:\d\dに更新/);
  }
  console.log('ok');
})().catch(e => { console.error(e); process.exit(1); });
