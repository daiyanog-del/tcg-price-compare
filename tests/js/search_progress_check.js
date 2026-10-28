// 検索中のバー（search-progress.js・2026-10-28）を実物で検証する。
// - 探している間は sp-running（空きの側に光が流れる）。全店終わったら外す
// - 終盤（残り1店・または4分の1以下）に SLOW_MS 経っても終わらなければ「応答の遅い店を待っています」
// - その前に次の店が終われば文言は戻る。混雑の文言が先に立つ
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert/strict');
const root = path.join(__dirname, '../..');

function el() {
  const cls = new Set();
  const e = { className: '', textContent: '', style: {}, children: [],
    classList: { contains: (c) => cls.has(c), toggle: (c, on) => (on ? cls.add(c) : cls.delete(c)) },
    appendChild(c) { this.children.push(c); } };
  Object.defineProperty(e, 'className', { set(v) { cls.clear(); v.split(' ').filter(Boolean).forEach((c) => cls.add(c)); } });
  Object.defineProperty(e, 'innerHTML', { set() { e._label = el(); e._fill = el(); } });
  e.querySelector = (s) => (s === '.sp-label' ? e._label : e._fill);
  return e;
}
let timers = [], now = 0;
const box = {
  document: { createElement: el },
  setTimeout: (fn, ms) => { const t = { fn, at: now + ms }; timers.push(t); return t; },
  clearTimeout: (t) => { timers = timers.filter((x) => x !== t); },
};
function advance(ms) {
  now += ms;
  for (const t of timers.filter((x) => x.at <= now)) { timers = timers.filter((x) => x !== t); t.fn(); }
}
vm.createContext(box);
vm.runInContext(fs.readFileSync(path.join(root, 'static/js/search-progress.js'), 'utf8') + ';this.SP=SearchProgress;', box);
const SP = box.SP;
const LABEL = '各店の価格を確認中';

// 終盤の判定
assert.equal(SP.isTail(1, 6), true);
assert.equal(SP.isTail(2, 6), false, '6店で残り2店はまだ終盤ではない');
assert.equal(SP.isTail(3, 12), true, '12店なら残り3店から');
assert.equal(SP.isTail(0, 6), false);

// 6店の検索: 5店はすぐ・最後の1店が遅い
const host = el();
const p = SP.create(host, LABEL);
const label = () => p.el.querySelector('.sp-label').textContent;
const running = () => p.el.classList.contains('sp-running');
assert.equal(label(), LABEL);
assert(running(), '始めた直後から光を流す');
const shops = ['A', 'B', 'C', 'D', 'E', 'F'];
p.setShops(shops);
['A', 'B', 'C', 'D'].forEach((s) => p.finish(s));
advance(SP.SLOW_MS + 100);
assert.equal(label(), LABEL, '残り2店のうちは文言を変えない');
p.finish('E');
advance(SP.SLOW_MS - 100);
assert.equal(label(), LABEL, 'SLOW_MS に届くまでは変えない');
advance(200);
assert.equal(label(), SP.SLOW_LABEL, '最後の1店が SLOW_MS 経っても終わらない');
assert(running());
p.setBusy(true);
assert.equal(label(), SP.BUSY_LABEL, '混雑の文言が先');
p.setBusy(false);
assert.equal(label(), SP.SLOW_LABEL);
p.finish('F');
assert.equal(label(), LABEL, '全店終わったら戻す');
assert(!running(), '全店終わったら光を止める');
assert.equal(p.el.querySelector('.sp-fill').style.width, '100%');
assert.equal(timers.length, 0, '残りの時計が無い');

// 終盤に入ってすぐ最後の店が終わったら、遅いとは言わない
const q = SP.create(el(), LABEL);
q.setShops(['A', 'B']);
q.finish('A');
advance(500);
q.finish('B');
advance(SP.SLOW_MS * 2);
assert.equal(q.el.querySelector('.sp-label').textContent, LABEL);

// 買取（店の一覧が来ない）: start で数え、終わった店が並んでも最後の1店で同じように動く
const r = SP.create(el(), '各店の買取価格を確認中');
['A', 'B', 'C'].forEach((s) => r.start(s));
r.finish('A'); r.finish('B');
advance(SP.SLOW_MS + 1);
assert.equal(r.el.querySelector('.sp-label').textContent, SP.SLOW_LABEL);

console.log('search_progress_check: ok');
