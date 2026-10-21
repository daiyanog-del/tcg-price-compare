/**
 * tests/js/sync_unsent_check.js
 *
 * 2026-10-21 端末同期の送信失敗表示（TASKS「端末同期の送信失敗表示」）。
 * 送信の再試行（最大3回）が尽きても届かなかった種別を cardprice_sync_unsent に残し、
 * 連携済みの端末では同期の欄・「他の端末でも見る」の窓に「まだ届いていません［もう一度送る］」を出す。
 *
 * 固定する仕様:
 *   1. 通信失敗が続くと、再試行が尽きた時点で未送信の印が付き、sync-unsent-change が出る
 *   2. 印が付いた後は、同じ内容で保存し直しても送り直す（直前の送信内容から外す）
 *   3. 送信が受け付けられたら印が消える。db_unavailable（ok:false）は届いていないので印を付ける。
 *      入力検証エラー（invalid）は送り直しても届かないので印を付けない
 *   4. 再読込後の pull: サーバーが変わっていれば従来どおり適用し、印を外す（手元を和集合で送り直すと
 *      他の端末の削除が復活するため、救済ルールは広げない）。変わっていなければ印は残し、自動では送らない
 *   5. resendUnsent() は印のある種別を順番に送る（同じ sync_id は5秒に1回の制限）。429 は制限より長く待つ
 *   5b. 古い送信の再試行が、新しい送信が届いた後に諦めても印を付けない
 *   6. 表示: 連携済み＋印ありのときだけ出す。未連携なら出さない。旧版の sync-client（getUnsentKinds 無し）でも壊れない
 *
 * 実行: node tests/js/sync_unsent_check.js
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..', '..');
const SYNC_CLIENT = fs.readFileSync(path.join(ROOT, 'static', 'shared', 'sync-client.js'), 'utf8');
const INDEX_SYNC = fs.readFileSync(path.join(ROOT, 'static', 'js', 'index-sync.js'), 'utf8');

let failures = 0;
function check(name, cond, detail) {
  if (cond) console.log('OK: ' + name);
  else { failures++; console.error('FAIL: ' + name + (detail ? ' — ' + detail : '')); }
}

function makeLocalStorage(initial) {
  const store = Object.assign({}, initial || {});
  return {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
    _store: store,
  };
}

// 待ち時間（デバウンス2秒・バックオフ1/2秒）を 1/100 に縮める
const fastTimeout = (fn, ms) => setTimeout(fn, Math.ceil((ms || 0) / 100));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function load({ storage, fetchImpl, withUi }) {
  const events = [];
  const listeners = {};
  const elements = {};
  const doc = {
    createElement: (tag) => ({ tag, style: {}, children: [], textContent: '', appendChild(c) { this.children.push(c); }, addEventListener() {} }),
    createTextNode: (t) => ({ text: t }),
    body: { appendChild() {}, style: {} },
    getElementById: (id) => elements[id] || null,
    querySelectorAll: (sel) => (sel === '[data-sync-unsent]' ? boxes : []),
  };
  function makeBox() {
    const classes = new Set(['hidden']);
    return {
      _children: [],
      set textContent(v) { this._children = v ? [{ text: v }] : []; },
      get textContent() { return this._children.map((c) => c.text || c.textContent || '').join(''); },
      appendChild(c) { this._children.push(c); },
      classList: {
        toggle: (c, f) => { if (f) classes.add(c); else classes.delete(c); },
        contains: (c) => classes.has(c),
      },
    };
  }
  const boxes = [makeBox(), makeBox()];
  const statusEl = { classList: makeBox().classList };
  elements.wishSyncStatus = statusEl;
  const win = {
    document: doc, localStorage: storage, fetch: fetchImpl,
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
    dispatchEvent: (e) => { events.push(e.type); (listeners[e.type] || []).forEach((fn) => fn(e)); },
  };
  const sandbox = {
    window: win, document: doc, fetch: fetchImpl, localStorage: storage,
    setTimeout: fastTimeout, clearTimeout, setInterval, clearInterval,
    CustomEvent: class { constructor(t) { this.type = t; } },
    console, Promise, JSON, Object, Math, Error, Array, String, Date,
    confirm: () => true, alert: () => {},
  };
  vm.createContext(sandbox);
  vm.runInContext(SYNC_CLIENT, sandbox, { filename: 'sync-client.js' });
  if (withUi) {
    const start = INDEX_SYNC.indexOf('let _syncShareTimerId=null;');
    if (start < 0) throw new Error('index-sync.js の切り出し位置（let _syncShareTimerId=null;）が見つからない');
    vm.runInContext(INDEX_SYNC.slice(start), sandbox, { filename: 'index-sync.js' });
  }
  return { client: win.SyncClient, sandbox, events, boxes, statusEl };
}

const WISH = [{ name: '灰流うらら', qty: 3, rarity: '' }];
const unsentOf = (s) => JSON.parse(s.getItem('cardprice_sync_unsent') || '{}');

async function scenario_retry_exhausted_marks_unsent() {
  console.log('\n=== 1・2: 再試行が尽きたら未送信の印・同じ内容でも送り直す ===');
  const storage = makeLocalStorage({
    cardprice_sync_state: JSON.stringify({ sync_id: 's1', wishlist_rev: 2, decks_rev: 1, linked: true }),
    cardprice_wishlist: JSON.stringify(WISH),
  });
  let pushes = 0;
  let fail = true;
  const fetchImpl = (url, opt) => {
    if (url === '/api/sync/push') {
      pushes++;
      if (fail) return Promise.reject(new Error('offline'));
      return Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve({ ok: true, status: 'applied', rev: 3 }) });
    }
    return Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve({ ok: true }) });
  };
  const { client, events } = load({ storage, fetchImpl });
  client.onWishSave(WISH);
  await sleep(200);
  check('通信失敗は3回まで試す', pushes === 3, 'pushes=' + pushes);
  check('再試行が尽きたら購入候補に未送信の印', !!unsentOf(storage).wishlist, JSON.stringify(unsentOf(storage)));
  check('印が付いたことを知らせる（sync-unsent-change）', events.includes('sync-unsent-change'));
  check('getUnsentKinds が購入候補を返す', JSON.stringify(client.getUnsentKinds()) === '["wishlist"]');

  fail = false;
  pushes = 0;
  client.onWishSave(WISH); // 同じ内容で保存し直す
  await sleep(100);
  check('同じ内容でも送り直す（直前の送信内容から外している）', pushes === 1, 'pushes=' + pushes);
  check('受け付けられたら印が消える', !storage.getItem('cardprice_sync_unsent'));
  check('rev は進む（従来どおり）', JSON.parse(storage.getItem('cardprice_sync_state')).wishlist_rev === 3);
}

async function scenario_db_unavailable_marks_unsent() {
  console.log('\n=== 3: db_unavailable（ok:false）は届いていないので印を付ける ===');
  const storage = makeLocalStorage({
    cardprice_sync_state: JSON.stringify({ sync_id: 's1', wishlist_rev: 2, decks_rev: 1, linked: true }),
    cardprice_saved_decks: JSON.stringify([{ id: 'd1', name: 'A', text: '1 灰流うらら', updated: 1 }]),
  });
  const fetchImpl = (url) => Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve(url === '/api/sync/push' ? { ok: false, reason: 'db_unavailable' } : { ok: true }) });
  const { client } = load({ storage, fetchImpl });
  client.onDecksSave(JSON.parse(storage.getItem('cardprice_saved_decks')));
  await sleep(100);
  check('デッキに未送信の印', !!unsentOf(storage).decks, JSON.stringify(unsentOf(storage)));
  check('decks_dirty は残る（従来どおり再読込で送り直す）', JSON.parse(storage.getItem('cardprice_sync_state')).decks_dirty === true);
}

async function scenario_reload_pull() {
  console.log('\n=== 4: 再読込後（印あり）の pull ===');
  const storage = makeLocalStorage({
    cardprice_sync_state: JSON.stringify({ sync_id: 's1', wishlist_rev: 2, decks_rev: 1, linked: true }),
    cardprice_wishlist: JSON.stringify(WISH),
    cardprice_sync_unsent: JSON.stringify({ wishlist: 1 }),
  });
  const bodies = [];
  let wl = { unchanged: true };
  const fetchImpl = (url, opt) => {
    bodies.push({ url, body: JSON.parse(opt.body) });
    return Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve({ ok: true, linked: true, wishlist: wl, decks: { unchanged: true } }) });
  };
  const appliedAll = [];
  const { client, sandbox } = load({ storage, fetchImpl });
  sandbox.window.wishSave = (items) => { appliedAll.push(items); storage.setItem('cardprice_wishlist', JSON.stringify(items)); };
  sandbox.window.renderWishlist = () => {};
  // (a) サーバーが変わっていない: 印は残し、勝手に送らない（pull と同時に送ると回数制限に当たる）
  await client.pullIfNeeded();
  await sleep(50);
  check('サーバーが変わっていなければ印は残る', !!unsentOf(storage).wishlist);
  check('再読込だけでは送らない（push なし）', !bodies.some((b) => b.url === '/api/sync/push'), JSON.stringify(bodies.map((b) => b.url)));
  // (b) サーバーが変わっていた: 従来どおり適用し、印を外す（和集合で送り直さない）
  wl = { rev: 5, items: [{ name: '増殖するG', qty: 1 }] };
  await client.pullIfNeeded();
  await sleep(50);
  check('サーバーの内容を従来どおり適用する', appliedAll.length === 1 && appliedAll[0][0].name === '増殖するG', JSON.stringify(appliedAll));
  check('適用したら印を外す', !storage.getItem('cardprice_sync_unsent'));
  check('手元を送り直さない（他の端末の削除を復活させない）', !bodies.some((b) => b.url === '/api/sync/push'));
}

async function scenario_rate_limit_invalid_stale() {
  console.log('\n=== 3b・5・5b: 回数制限・入力エラー・古い再試行 ===');
  const mk = () => makeLocalStorage({
    cardprice_sync_state: JSON.stringify({ sync_id: 's1', wishlist_rev: 2, decks_rev: 1, linked: true }),
    cardprice_wishlist: JSON.stringify(WISH),
    cardprice_saved_decks: JSON.stringify([{ id: 'd1', name: 'A', text: '1 灰流うらら', updated: 1 }]),
    cardprice_sync_unsent: JSON.stringify({ wishlist: 1, decks: 2 }),
  });
  // 同じ sync_id は 5秒（1/100 で 50ms）に1回。間隔が短いと 429
  let last = -1e9; const log = [];
  const limited = () => {
    const now = Date.now();
    if (now - last < 50) { log.push(429); return Promise.resolve({ status: 429, ok: false, json: () => Promise.resolve({}) }); }
    last = now; log.push(200);
    return Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve({ ok: true, status: 'applied', rev: 9 }) });
  };
  const st = mk();
  const a = load({ storage: st, fetchImpl: limited });
  await a.client.resendUnsent();
  check('もう一度送る: 回数制限があっても両方届く', !st.getItem('cardprice_sync_unsent'), JSON.stringify(unsentOf(st)) + ' ' + log.join(','));

  // 購入候補→デッキと続けて保存しても、429 を制限より長く待つので届く
  const st2 = mk(); st2.removeItem('cardprice_sync_unsent'); last = -1e9;
  const b = load({ storage: st2, fetchImpl: limited });
  b.client.onWishSave(WISH.concat([{ name: '増殖するG', qty: 1, rarity: '' }]));
  b.client.onDecksSave([{ id: 'd1', name: 'A', text: '2 灰流うらら', updated: 2 }]);
  await sleep(300);
  check('購入候補とデッキを続けて保存しても印は付かない', !st2.getItem('cardprice_sync_unsent'), JSON.stringify(unsentOf(st2)));

  // 入力検証エラー（invalid）は印を付けない
  const st3 = mk(); st3.removeItem('cardprice_sync_unsent');
  const c = load({ storage: st3, fetchImpl: () => Promise.resolve({ status: 400, ok: false, json: () => Promise.resolve({ ok: false, reason: 'invalid' }) }) });
  c.client.onWishSave(WISH);
  await sleep(100);
  check('invalid は印を付けない', !st3.getItem('cardprice_sync_unsent'));

  // 古い送信の再試行が、新しい送信が届いた後に諦めても印を付けない
  const st4 = mk(); st4.removeItem('cardprice_sync_unsent');
  let delivered = 0;
  const d = load({ storage: st4, fetchImpl: (url, opt) => {
    const items = JSON.parse(opt.body).items;
    if (items.length === 1) return Promise.reject(new Error('offline')); // 古い内容（1件）は常に通信失敗
    delivered++;
    return Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve({ ok: true, status: 'applied', rev: 9 }) });
  } });
  d.client.onWishSave(WISH);
  await sleep(25); // 1回目が失敗して再試行を待っている間に、新しい内容を保存
  d.client.onWishSave(WISH.concat([{ name: '増殖するG', qty: 1, rarity: '' }]));
  await sleep(200);
  check('新しい送信が届いていれば、古い再試行が諦めても印は付かない', delivered >= 1 && !st4.getItem('cardprice_sync_unsent'), JSON.stringify(unsentOf(st4)));
}

async function scenario_resend_and_ui() {
  console.log('\n=== 5・6: もう一度送る・表示の出し分け ===');
  const storage = makeLocalStorage({
    cardprice_sync_state: JSON.stringify({ sync_id: 's1', wishlist_rev: 2, decks_rev: 1, linked: true }),
    cardprice_wishlist: JSON.stringify(WISH),
    cardprice_sync_unsent: JSON.stringify({ wishlist: 1, decks: 2 }),
    cardprice_saved_decks: JSON.stringify([{ id: 'd1', name: 'A', text: '1 灰流うらら', updated: 1 }]),
  });
  let fail = true;
  const fetchImpl = (url) => (fail
    ? Promise.reject(new Error('offline'))
    : Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve({ ok: true, status: 'applied', rev: 9 }) }));
  const { sandbox, boxes, statusEl } = load({ storage, fetchImpl, withUi: true });
  sandbox._refreshSyncStatusUI();
  check('連携済み＋印あり: 同期の欄が出る', !statusEl.classList.contains('hidden'));
  check('連携済み＋印あり: 2か所とも未送信を出す', boxes.every((b) => !b.classList.contains('hidden')));
  check('種別の名前を出す', boxes[0].textContent.includes('購入候補・デッキ'), boxes[0].textContent);
  check('［もう一度送る］がある', boxes[0]._children.some((c) => c.textContent === 'もう一度送る'));

  sandbox.resendSyncUnsent();
  check('送っている間は「送っています…」', boxes[0].textContent === '送っています…', boxes[0].textContent);
  await sleep(200);
  check('通信失敗のままなら印は残り、また［もう一度送る］が出る',
    boxes[0]._children.some((c) => c.textContent === 'もう一度送る') && !!unsentOf(storage).wishlist);
  fail = false;
  sandbox.resendSyncUnsent();
  await sleep(100);
  check('届いたら印が消え、表示も消える', !storage.getItem('cardprice_sync_unsent') && boxes.every((b) => b.classList.contains('hidden')),
    JSON.stringify(unsentOf(storage)));

  // 未連携の端末では出さない（届け先の端末が無い）
  const s2 = makeLocalStorage({
    cardprice_sync_state: JSON.stringify({ sync_id: 's2', wishlist_rev: 1, decks_rev: 1, linked: false }),
    cardprice_sync_unsent: JSON.stringify({ wishlist: 1 }),
  });
  const u2 = load({ storage: s2, fetchImpl, withUi: true });
  u2.sandbox._refreshSyncStatusUI();
  check('未連携なら未送信を出さない', u2.boxes.every((b) => b.classList.contains('hidden')));

  // 旧版の sync-client（getUnsentKinds 無し）がキャッシュに残っていても壊れない
  const s3 = makeLocalStorage({ cardprice_sync_state: JSON.stringify({ sync_id: 's3', linked: true }) });
  const u3 = load({ storage: s3, fetchImpl, withUi: true });
  delete u3.sandbox.window.SyncClient.getUnsentKinds;
  let threw = null;
  try { u3.sandbox._refreshSyncStatusUI(); } catch (e) { threw = e; }
  check('旧版の sync-client でも例外なく、未送信は出さない', !threw && u3.boxes.every((b) => b.classList.contains('hidden')), String(threw));
}

(async () => {
  await scenario_retry_exhausted_marks_unsent();
  await scenario_db_unavailable_marks_unsent();
  await scenario_reload_pull();
  await scenario_rate_limit_invalid_stale();
  await scenario_resend_and_ui();
  if (failures) { console.error('\n' + failures + ' 件失敗'); process.exit(1); }
  console.log('\n端末同期の送信失敗表示を確認しました');
})().catch((e) => { console.error(e); process.exit(1); });
