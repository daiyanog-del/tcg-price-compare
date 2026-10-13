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
    ok: true, unclassified_count: 2, total_considered: 9, fetched_count: 120, num_found: 1316,
    search_url: 'https://jp.mercari.com/search?keyword=x&status=on_sale&category_id=1152',
    by_rarity: {
      'ノーマル': { count: 1, min: 300, median: null, min_is_floor: true, set: { unit_min: 300, unit_min_qty: 3, count: 2 } },
      'ウルトラ': { count: 5, min: 1000, median: 1500, min_is_floor: false, set: { unit_min: 100, unit_min_qty: 3, count: 4 } },
      'シークレット': { count: 3, min: 640, median: 700, min_is_floor: false },
    },
  });
  await tick(); await tick();
  assert(!classes.has('hidden'));
  const h = box.innerHTML;
  assert(h.indexOf('シークレット') < h.indexOf('ウルトラ') && h.indexOf('ウルトラ') < h.indexOf('ノーマル'), 'レアリティ順');
  // 見出しは1回だけ。各行は 札 | 最安 | 中央 | 件数 の4セル
  
  assert.equal(h.split('>最安<').length - 1, 1);
  assert(h.includes('<span class="mref-min">¥640</span><span class="mref-med">¥700</span><span class="mref-n">3</span>'));
  assert(h.includes('<span class="mref-min">¥300*</span><span class="mref-med">—</span><span class="mref-n">1</span>'), '300円は¥300*・中央なしは—');
  assert(h.includes('* 300円は最低出品価格・関連度上位120件から集計'));
  assert(h.includes('レアリティ判定できず 2件'));
  assert(h.includes('rel="noopener"') && h.includes('target="_blank"') && h.includes('メルカリで見る'));
  assert(!h.includes('sponsored') && !h.includes('（PR）'), 'affiliateなしはPR表記・sponsoredなし');
  assert(h.includes('rb-secret'));
  // 5列目「セット(1枚)」: ¥単価の下に「N枚組」。無ければ「—」。セットの単価には「*」を付けない
  assert(h.includes('>セット(1枚)<'));
  assert(h.includes('<span class="mref-set">¥100<small>3枚組</small></span>'));
  assert(h.includes('<span class="mref-set">¥300<small>3枚組</small></span>') && !h.includes('¥300<small>3枚組</small></span>*'));
  assert(h.includes('<span class="mref-n">3</span><span class="mref-set">—</span>'), 'セット無しは—');
  assert(h.includes('セット＝同じカード2〜3枚組の1枚あたり価格'));
  assert.equal(h.split('class="mref-h"').length - 1, 5, '見出しセルは5つ');

  // セットしか無いレアリティ: 単品列は—、セット列に単価
  api.load('x');
  reply(pending.shift(), { ok: true, unclassified_count: 0, fetched_count: 5, num_found: 5, search_url: 'https://jp.mercari.com/search?keyword=x',
    by_rarity: { 'スーパー': { count: 0, min: null, median: null, min_is_floor: false, set: { unit_min: 250, unit_min_qty: 2, count: 1 } } } });
  await tick(); await tick();
  assert(box.innerHTML.includes('<span class="mref-min">—</span><span class="mref-med">—</span><span class="mref-n">—</span><span class="mref-set">¥250<small>2枚組</small></span>'), box.innerHTML);
  assert(!box.innerHTML.includes('最低出品価格') && !box.innerHTML.includes('¥null'));

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
  assert(box.innerHTML.includes('出品なし') && !box.innerHTML.includes('条件に合う'));
  assert(!box.innerHTML.includes('取得できませんでした'));
  // 取得はできたが全件が除外された場合は「条件に合う出品なし」
  api.load('x');
  reply(pending.shift(), { ok: true, by_rarity: {}, unclassified_count: 0, fetched_count: 120, num_found: 1316, search_url: 'https://jp.mercari.com/search?keyword=x' });
  await tick(); await tick();
  assert(box.innerHTML.includes('条件に合う出品なし'));
  // 全件を取得できているときは「関連度上位」の注記を出さない
  api.load('x');
  reply(pending.shift(), { ok: true, by_rarity: { 'ウルトラ': { count: 1, min: 500, median: null, min_is_floor: false } }, unclassified_count: 0, fetched_count: 5, num_found: 5, search_url: 'https://jp.mercari.com/search?keyword=x' });
  await tick(); await tick();
  assert(!box.innerHTML.includes('関連度上位') && !box.innerHTML.includes('*'));

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
  reply(pending.shift(), { ok: false, error: 'busy' }, 503);
  await tick(); await tick();
  assert(box.innerHTML.includes('混雑中です。少し待って再表示してください') && !box.innerHTML.includes('取得できませんでした'));
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

  // 6b) affiliate:true はPR表記と rel=sponsored。URLのエスケープ(&)は維持
  api.load('x');
  reply(pending.shift(), { ok: true, by_rarity: {}, unclassified_count: 0, affiliate: true, search_url: 'https://jp.mercari.com/search?keyword=x&afid=8033172568' });
  await tick(); await tick();
  assert(box.innerHTML.includes('rel="sponsored noopener"') && box.innerHTML.includes('メルカリで見る（PR）'));
  assert(box.innerHTML.includes('afid=8033172568') && box.innerHTML.includes('target="_blank"'));
  // affiliate が true 以外(文字列"true"等)はPR扱いにしない
  api.load('x');
  reply(pending.shift(), { ok: true, by_rarity: {}, unclassified_count: 0, affiliate: 'true', search_url: 'https://jp.mercari.com/search?keyword=x' });
  await tick(); await tick();
  assert(!box.innerHTML.includes('sponsored') && box.innerHTML.includes('rel="noopener"'));

  // 7) 取得中は枠を出さない(店舗表の描画を遅らせない・前カードの表示を残さない)
  api.load('D');
  assert(classes.has('hidden') && box.innerHTML === '');
  console.log('mercari_ref_check: OK');
})().catch((e) => { console.error('FAIL', e); process.exit(1); });
