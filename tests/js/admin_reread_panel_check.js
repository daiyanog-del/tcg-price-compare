// 効果テキストの読み直しパネル: 食い違い箇所だけ <mark>・文字列は textContent のまま・一致/旧データは非表示
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../static/admin/admin.js'), 'utf8');
const start = source.indexOf('function _renderRereadPanel(');
const end = source.indexOf('/**', start);
assert.ok(start > 0 && end > start);

class El {
  constructor(tag) { this.tag = tag; this.children = []; this.hidden = false; this.textContent = ''; this.className = ''; }
  appendChild(c) { this.children.push(c); return c; }
  replaceChildren() { this.children = []; }
  querySelector(sel) { return this.map[sel]; }
}
const document = {
  createElement: (t) => new El(t),
  createTextNode: (t) => ({text: t}),
};
const context = vm.createContext({document});
vm.runInContext(source.slice(start, end) + '\nglobalThis.render = _renderRereadPanel;', context);

function form() {
  const panel = new El('div'); panel.hidden = true;
  const note = new El('p'); const rows = new El('dl');
  panel.map = {'.reread-panel__note': note, '.reread-panel__rows': rows};
  const f = new El('form'); f.map = {'.reread-panel': panel};
  return {f, panel, note, rows};
}

let t = form();
context.render(t.f, {});
assert.equal(t.panel.hidden, true);
context.render(t.f, {effect_reread: null});
assert.equal(t.panel.hidden, true);

t = form();
context.render(t.f, {effect_reread: {status: 'error', reason: '<b>x</b>'}});
assert.equal(t.panel.hidden, false);
assert.ok(t.note.textContent.includes('<b>x</b>'));  // HTMLとして解釈せず文字列のまま
assert.equal(t.rows.children.length, 0);

t = form();
context.render(t.f, {effect_reread: {status: 'skipped', reason: 'pendulum_not_evaluated'}});
assert.ok(t.note.textContent.includes('ペンデュラム'));

t = form();
context.render(t.f, {effect_reread: {status: 'disagree', unreadable: true, ops: [
  ['equal', 'A', 'A'], ['replace', 'され', 'し'], ['equal', 'た', 'た'], ['insert', '', '<i>']]}});
assert.equal(t.panel.hidden, false);
assert.ok(t.note.textContent.includes('〓'));
assert.equal(t.rows.children.length, 4);  // dt, dd, dt, dd
const first = t.rows.children[1].children, reread = t.rows.children[3].children;
assert.deepEqual(first.map(c => c.tag === 'mark' ? 'M:' + c.textContent : c.text), ['A', 'M:され', 'た']);
assert.deepEqual(reread.map(c => c.tag === 'mark' ? 'M:' + c.textContent : c.text), ['A', 'M:し', 'た', 'M:<i>']);
console.log('ok');
