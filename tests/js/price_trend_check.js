// 価格推移グラフの実コード(static/js/price-trend.js)を実行し、集計（日別・レアリティ別の最安値）・
// 表示切替（1本/すべて/店舗に無いレアリティの除外/履歴なし）・XSS・古い応答の破棄・読み取り操作を検証する。
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert/strict');
const root = path.join(__dirname, '../..');
const classes = new Set(['hidden']);
const box = { classList: { add: (n) => classes.add(n), remove: (n) => classes.delete(n), contains: (n) => classes.has(n) } };
const body = { innerHTML: '' };
const els = {};
const pending = [];
const gaCalls = [];
function mkPlot() {
  const h = {};
  return { addEventListener: (t, f) => { h[t] = f; }, getBoundingClientRect: () => ({ left: 0, width: 100 }), h };
}
const plotHolder = {};
const sandbox = {
  console, Number, Promise, encodeURIComponent, Object, Math, Date, isFinite,
  _ga: (n) => gaCalls.push(n),
  window: { RARITY_CONFIG: { 'シークレット': { color: '#f00', order: 40 }, 'ウルトラ': { color: '#0af', order: 110 }, 'ノーマル': { order: 160 } } },
  document: {
    getElementById: (id) => {
      if (id === 'priceTrend') return box;
      if (id === 'priceTrendBody') return body;
      if (id === 'ptPlot') return (plotHolder.p = plotHolder.p || mkPlot());
      if (id === 'ptCursor' || id === 'ptVal') return (els[id] = els[id] || { attrs: {}, innerHTML: '', setAttribute(k, v) { this.attrs[k] = v; } });
      return null;
    },
  },
  fetch(url) { return new Promise((resolve, reject) => pending.push({ url, resolve, reject })); },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(root, 'static/js/price-trend.js'), 'utf8'), sandbox);
const api = sandbox.window.PriceTrend;
const reply = (req, b, status = 200) => req.resolve({ ok: status >= 200 && status < 300, status, json: async () => b });
const tick = () => new Promise((r) => setImmediate(r));
const row = (date, shop, rarity, price) => ({ date, shop, rarity, price });
const DATA = [
  row('2026-09-01', 'A', 'シークレット', 5000), row('2026-09-01', 'B', 'シークレット', 4800),
  row('2026-09-10', 'A', 'シークレット', 5500), row('2026-09-10', 'B', 'シークレット', 6000),
  row('2026-09-01', 'A', 'ウルトラ', 300), row('2026-09-10', 'A', 'ウルトラ', 250),
  row('2026-09-10', 'A', 'ノーマル', 50),              // 1日分だけ → 線は描かない
  row('2026-09-01', 'A', '(不明)', 10), row('2026-09-10', 'A', '(不明)', 12), // 不明は「すべて」から除外
  row('2026-09-01', 'A', '<img onerror=x>', 100), row('2026-09-10', 'A', '<img onerror=x>', 120),
];

(async () => {
  // 1) 取得 → すべて表示（店舗結果に存在するレアリティだけ）
  api.setView({ rarity: '', stores: ['シークレット', 'ウルトラ'] });
  api.load('テスト');
  assert.match(pending[0].url, /^\/api\/price-history\?card=%E3/);
  reply(pending[0], { data: DATA }); await tick();
  assert(!classes.has('hidden'));
  let h = body.innerHTML;
  assert.equal((h.match(/<path /g) || []).length, 2, 'すべて: 店舗にある2レアリティだけ線を描く');
  assert(h.includes('レアリティ別の最安値・10日'));
  assert(h.includes('stroke="#f00"') && h.includes('stroke="#0af"'));
  assert(!h.includes('onerror') && !h.includes('movers-diff'), '店舗に無い/不明は出さず、すべてでは騰落率なし');
  assert(h.includes('vector-effect="non-scaling-stroke"') && h.includes('preserveAspectRatio="none"'));
  assert(h.includes('¥5,500') && h.includes('¥250'), '最大・最小金額を添える');

  // 2) 1本選択: 日別は全店舗の最安値（9/1=4800, 9/10=5500）、上昇は up
  api.setView({ rarity: 'シークレット', stores: ['シークレット'] });
  h = body.innerHTML;
  assert.equal((h.match(/<path /g) || []).length, 1);
  assert(h.includes('価格推移（シークレット・最安値・10日）'));
  assert(h.includes('¥4,800 → ¥5,500 ▲14.6%') && h.includes('movers-diff up'));
  assert(h.includes('安 ¥4,800 / 高 ¥5,500'));
  assert(!h.includes('--gold'));
  // 下降
  api.setView({ rarity: 'ウルトラ', stores: [] });
  assert(body.innerHTML.includes('movers-diff down') && body.innerHTML.includes('▼16.7%'));

  // 3) 履歴1日分のレアリティ / 存在しないレアリティ → 1行だけ
  api.setView({ rarity: 'ノーマル', stores: [] });
  assert(body.innerHTML.includes('このレアリティの価格推移はまだありません') && !body.innerHTML.includes('<svg'));
  api.setView({ rarity: 'エクストラ', stores: [] });
  assert(body.innerHTML.includes('まだありません'));

  // 4) (不明) は「すべて」と同じ・店舗が空なら絞らない・XSSは実体参照化
  api.setView({ rarity: '(不明)', stores: [] });
  h = body.innerHTML;
  assert(h.includes('&lt;img onerror=x&gt;') && !h.includes('<img onerror'));
  assert.equal((h.match(/<path /g) || []).length, 3);

  // 5) 読み取り操作: 縦線と値の差し替え、離れたら戻す、GAは1回だけ
  api.setView({ rarity: 'シークレット', stores: [] });
  const plot = plotHolder.p;
  plot.h.pointermove({ clientX: 95 });
  assert.equal(els.ptCursor.attrs.visibility, 'visible');
  assert.equal(els.ptCursor.attrs.x1, '1000.0');
  assert(els.ptVal.innerHTML.includes('9/10') && els.ptVal.innerHTML.includes('¥5,500'));
  plot.h.pointermove({ clientX: 2 });
  assert(els.ptVal.innerHTML.includes('9/1 ') && els.ptVal.innerHTML.includes('¥4,800'));
  plot.h.pointerleave();
  assert.equal(els.ptCursor.attrs.visibility, 'hidden');
  assert(els.ptVal.innerHTML.includes('▲14.6%'), '離れたら既定表示に戻る');
  assert.deepEqual(gaCalls, ['price_trend_touch']);

  // 6) 古い応答の破棄 / 空・失敗は枠ごと非表示 / clear
  api.load('A'); api.load('B');
  reply(pending[1], { data: [row('2026-01-01', 'A', 'ウルトラ', 1)] }); await tick();
  reply(pending[2], { data: DATA }); await tick();
  assert(body.innerHTML.includes('<svg'), '古い応答(A)は捨てられ、Bの描画が残る');
  api.load('C'); reply(pending[3], { data: [] }); await tick();
  assert(classes.has('hidden') && body.innerHTML === '');
  api.load('D'); reply(pending[4], { error: 'x' }, 500); await tick();
  assert(classes.has('hidden'));
  api.load('E'); pending[5].reject(new Error('net')); await tick();
  assert(classes.has('hidden'));
  api.load('F'); api.clear(); reply(pending[6], { data: DATA }); await tick();
  assert(classes.has('hidden') && body.innerHTML === '', 'clear 後に届いた応答は捨てる');
  console.log('price_trend_check OK');
})().catch((e) => { console.error(e); process.exit(1); });
