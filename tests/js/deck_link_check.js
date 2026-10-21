// シミュレーターのデッキ編集の土台（static/solitaire/js/services/deck-link.js）の検証。
// 画面の見た目・押下後の動きは Playwright で確認する。ここは保存の計算・ひも付けの状態・抜けるカードの選び方を見る。
'use strict';
const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const root=path.join(__dirname,'../..');
const load=async file=>import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync(path.join(root,file),'utf8')).toString('base64'));

// 最小の .tier-item-wrapper（id・data-token・親・中のカード名）
const W=(id,name,{parent='poolRow',token=false}={})=>({
  id, dataset:token?{token:'1'}:{}, parentElement:{id:parent},
  querySelector:sel=>sel==='.tier-item'?{id:id.split(' ')[0],dataset:{cardName:name}}:null,
});

(async()=>{
  const m=await load('static/solitaire/js/services/deck-link.js');

  // テキスト ⇔ {main,ex}: トップと同じ形式（1枚は名前だけ・[EX] 区切り）。同名の複数行は足す
  const deck=m.parseDeckSections('3 灰流うらら\n増殖するG\r\n\n2 灰流うらら\n[EX]\n1 アクセスコード・トーカー');
  assert.deepEqual(deck,{main:[{qty:5,name:'灰流うらら'},{qty:1,name:'増殖するG'}],ex:[{qty:1,name:'アクセスコード・トーカー'}]});
  assert.equal(m.serializeDeck(deck),'5 灰流うらら\n増殖するG\n[EX]\nアクセスコード・トーカー');
  assert.equal(m.serializeDeck({main:[],ex:[{qty:2,name:'X'}]}),'[EX]\n2 X');
  assert.deepEqual(m.parseDeckSections(m.serializeDeck(deck)),deck,'往復で変わった');

  // 差分の重ね方: 並びを保って枚数だけ変える・新しいカードは末尾・0枚で消える・元は変えない
  const base={main:[{qty:3,name:'A'},{qty:1,name:'B'}],ex:[{qty:1,name:'E'}]};
  const out=m.applyDeltas(base,[{sec:'main',name:'B',n:-1},{sec:'main',name:'C',n:2},{sec:'main',name:'A',n:1},{sec:'ex',name:'F',n:1}]);
  assert.deepEqual(out,{main:[{qty:4,name:'A'},{qty:2,name:'C'}],ex:[{qty:1,name:'E'},{qty:1,name:'F'}]});
  assert.deepEqual(base.main[1],{qty:1,name:'B'},'元のデッキを書き換えた');
  // 別の場所で既に消されたカードを抜く → 何もしない（マイナスの行を作らない）
  assert.deepEqual(m.applyDeltas({main:[{qty:1,name:'A'}],ex:[]},[{sec:'main',name:'Z',n:-1}]),{main:[{qty:1,name:'A'}],ex:[]});
  // 保存データでは反対側（メイン⇔EX）にあるカードを抜く → そちらから減らす
  assert.deepEqual(m.applyDeltas({main:[{qty:1,name:'E'}],ex:[]},[{sec:'ex',name:'E',n:-1}]),{main:[],ex:[]});
  // 足すときも、保存データでは反対側にあるカード（[EX] 無しで貼り付けた EX）はその行を増やす（メインと EX に分けない）
  assert.deepEqual(m.applyDeltas({main:[{qty:3,name:'A'},{qty:2,name:'E'}],ex:[]},[{sec:'ex',name:'E',n:1}]),{main:[{qty:3,name:'A'},{qty:3,name:'E'}],ex:[]});
  // 保存済みの中身: テキストを正とし、空なら配列（古いデータで「変更を保存」すると中身が消えるのを防ぐ）
  assert.deepEqual(m.deckContents({text:'2 A',main:[{qty:9,name:'Z'}]}),{main:[{qty:2,name:'A'}],ex:[]});
  assert.deepEqual(m.deckContents({text:'',main:[{qty:2,name:'A'},{qty:0,name:'B'},null],ex:[{qty:'1',name:' E '}]}),{main:[{qty:2,name:'A'}],ex:[{qty:1,name:'E'}]});
  assert.deepEqual(m.deckContents({}),{main:[],ex:[]});
  // 別の場所で増えていた分は消さずに今回の増減だけ重ねる（読み込み時3枚→別タブで4枚→ここで1枚抜く＝3枚）
  assert.deepEqual(m.applyDeltas({main:[{qty:4,name:'A'}],ex:[]},[{sec:'main',name:'A',n:-1}]).main,[{qty:3,name:'A'}]);

  // 盤面から数える: トークン・名前の無いものは数えない。EX は id の末尾で見る（場に出ても変わらない）
  const dom={querySelectorAll:sel=>{assert.equal(sel,m.BOARD_WRAPPERS,'置き場所で絞っていない（演出の複製まで数える）');return [
    W('item-001 normal','A'),W('item-002 normal','A',{parent:'center-slot'}),W('item-003 ex','E',{parent:'custom-slot-3'}),
    W('item-004 normal','トークン',{token:true}),W('item-005 normal',undefined)];}};
  assert.deepEqual(m.collectDeckFromDom(dom),{main:[{qty:2,name:'A'}],ex:[{qty:1,name:'E'}]});

  // 抜けるカード: デッキ欄にあって記録に出てこないもの。右クリックしたカードを先に見る
  const inHand=W('item-010 normal','A',{parent:'center-slot'}),logged=W('item-011 normal','A'),free=W('item-012 normal','A'),exFree=W('item-013 ex','E',{parent:'poolRow2'});
  assert.equal(m.pickRemovable([inHand,logged,free],new Set(['item-011'])),free);
  assert.equal(m.pickRemovable([inHand,logged],new Set(['item-011'])),null,'記録で使っているカードを抜こうとした');
  assert.equal(m.pickRemovable([exFree],new Set()),exFree,'EXデッキ欄から抜けない');
  assert.equal(m.pickRemovable([W('item-014 normal','T',{token:true})],new Set()),null,'トークンを抜こうとした');

  // ひも付けの状態: 読込で差分が空・増減で dirty・同じカードを足して抜けば dirty でない・保存で空
  const seen=[];const off=m.onDeckLinkChange(v=>seen.push(v));
  assert.equal(m.getDeckLink(),null);
  m.setDeckLink({savedId:'d_1',name:' 天盃龍 '});
  assert.deepEqual(m.getDeckLink(),{savedId:'d_1',name:'天盃龍',source:null,dirty:false});
  m.recordDelta('A','main',+1);assert.equal(m.getDeckLink().dirty,true);
  m.recordDelta('A','main',-1);assert.equal(m.getDeckLink().dirty,false,'足して抜いたのに変更ありのまま');
  m.recordDelta('B','ex',-1);m.recordDelta('C','main',+1);m.recordDelta('C','main',+1);
  assert.deepEqual(m.getDeltas(),[{sec:'ex',name:'B',n:-1},{sec:'main',name:'C',n:2}]);
  // 退避→取り消し用の復元（別のオブジェクトとして戻る）
  const snap=m.captureLinkState();
  m.markSaved({savedId:'d_1',name:'天盃龍'});assert.equal(m.getDeckLink().dirty,false);assert.deepEqual(m.getDeltas(),[]);
  m.restoreLinkState(snap);assert.deepEqual(m.getDeltas(),[{sec:'ex',name:'B',n:-1},{sec:'main',name:'C',n:2}]);
  assert.equal(m.getDeckLink().savedId,'d_1');
  // 名前が空なら「デッキ」・消去で null
  m.setDeckLink({});assert.deepEqual(m.getDeckLink(),{savedId:null,name:'デッキ',source:null,dirty:false});
  m.clearDeckLink();assert.equal(m.getDeckLink(),null);assert.equal(m.captureLinkState(),null);
  // ひも付けが無いまま（共有リプレイを開いた等）足すと、未登録のデッキとして始まる
  m.recordDelta('A','main',+1);assert.deepEqual(m.getDeckLink(),{savedId:null,name:'デッキ',source:null,dirty:true});
  assert(seen.length>=8,'変化を通知していない');off();

  // トップからの持ち込み: 保存済みデッキの ID は形が正しいときだけ引き継ぐ（変な値はひも付けないだけで読込は続ける）
  const h=await load('static/shared/deck-handoff.js');
  const card={main:[{name:'A',qty:1}],ex:[]};
  assert.equal(h.validateHandoff({version:1,name:'x',...card,savedId:'d_1729'}).savedId,'d_1729');
  for(const bad of ['', 'a b', 'x'.repeat(65), 3, null, '<img>'])
    assert.equal('savedId' in h.validateHandoff({version:1,name:'x',...card,savedId:bad}),false,'不正な savedId を通した: '+bad);

  // 配線（ソース確認）
  const read=f=>fs.readFileSync(path.join(root,f),'utf8');
  const ui=read('static/solitaire/js/ui/deck-edit-ui.js');
  assert(!/innerHTML|insertAdjacentHTML/.test(ui),'deck-edit-ui.js に innerHTML（カード名は textContent で入れる）');
  assert(/applyDeltas\(deckContents\(deck\), getDeltas\(\)\)/.test(ui),'上書き保存が「保存済み＋差分」になっていない');
  assert(/Object\.assign\(d, prev, \{ updated: Date\.now\(\) \}\)/.test(ui),'取り消しで更新時刻を古い値に戻している（同期で取り消しが消える）');
  assert(/'sol-deck-load-start', \(e\) => \{\r?\n\s*_dismissUndoToast\(\);/.test(ui)&&/'sol-board-cleared', \(\) => \{ _dismissUndoToast\(\);/.test(ui)&&/'sol-deck-edited', _dismissUndoToast\)/.test(ui),'読込・消去・編集で取り消しを閉じていない');
  const panel=read('static/solitaire/js/ui/deck-input-panel.js');
  // 入れ替えの3経路は取得できてから _announceLoadStart(replace) で合図する（2026-10-21）。EX だけの読込は直接 replace:false
  assert.equal((panel.match(/_announceLoadStart\((true|mainCards\.length > 0)\);/g)||[]).length,3,'入れ替えの3経路が開始の合図を出していない');
  assert(/new CustomEvent\('sol-deck-load-start', \{ detail: \{ replace \} \}\)/.test(panel),'開始の合図に replace が無い');
  assert.equal((panel.match(/new CustomEvent\('sol-deck-load-start', \{ detail: \{ replace: false \} \}\)/g)||[]).length,1,'EX だけの読込に replace:false が無い');
  assert(/loadDeckFromText\(deck\.text \|\| '', row\)\) \{?\s*setDeckLink\(\{ savedId: deck\.id/.test(panel),'マイデッキの読込でひも付けていない');
  const cm=read('static/solitaire/js/ui/context-menu.js');
  assert(!/background:\s*#fff/.test(read('static/solitaire/css/components.css').split('.sol-context-menu {')[1].split('}')[0]),'右クリックメニューが白のまま');
  assert(/setExtraMenuItemsProvider/.test(cm));
  assert(!fs.existsSync(path.join(root,'static/shared/mydeck-hint.js')),'置き換えた mydeck-hint.js が残っている');
  console.log('デッキ編集の土台: 保存の計算・ひも付けの状態・抜けるカード・配線を確認');
})().catch(e=>{console.error(e);process.exitCode=1;});
