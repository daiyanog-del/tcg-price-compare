// 実コードの履歴復元と非同期候補の競合を、通信順序を制御して検証する。
'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert/strict');
const src=fs.readFileSync(path.join(__dirname,'../../static/js/index-main.js'),'utf8');
function block(start,end){
  const a=src.indexOf(start),b=src.indexOf(end,a+start.length);
  assert(a>=0&&b>a);return src.slice(a,b);
}
function element(){
  const classes=new Set(),listeners={};
  return {value:'',innerHTML:'',textContent:'',style:{},dataset:{},listeners,children:[],attributes:{},
    classList:{add:c=>classes.add(c),remove:c=>classes.delete(c),contains:c=>classes.has(c),
      toggle(c,on){if(on)classes.add(c);else classes.delete(c);}},
    querySelector(selector){
      if(selector==='.search-failure-note')return this.children.find(child=>child.className==='search-failure-note')||null;
      return this.child||(this.child=element());
    },
    appendChild(child){this.children.push(child);child.parent=this;},
    remove(){this.parent.children=this.parent.children.filter(child=>child!==this);},
    addEventListener(k,fn){listeners[k]=fn;},
    setAttribute(name,value){this.attributes[name]=value;if(name==='title')this.title=value;},
    getAttribute(name){return this.attributes[name]??null;},
    removeAttribute(name){delete this.attributes[name];if(name==='title')delete this.title;},scrollIntoView(){}};
}
const elements=new Map(),get=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);};
const tabs=['search','meta','packs','mydeck','wishlist'].map(mode=>Object.assign(element(),{dataset:{mode}}));
const timers=new Map(),requests=[],searches=[],listeners={},history=[];
let serial=0,canceled=0;
const location={pathname:'/',search:'',hash:''};
function setUrl(url){const u=new URL(url,'https://example.test');Object.assign(location,{pathname:u.pathname,search:u.search,hash:u.hash});}
const sandbox={console,encodeURIComponent,decodeURIComponent,location,
  document:{getElementById:get,createElement:element,querySelectorAll:()=>tabs,activeElement:get('q'),title:''},
  window:{location,addEventListener:(k,fn)=>listeners[k]=fn},
  history:{pushState(state,unused,url){history.push(url);setUrl(url);}},
  setTimeout(fn){const id=++serial;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);},
  fetch(url){return new Promise((resolve,reject)=>requests.push({url,resolve:items=>resolve({json:()=>Promise.resolve(items)}),reject}));},
  _currentMode:'search',_pendingBuyInlineOpen:false,_searchGen:0,_newSearchGen(){canceled++;return ++sandbox._searchGen;},_setSearchLayout(){},
  _buyInlineGen:0,_buyInlineES:null,_buyInlineTimeoutId:null,_buyInlineD:null,
  _buyInlineFetched:false,_buyInlineFetchInFlight:false,_buyInlineFailedShops:[],_currentCardName:'',
  _ga(){},_scrollPct(){return 0;},loadPacks(){return Promise.resolve();},renderWishlist(){},
  loadMetaTiers(){},loadMetaDeckPrices(){},renderSavedDecks(){},
  doSearch(opts={}){sandbox.closeSuggest();searches.push({name:get('q').value,buy:sandbox._pendingBuyInlineOpen,trigger:opts.trigger});},
  esc:s=>s,escAttr:s=>s,escJs:s=>s,loadSuggestThumbnails(){},
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname,"../../static/js/search-status.js"),"utf8"),sandbox);
vm.runInContext([
  block('const qInput=', '// 検索候補にカード画像'),
  block('function closeSuggest(', 'function renderAll('),
  block('function _updateUrl(', '// G-5:'),
  block('function switchMode(', '// ── Deck Builder'),
  block('function rankingLinkClick(', 'function searchCard('),
  block('function _resetBuyInline(', '// U-1:'),
].join('\n'),sandbox);

// 警告と買取通信を持った状態から戻る。実際の reset 処理で通信・状態が全て解除される。
const status=vm.runInContext('SearchStatus',sandbox);
const partial={status:'partial',shop_count:2,successful_shops:['店舗A'],failed_shops:['店舗B']};
status.show(partial);
assert.equal(get('searchCoverage').hidden,false);
assert.match(get('searchCoverage').querySelector('span').textContent,/取得失敗/);
let buyClosed=0;
sandbox._buyInlineES={close(){buyClosed++;}};
sandbox._buyInlineTimeoutId=sandbox.setTimeout(()=>{throw Error('買取タイマーが残っている');});
const buyTimer=sandbox._buyInlineTimeoutId;
sandbox._buyInlineD=[{price:100}];sandbox._buyInlineFetched=true;sandbox._buyInlineFetchInFlight=true;
sandbox._buyInlineFailedShops=['店舗B'];sandbox._currentCardName='前のカード';
get('buyInline').open=true;get('buyInlineBody').innerHTML='前の買取結果';
const buyGen=sandbox._buyInlineGen;
setUrl('/');listeners.popstate({state:null});
assert.equal(buyClosed,1);assert.equal(sandbox._buyInlineES,null);
assert.equal(sandbox._buyInlineGen,buyGen+1);
assert.equal(timers.has(buyTimer),false);assert.equal(sandbox._buyInlineTimeoutId,null);
assert.equal(sandbox._buyInlineD,null);assert.equal(sandbox._buyInlineFetched,false);
assert.equal(sandbox._buyInlineFetchInFlight,false);assert.equal(sandbox._buyInlineFailedShops.length,0);
assert.equal(sandbox._currentCardName,'');assert.equal(get('buyInline').open,false);
assert.equal(get('buyInlineBody').innerHTML,'');
assert.equal(get('searchCoverage').hidden,true);
assert.equal(get('searchCoverage').querySelector('span').textContent,'');

// 失敗から成功への再取得で注記だけが消え、元の説明 title は変更されない。
const row=element();row.setAttribute('title','既存のカード説明');
status.annotate(row,partial);
assert.equal(row.children.length,1);
assert.match(row.querySelector('.search-failure-note').textContent,/店舗B/);
assert.equal(row.querySelector('.search-failure-note').getAttribute('role'),'status');
status.annotate(row,{...partial,status:'failed',successful_shops:[]});
assert.equal(row.children.length,1);
assert.match(row.querySelector('.search-failure-note').textContent,/全店舗/);
assert.equal(row.title,'既存のカード説明');
status.annotate(row,{status:'ok',failed_shops:[]});
assert.equal(row.querySelector('.search-failure-note'),null);
assert.equal(row.children.length,0);
assert.equal(row.title,'既存のカード説明');assert.equal(row.getAttribute('title'),'既存のカード説明');

// タブから戻ったときは state が null でもホームを復元する。
sandbox.switchMode('packs','tab');assert.equal(location.hash,'#packs');
setUrl('/');listeners.popstate({state:null});
assert.equal(get('mode-search').classList.contains('hidden'),false);
assert.equal(get('results').classList.contains('hidden'),true);
assert.equal(get('empty').classList.contains('hidden'),false);
assert.equal(get('q').value,'');
setUrl('/#packs');listeners.popstate({state:null});
assert.equal(get('mode-packs').classList.contains('hidden'),false);
const before=history.length;
setUrl('/buy/'+encodeURIComponent('青眼の白龍'));listeners.popstate({state:null});
assert.deepEqual(searches.at(-1),{name:'青眼の白龍',buy:true,trigger:'popstate'});
assert.equal(history.length,before);
sandbox._updateUrl('青眼の白龍','buyback');assert.equal(history.length,before);
sandbox._updateUrl('ブラック・マジシャン','search');assert.equal(history.length,before+1);
setUrl('/card/'+encodeURIComponent('青眼の白龍'));listeners.popstate({state:null});
assert.equal(searches.at(-1).buy,false);
setUrl('/card/'+encodeURIComponent('青眼の白龍')+'#mydeck');sandbox._restoreLocation('pageload');
assert.equal(get('mode-mydeck').classList.contains('hidden'),false);
setUrl('/#mydeck-import');sandbox._restoreLocation('pageload');
assert.equal(get('deckImportDetails').open,true);
assert(get('deckPanes').classList.contains('show-tools'));
assert(canceled>0);

// 通常クリックは既存ハンドラ、修飾キー・中ボタンは実リンクの標準動作。
for(const flags of [{ctrlKey:true},{metaKey:true},{shiftKey:true},{altKey:true},{button:1}]){
  assert.equal(sandbox.rankingLinkClick({button:0,preventDefault(){throw Error('標準動作を奪った');},...flags}),false);
}
let prevented=false;assert(sandbox.rankingLinkClick({button:0,preventDefault(){prevented=true;}}));assert(prevented);
assert.equal((src.match(/<a class="movers-item" href=/g)||[]).length,4);
assert(!src.includes('<div class="movers-item" onclick='));

const flush=()=>new Promise(resolve=>setImmediate(resolve));
function input(value){get('q').value=value;sandbox.document.activeElement=get('q');get('q').listeners.input();}
function runTimers(){const pending=[...timers.values()];timers.clear();pending.forEach(fn=>fn());}
(async()=>{
  input('青眼');get('q').listeners.blur();runTimers();assert.equal(requests.length,0);
  input('青眼');runTimers();const older=requests.at(-1);
  input('青眼の');runTimers();const newer=requests.at(-1);
  newer.resolve([{name:'青眼の白龍'}]);await flush();assert(get('suggestDrop').classList.contains('open'));
  older.resolve([{name:'古い候補'}]);await flush();assert(!get('suggestDrop').innerHTML.includes('古い候補'));
  input('青眼');runTimers();const blurred=requests.at(-1);
  get('q').listeners.blur();blurred.resolve([{name:'遅い候補'}]);await flush();
  assert(!get('suggestDrop').classList.contains('open'));
  input('青眼');runTimers();const submitted=requests.at(-1);
  get('q').listeners.keydown({key:'Enter'});submitted.resolve([{name:'遅い候補'}]);await flush();
  assert(!get('suggestDrop').classList.contains('open'));
  input('青眼');runTimers();const failed=requests.at(-1);
  input('青眼の');runTimers();requests.at(-1).resolve([{name:'最新候補'}]);await flush();
  failed.reject(Error('古い通信エラー'));await flush();assert(get('suggestDrop').classList.contains('open'));

  // URL復元から実際の検索入口まで実行し、未発売判定を飛ばさないことを確認する。
  vm.runInContext(block('function doSearch(', '// ── Autocomplete'),sandbox);
  const unreleased=[];
  sandbox.showUnreleasedCard=name=>unreleased.push(name);
  for(const [trigger,prefix] of [['pageload','/card/'],['popstate','/buy/']]){
    const requestCount=requests.length;
    setUrl(prefix+encodeURIComponent('発売前カード'));
    sandbox._restoreLocation(trigger);
    assert.equal(requests.length,requestCount+1);
    assert.equal(requests.at(-1).url,'/api/validate?q='+encodeURIComponent('発売前カード'));
    requests.at(-1).resolve({valid:true,name:'発売前カード',unreleased:true});
    await flush();
    assert.equal(unreleased.at(-1),'発売前カード');
    assert.equal(requests.length,requestCount+1,'未発売カードの価格検索に進んでいる');
    assert.equal(get('btn').disabled,false);
  }
  assert.equal(unreleased.length,2);
  console.log('履歴・直リンク・候補競合・買取リセット・警告解除・未発売判定・ランキング標準操作を確認');
})().catch(error=>{console.error(error);process.exitCode=1;});

