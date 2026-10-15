// 「テキストの誤りを報告」の共通部品（static/shared/text-report.js）と、管理画面の報告札・一覧
// （static/admin/admin.js の _renderReportBadge / _renderReportList）の挙動を DOM スタブで検証する。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function fakeEl(tag) {
  const e = {
    tag, children: [], className: '', hidden: false, textContent: '', value: '', disabled: false, id: '',
    attrs: {}, listeners: {},
    classList: {items: [], add(c) { this.items.push(c); }},
    appendChild(c) { this.children.push(c); c.parent = this; return c; },
    replaceChildren() { this.children = []; this.textContent = ''; },
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
    focus() { fakeDoc.activeElement = e; },
    remove() { if (e.parent) e.parent.children = e.parent.children.filter(x => x !== e); },
    querySelectorAll(sel) {
      const cls = sel.replace('.', '');
      const out = [];
      (function walk(n) { for (const c of n.children) { if ((c.className || '').split(' ').includes(cls)) out.push(c); walk(c); } })(e);
      return out;
    },
    click() { (e.listeners.click || []).forEach(f => f({})); },
  };
  return e;
}
const fakeDoc = {
  activeElement: null, body: fakeEl('body'), listeners: {},
  createElement: fakeEl, createTextNode: t => ({textContent: t}),
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
};
fakeDoc.body.appendChild = function (c) { this.children.push(c); c.parent = this; return c; };

// ── 共通部品 ──
const fetches = [];
let nextResponse = {status: 200, body: {ok: true}};
const ctx = vm.createContext({
  document: fakeDoc, window: {},
  fetch: (url, opts) => { fetches.push({url, opts}); const r = nextResponse;
    return Promise.resolve({status: r.status, ok: r.status >= 200 && r.status < 300, json: () => Promise.resolve(r.body)}); },
  setTimeout, console,
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../static/shared/text-report.js'), 'utf8'), ctx);
const TR = ctx.window.TextReport;
assert.ok(TR && typeof TR.attach === 'function');

const textOf = n => (n.textContent || '') + (n.children || []).map(textOf).join('');
const find = (n, cls) => (n.className || '').split(' ').includes(cls) ? n : (n.children || []).map(c => find(c, cls)).find(Boolean);

(async () => {
  // ボタンは1つだけ（再描画で二重にならない）。名前が空なら置かない
  const box = fakeEl('div');
  TR.attach(box, '<img src=x onerror=alert(1)>');
  TR.attach(box, '<img src=x onerror=alert(1)>');
  assert.equal(box.querySelectorAll('.tr-open-btn').length, 1);
  assert.equal(find(box, 'tr-open-btn').textContent, 'テキストの誤りを報告');
  const empty = fakeEl('div');
  assert.equal(TR.attach(empty, ''), null);
  assert.equal(empty.children.length, 0);

  // 開くとフォームが1つ作られ、カード名は textContent で入る（HTML として解釈されない）
  find(box, 'tr-open-btn').click();
  const overlay = fakeDoc.body.children.find(c => c.className === 'tr-overlay');
  assert.ok(overlay && overlay.hidden === false);
  assert.equal(find(overlay, 'tr-card-name').textContent, '<img src=x onerror=alert(1)>');
  // 欄の選択肢は5つ。ひとことは500字上限
  assert.equal(find(overlay, 'tr-select').children.length, 5);
  assert.equal(find(overlay, 'tr-textarea').maxLength, 500);
  // 開き直してもフォームは増えない
  find(box, 'tr-open-btn').click();
  assert.equal(fakeDoc.body.children.filter(c => c.className === 'tr-overlay').length, 1);

  // 送信: POST /api/card-text-report {name, field, note}
  const sel = find(overlay, 'tr-select'), note = find(overlay, 'tr-textarea');
  sel.value = 'pendulum_effect'; note.value = '  墓地へ返って  ';
  find(overlay, 'tr-submit').click();
  await new Promise(r => setImmediate(r));
  assert.equal(fetches.length, 1);
  assert.equal(fetches[0].url, '/api/card-text-report');
  assert.equal(fetches[0].opts.method, 'POST');
  assert.deepEqual(JSON.parse(fetches[0].opts.body),
    {name: '<img src=x onerror=alert(1)>', field: 'pendulum_effect', note: '墓地へ返って'});
  assert.equal(find(overlay, 'tr-status').textContent, '報告を受け付けました。ありがとうございます');
  assert.equal(find(overlay, 'tr-submit').textContent, '閉じる');
  // 送信後にもう一度押しても二重送信しない（閉じるだけ）
  find(overlay, 'tr-submit').click();
  await new Promise(r => setImmediate(r));
  assert.equal(fetches.length, 1);
  assert.equal(overlay.hidden, true);

  // 429: 失敗表示にして、再送できる状態に戻す
  nextResponse = {status: 429, body: {error: 'x'}};
  find(box, 'tr-open-btn').click();
  find(overlay, 'tr-submit').click();
  await new Promise(r => setImmediate(r));
  assert.match(find(overlay, 'tr-status').textContent, /送りすぎ/);
  assert.equal(find(overlay, 'tr-submit').disabled, false);

  // サーバーのエラー文をそのまま表示（textContent）
  nextResponse = {status: 404, body: {error: '対象のカードが見つかりません'}};
  find(overlay, 'tr-submit').click();
  await new Promise(r => setImmediate(r));
  assert.equal(find(overlay, 'tr-status').textContent, '対象のカードが見つかりません');

  // ── 管理画面 ──
  const src = fs.readFileSync(path.join(__dirname, '../../static/admin/admin.js'), 'utf8');
  const s = src.indexOf('/** 未対応の報告件数の札。');
  const e = src.indexOf('function _setupReportPanel(');
  assert.ok(s > 0 && e > s);
  const adminCtx = vm.createContext({document: fakeDoc, _formatDate: x => 'D:' + x});
  vm.runInContext(src.slice(s, e) + '\nglobalThis.badge = _renderReportBadge; globalThis.list = _renderReportList;', adminCtx);

  const b = fakeEl('span'); b.hidden = true;
  adminCtx.badge(b, 3);
  assert.equal(b.hidden, false); assert.equal(b.textContent, '報告 3件（未対応）');
  adminCtx.badge(b, 0);
  assert.equal(b.hidden, true);

  const panel = fakeEl('div');
  const ul = fakeEl('ul'); panel.querySelector = () => ul;
  const resolved = [];
  adminCtx.list(panel, [
    {id: 7, field_label: '効果テキスト', note: '<b>x</b>', created_at: 'T1', status: 'open'},
    {id: 6, field_label: 'その他', note: '', created_at: 'T0', status: 'resolved'},
  ], (id, btn) => resolved.push(id));
  assert.equal(panel.hidden, false);
  assert.equal(ul.children.length, 2);
  assert.match(textOf(ul.children[0]), /<b>x<\/b>/);        // HTML としては解釈されない
  assert.match(textOf(ul.children[0]), /対応済みにする/);
  assert.match(textOf(ul.children[1]), /対応済み/);
  assert.doesNotMatch(textOf(ul.children[1]), /対応済みにする/);
  assert.match(textOf(ul.children[1]), /ひとことなし/);
  const btn = find(ul.children[0], 'btn');
  btn.click();
  assert.deepEqual(resolved, [7]);
  adminCtx.list(panel, [], () => {});
  assert.equal(panel.hidden, true);
  console.log('ok');
})().catch(err => { console.error(err); process.exit(1); });
