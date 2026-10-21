// マイデッキの一覧（static/mydeck/deck-library.js）と右の欄（static/mydeck/deck-tools.js）を実モジュールで確かめる。
// renderSavedDecks の置き換え・件数・登録していない編集中のデッキを開くときの確認・取り込めたら窓を閉じる順序・
// 価格計算の欄の置き場所・テキストが0枚なら空として扱う・別の画面へ切り替えたら閉じる。
'use strict';
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');

class El {
  constructor(id){ this.id = id; this.children = []; this.parentNode = null; this.hidden = false; this.textContent = ''; this.value = '';
    this.attrs = {}; this.style = {}; this.listeners = {};
    this.classList = { _s: new Set(), add: c => this.classList._s.add(c), remove: c => this.classList._s.delete(c), toggle: (c, on) => on ? this.classList._s.add(c) : this.classList._s.delete(c), contains: c => this.classList._s.has(c) }; }
  appendChild(c){ if(c.parentNode) c.parentNode.children = c.parentNode.children.filter(x => x !== c); c.parentNode = this; this.children.push(c); return c; }
  insertBefore(c){ return this.appendChild(c); }
  removeChild(c){ this.children = this.children.filter(x => x !== c); c.parentNode = null; }
  addEventListener(n, fn){ (this.listeners[n] = this.listeners[n] || []).push(fn); }
  removeEventListener(){}
  setAttribute(k, v){ this.attrs[k] = v; } getAttribute(k){ return this.attrs[k]; }
  querySelector(){ return null; } querySelectorAll(){ return []; }
  contains(x){ return x === this; } getBoundingClientRect(){ return { left: 0, right: 0, top: 0, bottom: 0 }; }
  focus(){} get offsetWidth(){ return 0; } get offsetHeight(){ return 0; }
}
const ids = ['deckLibBtn', 'deckLibCount', 'deckTextarea', 'deckImportPop', 'deckImportBtn', 'deckTextPop', 'deckTextBtn',
  'deckCalc', 'deckCalcSlotMain', 'deckCalcSlotTools', 'deckCalcHeading'];
const byId = {}; ids.forEach(id => { byId[id] = new El(id); });
byId.deckImportPop.hidden = true; byId.deckTextPop.hidden = true;
const modeHolder = new El('mode'); modeHolder.appendChild(byId.deckImportPop); modeHolder.appendChild(byId.deckTextPop);
byId.deckCalcSlotMain.appendChild(byId.deckCalc);
let sheet = false, confirmAnswer = true, confirms = 0, loaded = [], previews = 0, chromes = 0, calls = [];
let decks = [{ id: 'd_1', name: 'A', text: '3 灰流うらら', updated: 1 }, { id: 'd_2', name: 'B', text: '1 増殖するG', updated: 'x' }];
const sandbox = {
  console, setTimeout: () => 0, clearTimeout(){}, JSON, Object, Date, isNaN,
  document: { readyState: 'complete', body: new El('body'), activeElement: null, documentElement: { clientWidth: 1440 },
    getElementById: id => byId[id] || null, createElement: () => new El(''), querySelector: () => null, addEventListener(){}, removeEventListener(){} },
  matchMedia: () => ({ get matches(){ return sheet; }, addEventListener(){} }), innerHeight: 900, addEventListener(){}, removeEventListener(){},
  savedDecksGet: () => decks, normalizeDeck: d => ({ main: [{ name: d.text.split(' ')[1], qty: 1 }], ex: [] }),
  renderSavedDecks: () => { calls.push('old'); },
  loadSavedDeck: id => loaded.push(id), confirm: () => { confirms++; return confirmAnswer; },
  switchMode: m => calls.push('switch:' + m), switchDeckPane(){},
  onDeckImported: () => calls.push('imported'),
  parseDeckSections: t => { const lines = t.split('\n').filter(l => l.trim() && l.trim() !== '[EX]'); return { main: lines, ex: [] }; },
  calcDeckEstimate: () => { previews++; }, DECK_CTX: { mydeck: {} }, _refreshDeckChrome: () => { chromes++; },
  _currentSavedDeckId: null,
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('static/mydeck/deck-library.js', 'utf8'), sandbox);
vm.runInContext(fs.readFileSync('static/mydeck/deck-tools.js', 'utf8'), sandbox);

// ── renderSavedDecks は一覧の窓の描き直しに置き換わり、件数を出す ──
assert.notEqual(sandbox.renderSavedDecks.toString(), "() => { calls.push('old'); }", 'renderSavedDecks が置き換わっていない');
decks.push({ id: 'd_3', name: 'C', text: '1 墓穴の指名者', updated: 3 });
sandbox.renderSavedDecks();
assert.equal(byId.deckLibCount.textContent, '3');
assert.ok(!calls.includes('old'));

// ── 登録していない編集中のデッキがあるときに別のデッキを開くと確認する（やめたら開かない） ──
byId.deckTextarea.value = '3 灰流うらら';
confirmAnswer = false; sandbox.DeckLibrary._openDeck('d_2');
assert.equal(confirms, 1); assert.deepEqual(loaded, [], '確認でやめたのに開いた');
confirmAnswer = true; sandbox.DeckLibrary._openDeck('d_2');
assert.deepEqual(loaded, ['d_2']);
// 登録済みのデッキを開いているときは確認しない
sandbox._currentSavedDeckId = 'd_2'; sandbox.DeckLibrary._openDeck('d_1');
assert.equal(confirms, 2); assert.deepEqual(loaded, ['d_2', 'd_1']);

// ── 取り込めたら取り込みの窓を閉じる（元の onDeckImported の後で） ──
sandbox.DeckTools.openImport();
assert.equal(byId.deckImportPop.hidden, false);
sandbox.onDeckImported({});
assert.equal(calls[calls.length - 1], 'imported');
assert.equal(byId.deckImportPop.hidden, true, '取り込んでも窓が閉じない');

// ── 別の画面へ切り替えたら窓を閉じる ──
sandbox.DeckTools.openText();
assert.equal(byId.deckTextPop.hidden, false);
sandbox.switchMode('search');
assert.equal(byId.deckTextPop.hidden, true, '別の画面へ切り替えても窓が残る');

// ── 価格計算の欄: PC は右の欄・スマホはデッキの下 ──
assert.equal(byId.deckCalc.parentNode, byId.deckCalcSlotTools, 'PC で価格計算が右の欄に移っていない');
assert.equal(byId.deckCalcHeading.hidden, false);

// ── テキストが0枚（[EX] だけ）なら空として扱い、古い一覧を残さない ──
sandbox.DeckTools.openText();
byId.deckTextarea.value = '[EX]';
sandbox.DeckTools.close(false);
assert.equal(chromes, 1, '0枚のテキストで一覧を空にしていない'); assert.equal(previews, 0);
sandbox.DeckTools.openText();
byId.deckTextarea.value = '1 灰流うらら';
sandbox.DeckTools.close(false);
assert.equal(previews, 1, 'テキストを変えて閉じても一覧に反映しない');

console.log('マイデッキの一覧と右の欄: renderSavedDecks の置き換え・開くときの確認・取り込み後に閉じる・画面切替で閉じる・価格計算の置き場所・0枚のテキストを確認');
