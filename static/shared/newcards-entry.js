/* /solitaire?newcards=1&focus=<カード名> の受け口。
   新着の未発売カードを1枚ずつデッキ欄へ読み込み、focus のカードを強調・スクロール・詳細表示する。
   デッキ読込そのものは既存の loadDeck（loadDeckFromNeuron）に任せ、ここでは重複実装しない。 */
// カード名の上限・改行禁止は deck-handoff.js の validateHandoff と同じ。
const CARD_NAME_MAX=50;
export const FOCUS_CLASS='sol-newcard-focus';

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

async function fetchJson(fetchFn,url,init){
  try{
    const res=await fetchFn(url,init);
    return res&&res.ok?await res.json():null;
  }catch{return null;}
}

/** 新着カード名の一覧（最大36件）。取得失敗は空配列。 */
async function fetchNewCardNames(fetchFn){
  const data=await fetchJson(fetchFn,'/api/new-cards');
  return Array.isArray(data?.cards)?data.cards.map(c=>c&&c.name).filter(n=>validateFocus(n)===n):[];
}

/** focus が画像を解決できる（card-images が null を返さない）カードか。 */
async function isResolvable(fetchFn,name){
  const data=await fetchJson(fetchFn,'/api/card-images',{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({names:[name]}),
  });
  const value=data?.images?.[name];
  return !!value&&!(typeof value==='object'&&value.kind==='none');
}

/**
 * @returns {Promise<boolean>} 新着を読み込んだら true（呼び出し側は前回盤面を復元しない）。何もしなければ false。
 */
export async function receiveNewCards({loadDeck,clearDeck,showCard,canShowCard=()=>true,fetchFn=fetch,doc=document,loc=location,hist=history}){
  const url=new URL(loc.href);
  const request=parseNewCardsRequest(url);
  if(!request)return false;
  const names=await fetchNewCardNames(fetchFn);
  let {focus}=request;
  if(focus&&!names.includes(focus)){
    // 新着に無い focus は、解決できるカードだけ先頭に足す。
    if(await isResolvable(fetchFn,focus))names.unshift(focus);else focus=null;
  }
  // 読み込むものが無ければ通常起動（前回盤面の復元も止めない）。
  if(!names.length)return false;
  try{
    if(await clearDeck()===false){console.warn('[newcards-entry] デッキ読込中のため新着の読込を見送りました');return true;}
    await loadDeck(names.map(n=>'1 '+n).join('\n'),'');
  }catch(error){
    // 失敗トーストは sol-deck-load-end が出す。古い盤面を代わりに復元しない。
    console.warn('[newcards-entry] 新着カードの読込失敗',error);
    return true;
  }
  // 再読込で何度も読まないよう、読込後にパラメータを消す。
  url.searchParams.delete('newcards');url.searchParams.delete('focus');
  hist.replaceState(hist.state,'',url.pathname+url.search+url.hash);
  if(focus){
    const target=[...doc.querySelectorAll('#poolRow .tier-item, #poolRow2 .tier-item')].find(el=>el.dataset.cardName===focus);
    const wrapper=target?.closest('.tier-item-wrapper');
    if(wrapper){
      wrapper.classList.add(FOCUS_CLASS);
      wrapper.scrollIntoView({block:'nearest',inline:'center'});
      if(canShowCard())showCard(target);
    }
  }
  return true;
}
