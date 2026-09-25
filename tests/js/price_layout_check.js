// 実コードを実行し、文言変更後のナビと0件検索後のDOM保持を検証する。
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert/strict');
const src = fs.readFileSync(path.join(__dirname, '../../static/js/index-main.js'), 'utf8');
function block(start, end) {
  const a = src.indexOf(start), b = src.indexOf(end, a + start.length);
  assert(a >= 0 && b > a, '実コードの検証対象が見つかりません');
  return src.slice(a, b);
}
function element() {
  const classes = new Set();
  return {
    dataset: {}, attributes: {}, textContent: '', value: '', innerHTML: '<保持する詳細DOM>',
    classList: {
      add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
      toggle(c, enabled) { if (enabled) classes.add(c); else classes.delete(c); },
    },
    setAttribute(k, v) { this.attributes[k] = v; },
    removeAttribute(k) { delete this.attributes[k]; },
  };
}
const elements = new Map();
const get = id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
const modes = ['search', 'meta', 'mydeck', 'packs', 'wishlist'];
const tabs = modes.map((mode, i) => Object.assign(element(), {
  dataset: { mode }, textContent: ['価格比較', '環境デッキ', 'マイデッキ', '新商品', '購入候補12'][i],
}));
let consumed = 0;
const sandbox = {
  document: { getElementById: get, querySelectorAll: () => tabs },
  window: {}, location: { hash: '', pathname: '/', search: '' },
  history: { replaceState() {} }, _currentMode: 'search',
  loadPacks() {}, renderWishlist() {}, loadMetaTiers() {}, loadMetaDeckPrices() {}, renderSavedDecks() {},
  _maybeOpenBuyInlineOnLoad() { consumed++; },
};
vm.createContext(sandbox);
vm.runInContext([
  block('function _setSearchLayout(', '// ── アフィリエイト'),
  block('function switchMode(', '// ── Deck Builder'),
  block('function renderAll(', 'function _summaryShopItems('),
].join('\n'), sandbox);

// ラベルの変更・並び変更・バッジ件数に関係なく、選択とコンテンツが一致する。
for (const mode of modes) {
  sandbox.switchMode(mode);
  assert.deepEqual(tabs.filter(b => b.classList.contains('active')).map(b => b.dataset.mode), [mode]);
  assert.deepEqual(tabs.filter(b => b.attributes['aria-current'] === 'page').map(b => b.dataset.mode), [mode]);
  for (const other of modes) assert.equal(get('mode-' + other).classList.contains('hidden'), other !== mode);
}

// 0件検索を繰り返しても、次の検索に必要な比較表・画像枠の親DOMを壊さない。
get('q').value = '見つからないカード';
const original = get('results').innerHTML;
sandbox._setSearchLayout(true);
for (let i = 0; i < 2; i++) sandbox.renderAll({ total: 0 });
assert.equal(get('results').innerHTML, original);
assert.equal(get('results').classList.contains('hidden'), true);
assert.equal(get('empty').classList.contains('hidden'), false);
assert.equal(get('emptyError').classList.contains('hidden'), false);
assert.equal(get('emptyError').textContent, '該当するカードが見つかりませんでした');
assert.equal(get('mode-search').classList.contains('has-search-result'), false);
assert.equal(consumed, 2);
sandbox._setSearchLayout(true);
assert.equal(get('mode-search').classList.contains('has-search-result'), true);
console.log('価格画面: ナビ5モードと0件検索後の再利用を確認');
