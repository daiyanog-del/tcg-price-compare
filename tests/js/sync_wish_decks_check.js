/**
 * tests/js/sync_wish_decks_check.js
 *
 * 購入候補の decks（どのデッキ用か・2026-10-20）の同期の回帰テスト（reviewer 指摘 1・2）。
 *   1. push の本文に decks が入る（空でも {}。サーバーはキーの無い行を古い版からの送信とみなす）
 *   2. 結合（merged）の結果を手元に適用できないページ（wishSave が無い）では rev を進めない
 *      （rev だけ進むと次の pull が「変化なし」になり、サーバーだけの候補を次の通常 push で消す）
 *   3. 適用できるページでは、結合結果の decks も手元に残る
 *
 * static/shared/sync-client.js をそのまま読み込み、fetch・localStorage だけ差し替える。
 * 実行: node tests/js/sync_wish_decks_check.js
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert/strict');

const CODE = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'shared', 'sync-client.js'), 'utf8');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function setup({ withWishSave, response }) {
  const store = {
    cardprice_sync_state: JSON.stringify({ sync_id: 's1', wishlist_rev: 3, decks_rev: 1 }),
  };
  const localStorage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };
  const sent = [];
  const win = {
    localStorage,
    document: { createElement: () => ({ style: {} }), body: { appendChild() {} } },
    fetch: (url, opts) => {
      sent.push({ url, body: JSON.parse(opts.body) });
      return Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve(response) });
    },
  };
  if (withWishSave) win.wishSave = (list) => { store.cardprice_wishlist = JSON.stringify(list); };
  const sandbox = { window: win, document: win.document, fetch: win.fetch, localStorage,
    setTimeout, clearTimeout, console, Promise, JSON, Object, Math, Error, Array };
  vm.createContext(sandbox);
  vm.runInContext(CODE, sandbox, { filename: 'sync-client.js' });
  return { client: win.SyncClient, store, sent };
}

(async () => {
  const mine = [{ name: 'X', qty: 3, rarity: '', decks: { d_1: 3 } }, { name: 'Y', qty: 1, rarity: '' }];
  const merged = { ok: true, status: 'merged', rev: 5,
    items: [{ name: 'X', rarity: '', qty: 3, decks: { d_1: 3 } }, { name: 'Y', rarity: '', qty: 1 }, { name: 'Z', rarity: '', qty: 2 }] };

  // 1・2: wishSave の無いページ（シミュレーターに wishSave を用意する前の形）
  {
    const { client, store, sent } = setup({ withWishSave: false, response: merged });
    client.onWishSave(mine);
    await sleep(2300);
    const push = sent.find((s) => s.url === '/api/sync/push');
    assert.ok(push, 'push されない');
    assert.deepEqual(push.body.items, [
      { name: 'X', rarity: '', qty: 3, decks: { d_1: 3 } },
      { name: 'Y', rarity: '', qty: 1, decks: {} },
    ], 'push の本文に decks が入っていない（空は {}）');
    assert.equal(JSON.parse(store.cardprice_sync_state).wishlist_rev, 3, '結合結果を適用できないのに rev が進んだ');
  }

  // 3: 適用できるページでは結合結果（decks 込み）が手元に入り、rev が進む
  {
    const { client, store } = setup({ withWishSave: true, response: merged });
    client.onWishSave(mine);
    await sleep(2300);
    assert.equal(JSON.parse(store.cardprice_sync_state).wishlist_rev, 5);
    const local = JSON.parse(store.cardprice_wishlist);
    assert.deepEqual(local.find((c) => c.name === 'X').decks, { d_1: 3 }, '結合結果を適用したら decks が消えた');
    assert.ok(local.some((c) => c.name === 'Z'), 'サーバーだけの候補が取り込まれていない');
  }

  // 内訳だけが変わったときも送る（比較用の文字列に decks が入っている）
  {
    const { client, sent } = setup({ withWishSave: true, response: { ok: true, status: 'applied', rev: 4 } });
    client.onWishSave([{ name: 'X', qty: 3, rarity: '', decks: { d_1: 3 } }]);
    await sleep(2300);
    client.onWishSave([{ name: 'X', qty: 3, rarity: '', decks: { d_2: 3 } }]);
    await sleep(2300);
    assert.equal(sent.filter((s) => s.url === '/api/sync/push').length, 2, '内訳だけの変化が送られない');
  }

  console.log('購入候補の decks の同期: 送信に含める・適用できない結合で rev を進めない・結合結果の decks を残す・内訳だけの変化も送る を確認');
})().catch((e) => { console.error('FAIL', e); process.exit(1); });
