// 価格推移グラフの実コード(static/js/price-trend.js)を実行し、集計（日別・レアリティ別の最安値）・
// 表示切替（1本/すべて・(不明)は案内のみ/履歴なし）・XSS・古い応答の破棄・読み取り操作を検証する。
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
      if (id === 'ptCursor' || id === 'ptDot' || id === 'ptTip') return (els[id] = els[id] || { attrs: {}, innerHTML: '', style: {}, setAttribute(k, v) { this.attrs[k] = v; } });
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
  row('2026-09-01', 'A', 'ウルトラ', 300), row('2026-09-01', 'B', 'ウルトラ', 300), row('2026-09-10', 'Z<b>', 'ウルトラ', 250),
  row('2026-09-10', 'A', 'ノーマル', 50),              // 1日分だけ → 線は描かない
  row('2026-09-01', 'A', '(不明)', 10), row('2026-09-10', 'A', '(不明)', 12), // 不明は「すべて」から除外
  row('2026-09-01', 'A', '<img onerror=x>', 100), row('2026-09-10', 'A', '<img onerror=x>', 120),
];

(async () => {
  // 1) 取得 → 「すべて」はグラフを描かず案内1行だけ
  api.setView({ rarity: '' });
  api.load('テスト');
  assert.match(pending[0].url, /^\/api\/price-history\?card=%E3/);
  reply(pending[0], { data: DATA }); await tick();
  assert(!classes.has('hidden'));
  let h = body.innerHTML;
  assert(h.includes('レアリティを選ぶと価格推移を表示します') && !h.includes('<svg') && !h.includes('<path'));

  // 2) 1本選択: 日別は全店舗の最安値（9/1=4800, 9/10=5500）、上昇は up。階段線・グラデ・目盛り・月ラベル
  api.setView({ rarity: 'シークレット' });
  h = body.innerHTML;
  assert(h.includes('価格推移（シークレット・その日の最安値）'));
  assert(!h.includes('・最安値・') && !h.includes('pt-axis'), '旧タイトル・下部の始点終点日付は廃止');
  assert(h.includes('¥4,800 → ¥5,500 ▲14.6%') && h.includes('movers-diff up'));
  assert(h.includes('安 ¥4,800 / 高 ¥5,500'));
  assert(!h.includes('--gold') && !h.includes('pt-legend'));
  // 階段線: 始点 M0 → 水平 H262(=320-58) → 縦 V。折れ線(L)は使わない
  const stepM = /<path d="(M[^"]*)" fill="none" stroke="var\(--accent-l\)"/.exec(h);
  assert(stepM, '線色はアクセント色');
  assert.match(stepM[1], /^M0\.0 [\d.]+H262\.0V[\d.]+$/);
  assert(!stepM[1].includes('L'));
  assert(h.includes('fill="url(#ptGrad)"') && h.includes('stop-opacity=".28"') && h.includes('stop-opacity="0"'));
  assert(h.includes('r="3.5"'), '最新日の点');
  assert(!h.includes('#f00'), 'レアリティ色は使わない');
  // 目盛り（4800〜5500 を12%余白→ 約3本、2.5×10^n 系の刻み）
  const ticks = [...h.matchAll(/class="pt-axr"[^>]*>([^<]*)</g)].map((m) => m[1]);
  assert(ticks.length >= 2 && ticks.length <= 4, '目盛りは約3本: ' + ticks);
  assert(ticks.every((t) => /^¥[\d,]+$/.test(t)));
  assert((h.match(/class="pt-grid"/g) || []).length === ticks.length, '9/1〜9/10 は月跨ぎなし→横グリッドのみ');
  assert(!h.includes('class="pt-ax"'), '月が変わらない期間は月ラベルなし');
  // 下降 + 同額複数店 + 店名エスケープは読み取りで検証
  api.setView({ rarity: 'ウルトラ' });
  assert(body.innerHTML.includes('movers-diff down') && body.innerHTML.includes('▼16.7%'));

  // 3) 履歴1日分のレアリティ / 存在しないレアリティ → 1行だけ
  api.setView({ rarity: 'ノーマル' });
  assert(body.innerHTML.includes('このレアリティの価格推移はまだありません') && !body.innerHTML.includes('<svg'));
  api.setView({ rarity: 'エクストラ' });
  assert(body.innerHTML.includes('まだありません'));

  // 4) (不明) も案内1行・XSS（レアリティ名）は実体参照化
  api.setView({ rarity: '(不明)' });
  assert(body.innerHTML.includes('レアリティを選ぶと価格推移を表示します') && !body.innerHTML.includes('<svg'));
  api.setView({ rarity: '<img onerror=x>' });
  h = body.innerHTML;
  assert(h.includes('&lt;img onerror=x&gt;') && !h.includes('<img onerror') && h.includes('<svg'));

  // 5) 読み取り操作: 縦線・白丸・吹き出し（日付・価格・店名）。見出し右の騰落表示は変えない。GAは1回だけ
  api.setView({ rarity: 'シークレット' });
  const summaryBefore = body.innerHTML;
  const plot = plotHolder.p;
  plot.h.pointermove({ clientX: 95 });
  assert.equal(els.ptCursor.attrs.visibility, 'visible');
  assert.equal(els.ptCursor.attrs.x1, '262.0');
  assert.equal(els.ptDot.attrs.visibility, 'visible');
  assert.equal(els.ptDot.attrs.cx, '262.0');
  assert.equal(els.ptTip.style.display, 'block');
  assert(els.ptTip.innerHTML.includes('9/10') && els.ptTip.innerHTML.includes('<b>¥5,500</b>') && els.ptTip.innerHTML.includes('>A<'));
  assert.equal(body.innerHTML, summaryBefore, '見出し右の騰落表示はなぞり中も変えない');
  plot.h.pointermove({ clientX: 2 });
  assert(els.ptTip.innerHTML.includes('9/1 ') && els.ptTip.innerHTML.includes('¥4,800') && els.ptTip.innerHTML.includes('>B<'));
  assert.equal(els.ptCursor.attrs.x1, '0.0');
  assert.equal(els.ptTip.style.left, '50px', '左端ではクランプ(吹き出し幅100→半分50)');
  plot.h.pointermove({ clientX: 100 });
  assert.equal(els.ptTip.style.left, '50px', '右端でもクランプ');
  plot.h.pointerleave();
  assert.equal(els.ptCursor.attrs.visibility, 'hidden');
  assert.equal(els.ptDot.attrs.visibility, 'hidden');
  assert.equal(els.ptTip.style.display, 'none');
  plot.h.pointermove({ clientX: 5 });
  plot.h.pointercancel();
  assert.equal(els.ptTip.style.display, 'none');
  // 同額で複数店 → 「ほか」。店名はエスケープ
  api.setView({ rarity: 'ウルトラ' });
  plotHolder.p.h.pointermove({ clientX: 2 });
  assert(els.ptTip.innerHTML.includes('>A ほか<'), els.ptTip.innerHTML);
  plotHolder.p.h.pointermove({ clientX: 95 });
  assert(els.ptTip.innerHTML.includes('Z&lt;b&gt;') && !els.ptTip.innerHTML.includes('Z<b>'));
  assert.deepEqual(Array.from(gaCalls), ['price_trend_touch']);

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
  // 7) 月の区切りと年またぎ: 期間が月をまたぐと「N月」、年をまたぐと1月だけ「2027年1月」
  api.load('M');
  reply(pending[pending.length - 1], { data: [
    row('2026-12-20', 'A', 'ウルトラ', 100), row('2026-12-28', 'A', 'ウルトラ', 120),
    row('2027-01-05', 'A', 'ウルトラ', 110), row('2027-02-10', 'A', 'ウルトラ', 130),
  ] }); await tick();
  api.setView({ rarity: 'ウルトラ' });
  h = body.innerHTML;
  const labels = [...h.matchAll(/class="pt-ax"[^>]*>([^<]*)</g)].map((m) => m[1]);
  assert.deepEqual(Array.from(labels), ['2027年1月', '2月']);
  api.load('N');
  reply(pending[pending.length - 1], { data: [row('2026-08-20', 'A', 'ウルトラ', 100), row('2026-10-03', 'A', 'ウルトラ', 130)] }); await tick();
  api.setView({ rarity: 'ウルトラ' });
  assert.deepEqual(Array.from(body.innerHTML.matchAll(/class="pt-ax"[^>]*>([^<]*)</g), (m) => m[1]), ['9月', '10月'], '同一年は年を付けない');

  // 8) 目盛りの刻みは 1/2/2.5/5×10^n
  const nt = api._niceTicks;
  assert.deepEqual(Array.from(nt(0, 100, 3)), [0, 50, 100]);
  assert.deepEqual(Array.from(nt(180, 260, 3)), [200, 250]);
  assert.deepEqual(Array.from(nt(4500, 5800, 3)), [4500, 5000, 5500]);
  console.log('price_trend_check OK');
})().catch((e) => { console.error(e); process.exit(1); });
