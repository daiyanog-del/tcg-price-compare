// 「買取価格も表示する」ボタン（_openBuyInlineFromTableBtn）が #buyInline へスクロールすることを検証する。
// 開く場合も既に開いている場合もスクロールし、既に開いている場合は計測用トリガーを触らない。
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert/strict');
const main = fs.readFileSync(path.join(__dirname, '../../static/js/index-main.js'), 'utf8');
const a = main.indexOf('function _openBuyInlineFromTableBtn('), b = main.indexOf('function _fetchBuyInline(', a);
assert(a >= 0 && b > a, '抽出失敗');
function run(reduce, openInit) {
  const calls = [];
  const det = { open: openInit, scrollIntoView(o) { calls.push({ behavior: o.behavior, block: o.block }); } };
  const sandbox = { document: { getElementById: () => det }, window: { matchMedia: () => ({ matches: reduce }) } };
  vm.createContext(sandbox);
  vm.runInContext('let _buyInlineOpenTrigger="inline";' + main.slice(a, b) + ';this.trig=()=>_buyInlineOpenTrigger', sandbox);
  vm.runInContext('_openBuyInlineFromTableBtn()', sandbox);
  return { calls, det, trig: sandbox.trig() };
}
let r = run(false, false);
assert.equal(r.det.open, true); assert.equal(r.trig, 'table_button');
assert.deepEqual(r.calls, [{ behavior: 'smooth', block: 'start' }]);
r = run(false, true);
assert.equal(r.trig, 'inline'); // 二重計測しない
assert.deepEqual(r.calls, [{ behavior: 'smooth', block: 'start' }]);
r = run(true, false);
assert.equal(r.calls[0].behavior, 'auto');
console.log('buy_scroll_check OK');
