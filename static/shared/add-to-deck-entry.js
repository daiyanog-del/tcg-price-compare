/* /?addToDeck=<カード名> の受け口（トップページ用）。
   一人回し（シミュレーター）の「＋ マイデッキに追加」から来た人に、既存の追加先ピッカー
   （deck-edit.js の deckOpenAddPicker＝編集中/保存済み/新規を選ぶ）を開く。
   追加の処理そのものは作らず、既存の関数を呼ぶだけ。 */
// カード名の上限・改行禁止は newcards-entry.js の validateFocus・deck-handoff.js と同じ。
const CARD_NAME_MAX=50;
export const PARAM='addToDeck';

/** カード名の検証。使えない値は null。 */
export function validateCardName(value){
  if(typeof value!=='string')return null;
  const name=value.trim();
  if(!name||[...name].length>CARD_NAME_MAX||/[\r\n]/.test(name))return null;
  return name;
}

/** トップページを開く URL（追加先ピッカー付き）。名前が使えなければ null。 */
export function buildAddToDeckUrl(name){
  const valid=validateCardName(name);
  return valid?'/?'+PARAM+'='+encodeURIComponent(valid):null;
}

/**
 * URL にパラメータがあれば消し、有効なカード名で open を呼ぶ。
 * @returns {boolean} open を呼んだら true。
 */
export function receiveAddToDeck({open,loc=location,hist=history}){
  const url=new URL(loc.href);
  if(!url.searchParams.has(PARAM))return false;
  const name=validateCardName(url.searchParams.get(PARAM));
  // 再読込やブックマークで何度も開かないよう、先にパラメータを消す。
  url.searchParams.delete(PARAM);
  hist.replaceState(hist.state,'',url.pathname+url.search+url.hash);
  if(!name){console.warn('[add-to-deck-entry] カード名が不正なため追加先を開きませんでした');return false;}
  open(name);
  return true;
}

// ブラウザで読み込まれたときだけ配線する（node のテストでは document が無いので動かない）。
// deck-edit.js の _init（DOMContentLoaded）より後に走るよう、同じイベントを使う（登録順で後になる）。
if(typeof document!=='undefined'&&typeof window!=='undefined'){
  // ピッカーの起点＋「どのカードを追加するか」の表示。ピッカーだけでは対象のカードが分からないため。
  // 配色は index の CSS 変数（--bg-card / --border / --text）。カード名は textContent で入れる。
  const makeChip=name=>{
    const chip=document.createElement('div');
    chip.className='add-to-deck-entry-chip';
    chip.style.cssText='position:fixed;top:16px;left:50%;transform:translateX(-50%);z-index:1100;max-width:min(320px,calc(100vw - 24px));'
      +'box-sizing:border-box;padding:8px 14px;border-radius:var(--radius);background:var(--bg-card);border:1px solid var(--border);'
      +'color:var(--text);font-size:.8rem;line-height:1.4;box-shadow:0 6px 20px rgba(0,0,0,.5);overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
    chip.textContent='追加するカード: '+name;
    chip.title=name;
    document.body.appendChild(chip);
    // ピッカーが閉じたら（追加先を選んだ・外側クリック・Esc）一緒に消す。
    const watcher=new MutationObserver(()=>{
      if(!document.querySelector('.deck-add-picker')){chip.remove();watcher.disconnect();}
    });
    watcher.observe(document.body,{childList:true});
    return chip;
  };
  const start=()=>{
    receiveAddToDeck({open:name=>{
      if(typeof window.deckOpenAddPicker!=='function'){
        console.warn('[add-to-deck-entry] 追加先ピッカー(deckOpenAddPicker)が未読込です');
        return;
      }
      const chip=makeChip(name);
      window.deckOpenAddPicker(name,chip);
      // ピッカーが開かなかった（名前が不正など）ときに表示だけが残らないようにする。
      if(!document.querySelector('.deck-add-picker'))chip.remove();
    }});
  };
  if(document.readyState==='complete')start();
  else document.addEventListener('DOMContentLoaded',start);
}
