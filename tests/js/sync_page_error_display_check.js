/**
 * tests/js/sync_page_error_display_check.js
 *
 * 2026-08-17 本番不具合#2の回帰テスト: 「同期する」ボタンを押して失敗しても
 * 画面に一切表示されない問題。実物の templates/sync.html（Flaskでレンダリング済みのHTML。
 * 引数で渡される）と実物の static/shared/sync-client.js の両方をそのままロードし、
 * 最小限のフェイクDOM上で「/sync?t=... を開く→同期するボタンを押す→429が返る」という
 * 実際の操作フローを再現して、画面（フェイクDOMの要素）に理由付きのメッセージが
 * 現れることを検証する。関数単体の戻り値だけでなく、DOMへの反映まで確認する。
 *
 * 使い方: node sync_page_error_display_check.js <rendered_sync_html_path>
 * 終了コード0=全項目OK、非0=失敗（stderrにFAIL行）。
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SYNC_CLIENT_PATH = path.join(__dirname, '..', '..', 'static', 'shared', 'sync-client.js');
const renderedHtmlPath = process.argv[2];
if (!renderedHtmlPath) {
  console.error('FAIL: 引数にレンダリング済み /sync のHTMLパスを指定してください');
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

// ── フェイクDOM（最小限。id引きの要素・classList・addEventListener・valueだけ実装） ──
class FakeElement {
  constructor(id) {
    this.id = id;
    this._classes = new Set();
    this.style = {};
    this.textContent = '';
    this.value = '';
    this.placeholder = '';
    this.disabled = false;
    this.innerHTML = '';
    this._listeners = {};
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
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  dispatchClick() { (this._listeners.click || []).forEach((fn) => fn()); }
  select() {}
}

function makeFakeDocument(ids) {
  const elements = {};
  ids.forEach((id) => { elements[id] = new FakeElement(id); });
  return {
    getElementById: (id) => elements[id] || null,
    _elements: elements,
  };
}

function extractInlineScript(html) {
  const re = /<script(?![^>]*src=)[^>]*>([\s\S]*?)<\/script>/g;
  const m = re.exec(html);
  if (!m) throw new Error('rendered HTMLからインラインscriptを抽出できませんでした');
  return m[1];
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

const SYNC_HTML_IDS = [
  'stateLoading', 'stateConfirm', 'stateDone', 'stateInvalid',
  'localWishCount', 'localDeckCount', 'remoteWishCount', 'remoteDeckCount',
  'confirmTimer', 'confirmError', 'btnCancel', 'btnRedeem',
  'doneWishCount', 'doneDeckCount', 'invalidTitle', 'invalidBody',
];

async function scenario_redeem_429_shows_message_and_reenables_button() {
  console.log('\n=== シナリオ: 「同期する」押下→429 で画面にメッセージが出て、ボタンが再度押せる状態に戻るか ===');
  const html = fs.readFileSync(renderedHtmlPath, 'utf8');
  const pageScript = extractInlineScript(html);
  const syncClientCode = fs.readFileSync(SYNC_CLIENT_PATH, 'utf8');

  const doc = makeFakeDocument(SYNC_HTML_IDS);
  const localStorageStore = {};
  const fakeLocalStorage = {
    getItem: (k) => (k in localStorageStore ? localStorageStore[k] : null),
    setItem: (k, v) => { localStorageStore[k] = String(v); },
    removeItem: (k) => { delete localStorageStore[k]; },
  };

  let redeemCallCount = 0;
  const fakeWindow = {
    document: doc,
    localStorage: fakeLocalStorage,
    location: { search: '?t=tok-429test', href: '' },
    fetch: (url, opts) => {
      if (url === '/api/sync/link/preview') {
        return Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve({ ok: true, remote: { wishlist_count: 2, decks_count: 1 }, expires_in: 500 }),
        });
      }
      if (url === '/api/sync/link/redeem') {
        redeemCallCount++;
        return Promise.resolve({ ok: false, status: 429, json: () => Promise.resolve({ error: 'too many' }) });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
    },
  };

  const sandbox = {
    window: fakeWindow,
    document: doc,
    fetch: fakeWindow.fetch,
    localStorage: fakeLocalStorage,
    location: fakeWindow.location,
    URLSearchParams,
    setTimeout, clearTimeout, setInterval, clearInterval,
    console, Promise, JSON, Object, Math, Error, Array, String,
  };
  vm.createContext(sandbox);
  vm.runInContext(syncClientCode, sandbox, { filename: 'sync-client.js' });
  vm.runInContext(pageScript, sandbox, { filename: 'sync.html-inline.js' });

  // previewLink() の非同期解決を待つ（確認画面が表示されるまで）
  await sleep(50);
  check('プレビュー成功後に確認画面(stateConfirm)が表示された',
    doc._elements.stateConfirm.classList.contains('hidden') === false);

  const btnRedeem = doc._elements.btnRedeem;
  check('同期するボタンは最初は押せる状態', btnRedeem.disabled === false);

  // 「同期する」ボタンを押す（実際のクリックイベントを模したハンドラ呼び出し）
  btnRedeem.dispatchClick();
  await sleep(50);

  check('redeemLink相当のfetchが呼ばれた', redeemCallCount === 1);
  check('429失敗後、確認画面から失敗画面へは強制遷移していない（トークンはまだ有効なため）',
    doc._elements.stateInvalid.classList.contains('hidden') === true);
  check('confirmError に理由付きメッセージが表示された',
    doc._elements.confirmError.classList.contains('hidden') === false
    && doc._elements.confirmError.textContent.length > 0,
    JSON.stringify(doc._elements.confirmError.textContent));
  check('メッセージの中身が429固有（「混み合って」等）を含む',
    /混み合|待/.test(doc._elements.confirmError.textContent),
    doc._elements.confirmError.textContent);
  check('失敗後、ボタンが再び押せる状態に戻っている（押しっぱなし状態にしない）',
    btnRedeem.disabled === false, 'disabled=' + btnRedeem.disabled);
  check('ボタンの文言も「同期する」に戻っている', btnRedeem.textContent === '同期する',
    btnRedeem.textContent);
}

async function scenario_preview_network_error_shows_invalid_screen() {
  console.log('\n=== シナリオ: previewLink自体が失敗しても無言のままにならないか ===');
  const html = fs.readFileSync(renderedHtmlPath, 'utf8');
  const pageScript = extractInlineScript(html);
  const syncClientCode = fs.readFileSync(SYNC_CLIENT_PATH, 'utf8');

  const doc = makeFakeDocument(SYNC_HTML_IDS);
  const fakeLocalStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  const fakeWindow = {
    document: doc,
    localStorage: fakeLocalStorage,
    location: { search: '?t=tok-neterr', href: '' },
    fetch: () => Promise.reject(new Error('network down')),
  };
  const sandbox = {
    window: fakeWindow, document: doc, fetch: fakeWindow.fetch, localStorage: fakeLocalStorage,
    location: fakeWindow.location, URLSearchParams,
    setTimeout, clearTimeout, setInterval, clearInterval,
    console, Promise, JSON, Object, Math, Error, Array, String,
  };
  vm.createContext(sandbox);
  vm.runInContext(syncClientCode, sandbox, { filename: 'sync-client.js' });
  vm.runInContext(pageScript, sandbox, { filename: 'sync.html-inline.js' });

  await sleep(50);
  check('previewLink自体がネットワークエラーでも失敗画面(stateInvalid)が表示される',
    doc._elements.stateInvalid.classList.contains('hidden') === false);
  check('失敗画面に案内文が入っている', doc._elements.invalidBody.textContent.length > 0);
}

(async () => {
  await scenario_redeem_429_shows_message_and_reenables_button();
  await scenario_preview_network_error_shows_invalid_screen();

  console.log('\n' + (failures === 0 ? 'ALL OK' : failures + ' FAILURE(S)'));
  process.exit(failures === 0 ? 0 : 1);
})();
