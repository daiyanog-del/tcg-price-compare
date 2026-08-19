/**
 * tests/js/sync_ensure_account_concurrency_check.js
 *
 * 2026-08-17 本番不具合の回帰テスト: 購入候補の追加とデッキの保存が2秒以内に起きると、
 * 種別ごとのデバウンスタイマー（onWishSave用・onDecksSave用）がほぼ同時に発火し、
 * どちらも「sync_id無し」と判断して _ensureAccount() を独立に呼ぶため、
 * /api/sync/init が2回叩かれ、内容は同じだが孤児の行が1つ余分にできていた
 * （本番の sync_accounts で0.2秒差の重複行として確認された）。
 *
 * 固定する仕様（司令塔の完了条件に対応）:
 *   1. onWishSave/onDecksSave がほぼ同時にデバウンスを発火させても、
 *      /api/sync/init へのリクエストはちょうど1回であること
 *   2. そのとき、両方の種別のデータが最終的にサーバーへ渡っていること（片方が消えないこと）
 *   3. 発行が失敗した場合、次の呼び出しで再度発行を試みられること（状態が固まらない）
 *
 * static/shared/sync-client.js のソースをそのままロードして検証する（別実装で検証しない。
 * tests/js/sync_client_data_loss_check.js と同じ流儀）。
 *
 * 実行: node tests/js/sync_ensure_account_concurrency_check.js
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
function fakeDocument() { return { createElement: () => ({ style: {} }), body: { appendChild() {} } }; }

// 実ネットワークの往復時間を模した遅延つきのfetch応答を返す。
// 【重要】この遅延が無いと、_ensureAccount() 内の Promise チェーンが同期的な
// マイクロタスクだけで即座に解決してしまい、2つ目のデバウンスタイマー（別のマクロタスク）が
// 発火する前に1つ目の _setState() が完了してしまう。すると2つ目の呼び出しは
// _getState() で既に sync_id を見つけてしまい、そもそも _ensureAccount() を
// 二度呼ぶ状況（本番のレース条件）を再現できず、修正の有無に関わらずテストが
// 常に「1回」と判定してしまう偽陽性を生む（実装時に実際に踏んだ）。
// 本番のネットワーク往復（数十〜数百ms）を模し、2つのデバウンス発火の間に
// レスポンスがまだ返っていない状態を作る
function delayedJsonResponse(payload, ms) {
  return new Promise((resolve) => {
    setTimeout(() => {
      resolve({ ok: true, status: 200, json: () => Promise.resolve(payload) });
    }, ms);
  });
}
const FAKE_NETWORK_LATENCY_MS = 50;

let failures = 0;
function check(name, cond, detail) {
  if (cond) {
    console.log('OK: ' + name);
  } else {
    failures++;
    console.error('FAIL: ' + name + (detail ? ' — ' + detail : ''));
  }
}

// ── シナリオ1: onWishSave/onDecksSaveがほぼ同時発火→initは1回だけ。両方のデータが届く ──
async function scenario1_concurrent_debounce_triggers_single_init() {
  console.log('\n=== シナリオ1: 購入候補追加とデッキ保存がほぼ同時→/api/sync/initは1回だけか ===');
  const localStorage = makeLocalStorage({});
  const calls = { init: [], push: [] };
  const windowObj = {
    document: fakeDocument(),
    localStorage,
    fetch: (url, opts) => {
      const body = JSON.parse(opts.body);
      if (url === '/api/sync/init') {
        calls.init.push(body);
        // 実ネットワーク遅延を模す（コメント参照）。これが無いと2つ目のデバウンス発火前に
        // 1つ目のinitが解決してしまい、レース条件そのものを再現できない
        return delayedJsonResponse(
          { ok: true, sync_id: 's-concurrent', wishlist_rev: 1, decks_rev: 1 }, FAKE_NETWORK_LATENCY_MS);
      }
      if (url === '/api/sync/push') {
        calls.push.push(body);
        return delayedJsonResponse({ ok: true, status: 'applied', rev: 2 }, FAKE_NETWORK_LATENCY_MS);
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
    },
    savedDecksSet: (list) => { localStorage.setItem('cardprice_saved_decks', JSON.stringify(list)); },
  };
  const SyncClient = loadSyncClient(windowObj);

  const wishlist = [{ name: '購入候補アイテム', rarity: '', qty: 1 }];
  const decks = [{ id: 'd1', name: 'デッキアイテム', text: '', main: [], ex: [], updated: Date.now() }];

  // 実際のページの配線を模す: localStorageへの書き込み（wishSave/savedDecksSet相当）が先に
  // 起き、その直後にSyncClientのフックが呼ばれる。購入候補追加とデッキ保存がほぼ同時に
  // 起きたケースを再現するため、2つのデバウンスをほぼ同時刻に仕掛ける
  localStorage.setItem('cardprice_wishlist', JSON.stringify(wishlist));
  SyncClient.onWishSave(wishlist);
  windowObj.savedDecksSet(decks);
  SyncClient.onDecksSave(decks);

  await sleep(2300); // デバウンス2秒+マージン。両方のタイマーがほぼ同時に発火する

  check('/api/sync/init へのリクエストがちょうど1回', calls.init.length === 1,
    'init呼び出し回数=' + calls.init.length);
  if (calls.init.length === 1) {
    check('initのpayloadに購入候補が含まれる（片方が消えていない）',
      calls.init[0].wishlist.length === 1 && calls.init[0].wishlist[0].name === '購入候補アイテム',
      JSON.stringify(calls.init[0].wishlist));
    check('initのpayloadにデッキも含まれる（片方が消えていない）',
      calls.init[0].decks.length === 1 && calls.init[0].decks[0].name === 'デッキアイテム',
      JSON.stringify(calls.init[0].decks));
  }

  // 後から来た側（フォロワー）は新しいinitを投げず、initの完了を待ってから
  // 自分のkindを明示的に追送する（退行防止: デッキの内容がpushされないまま終わらないこと）
  check('フォロワー側が発行完了後に自分の分を明示的にpushした（ちょうど1回）',
    calls.push.length === 1, 'push呼び出し回数=' + calls.push.length + ' ' + JSON.stringify(calls.push));

  const state = JSON.parse(localStorage.getItem('cardprice_sync_state'));
  check('新しいsync_idが保存された', state && state.sync_id === 's-concurrent', JSON.stringify(state));
  check('wishlist_rev・decks_revのどちらも欠落していない（0や未定義のまま残っていない）',
    !!state && typeof state.wishlist_rev === 'number' && state.wishlist_rev >= 1
    && typeof state.decks_rev === 'number' && state.decks_rev >= 1,
    JSON.stringify(state));
}

// ── シナリオ2: 発行が失敗しても、次の呼び出しで再度initを試みられる（状態が固まらない） ──
async function scenario2_failed_init_can_be_retried_on_next_call() {
  console.log('\n=== シナリオ2: 発行が失敗しても次回の呼び出しで再度initを試みられるか（状態固着防止） ===');
  const localStorage = makeLocalStorage({});
  let initCallCount = 0;
  let shouldFail = true;
  const windowObj = {
    document: fakeDocument(),
    localStorage,
    fetch: (url) => {
      if (url === '/api/sync/init') {
        initCallCount++;
        if (shouldFail) {
          return Promise.resolve({ ok: false, status: 500, json: () => Promise.resolve({}) });
        }
        return Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve({ ok: true, sync_id: 's-recovered', wishlist_rev: 1, decks_rev: 1 }),
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
    },
  };
  const SyncClient = loadSyncClient(windowObj);

  const wishlist1 = [{ name: '1回目', rarity: '', qty: 1 }];
  localStorage.setItem('cardprice_wishlist', JSON.stringify(wishlist1));
  SyncClient.onWishSave(wishlist1);
  await sleep(2300);

  check('1回目のinitが呼ばれ、失敗した(サーバーエラーを模擬)', initCallCount === 1,
    'initCallCount=' + initCallCount);
  check('失敗後はsync_idが保存されていない',
    !JSON.parse(localStorage.getItem('cardprice_sync_state') || 'null'));

  // サーバーが復旧したとして、再度保存操作を行う
  shouldFail = false;
  const wishlist2 = [{ name: '1回目', rarity: '', qty: 1 }, { name: '2回目', rarity: '', qty: 1 }];
  localStorage.setItem('cardprice_wishlist', JSON.stringify(wishlist2));
  SyncClient.onWishSave(wishlist2);
  await sleep(2300);

  check('進行中フラグが固まらず、2回目のinitが試みられた', initCallCount === 2,
    'initCallCount=' + initCallCount);
  const state = JSON.parse(localStorage.getItem('cardprice_sync_state'));
  check('2回目で発行成功し、sync_idが保存された', !!state && state.sync_id === 's-recovered',
    JSON.stringify(state));
}

(async () => {
  await scenario1_concurrent_debounce_triggers_single_init();
  await scenario2_failed_init_can_be_retried_on_next_call();

  console.log('\n' + (failures === 0 ? 'ALL OK' : failures + ' FAILURE(S)'));
  process.exit(failures === 0 ? 0 : 1);
})();
