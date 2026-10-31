// 店舗検索の混雑応答（2026-10-22）の画面側を、実物の ShopBusy・SearchStatus・doSearch で検証する。
// - fetch で読む画面: X-Search-Busy の応答は本文を読まずに数秒後に取り直し、「順番待ち」を出す
// - 単品検索（EventSource）: busy イベントで閉じ、数秒後に同じ検索を張り直す。新しい検索を始めたら張り直さない
// - 完了の表示: 混雑で取れなかった店を「取得失敗」と分けて書く
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert/strict');
const root = path.join(__dirname, '../..');
const main = fs.readFileSync(path.join(root, 'static/js/index-main.js'), 'utf8');
function block(start, end) {
  const a = main.indexOf(start), b = main.indexOf(end, a + start.length);
  assert(a >= 0 && b > a, '抽出失敗: ' + start);
  return main.slice(a, b);
}

// ── 1) ShopBusy.fetch ──
function headers(map) { return { get: (k) => (k in map ? map[k] : null) }; }
function busyRes() { let cancelled = false; return { headers: headers({ 'X-Search-Busy': '1', 'Retry-After': '4' }), body: { cancel() { cancelled = true; } }, get cancelled() { return cancelled; } }; }
const okRes = { headers: headers({}), body: {}, ok: true };

(async () => {
  const sleeps = [];
  const box = { console, Number, Promise, Error, setTimeout: (fn, ms) => { sleeps.push(ms); fn(); return 1; }, clearTimeout() {} };
  vm.createContext(box);
  vm.runInContext(fs.readFileSync(path.join(root, 'static/js/shop-busy.js'), 'utf8') + ';this.ShopBusy=ShopBusy;', box);
  const SB = box.ShopBusy;

  // 混雑2回のあとに通常の応答 → 2回待って3回目を返す
  let calls = 0; const seq = [busyRes(), busyRes(), okRes];
  box.fetch = async (url, init) => { calls++; assert.equal(url, '/api/deck?cards=x'); assert.equal(init.method, 'POST'); return seq.shift(); };
  const waits = [], resumes = [];
  const res = await SB.fetch('/api/deck?cards=x', { method: 'POST' }, { onWait: (n) => waits.push(n), onResume: () => resumes.push(1) });
  assert.equal(res, okRes);
  assert.equal(calls, 3);
  assert.deepEqual(waits, [1, 2]);
  assert.equal(resumes.length, 2);
  assert.deepEqual(sleeps, [4000, 4000], 'Retry-After 秒だけ待つ');

  // 上限まで混雑が続いたら Error（呼び出し側の catch でエラー表示）
  box.fetch = async () => busyRes();
  await assert.rejects(SB.fetch('/x', undefined, {}), /混雑/);

  // 古くなった（別のデッキを開いた）ら取り直さず、そのまま返す
  calls = 0; box.fetch = async () => { calls++; return busyRes(); };
  const staleRes = await SB.fetch('/x', undefined, { isStale: () => true });
  assert(SB.isBusy(staleRes));
  assert.equal(calls, 1);

  // 中止（AbortController）は待ちの途中でも AbortError
  const box2 = { console, Number, Promise, Error, DOMException, setTimeout: () => 1, clearTimeout() {} };
  vm.createContext(box2);
  vm.runInContext(fs.readFileSync(path.join(root, 'static/js/shop-busy.js'), 'utf8') + ';this.ShopBusy=ShopBusy;', box2);
  const ctl = new AbortController();
  box2.fetch = async () => busyRes();
  const pending = box2.ShopBusy.fetch('/x', { signal: ctl.signal }, {});
  await new Promise((r) => setImmediate(r));
  ctl.abort();
  await assert.rejects(pending, (e) => e.name === 'AbortError');

  // ── 2) SearchStatus.message ──
  const sbox = {};
  vm.createContext(sbox);
  vm.runInContext(fs.readFileSync(path.join(root, 'static/js/search-status.js'), 'utf8') + ';this.SearchStatus=SearchStatus;', sbox);
  const msg = (d) => sbox.SearchStatus.message(d);
  assert.equal(msg({ failed_shops: ['A'], successful_shops: ['B', 'C'], shop_count: 3, status: 'partial' }),
    '3店舗中2店舗取得・1店舗で取得失敗。表示価格は取得できた範囲の参考値です（A）。', '混雑が無いときは従来どおり');
  const both = msg({ failed_shops: ['A', 'B'], busy_shops: ['B'], successful_shops: ['C'], shop_count: 3, status: 'partial' });
  assert(both.includes('1店舗で取得失敗（A）') && both.includes('1店舗は混雑で今回は取得できず') && both.includes('（B）'), both);
  assert.equal(msg({ failed_shops: ['A'], busy_shops: ['A'], successful_shops: [], shop_count: 1, status: 'failed' }),
    '混雑のため店舗から取得できませんでした。少し時間をおいて再度お試しください。');
  assert.equal(msg({ failed_shops: ['A'], successful_shops: [], shop_count: 1, status: 'failed' }),
    '全店舗の取得に失敗しました。時間をおいて再試行してください。');

  // ── 3) 単品検索（doSearch）の張り直し ──
  function element(id) {
    const cls = new Set();
    return { id, value: '', innerHTML: '', textContent: '', style: {}, dataset: {}, className: '', children: [],
      classList: { add: (c) => cls.add(c), remove: (c) => cls.delete(c), contains: (c) => cls.has(c), toggle() {} },
      querySelector() { return this._q || (this._q = element('q')); }, querySelectorAll() { return []; },
      appendChild(c) { this.children.push(c); }, insertBefore() {}, addEventListener() {}, remove() { this.removed = true; },
      parentNode: { insertBefore() {} }, setAttribute() {}, removeAttribute() {} };
  }
  const els = new Map();
  const get = (id) => { if (!els.has(id)) els.set(id, element(id)); return els.get(id); };
  const sources = [], timers = new Map(), gaCalls = [], mercari = [];
  let timerSeq = 0;
  class FakeES { constructor(url) { this.url = url; this.closed = false; sources.push(this); } close() { this.closed = true; } }
  const sandbox = {
    console, URL, Number, Promise, Object, Set, JSON, URLSearchParams, encodeURIComponent, Error,
    window: { MercariRef: { load: (q) => mercari.push(q), clear() {} } },
    document: { getElementById: get, createElement: () => element('new'), querySelector: () => get('previewInfo'), querySelectorAll: () => [] },
    EventSource: FakeES,
    fetch: () => new Promise(() => {}),
    setTimeout(fn, ms) { const id = ++timerSeq; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    _ga(name, p) { gaCalls.push([name, p]); }, _setSearchLayout() {}, _setPriceSectionsVisible() {}, _updateRarityBanner() {},
    renderAll() {}, renderTable() {}, SearchStatus: { show() {} }, TABLE_PAGE_SIZE: 20,
    _prefetchCardImage() {}, _prefetchCardInfo() {}, _hideSearchPreview() {}, _syncMercariView() {}, _resetBuyInline() {}, _syncPriceTrendView() {}, closeSuggest() {},
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(root, 'static/js/shop-busy.js'), 'utf8') + ';this.ShopBusy=ShopBusy;', sandbox);
  vm.runInContext(fs.readFileSync(path.join(root, 'static/js/search-progress.js'), 'utf8') + ';this.SearchProgress=SearchProgress;', sandbox);
  vm.runInContext('let D=[],rarityFilter="",tableShowCount=0,_currentCardName="";', sandbox);
  vm.runInContext(block('function esc(', 'function rankingLinkClick('), sandbox);
  vm.runInContext(block('let _searchES=null;', '// ── Autocomplete'), sandbox);
  sandbox.MercariRef = sandbox.window.MercariRef;
  const run = (c) => vm.runInContext(c, sandbox);
  const send = (es, o) => es.onmessage({ data: JSON.stringify(o) });
  const timerList = () => [...timers.values()];

  get('q').value = '灰流うらら';
  run('doSearch({confirmed:true})');
  assert.equal(sources.length, 1);
  const url0 = sources[0].url;
  send(sources[0], { type: 'busy', retry_after: 4 });
  assert(sources[0].closed, '混雑応答で閉じる');
  assert.equal(mercari.length, 0, '混雑応答ではメルカリを取りに行かない');
  // 進み具合は先出しのカード欄に足したバー1本（2026-10-28 店ごとの札をやめた）
  const bar = get('previewInfo').children[get('previewInfo').children.length - 1];
  const barLabel = () => bar.querySelector('.sp-label').textContent;
  assert.equal(barLabel(), sandbox.SearchProgress.BUSY_LABEL, '順番待ちを出す');
  const retry = timerList();
  assert.equal(retry.length, 1, '張り直しのタイマーだけが残る（60秒タイムアウトは止める）');
  assert.equal(retry[0].ms, 4000);
  retry[0].fn();
  assert.equal(sources.length, 2, '数秒後に張り直す');
  assert.equal(sources[1].url, url0, '同じ検索を張り直す');
  assert.equal(gaCalls.filter(([n]) => n === 'search_submit').length, 1, '張り直しは検索回数に数えない');
  // 張り直した検索は普段どおり進む（shops で順番待ちの文言を戻す）
  send(sources[1], { type: 'shops', shops: ['A', 'B'] });
  assert.equal(barLabel(), '各店の価格を確認中', '店の一覧が届いたら順番待ちの文言を戻す');
  assert.equal(mercari.length, 1);
  send(sources[1], { type: 'waiting', queued_shops: ['B'] });
  assert.equal(barLabel(), sandbox.SearchProgress.BUSY_LABEL, '取得口の順番待ちの店があるあいだは順番待ち');
  send(sources[1], { type: 'waiting', queued_shops: [] });
  assert.equal(barLabel(), '各店の価格を確認中');
  send(sources[1], { type: 'shop_done', shop: 'A', status: 'busy', count: 2, results: [] });
  assert.equal(bar.querySelector('.sp-fill').style.width, '50%', '混雑で取れなかった店も終わった店として進める');
  send(sources[1], { type: 'shop_error', shop: 'B' });
  assert.equal(bar.querySelector('.sp-fill').style.width, '100%');

  // 待っている間に別の検索を始めたら、前の検索は張り直さない
  get('q').value = '増殖するG';
  run('doSearch({confirmed:true})');
  send(sources[2], { type: 'busy', retry_after: 4 });
  const pendingRetry = timerList()[0];
  get('q').value = '別のカード';
  run('doSearch({confirmed:true})');
  assert.equal(sources.length, 4);
  pendingRetry.fn();
  assert.equal(sources.length, 4, '世代が進んだ検索は張り直さない');

  console.log('shop_busy_check: ok');
})().catch((e) => { console.error(e); process.exit(1); });
