/**
 * tests/js/sync_status_display_check.js
 *
 * 2026-08-18 実機バグの回帰テスト: 一度も他の端末と同期していない端末で
 * 「他の端末と同期中」と表示されるバグ。
 *
 * 原因: templates/index.html の _refreshSyncStatusUI() が sync_id の有無だけで
 * 表示を判定していた。sync_id は購入候補・デッキを1件でも作れば自動発行される
 * （設計文書 §8 遅延発行）ため、他端末と繋がっているかどうかとは無関係だった。
 *
 * 固定する仕様（司令塔の完了条件に対応）:
 *   1. sync_id があっても linked が false（または未確定=undefined）なら
 *      表示要素（#wishSyncStatus）は hidden のままであること
 *   2. linked が true なら表示される（hiddenが外れる）こと
 *
 * P3で追加した自己完結ブロック（「端末間同期: ...」開始コメントから
 * unlinkThisDeviceSync() の終わりまで）を、実際にレンダリングされたHTMLから
 * そのまま抽出して実行する（tests/js/sync_share_dialog_error_display_check.js と同じ方式）。
 * 手書きで複製したコードではなく実物のテンプレート出力を対象にすることで、
 * テンプレートとテストの乖離を防ぐ。_refreshSyncStatusUI 自体はネットワークを
 * 使わない同期関数なので、fetchモックは不要（localStorageの状態だけで検証できる）。
 *
 * 使い方: node sync_status_display_check.js <rendered_index_html_path>
 * 終了コード0=全項目OK、非0=失敗（stderrにFAIL行）。
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SYNC_CLIENT_PATH = path.join(__dirname, '..', '..', 'static', 'shared', 'sync-client.js');
const renderedHtmlPath = process.argv[2];
if (!renderedHtmlPath) {
  console.error('FAIL: 引数にレンダリング済み index.html のパスを指定してください');
  process.exit(1);
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

class FakeElement {
  constructor(id) {
    this.id = id;
    this._classes = new Set();
    this.style = {};
    this.textContent = '';
    const self = this;
    this.classList = {
      add: (c) => self._classes.add(c),
      remove: (c) => self._classes.delete(c),
      toggle: (c, force) => {
        if (force === undefined) { if (self._classes.has(c)) self._classes.delete(c); else self._classes.add(c); }
        else if (force) self._classes.add(c); else self._classes.delete(c);
      },
      contains: (c) => self._classes.has(c),
    };
  }
}

const DIALOG_IDS = [
  'syncShareOverlay', 'syncShareQr', 'syncShareUrl', 'syncShareTimer',
  'syncShareError', 'syncShareCopyBtn', 'wishSyncStatus',
];

function makeFakeDocument() {
  const elements = {};
  DIALOG_IDS.forEach((id) => { elements[id] = new FakeElement(id); });
  // 実物のHTMLでは #wishSyncStatus は初期状態で class="hidden" になっている
  // （静的HTMLの既定値。JSでの明示的な操作前は非表示がデフォルト）
  elements.wishSyncStatus.classList.add('hidden');
  return {
    getElementById: (id) => elements[id] || null,
    body: { style: {} },
    _elements: elements,
  };
}

// 「端末間同期: ...」ブロックだけを実レンダリング結果から抽出する（他ページ関数非依存の自己完結ブロック）
function extractSyncShareBlock(html) {
  const startMarker = 'let _syncShareTimerId=null;';
  const startIdx = html.indexOf(startMarker);
  if (startIdx === -1) throw new Error('開始マーカーが見つかりません: ' + startMarker);
  const endIdx = html.indexOf('</script>', startIdx);
  if (endIdx === -1) throw new Error('ブロックの終端(</script>)が見つかりません');
  return html.slice(startIdx, endIdx);
}

function makeLocalStorage(initial) {
  const store = Object.assign({}, initial || {});
  return {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };
}

function loadBlockWithState(blockSource, syncStateObj) {
  const doc = makeFakeDocument();
  const localStorageStore = {};
  if (syncStateObj !== null) {
    localStorageStore.cardprice_sync_state = JSON.stringify(syncStateObj);
  }
  const fakeLocalStorage = makeLocalStorage(localStorageStore);
  const windowObj = {
    document: doc,
    localStorage: fakeLocalStorage,
    fetch: () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) }),
    confirm: () => true,
    alert: () => {},
  };
  const syncClientCode = fs.readFileSync(SYNC_CLIENT_PATH, 'utf8');
  const sandbox = {
    window: windowObj, document: doc, fetch: windowObj.fetch, localStorage: fakeLocalStorage,
    confirm: windowObj.confirm, alert: windowObj.alert,
    setTimeout, clearTimeout, setInterval, clearInterval,
    console, Promise, JSON, Object, Math, Error, Array, String,
  };
  vm.createContext(sandbox);
  vm.runInContext(syncClientCode, sandbox, { filename: 'sync-client.js' });
  vm.runInContext(blockSource, sandbox, { filename: 'index.html-sync-share-block.js' });
  return { sandbox, doc };
}

function scenario_sync_id_without_linked_stays_hidden() {
  console.log('\n=== シナリオ1(本番バグの再現): sync_idはあるがlinkedがfalseなら非表示のまま ===');
  const html = fs.readFileSync(renderedHtmlPath, 'utf8');
  const block = extractSyncShareBlock(html);

  // 購入候補を1件登録しただけで自動発行されたsync_id。他端末とは一度も連携していない
  // （§8遅延発行そのものの状態。以前はこれだけで「同期中」と誤表示されていた）
  const { sandbox, doc } = loadBlockWithState(block, { sync_id: 's-never-linked', wishlist_rev: 1, decks_rev: 1, linked: false });
  sandbox._refreshSyncStatusUI();

  check('sync_idがあってもlinked:falseなら#wishSyncStatusはhiddenのまま',
    doc._elements.wishSyncStatus.classList.contains('hidden') === true);
}

function scenario_sync_id_with_linked_undefined_stays_hidden() {
  console.log('\n=== シナリオ2: linkedフィールドが未確定(undefined)のときも非表示のまま（旧クライアント/pull未完了を想定） ===');
  const html = fs.readFileSync(renderedHtmlPath, 'utf8');
  const block = extractSyncShareBlock(html);

  // linkedキー自体が無い状態（pullがまだ一度も完了していない・または旧バージョンの
  // localStorageが残っている端末を想定）。「未確定のうちは出さない」の確認
  const { sandbox, doc } = loadBlockWithState(block, { sync_id: 's-unknown', wishlist_rev: 1, decks_rev: 1 });
  sandbox._refreshSyncStatusUI();

  check('linkedが未確定(undefined)でも#wishSyncStatusはhiddenのまま',
    doc._elements.wishSyncStatus.classList.contains('hidden') === true);
}

function scenario_linked_true_shows_status() {
  console.log('\n=== シナリオ3: linked:trueなら「他の端末と同期中」が表示される ===');
  const html = fs.readFileSync(renderedHtmlPath, 'utf8');
  const block = extractSyncShareBlock(html);

  const { sandbox, doc } = loadBlockWithState(block, { sync_id: 's-linked', wishlist_rev: 3, decks_rev: 2, linked: true });
  sandbox._refreshSyncStatusUI();

  check('linked:trueなら#wishSyncStatusのhiddenが外れる（表示される）',
    doc._elements.wishSyncStatus.classList.contains('hidden') === false);
}

function scenario_no_sync_state_at_all_stays_hidden() {
  console.log('\n=== シナリオ4: 同期状態が全く無い端末（sync_id未発行）でも非表示のまま ===');
  const html = fs.readFileSync(renderedHtmlPath, 'utf8');
  const block = extractSyncShareBlock(html);

  const { sandbox, doc } = loadBlockWithState(block, null);
  sandbox._refreshSyncStatusUI();

  check('同期状態が無い端末では#wishSyncStatusはhiddenのまま',
    doc._elements.wishSyncStatus.classList.contains('hidden') === true);
}

// ── ここから: 表示だけでなく「pullの応答→ローカル状態」の伝播経路自体も検証する。
//    表示テスト（上記）だけだと、pullIfNeeded() 自身が data.linked を正しく
//    保存できていない場合を見逃す（localStorageへ直接linkedを仕込んでいるため） ──
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
function fakeDocument() { return { createElement: () => ({ style: {} }), body: { appendChild() {} } }; }

function loadSyncClientOnly(windowObj) {
  const code = fs.readFileSync(SYNC_CLIENT_PATH, 'utf8');
  const sandbox = {
    window: windowObj, document: windowObj.document, fetch: windowObj.fetch,
    localStorage: windowObj.localStorage,
    setTimeout, clearTimeout, console, Promise, JSON, Object, Math, Error, Array,
  };
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: 'sync-client.js' });
  return sandbox.window.SyncClient;
}

async function scenario_pull_persists_linked_true_into_local_state() {
  console.log('\n=== シナリオ5: pullIfNeeded() がサーバー応答のlinked:trueをローカル状態に保存するか ===');
  const localStorage = makeLocalStorage({
    cardprice_sync_state: JSON.stringify({ sync_id: 's1', wishlist_rev: 3, decks_rev: 2, linked: false }),
  });
  const windowObj = {
    document: fakeDocument(),
    localStorage,
    fetch: (url) => {
      if (url === '/api/sync/pull') {
        return Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve({
            ok: true, linked: true,
            wishlist: { unchanged: true }, decks: { unchanged: true },
          }),
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
    },
  };
  const SyncClient = loadSyncClientOnly(windowObj);
  await SyncClient.pullIfNeeded();

  const state = JSON.parse(localStorage.getItem('cardprice_sync_state'));
  check('pull応答のlinked:trueがローカル状態に反映された', state.linked === true, JSON.stringify(state));
}

async function scenario_pull_persists_linked_false_into_local_state() {
  console.log('\n=== シナリオ6: linked:falseに変わった場合もローカル状態が追従するか ===');
  const localStorage = makeLocalStorage({
    cardprice_sync_state: JSON.stringify({ sync_id: 's1', wishlist_rev: 3, decks_rev: 2, linked: true }),
  });
  const windowObj = {
    document: fakeDocument(),
    localStorage,
    fetch: (url) => {
      if (url === '/api/sync/pull') {
        return Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve({
            ok: true, linked: false,
            wishlist: { unchanged: true }, decks: { unchanged: true },
          }),
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
    },
  };
  const SyncClient = loadSyncClientOnly(windowObj);
  await SyncClient.pullIfNeeded();

  const state = JSON.parse(localStorage.getItem('cardprice_sync_state'));
  check('pull応答のlinked:falseがローカル状態に反映された', state.linked === false, JSON.stringify(state));
}

(async () => {
  scenario_sync_id_without_linked_stays_hidden();
  scenario_sync_id_with_linked_undefined_stays_hidden();
  scenario_linked_true_shows_status();
  scenario_no_sync_state_at_all_stays_hidden();
  await scenario_pull_persists_linked_true_into_local_state();
  await scenario_pull_persists_linked_false_into_local_state();

  console.log('\n' + (failures === 0 ? 'ALL OK' : failures + ' FAILURE(S)'));
  process.exit(failures === 0 ? 0 : 1);
})();
