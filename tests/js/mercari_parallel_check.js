// 検索SSEとメルカリ取得の並行化の配線を、実物の doSearch / _syncMercariView / MercariRef で検証する。
// （EventSource・fetch は偽物。検索開始と同時に取得し、店舗検索中も枠が出て、補正・0件・失敗で正しく動くか）
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert/strict');
const root = path.join(__dirname, '../..');
const main = fs.readFileSync(path.join(root, 'static/js/index-main.js'), 'utf8');
function block(start, end) {
  const a = main.indexOf(start), b = main.indexOf(end, a + start.length);
  assert(a >= 0 && b > a, '抽出失敗: ' + start);
  return main.slice(a, b);
}
function element(id) {
  const cls = new Set();
  return { id, value: '', innerHTML: '', textContent: '', style: {}, dataset: {}, open: false, disabled: false, children: [],
    classList: { add: (c) => cls.add(c), remove: (c) => cls.delete(c), contains: (c) => cls.has(c), toggle() {} },
    querySelector() { return this._q || (this._q = element('q')); }, appendChild() {}, insertBefore() {}, addEventListener() {}, remove() {},
    parentNode: { insertBefore() {} }, setAttribute() {}, removeAttribute() {} };
}
const els = new Map();
const get = (id) => { if (!els.has(id)) els.set(id, element(id)); return els.get(id); };
get('mercariRef').classList.add('hidden');
const fetches = [], sources = [], timers = new Map();
let timerSeq = 0;
class FakeES { constructor(url) { this.url = url; this.closed = false; sources.push(this); } close() { this.closed = true; } }
const sandbox = {
  console, URL, Number, Promise, Object, Set, JSON, URLSearchParams, encodeURIComponent,
  window: { RARITY_CONFIG: { 'ウルトラ': { order: 110 }, 'ノーマル': { order: 160 }, 'シークレット': { order: 40 } } },
  document: {
    getElementById: get,
    createElement: () => ({ set textContent(v) { this._t = String(v == null ? '' : v); }, get innerHTML() { return this._t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }, className: '', remove() {} }),
    querySelectorAll: () => [],
  },
  EventSource: FakeES,
  fetch(url) { return new Promise((resolve, reject) => fetches.push({ url, resolve, reject })); },
  setTimeout(fn) { const id = ++timerSeq; timers.set(id, fn); return id; },
  clearTimeout(id) { timers.delete(id); },
  _ga() {}, _setSearchLayout() {}, _setAffNote() {}, _setPriceSectionsVisible() {}, _updateRarityBanner() {}, renderTable() {}, closeSuggest() {},
  renderAll() {}, SearchStatus: { show() {} }, TABLE_PAGE_SIZE: 20,
};
vm.createContext(sandbox);
vm.runInContext('let D=[],rarityFilter="",tableShowCount=0,_currentCardName="";let _buyInlineGen=0,_buyInlineES=null,_buyInlineTimeoutId=null,_buyInlineD=null,_buyInlineFetched=false,_buyInlineFetchInFlight=false,_buyInlineFailedShops=[];', sandbox);
vm.runInContext(block('function esc(', 'function rankingLinkClick('), sandbox);
vm.runInContext(block('const AFF_PR_BADGE=', '// 注記ブロック') + block('const AFF_NOTE_TEXT=', 'function affPixel('), sandbox);
vm.runInContext(fs.readFileSync(path.join(root, 'static/js/mercari-ref.js'), 'utf8'), sandbox);
sandbox.MercariRef = sandbox.window.MercariRef; // ブラウザでは window のプロパティが素の識別子で参照できる
vm.runInContext([
  block('let _searchES=null;', '// ── Autocomplete'),
  block('function _resetBuyInline(', '// U-1:'),
  block('function _syncMercariView(', 'function esc('),
].join('\n'), sandbox);
const run = (c) => vm.runInContext(c, sandbox);
const tick = () => new Promise((r) => setImmediate(r));
const mercariCalls = () => fetches.filter((f) => f.url.startsWith('/api/mercari'));
const body = () => get('mercariRefBody').innerHTML;
const hidden = () => get('mercariRef').classList.contains('hidden');
const U = 'https://jp.mercari.com/search?keyword=x';
const BY = {
  'ウルトラ': { count: 5, min: 1000, median: 1500, min_is_floor: false, set: null },
  'ノーマル': { count: 1, min: 300, median: null, min_is_floor: true, set: null },
};
const okResp = (by) => ({ ok: true, status: 200, json: async () => ({ ok: true, unclassified_count: 0, fetched_count: 5, num_found: 5, search_url: U, by_rarity: by }) });
const send = (es, o) => es.onmessage({ data: JSON.stringify(o) });
const row = (r) => ({ rarity: r, price: 100 });
const lastSource = () => sources[sources.length - 1];

(async () => {
  // 1) 検索開始（EventSource を張った直後）にメルカリを取得する
  get('q').value = '灰流うらら';
  run('doSearch({validated:true})');
  assert.equal(sources.length, 1);
  assert(sources[0].url.startsWith('/api/search?'));
  assert.equal(mercariCalls().length, 1, '検索開始と同時に取得');
  assert.equal(mercariCalls()[0].url, '/api/mercari?name=' + encodeURIComponent('灰流うらら'));
  assert(hidden(), '応答前は枠を出さない');

  // 2) 店舗検索の途中（done 前）に応答が届く: 枠が出て、D 空なら確認中の注記のみ
  mercariCalls()[0].resolve(okResp(BY));
  await tick(); await tick();
  assert(!hidden(), '店舗検索中でも枠が出る');
  assert.equal(get('mercariRef').open, false);
  assert(body().includes('確認中のレアリティ（ウルトラ・ノーマル）があります') && !body().includes('mref-grid'), body());

  // 3) shop_done で店舗結果が溜まるたびに描き直す（再取得なし・開いたまま）
  get('mercariRef').open = true;
  send(sources[0], { type: 'shop_done', shop: 'A', status: 'ok', count: 1, results: [row('ウルトラ')] });
  assert.equal(mercariCalls().length, 1, '再取得しない');
  assert(body().includes('ウルトラ・出品5件の最安') && body().includes('確認中のレアリティ（ノーマル）があります'), body());
  assert.equal(get('mercariRef').open, true, '開いたまま');
  send(sources[0], { type: 'shop_done', shop: 'B', status: 'ok', count: 0, results: [] });
  assert(body().includes('確認中のレアリティ（ノーマル）'));

  // 4) done: 確定。最終の店舗レアリティで絞り込み。同じ名前なら取り直さない
  run('rarityFilter="ウルトラ"');
  send(sources[0], { type: 'done', total: 1, results: [row('ウルトラ')] });
  assert.equal(mercariCalls().length, 1, 'done で同名なら取り直さない');
  assert(body().includes('店舗に無いレアリティ（ノーマル）は表示していません') && !body().includes('確認中'), body());
  assert(!hidden() && get('mercariRef').open === true);

  // 5) done で total=0 → 枠を消す
  get('q').value = 'ゼロ件';
  run('doSearch({validated:true})');
  assert.equal(mercariCalls().length, 2);
  assert(sources[0].closed, '旧ストリームは閉じる');
  mercariCalls()[1].resolve(okResp(BY));
  await tick(); await tick();
  assert(!hidden());
  send(sources[1], { type: 'done', total: 0, results: [] });
  assert(hidden() && body() === '', '全店0件ならメルカリも出さない');

  // 6) 検索中の名前補正: corrected で補正後の名前で取り直し、古い応答は捨てる
  get('q').value = 'はいりゅううらら';
  run('doSearch({confirmed:true})');
  const first = mercariCalls()[2];
  assert.equal(first.url, '/api/mercari?name=' + encodeURIComponent('はいりゅううらら'));
  send(sources[2], { type: 'corrected', original: 'はいりゅううらら', corrected: '灰流うらら' });
  const second = mercariCalls()[3];
  assert.equal(second.url, '/api/mercari?name=' + encodeURIComponent('灰流うらら'), '補正後の名前で取り直す');
  send(sources[2], { type: 'shop_done', shop: 'A', status: 'ok', count: 1, results: [row('ウルトラ')] });
  second.resolve(okResp({ 'ウルトラ': { count: 1, min: 111, median: null, min_is_floor: false, set: null } }));
  await tick(); await tick();
  first.resolve(okResp({ 'ウルトラ': { count: 1, min: 999, median: null, min_is_floor: false, set: null } }));
  await tick(); await tick();
  assert(body().includes('¥111') && !body().includes('¥999'), '古い名前の応答を捨てる');
  send(sources[2], { type: 'done', total: 1, corrected_name: '灰流うらら', results: [row('ウルトラ')] });
  assert.equal(mercariCalls().length, 4, 'corrected と同名なら取り直さない');

  // done.corrected_name だけで名前が変わる場合も取り直す
  get('q').value = 'abc';
  run('doSearch({confirmed:true})');
  const n = mercariCalls().length;
  send(lastSource(), { type: 'done', total: 1, corrected_name: '別名カード', results: [row('ウルトラ')] });
  assert.equal(mercariCalls().length, n + 1);
  assert.equal(mercariCalls()[n].url, '/api/mercari?name=' + encodeURIComponent('別名カード'), 'done.corrected_name で取り直す');

  // 7) 検索失敗（onerror）で枠を消す
  get('q').value = 'error-card';
  run('doSearch({confirmed:true})');
  mercariCalls()[mercariCalls().length - 1].resolve(okResp(BY));
  await tick(); await tick();
  assert(!hidden());
  lastSource().onerror();
  assert(hidden() && body() === '', '検索失敗で消す');
  assert.equal(run('_mercariPending'), false);

  // 7b) タイムアウトで枠を消す（setTimeout で登録された60秒タイマーを実行する）
  get('q').value = 'timeout-card';
  timers.clear();
  run('doSearch({confirmed:true})');
  mercariCalls()[mercariCalls().length - 1].resolve(okResp(BY));
  await tick(); await tick();
  assert(!hidden());
  const fns = [...timers.values()];
  assert.equal(fns.length, 1, 'タイムアウトタイマーが1つ');
  fns[0]();
  assert(hidden() && body() === '', 'タイムアウトで消す');
  assert.equal(run('_mercariPending'), false);

  // 8) 新しい検索・未発売カード表示・戻る/進む（_resetBuyInline 経由）でクリアされる
  get('q').value = 'reset-card';
  run('doSearch({confirmed:true})');
  mercariCalls()[mercariCalls().length - 1].resolve(okResp(BY));
  await tick(); await tick();
  assert(!hidden());
  run('_resetBuyInline()');
  assert(hidden() && body() === '', '_resetBuyInline でメルカリ枠が消える');
  assert.equal(run('_mercariPending'), false);

  // 配線がソースに残っていること（showUnreleasedCard と popstate 経路は _resetBuyInline を呼ぶ）
  assert(/function showUnreleasedCard[\s\S]*?_resetBuyInline\(\)/.test(main), '未発売カード表示で _resetBuyInline');
  console.log('mercari_parallel_check: OK');
})().catch((e) => { console.error('FAIL', e); process.exit(1); });
