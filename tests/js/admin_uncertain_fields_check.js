// AIが不確かとした欄の表示: 空なら出さない・文字列は textContent のまま（HTMLとして解釈しない）
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../static/admin/admin.js'), 'utf8');
const start = source.indexOf('function _renderUncertainFields(');
const end = source.indexOf('/**', start);
assert.ok(start > 0 && end > start);
const context = vm.createContext({});
vm.runInContext(source.slice(start, end) + '\nglobalThis.render = _renderUncertainFields;', context);

const el = () => ({hidden: true, textContent: ''});
let e = el();
context.render(e, {uncertain_field_labels: ['効果テキスト', '攻撃力']});
assert.equal(e.hidden, false);
assert.equal(e.textContent, 'AIが不確かとした欄: 効果テキスト、攻撃力');
for (const card of [{}, {uncertain_field_labels: []}, {uncertain_field_labels: null}]) {
  e = el(); context.render(e, card);
  assert.equal(e.hidden, true); assert.equal(e.textContent, '');
}
context.render(null, {});
e = el(); context.render(e, {uncertain_field_labels: ['<img src=x>']});
assert.equal(e.textContent, 'AIが不確かとした欄: <img src=x>');
console.log('ok');
