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
    querySelector() { return this._q || (this._q = element('q')); }, appendChild() {}, insertBefore() {}, listeners: {}, addEventListener(k, fn) { this.listeners[k] = fn; }, contains() { return true; }, remove() {},
    parentNode: { insertBefore() {} }, setAttribute() {}, removeAttribute() {} };
}
const els = new Map();
const get = (id) => { if (!els.has(id)) els.set(id, element(id)); return els.get(id); };
get('mercariRef').classList.add('hidden');
const fetches = [], sources = [], timers = new Map(), gaCalls = [];
let timerSeq = 0;
class FakeES { constructor(url) { this.url = url; this.closed = false; sources.push(this); } close() { this.closed = true; } }
const sandbox = {
  console, URL, Number, Promise, Object, Set, JSON, URLSearchParams, encodeURIComponent, location: { origin: 'https://example.test' },
  window: { RARITY_CONFIG: { 'ウルトラ': { order: 110 }, 'ノーマル': { order: 160 }, 'シークレット': { order: 40 } } },
  document: {
    getElementById: get,
    createElement: () => ({ set textContent(v) { this._t = String(v == null ? '' : v); }, get innerHTML() { return this._t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }, className: '', remove() {} }),
    querySelector: () => null,
    querySelectorAll: () => [],
  },
  // 店舗検索の進み具合のバー（2026-10-28）。この検証の対象外なので何もしない（検証は shop_busy_check.js）
  SearchProgress: { create: () => ({ setShops() {}, start() {}, finish() {}, setBusy() {} }) },
  EventSource: FakeES,
  fetch(url) { return new Promise((resolve, reject) => fetches.push({ url, resolve, reject })); },
  setTimeout(fn) { const id = ++timerSeq; timers.set(id, fn); return id; },
  clearTimeout(id) { timers.delete(id); },
  _ga(name, p) { gaCalls.push([name, p]); }, _setSearchLayout() {}, _setPriceSectionsVisible() {}, _updateRarityBanner() {}, renderTable() {}, closeSuggest() {},
  renderAll() {}, SearchStatus: { show() {} }, TABLE_PAGE_SIZE: 20,
  _prefetchCardImage() {}, // カード画像の先読み（2026-10-22 LCP）。この検証の対象外なので何もしない
  _prefetchCardInfo() {}, // カード情報の先読み（2026-10-31）。同じく対象外
  _hideSearchPreview() {}, // 検索中の先出しの欄（2026-10-22 LCP）。この検証の対象外
};
vm.createContext(sandbox);
vm.runInContext('let D=[],rarityFilter="",tableShowCount=0,_currentCardName="";let _buyInlineGen=0,_buyInlineES=null,_buyInlineTimeoutId=null,_buyInlineD=null,_buyInlineFetched=false,_buyInlineFetchInFlight=false,_buyInlineFailedShops=[];', sandbox);
vm.runInContext(block('function esc(', 'function rankingLinkClick('), sandbox);
vm.runInContext(block('const AFF_PR_BADGE=', 'function affPixel('), sandbox);
vm.runInContext(fs.readFileSync(path.join(root, 'static/js/mercari-ref.js'), 'utf8'), sandbox);
sandbox.MercariRef = sandbox.window.MercariRef; // ブラウザでは window のプロパティが素の識別子で参照できる
vm.runInContext([
  block('let _searchES=null;', '// ── Autocomplete'),
  block('function _resetBuyInline(', '// #buyInline を開いた経路を GA で区別するためのフラグ'),
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
  assert.equal(mercariCalls().length, 0, 'EventSource作成直後には呼ばない（最初のイベント待ち）');
  send(sources[0], { type: 'progress', shop: 'A' });
  assert.equal(mercariCalls().length, 1, '最初のイベントで取得開始');
  assert.equal(mercariCalls()[0].url, '/api/mercari?name=' + encodeURIComponent('灰流うらら'));
  send(sources[0], { type: 'progress', shop: 'B' });
  assert.equal(mercariCalls().length, 1, '2つ目以降のイベントでは呼ばない');
  assert(hidden(), '応答前は枠を出さない');

  // 2) 店舗検索の途中（done 前）に応答が届く: 枠が出て、D 空なら確認中の注記のみ
  mercariCalls()[0].resolve(okResp(BY));
  await tick(); await tick();
  assert(!hidden(), '店舗検索中でも枠が出る');
  assert.equal(get('mercariRef').open, false, '検索中は閉じた見出しだけで出る');
  assert(!body().includes('class="scard fin"') && body().includes('メルカリで見る') && !body().includes('確認中'), 'D空: タイルなし・リンクのみ: ' + body());

  // 3) shop_done では描き直さない（結果は done で一度だけ描く。2026-10-16 表の跳ね対策）・再取得もしない
  send(sources[0], { type: 'shop_done', shop: 'A', status: 'ok', count: 1, results: [row('ウルトラ')] });
  assert.equal(mercariCalls().length, 1, '再取得しない');
  assert(!body().includes('class="scard fin"'), 'done 前はタイルを出さない: ' + body());
  assert.equal(get('mercariRef').open, false, '検索中は閉じたまま');
  send(sources[0], { type: 'shop_done', shop: 'B', status: 'ok', count: 0, results: [] });
  assert(!body().includes('>ノーマル<'));

  // 4) done: 確定。最終の店舗レアリティで絞り込み。同じ名前なら取り直さない
  run('rarityFilter="ウルトラ"');
  send(sources[0], { type: 'done', total: 1, results: [row('ウルトラ')] });
  assert.equal(mercariCalls().length, 1, 'done で同名なら取り直さない');
  assert(body().split('class="scard fin"').length - 1 === 1 && !body().includes('>ノーマル<') && !body().includes('表示していません'), body());
  assert(!hidden() && get('mercariRef').open === true, 'done で確定した時点で開く');

  // 5) done で total=0 → 枠を消す
  get('q').value = 'ゼロ件';
  run('doSearch({validated:true})');
  send(lastSource(), { type: 'progress', shop: 'A' });
  assert.equal(mercariCalls().length, 2);
  assert(sources[0].closed, '旧ストリームは閉じる');
  mercariCalls()[1].resolve(okResp(BY));
  await tick(); await tick();
  assert(!hidden());
  send(sources[1], { type: 'done', total: 0, results: [] });
  assert(hidden() && body() === '', '全店0件ならメルカリも出さない');

  // 6) 検索中の名前補正: 取り直さない（サーバーが /api/mercari 内で補正する）。世代管理は維持される
  get('q').value = 'はいりゅううらら';
  run('doSearch({confirmed:true})');
  send(lastSource(), { type: 'corrected', original: 'はいりゅううらら', corrected: '灰流うらら' });
  assert.equal(mercariCalls().length, 3);
  const only = mercariCalls()[2];
  assert.equal(only.url, '/api/mercari?name=' + encodeURIComponent('はいりゅううらら'), '入力名のまま1回だけ呼ぶ');
  send(lastSource(), { type: 'shop_done', shop: 'A', status: 'ok', count: 1, results: [row('ウルトラ')] });
  only.resolve(okResp({ 'ウルトラ': { count: 1, min: 111, median: null, min_is_floor: false, set: null } }));
  await tick(); await tick();
  send(lastSource(), { type: 'done', total: 1, corrected_name: '灰流うらら', results: [row('ウルトラ')] });
  assert(body().includes('ウルトラ'), body());
  assert.equal(mercariCalls().length, 3, 'done.corrected_name でも取り直さない');
  // 次の検索を始めると、前の検索の応答は世代管理で捨てられる
  get('q').value = 'next';
  run('doSearch({confirmed:true})');
  send(lastSource(), { type: 'progress', shop: 'A' });
  const nxt = mercariCalls()[3];
  assert(hidden());

  // 7) 検索失敗（onerror）で枠を消す
  // 7a) イベントを1つも受けずに失敗（/api/search が429等）した検索ではメルカリを呼ばない
  const before = mercariCalls().length;
  get('q').value = 'fail-first';
  run('doSearch({confirmed:true})');
  lastSource().onerror();
  assert.equal(mercariCalls().length, before, 'onerror のみの検索ではメルカリを呼ばない');
  assert(hidden());
  get('q').value = 'error-card';
  run('doSearch({confirmed:true})');
  send(lastSource(), { type: 'progress', shop: 'A' });
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
  send(lastSource(), { type: 'progress', shop: 'A' });
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
  send(lastSource(), { type: 'progress', shop: 'A' });
  mercariCalls()[mercariCalls().length - 1].resolve(okResp(BY));
  await tick(); await tick();
  assert(!hidden());
  run('_resetBuyInline()');
  assert(hidden() && body() === '', '_resetBuyInline でメルカリ枠が消える');
  assert.equal(run('_mercariPending'), false);

  // 9) validate の確認ダイアログでキャンセルしたら枠を消し pending を解除する（もしかして／見つかりません の両方）
  sandbox.confirm = () => false;
  for (const v of [{ valid: false, suggestion: '候補カード' }, { valid: false }]) {
    get('q').value = 'zz-cancel';
    run('doSearch({confirmed:true})');
    send(lastSource(), { type: 'progress', shop: 'A' });
    mercariCalls()[mercariCalls().length - 1].resolve(okResp(BY));
    await tick(); await tick();
    assert(!hidden());
    get('q').value = 'zz-cancel2';
    run('doSearch()');
    const vreq = fetches[fetches.length - 1];
    assert(vreq.url.startsWith('/api/validate'));
    vreq.resolve({ json: async () => v });
    await tick(); await tick();
    assert(hidden() && body() === '', 'キャンセルで消す');
    assert.equal(run('_mercariPending'), false);
  }

  // 10) メルカリ欄の価格リンクのクリック計測（イベント委譲・affiliate 付き）。パネル本文以外のクリックは数えない
  vm.runInContext(block('// メルカリ欄の価格リンク（タイル内の', 'function _fetchBuyInline('), sandbox);
  const onClick = get('mercariRefBody').listeners.click;
  assert(onClick, '本文にクリック委譲が登録されている');
  const linkEl = (rel) => ({ getAttribute: (k) => (k === 'rel' ? rel : null) });
  onClick.call(get('mercariRefBody'), { target: { closest: () => linkEl('sponsored noopener') } });
  onClick.call(get('mercariRefBody'), { target: { closest: () => linkEl('noopener') } });
  onClick.call(get('mercariRefBody'), { target: { closest: () => null } });
  const clicks = gaCalls.filter((c) => c[0] === 'mercari_link_click');
  assert.equal(clicks.length, 2);
  assert.deepEqual(clicks.map((c) => c[1].affiliate), [true, false]);

  // 配線がソースに残っていること（showUnreleasedCard と popstate 経路は _resetBuyInline を呼ぶ）
  assert(/function showUnreleasedCard[\s\S]*?_resetBuyInline\(\)/.test(main), '未発売カード表示で _resetBuyInline');
  console.log('mercari_parallel_check: OK');
})().catch((e) => { console.error('FAIL', e); process.exit(1); });
