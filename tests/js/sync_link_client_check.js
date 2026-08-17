/**
 * tests/js/sync_link_client_check.js
 *
 * P3（ワンタイムリンク）の static/shared/sync-client.js 側の回帰テスト。
 *
 * 司令塔の完了条件(G)に対応する3点を検証する:
 *   1. redeem結果の適用が wishSave() / savedDecksSet() 経由で行われること
 *      （window にそれらが存在する場合。P2と同じ「経由で適用」の経路を再利用しているか）
 *   2. 適用後に新しい sync_id と「両方」の rev が保存されること
 *      （2026-08-13データ消失バグと同じ構造の再発防止。片方だけ進めるrev=0プレースホルダを作らない）
 *   3. previewLink() だけでは何も変更されない（localStorageのstate・wishlist・decksが不変）こと
 * 加えて、wishSave/savedDecksSet が存在しないページ（本番の /sync ページを想定）でも
 * applyRedeemResult がフォールバックで正しく localStorage に書き込むことを確認する
 * （「合流できたように見えて実は書かれていない」を防ぐ）。
 *
 * static/shared/sync-client.js のソースをそのままロードして検証する（別実装で検証しない。
 * tests/js/sync_client_data_loss_check.js と同じ流儀）。
 *
 * 実行: node tests/js/sync_link_client_check.js
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

// ── シナリオ1: redeem結果が wishSave/savedDecksSet 経由で適用され、両方のrevが保存される ──
async function scenario1_redeem_applies_via_wishSave_and_savedDecksSet() {
  console.log('\n=== シナリオ1: redeemLink() が wishSave/savedDecksSet 経由で適用し、両方のrevを保存するか ===');
  const localStorage = makeLocalStorage({
    cardprice_sync_state: JSON.stringify({ sync_id: 'old-id', wishlist_rev: 2, decks_rev: 1 }),
    cardprice_wishlist: JSON.stringify([{ name: 'ローカル購入候補', rarity: '', qty: 1 }]),
    cardprice_saved_decks: JSON.stringify([{ id: 'd-local', name: 'ローカルデッキ', text: '', main: [], ex: [], updated: 1 }]),
  });
  let wishSaveCalledWith = null;
  let savedDecksSetCalledWith = null;
  let redeemRequestBody = null;
  const windowObj = {
    document: fakeDocument(),
    localStorage,
    fetch: (url, opts) => {
      if (url === '/api/sync/link/redeem') {
        redeemRequestBody = JSON.parse(opts.body);
        return Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve({
            ok: true,
            sync_id: 'new-merged-id',
            wishlist: { rev: 5, items: [{ name: 'マージ後の購入候補', rarity: '', qty: 1 }] },
            decks: { rev: 2, items: [{ id: 'd-merged', name: 'マージ後のデッキ', text: '', main: [], ex: [], updated: 9 }] },
          }),
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
    },
    wishSave: (list) => { wishSaveCalledWith = list; },
    savedDecksSet: (list) => { savedDecksSetCalledWith = list; },
  };
  const SyncClient = loadSyncClient(windowObj);
  const data = await SyncClient.redeemLink('tok-abc');

  check('redeemLinkがredeemエンドポイントにold_sync_id(既存state由来)を送った',
    !!redeemRequestBody && redeemRequestBody.old_sync_id === 'old-id', JSON.stringify(redeemRequestBody));
  check('redeemLinkが成功結果を返した', !!data && data.ok === true);

  check('wishSave() が呼ばれ、マージ後の購入候補が渡された',
    !!wishSaveCalledWith && wishSaveCalledWith.length === 1
    && wishSaveCalledWith[0].name === 'マージ後の購入候補', JSON.stringify(wishSaveCalledWith));
  check('savedDecksSet() が呼ばれ、マージ後のデッキが渡された',
    !!savedDecksSetCalledWith && savedDecksSetCalledWith.length === 1
    && savedDecksSetCalledWith[0].name === 'マージ後のデッキ', JSON.stringify(savedDecksSetCalledWith));

  const state = JSON.parse(localStorage.getItem('cardprice_sync_state'));
  check('新しいsync_idが保存された', state.sync_id === 'new-merged-id', JSON.stringify(state));
  check('【P2データ消失バグと同じ構造の再発防止】wishlist_revが保存された', state.wishlist_rev === 5, JSON.stringify(state));
  check('【P2データ消失バグと同じ構造の再発防止】decks_revも同時に保存された(rev=0のまま放置しない)',
    state.decks_rev === 2, JSON.stringify(state));
}

// ── シナリオ2: wishSave/savedDecksSetが存在しないページ（/sync想定）でもフォールバックで
//    localStorageへ正しく書き込まれる（黙って何も反映されない、を防ぐ） ──
async function scenario2_fallback_when_no_wishSave_present() {
  console.log('\n=== シナリオ2: wishSave/savedDecksSetが無いページでも合流結果が保存されるか（フォールバック） ===');
  const localStorage = makeLocalStorage({});
  const windowObj = {
    document: fakeDocument(),
    localStorage,
    fetch: () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) }),
    // wishSave / savedDecksSet を意図的に定義しない（/sync ページの実態を模倣）
  };
  const SyncClient = loadSyncClient(windowObj);
  const applied = SyncClient.applyRedeemResult({
    sync_id: 'sid-fallback',
    wishlist: { rev: 3, items: [{ name: 'フォールバック購入候補', rarity: '', qty: 2 }] },
    decks: { rev: 7, items: [{ id: 'd1', name: 'フォールバックデッキ', text: '', main: [], ex: [], updated: 1 }] },
  });
  check('applyRedeemResult が true を返した(適用完了)', applied === true);

  const savedWishlist = JSON.parse(localStorage.getItem('cardprice_wishlist'));
  const savedDecks = JSON.parse(localStorage.getItem('cardprice_saved_decks'));
  check('フォールバックでも cardprice_wishlist に正しく書き込まれた',
    Array.isArray(savedWishlist) && savedWishlist.length === 1 && savedWishlist[0].name === 'フォールバック購入候補',
    JSON.stringify(savedWishlist));
  check('フォールバックでも cardprice_saved_decks に正しく書き込まれた',
    Array.isArray(savedDecks) && savedDecks.length === 1 && savedDecks[0].name === 'フォールバックデッキ',
    JSON.stringify(savedDecks));

  const state = JSON.parse(localStorage.getItem('cardprice_sync_state'));
  check('フォールバック経路でも新しいsync_idと両方のrevが保存された',
    state.sync_id === 'sid-fallback' && state.wishlist_rev === 3 && state.decks_rev === 7,
    JSON.stringify(state));
}

// ── シナリオ3: previewLink() だけでは何も変更されない（副作用なし） ──
async function scenario3_preview_has_no_side_effects() {
  console.log('\n=== シナリオ3: previewLink() だけでは localStorage・wishSave等に何も影響しないか ===');
  const initialState = { sync_id: 'unchanged-id', wishlist_rev: 9, decks_rev: 9 };
  const localStorage = makeLocalStorage({
    cardprice_sync_state: JSON.stringify(initialState),
    cardprice_wishlist: JSON.stringify([{ name: '変化しないはずの購入候補', rarity: '', qty: 1 }]),
  });
  let wishSaveCalled = false;
  let savedDecksSetCalled = false;
  const windowObj = {
    document: fakeDocument(),
    localStorage,
    fetch: (url) => {
      if (url === '/api/sync/link/preview') {
        return Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve({ ok: true, remote: { wishlist_count: 3, decks_count: 1 }, expires_in: 480 }),
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
    },
    wishSave: () => { wishSaveCalled = true; },
    savedDecksSet: () => { savedDecksSetCalled = true; },
  };
  const SyncClient = loadSyncClient(windowObj);
  const res = await SyncClient.previewLink('tok-preview');

  check('previewLinkが下見結果を返した', !!res && res.ok === true && res.remote.wishlist_count === 3);
  check('previewLinkだけではwishSaveが呼ばれない', wishSaveCalled === false);
  check('previewLinkだけではsavedDecksSetが呼ばれない', savedDecksSetCalled === false);

  const stateAfter = JSON.parse(localStorage.getItem('cardprice_sync_state'));
  check('previewLinkだけではcardprice_sync_stateが変化しない',
    JSON.stringify(stateAfter) === JSON.stringify(initialState), JSON.stringify(stateAfter));
  const wishlistAfter = JSON.parse(localStorage.getItem('cardprice_wishlist'));
  check('previewLinkだけではcardprice_wishlistが変化しない',
    wishlistAfter.length === 1 && wishlistAfter[0].name === '変化しないはずの購入候補');
}

// ── シナリオ4: この端末が未同期(sync_id無し)の場合、old_sync_idはnullで送られる ──
async function scenario4_redeem_sends_null_old_sync_id_when_unsynced() {
  console.log('\n=== シナリオ4: 未同期端末からのredeemはold_sync_id:nullを送るか ===');
  const localStorage = makeLocalStorage({}); // cardprice_sync_state 無し = 未同期
  let redeemRequestBody = null;
  const windowObj = {
    document: fakeDocument(),
    localStorage,
    fetch: (url, opts) => {
      if (url === '/api/sync/link/redeem') {
        redeemRequestBody = JSON.parse(opts.body);
        return Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve({
            ok: true, sync_id: 'brand-new-id',
            wishlist: { rev: 1, items: [] }, decks: { rev: 1, items: [] },
          }),
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
    },
  };
  const SyncClient = loadSyncClient(windowObj);
  await SyncClient.redeemLink('tok-fresh');
  check('未同期端末はold_sync_id:nullを送る', redeemRequestBody && redeemRequestBody.old_sync_id === null,
    JSON.stringify(redeemRequestBody));
}

// ── シナリオ5: 発行側 startLinkShare() — sync_id未発行・ローカルデータも無ければ
//    APIを一切呼ばず no_local_data を返す（設計文書 §8 遅延発行の条件） ──
async function scenario5_start_link_share_no_data_guard() {
  console.log('\n=== シナリオ5: startLinkShare() はデータが無い端末ではAPIを呼ばないか ===');
  const localStorage = makeLocalStorage({});
  let fetchCalled = false;
  const windowObj = {
    document: fakeDocument(),
    localStorage,
    fetch: () => { fetchCalled = true; return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) }); },
  };
  const SyncClient = loadSyncClient(windowObj);
  const res = await SyncClient.startLinkShare();
  check('データが無ければreason:no_local_dataを返す', !!res && res.ok === false && res.reason === 'no_local_data', JSON.stringify(res));
  check('この場合fetchは一切呼ばれない', fetchCalled === false);
}

// ── シナリオ6: startLinkShare() — sync_id未発行だがローカルにデータがあれば
//    先にinitしてからlink/createを呼ぶ（§8） ──
async function scenario6_start_link_share_inits_then_creates_link() {
  console.log('\n=== シナリオ6: startLinkShare() は未発行端末でデータがあれば先にinitしてからlink/createを呼ぶか ===');
  const localStorage = makeLocalStorage({
    cardprice_wishlist: JSON.stringify([{ name: '未発行端末の購入候補', rarity: '', qty: 1 }]),
  });
  const calledUrls = [];
  const windowObj = {
    document: fakeDocument(),
    localStorage,
    fetch: (url, opts) => {
      calledUrls.push(url);
      if (url === '/api/sync/init') {
        return Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve({ ok: true, sync_id: 'freshly-inited', wishlist_rev: 1, decks_rev: 1 }),
        });
      }
      if (url === '/api/sync/link/create') {
        const body = JSON.parse(opts.body);
        check('link/createには直前にinitで得たsync_idが渡された', body.sync_id === 'freshly-inited', JSON.stringify(body));
        return Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve({ ok: true, token: 'tok-x', url: 'https://example/sync?t=tok-x', expires_in: 600 }),
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
    },
  };
  const SyncClient = loadSyncClient(windowObj);
  const res = await SyncClient.startLinkShare();
  check('init → link/create の順で呼ばれた', calledUrls[0] === '/api/sync/init' && calledUrls[1] === '/api/sync/link/create', JSON.stringify(calledUrls));
  check('最終的にトークン付きの結果が返る', !!res && res.ok === true && res.token === 'tok-x', JSON.stringify(res));
}

// ── シナリオ7: startLinkShare() — 既にsync_idがある端末はinitを呼ばず直接link/create ──
async function scenario7_start_link_share_skips_init_when_already_synced() {
  console.log('\n=== シナリオ7: startLinkShare() は既に同期中ならinitを呼ばないか ===');
  const localStorage = makeLocalStorage({
    cardprice_sync_state: JSON.stringify({ sync_id: 'already-synced', wishlist_rev: 3, decks_rev: 3 }),
  });
  const calledUrls = [];
  const windowObj = {
    document: fakeDocument(),
    localStorage,
    fetch: (url, opts) => {
      calledUrls.push(url);
      const body = JSON.parse(opts.body);
      check('既存のsync_idがそのまま渡された', body.sync_id === 'already-synced', JSON.stringify(body));
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({ ok: true, token: 'tok-y', url: 'https://example/sync?t=tok-y', expires_in: 600 }),
      });
    },
  };
  const SyncClient = loadSyncClient(windowObj);
  await SyncClient.startLinkShare();
  check('initは呼ばれずlink/createのみ', calledUrls.length === 1 && calledUrls[0] === '/api/sync/link/create', JSON.stringify(calledUrls));
}

// ── シナリオ8: unlinkThisDevice() — 成功したら新しいsync_id・revに状態を張り替える ──
async function scenario8_unlink_replaces_state() {
  console.log('\n=== シナリオ8: unlinkThisDevice() は成功後に新しいsync_id・revへ張り替えるか ===');
  const localStorage = makeLocalStorage({
    cardprice_sync_state: JSON.stringify({ sync_id: 'old-linked-id', wishlist_rev: 8, decks_rev: 4 }),
  });
  let unlinkRequestBody = null;
  const windowObj = {
    document: fakeDocument(),
    localStorage,
    fetch: (url, opts) => {
      if (url === '/api/sync/unlink') {
        unlinkRequestBody = JSON.parse(opts.body);
        return Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve({ ok: true, sync_id: 'unlinked-new-id', wishlist_rev: 1, decks_rev: 1 }),
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
    },
  };
  const SyncClient = loadSyncClient(windowObj);
  const res = await SyncClient.unlinkThisDevice();
  check('unlinkは現在のsync_idを送った', unlinkRequestBody && unlinkRequestBody.sync_id === 'old-linked-id', JSON.stringify(unlinkRequestBody));
  check('unlinkThisDeviceが成功結果を返した', !!res && res.ok === true);
  const state = JSON.parse(localStorage.getItem('cardprice_sync_state'));
  check('ローカル状態が新しいsync_id・revに張り替わった',
    state.sync_id === 'unlinked-new-id' && state.wishlist_rev === 1 && state.decks_rev === 1, JSON.stringify(state));
}

(async () => {
  await scenario1_redeem_applies_via_wishSave_and_savedDecksSet();
  await scenario2_fallback_when_no_wishSave_present();
  await scenario3_preview_has_no_side_effects();
  await scenario4_redeem_sends_null_old_sync_id_when_unsynced();
  await scenario5_start_link_share_no_data_guard();
  await scenario6_start_link_share_inits_then_creates_link();
  await scenario7_start_link_share_skips_init_when_already_synced();
  await scenario8_unlink_replaces_state();

  console.log('\n' + (failures === 0 ? 'ALL OK' : failures + ' FAILURE(S)'));
  process.exit(failures === 0 ? 0 : 1);
})();
