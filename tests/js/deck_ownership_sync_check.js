// 遅延した同期応答が、送信後に編集した所持枚数を巻き戻さないことを確認する。
'use strict';
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('static/shared/sync-client.js','utf8');
const ownershipSource=fs.readFileSync('static/mydeck/deck-ownership.js','utf8');
const key='cardprice_saved_decks';
const deck=n=>[{id:'d1',name:'試験',text:'3 A',main:[{name:'A',qty:3}],ex:[],updated:1,owned:{A:n}}];
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function fixture(){
  const data=new Map([[key,JSON.stringify(deck(1))],['cardprice_sync_state',JSON.stringify({sync_id:'s',decks_rev:1,wishlist_rev:1})]]);
  data.set('cardprice_deck_draft',JSON.stringify({...deck(1)[0],savedId:'d1'}));
  const timers=new Map(),requests=[],applied=[];let seq=0;
  const s={console,localStorage:{getItem:k=>data.get(k)||null,setItem:(k,v)=>data.set(k,v)},
    setTimeout:fn=>{timers.set(++seq,fn);return seq;},clearTimeout:id=>timers.delete(id),
    fetch:(url,opts)=>new Promise(resolve=>requests.push({url,body:JSON.parse(opts.body),resolve})),
    document:{readyState:'loading',addEventListener(){},getElementById:id=>id==='deckTextarea'?{value:'3 A'}:null},
    parseDeckSections:()=>({main:[{name:'A',qty:3}],ex:[]}),_currentSavedDeckId:'d1',
    savedDecksSet:items=>{data.set(key,JSON.stringify(items));s.SyncClient.onDecksSave(items);}};
  s.window=s;vm.createContext(s);vm.runInContext(ownershipSource,s);s.DeckOwnership.set({A:1});
  const apply=s.DeckOwnership.applyReceived;s.DeckOwnership.applyReceived=items=>{applied.push(items);apply(items);};
  vm.runInContext(source,s);
  return {s,data,requests,applied,timers,
    timer(){const [id,fn]=timers.entries().next().value;timers.delete(id);fn();},
    edit(n){s.DeckOwnership.set({A:n});data.set('cardprice_deck_draft',JSON.stringify({...deck(n)[0],savedId:'d1'}));s.savedDecksSet(deck(n));},
    reload(){vm.runInContext(source,s);},
    fail(i){requests[i].resolve({ok:false,status:503,json:async()=>({})});},
    owned(){return s.DeckOwnership.snapshot().A;},
    draft(){return JSON.parse(data.get('cardprice_deck_draft')).owned.A;},
    answer(i,body){requests[i].resolve({ok:true,status:200,json:async()=>body});},
    value(){return JSON.parse(data.get(key))[0].owned.A;}};
}
(async()=>{
  // ownedだけの変更が送信対象になり、送信待ちsnapshotは参照共有しない。
  const f=fixture();const first=deck(1);f.s.savedDecksSet(first);first[0].owned.A=99;f.timer();
  assert.equal(f.requests[0].body.items[0].owned.A,1);
  f.edit(2);
  const serverOnly={id:'d2',name:'他端末のデッキ',text:'B',main:[{name:'B',qty:1}],ex:[],updated:1};
  f.answer(0,{ok:true,status:'merged',rev:2,items:[...deck(1),serverOnly]});await tick();
  assert.equal(f.value(),2);assert.equal(f.applied.length,0,'旧応答を編集中UIへ適用した');
  f.timer();assert.equal(f.requests[1].body.items[0].owned.A,2);
  assert.equal(f.requests[1].body.base_rev,1,'未適用の合流revを採用するとサーバーだけのデッキが消える');
  f.answer(1,{ok:true,status:'merged',rev:3,items:[...deck(2),serverOnly]});await tick();
  assert.equal(f.value(),2);assert.equal(f.applied.length,1);
  assert.equal(JSON.parse(f.data.get(key))[1].id,'d2');
  // updatedも本文も同じでownedだけ違う場合も重複送信判定で落とさない。
  f.edit(0);f.timer();assert.equal(f.requests.length,3);assert.equal(f.requests[2].body.items[0].owned.A,0);
  f.answer(2,{ok:true,status:'applied',rev:4});await tick();

  const g=fixture();const pull=g.s.SyncClient.pullIfNeeded();g.edit(2);
  g.answer(0,{ok:true,decks:{rev:2,items:deck(1)},wishlist:{unchanged:true}});await pull;
  assert.equal(g.value(),2);assert.equal(g.owned(),2);assert.equal(g.draft(),2);assert.equal(g.applied.length,0);
  // pull開始前から保存待ちの場合も、比較snapshotが一致するだけで古い内容を適用しない。
  const h=fixture();h.edit(2);const pending=h.s.SyncClient.pullIfNeeded();
  h.answer(0,{ok:true,decks:{rev:2,items:deck(1)},wishlist:{unchanged:true}});await pending;
  assert.equal(h.value(),2);assert.equal(h.applied.length,0);
  // 編集のない通常pullは所持枚数をUIへ反映する。
  const normal=fixture();const ready=normal.s.SyncClient.pullIfNeeded();
  normal.answer(0,{ok:true,decks:{rev:2,items:deck(0)},wishlist:{unchanged:true}});await ready;
  assert.equal(normal.value(),0);assert.equal(normal.applied.length,1);
  // リトライの待機中も、リクエスト数が0になっただけではACK済みにならない。
  const backoff=fixture();backoff.edit(2);backoff.timer();backoff.fail(0);await tick();
  const duringBackoff=backoff.s.SyncClient.pullIfNeeded();
  assert.equal(backoff.requests[1].url,'/api/sync/pull');
  backoff.answer(1,{ok:true,decks:{rev:2,items:deck(1)},wishlist:{unchanged:true}});await duringBackoff;
  assert.equal(backoff.value(),2);assert.equal(backoff.owned(),2);assert.equal(backoff.draft(),2);
  // 失敗後の未ACK状態は永続化し、再読込後も古いpullで所持/下書きを戻さない。
  const failed=fixture();failed.edit(2);failed.timer();
  for(let i=0;i<3;i++){
    failed.fail(i);await tick();
    if(i<2){failed.timer();await tick();}
  }
  assert.equal(JSON.parse(failed.data.get('cardprice_sync_state')).decks_dirty,true);
  failed.reload();const recovered=failed.s.SyncClient.pullIfNeeded();
  assert.equal(failed.requests[3].url,'/api/sync/push');
  assert.equal(failed.requests[3].body.items[0].owned.A,2);
  failed.answer(4,{ok:true,decks:{rev:2,items:deck(1)},wishlist:{unchanged:true}});await recovered;
  assert.equal(failed.value(),2);assert.equal(failed.owned(),2);assert.equal(failed.draft(),2);
  failed.answer(3,{ok:true,status:'merged',rev:3,items:deck(2)});await tick();
  assert(!JSON.parse(failed.data.get('cardprice_sync_state')).decks_dirty);
  const normalAfterAck=failed.s.SyncClient.pullIfNeeded();
  failed.answer(5,{ok:true,decks:{rev:4,items:deck(0)},wishlist:{unchanged:true}});await normalAfterAck;
  assert.equal(failed.value(),0);assert.equal(failed.owned(),0);assert.equal(failed.draft(),0);
  // 送信失敗後の解除はサーバーの旧データを複製するため、未ACKを新IDへ追送する。
  const unlinkFailed=fixture();unlinkFailed.edit(2);unlinkFailed.timer();
  for(let i=0;i<3;i++){unlinkFailed.fail(i);await tick();if(i<2){unlinkFailed.timer();await tick();}}
  const unlink=unlinkFailed.s.SyncClient.unlinkThisDevice();
  unlinkFailed.answer(3,{ok:true,sync_id:'new',decks_rev:1,wishlist_rev:1});await unlink;
  assert.equal(unlinkFailed.requests[4].body.sync_id,'new');
  assert.equal(unlinkFailed.requests[4].body.base_rev,0,'複製先だけのデッキを通常置換で消さない');
  assert.equal(unlinkFailed.requests[4].body.items[0].owned.A,2);
  assert.equal(JSON.parse(unlinkFailed.data.get('cardprice_sync_state')).decks_dirty,true);
  unlinkFailed.reload();const afterUnlink=unlinkFailed.s.SyncClient.pullIfNeeded();
  assert.equal(unlinkFailed.requests[5].body.sync_id,'new');
  unlinkFailed.answer(6,{ok:true,decks:{rev:2,items:deck(1)},wishlist:{unchanged:true}});await afterUnlink;
  assert.equal(unlinkFailed.value(),2);assert.equal(unlinkFailed.owned(),2);assert.equal(unlinkFailed.draft(),2);
  unlinkFailed.answer(5,{ok:true,status:'applied',rev:3});await tick();
  assert(!JSON.parse(unlinkFailed.data.get('cardprice_sync_state')).decks_dirty);
  // 解除リクエスト開始時には未編集でも、応答待ち中の編集を新IDへ引き継ぐ。
  const unlinkEditing=fixture();const unlinkWait=unlinkEditing.s.SyncClient.unlinkThisDevice();
  unlinkEditing.edit(3);unlinkEditing.answer(0,{ok:true,sync_id:'new',decks_rev:1,wishlist_rev:1});await unlinkWait;
  assert.equal(unlinkEditing.requests[1].body.sync_id,'new');assert.equal(unlinkEditing.requests[1].body.items[0].owned.A,3);
  // 旧IDの遅延ACKが新IDのdirtyやrevを変更してはいけない。
  const oldAck=fixture();oldAck.edit(2);oldAck.timer();const unlinkDuringPush=oldAck.s.SyncClient.unlinkThisDevice();
  oldAck.answer(1,{ok:true,sync_id:'new',decks_rev:1,wishlist_rev:1});await unlinkDuringPush;
  oldAck.answer(0,{ok:true,status:'applied',rev:9});await tick();
  const detached=JSON.parse(oldAck.data.get('cardprice_sync_state'));
  assert.equal(detached.sync_id,'new');assert.equal(detached.decks_rev,0);assert.equal(detached.decks_dirty,true);
  // QR引換の待機中の編集はローカル/UI/下書きを維持し、新IDへ競合合流する。
  const qrEditing=fixture();const redeem=qrEditing.s.SyncClient.redeemLink('token');qrEditing.edit(2);
  qrEditing.answer(0,{ok:true,sync_id:'joined',decks:{rev:5,items:[...deck(1),serverOnly]},wishlist:{rev:1,items:[]}});await redeem;
  assert.equal(qrEditing.value(),2);assert.equal(qrEditing.owned(),2);assert.equal(qrEditing.draft(),2);
  assert.equal(qrEditing.requests[1].body.sync_id,'joined');assert.equal(qrEditing.requests[1].body.base_rev,0);
  assert.equal(qrEditing.requests[1].body.items[0].owned.A,2);
  qrEditing.answer(1,{ok:true,status:'merged',rev:6,items:[...deck(2),serverOnly]});await tick();
  assert.equal(JSON.parse(qrEditing.data.get(key))[1].id,'d2');
  // 編集のない引換も、保存リストと編集中UIと下書きを同じ所持情報へ揃える。
  const qrNormal=fixture();qrNormal.edit(1);const qrDone=qrNormal.s.SyncClient.redeemLink('token');
  qrNormal.answer(0,{ok:true,sync_id:'joined',decks:{rev:5,items:[...deck(0),serverOnly]},wishlist:{rev:1,items:[]}});await qrDone;
  assert.equal(qrNormal.value(),0);assert.equal(qrNormal.owned(),0);assert.equal(qrNormal.draft(),0);
  assert.equal(qrNormal.timers.size,0,'合流前の送信予約は新デッキを削除し得る');
  // /syncページには編集UI・保存関数がない。それでも次回ホームの下書きは更新する。
  const qrPage=fixture();delete qrPage.s.DeckOwnership;delete qrPage.s.savedDecksSet;
  const qrPageDone=qrPage.s.SyncClient.redeemLink('token');
  qrPage.answer(0,{ok:true,sync_id:'joined',decks:{rev:5,items:deck(0)},wishlist:{rev:1,items:[]}});await qrPageDone;
  assert.equal(qrPage.value(),0);assert.equal(qrPage.draft(),0);
  console.log('所持枚数同期: snapshot・owned単独変更・遅延push/pull・通常反映を確認');
})().catch(e=>{console.error(e);process.exitCode=1;});
