// 実モジュールで不足分計算、保存/復元、購入候補重複、旧データを検証する。
'use strict';
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=file=>fs.readFileSync(file,'utf8');
const data=new Map(),listeners=[];
const ta={value:'',addEventListener(){}};
const sandbox={console,navigator:{maxTouchPoints:0},setTimeout:()=>1,clearTimeout(){},
  localStorage:{getItem:k=>data.get(k)||null,setItem:(k,v)=>data.set(k,v),removeItem:k=>data.delete(k)},
  document:{readyState:'loading',getElementById:id=>id==='deckTextarea'?ta:null,
    querySelectorAll:()=>[],querySelector:()=>null,addEventListener:(name,fn)=>listeners.push(fn)},
  parseDeckSections:text=>text?JSON.parse(text):{main:[],ex:[]},
  _currentMydeckCards:{main:[],ex:[]},_currentMydeckText:'',_currentDeckName:'試験',
  CSS:{escape:x=>x},calcDeckEstimate(){},confirm:()=>true,
  savedDecksGet:()=>JSON.parse(data.get('cardprice_saved_decks')||'[]'),
  savedDecksSet:list=>data.set('cardprice_saved_decks',JSON.stringify(list))};
sandbox.window=sandbox;vm.createContext(sandbox);
vm.runInContext(source('static/mydeck/deck-ownership.js'),sandbox);
const own=sandbox.DeckOwnership;
const deck={main:[{name:'A',qty:3},{name:'B',qty:2}],ex:[{name:'A',qty:1},{name:'__proto__',qty:1}]};
const values=JSON.parse('{"A":1,"__proto__":0}');
const wishlist=[{name:'A',qty:1,rarity:'ウルトラ'}];
let rows=own.plan(deck,values,wishlist);
assert.equal(rows[0].required,4);assert.equal(rows[0].shortage,3);assert.equal(rows[0].candidate,1);assert.equal(rows[0].add,2);
assert.equal(rows[1].owned,null);assert.equal(rows[1].add,0);
assert.equal(rows[2].owned,0);assert.equal(rows[2].add,1);
const next=own.supplement(wishlist,rows);
assert.equal(next[0].rarity,'ウルトラ');assert.equal(next[0].qty,1);
assert.equal(next.find(c=>c.name==='A'&&c.rarity==='').qty,2);
assert.equal(own.plan(deck,values,next).reduce((n,c)=>n+c.add,0),0,'繰り返しで二重加算');
assert.equal(wishlist.length,1,'元データを変更');
assert.equal(own.plan(deck,{A:99},wishlist)[0].shortage,0);
for(const bad of [null,[],{A:-1},{A:100},{A:1.5},{A:'0'},{A:true}])assert.throws(()=>own.validateOwned(bad));
assert.throws(()=>own.plan({main:[{name:'A',qty:'3'}]}, {}, []));
assert.throws(()=>own.plan(deck,{},[{name:'A',qty:-1}]));
assert.throws(()=>own.supplement(Array.from({length:200},(_,i)=>({name:String(i),qty:1})),[{name:'new',add:1}]));
assert.throws(()=>own.supplement([{name:'A',qty:99}], [{name:'A',add:1}]));

// deck-edit全体のラッパーを起動し、保存・読込・新規/取込・下書きを確認する。
sandbox.loadSavedDeck=id=>{const d=sandbox.savedDecksGet().find(d=>d.id===id);if(d){ta.value=d.text;sandbox._currentMydeckText=d.text;sandbox._currentMydeckCards={main:d.main,ex:d.ex};}};
sandbox.clearDeck=()=>{ta.value='';sandbox._currentMydeckText='';sandbox._currentMydeckCards={main:[],ex:[]};};
sandbox.onDeckImported=()=>{ta.value=JSON.stringify(deck);};
sandbox.applyMetaDeckToTextarea=sandbox.onDeckImported;
// 実保存関数も同じVMへ入れ、名前入力後の保存にownedが含まれることを確認する。
const wishSource=source('static/js/index-wish.js');
sandbox.prompt=()=> '保存テスト';sandbox.alert=message=>{throw Error(message);};
sandbox.trackDeckCards=()=>{};sandbox.renderSavedDecks=()=>{};
sandbox.normalizeDeck=d=>({main:d.main||[],ex:d.ex||[]});sandbox.DECK_CTX={mydeck:{}};
vm.runInContext(wishSource.slice(wishSource.indexOf('function saveCurrentDeck(){'),wishSource.indexOf('function deleteSavedDeck(')),sandbox);
vm.runInContext(source('static/mydeck/deck-edit.js'),sandbox);
listeners.forEach(fn=>fn());
ta.value=JSON.stringify(deck);own.set(values);
sandbox.saveCurrentDeck();
assert.equal(sandbox.savedDecksGet()[0].owned.A,1,'新規保存から所持枚数が消えた');
const savedId=sandbox._currentSavedDeckId;
own.set({A:0});sandbox.saveCurrentDeck();
assert.equal(sandbox.savedDecksGet()[0].owned.A,0,'上書きで0枚を保持できない');
assert.equal(sandbox._currentSavedDeckId,savedId);
sandbox.clearDeck();sandbox.loadSavedDeck(savedId);assert.equal(own.snapshot().A,0);
// ownedのない旧デッキへ切り替えると、前の所持枚数を引き継がない。
sandbox.savedDecksSet([{id:'old',name:'旧',text:JSON.stringify(deck),main:deck.main,ex:deck.ex}]);
sandbox.loadSavedDeck('old');assert.equal(Object.keys(own.snapshot()).length,0);
ta.value=JSON.stringify(deck);own.set(values);
sandbox._currentSavedDeckId='one';sandbox.savedDecksSet([{id:'one',name:'試験',text:ta.value,main:deck.main,ex:deck.ex}]);
sandbox._persistDeckOwnership();
assert.equal(sandbox.savedDecksGet()[0].owned.A,1);
assert.equal(JSON.parse(data.get('cardprice_deck_draft')).owned.__proto__,0);
sandbox.clearDeck();assert.equal(Object.keys(own.snapshot()).length,0);
sandbox.loadSavedDeck('one');assert.equal(own.snapshot().A,1);
sandbox.onDeckImported();assert.equal(Object.keys(own.snapshot()).length,0);assert.equal(sandbox._currentSavedDeckId,null);
sandbox.loadSavedDeck('one');sandbox.applyMetaDeckToTextarea();assert.equal(Object.keys(own.snapshot()).length,0);
// ローカル保存失敗は呼び出し元に伝播し、成功扱いしない。
const original=sandbox.localStorage.setItem;sandbox.localStorage.setItem=()=>{throw Error('容量不足');};
assert.throws(()=>sandbox._persistDeckOwnership(),/容量不足/);sandbox.localStorage.setItem=original;
// 下書き復元をもう一度起動する。
data.set('cardprice_deck_draft',JSON.stringify({text:JSON.stringify(deck),main:deck.main,ex:deck.ex,owned:values}));
ta.value='';vm.runInContext(source('static/mydeck/deck-edit.js'),sandbox);listeners[listeners.length-1]();
assert.equal(own.snapshot().A,1);assert.equal(own.snapshot().__proto__,0);
console.log('所持枚数: 計算・二重追加防止・保存/読込/取込/下書き・入力検証・保存失敗を確認');
