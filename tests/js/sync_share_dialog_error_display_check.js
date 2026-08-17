/**
 * tests/js/sync_share_dialog_error_display_check.js
 *
 * 2026-08-17 本番不具合#2の回帰テスト（index.html側）:「他の端末でも見る」「同期を解除」
 * を押して失敗しても画面に一切表示されない問題。
 *
 * templates/index.html は巨大な単一スクリプトで多数の関数が相互依存しており、
 * ページ全体をそのままロードして検証するのは現実的ではない。そのため、P3で追加した
 * 「端末間同期: ...ダイアログ・状態表示・解除」ブロック（開始コメントから
 * unlinkThisDeviceSync() の終わりまで。他ページ関数に依存しない自己完結ブロック）だけを
 * 実際にレンダリングされたHTMLから抽出し、そのままフェイクDOM上で実行する。
 * 手書きで複製したコードではなく実物のテンプレート出力を対象にすることで、
 * テンプレートとテストの乖離（テストは通るが実物は直っていない）を防ぐ。
 *
 * 使い方: node sync_share_dialog_error_display_check.js <rendered_index_html_path>
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
    this.value = '';
    this.placeholder = '';
    this.innerHTML = '';
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
  select() {}
}

const DIALOG_IDS = [
  'syncShareOverlay', 'syncShareQr', 'syncShareUrl', 'syncShareTimer',
  'syncShareError', 'syncShareCopyBtn', 'wishSyncStatus',
];

function makeFakeDocument() {
  const elements = {};
  DIALOG_IDS.forEach((id) => { elements[id] = new FakeElement(id); });
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

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

function loadBlock(blockSource, windowObj) {
  const syncClientCode = fs.readFileSync(SYNC_CLIENT_PATH, 'utf8');
  const sandbox = {
    window: windowObj, document: windowObj.document, fetch: windowObj.fetch,
    localStorage: windowObj.localStorage,
    confirm: windowObj.confirm, alert: windowObj.alert,
    setTimeout, clearTimeout, setInterval, clearInterval,
    console, Promise, JSON, Object, Math, Error, Array, String,
  };
  vm.createContext(sandbox);
  vm.runInContext(syncClientCode, sandbox, { filename: 'sync-client.js' });
  vm.runInContext(blockSource, sandbox, { filename: 'index.html-sync-share-block.js' });
  return sandbox;
}

async function scenario_open_dialog_429_shows_message() {
  console.log('\n=== シナリオ: 「他の端末でも見る」ダイアログ — link/create が429でメッセージが出るか ===');
  const html = fs.readFileSync(renderedHtmlPath, 'utf8');
  const block = extractSyncShareBlock(html);

  const doc = makeFakeDocument();
  const localStorageStore = {
    cardprice_sync_state: JSON.stringify({ sync_id: 's1', wishlist_rev: 1, decks_rev: 1 }),
  };
  const fakeLocalStorage = {
    getItem: (k) => (k in localStorageStore ? localStorageStore[k] : null),
    setItem: (k, v) => { localStorageStore[k] = String(v); },
    removeItem: (k) => { delete localStorageStore[k]; },
  };
  const windowObj = {
    document: doc,
    localStorage: fakeLocalStorage,
    fetch: (url) => {
      if (url === '/api/sync/link/create') {
        return Promise.resolve({ ok: false, status: 429, json: () => Promise.resolve({ error: 'too many' }) });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
    },
    confirm: () => true,
    alert: () => {},
  };
  const sandbox = loadBlock(block, windowObj);

  sandbox.openSyncShareDialog();
  await sleep(50);

  check('ダイアログのオーバーレイが表示状態になった',
    doc._elements.syncShareOverlay.classList.contains('active') === true);
  check('syncShareError に理由付きメッセージが表示された',
    doc._elements.syncShareError.style.display === 'block' && doc._elements.syncShareError.textContent.length > 0,
    JSON.stringify({ display: doc._elements.syncShareError.style.display, text: doc._elements.syncShareError.textContent }));
  check('メッセージの中身が429固有（「混み合って」等）を含む',
    /混み合|待/.test(doc._elements.syncShareError.textContent),
    doc._elements.syncShareError.textContent);
}

async function scenario_unlink_429_shows_alert_with_reason() {
  console.log('\n=== シナリオ: 「同期を解除」— unlink が429でアラートに理由が出るか ===');
  const html = fs.readFileSync(renderedHtmlPath, 'utf8');
  const block = extractSyncShareBlock(html);

  const doc = makeFakeDocument();
  const localStorageStore = {
    cardprice_sync_state: JSON.stringify({ sync_id: 's1', wishlist_rev: 1, decks_rev: 1 }),
  };
  const fakeLocalStorage = {
    getItem: (k) => (k in localStorageStore ? localStorageStore[k] : null),
    setItem: (k, v) => { localStorageStore[k] = String(v); },
    removeItem: (k) => { delete localStorageStore[k]; },
  };
  const alerts = [];
  const windowObj = {
    document: doc,
    localStorage: fakeLocalStorage,
    fetch: (url) => {
      if (url === '/api/sync/unlink') {
        return Promise.resolve({ ok: false, status: 429, json: () => Promise.resolve({ error: 'too many' }) });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
    },
    confirm: () => true, // ユーザーが確認ダイアログでOKを押した想定
    alert: (msg) => { alerts.push(msg); },
  };
  const sandbox = loadBlock(block, windowObj);

  sandbox.unlinkThisDeviceSync();
  await sleep(50);

  check('unlink失敗時にalertが呼ばれた(=画面に何かしら表示される)', alerts.length === 1, JSON.stringify(alerts));
  check('alertの中身が429固有の理由を含む', alerts[0] && /混み合|待/.test(alerts[0]), JSON.stringify(alerts));
}

(async () => {
  await scenario_open_dialog_429_shows_message();
  await scenario_unlink_429_shows_alert_with_reason();

  console.log('\n' + (failures === 0 ? 'ALL OK' : failures + ' FAILURE(S)'));
  process.exit(failures === 0 ? 0 : 1);
})();
