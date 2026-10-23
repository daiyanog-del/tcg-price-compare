/* デッキ別の所持枚数。カード配列とは分離し、未確認と明示した0枚を区別する。 */
(function(global){
  'use strict';
  const has=(o,k)=>Object.prototype.hasOwnProperty.call(o,k);
  const ownMax=99; // 購入候補・同期の既存枚数上限と同じ
  let owned=Object.create(null), preview=null, rendered='';

  function validateOwned(value){
    if(value===undefined)return Object.create(null);
    if(!value||typeof value!=='object'||Array.isArray(value))throw Error('所持枚数の形式が正しくありません。');
    const out=Object.create(null);
    if(Object.keys(value).length>160)throw Error('所持枚数は最大160種類までです。');
    Object.keys(value).forEach(name=>{
      const qty=value[name];
      if(!name.trim()||name.length>50||!Number.isInteger(qty)||qty<0||qty>ownMax)throw Error('所持枚数は0〜99の整数で入力してください。');
      out[name]=qty;
    });
    return out;
  }
  function requiredCards(deck){
    const totals=new Map();
    [...(deck.main||[]),...(deck.ex||[])].forEach(card=>{
      if(!card||typeof card.name!=='string'||!card.name.trim()||card.name.length>50||!Number.isInteger(card.qty)||card.qty<1||card.qty>99)throw Error('デッキのカード名・枚数を確認してください。');
      totals.set(card.name,(totals.get(card.name)||0)+card.qty);
    });
    return totals;
  }
  // 「候補にある」は、このデッキ用＋デッキ指定なしの分（shared/wish-deck.js・2026-10-20 合計で数える）
  function plan(deck,values,wishlist,deckId){
    const clean=validateOwned(values), totals=requiredCards(deck);
    wishlist.forEach(c=>{
      if(!c||typeof c.name!=='string'||!Number.isInteger(c.qty)||c.qty<1||c.qty>99)throw Error('購入候補に不正な枚数があります。購入候補画面で確認してください。');
    });
    // 今は無いデッキ（削除したデッキ）用の分はデッキ指定なしとして数える
    const known=typeof global.savedDecksGet==='function'?global.savedDecksGet().map(d=>d.id):[];
    return global.WishDeck.plan(totals,clean,global.WishDeck.forgetUnknown(wishlist,known),deckId||null);
  }
  // 購入候補に足す計算。TCG先行カード（英語版で先に出て日本に無いカード）は価格が無いので外す
  // （所持枚数の入力欄 render() は plan のままなので、TCG先行カードの所持枚数も入れられる）
  function wishPlan(deck,values,wishlist,deckId){
    const tcg=global.TcgPreview;
    return plan(deck,values,wishlist,deckId).filter(r=>!(tcg&&tcg.has(r.name)));
  }
  function supplement(wishlist,rows,deckId){
    return global.WishDeck.supplement(wishlist,rows,deckId||null);
  }
  function current(){
    const ta=document.getElementById('deckTextarea');
    return parseDeckSections(ta?ta.value:'');
  }
  function el(tag,text,cls){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;}
  function message(text,error){
    const box=document.getElementById('deckOwnershipStatus');if(!box)return;
    box.textContent=text;box.setAttribute('role',error?'alert':'status');
  }
  function invalidate(){preview=null;const box=document.getElementById('deckOwnershipConfirm');if(box)box.hidden=true;}
  function save(){
    invalidate();
    try{global._persistDeckOwnership();message('所持枚数をこの端末に保存しました。',false);return true;}
    catch(e){message('所持枚数を保存できませんでした。'+e.message,true);return false;}
  }
  function render(){
    const box=document.getElementById('deckOwnershipRows');if(!box)return;
    // 非同期の価格再描画だけでは入力フォーカスや確認内容を破棄しない。
    try{
      const rows=plan(current(),owned,[]), signature=JSON.stringify(rows);
      if(signature===rendered)return;
      rendered=signature;invalidate();box.replaceChildren();
      if(!rows.length){box.append(el('p','カードをデッキに追加すると、所持枚数を入力できます。'));return;}
      rows.forEach((row,index)=>{
        const line=el('div',undefined,'deck-owned-row');
        const label=el('label',row.name);label.htmlFor='deckOwned-'+index;
        const input=el('input');input.id=label.htmlFor;input.type='number';input.min='0';input.max='99';input.step='1';input.inputMode='numeric';input.placeholder='未確認';input.value=row.owned===null?'':String(row.owned);
        input.setAttribute('aria-label',row.name+'の所持枚数');
        input.dataset.cardName=row.name;
        input.addEventListener('input',()=>{
          if(!input.checkValidity()||(!input.value&&input.validity.badInput)){invalidate();message('所持枚数は0〜99の整数で入力してください。',true);return;}
          if(input.value==='')delete owned[row.name];
          else {const n=Number(input.value);if(!Number.isInteger(n)||n<0||n>99){input.reportValidity();return;}owned[row.name]=n;}
          save();
          const shortage=has(owned,row.name)?Math.max(0,row.required-owned[row.name]):null;
          counts.textContent='必要 '+row.required+'枚 / 不足 '+(shortage===null?'未確認':shortage+'枚');
          rendered=JSON.stringify(plan(current(),owned,[]));
        });
        const counts=el('span','必要 '+row.required+'枚 / 不足 '+(row.shortage===null?'未確認':row.shortage+'枚'),'deck-owned-count');
        line.append(label,input,counts);box.append(line);
      });
    }catch(e){message(e.message,true);}
  }
  function showPreview(){
    // TCG先行カードの名前の一覧が届くまで待つ（届く前に計算すると英語名が購入候補に入る）
    if(global.TcgPreview&&!global.TcgPreview.isReady()){global.TcgPreview.ready.then(showPreview);return;}
    const box=document.getElementById('deckOwnershipConfirm');
    try{
      // autofillやフォーカス中の値も確認時に検証し、表示と保存状態を一致させる。
      const before=JSON.stringify(owned);
      const pending=validateOwned(owned);
      document.querySelectorAll('#deckOwnershipRows input').forEach(input=>{
        if(!input.checkValidity()){input.reportValidity();throw Error('所持枚数は0〜99の整数で入力してください。');}
        if(input.value==='')delete pending[input.dataset.cardName];
        else pending[input.dataset.cardName]=Number(input.value);
      });
      owned=validateOwned(pending);
      if(JSON.stringify(owned)!==before&&!save())return;
      const rows=wishPlan(current(),owned,wishGet(),global._currentSavedDeckId);
      supplement(wishGet(),rows,global._currentSavedDeckId); // 上限違反は確認画面を出す前に通知する
      preview=JSON.stringify(rows);box.replaceChildren();
      const title=el('h3','購入候補への追加内容');title.tabIndex=-1;box.append(title);
      box.append(el('p','候補済みは、このデッキ用とデッキ指定なしの分の合計です（他のデッキ用の分は数えません）。追加分はこのデッキ用・レアリティ指定なしになり、既存の指定は維持します。'));
      rows.forEach(r=>{
        const line=el('div',undefined,'deck-owned-review');line.append(el('strong',r.name));
        line.append(el('span','必要 '+r.required+' / 所持 '+(r.owned===null?'未確認':r.owned)+' / 不足 '+(r.shortage===null?'未確認':r.shortage)+' / 候補済み '+r.candidate+' / 追加 '+r.add+'枚'));
        if(r.rarities.length)line.append(el('small','候補内訳：'+r.rarities.map(c=>(c.rarity||'指定なし')+' '+c.qty+'枚').join('、')));
        box.append(line);
      });
      const unchecked=rows.filter(r=>r.owned===null).length;
      if(unchecked)box.append(el('p','未確認の'+unchecked+'種類は追加しません。所持していないカードには0を入力してください。'));
      const total=rows.reduce((n,r)=>n+r.add,0), actions=el('div',undefined,'deck-owned-actions');
      const commit=el('button',total+'枚を購入候補に追加','deck-btn deck-btn-primary');commit.type='button';commit.disabled=!total;commit.onclick=commitPreview;
      const cancel=el('button','閉じる','deck-btn-text');cancel.type='button';cancel.onclick=invalidate;actions.append(commit,cancel);box.append(actions);
      box.hidden=false;title.focus();
    }catch(e){message(e.message,true);}
  }
  function commitPreview(){
    try{
      const list=wishGet(),rows=wishPlan(current(),owned,list,global._currentSavedDeckId);
      if(!preview||JSON.stringify(rows)!==preview){showPreview();message('デッキまたは購入候補が変わりました。追加内容をもう一度確認してください。',false);return;}
      const next=supplement(list,rows,global._currentSavedDeckId), count=rows.reduce((n,r)=>n+r.add,0);
      if(!count){invalidate();return;}
      wishSave(next);
      // 保存完了後だけ成功を表示。通常のwishAddの加算経路は通さない。
      invalidate();message(count+'枚を購入候補に追加しました。',false);
      if(typeof global.wishOwnershipAdded==='function')global.wishOwnershipAdded(rows.filter(r=>r.add>0));
    }catch(e){message('購入候補に追加できませんでした。'+e.message,true);}
  }
  function set(value){owned=validateOwned(value);rendered='';invalidate();message('',false);}
  function snapshot(){
    const cards=requiredCards(current()),out=Object.create(null);
    cards.forEach((qty,name)=>{if(has(owned,name))out[name]=owned[name];});
    return out;
  }
  function applyReceived(list){
    const deck=list.find(d=>d.id===global._currentSavedDeckId);if(!deck)return;
    set(deck.owned);
    render();
  }
  // 新着から入れたカードを「持っていない（0枚）」にする。入力済みなら変えない（保存は呼び出し側のデッキ更新で行う）
  // デッキに元から入っているカードなら変えない（元の分まで「持っていない」にしないため）
  function markNotOwned(name){
    try{if(!has(owned,name)&&!requiredCards(current()).has(name)){owned[name]=0;rendered='';}}
    catch(e){console.warn('[deck-ownership] 所持枚数を0にできませんでした（デッキの入力欄を確認してください）',e);}
  }
  global.DeckOwnership={validateOwned,requiredCards,plan,wishPlan,supplement,set,snapshot,render,applyReceived,markNotOwned,
    saveError:e=>message('所持枚数を保存できませんでした。'+e.message,true)};
  function init(){
    const box=document.getElementById('deckOwnership');if(!box)return;
    box.addEventListener('toggle',()=>{if(box.open)render();});
    document.getElementById('deckOwnershipPreview').onclick=showPreview;
    render();
  }
  if(document.readyState==='complete')init();else document.addEventListener('DOMContentLoaded',init);
})(window);
