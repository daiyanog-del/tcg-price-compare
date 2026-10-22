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
const ids=['newCardsInitial','newCardsSection','newCardsGrid','newCardsMore','newCardsStatus','newCardsRetry','newCardsHeading','newCardsAll','mode-search','empty','q'];
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
// サーバーが埋め込んだ一覧（#newCardsInitial）があれば、/api/new-cards を呼ばずに描く（2026-10-22 LCP）
const initialEl=get('newCardsInitial');
get('newCardsSection').appendChild(initialEl);
initialEl.textContent=JSON.stringify({cards:[{name:'埋め込み1',thumb_url:'https://example.test/t1.webp'},{name:'埋め込み2',image_url:'https://example.test/2.png'}],has_more:false});
(async()=>{
  events.DOMContentLoaded();
  await api.load();
  assert.equal(pending.length,0,'埋め込みがあるのに /api/new-cards を呼んだ');
  const wraps=get('newCardsGrid').children;
  assert.equal(wraps.length,2);
  assert.equal(wraps[0].dataset.cardName,'埋め込み1');
  assert.equal(initialEl.parent,null,'埋め込みの要素を一度使ったら外す');
  elements.delete('newCardsInitial');
  // 直後（30秒以内）にもう一度呼ばれても取り直さない（描き直すと最初の絵が後回しになる・2026-10-22）
  await api.load();
  assert.equal(pending.length,0,'埋め込みを使った直後に取り直した');
  // 30秒を過ぎたら、表示のたびに取り直す従来の動き
  const realNow=Date.now;sandbox.Date.now=()=>realNow()+31000;
  const again=api.load();
  assert.equal(pending.length,1);assert.equal(pending[0].url,'/api/new-cards');
  pending[0].resolve({ok:true,json:async()=>({cards:[],has_more:false})});await again;
  console.log('新着カード（埋め込み）: API を待たずに描く・一度だけ使う・再読込は API');
})().catch(e=>{console.error('FAIL',e);process.exit(1);});
