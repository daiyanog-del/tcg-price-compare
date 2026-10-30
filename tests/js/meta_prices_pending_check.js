// 環境デッキ「安い順」の価格取得: サーバーが pending（起動直後の計算中など）を返す間は失敗にせず待つ。
// 2026-10-30 デプロイ直後に開くと、2秒おき2回の再試行で諦めて「取得に失敗しました」に固定されていた。
'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../../static/js/index-main.js'),'utf8');
function block(text,start,end){const a=text.indexOf(start),b=text.indexOf(end,a+start.length);assert(a>=0&&b>a);return text.slice(a,b);}
const pending={decks:[],error:'計算中です。しばらくしてから再度お試しください',pending:true};
const failed={decks:[],error:'環境データを取得できませんでした',pending:false};
const ok={decks:[{name:'検証テーマ',total:1000,priced_count:3,missing_count:0}],error:null,pending:false};
function ctx(responses){
  // 待ち時間は実時間を使わず、setTimeout を即時に進め、Date.now を進めた分だけ足す
  let now=0,calls=0;const el={innerHTML:''};
  const tabs=['入賞傾向','安い順'].map(textContent=>({textContent,classList:{toggle(){}}}));
  const context={console,Date:{now:()=>now},
    setTimeout:(fn,ms)=>{now+=ms;Promise.resolve().then(fn);},
    document:{getElementById:()=>el,querySelectorAll:()=>tabs},
    fetch:async()=>{const r=responses[Math.min(calls,responses.length-1)];calls++;return {json:async()=>r};},
    esc:String,escAttr:String,escJs:String,safeUrl:String};
  vm.createContext(context);
  vm.runInContext(block(source,'let _metaLoaded=false;','async function loadMetaDeck('),context);
  vm.runInContext("_metaTiers=[{name:'検証テーマ',tier:1,share:10}];_metaSort='price';",context);
  return {context,el,get calls(){return calls;}};
}
(async()=>{
  // pending が数回続いた後に揃う → 価格が出る（以前は3回目で失敗に固定）
  const a=ctx([pending,pending,pending,pending,ok]);
  await a.context.loadMetaDeckPrices();
  assert.equal(a.calls,5);
  assert.equal(vm.runInContext('_metaPricesError',a.context),false);
  assert.match(a.el.innerHTML,/検証テーマ/);
  assert.doesNotMatch(a.el.innerHTML,/取得に失敗/);

  // 待っている間の2回目の呼び出しは同じ取得に相乗りする（並走しない）
  const b=ctx([pending,ok]);
  const p1=b.context.loadMetaDeckPrices(),p2=b.context.loadMetaDeckPrices();
  assert.equal(p1,p2);await p1;assert.equal(b.calls,2);

  // pending でも上限（90秒）を過ぎたら失敗を出す
  const c=ctx([pending]);
  await c.context.loadMetaDeckPrices();
  assert.equal(vm.runInContext('_metaPricesError',c.context),true);
  assert.match(c.el.innerHTML,/取得に失敗/);
  assert.ok(c.calls>=25&&c.calls<=40,`pending の再試行回数が想定外: ${c.calls}`);

  // pending でない失敗は従来どおり2回まで再試行して諦める
  const d=ctx([failed]);
  await d.context.loadMetaDeckPrices();
  assert.equal(d.calls,3);
  assert.match(d.el.innerHTML,/取得に失敗/);

  // 失敗の後に「安い順」を押し直すと取り直す（以前は再読込まで失敗のまま）
  vm.runInContext('fetch=async()=>({json:async()=>('+JSON.stringify(ok)+')});',d.context);
  vm.runInContext("switchMetaSort('price');",d.context);
  await vm.runInContext('_metaPricesLoadPromise',d.context);
  assert.equal(vm.runInContext('_metaPricesError',d.context),false);
  assert.match(d.el.innerHTML,/検証テーマ/);
  console.log('meta_prices_pending_check: OK');
})().catch(e=>{console.error(e);process.exit(1);});
