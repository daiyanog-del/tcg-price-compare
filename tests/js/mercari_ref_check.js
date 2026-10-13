// メルカリ参考相場の実コード(static/js/mercari-ref.js)を実行し、表示内容・XSS対策・
// 古い応答の破棄を検証する。esc/escAttr/cssClass/safeUrl は index-main.js の実物を使う。
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert/strict');
const root = path.join(__dirname, '../..');
const main = fs.readFileSync(path.join(root, 'static/js/index-main.js'), 'utf8');
function block(start, end) {
  const a = main.indexOf(start), b = main.indexOf(end, a + start.length);
  assert(a >= 0 && b > a, '抽出失敗: ' + start);
  return main.slice(a, b);
}
class FakeTextDiv {
  constructor() { this._t = ''; }
  set textContent(v) { this._t = String(v == null ? '' : v); }
  get innerHTML() { return this._t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
}
const classes = new Set(['hidden']);
const box = {
  innerHTML: '',
  classList: { add: (n) => classes.add(n), remove: (n) => classes.delete(n), contains: (n) => classes.has(n) },
};
const pending = [];
const sandbox = {
  console, URL, Number, Promise, encodeURIComponent, Object,
  location: { origin: 'https://example.test' },
  window: { RARITY_CONFIG: { 'シークレット': { slug: 'secret', order: 40 }, 'ウルトラ': { slug: 'ultra', order: 110 }, 'ノーマル': { slug: 'normal', order: 160 } } },
  document: { getElementById: (id) => (id === 'mercariRef' ? box : null), createElement: () => new FakeTextDiv() },
  fetch(url) { return new Promise((resolve, reject) => pending.push({ url, resolve, reject })); },
};
vm.createContext(sandbox);
vm.runInContext(block('function esc(', 'function rankingLinkClick('), sandbox);
vm.runInContext(fs.readFileSync(path.join(root, 'static/js/mercari-ref.js'), 'utf8'), sandbox);
const api = sandbox.window.MercariRef;
const reply = (req, body, status = 200) => req.resolve({ ok: status >= 200 && status < 300, status, json: async () => body });
const tick = () => new Promise((r) => setImmediate(r));

(async () => {
  // 1) 成功: レアリティ順・最安/中央/件数・判定できず・リンク
  api.load('灰流うらら');
  assert.equal(pending.length, 1);
  assert.equal(pending[0].url, '/api/mercari?name=' + encodeURIComponent('灰流うらら'));
  reply(pending.shift(), {
    ok: true, unclassified_count: 2, total_considered: 9,
    search_url: 'https://jp.mercari.com/search?keyword=x&status=on_sale&category_id=1152',
    by_rarity: {
      'ノーマル': { count: 1, min: 300, median: null, min_is_floor: true },
      'ウルトラ': { count: 5, min: 1000, median: 1500, min_is_floor: false },
      'シークレット': { count: 3, min: 640, median: 700, min_is_floor: false },
    },
  });
  await tick(); await tick();
  assert(!classes.has('hidden'));
  const h = box.innerHTML;
  assert(h.indexOf('シークレット') < h.indexOf('ウルトラ') && h.indexOf('ウルトラ') < h.indexOf('ノーマル'), 'レアリティ順');
  assert(h.includes('最安 ¥640') && h.includes('中央 ¥700') && h.includes('（3件）'));
  assert(h.includes('最安 ¥300（最低出品価格）'));
  assert(!h.includes('中央 ¥null') && h.split('中央').length === 3, '中央値なしの行は中央を出さない');
  assert(h.includes('レアリティ判定できず 2件'));
  assert(h.includes('rel="noopener"') && h.includes('target="_blank"') && h.includes('メルカリで見る'));
  assert(h.includes('rb-secret'));

  // 2) XSS: レアリティ名・URLに脱出ペイロード
  api.load('x');
  reply(pending.shift(), {
    ok: true, unclassified_count: 0,
    search_url: 'javascript:alert(1)',
    by_rarity: { '"><img src=x onerror=alert(1)>': { count: 1, min: 500, median: null, min_is_floor: false } },
  });
  await tick(); await tick();
  assert(!box.innerHTML.includes('<img'), 'レアリティ名がエスケープされていない: ' + box.innerHTML);
  assert(!box.innerHTML.includes('javascript:'), '危険なURLが出力された');
  assert(!box.innerHTML.includes('メルカリで見る'), '安全でないURLのときはリンクを出さない');
  api.load('x');
  reply(pending.shift(), { ok: true, unclassified_count: 0, search_url: 'https://jp.mercari.com/search?keyword="onmouseover="x', by_rarity: {} });
  await tick(); await tick();
  assert(!/href="[^"]*"[^>]*onmouseover=/.test(box.innerHTML) && !box.innerHTML.includes('"onmouseover="'), 'URLが属性から脱出');

  // 3) 出品なし
  api.load('x');
  reply(pending.shift(), { ok: true, by_rarity: {}, unclassified_count: 0, search_url: 'https://jp.mercari.com/search?keyword=x' });
  await tick(); await tick();
  assert(box.innerHTML.includes('出品なし'));
  assert(!box.innerHTML.includes('取得できませんでした'));

  // 4) 取得失敗(ok:false / HTTPエラー / 通信例外)は0件と区別した文言
  api.load('x');
  reply(pending.shift(), { ok: false, error: 'fetch_failed' });
  await tick(); await tick();
  assert(box.innerHTML.includes('メルカリの情報を取得できませんでした') && !box.innerHTML.includes('出品なし'));
  api.load('x');
  reply(pending.shift(), { ok: false, error: 'rate_limited' }, 429);
  await tick(); await tick();
  assert(box.innerHTML.includes('メルカリの情報を取得できませんでした'));
  api.load('x');
  pending.shift().reject(new Error('network'));
  await tick(); await tick();
  assert(box.innerHTML.includes('メルカリの情報を取得できませんでした'));

  // 5) 未知のカード(404)は枠ごと出さない
  api.load('x');
  reply(pending.shift(), { ok: false, error: 'unknown_card' }, 404);
  await tick(); await tick();
  assert(classes.has('hidden') && box.innerHTML === '');

  // 6) 古い応答は捨てる(カード切替・clear後に届いた応答)
  api.load('A');
  const a = pending.shift();
  api.load('B');
  const b = pending.shift();
  reply(b, { ok: true, by_rarity: { 'ウルトラ': { count: 1, min: 111, median: null, min_is_floor: false } }, unclassified_count: 0, search_url: 'https://jp.mercari.com/search?keyword=B' });
  await tick(); await tick();
  reply(a, { ok: true, by_rarity: { 'ウルトラ': { count: 1, min: 999, median: null, min_is_floor: false } }, unclassified_count: 0, search_url: 'https://jp.mercari.com/search?keyword=A' });
  await tick(); await tick();
  assert(box.innerHTML.includes('¥111') && !box.innerHTML.includes('¥999'), '古いカードの応答で上書きされた');
  api.load('C');
  const c = pending.shift();
  api.clear();
  reply(c, { ok: true, by_rarity: { 'ウルトラ': { count: 1, min: 222, median: null, min_is_floor: false } }, unclassified_count: 0, search_url: 'https://jp.mercari.com/search?keyword=C' });
  await tick(); await tick();
  assert(classes.has('hidden') && box.innerHTML === '', 'clear後の応答が描画された');

  // 7) 取得中は枠を出さない(店舗表の描画を遅らせない・前カードの表示を残さない)
  api.load('D');
  assert(classes.has('hidden') && box.innerHTML === '');
  console.log('mercari_ref_check: OK');
})().catch((e) => { console.error('FAIL', e); process.exit(1); });
