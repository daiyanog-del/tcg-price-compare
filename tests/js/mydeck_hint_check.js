// 「マイデッキに追加」案内（static/shared/mydeck-hint.js）と受け口（add-to-deck-entry.js）の検証。
// 実画面の見た目・押下後の動きは Playwright で確認する。ここは条件分岐と URL・検証を見る。
'use strict';
const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const root=path.join(__dirname,'../..');
const load=async file=>{
  let src=fs.readFileSync(path.join(root,file),'utf8');
  // 相対 import は data: URL から解決できないので、依存モジュールを globalThis 経由に差し替える
  src=src.replace(/^import \{buildAddToDeckUrl\} from '\.\/add-to-deck-entry\.js';/m,'const {buildAddToDeckUrl}=globalThis.__entry;');
  return import('data:text/javascript;base64,'+Buffer.from(src).toString('base64'));
};

// 最小の DOM（createElement・append・querySelectorAll・イベント）
function fakeDoc(){
  const handlers={};
  const mk=tag=>({tag,className:'',children:[],textContent:'',href:'',
    append(...c){c.forEach(x=>{x.parent=this;this.children.push(x);});},
    appendChild(c){this.append(c);},
    remove(){if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this);},
    all(){return this.children.flatMap(c=>[c,...c.all()]);},
    querySelectorAll(sel){const cls=sel.split(',').map(s=>s.trim().slice(1));return this.all().filter(e=>cls.includes(e.className));}});
  const label=mk('div'),body=mk('div');
  return {label,body,
    createElement:mk,
    querySelector:sel=>sel==='#imagePool .pool-label'?label:null,
    querySelectorAll(sel){return [...label.querySelectorAll(sel),...body.querySelectorAll(sel)];},
    addEventListener:(t,fn)=>{(handlers[t]??=new Set()).add(fn);},
    removeEventListener:(t,fn)=>{handlers[t]?.delete(fn);},
    emit:t=>[...(handlers[t]??[])].forEach(fn=>fn())};
}

(async()=>{
  const entry=await load('static/shared/add-to-deck-entry.js');
  globalThis.__entry=entry;
  const hint=await load('static/shared/mydeck-hint.js');

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

  // PC: focus ありで見出しに一文＋リンク。別デッキ読込で消える
  let d=fakeDoc();
  hint.startHint({focus:'新B',showPc:true,doc:d});
  const link=d.label.querySelectorAll('.sol-mydeck-hint-link');
  assert.equal(link.length,1);assert.equal(link[0].href,'/?addToDeck=%E6%96%B0B');
  assert.match(d.label.querySelectorAll('.sol-mydeck-hint-text')[0].textContent,/自分のデッキで回すなら/);
  d.emit('sol-deck-load-start');
  assert.equal(d.label.querySelectorAll('.sol-mydeck-hint').length,0,'別デッキ読込で消えない');
  hint.appendMobileHintBox(d.body,'新B',d);
  assert.equal(d.body.querySelectorAll('.sol-mydeck-hint-box').length,0,'終了後にスマホの箱が出た');

  // 最初の操作（click）では消えない。盤面消去では消える
  d=fakeDoc();hint.startHint({focus:'新B',showPc:true,doc:d});
  d.emit('click');assert.equal(d.label.querySelectorAll('.sol-mydeck-hint').length,1);
  d.emit('sol-board-cleared');assert.equal(d.label.querySelectorAll('.sol-mydeck-hint').length,0);

  // PC: focus なし・スマホ縦（showPc=false）では一文を出さない
  for(const arg of [{focus:null,showPc:true},{focus:'新B',showPc:false}]){
    d=fakeDoc();hint.startHint({...arg,doc:d});
    assert.equal(d.label.querySelectorAll('.sol-mydeck-hint').length,0);
  }

  // スマホの箱: startHint 後だけ。何度呼んでも1個。カード名が変われば作り直す。不正名は出さない
  d=fakeDoc();hint.endHint(d); // モジュールの状態を通常起動（案内なし）に戻す
  hint.appendMobileHintBox(d.body,'新A',d);
  assert.equal(d.body.querySelectorAll('.sol-mydeck-hint-box').length,0,'startHint 前（通常起動）に箱が出た');
  hint.startHint({focus:null,showPc:false,doc:d});
  hint.appendMobileHintBox(d.body,'新A',d);hint.appendMobileHintBox(d.body,'新B',d);
  assert.equal(d.body.querySelectorAll('.sol-mydeck-hint-box').length,1);
  assert.equal(d.body.querySelectorAll('.sol-mydeck-hint-box-btn')[0].href,'/?addToDeck=%E6%96%B0B');
  hint.appendMobileHintBox(d.body,'a\nb',d);
  assert.equal(d.body.querySelectorAll('.sol-mydeck-hint-box').length,0,'不正名で古い箱が残った');

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

  // 新着0件（表示できる未発売カードはありません）は待たず即 null（sleep が呼ばれない）
  ld=listDoc({visible:0,total:names,status:'現在、表示できる未発売カードはありません。'});let slept=0;
  assert.equal(await entry.findNewCard(ld,'C1',{intervalMs:0,sleep:async()=>{slept++;}}),null);assert.equal(slept,0,'0件でも待った');

  // 配線（ソース確認）: 未発売(proxy)の描画直後だけ呼ぶ・index.html は module 1行・innerHTML を使わない
  const mui=fs.readFileSync(path.join(root,'static/solitaire/js/ui/mobile-ui.js'),'utf8');
  assert(/_renderCard\(data\.proxy[^\n]*\n[^\n]*\n\s*appendMobileHintBox\(body, name\);/.test(mui));
  assert.equal((mui.match(/appendMobileHintBox\(/g)||[]).length,1,'呼び出しは1箇所だけ');
  const ent=fs.readFileSync(path.join(root,'static/shared/add-to-deck-entry.js'),'utf8');
  assert(/scrollIntoView\(\{block:'center'\}\)/.test(ent)&&!/behavior/.test(ent),'scrollIntoView は block:center のみ');
  assert(/card\.offsetParent!==null/.test(ent),'非表示カードの判定が無い');
  assert(/const openFor=async name=>\{[\s\S]*try\{[\s\S]*\}catch\(error\)\{[\s\S]*openWithChip\(name\);/.test(ent),'openFor の try/catch→チップが無い');
  assert(/picker\.isConnected/.test(ent),'チップが開いた時点のピッカー要素を見ていない');
  const mcss=fs.readFileSync(path.join(root,'static/solitaire/css/mobile.css'),'utf8');
  assert(/\.pool-label \.sol-mydeck-hint \{ display: none; \}/.test(mcss),'スマホ縦で PC の一文を隠していない');
  const ccss=fs.readFileSync(path.join(root,'static/solitaire/css/components.css'),'utf8').split('/* 新着直行のとき出す')[1];
  assert(!/--gold|#f3ce46|243, 206, 70/i.test(ccss),'案内に金色が残っている');
  const idx=fs.readFileSync(path.join(root,'templates/index.html'),'utf8');
  assert(/<script type="module" src="\{\{ static_url\('shared\/add-to-deck-entry\.js'\) \}\}"><\/script>/.test(idx));
  for(const f of ['static/shared/mydeck-hint.js','static/shared/add-to-deck-entry.js'])
    assert(!/innerHTML|insertAdjacentHTML/.test(fs.readFileSync(path.join(root,f),'utf8')),f+' に innerHTML');
  console.log('マイデッキ追加の案内: 検証・URL・受け口・PC/スマホの出し分け・終了条件・配線を確認');
})().catch(e=>{console.error(e);process.exitCode=1;});
