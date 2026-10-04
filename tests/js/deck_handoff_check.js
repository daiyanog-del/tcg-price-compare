// 実モジュールとmain.jsの起動分岐で、一度だけの持込と失敗時の再試行を検証する。
'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert/strict');
const root=path.join(__dirname,'../..');
function element(tag='div'){
  return {tag,children:[],style:{},attributes:{},listeners:{},textContent:'',hidden:false,
    setAttribute(k,v){this.attributes[k]=v;},append(...values){this.children.push(...values);},
    prepend(child){this.children.unshift(child);},addEventListener(k,fn){this.listeners[k]=fn;},
    set innerHTML(value){throw Error('HTMLとして解釈されています: '+value);}};
}
function fixture(){
  const values=new Map(),calls=[],body=element('body'),loc={href:'https://example.test/solitaire?from=mydeck'};
  const storage={getItem:k=>values.get(k)??null,setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k)};
  const hist={state:{test:1},replaceState(state,unused,url){calls.push(['url',url]);loc.href=new URL(url,loc.href).href;}};
  const doc={body,createElement:element};
  let counts={main:0,ex:0};
  return {values,calls,body,loc,storage,hist,doc,
    getCounts:()=>counts,
    async clearDeck(){calls.push(['clear']);counts={main:0,ex:0};return true;},
    async loadDeck(main,ex){calls.push(['load',main,ex]);counts={main:2,ex:1};},
  };
}
(async()=>{
  const source=fs.readFileSync(path.join(root,'static/shared/deck-handoff.js'),'utf8');
  const {HANDOFF_KEY,validateHandoff,storeHandoff,receiveDeckHandoff}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
  const deck={version:1,name:'試験 <img src=x onerror=alert(1)>',main:[{name:'青眼の白龍',qty:2}],ex:[{name:'青眼の究極竜',qty:1}]};
  const f=fixture();
  f.values.set('cardprice_saved_decks','保存デッキは変更禁止');
  storeHandoff(deck,f.storage);
  const snapshot=f.storage.getItem(HANDOFF_KEY);
  assert.equal(await receiveDeckHandoff(f),true);
  assert.deepEqual(f.calls.slice(0,2),[['clear'],['load','2 青眼の白龍','1 青眼の究極竜']]);
  assert.equal(f.storage.getItem(HANDOFF_KEY),null);
  assert.equal(f.loc.href,'https://example.test/solitaire');
  assert(f.body.children[0].children[0].textContent.includes(deck.name));
  assert.equal(f.storage.getItem('cardprice_saved_decks'),'保存デッキは変更禁止');
  assert.equal(await receiveDeckHandoff(f),false,'消費したURLで再び取り込んだ');

  // 境界値と不正値。巨大入力はJSON解析前に拒否し、盤面に触らない。
  validateHandoff({...deck,main:[{name:'青眼',qty:60}],ex:[{name:'究極竜',qty:15}]});
  for(const bad of [null,{...deck,version:2},{...deck,name:'名'.repeat(201)},
    {...deck,main:[],ex:[]},{...deck,main:[{name:'青眼',qty:61}]},
    {...deck,ex:[{name:'究極竜',qty:16}]},{...deck,main:[{name:'青眼',qty:0}]},
    {...deck,main:[{name:'青眼',qty:1.5}]},{...deck,main:[{name:'青眼',qty:'2'}]},
    {...deck,main:[{name:'青眼\n3 別カード',qty:1}]},{...deck,main:[{name:'名'.repeat(51),qty:1}]}]){
    assert.throws(()=>validateHandoff(bad));
  }
  for(const raw of ['x'.repeat(12001),'{broken',null]){
    const invalid=fixture();if(raw!==null)invalid.storage.setItem(HANDOFF_KEY,raw);
    assert.equal(await receiveDeckHandoff(invalid),true);
    assert.equal(invalid.calls.length,0);
    assert.equal(invalid.body.children[0].children[1].hidden,false);
  }
  assert.throws(()=>storeHandoff(deck,{setItem(){throw Error('保存不可');}}),/保存不可/);

  // 部分取得で正常resolveしても、期待枚数に満たなければデータとURLを残す。
  const partial=fixture();partial.storage.setItem(HANDOFF_KEY,snapshot);
  let tries=0;
  const originalLoad=partial.loadDeck;
  partial.loadDeck=async(...args)=>{tries++;if(tries>1)await originalLoad(...args);};
  await receiveDeckHandoff(partial);
  assert.equal(partial.storage.getItem(HANDOFF_KEY),snapshot);
  assert(partial.loc.href.endsWith('?from=mydeck'));
  const panel=partial.body.children[0];
  assert.match(panel.children[0].textContent,/一部のカード/);
  assert.equal(panel.children[1].hidden,false);
  await panel.children[1].listeners.click();
  assert.equal(tries,2);assert.equal(partial.calls.filter(c=>c[0]==='clear').length,2);
  assert.equal(partial.storage.getItem(HANDOFF_KEY),null);
  assert.equal(panel.children[1].hidden,true);

  // main.jsの実際の分岐を抽出し、持込・失敗・リプレイ・通常復元の優先順を検証する。
  const main=fs.readFileSync(path.join(root,'static/solitaire/js/main.js'),'utf8');
  const start=main.indexOf('  const hasReplayInUrl ='),end=main.indexOf('  if (restored)',start);
  assert(start>=0&&end>start);
  async function boot(url,fail=false){
    const env=fixture();env.loc.href=url;env.storage.setItem(HANDOFF_KEY,snapshot);
    const parsed=new URL(url);let resumed=0;
    const context={
      location:{search:parsed.search,hash:parsed.hash},
      loadDeckFromNeuron:fail?async()=>{throw Error('通信失敗');}:env.loadDeck,
      clearEverything:env.clearDeck,
      document:{querySelectorAll:selector=>({length:env.getCounts()[selector.includes('poolRow2')?'ex':'main']})},
      receiveDeckHandoff:args=>receiveDeckHandoff({...env,...args}),
      loadSessionResume:async()=>{resumed++;return true;},
    };
    const restored=await vm.runInNewContext('(async()=>{'+main.slice(start,end)+'return restored;})()',context);
    return {env,resumed,restored};
  }
  const successful=await boot('https://example.test/solitaire?from=mydeck');
  assert.equal(successful.resumed,0);assert.equal(successful.restored,false);
  const failed=await boot('https://example.test/solitaire?from=mydeck',true);
  assert.equal(failed.resumed,0);assert.equal(failed.env.storage.getItem(HANDOFF_KEY),snapshot);
  for(const url of ['https://example.test/solitaire?from=mydeck&replay=abc','https://example.test/solitaire?from=mydeck#replay=abc']){
    const replay=await boot(url);assert.equal(replay.resumed,0);assert.equal(replay.env.calls.length,0);
    assert.equal(replay.env.storage.getItem(HANDOFF_KEY),snapshot);
  }
  const normal=await boot('https://example.test/solitaire');
  assert.equal(normal.resumed,1);assert.equal(normal.restored,true);assert.equal(normal.env.calls.length,0);
  console.log('デッキ引継ぎ: 上限・安全な表示・分離・成功消費・失敗再試行・リプレイ優先・旧盤面抑止を確認');
})().catch(error=>{console.error(error);process.exitCode=1;});
