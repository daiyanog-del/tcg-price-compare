// マイデッキの見出し（static/mydeck/deck-head.js）の保存まわりを、実モジュールで確かめる。
// 登録の中身（text/main/ex/owned・ひも付け）、削除の取り消し（位置・名前のぶつかり・ひも付けの戻し）、
// 取り消しの期限で購入候補の「どのデッキ用か」を外すこと、テキスト欄の入力で登録ボタンが出ること。
'use strict';
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = file => fs.readFileSync(file, 'utf8');

// ── 最小限の DOM ──
class El {
  constructor(tag){ this.tagName = tag; this.children = []; this.parentNode = null; this.listeners = {}; this.hidden = false;
    this.textContent = ''; this.className = ''; this.attrs = {}; this.style = {}; this.dataset = {};
    this.classList = { _s: new Set(), add: c => this.classList._s.add(c), remove: c => this.classList._s.delete(c),
      toggle: (c, on) => { if(on === undefined ? !this.classList._s.has(c) : on) this.classList._s.add(c); else this.classList._s.delete(c); },
      contains: c => this.classList._s.has(c) }; }
  appendChild(c){ c.parentNode = this; this.children.push(c); return c; }
  append(...cs){ cs.forEach(c => this.appendChild(c)); }
  removeChild(c){ this.children = this.children.filter(x => x !== c); c.parentNode = null; }
  addEventListener(n, fn){ (this.listeners[n] = this.listeners[n] || []).push(fn); }
  removeEventListener(){}
  dispatch(n, ev){ (this.listeners[n] || []).forEach(fn => fn(ev || { preventDefault(){} })); }
  setAttribute(k, v){ this.attrs[k] = v; }
  getAttribute(k){ return this.attrs[k]; }
  querySelector(){ return null; }
  querySelectorAll(){ return []; }
  contains(x){ return x === this || this.children.some(c => c.contains && c.contains(x)); }
  getBoundingClientRect(){ return { left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0 }; }
  focus(){} select(){}
  get offsetWidth(){ return 0; } get offsetHeight(){ return 0; } get offsetParent(){ return {}; }
}
const byId = {};
['deckHeadName', 'deckSaveStatus', 'deckRegisterBtn', 'deckImageBtn', 'deckMoreBtn', 'deckTextRegisterBtn', 'deckCardList'].forEach(id => { byId[id] = new El('div'); });
const ta = new El('textarea'); ta.value = ''; byId.deckTextarea = ta;
const body = new El('body');
const allToasts = () => body.children.filter(c => c.className === 'deck-head-toast');
const toastText = el => el.children[0].textContent;

// ── 時計（取り消しの期限を進めるため） ──
let now = 1000, timers = [];
const setTimeout_ = (fn, ms) => { const t = { fn, at: now + ms }; timers.push(t); return t; };
const clearTimeout_ = t => { timers = timers.filter(x => x !== t); };
const advance = ms => { now += ms; const due = timers.filter(t => t.at <= now); timers = timers.filter(t => t.at > now); due.forEach(t => t.fn()); };

const data = new Map();
const sandbox = {
  console, Date: { now: () => now }, setTimeout: setTimeout_, clearTimeout: clearTimeout_, Object, JSON, Math, String, Promise,
  localStorage: { getItem: k => data.get(k) || null, setItem: (k, v) => data.set(k, v), removeItem: k => data.delete(k) },
  document: { readyState: 'complete', body, documentElement: { clientWidth: 1440 },
    getElementById: id => byId[id] || null, createElement: tag => new El(tag),
    querySelector: () => null, querySelectorAll: () => [], addEventListener(){}, removeEventListener(){} },
  innerHeight: 900, addEventListener(){}, removeEventListener(){},
  CSS: { escape: x => x },
  // index-main.js の parseDeckSections と同じ書式（[EX] で区切る・「N 名前」）
  parseDeckSections: text => {
    const parse = t => t.split('\n').map(l => l.trim()).filter(Boolean).map(l => { const m = l.match(/^(\d+)\s+(.+)$/); return m ? { name: m[2], qty: +m[1] } : { name: l, qty: 1 }; });
    const lines = text.split('\n'); const i = lines.findIndex(l => l.trim() === '[EX]');
    return i < 0 ? { main: parse(text), ex: [] } : { main: parse(lines.slice(0, i).join('\n')), ex: parse(lines.slice(i + 1).join('\n')) };
  },
  _currentMydeckCards: { main: [], ex: [] }, _currentMydeckText: '', _currentDeckName: '',
  savedDecksGet: () => JSON.parse(data.get('cardprice_saved_decks') || '[]'),
  savedDecksSet: list => data.set('cardprice_saved_decks', JSON.stringify(list)),
  renderSavedDecks(){}, trackDeckCards(){}, _ga(){},
  DeckOwnership: { snapshot: () => ({ 灰流うらら: 2 }) },
};
let persisted = 0;
sandbox._deckPersist = () => { persisted++; };
sandbox._refreshDeckChrome = () => sandbox.DeckHead.refresh();
let wishRendered = 0;
let wish = [{ name: '灰流うらら', qty: 1, decks: {} }];
sandbox.wishGet = () => JSON.parse(JSON.stringify(wish));
sandbox.wishSave = list => { wish = list; };
sandbox.renderWishlist = () => { wishRendered++; };
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(source('static/shared/wish-deck.js'), sandbox);
vm.runInContext(source('static/mydeck/deck-head.js'), sandbox);
const H = sandbox.DeckHead;

// ── 1. テキスト欄に貼っただけで「マイデッキに登録」が出る（reviewer 高1） ──
assert.equal(byId.deckRegisterBtn.hidden, true, '空のデッキで登録ボタンが出ている');
ta.value = '3 灰流うらら\n2 増殖するG\n[EX]\n1 アクセスコード・トーカー';
ta.dispatch('input');
assert.equal(byId.deckRegisterBtn.hidden, false, 'テキスト欄に貼っても登録ボタンが出ない');
assert.equal(byId.deckTextRegisterBtn.hidden, false, 'テキスト欄の下の登録ボタンが出ない');
assert.equal(byId.deckHeadName.textContent, 'マイデッキ未登録');

// ── 2. 登録の中身（以前の saveCurrentDeck と同じ text/main/ex/owned）とひも付け ──
const deck = H._createFromEditor('ドラグマ');
const saved = sandbox.savedDecksGet();
assert.equal(saved.length, 1);
assert.equal(saved[0].text, ta.value);
assert.deepEqual(saved[0].main, [{ name: '灰流うらら', qty: 3 }, { name: '増殖するG', qty: 2 }]);
assert.deepEqual(saved[0].ex, [{ name: 'アクセスコード・トーカー', qty: 1 }]);
assert.deepEqual(saved[0].owned, { 灰流うらら: 2 });
assert.equal(sandbox._currentSavedDeckId, deck.id);
assert.equal(sandbox._currentDeckName, 'ドラグマ');
assert.equal(byId.deckHeadName.textContent, 'ドラグマ');
assert.equal(byId.deckSaveStatus.textContent, '自動で保存しています');
assert.equal(byId.deckRegisterBtn.hidden, true, '登録後も登録ボタンが出ている');

// ── 3. 開いているデッキを削除 → 未登録に。取り消しでひも付けも戻る ──
wish = [{ name: '灰流うらら', qty: 1, decks: { [deck.id]: 1 } }];
H.deleteDeck(deck.id);
assert.equal(sandbox.savedDecksGet().length, 0);
assert.equal(sandbox._currentSavedDeckId, null, '削除後もひも付いたまま');
assert.equal(byId.deckHeadName.textContent, 'マイデッキ未登録');
assert.ok(wishRendered > 0, '削除で購入候補を描き直していない');
let undo = allToasts().find(t => t.children[1]);
assert.ok(undo, '取り消しのお知らせが出ない');
// 取り消しを待っている間の普通のお知らせで、取り消しが消えない（reviewer 中3）
advance(1); H._createFromEditor('別のデッキ');
sandbox._currentSavedDeckId = null; // 登録した方からは外しておく（下の名前のぶつかりの試験用）
sandbox.savedDecksSet(sandbox.savedDecksGet().map(d => ({ ...d, name: 'ドラグマ' })));
assert.ok(allToasts().includes(undo), '別の操作で取り消しのお知らせが消えた');
undo.children[1].dispatch('click');
let names = sandbox.savedDecksGet().map(d => d.name).sort();
assert.deepEqual(names, ['ドラグマ', 'ドラグマ (2)'], '取り消しで同じ名前のデッキが2つできた（reviewer 中2）');
assert.equal(sandbox.savedDecksGet()[0].id, deck.id, '元の位置に戻っていない');
assert.equal(sandbox._currentSavedDeckId, deck.id, '中身を変えていないのにひも付けが戻らない');
assert.equal(sandbox._currentDeckName, 'ドラグマ (2)');
// 取り消したので期限が来ても購入候補の内訳は残る
advance(10000);
assert.equal(wish[0].decks[deck.id], 1, '取り消したのに購入候補の内訳が消えた');

// ── 4. 削除して取り消さなければ、期限で購入候補を「デッキ指定なし」に戻す（reviewer 中1） ──
H.deleteDeck(deck.id);
assert.equal(wish[0].decks && wish[0].decks[deck.id], 1, '期限前に内訳を消した（取り消しで戻せなくなる）');
advance(6000);
assert.ok(!wish[0].decks || !(deck.id in wish[0].decks), '期限が来ても購入候補の内訳が残っている');

// ── 5. 削除の後に中身を変えてから取り消すと、デッキは戻るがひも付けは戻さない ──
advance(1); const d2 = H._createFromEditor('三つ目');
H.deleteDeck(d2.id);
ta.value = '1 灰流うらら';
ta.dispatch('input');
undo = allToasts().find(t => t.children[1]);
undo.children[1].dispatch('click');
assert.ok(sandbox.savedDecksGet().some(d => d.id === d2.id), '取り消しでデッキが戻らない');
assert.equal(sandbox._currentSavedDeckId, null, '中身を変えたのにひも付けを戻した');

// ── 6. 名前の空き ──
assert.equal(H._freeName('新しい名前'), '新しい名前');
assert.equal(H._freeName('ドラグマ'), 'ドラグマ (2)'); // 「ドラグマ (2)」は4で削除済み

console.log('マイデッキの見出し: 登録の中身・削除の取り消し・名前のぶつかり・購入候補の後始末・テキスト入力での登録ボタンを確認');
