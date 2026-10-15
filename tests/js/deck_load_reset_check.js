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
`;
  const pool = { querySelector: () => ({ querySelector: () => ({ id: 'item-1' }) }), innerHTML: '' };
  globalThis.window = {};
  globalThis.localStorage = { getItem: () => null };
  globalThis.sessionStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
  globalThis.document = {
    getElementById: (id) => (id === 'poolRow' || id === 'poolRow2' ? pool : null),
    querySelectorAll: () => [],
    dispatchEvent: (e) => events.push(e.type),
  };
  globalThis.CustomEvent = class { constructor(t) { this.type = t; } };
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

  // 1. 記録があるときに確認でキャンセルすると、3つの読み込みとも何も起きない
  logLen = 3; confirmAnswer = false;
  await m.loadDeckFromText('1 A');
  await m.loadMetaDeck('X');
  await m.loadDeckFromNeuron('1 A', '1 B');
  assert.equal(confirms, 3, '3つの読み込みで1回ずつ確認が出る');
  assert.deepEqual(events, [], 'キャンセルなら読み込みのイベントは出ない');
  assert.deepEqual(calls, [], 'キャンセルなら片付けも初期化もしない');

  // 2. 確認で OK にすると、記録の初期化が新しいカードの追加・登録より前に行われる（回帰検出の芯）
  confirmAnswer = true; reset();
  await m.loadDeckFromText('1 A');
  const iReset = calls.indexOf('reset');
  assert.ok(iReset >= 0, '入れ替えで記録を初期化する');
  const iFirstAdd = calls.findIndex((c) => c === 'add' || c === 'regImg');
  assert.ok(iFirstAdd > iReset, `初期化は追加より前（${calls.join(',')}）`);

  // 3. 記録が0件なら確認は出さない
  logLen = 0; setupLen = 0; reset();
  const before = confirms;
  await m.loadDeckFromText('1 A');
  assert.equal(confirms, before, '記録が無ければ確認しない');
  assert.ok(calls.includes('reset'), '記録が無くても辞書は初期化する（前のデッキの分を残さない）');

  // 4. 範囲共有の初期配置（setup）だけでも記録ありとして確認する
  setupLen = 2; reset();
  confirmAnswer = false;
  const before4 = confirms;
  await m.loadDeckFromText('1 A');
  assert.equal(confirms, before4 + 1, 'setup だけの記録でも確認する');
  setupLen = 0; confirmAnswer = true;

  // 5. EXデッキだけの読み込みは入れ替えではないので、確認も初期化もしない
  logLen = 3; reset();
  const before5 = confirms;
  await m.loadDeckFromNeuron('', '1 B');
  assert.equal(confirms, before5, 'EXだけなら確認しない');
  assert.ok(!calls.includes('reset'), 'EXだけなら記録を消さない');

  // 6. 環境デッキは通信に失敗したら記録を消さない（取得できてから片付けるため）
  reset(); fetchFails = true;
  await m.loadMetaDeck('X');
  assert.ok(!calls.includes('reset'), '環境デッキの取得に失敗したら記録は残す');
  fetchFails = false;

  // 7. 「クリア」も同じ片付け（記録と辞書の初期化）を通る
  reset();
  assert.equal(await m.clearEverything(), true);
  assert.ok(calls.includes('reset'), 'クリアで記録を初期化する');

  console.log('デッキ読み込み時の記録の初期化を確認しました');
})().catch((e) => { console.error(e); process.exit(1); });
