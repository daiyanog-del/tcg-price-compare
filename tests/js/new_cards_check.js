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
  return {tag,children:[],attributes:{},listeners:{},hidden:false,value:'',textContent:'',
    classList:{contains:name=>classes.has(name),add:name=>classes.add(name),remove:name=>classes.delete(name)},
    // HTMLパーサーに入力を渡す実装へ退行した場合は、名前の内容に関係なく失敗させる。
    set innerHTML(value){throw Error('HTML文字列によるDOM作成が使われています: '+value);},
    appendChild(child){this.children.push(child);child.parent=this;},
    append(...children){children.forEach(child=>this.appendChild(child));},
    replaceChildren(...children){this.children.forEach(child=>child.parent=null);this.children=[];this.append(...children);},
    remove(){this.parent.children=this.parent.children.filter(child=>child!==this);this.parent=null;},
    setAttribute(key,value){this.attributes[key]=value;},
    addEventListener(event,handler){this.listeners[event]=handler;},
  };
}
const ids=['newCardsSection','newCardsGrid','newCardsMore','newCardsStatus','newCardsRetry','empty','q'];
const elements=new Map(ids.map(id=>[id,element()]));
const get=id=>elements.get(id)||null;
const pending=[],searches=[],events={};
const sandbox={console,URL,Intl,Date,Number,Promise,encodeURIComponent,
  location:{origin:'https://example.test'},window:{},
  document:{getElementById:get,createElement:element,addEventListener:(event,fn)=>events[event]=fn},
  fetch(url){return new Promise((resolve,reject)=>pending.push({url,resolve,reject}));},
  doSearch(opts){searches.push({name:get('q').value,opts});},
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
  let links=get('newCardsGrid').children;
  assert.equal(links.length,6);assert.equal(get('newCardsMore').hidden,false);
  assert.equal(links[0].tag,'a');
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
  console.log('新着カード: 6件追加・DOM安全性・JST日付・クリック・通信共有・失敗/空/再試行を確認');
})().catch(error=>{console.error(error);process.exitCode=1;});
