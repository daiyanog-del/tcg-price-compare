// トップの /?addToDeck= の受け口（static/shared/add-to-deck-entry.js）の検証。
// 2026-10-20 シミュレーター側の案内（mydeck-hint.js）はデッキ編集（deck-edit-ui.js）に置き換えて削除した。受け口はトップに残る。
'use strict';
const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const root=path.join(__dirname,'../..');
const load=async file=>{
  let src=fs.readFileSync(path.join(root,file),'utf8');
  return import('data:text/javascript;base64,'+Buffer.from(src).toString('base64'));
};

(async()=>{
  const entry=await load('static/shared/add-to-deck-entry.js');

  // カード名の検証と URL（エンコード・不正値は null）
  assert.equal(entry.validateCardName(' 青眼の白龍 '),'青眼の白龍');
  for(const bad of ['あ'.repeat(51),'a\nb','a\rb','',' ',null,undefined,3])assert.equal(entry.validateCardName(bad),null);
  assert.equal(entry.buildAddToDeckUrl('A&B#C'),'/?addToDeck=A%26B%23C');
  assert.equal(entry.buildAddToDeckUrl('a\nb'),null);

  // 受け口: 有効名で open・URL から除去／不正名は open せず除去／パラメータ無しは何もしない
  const run=href=>{
    const loc={href},calls=[],opened=[];
    const hist={state:{s:1},replaceState:(st,u,url)=>{calls.push(url);loc.href=new URL(url,href).href;}};
    const r=entry.receiveAddToDeck({open:n=>opened.push(n),loc,hist});
    return {r,calls,opened};
  };
  let x=run('https://e.test/?addToDeck='+encodeURIComponent('A&B#C')+'&q=1#h');
  assert.deepEqual(x,{r:true,calls:['/?q=1#h'],opened:['A&B#C']});
  x=run('https://e.test/?addToDeck='+encodeURIComponent('a\nb'));
  assert.deepEqual(x,{r:false,calls:['/'],opened:[]});
  x=run('https://e.test/?addToDeck=');
  assert.deepEqual(x,{r:false,calls:['/'],opened:[]});
  x=run('https://e.test/?q=1');
  assert.deepEqual(x,{r:false,calls:[],opened:[]});

  // 新着欄の探索: 描画待ち→見つかる／「もっと見る」で先へ／全件出ていて無い→null／取得失敗→null／描画されないままタイムアウト→null
  const mkCards=names=>names.map(n=>({dataset:{cardName:n},n}));
  function listDoc({visible,total,status='',step=6}){
    // visible 件が描画済みから始まる。more.click() で step 件ずつ増える。
    let shown=visible,clicks=0;
    const all=mkCards(total);
    const more={get hidden(){return shown>=all.length;},click(){clicks++;shown=Math.min(all.length,shown+step);}};
    return {get clicks(){return clicks;},setShown:n=>{shown=n;},
      querySelectorAll:()=>all.slice(0,shown),
      getElementById:id=>id==='newCardsMore'?more:id==='newCardsStatus'?{textContent:status}:null};
  }
  const names=Array.from({length:20},(_,i)=>'C'+i);
  const fast={sleep:async()=>{},intervalMs:0};
  let ld=listDoc({visible:6,total:names});
  assert.equal((await entry.findNewCard(ld,'C3',fast)).n,'C3');assert.equal(ld.clicks,0);
  ld=listDoc({visible:6,total:names});
  assert.equal((await entry.findNewCard(ld,'C14',fast)).n,'C14');assert.equal(ld.clicks,2,'もっと見る で先へ進めなかった');
  ld=listDoc({visible:6,total:names});
  assert.equal(await entry.findNewCard(ld,'無い',fast),null);assert.equal(ld.clicks,3,'全件表示後に探索を止めなかった');
  ld=listDoc({visible:0,total:names,status:'新着カードを取得できませんでした。'});
  assert.equal(await entry.findNewCard(ld,'C1',fast),null);
  // 描画待ち: 3回目の待ちの後に描画される
  ld=listDoc({visible:0,total:names});let waits=0;
  const slow={intervalMs:0,sleep:async()=>{if(++waits===3)ld.setShown(6);}};
  assert.equal((await entry.findNewCard(ld,'C2',slow)).n,'C2');assert.equal(waits,3);
  // タイムアウト: 描画されないまま時間切れ
  ld=listDoc({visible:0,total:names});let t=0;
  assert.equal(await entry.findNewCard(ld,'C2',{intervalMs:0,sleep:async()=>{},now:()=>(t+=1000)}),null);

  // HTMLの仮枠（.new-card-skel）の間は描画済みと数えず待つ（「もっと見る」を押さない）
  {
    let real=[];const skel=[1,2,3,4,5,6].map(()=>({dataset:{}}));let clicks=0,w=0;
    const sd={querySelectorAll:sel=>sel.includes(':not(.new-card-skel)')?real:skel.concat(real),
      getElementById:id=>id==='newCardsMore'?{hidden:false,click(){clicks++;}}:id==='newCardsStatus'?{textContent:''}:null};
    const r=await entry.findNewCard(sd,'C2',{intervalMs:0,sleep:async()=>{if(++w===2)real=mkCards(names.slice(0,6));}});
    assert.equal(r&&r.n,'C2','仮枠の後に描画されたカードを見つけられなかった');assert.equal(clicks,0,'仮枠の間に もっと見る を押した');
  }

  // 新着0件（表示できる未発売カードはありません）は待たず即 null（sleep が呼ばれない）
  ld=listDoc({visible:0,total:names,status:'現在、表示できる未発売カードはありません。'});let slept=0;
  assert.equal(await entry.findNewCard(ld,'C1',{intervalMs:0,sleep:async()=>{slept++;}}),null);assert.equal(slept,0,'0件でも待った');

  // 配線（ソース確認）: index.html は module 1行・innerHTML を使わない
  const ent=fs.readFileSync(path.join(root,'static/shared/add-to-deck-entry.js'),'utf8');
  assert(/scrollIntoView\(\{block:'center'\}\)/.test(ent)&&!/behavior/.test(ent),'scrollIntoView は block:center のみ');
  assert(/card\.offsetParent!==null/.test(ent),'非表示カードの判定が無い');
  assert(/const openFor=async name=>\{[\s\S]*try\{[\s\S]*\}catch\(error\)\{[\s\S]*openWithChip\(name\);/.test(ent),'openFor の try/catch→チップが無い');
  assert(/picker\.isConnected/.test(ent),'チップが開いた時点のピッカー要素を見ていない');
  const idx=fs.readFileSync(path.join(root,'templates/index.html'),'utf8');
  assert(/<script type="module" src="\{\{ static_url\('shared\/add-to-deck-entry\.js'\) \}\}"><\/script>/.test(idx));
  for(const f of ['static/shared/add-to-deck-entry.js'])
    assert(!/innerHTML|insertAdjacentHTML/.test(fs.readFileSync(path.join(root,f),'utf8')),f+' に innerHTML');
  console.log('トップの addToDeck 受け口: 検証・URL・新着欄の探索・配線を確認');
})().catch(e=>{console.error(e);process.exitCode=1;});
