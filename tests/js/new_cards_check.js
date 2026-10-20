// 新着カードの実コードを実行し、通信順序・安全なDOM作成・段階表示を検証する。
'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert/strict');
const root=path.join(__dirname,'../..');
const main=fs.readFileSync(path.join(root,'static/js/index-main.js'),'utf8');
function block(start,end){
  const a=main.indexOf(start),b=main.indexOf(end,a+start.length);
  assert(a>=0&&b>a);return main.slice(a,b);
}
function element(tag='div'){
  const classes=new Set();
  return {tag,dataset:{},children:[],attributes:{},listeners:{},hidden:false,value:'',textContent:'',
    classList:{contains:name=>classes.has(name),add:name=>classes.add(name),remove:name=>classes.delete(name),
      toggle:(name,on)=>{if(on)classes.add(name);else classes.delete(name);}},
    // HTMLパーサーに入力を渡す実装へ退行した場合は、名前の内容に関係なく失敗させる。
    set innerHTML(value){throw Error('HTML文字列によるDOM作成が使われています: '+value);},
    appendChild(child){this.children.push(child);child.parent=this;},
    append(...children){children.forEach(child=>this.appendChild(child));},
    replaceChildren(...children){this.children.forEach(child=>child.parent=null);this.children=[];this.append(...children);},
    remove(){this.parent.children=this.parent.children.filter(child=>child!==this);this.parent=null;},
    setAttribute(key,value){this.attributes[key]=value;},removeAttribute(key){delete this.attributes[key];},
    addEventListener(event,handler){this.listeners[event]=handler;},
  };
}
const ids=['newCardsSection','newCardsGrid','newCardsMore','newCardsStatus','newCardsRetry','newCardsHeading','newCardsAll','mode-search','empty','q'];
const elements=new Map(ids.map(id=>[id,element()]));
const get=id=>elements.get(id)||null;
const pending=[],searches=[],added=[],events={};
const gaEvents=[];
const sandbox={console,URL,Intl,Date,Number,Promise,encodeURIComponent,
  location:{origin:'https://example.test'},window:{},
  document:{getElementById:get,createElement:element,addEventListener:(event,fn)=>events[event]=fn},
  fetch(url){return new Promise((resolve,reject)=>pending.push({url,resolve,reject}));},
  doSearch(opts){searches.push({name:get('q').value,opts});},
  addToDeck(name,button){added.push({name,button});},
  _ga(name,params){gaEvents.push({name,params});},
};
vm.createContext(sandbox);
vm.runInContext(block('function safeUrl(', 'function searchCard('),sandbox);
vm.runInContext(fs.readFileSync(path.join(root,'static/js/new-cards.js'),'utf8'),sandbox);
const api=sandbox.window.NewCards;
const reply=(request,body,ok=true)=>request.resolve({ok,json:async()=>body});
const cards=Array.from({length:13},(_,i)=>({name:'カード'+i,image_url:'https://example.test/'+i+'.jpg',added_at:'2026-10-01T15:30:00Z'}));
cards[0].name='<img src=x onerror="globalThis.injected=true">';
cards[0].image_url='javascript:globalThis.injected=true';
cards[1].image_url='data:image/svg+xml,<svg onload="alert(1)"/>';
cards[2].image_url='';
cards[3].added_at='不正な日付';
function click(link,flags={}){
  const event={button:0,prevented:false,preventDefault(){this.prevented=true;},...flags};
  link.listeners.click(event);return event;
}
(async()=>{
  events.DOMContentLoaded();
  assert.equal(pending.length,1);assert.equal(pending[0].url,'/api/new-cards');
  const first=api.load(),duplicate=api.load();
  assert.equal(first,duplicate);assert.equal(pending.length,1,'二重loadで通信が重複した');
  reply(pending[0],{cards,has_more:false});await first;
  // 各カードは外枠 .new-card（<a>＋デッキ追加ボタン）。<a>の中にbuttonを入れない。
  const wraps=get('newCardsGrid').children;
  let links=wraps.map(w=>w.children[0]);
  assert.equal(links.length,6);assert.equal(get('newCardsMore').hidden,false);
  assert.equal(wraps[0].className,'new-card');
  assert.equal(wraps[0].dataset.cardName,cards[0].name,'/?addToDeck= の受け口が探すための data-card-name が無い');
  assert.equal(links[0].tag,'a');
  assert.equal(wraps[0].children[1].tag,'button');
  assert.equal(wraps[0].children[1].textContent,'+ デッキに追加');
  assert.equal(links[0].children.some(c=>c.tag==='button'),false,'<a>内にbuttonがある');
  assert.equal(links[0].href,'/card/'+encodeURIComponent(cards[0].name));
  assert.equal(links[0].children[2].tag,'strong');
  assert.equal(links[0].children[2].textContent,cards[0].name);
  assert.equal(sandbox.injected,undefined);
  for(const link of links.slice(0,3))assert.equal(link.children[0].children.length,1,'不正・空URLからimgが作成された');
  const picture=links[4].children[0],image=picture.children[1];
  assert.equal(image.tag,'img');assert.equal(image.src,cards[4].image_url);
  image.listeners.error();assert.equal(picture.children.length,1);
  assert.equal(picture.children[0].textContent,cards[4].name,'画像失敗時の代替カード名が消えた');
  assert.equal(links[0].children[1].children[1].textContent,'10/02 追加','JSTの日付境界が反映されていない');
  assert.equal(links[3].children[1].children[1].textContent,'');

  // デッキ追加ボタンは既存の addToDeck を呼ぶだけ。
  wraps[1].children[1].listeners.click();
  assert.equal(added.length,1);assert.equal(added[0].name,cards[1].name);assert.equal(added[0].button,wraps[1].children[1]);
  assert.deepEqual(gaEvents.map(e=>e.name),['new_card_add_deck'],'新着からのデッキ追加が計測されていない');
  assert.equal(get('newCardsAll').hidden,true,'has_moreが偽なのに全件リンクが出ている');

  // 通常クリックだけ既存検索へ渡し、別タブ・別ウィンドウ操作は妨げない。
  assert.equal(click(links[0]).prevented,true);
  assert.equal(searches.length,1);assert.equal(searches[0].name,cards[0].name);
  assert.equal(searches[0].opts.trigger,'new_card');
  assert.equal(searches[0].opts.validated,undefined);
  for(const flags of [{ctrlKey:true},{metaKey:true},{shiftKey:true},{altKey:true},{button:1}]){
    assert.equal(click(links[0],flags).prevented,false);
  }
  assert.equal(searches.length,1);

  get('newCardsMore').listeners.click();assert.equal(get('newCardsGrid').children.length,12);
  assert.equal(get('newCardsMore').hidden,false);
  get('newCardsMore').listeners.click();assert.equal(get('newCardsGrid').children.length,13);
  assert.equal(get('newCardsMore').hidden,true);
  assert.equal(pending.length,1,'もっと見るで不要な再通信が発生した');

  // 再取得失敗時は以前の一覧を残さず、正常0件とは別の案内にする。
  const failed=api.load();reply(pending.at(-1),{},false);await failed;
  assert.equal(get('newCardsGrid').children.length,0);
  assert.equal(get('newCardsMore').hidden,true);assert.equal(get('newCardsRetry').hidden,false);
  const failureMessage=get('newCardsStatus').textContent;assert.match(failureMessage,/取得できません/);
  const retry=get('newCardsRetry').listeners.click();
  assert.equal(get('newCardsRetry').hidden,true);
  reply(pending.at(-1),{cards:[],has_more:false});await retry;
  assert.equal(get('newCardsGrid').children.length,0);
  assert.equal(get('newCardsRetry').hidden,true);
  assert.notEqual(get('newCardsStatus').textContent,failureMessage);
  assert.match(get('newCardsStatus').textContent,/未発売カードはありません/);

  const invalid=api.load();reply(pending.at(-1),{cards:null});await invalid;
  assert.equal(get('newCardsRetry').hidden,false);
  assert.equal(get('newCardsStatus').textContent,failureMessage);
  const restored=api.load();reply(pending.at(-1),{cards,has_more:false});await restored;
  assert.equal(get('newCardsGrid').children.length,6,'再取得後に表示件数が初期化されていない');
  assert.equal(get('newCardsStatus').textContent,'');assert.equal(get('newCardsRetry').hidden,true);
  // has_more が真で全件表示しきったときだけ一覧ページへのリンクを出す。
  const more=api.load();reply(pending.at(-1),{cards:cards.slice(0,7),has_more:true});await more;
  assert.equal(get('newCardsAll').hidden,true);
  get('newCardsMore').listeners.click();
  assert.equal(get('newCardsAll').hidden,false);assert.equal(get('newCardsStatus').textContent,'');

  // 一覧ページ: all=1 で取得し、商品ごとにグループ化（新しい順・未定は最後・発売日は最も早い日付）。
  get('mode-search').classList.add('new-cards-page');
  const listCards=[
    {name:'A1',product_name:'商品X',added_at:'2026-10-01T00:00:00Z',release_date:'2026-12-20'},
    {name:'B1',product_name:'',added_at:'2026-10-09T00:00:00Z',release_date:null},
    {name:'C1',product_name:'商品Y',added_at:'2026-10-05T00:00:00Z',release_date:'2026-11-05'},
    {name:'A2',product_name:'商品X',added_at:'2026-10-03T00:00:00Z',release_date:'2026-12-01'},
  ];
  const listLoad=api.load();
  assert.equal(pending.at(-1).url,'/api/new-cards?all=1');
  reply(pending.at(-1),{cards:listCards,has_more:false});await listLoad;
  const groups=get('newCardsGrid').children;
  assert.deepEqual(groups.map(g=>g.children[0].children[0].textContent),['商品Y（1件）','商品X（2件）','収録商品未定（1件）']);
  assert.equal(groups[1].children[0].children[1].textContent,'12/01 発売');
  assert.equal(groups[2].children[0].children.length,1,'発売日が無いのに表示された');
  assert.equal(groups[1].children[1].children.length,2);
  assert.equal(get('newCardsMore').hidden,true);assert.equal(get('newCardsAll').hidden,true);
  assert.equal(get('newCardsHeading').textContent,'未発売カード一覧');
  console.log('新着カード: 6件追加・DOM安全性・JST日付・クリック・通信共有・失敗/空/再試行を確認');
})().catch(error=>{console.error(error);process.exitCode=1;});
