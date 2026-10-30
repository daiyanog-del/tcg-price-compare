// 海外先行カード一覧（/tcg-preview・2026-10-23）を new-cards.js の実コードで確かめる。
// 読み先・商品の並び（英語版の発売日の新しい順）・ボタン（購入候補は出さない）・日付の書き方・見出し・取得失敗の文言。
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
// 一覧ページ（new-cards-page）かつ海外先行（tcg-preview-page）
get('mode-search').classList.add('new-cards-page');get('mode-search').classList.add('tcg-preview-page');
const pending=[],events={};
const sandbox={console,URL,Intl,Date,Number,Promise,encodeURIComponent,
  location:{origin:'https://example.test'},window:{},
  document:{getElementById:get,createElement:element,addEventListener:(event,fn)=>events[event]=fn},
  fetch(url){return new Promise((resolve,reject)=>pending.push({url,resolve,reject}));},
  doSearch(){},addToDeck(){},_ga(){},
};
vm.createContext(sandbox);
vm.runInContext(block('function safeUrl(', 'function searchCard('),sandbox);
vm.runInContext(fs.readFileSync(path.join(root,'static/js/new-cards.js'),'utf8'),sandbox);
const api=sandbox.window.NewCards;
const cards=[
  {name:'Old Prize',product_name:'Prize 2023',release_date:'2023-09-02',image_url:'https://example.test/1.png'},
  {name:'Angelechy Destrier',product_name:'CHAOS ORIGINS',release_date:'2026-07-03',image_url:'https://example.test/2.png'},
  {name:'Kerolia the Frolicsome',product_name:'BEYOND THE BRAVE',release_date:'2026-10-09',image_url:'https://example.test/3.png'},
  {name:'Just Flip a Coin',product_name:'BEYOND THE BRAVE',release_date:'2026-10-09',image_url:null},
];
(async()=>{
  events.DOMContentLoaded();
  assert.equal(pending.length,1);assert.equal(pending[0].url,'/api/tcg-preview-cards','海外先行の一覧から取っていない');
  pending[0].resolve({ok:true,json:async()=>({cards})});await api.load();await new Promise(r=>setTimeout(r,0));
  assert.equal(get('newCardsHeading').textContent,'海外先行カード一覧');
  const groups=get('newCardsGrid').children;
  const titles=groups.map(g=>g.children[0].children[0].textContent);
  assert.deepEqual(titles,['BEYOND THE BRAVE（2件）','CHAOS ORIGINS（1件）','Prize 2023（1件）'],'英語版の発売日の新しい順になっていない');
  assert.equal(groups[0].children[0].children[1].textContent,'英語版 2026/10/09 発売');
  assert.equal(groups[2].children[0].children[1].textContent,'英語版 2023/09/02 発売','過去の年が付いていない');
  const card=groups[0].children[1].children[0];
  const link=card.children[0];
  // 「TCG先行」の札は出さない（全カード同じで見出しと同じことしか言わない。2026-10-30）。追加日も無いので画像に何も重ねない
  assert.equal(link.children.length,2);assert.equal(link.children[1].tag,'strong');
  assert.ok(!link.children[0].children.some(c=>c.className==='new-card-added'));
  // 価格が無いので購入候補ボタンは出さず、デッキ追加だけ（詳細画面と同じ）
  assert.deepEqual(card.children[1].children.map(b=>b.className+':'+b.textContent),['new-card-add:+ デッキに追加']);
  assert.equal(link.href,'/card/'+encodeURIComponent('Kerolia the Frolicsome'));
  assert.equal(get('newCardsMore').hidden,true);
  assert.equal(get('newCardsStatus').textContent,'');
  // 取得失敗は海外先行カードの文言で出す
  get('newCardsRetry').listeners.click();
  assert.equal(pending[1].url,'/api/tcg-preview-cards');
  pending[1].resolve({ok:false,json:async()=>({})});await new Promise(r=>setTimeout(r,0));await new Promise(r=>setTimeout(r,0));
  assert.equal(get('newCardsStatus').textContent,'海外先行カードを取得できませんでした。');
  // 取得中に別のページへ移ったら古い応答は捨てる（未発売カードに「TCG先行」の札を付けない。2026-10-23 reviewer 指摘）
  const ms=get('mode-search');
  ms.classList.remove('tcg-preview-page');ms.classList.remove('new-cards-page'); // トップへ
  const topLoad=api.load();
  assert.equal(pending[2].url,'/api/new-cards');
  ms.classList.add('new-cards-page');ms.classList.add('tcg-preview-page'); // 応答前に戻る
  const tcgLoad=api.load();
  assert.notEqual(topLoad,tcgLoad,'別のページの取得を使い回した');
  assert.equal(pending[3].url,'/api/tcg-preview-cards');
  pending[2].resolve({ok:true,json:async()=>({cards:[{name:'未発売カード',product_name:'日本の商品',release_date:'2099-01-01',added_at:'2026-10-01T00:00:00Z'}]})});
  await topLoad;
  assert.ok(!JSON.stringify(get('newCardsGrid').children.map(g=>g.children[0].children[0].textContent)).includes('日本の商品'),'古い応答で描いた');
  pending[3].resolve({ok:true,json:async()=>({cards})});await tcgLoad;
  assert.equal(get('newCardsGrid').children.length,3);
  // トップ（海外先行でない）では追加日を画像に重ね、デッキと購入候補の2つのボタンを出す
  ms.classList.remove('tcg-preview-page');ms.classList.remove('new-cards-page');
  const topAgain=api.load();
  assert.equal(pending[4].url,'/api/new-cards');
  pending[4].resolve({ok:true,json:async()=>({cards:[{name:'未発売カード',added_at:'2026-10-01T00:00:00Z'}],has_more:false})});await topAgain;
  const topCard=get('newCardsGrid').children[0];
  assert.equal(topCard.children[0].children[0].children.find(c=>c.className==='new-card-added').textContent,'10/01 追加');
  assert.deepEqual(topCard.children[1].children.map(b=>b.className),['new-card-add','new-card-wish']);
  assert.equal(get('newCardsHeading').textContent,'新着の未発売カード');
  console.log('海外先行カード一覧: 読み先・商品の並び・ボタン・日付・見出し・取得失敗の文言・ページの行き来・トップの表示を確認');
})().catch(e=>{console.error(e);process.exit(1);});
