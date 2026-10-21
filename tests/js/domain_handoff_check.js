// 独自ドメインへの引き継ぎ（static/shared/domain-handoff.js）の単体確認。
// 送る側の中身・受け取る側の振り分け（初回の丸ごと／使用中の合流／2回目以降の差分）・
// referrer の3通り・next の検証・運ぶキーの列挙を、ブラウザの代わりの最小の部品で確かめる。
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert/strict');
const src = fs.readFileSync(path.join(__dirname, '../../static/shared/domain-handoff.js'), 'utf8');

const OLD = 'https://tcg-price-compare.onrender.com';
const NEW = 'https://greedy-lab.com';

function makeStorage(init, maxValueLen) {
  const m = new Map(Object.entries(init || {}));
  return {
    get length() { return m.size; },
    key(i) { return [...m.keys()][i] ?? null; },
    getItem(k) { return m.has(k) ? m.get(k) : null; },
    setItem(k, v) {
      if (maxValueLen && String(v).length > maxValueLen) throw new Error('QuotaExceededError');
      m.set(k, String(v));
    },
    removeItem(k) { m.delete(k); },
    dump() { return Object.fromEntries(m); },
  };
}

function el() {
  const classes = new Set(['hidden']);
  const listeners = {};
  return {
    textContent: '', disabled: false, dataset: {},
    classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c) },
    addEventListener: (t, f) => { listeners[t] = f; },
    appendChild() {},
    click() { listeners.click && listeners.click(); },
    get visible() { return !classes.has('hidden'); },
  };
}

async function run({ mode, origin, pathname = '/', search = '', hash = '', referrer = '', storage = {}, dataset = {}, ls: givenLs, wait = 50 }) {
  const ls = givenLs || makeStorage(storage);
  const els = { handoffStatus: el(), handoffFallback: el(), handoffConfirm: el(), handoffNote: el() };
  els.handoffConfig = { dataset: Object.assign({ mode }, dataset) };
  const replaced = [];
  const location = { origin, pathname, search, hash, replace: u => replaced.push(u) };
  const sandbox = {
    document: { getElementById: id => els[id] || null, querySelector: () => null, createElement: () => el(), referrer },
    location, localStorage: ls,
    history: { replaceState: (s, t, u) => { location.hash = ''; location._replacedUrl = u; } },
    URL, URLSearchParams, TextEncoder, TextDecoder, Response, Blob, CompressionStream, DecompressionStream,
    btoa, atob, setTimeout, Promise, JSON, Date, Uint8Array, String, Number, Array, Object, Error,
  };
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  await new Promise(r => setTimeout(r, wait));
  return { ls, els, replaced, location };
}

async function sendAndGetFragment(storage) {
  const r = await run({ mode: 'send', origin: OLD, dataset: { canonicalBase: NEW, targetPath: '/new-cards?x=1' }, storage });
  assert.equal(r.replaced.length, 1);
  return r.replaced[0];
}

const deck = (id, updated) => JSON.stringify([{ id, name: id, updated }]);
const recvData = { legacyOrigins: OLD };

(async () => {
  // ── 送る側: 運ぶキーだけ・管理画面の鍵は運ばない ──
  const sent = await sendAndGetFragment({
    cardprice_saved_decks: deck('d1', 1), cardprice_sync_state: '{"sync_id":"s"}', 'sol-saved-replays': '[{"id":"r1"}]',
    'card-sim-save-slot-1': 'x', admin_key: 'secret', cardprice_wish_prices_v1: '{}', unrelated: '1',
  });
  assert.ok(sent.startsWith(NEW + '/handoff?next=%2Fnew-cards%3Fx%3D1#'), sent);
  const frag = sent.split('#')[1];

  // 送る側: 何も無ければ /handoff を経由しない
  const plain = await run({ mode: 'send', origin: OLD, dataset: { canonicalBase: NEW, targetPath: '/' }, storage: { admin_key: 'k' } });
  assert.deepEqual(plain.replaced, [NEW + '/']);

  // ── 受け取る側: 初回・空 → 丸ごと（同期の状態も） ──
  let r = await run({ mode: 'receive', origin: NEW, pathname: '/handoff', search: '?next=%2Fnew-cards%3Fx%3D1', hash: '#' + frag, referrer: OLD + '/new-cards', dataset: recvData });
  let d = r.ls.dump();
  assert.deepEqual(r.replaced, ['/new-cards?x=1']);
  assert.equal(d.cardprice_sync_state, '{"sync_id":"s"}');
  assert.equal(d['card-sim-save-slot-1'], 'x');
  assert.ok(!('admin_key' in d) && !('unrelated' in d) && !('cardprice_wish_prices_v1' in d), JSON.stringify(d));
  assert.ok(d.gl_handoff_received_at);
  assert.equal(r.location._replacedUrl, '/handoff?next=%2Fnew-cards%3Fx%3D1'); // # を消す

  // ── 2回目以降: 前回より後のデッキだけ合流待ちへ（消したデッキは復活しない） ──
  const later = await sendAndGetFragment({ cardprice_saved_decks: JSON.stringify([{ id: 'old', updated: 100 }, { id: 'ext', updated: 5000 }]) });
  r = await run({ mode: 'receive', origin: NEW, pathname: '/handoff', search: '?next=/', hash: '#' + later.split('#')[1], referrer: OLD + '/', dataset: recvData,
    storage: { gl_handoff_received_at: '1000', cardprice_saved_decks: '[]' } });
  d = r.ls.dump();
  assert.deepEqual(JSON.parse(d.gl_handoff_pending).decks.map(x => x.id), ['ext']);
  assert.equal(d.cardprice_saved_decks, '[]');

  // ── 初回・新ドメインを先に使っていた → 合流待ち・リプレイは id で足す・同期の状態は新側 ──
  const both = await sendAndGetFragment({
    cardprice_saved_decks: deck('o1', 1), cardprice_wishlist: '[{"name":"A"}]', cardprice_sync_state: '{"sync_id":"old"}',
    'sol-saved-replays': '[{"id":"r1"},{"id":"r2"}]', 'sol-toggle-seen': '1',
  });
  r = await run({ mode: 'receive', origin: NEW, pathname: '/handoff', search: '?next=/', hash: '#' + both.split('#')[1], referrer: OLD + '/', dataset: recvData,
    storage: { cardprice_saved_decks: deck('n1', 1), cardprice_sync_state: '{"sync_id":"new"}', 'sol-saved-replays': '[{"id":"r2"},{"id":"r3"}]' } });
  d = r.ls.dump();
  const pending = JSON.parse(d.gl_handoff_pending);
  assert.deepEqual(pending.decks.map(x => x.id), ['o1']);
  assert.deepEqual(pending.wishlist.map(x => x.name), ['A']);
  assert.equal(d.cardprice_sync_state, '{"sync_id":"new"}');
  assert.deepEqual(JSON.parse(d['sol-saved-replays']).map(x => x.id), ['r2', 'r3', 'r1']);
  assert.equal(d['sol-toggle-seen'], '1');

  // ── referrer: 自分自身・他サイトからは受け取らない ──
  for (const ref of [NEW + '/handoff', 'https://evil.example/']) {
    r = await run({ mode: 'receive', origin: NEW, pathname: '/handoff', search: '?next=/', hash: '#' + frag, referrer: ref, dataset: recvData });
    assert.equal(r.ls.length, 0, ref);
    assert.deepEqual(r.replaced, [], ref);
    assert.ok(r.els.handoffStatus.textContent.includes('引き継げません'), ref);
  }

  // ── referrer 無し: 確認してから・同期の状態は写さない ──
  r = await run({ mode: 'receive', origin: NEW, pathname: '/handoff', search: '?next=/', hash: '#' + frag, referrer: '', dataset: recvData });
  assert.equal(r.ls.length, 0);
  assert.ok(r.els.handoffConfirm.visible);
  r.els.handoffConfirm.click();
  await new Promise(res => setTimeout(res, 50));
  assert.equal(r.ls.getItem('cardprice_sync_state'), null);
  assert.ok(r.ls.getItem('cardprice_saved_decks'));
  assert.deepEqual(r.replaced, ['/']);

  // ── 確認の経路で細工された中身でも、運ぶキー以外は書かない ──
  const evil = await sendAndGetFragment({ cardprice_saved_decks: '[]' });
  const evilPayload = Buffer.from(JSON.stringify({ v: 2, items: { cardprice_saved_decks: '[]', admin_key: 'x', evil_html: '<img>' } })).toString('base64url');
  r = await run({ mode: 'receive', origin: NEW, pathname: '/handoff', search: '?next=/', hash: '#j.' + evilPayload, referrer: '', dataset: recvData });
  r.els.handoffConfirm.click();
  await new Promise(res => setTimeout(res, 50));
  assert.equal(r.ls.getItem('admin_key'), null);
  assert.equal(r.ls.getItem('evil_html'), null);
  void evil;

  // ── next の検証: 別 origin・/handoff・# は通さない ──
  const cases = [['/%09/evil.com', '/'], ['%2F%2Fevil.com', '/'], ['/%0a/evil.com', '/'], ['https://evil.com/', '/'],
    ['/handoff%23j.x', '/'], ['/card/x%3Fy%3D1', '/card/x?y=1'], ['/%3Fa%3D1%23frag', '/?a=1'], ['', '/']];
  for (const [next, want] of cases) {
    r = await run({ mode: 'receive', origin: NEW, pathname: '/handoff', search: '?next=' + next, hash: '', dataset: recvData });
    assert.deepEqual(r.replaced, [want], next);
  }

  // ── 壊れた中身: 書かずに「もう一度」を出す ──
  r = await run({ mode: 'receive', origin: NEW, pathname: '/handoff', search: '?next=/', hash: '#z.!!!', referrer: OLD + '/', dataset: recvData });
  assert.equal(r.ls.length, 0);
  assert.ok(r.els.handoffFallback.visible);

  // ── 同期の状態は最初の1回だけ送る ──
  const oldLs = makeStorage({ cardprice_saved_decks: deck('d1', 1), cardprice_sync_state: '{"sync_id":"s"}' });
  const decodeFrag = async u => {
    const rr = await run({ mode: 'receive', origin: NEW, pathname: '/handoff', search: '?next=/', hash: '#' + u.split('#')[1], referrer: OLD + '/', dataset: recvData });
    return rr.ls.dump();
  };
  let s1 = await run({ mode: 'send', origin: OLD, dataset: { canonicalBase: NEW, targetPath: '/' }, ls: oldLs });
  assert.ok(oldLs.getItem('gl_handoff_sync_sent'));
  assert.equal((await decodeFrag(s1.replaced[0])).cardprice_sync_state, '{"sync_id":"s"}');
  s1 = await run({ mode: 'send', origin: OLD, dataset: { canonicalBase: NEW, targetPath: '/' }, ls: oldLs });
  const second = await decodeFrag(s1.replaced[0]);
  assert.equal(second.cardprice_sync_state, undefined);
  assert.ok(second.cardprice_saved_decks);

  // ── 大きすぎるとき: 新しいリプレイから入る分だけ・デッキと同期の状態は必ず ──
  const crypto = require('crypto');
  const replays = Array.from({ length: 20 }, (_, i) => ({ id: 'r' + i, savedAt: i, data: crypto.randomBytes(60000).toString('base64') }));
  const big = await run({ mode: 'send', origin: OLD, dataset: { canonicalBase: NEW, targetPath: '/' }, wait: 4500,
    storage: { cardprice_saved_decks: deck('d1', 1), cardprice_sync_state: '{"sync_id":"s"}', 'sol-saved-replays': JSON.stringify(replays), 'card-sim-save-slot-1': crypto.randomBytes(300000).toString('base64') } });
  assert.equal(big.replaced.length, 1);
  assert.ok(big.replaced[0].length < 1000100);
  assert.ok(big.els.handoffStatus.textContent.includes('件は引き継げませんでした'), big.els.handoffStatus.textContent);
  const got = await decodeFrag(big.replaced[0]);
  assert.ok(got.cardprice_saved_decks && got.cardprice_sync_state);
  const keptIds = JSON.parse(got['sol-saved-replays'] || '[]').map(r => r.id);
  assert.ok(keptIds.length > 0 && keptIds.length < 20, keptIds.length);
  assert.ok(keptIds.includes('r19') && !keptIds.includes('r0'), keptIds.join(','));
  assert.equal(got['card-sim-save-slot-1'], undefined);

  // ── 容量の上限で大きいキーが書けなくても、デッキ・同期の状態は写り、受け取りの印も立つ ──
  const small = await sendAndGetFragment({ cardprice_saved_decks: deck('d1', 1), cardprice_sync_state: '{"sync_id":"s"}', 'sol-saved-replays': JSON.stringify([{ id: 'r', data: 'x'.repeat(5000) }]) });
  const quotaLs = makeStorage({}, 1000);
  r = await run({ mode: 'receive', origin: NEW, pathname: '/handoff', search: '?next=/', hash: '#' + small.split('#')[1], referrer: OLD + '/', dataset: recvData, ls: quotaLs });
  d = quotaLs.dump();
  assert.ok(d.cardprice_saved_decks && d.cardprice_sync_state && d.gl_handoff_received_at, JSON.stringify(Object.keys(d)));
  assert.equal(d['sol-saved-replays'], undefined);
  assert.deepEqual(r.replaced, ['/']);

  console.log('domain_handoff_check OK');
})().catch(e => { console.error(e); process.exit(1); });
