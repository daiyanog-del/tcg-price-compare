'use strict';
// デッキを読み込み直すときに、リプレイの記録と辞書も初期化されることの回帰検証
// （2026-10-15 ユーザー報告: 読み込み直すたびに前のデッキの辞書が積み重なり、
//   記録を再生してもカードが動かない／共有リンクでデッキとEXデッキが崩れる）。
// deck-input-panel.js の import を外し、依存をスタブに差し替えて data:URL で読み込む
// （replay_service_comment_check.js と同じやり方）。
const fs = require('fs');
const path = require('path');
const assert = require('assert/strict');

const root = path.resolve(__dirname, '..', '..');
const b64 = (s) => 'data:text/javascript;base64,' + Buffer.from(s).toString('base64');

(async () => {
  let src = fs.readFileSync(path.join(root, 'static/solitaire/js/ui/deck-input-panel.js'), 'utf8');
  src = src.replace(/^import [\s\S]*?from '[^']+';\r?$/gm, '');
  src = src.replace(
    "await import('../components/card-manager.js')",
    "({returnAllCardsToDeck:()=>globalThis.T.push('returnAll')})",
  );

  const calls = [];
  const events = [];
  let logLen = 0;
  let setupLen = 0;
  let confirmAnswer = true;
  let confirms = 0;
  let fetchFails = false;
  globalThis.T = calls;
  globalThis.LOGLEN = () => logLen;
  globalThis.SETUPLEN = () => setupLen;

  const stubs = `
const T = globalThis.T;
const addCardToPool = () => { T.push('add'); };
const sortPoolCards = () => {};
const registerCardImage = () => T.push('regImg');
const registerCardName = () => {};
const registerCardType = () => {};
const resetReplay = () => T.push('reset');
const getLogLength = () => globalThis.LOGLEN();
const getSetupLogs = () => ({ length: globalThis.SETUPLEN() });
const setCardName = () => {};
const clearSessionResume = () => {};
const showToast = (m) => T.push('toast:' + m);
const showActionToast = (m) => T.push('action:' + m);
const guardUndoable = async () => { T.push('undoPoint'); return { ok: true, id: 'u1' }; };
const offerUndo = (m, id) => T.push('offer:' + m + '#' + id);
const discardUndoPoint = (id) => T.push('discard#' + id);
const setDeckLink = () => {};
const getDeckLink = () => null;
const onDeckLinkChange = () => {};
const isMobilePortrait = () => false;
const watchMobilePortrait = () => {};
`;
  const pool = { querySelector: () => ({ querySelector: () => ({ id: 'item-1' }) }), innerHTML: '' };
  globalThis.window = {};
  globalThis.localStorage = { getItem: () => null };
  globalThis.sessionStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
  // 読み込み終わり（sol-deck-load-end）で「元に戻す」を出すので、addEventListener と dispatchEvent をつなぐ
  let listeners = [];
  globalThis.document = {
    getElementById: (id) => (id === 'poolRow' || id === 'poolRow2' ? pool : null),
    querySelectorAll: () => [],
    addEventListener: (type, fn, opts) => listeners.push({ type, fn, once: !!(opts && opts.once) }),
    dispatchEvent: (e) => {
      events.push(e.type);
      for (const l of listeners.filter(x => x.type === e.type)) {
        if (l.once) listeners = listeners.filter(x => x !== l);
        l.fn(e);
      }
    },
  };
  globalThis.CustomEvent = class { constructor(t, o) { this.type = t; this.detail = o && o.detail; } };
  globalThis.confirm = () => { confirms++; return confirmAnswer; };
  globalThis.alert = () => {};
  globalThis.fetch = async (u) => {
    if (fetchFails) throw new Error('通信失敗');
    return {
      ok: true,
      json: async () => (String(u).includes('meta/deck')
        ? { full_deck: [{ name: 'A', qty: 1, is_ex: false }] }
        : { images: { A: 'http://x/a.jpg' }, infos: {} }),
    };
  };

  const m = await import(b64(src + stubs));
  const reset = () => { calls.length = 0; events.length = 0; };

  // 1. 記録があるときは確認窓を出さず、入れ替えの前に写しを取り、読み込み終わりに「元に戻す」を出す（2026-10-21）
  logLen = 3;
  for (const load of [() => m.loadDeckFromText('1 A'), () => m.loadMetaDeck('X'), () => m.loadDeckFromNeuron('1 A', '1 B')]) {
    reset();
    await load();
    assert.equal(confirms, 0, '確認窓は出さない');
    const iUndo = calls.indexOf('undoPoint');
    assert.ok(iUndo >= 0 && iUndo < calls.indexOf('reset'), `写しは片付けより前（${calls.join(',')}）`);
    assert.ok(calls.some(c => c.startsWith('offer:デッキを読み込みました')), `読み込み後に元に戻すを出す（${calls.join(',')}）`);
    assert.ok(events.includes('sol-deck-load-end'));
  }

  // 2. 記録の初期化が新しいカードの追加・登録より前に行われる（回帰検出の芯）
  reset();
  await m.loadDeckFromText('1 A');
  const iReset = calls.indexOf('reset');
  assert.ok(iReset >= 0, '入れ替えで記録を初期化する');
  const iFirstAdd = calls.findIndex((c) => c === 'add' || c === 'regImg');
  assert.ok(iFirstAdd > iReset, `初期化は追加より前（${calls.join(',')}）`);

  // 3. 記録が0件なら写しも元に戻すも出さない
  logLen = 0; setupLen = 0; reset();
  await m.loadDeckFromText('1 A');
  assert.ok(!calls.includes('undoPoint') && !calls.some(c => c.startsWith('offer:')), '記録が無ければ元に戻すは出さない');
  assert.ok(calls.includes('reset'), '記録が無くても辞書は初期化する（前のデッキの分を残さない）');

  // 4. 範囲共有の初期配置（setup）だけでも記録ありとして写しを取る
  setupLen = 2; reset();
  await m.loadDeckFromText('1 A');
  assert.ok(calls.includes('undoPoint'), 'setup だけの記録でも写しを取る');
  setupLen = 0;

  // 5. EXデッキだけの読み込みは入れ替えではないので、写しも初期化もしない
  logLen = 3; reset();
  await m.loadDeckFromNeuron('', '1 B');
  assert.ok(!calls.includes('undoPoint'), 'EXだけなら写しを取らない');
  assert.ok(!calls.includes('reset'), 'EXだけなら記録を消さない');

  // 6. 環境デッキは通信に失敗したら記録を消さない（取得できてから片付けるため）。
  //    何も変わっていないので「元に戻す」は出さず、写しを捨てて失敗だけを1回知らせる（reviewer 指摘）
  reset(); fetchFails = true;
  await m.loadMetaDeck('X');
  assert.ok(!calls.includes('reset'), '環境デッキの取得に失敗したら記録は残す');
  assert.ok(!calls.some(c => c.startsWith('offer:')), `片付ける前の失敗では元に戻すを出さない（${calls.join(',')}）`);
  assert.ok(calls.includes('discard#u1'), 'その世代の写しを捨てる');
  assert.equal(calls.filter(c => c.startsWith('action:')).length, 1, `失敗の知らせは1回（${calls.join(',')}）`);
  // 記録が無いときの失敗は、失敗だけを知らせる
  logLen = 0; reset();
  await m.loadMetaDeck('X');
  assert.ok(calls.some(c => c.startsWith('action:環境デッキの読み込みに失敗しました')), '記録が無いときは失敗を知らせる');
  fetchFails = false; logLen = 3;

  // 7. 「クリア」も同じ片付け（記録と辞書の初期化）を通る
  reset();
  assert.equal(await m.clearEverything(), true);
  assert.ok(calls.includes('reset'), 'クリアで記録を初期化する');

  console.log('デッキ読み込み時の記録の初期化を確認しました');
})().catch((e) => { console.error(e); process.exit(1); });
