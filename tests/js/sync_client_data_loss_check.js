/**
 * tests/js/sync_client_data_loss_check.js
 *
 * 2026-08-13 本番で再現したデータ消失バグの回帰テスト。
 *
 * バグの構造:
 *   _ensureAccount(kind, items) が「きっかけになった種別」のリストしか /api/sync/init に
 *   送らず、もう一方のリビジョンをローカルで rev=0 のまま初期化していた。次の pull で
 *   ローカルrev=0 とサーバーrev=1（未送信のため中身は空）が不一致となり、サーバーの
 *   空リストが「新しいデータ」として wishSave([])/savedDecksSet([]) で上書き適用され、
 *   ローカルにしか無かったデータが消えていた。
 *
 * 固定する仕様（このスクリプトが検証する3点。司令塔の完了条件と対応）:
 *   1. init はきっかけの種別に関わらず、ローカルの購入候補・デッキ両方を送る
 *   2. ローカルrevが0（=一度もサーバーと揃っていない。旧バグで壊れた端末を含む）の
 *      種別は、pull結果を適用せず、代わりにローカル内容をpushして和集合マージで合流する
 *   3. ローカルrev>0の通常時は、従来どおりpull結果をそのまま適用する
 *
 * static/shared/sync-client.js のソースをそのままロードして検証する（別実装で検証しない）。
 * fetch/localStorage/document はNode上の最小スタブに差し替える。
 *
 * 実行: node tests/js/sync_client_data_loss_check.js
 * 終了コード0=全項目OK、非0=失敗（stderrにFAIL行）。
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SYNC_CLIENT_PATH = path.join(__dirname, '..', '..', 'static', 'shared', 'sync-client.js');

function makeLocalStorage(initial) {
  const store = Object.assign({}, initial || {});
  return {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
    _dump: () => JSON.parse(JSON.stringify(store)),
  };
}

function loadSyncClient(windowObj) {
  const code = fs.readFileSync(SYNC_CLIENT_PATH, 'utf8');
  const sandbox = {
    window: windowObj,
    document: windowObj.document,
    fetch: windowObj.fetch,
    localStorage: windowObj.localStorage,
    setTimeout, clearTimeout, console, Promise, JSON, Object, Math, Error, Array,
  };
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: 'sync-client.js' });
  return sandbox.window.SyncClient;
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

function fakeDocument() {
  return { createElement: () => ({ style: {} }), body: { appendChild() {} } };
}

let failures = 0;
function check(name, cond, detail) {
  if (cond) {
    console.log('OK: ' + name);
  } else {
    failures++;
    console.error('FAIL: ' + name + (detail ? ' — ' + detail : ''));
  }
}

// ── シナリオ1: initはきっかけの種別に関わらず両方のローカル内容を送る ──
async function scenario1_init_sends_both_lists() {
  console.log('\n=== シナリオ1: init が常に両方のリストを送るか ===');
  const calls = [];
  const localStorage = makeLocalStorage({
    // 購入候補は既にローカルにある（同期は未開始 = cardprice_sync_state 無し）
    cardprice_wishlist: JSON.stringify([{ name: '灰流うらら', qty: 3, rarity: '' }]),
  });
  const windowObj = {
    document: fakeDocument(),
    localStorage,
    fetch: (url, opts) => {
      calls.push({ url, body: JSON.parse(opts.body) });
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({ ok: true, sync_id: 's-new', wishlist_rev: 1, decks_rev: 1 }),
      });
    },
    savedDecksSet: (list) => {
      localStorage.setItem('cardprice_saved_decks', JSON.stringify(list));
    },
  };
  const SyncClient = loadSyncClient(windowObj);

  // デッキを保存 = savedDecksSet が呼ばれ、その中から onDecksSave が発火する想定
  // （index.html/一人回しの実際の配線を模倣）
  const newDeck = [{ id: 'd1', name: '新デッキ', text: '', main: [], ex: [], updated: Date.now() }];
  windowObj.savedDecksSet(newDeck);
  SyncClient.onDecksSave(newDeck);
  await sleep(2300); // デバウンス2秒 + マージン

  const initCall = calls.find((c) => c.url === '/api/sync/init');
  check('init が呼ばれた', !!initCall);
  if (!initCall) return;
  check('init が wishlist（ローカルの既存データ）を含む',
    Array.isArray(initCall.body.wishlist) && initCall.body.wishlist.length === 1
    && initCall.body.wishlist[0].name === '灰流うらら',
    JSON.stringify(initCall.body.wishlist));
  check('init が decks（きっかけになったデータ）も含む',
    Array.isArray(initCall.body.decks) && initCall.body.decks.length === 1
    && initCall.body.decks[0].name === '新デッキ',
    JSON.stringify(initCall.body.decks));

  const state = JSON.parse(localStorage.getItem('cardprice_sync_state'));
  check('wishlist_rev が rev=0 のプレースホルダではなくサーバー応答値', state.wishlist_rev === 1,
    JSON.stringify(state));
  check('decks_rev もサーバー応答値', state.decks_rev === 1, JSON.stringify(state));
}

// ── シナリオ2: 逆方向（デッキを持つ人が購入候補を先に追加）でも両方送る ──
async function scenario1b_init_sends_both_lists_reverse_trigger() {
  console.log('\n=== シナリオ1b: 逆方向（デッキ保持者が購入候補をきっかけに同期開始） ===');
  const calls = [];
  const localStorage = makeLocalStorage({
    cardprice_saved_decks: JSON.stringify([{ id: 'd1', name: '既存デッキ', text: '', main: [], ex: [], updated: 1 }]),
  });
  const windowObj = {
    document: fakeDocument(),
    localStorage,
    fetch: (url, opts) => {
      calls.push({ url, body: JSON.parse(opts.body) });
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({ ok: true, sync_id: 's-new2', wishlist_rev: 1, decks_rev: 1 }),
      });
    },
  };
  const SyncClient = loadSyncClient(windowObj);
  const newWish = [{ name: 'カードA', rarity: '', qty: 1 }];
  localStorage.setItem('cardprice_wishlist', JSON.stringify(newWish));
  SyncClient.onWishSave(newWish);
  await sleep(2300);

  const initCall = calls.find((c) => c.url === '/api/sync/init');
  check('init が呼ばれた（逆方向）', !!initCall);
  if (!initCall) return;
  check('init が decks（ローカルの既存データ）を含む（逆方向）',
    Array.isArray(initCall.body.decks) && initCall.body.decks.length === 1
    && initCall.body.decks[0].name === '既存デッキ', JSON.stringify(initCall.body.decks));
  check('init が wishlist（きっかけになったデータ）も含む（逆方向）',
    Array.isArray(initCall.body.wishlist) && initCall.body.wishlist.length === 1, JSON.stringify(initCall.body.wishlist));
}

// ── シナリオ2: 既に壊れた端末（decks_rev=0）を pull がデータ消失させないこと ──
async function scenario2_rescue_rule_prevents_data_loss_on_pull() {
  console.log('\n=== シナリオ2: rev=0の種別はpull結果を適用せず、pushで合流する（救済ルール） ===');
  const localDeckList = [{ id: 'd-local', name: 'ローカルにしか無いデッキ', text: '', main: [], ex: [], updated: 999 }];
  const localStorage = makeLocalStorage({
    // 旧バグにより decks_rev=0 のまま保存されている「壊れた」状態を再現
    cardprice_sync_state: JSON.stringify({ sync_id: 's1', wishlist_rev: 5, decks_rev: 0 }),
    cardprice_saved_decks: JSON.stringify(localDeckList),
  });

  const pushCalls = [];
  let savedDecksSetCalls = 0;
  const windowObj = {
    document: fakeDocument(),
    localStorage,
    fetch: (url, opts) => {
      const body = opts && opts.body ? JSON.parse(opts.body) : null;
      if (url === '/api/sync/pull') {
        // サーバー側はこの端末のdecksを一度も受け取っていない = 空
        return Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve({
            ok: true,
            wishlist: { unchanged: true },
            decks: { rev: 1, items: [] },
          }),
        });
      }
      if (url === '/api/sync/push') {
        pushCalls.push(body);
        return Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve({ ok: true, status: 'applied', rev: 1 }),
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
    },
    // savedDecksSetが呼ばれたら「適用された（＝データが上書きされた）」とみなして記録する
    savedDecksSet: () => { savedDecksSetCalls++; },
  };
  const SyncClient = loadSyncClient(windowObj);
  await SyncClient.pullIfNeeded();

  check('rev=0のdecksに対してsavedDecksSet(=適用)が呼ばれていない（データ消失していない）',
    savedDecksSetCalls === 0, 'savedDecksSet が ' + savedDecksSetCalls + '回呼ばれた');
  check('ローカルのデッキがpullで上書きされず残っている',
    JSON.parse(localStorage.getItem('cardprice_saved_decks'))[0].name === 'ローカルにしか無いデッキ');

  const deckPush = pushCalls.find((b) => b.kind === 'decks');
  check('代わりにローカルのデッキ内容がpushされた（サーバー側で和集合マージさせるため）',
    !!deckPush && deckPush.items.length === 1 && deckPush.items[0].name === 'ローカルにしか無いデッキ',
    JSON.stringify(deckPush));

  const state = JSON.parse(localStorage.getItem('cardprice_sync_state'));
  check('wishlist(rev>0・unchanged)は据え置きのまま（無関係な種別に影響していない）',
    state.wishlist_rev === 5);
}

// ── シナリオ3: 通常時（rev>0）はこれまでどおりpull結果を適用する ──
async function scenario3_normal_case_still_applies_pull_result() {
  console.log('\n=== シナリオ3: rev>0の通常時は、pull結果をこれまでどおり適用する ===');
  const localStorage = makeLocalStorage({
    cardprice_sync_state: JSON.stringify({ sync_id: 's1', wishlist_rev: 3, decks_rev: 4 }),
  });
  let appliedDecks = null;
  const windowObj = {
    document: fakeDocument(),
    localStorage,
    fetch: (url) => {
      if (url === '/api/sync/pull') {
        return Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve({
            ok: true,
            wishlist: { unchanged: true },
            decks: { rev: 5, items: [{ id: 'd9', name: '他端末のデッキ', text: '', main: [], ex: [], updated: 1 }] },
          }),
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
    },
    savedDecksSet: (list) => { appliedDecks = list; },
  };
  const SyncClient = loadSyncClient(windowObj);
  await SyncClient.pullIfNeeded();

  check('rev>0の通常時はpull結果が適用される', !!appliedDecks && appliedDecks[0].name === '他端末のデッキ',
    JSON.stringify(appliedDecks));
  const state = JSON.parse(localStorage.getItem('cardprice_sync_state'));
  check('decks_revがサーバー応答値に更新される', state.decks_rev === 5, JSON.stringify(state));
  check('wishlist_rev(unchanged)は変化しない', state.wishlist_rev === 3);
}

// ── シナリオ4: push再発行(reissued)経路でも、もう一方をrev=0のまま放置しない ──
async function scenario4_reissue_also_syncs_sibling_kind() {
  console.log('\n=== シナリオ4: push再発行(reissued)でも、もう一方の種別を直後にpushする ===');
  const localStorage = makeLocalStorage({
    cardprice_sync_state: JSON.stringify({ sync_id: 's-old', wishlist_rev: 2, decks_rev: 3 }),
    cardprice_wishlist: JSON.stringify([{ name: '既存の購入候補', rarity: '', qty: 1 }]),
  });
  const pushCalls = [];
  const windowObj = {
    document: fakeDocument(),
    localStorage,
    fetch: (url, opts) => {
      const body = opts && opts.body ? JSON.parse(opts.body) : null;
      if (url === '/api/sync/push') {
        pushCalls.push(body);
        if (pushCalls.length === 1) {
          // 1回目（デッキのpush）: sync_idが見つからず再発行される
          return Promise.resolve({
            ok: true, status: 200,
            json: () => Promise.resolve({ ok: true, reissued: true, sync_id: 's-new', decks_rev: 1 }),
          });
        }
        // 2回目（siblingのwishlist push）: 新しいsync_idに対する通常成功
        return Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve({ ok: true, status: 'applied', rev: 1 }),
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
    },
  };
  const SyncClient = loadSyncClient(windowObj);
  SyncClient.onDecksSave([{ id: 'd1', name: 'デッキ', text: '', main: [], ex: [], updated: 1 }]);
  await sleep(2300);
  // sibling(wishlist)のpushも2秒デバウンスされるため、さらに待つ
  await sleep(2300);

  check('デッキpushが再発行を受けた', pushCalls.length >= 1 && pushCalls[0].kind === 'decks');
  const siblingPush = pushCalls.find((b) => b.kind === 'wishlist');
  check('再発行後、もう一方(wishlist)も新しいsync_idへpushされた', !!siblingPush,
    JSON.stringify(pushCalls));
  if (siblingPush) {
    check('siblingのpush内容がローカルの購入候補と一致', siblingPush.items[0].name === '既存の購入候補');
  }
  const state = JSON.parse(localStorage.getItem('cardprice_sync_state'));
  check('新しいsync_idに切り替わっている', state.sync_id === 's-new', JSON.stringify(state));
}

(async () => {
  await scenario1_init_sends_both_lists();
  await scenario1b_init_sends_both_lists_reverse_trigger();
  await scenario2_rescue_rule_prevents_data_loss_on_pull();
  await scenario3_normal_case_still_applies_pull_result();
  await scenario4_reissue_also_syncs_sibling_kind();

  console.log('\n' + (failures === 0 ? 'ALL OK' : failures + ' FAILURE(S)'));
  process.exit(failures === 0 ? 0 : 1);
})();
