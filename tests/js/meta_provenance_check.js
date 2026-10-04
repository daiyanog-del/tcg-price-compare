// 実UIコードで新旧API・取得条件表示・更新失敗後の再試行を検証する。
'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../../static/js/index-main.js'),'utf8');
const solitaire=fs.readFileSync(path.join(__dirname,'../../static/solitaire/js/ui/deck-input-panel.js'),'utf8').replace(/\r/g,'');
const tiers=[{name:'検証テーマ',tier:1,share:10}];
const metadata={from:'2026-08-05',to:'2026-10-04',fetched_at:'2026-10-04T12:00:00+09:00',sample_size:null,stale:false,refresh_error:null};
function block(text,start,end){const a=text.indexOf(start),b=text.indexOf(end,a+start.length);assert(a>=0&&b>a);return text.slice(a,b);}
function indexContext(payload,storage=new Map()){
  const el={innerHTML:''};let calls=0;
  const tabs=['入賞傾向','安い順'].map(textContent=>({textContent,classList:{toggle(key,value){this[key]=value;}}}));
  const context={console,document:{getElementById:()=>el,querySelectorAll:()=>tabs},
    sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
    fetch:async()=>{calls++;return {ok:true,json:async()=>payload};},
    esc:value=>String(value).replaceAll('<','&lt;'),escAttr:value=>value,escJs:value=>value,safeUrl:value=>value};
  vm.createContext(context);
  vm.runInContext(block(source,'let _metaLoaded=false;','async function loadMetaDeck('),context);
  return {context,el,storage,tabs,get calls(){return calls;}};
}
function solitaireContext(payload){
  const el={dataset:{},innerHTML:'',children:[],appendChild(child){this.children.push(child);}};let calls=0;
  const context={API_META:'/api/meta',document:{getElementById:()=>el,createElement:()=>({addEventListener(){}})},
    fetch:async()=>{calls++;return {ok:true,json:async()=>payload};},_esc:String};
  vm.createContext(context);
  vm.runInContext(block(solitaire,'async function loadMetaTierList()', '/**\n * マイデッキ一覧').replace(/\r/g,''),context);
  return {context,el,get calls(){return calls;}};
}
(async()=>{
  const fresh=indexContext({tiers,metadata});
  await fresh.context.loadMetaTiers();
  assert.match(fresh.el.innerHTML,/2026-08-05〜2026-10-04/);
  assert.match(fresh.el.innerHTML,/データ取得: 2026-10-04T12:00:00/);
  assert.match(fresh.el.innerHTML,/母数不明/);
  assert.doesNotMatch(fresh.el.innerHTML,/直近1ヶ月/);
  assert.equal(JSON.parse(fresh.storage.get('tcgym_meta_cache_v2')).data.metadata.from,metadata.from);
  const restored=indexContext(null,fresh.storage);await restored.context.loadMetaTiers();
  assert.equal(restored.calls,0);assert.match(restored.el.innerHTML,/2026-08-05/);
  vm.runInContext("loadMetaDeckPrices=()=>{}; switchMetaSort('tier');",fresh.context);
  assert.equal(fresh.tabs[0].classList.active,true);
  vm.runInContext("_metaSort='price';_metaPriceMap={'検証テーマ':{total:100,priced_count:1,missing_count:0}};renderMetaTiers();",fresh.context);
  assert.match(fresh.el.innerHTML,/2026-08-05〜2026-10-04/);
  const old=indexContext(tiers,new Map([['tcgym_meta_cache',JSON.stringify({ts:Date.now(),data:tiers})]]));
  await old.context.loadMetaTiers();assert.equal(old.calls,1);
  assert.match(old.el.innerHTML,/集計期間不明/);assert.match(old.el.innerHTML,/取得日時不明/);
  const stalePayload={tiers,metadata:{...metadata,stale:true,refresh_error:'fetch_failed'}};
  const stale=indexContext(stalePayload);
  await stale.context.loadMetaTiers();assert.match(stale.el.innerHTML,/以前取得したデータ/);
  assert.equal(stale.storage.has('tcgym_meta_cache_v2'),false);
  await stale.context.loadMetaTiers();assert.equal(stale.calls,2);
  const failed=indexContext({tiers:[],metadata:{refresh_error:'fetch_failed'}});
  await failed.context.loadMetaTiers();assert.match(failed.el.innerHTML,/取得に失敗/);
  assert.equal(failed.storage.has('tcgym_meta_cache_v2'),false);
  for(const payload of [tiers,{tiers,metadata},stalePayload]){
    const game=solitaireContext(payload);await game.context.loadMetaTierList();
    assert.match(game.el.children[0].innerHTML,/検証テーマ/);
    await game.context.loadMetaTierList();
    assert.equal(game.calls,payload===stalePayload?2:1);
  }
  console.log('環境データ: 新旧API、期間・取得日時・不明表記、両画面の再取得を確認');
})().catch(error=>{console.error(error);process.exitCode=1;});
