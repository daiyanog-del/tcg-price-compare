/* /solitaire?newcards=1&focus=<カード名> の受け口。
   新着の未発売カードを1枚ずつデッキ欄へ読み込み、focus のカードを強調・スクロール・詳細表示する。
   デッキ読込そのものは既存の loadDeck（loadDeckFromNeuron）に任せ、ここでは重複実装しない。 */
// カード名の上限・改行禁止は deck-handoff.js の validateHandoff と同じ。
const CARD_NAME_MAX=50;
export const FOCUS_CLASS='sol-newcard-focus';
// 強調中だけ body に付く。CSS が「強調中は focus 以外を暗くする」のに使う（解除では必ず外す）。
export const ACTIVE_CLASS='sol-newcard-focus-active';

/** focus 名の検証。使えない値は null（focus なしとして扱う）。 */
export function validateFocus(value){
  if(typeof value!=='string')return null;
  const name=value.trim();
  if(!name||[...name].length>CARD_NAME_MAX||/[\r\n]/.test(name))return null;
  return name;
}

/** URL から新着直行の指定を読む。対象外（指定なし・リプレイ指定あり・マイデッキ持込）は null。 */
export function parseNewCardsRequest(url){
  // 共有リプレイ・端末保存が最優先。マイデッキ持込（from=mydeck）とも併用しない。
  if(url.searchParams.has('replay')||url.searchParams.has('saved')||url.hash.startsWith('#replay=')||url.searchParams.has('from'))return null;
  if(url.searchParams.get('newcards')!=='1')return null;
  return {focus:validateFocus(url.searchParams.get('focus'))};
}

// 取得待ちの上限（ミリ秒）。取得が止まったまま通常起動が遅れないようにする。
// TODO: calibrate from data
const FETCH_TIMEOUT_MS=8000;

async function fetchJson(fetchFn,url,init,timeoutMs){
  const ctl=new AbortController();
  const timer=setTimeout(()=>ctl.abort(),timeoutMs);
  try{
    const res=await fetchFn(url,{...init,signal:ctl.signal});
    return res&&res.ok?await res.json():null;
  }catch{return null;}finally{clearTimeout(timer);}
}

/** 新着カード名の一覧（最大36件・同名は1枚）。取得失敗は null（0件の空配列と区別する）。 */
async function fetchNewCardNames(fetchFn,timeoutMs){
  const data=await fetchJson(fetchFn,'/api/new-cards',undefined,timeoutMs);
  if(!Array.isArray(data?.cards))return null;
  return [...new Set(data.cards.map(c=>c&&c.name).filter(n=>validateFocus(n)===n))];
}

/** focus が画像を解決できる（card-images が null を返さない）カードか。 */
async function isResolvable(fetchFn,name,timeoutMs){
  const data=await fetchJson(fetchFn,'/api/card-images',{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({names:[name]}),
  },timeoutMs);
  const value=data?.images?.[name];
  return !!value&&!(typeof value==='object'&&value.kind==='none');
}

/** 強調を全カードから外す（盤面へ移った強調が残らないよう、最初のカード操作などで呼ぶ）。 */
export function clearFocus(doc=document){
  doc.querySelectorAll('.'+FOCUS_CLASS).forEach(el=>el.classList.remove(FOCUS_CLASS));
  doc.body?.classList.remove(ACTIVE_CLASS);
}

// 最初のカード操作（クリック/ドラッグ開始 dragstart/ドロー/リセット）・別のデッキ読込・盤面消去で強調を外す。
const CLEAR_EVENTS=['sol-deck-load-start','sol-board-cleared','sol-replay-reset'];
// pointerdown では外さない: 強調の前面表示（z-index）が消えると、押した瞬間に重なっている隣のカードを掴んでしまう。
const OP_EVENTS=['click','dragstart'];
// 操作とみなす対象: カード・ドロー・リセット・スマホのデッキシートのカード選択と操作ボタン（閉じる/検索は含めない）
const OPERATION_TARGETS='.tier-item, #randomButton, #resetButton, .sol-mobile-deck-cell, #solMobileDeckSheet .sol-mobile-deck-primary-btn, #solMobileDeckSheet .sol-mobile-menu-item';
function clearOnFirstOperation(doc){
  const onPointer=e=>{if(e.target?.closest?.(OPERATION_TARGETS))done();};
  const onEvent=()=>done();
  const done=()=>{
    clearFocus(doc);
    for(const t of OP_EVENTS)doc.removeEventListener(t,onPointer,true);
    for(const t of CLEAR_EVENTS)doc.removeEventListener(t,onEvent);
  };
  for(const t of OP_EVENTS)doc.addEventListener(t,onPointer,true);
  for(const t of CLEAR_EVENTS)doc.addEventListener(t,onEvent);
}

/**
 * @param onLoaded ({focus}) 新着の読込に成功した直後（focus の詳細表示より前）に1回呼ぶ。失敗・中止では呼ばない
 * @param notify (メッセージ, {afterLoad}) 状態表示。afterLoad=読込を試みた後の通知（スマホは既存のトーストが出るので呼び出し側で抑える）
 * @returns {Promise<boolean>} 新着を読み込んだら true（呼び出し側は前回盤面を復元しない）。何もしなければ false。
 */
export async function receiveNewCards({loadDeck,clearDeck,showCard,notify=()=>{},onLoaded=()=>{},fetchFn=fetch,timeoutMs=FETCH_TIMEOUT_MS,doc=document,loc=location,hist=history}){
  const url=new URL(loc.href);
  const request=parseNewCardsRequest(url);
  if(!request)return false;
  // 取得待ちの間に別のデッキ読込が始まったら、新着の読込は中止する（盤面を消さない）。
  let cancelled=false;
  const onOtherLoad=()=>{cancelled=true;};
  doc.addEventListener('sol-deck-load-start',onOtherLoad);
  let names,focus;
  try{
    names=await fetchNewCardNames(fetchFn,timeoutMs);
    if(names===null){notify('新着カードを取得できませんでした。通常の画面で開きます',{});return false;}
    ({focus}=request);
    let focusUnresolved=false;
    if(focus&&!names.includes(focus)){
      // 新着に無い focus は、解決できるカードだけ先頭に足す。
      if(await isResolvable(fetchFn,focus,timeoutMs))names.unshift(focus);else{focusUnresolved=true;focus=null;}
    }
    // 読み込むものが無ければ通常起動（前回盤面の復元も止めない）。
    if(!names.length){notify(focusUnresolved?'指定のカードを読み込めませんでした':'新着の未発売カードはまだありません',{});return false;}
    if(cancelled){console.warn('[newcards-entry] 別のデッキ読込が始まったため新着の読込を中止しました');return false;}
    request.focusUnresolved=focusUnresolved;
  }finally{doc.removeEventListener('sol-deck-load-start',onOtherLoad);}
  try{
    if(await clearDeck()===false){console.warn('[newcards-entry] デッキ読込中のため新着の読込を見送りました');return true;}
    await loadDeck(names.map(n=>'1 '+n).join('\n'),'');
  }catch(error){
    // スマホの失敗トーストは sol-deck-load-end が出す。古い盤面を代わりに復元しない。
    console.warn('[newcards-entry] 新着カードの読込失敗',error);
    notify('新着カードを読み込めませんでした',{afterLoad:true});
    return true;
  }
  // 再読込で何度も読まないよう、読込後にパラメータを消す。
  url.searchParams.delete('newcards');url.searchParams.delete('focus');
  hist.replaceState(hist.state,'',url.pathname+url.search+url.hash);
  if(request.focusUnresolved)notify('指定のカードは読み込めませんでした',{afterLoad:true});
  onLoaded({focus});
  if(focus){
    const target=[...doc.querySelectorAll('#poolRow .tier-item, #poolRow2 .tier-item')].find(el=>el.dataset.cardName===focus);
    const wrapper=target?.closest('.tier-item-wrapper');
    if(wrapper){
      wrapper.classList.add(FOCUS_CLASS);
      doc.body?.classList.add(ACTIVE_CLASS);
      clearOnFirstOperation(doc);
      wrapper.scrollIntoView({block:'nearest',inline:'center'});
      showCard(target);
    }
  }
  return true;
}
