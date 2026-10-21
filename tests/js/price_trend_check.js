// 価格推移グラフの実コード(static/js/price-trend.js)を実行し、集計（日別・レアリティ別の最安値）・
// 表示切替（1本/すべて・(不明)は案内のみ/履歴なし）・XSS・古い応答の破棄・読み取り操作を検証する。
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert/strict');
const root = path.join(__dirname, '../..');
const classes = new Set(['hidden']);
const box = { classList: { add: (n) => classes.add(n), remove: (n) => classes.delete(n), contains: (n) => classes.has(n) } };
let bodyWidth = 0;   // body の実幅（0=測れない→下限240で描く）
const body = { innerHTML: '', getBoundingClientRect: () => ({ width: bodyWidth }) };
const roInstances = [];
const docHandlers = {};
const els = {};
const pending = [];
const gaCalls = [];
function mkPlot() {
  const h = {};
  return { addEventListener: (t, f) => { h[t] = f; }, contains: (t) => t === 'inside', getBoundingClientRect: () => ({ left: 0, width: 100 }), h };
}
const plotHolder = {};
const sandbox = {
  console, Number, Promise, encodeURIComponent, Object, Math, Date, isFinite,
  _ga: (n) => gaCalls.push(n),
  window: { RARITY_CONFIG: { 'シークレット': { color: '#f00', order: 40 }, 'ウルトラ': { color: '#0af', order: 110 }, 'ノーマル': { order: 160 } } },
  ResizeObserver: class { constructor(cb) { this.cb = cb; this.observed = 0; roInstances.push(this); } observe() { this.observed++; } },
  document: {
    addEventListener: (t, f) => { docHandlers[t] = f; },
    removeEventListener: (t, f) => { if (docHandlers[t] === f) delete docHandlers[t]; },
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
  assert(h.includes('相場の動き（シークレット・日ごとの最安）'));
  assert(!h.includes('・最安値・') && !h.includes('pt-axis'), '旧タイトル・下部の始点終点日付は廃止');
  assert(h.includes('¥4,800 → ¥5,500 ▲14.6%') && h.includes('movers-diff up'));
  assert(h.includes('安 ¥4,800 / 高 ¥5,500'));
  assert(!h.includes('--gold') && !h.includes('pt-legend'));
  // 階段線: 始点 M0 → 水平 H182(=240-58) → 縦 V。折れ線(L)は使わない
  const stepM = /<path class="pt-line" d="(M[^"]*)"/.exec(h);
  assert(stepM, '線はクラス指定(色はCSSでアクセント色)');
  assert(!/(stroke|fill|stop-color)="var\(/.test(h), 'SVG属性にvar()で色を書かない');
  assert.match(stepM[1], /^M0\.0 [\d.]+H182\.0V[\d.]+$/);
  assert(!stepM[1].includes('L'));
  assert(h.includes('class="pt-area"') && h.includes('stop-opacity=".28"') && h.includes('stop-opacity="0"'));
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
  assert.equal(els.ptCursor.attrs.x1, '182.0');
  assert.equal(els.ptDot.attrs.visibility, 'visible');
  assert.equal(els.ptDot.attrs.cx, '182.0');
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

  // 5b) 吹き出しの縦位置: 最高値の点(y≈17)を隠さず点の下へ。低い点では上へ
  api.setView({ rarity: 'シークレット' });
  plot.h.pointermove({ clientX: 95 });
  assert.equal(els.ptTip.style.top, '27px', '最高値＝最新日の点は上に入らないので下に出す');
  plot.h.pointermove({ clientX: 2 });
  assert.equal(els.ptTip.style.top, '39px', '低い点では点の上に出す');
  plot.h.pointerleave();

  // 5c) タッチ: pointerleave では消さず、グラフ外タップか pointercancel で消す。マウスは離れたら消す
  plot.h.pointermove({ clientX: 95, pointerType: 'touch' });
  plot.h.pointerleave({ pointerType: 'touch' });
  assert.equal(els.ptTip.style.display, 'block', 'touch は離れても残る');
  docHandlers.pointerdown({ target: 'inside' });
  assert.equal(els.ptTip.style.display, 'block', 'グラフ内タップでは消さない');
  docHandlers.pointerdown({ target: 'outside' });
  assert.equal(els.ptTip.style.display, 'none', 'グラフ外タップで消える');
  assert.equal(els.ptDot.attrs.visibility, 'hidden');
  plot.h.pointermove({ clientX: 5, pointerType: 'touch' });
  plot.h.pointercancel({ pointerType: 'touch' });
  assert.equal(els.ptTip.style.display, 'none', 'pointercancel(スクロール開始)で消える');
  plot.h.pointermove({ clientX: 5, pointerType: 'mouse' });
  plot.h.pointerleave({ pointerType: 'mouse' });
  assert.equal(els.ptTip.style.display, 'none', 'マウスは離れたら消える');

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
  assert.deepEqual(Array.from(nt(30, 31, 3)), [30, 31], '1円差でも刻みは1以上（¥30/¥31/¥31の重複表示を出さない）');
  api.load('Y');
  reply(pending[pending.length - 1], { data: [row('2026-09-01', 'A', 'ウルトラ', 30), row('2026-09-02', 'A', 'ウルトラ', 31)] }); await tick();
  api.setView({ rarity: 'ウルトラ' });
  const yt = Array.from(body.innerHTML.matchAll(/class="pt-axr"[^>]*>([^<]*)</g), (m) => m[1]);
  assert.equal(new Set(yt).size, yt.length, '目盛りに重複なし: ' + yt);

  // 9) 右余白は目盛り文字の最長幅に合わせる（¥1,000,000 以上でもはみ出さない）
  api.load('P');
  reply(pending[pending.length - 1], { data: [row('2026-09-01', 'A', 'ウルトラ', 1000000), row('2026-09-10', 'A', 'ウルトラ', 1200000)] }); await tick();
  api.setView({ rarity: 'ウルトラ' });
  h = body.innerHTML;
  const lab = Array.from(h.matchAll(/class="pt-axr" x="([\d.]+)"[^>]*>([^<]*)</g), (m) => ({ x: +m[1], t: m[2] }));
  assert(lab.length >= 2);
  lab.forEach((l) => assert(l.x + l.t.length * 6.2 <= 240, '文字が右端(240)内に収まる: ' + l.t + ' x=' + l.x));
  assert(+/H([\d.]+)V/.exec(h)[1] < 182, '長い目盛りのぶんグラフ本体の右端が内側に寄る');

  // 10) ResizeObserver: 生成は1回だけ・同じ幅の通知では描き直さない・幅が変われば描き直す・実幅が座標に反映される
  assert.equal(roInstances.length, 1, 'render を何度呼んでも observe 用の ResizeObserver は1つ');
  assert.equal(roInstances[0].observed, 1);
  bodyWidth = 500;
  api.setView({ rarity: 'ウルトラ' });
  assert(body.innerHTML.includes('viewBox="0 0 500 96"'), '実幅500で描く');
  assert(/H426\.0V/.test(body.innerHTML), '右端 = 実幅 - 右余白(500-74。7桁価格の目盛り幅)');
  assert.equal(roInstances.length, 1);
  body.innerHTML += '<!--marker-->';
  roInstances[0].cb();
  assert(body.innerHTML.includes('<!--marker-->'), '同じ幅の通知では描き直さない');
  bodyWidth = 700;
  roInstances[0].cb();
  assert(!body.innerHTML.includes('<!--marker-->') && body.innerHTML.includes('viewBox="0 0 700 96"'), '幅が変われば描き直す');
  bodyWidth = 0;
  body.innerHTML += '<!--marker2-->';
  roInstances[0].cb();
  assert(body.innerHTML.includes('<!--marker2-->'), '幅0(非表示)の通知では描き直さない');
  assert.equal(roInstances.length, 1);
  console.log('price_trend_check OK');
})().catch((e) => { console.error(e); process.exit(1); });
