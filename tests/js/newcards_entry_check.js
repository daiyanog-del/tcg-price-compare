// ?newcards=1&focus= の受け口（static/shared/newcards-entry.js）と main.js の起動分岐を検証する。
'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert/strict');
const root=path.join(__dirname,'../..');

function fixture(href,{newCards=['新A','新B'],resolvable={}}={}){
  const calls=[],loc={href};
  const hist={state:{t:1},replaceState(state,unused,url){calls.push(['url',url]);loc.href=new URL(url,loc.href).href;}};
  const fetched=[];
  const fetchFn=async(url,init)=>{
    fetched.push(url);
    if(url==='/api/new-cards')return newCards===null?{ok:false}:{ok:true,json:async()=>({cards:newCards.map(name=>({name})),has_more:false})};
    if(url==='/api/card-images'){
      const name=JSON.parse(init.body).names[0];
      return {ok:true,json:async()=>({images:{[name]:resolvable[name]??null}})};
    }
    throw Error('想定外のfetch '+url);
  };
  const classes=new Set();let scrolled=0;
  const wrapper={classList:{add:c=>classes.add(c)},scrollIntoView(o){scrolled++;wrapper.scrollOpts=o;}};
  let loaded=[];
  const handlers={},bodyClasses=new Set();
  const doc={body:{classList:{add:c=>bodyClasses.add(c),remove:c=>bodyClasses.delete(c)}},querySelectorAll:sel=>sel.startsWith('.')?(classes.has(sel.slice(1))?[wrapper]:[]):loaded.map(name=>({dataset:{cardName:name},closest:()=>wrapper})),
    addEventListener:(t,fn)=>{(handlers[t]??=new Set()).add(fn);},
    removeEventListener:(t,fn)=>{handlers[t]?.delete(fn);},
    emit:(t,ev={})=>[...(handlers[t]??[])].forEach(fn=>fn(ev))};
  wrapper.classList.remove=c=>classes.delete(c);
  const shown=[],notices=[];
  return {bodyClasses,notices,calls,loc,hist,fetched,fetchFn,classes,doc,shown,wrapper,get scrolled(){return scrolled;},
    async clearDeck(){calls.push(['clear']);return true;},
    async loadDeck(main,ex){calls.push(['load',main,ex]);loaded=main.split('\n').map(l=>l.slice(2));},
    showCard:el=>shown.push(el.dataset.cardName)};
}
const run=(mod,f,extra={})=>mod.receiveNewCards({loadDeck:f.loadDeck,clearDeck:f.clearDeck,showCard:f.showCard,notify:(m,o)=>f.notices.push([m,o]),fetchFn:f.fetchFn,doc:f.doc,loc:f.loc,hist:f.hist,...extra});

(async()=>{
  const source=fs.readFileSync(path.join(root,'static/shared/newcards-entry.js'),'utf8');
  const mod=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));

  // focus の検証（50字上限・改行・空・型）
  assert.equal(mod.validateFocus('青眼の白龍'),'青眼の白龍');
  assert.equal(mod.validateFocus('あ'.repeat(50)),'あ'.repeat(50));
  for(const bad of ['あ'.repeat(51),'a\nb','a\rb','  ','',null,undefined,5])assert.equal(mod.validateFocus(bad),null);

  // URLパース: 指定なし／newcards!=1／リプレイ・端末保存・マイデッキ持込があれば無視
  const req=u=>mod.parseNewCardsRequest(new URL(u));
  assert.equal(req('https://e.test/solitaire'),null);
  assert.equal(req('https://e.test/solitaire?newcards=0'),null);
  assert.deepEqual(req('https://e.test/solitaire?newcards=1'),{focus:null});
  assert.deepEqual(req('https://e.test/solitaire?newcards=1&focus='+encodeURIComponent('A&B#C?D/E')),{focus:'A&B#C?D/E'});
  assert.deepEqual(req('https://e.test/solitaire?newcards=1&focus='+encodeURIComponent('あ'.repeat(51))),{focus:null});
  for(const u of ['?newcards=1&replay=abc','?newcards=1&saved=r_1','?newcards=1&from=mydeck','?newcards=1#replay=abc'])assert.equal(req('https://e.test/solitaire'+u),null,u);

  // 新着に含まれる focus: 解決確認の追加fetchなし・強調・スクロール・詳細・URLから消える
  let f=fixture('https://e.test/solitaire?newcards=1&focus='+encodeURIComponent('新B')+'&x=1#h');
  assert.equal(await run(mod,f),true);
  assert.deepEqual(f.calls,[['clear'],['load','1 新A\n1 新B',''],['url','/solitaire?x=1#h']]);
  assert.deepEqual(f.fetched,['/api/new-cards']);
  assert(f.classes.has(mod.FOCUS_CLASS));assert.equal(f.scrolled,1);assert.deepEqual(f.shown,['新B']);
  assert.equal(await run(mod,f),false,'URLから消えた後に再度読み込んだ');

  // 新着に無い focus: 解決できれば先頭に追加（重複なし）
  f=fixture('https://e.test/solitaire?newcards=1&focus='+encodeURIComponent('既知X'),{resolvable:{既知X:'https://img/x.jpg'}});
  assert.equal(await run(mod,f),true);
  assert.equal(f.calls[1][1],'1 既知X\n1 新A\n1 新B');
  assert.deepEqual(f.shown,['既知X']);

  // 解決できない focus（null / kind:none）は読み込まず強調もしない。新着だけは読む
  for(const val of [null,{kind:'none'}]){
    f=fixture('https://e.test/solitaire?newcards=1&focus='+encodeURIComponent('不明'),{resolvable:{不明:val}});
    assert.equal(await run(mod,f),true);
    assert.equal(f.calls[1][1],'1 新A\n1 新B');
    assert.equal(f.classes.size,0);assert.equal(f.shown.length,0);
    assert.equal(f.notices.length,1);assert.deepEqual(f.notices[0][1],{afterLoad:true});
  }

  // 新着0件＋focus解決不可 → 何もしない（盤面を消さず、通常起動＝前回盤面の復元に任せる）
  f=fixture('https://e.test/solitaire?newcards=1&focus='+encodeURIComponent('不明'),{newCards:[]});
  assert.equal(await run(mod,f),false);assert.equal(f.calls.length,0);assert(f.loc.href.includes('newcards=1'));
  assert.equal(f.notices.length,1);assert.match(f.notices[0][0],/まだありません|読み込めませんでした/);
  // new-cards 取得失敗（HTTPエラー）は通知して通常起動。盤面を消さず、パラメータも残す
  f=fixture('https://e.test/solitaire?newcards=1&focus='+encodeURIComponent('不明'),{newCards:null,resolvable:{不明:'u'}});
  assert.equal(await run(mod,f),false);assert.equal(f.calls.length,0);assert(f.loc.href.includes('newcards=1'));
  assert.match(f.notices[0][0],/取得できませんでした/);
  // タイムアウト: 応答が来ない fetch は AbortController で打ち切り、同じく通常起動
  f=fixture('https://e.test/solitaire?newcards=1');
  f.fetchFn=(url,init)=>new Promise((_,reject)=>init.signal.addEventListener('abort',()=>reject(Error('abort'))));
  assert.equal(await run(mod,f,{timeoutMs:20}),false);assert.equal(f.calls.length,0);assert(f.loc.href.includes('newcards=1'));
  assert.equal(f.notices.length,1);
  // 取得待ちの間に別のデッキ読込が始まったら中止（clearDeck しない・盤面を消さない）
  f=fixture('https://e.test/solitaire?newcards=1');
  const inner=f.fetchFn;f.fetchFn=async(u,i)=>{f.doc.emit('sol-deck-load-start');return inner(u,i);};
  assert.equal(await run(mod,f),false);assert.equal(f.calls.length,0);assert(f.loc.href.includes('newcards=1'));
  // 同名の重複は1枚にまとめる（focus との重複も）
  f=fixture('https://e.test/solitaire?newcards=1&focus='+encodeURIComponent('新A'),{newCards:['新A','新B','新A','新B']});
  await run(mod,f);assert.equal(f.calls[1][1],'1 新A\n1 新B');
  // 強調は最初のカード操作・別デッキ読込・盤面消去・ドロー/リセットで全カードから外れる
  for(const [type,ev] of [['click',{target:{closest:()=>true}}],['dragstart',{target:{closest:()=>true}},],['sol-deck-load-start'],['sol-board-cleared'],['sol-replay-reset']]){
    f=fixture('https://e.test/solitaire?newcards=1&focus='+encodeURIComponent('新A'));
    await run(mod,f);assert(f.classes.has(mod.FOCUS_CLASS),type);
    assert(f.bodyClasses.has(mod.ACTIVE_CLASS),'強調中は body に active が付く');
    f.doc.emit(type,ev);assert(!f.classes.has(mod.FOCUS_CLASS),type+' で強調が残った');
    assert(!f.bodyClasses.has(mod.ACTIVE_CLASS),type+' で body の active が残った');
  }
  // スマホのデッキシート: カード選択・操作ボタンで外れる。閉じる・検索・シートの開閉では外れない
  const sel=['.sol-mobile-deck-cell','#solMobileDeckSheet .sol-mobile-deck-primary-btn','#solMobileDeckSheet .sol-mobile-menu-item'];
  for(const target of sel){
    f=fixture('https://e.test/solitaire?newcards=1&focus='+encodeURIComponent('新A'));
    await run(mod,f);
    f.doc.emit('click',{target:{closest:q=>q.includes(target)?{}:null}});
    assert(!f.classes.has(mod.FOCUS_CLASS)&&!f.bodyClasses.has(mod.ACTIVE_CLASS),target);
  }
  f=fixture('https://e.test/solitaire?newcards=1&focus='+encodeURIComponent('新A'));
  await run(mod,f);
  f.doc.emit('click',{target:{closest:q=>(q.includes('.sol-mobile-sheet-close-btn')||q.includes('#solMobileCipClose'))?{}:null}});
  assert(f.classes.has(mod.FOCUS_CLASS)&&f.bodyClasses.has(mod.ACTIVE_CLASS),'閉じる操作では外さない');
  // pointerdown では外さない（前面表示が消えて隣のカードを掴むのを防ぐ）
  f=fixture('https://e.test/solitaire?newcards=1&focus='+encodeURIComponent('新A'));
  await run(mod,f);f.doc.emit('pointerdown',{target:{closest:()=>true}});assert(f.classes.has(mod.FOCUS_CLASS));
  // 操作と無関係なクリックでは外れない
  f=fixture('https://e.test/solitaire?newcards=1&focus='+encodeURIComponent('新A'));
  await run(mod,f);f.doc.emit('click',{target:{closest:()=>null}});assert(f.classes.has(mod.FOCUS_CLASS));
  // 新着0件でも focus が解決できれば読む
  f=fixture('https://e.test/solitaire?newcards=1&focus='+encodeURIComponent('不明'),{newCards:[],resolvable:{不明:'u'}});
  assert.equal(await run(mod,f),true);

  // リプレイ指定があれば何も取得しない
  f=fixture('https://e.test/solitaire?newcards=1&replay=abc');
  assert.equal(await run(mod,f),false);assert.equal(f.fetched.length,0);assert.equal(f.calls.length,0);

  // focus 不正値は無視して新着だけ読む（強調なし）
  f=fixture('https://e.test/solitaire?newcards=1&focus='+encodeURIComponent('あ'.repeat(51)));
  assert.equal(await run(mod,f),true);assert.equal(f.classes.size,0);assert.deepEqual(f.fetched,['/api/new-cards']);

  // 読込失敗: 例外でも true（古い盤面を復元しない）、URLは残して再読込で再試行できる
  f=fixture('https://e.test/solitaire?newcards=1');
  f.loadDeck=async()=>{throw Error('通信失敗');};
  assert.equal(await run(mod,f),true);assert(f.loc.href.includes('newcards=1'));
  assert.deepEqual(f.notices.map(n=>n[1]),[{afterLoad:true}]);
  // 読込中で clearDeck が false
  f=fixture('https://e.test/solitaire?newcards=1');
  f.clearDeck=async()=>false;
  assert.equal(await run(mod,f),true);

  // 新着名に不正な名前（改行・51字）が混じっても読み込み文に入れない
  f=fixture('https://e.test/solitaire?newcards=1',{newCards:['正常','悪い\n9 別','あ'.repeat(51)]});
  await run(mod,f);assert.equal(f.calls[1][1],'1 正常');

  // main.js の配線: 優先順位（持込 > リプレイ > newcards > 前回盤面復元）
  const main=fs.readFileSync(path.join(root,'static/solitaire/js/main.js'),'utf8');
  const start=main.indexOf('  const newCardsLoaded ='),end=main.indexOf('  if (restored)',start);
  assert(start>=0&&end>start);
  async function boot(hasReplayInUrl,handedOff,newCardsResult){
    let resumed=0,newCardsCalls=0;
    const context={hasReplayInUrl,handedOff,loadDeckFromNeuron(){},clearEverything(){},showCardByName(){},openCardDetailByName(){},isMobilePortrait:()=>false,
      receiveNewCards:async()=>{newCardsCalls++;return newCardsResult;},
      loadSessionResume:async()=>{resumed++;return true;}};
    const restored=await vm.runInNewContext('(async()=>{'+main.slice(start,end)+'return restored;})()',context);
    return {resumed,newCardsCalls,restored};
  }
  let r=await boot(false,false,true);assert.deepEqual(r,{resumed:0,newCardsCalls:1,restored:false});
  r=await boot(false,false,false);assert.deepEqual(r,{resumed:1,newCardsCalls:1,restored:true});
  r=await boot(true,false,true);assert.deepEqual(r,{resumed:0,newCardsCalls:0,restored:false});
  r=await boot(false,true,true);assert.deepEqual(r,{resumed:0,newCardsCalls:0,restored:false});
  // 詳細の呼び分け: スマホ縦は既存のスマホ用詳細UI、PCは左パネル
  for(const [mobile,expected] of [[true,'mobile'],[false,'pc']]){
    const seen=[];
    const el={dataset:{cardName:'新A'},src:'u.jpg',closest:()=>null};
    const ctx={hasReplayInUrl:false,handedOff:false,loadDeckFromNeuron(){},clearEverything(){},isMobilePortrait:()=>mobile,
      openCardDetailByName:(n,s)=>seen.push(['mobile',n,s]),showCardByName:(n,s,k)=>seen.push(['pc',n,s,k]),
      receiveNewCards:async a=>{a.showCard(el);return true;},loadSessionResume:async()=>true};
    await vm.runInNewContext('(async()=>{'+main.slice(start,end)+'})()',ctx);
    assert.equal(seen.length,1);assert.equal(seen[0][0],expected);assert.equal(seen[0][1],'新A');
  }
  // mobile-ui.js: デッキシートへの強調の引き継ぎ・選択時の解除・表示後スクロール（ソースの配線確認。実挙動は Playwright で確認）
  const mui=fs.readFileSync(path.join(root,'static/solitaire/js/ui/mobile-ui.js'),'utf8');
  assert(/export function openCardDetailByName/.test(mui));
  assert(/wrapper\.classList\.contains\('sol-newcard-focus'\)\) cell\.classList\.add\('sol-newcard-focus'\)/.test(mui));
  assert(!/_toggleDeckCardSelection\(cell\) \{[\s\S]{0,200}sol-newcard-focus/.test(mui),'選択で強調を外さない（選択とは別の目印）');
  assert(/setHidden\('solMobileDeckSheet', false\);[\s\S]{0,120}scrollIntoView/.test(mui));
  console.log('新着直行: URLパース・focus検証・優先順位・URL除去・解決不可focus・失敗時・起動分岐を確認');
})().catch(error=>{console.error(error);process.exitCode=1;});
