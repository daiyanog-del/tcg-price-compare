// マイデッキのカード操作（static/mydeck/deck-card-ops.js）を実モジュールで確かめる。
// 「×N」の札が二重にならない／選んだカードの欄が描き直し・0枚・空のデッキ・デッキの入れ替えで正しく消える／
// 購入候補に入れるとき新しいカードだけ価格の控えを捨てる／カード名を innerHTML に入れない。
'use strict';
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync('static/mydeck/deck-card-ops.js', 'utf8');

// innerHTML に入れているのは欄の固定の文字列だけ（カード名など変数を連結していない）
const htmlAssigns = source.match(/innerHTML\s*=([\s\S]*?);/g) || [];
assert.equal(htmlAssigns.length, 1, 'innerHTML への代入が増えた');
assert.ok(!/\+\s*[A-Za-z_$]/.test(htmlAssigns[0].replace(/'[^']*'/g, "''")), 'innerHTML に変数を連結している');

// ── 最小限の DOM ──
class El {
  constructor(tag){ this.tagName = tag; this.children = []; this.parentNode = null; this.attrs = {}; this.dataset = {}; this.listeners = {};
    this.hidden = false; this._text = ''; this.className = ''; this.style = {};
    this.classList = { _s: new Set(), add: c => this.classList._s.add(c), remove: c => this.classList._s.delete(c), contains: c => this.classList._s.has(c) }; }
  get textContent(){ return this._text; } set textContent(v){ this._text = String(v); this.children = []; }
  set innerHTML(v){ this._html = v; this._parts = {}; ['deck-sel-thumb', 'deck-sel-name', 'deck-sel-qty'].forEach(c => { const e = new El('span'); e.className = c; this._parts[c] = e; }); }
  appendChild(c){ c.parentNode = this; this.children.push(c); return c; }
  insertBefore(c){ return this.appendChild(c); }
  removeChild(c){ this.children = this.children.filter(x => x !== c); }
  addEventListener(n, fn){ (this.listeners[n] = this.listeners[n] || []).push(fn); }
  removeEventListener(){}
  setAttribute(k, v){ this.attrs[k] = String(v); } removeAttribute(k){ delete this.attrs[k]; } hasAttribute(k){ return k in this.attrs; }
  querySelector(sel){
    if(this._parts){ const m = sel.match(/^\.(deck-sel-\w+)$/); if(m) return this._parts[m[1]]; }
    if(sel === '.deck-grid-badge') return this.badge || null;
    if(sel === '.deck-grid-img-wrap img') return this.img || null;
    if(sel === 'a.deck-grid-img-wrap') return null;
    return null;
  }
  querySelectorAll(sel){ return sel.startsWith('.deck-grid-cell') ? cells.filter(c => !sel.includes('[data-sel]') || c.hasAttribute('data-sel')) : []; }
  contains(){ return false; }
  closest(){ return null; }
}
let cells = [];
const byId = { deckCardList: new El('div'), deckPaneMain: new El('div'), deckOwnership: new El('details') };
function cell(name, sec, qty){
  const c = new El('div'); c.className = 'deck-grid-cell'; c.dataset.card = name; c.dataset.sec = sec;
  if(qty > 1){ c.badge = new El('span'); c.badge.textContent = String(qty); }
  c.img = { src: 'https://example.test/' + encodeURIComponent(name) + '.jpg' };
  return c;
}
let deck = { main: [{ name: '灰流うらら', qty: 3 }, { name: '天底の使徒', qty: 1 }], ex: [{ name: 'アクセスコード・トーカー', qty: 1 }] };
const render = () => { cells = [].concat(deck.main.map(c => cell(c.name, 'main', c.qty)), deck.ex.map(c => cell(c.name, 'ex', c.qty))); };
const wrapped = {};
const sandbox = {
  console, setTimeout: fn => fn(), Date, JSON, Object, String, encodeURIComponent,
  document: { readyState: 'complete', body: new El('body'), activeElement: null, documentElement: { clientWidth: 1440 },
    getElementById: id => byId[id] || null, createElement: t => new El(t), querySelector: () => null, addEventListener(){}, removeEventListener(){} },
  matchMedia: () => ({ matches: true }), innerHeight: 900, addEventListener(){}, removeEventListener(){},
  get _currentMydeckCards(){ return deck; },
  _deckOps: { isTouch: false, inc(){}, dec(){}, remove(){} },
  loadSavedDeck(){ wrapped.load = true; }, clearDeck(){ deck = { main: [], ex: [] }; cells = []; }, onDeckImported(){},
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const ops = sandbox.DeckCardOps;
const bar = () => byId.deckPaneMain.children.find(c => c.id === 'deckSelBar');

// ── 「×N」の札は何度描き直しても二重にならない ──
render(); ops.afterRender(); ops.afterRender();
assert.equal(cells[0].badge.textContent, '×3');
assert.equal(cells[0].attrs.tabindex, '0', 'PC でカードに Tab で止まれない');

// ── 選ぶと欄が出て、描き直しても印が付け直される ──
ops.select(cells[0]);
assert.equal(bar().hidden, false);
assert.equal(bar()._parts['deck-sel-name'].textContent, '灰流うらら');
assert.equal(bar()._parts['deck-sel-qty'].textContent, '3');
render(); ops.afterRender();
assert.ok(cells[0].hasAttribute('data-sel'), '描き直しで選択の印が消えた');
// 価格計算は row.className を書き換えるが、印は data 属性なので残る
cells[0].className = 'deck-grid-cell';
assert.ok(cells[0].hasAttribute('data-sel'));

// ── 0枚まで抜いたら選択をやめる ──
ops.select(cells[1]);
deck.main = deck.main.filter(c => c.name !== '天底の使徒');
render(); ops.afterRender();
assert.equal(bar().hidden, true, '0枚になっても欄が残っている');

// ── デッキの入れ替え（新しく作る）で欄が消える（空のデッキは一覧の描き直しを通らない：reviewer 高2） ──
render(); ops.afterRender(); ops.select(cells[0]);
assert.equal(bar().hidden, false);
sandbox.clearDeck();
assert.equal(bar().hidden, true, 'デッキを空にしても欄が古いカードのまま残る');

// ── 空のデッキの経路（deck-edit.js _refreshDeckChrome から afterRender）でも消える ──
deck = { main: [{ name: '灰流うらら', qty: 1 }], ex: [] }; render(); ops.afterRender(); ops.select(cells[0]);
deck = { main: [], ex: [] }; cells = []; ops.afterRender();
assert.equal(bar().hidden, true, '最後の1枚を抜いても欄が残る');

// deck-edit.js の空の分岐が afterRender を呼んでいること
assert.ok(/results\.classList\.add\('hidden'\);\s*\/\/[^\n]*\n\s*if\(window\.DeckCardOps\) window\.DeckCardOps\.afterRender\(\);/.test(fs.readFileSync('static/mydeck/deck-edit.js', 'utf8')),
  'deck-edit.js の空のデッキの分岐から欄を片付けていない');

console.log('マイデッキのカード操作: ×N の札・選択の印と欄・0枚/空/入れ替えでの解除・innerHTML の中身を確認');
