// 効果テキストの誤字チェック表示: HTMLとして解釈されないこと・強弱の並び・未検証の表示を検証する。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../static/admin/admin.js'), 'utf8');
const start = source.indexOf('const TEXT_CHECK_FIELD_LABELS');
const end = source.indexOf('function _setupTextCheck(');
assert.ok(start > 0 && end > start);

function fakeEl(tag) {
  return {
    tag, children: [], className: '', hidden: false, textContent: '',
    classList: {items: [], add(c) { this.items.push(c); }},
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren() { this.children = []; this.textContent = ''; },
  };
}
const context = vm.createContext({
  document: {createElement: fakeEl, createTextNode: text => ({textContent: text})},
});
vm.runInContext(source.slice(start, end) +
  '\nglobalThis.badge = _renderTextCheckBadge; globalThis.panel = _renderTextCheckPanel;', context);
const textOf = node => (node.textContent || '') + (node.children || []).map(textOf).join('');

const note = fakeEl('p'), list = fakeEl('ul');
const root = fakeEl('div'); root.hidden = true;
const form = {querySelector: sel => sel === '.text-check' ? root : sel === '.text-check__note' ? note : sel === '.text-check__list' ? list : null};
root.querySelector = sel => sel === '.text-check__note' ? note : list;

// 強・弱が混在: 強が先に並び、文字列は textContent で出る（HTMLとして解釈されない）
context.panel(form, {
  status: 'checked', strong: 1, weak: 1, unverified_fields: [],
  suspects: [
    {severity: 'weak', kind: 'unknown_ngram', field: 'effect_text', text: 'ab', context: 'xxabyy', suggestion: null, message: '参考'},
    {severity: 'strong', kind: 'unknown_ngram', field: 'effect_text', text: '<img src=x>', context: 'a<img src=x>b', suggestion: '墓', message: '要確認'},
  ],
}, false);
assert.equal(root.hidden, false);
assert.equal(list.children.length, 2);
assert.match(textOf(list.children[0]), /要確認/);
assert.match(textOf(list.children[0]), /候補: 墓/);
assert.match(textOf(list.children[1]), /参考/);
// 強調は <mark> 要素として作られ、中身は文字列のまま
const mark = (function find(n) { return n.tag === 'mark' ? n : (n.children || []).map(find).find(Boolean); })(list.children[0]);
assert.equal(mark.textContent, '<img src=x>');

// 未検証: 疑義なしと誤解させない文言
context.panel(form, {status: 'unchecked', suspects: []}, false);
assert.match(note.textContent, /疑義なしの意味ではありません/);
assert.equal(list.children.length, 0);

// 記録なし（旧データ）は欄ごと隠す
context.panel(form, null, true);
assert.equal(root.hidden, true);

// 札: 強は注意色クラス、疑義なしは出さない
const b1 = fakeEl('span'); context.badge(b1, {status: 'checked', strong: 2, weak: 0});
assert.equal(b1.hidden, false); assert.match(b1.textContent, /2件/); assert.ok(b1.classList.items.includes('text-check-badge--strong'));
const b2 = fakeEl('span'); b2.hidden = true; context.badge(b2, {status: 'checked', strong: 0, weak: 0});
assert.equal(b2.hidden, true);

// 札には「取込時」の注記（title）が付く
assert.match(b1.title, /取込時/);

// 強調は start/end の位置で行う: 文脈に同じ文字列が2回あっても、疑義の位置のほうを強調する
const marks = n => n.tag === 'mark' ? [n] : (n.children || []).flatMap(marks);
const beforeMark = n => {  // mark より前のテキスト
  let acc = '';
  (function walk(x) { if (x.tag === 'mark') return true; if (x.tag !== 'span' && x.textContent && !x.children) acc += x.textContent;
    for (const c of x.children || []) if (walk(c)) return true; return false; })(n);
  return acc;
};
context.panel(form, {
  status: 'checked', strong: 1, weak: 0, unverified_fields: [],
  suspects: [{severity: 'strong', kind: 'unknown_ngram', field: 'effect_text', text: 'ab', start: 16, end: 18,
              context_start: 10, context: 'xxabyyab', suggestion: null, message: 'm'}],
}, false);
const ctxSpan = (function find(n) { return n.className === 'text-check__context' ? n : (n.children || []).map(find).find(Boolean); })(list.children[0]);
assert.equal(marks(ctxSpan).length, 1);
assert.equal(beforeMark(ctxSpan), '…xxabyy');  // 2つ目の ab（文脈内の位置6）が強調される。先頭の … と手前の文字列

// 位置が文脈の中身と食い違うとき（編集で文字列が変わった等）は最初の一致で代用
context.panel(form, {
  status: 'checked', strong: 1, weak: 0, unverified_fields: [],
  suspects: [{severity: 'strong', kind: 'format', field: 'effect_text', text: 'ab', start: 99, end: 101,
              context_start: 10, context: 'xxabyyab', suggestion: null, message: 'm'}],
}, false);
const ctxSpan2 = (function find(n) { return n.className === 'text-check__context' ? n : (n.children || []).map(find).find(Boolean); })(list.children[0]);
assert.equal(beforeMark(ctxSpan2), '…xx');
console.log('ok');
