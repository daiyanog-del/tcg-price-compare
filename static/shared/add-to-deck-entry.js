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

// 新着欄（static/js/new-cards.js が /api/new-cards の応答後に描画する）から対象カードの枠を探す。
// 描画待ち（タイムアウト付き）→ 無ければ「もっと見る」を押して先へ進める。見つからなければ null。
export async function findNewCard(doc,name,{timeoutMs=6000,intervalMs=150,sleep=ms=>new Promise(r=>setTimeout(r,ms)),now=()=>Date.now()}={}){
  const deadline=now()+timeoutMs;
  // HTMLの読込前の仮枠（.new-card-skel）は描画済みと数えない（数えると「もっと見る」を空のまま押してしまう）
  const CARD='.new-card:not(.new-card-skel)';
  const find=()=>[...doc.querySelectorAll(CARD)].find(el=>el.dataset&&el.dataset.cardName===name)||null;
  for(let guard=0;guard<100&&now()<deadline;guard++){
    const hit=find();
    if(hit)return hit;
    const rendered=doc.querySelectorAll(CARD).length>0;
    const more=doc.getElementById('newCardsMore');
    if(rendered){
      if(more&&!more.hidden){more.click();continue;} // 6件ずつ表示を増やす（同期で描画される）
      return null; // 全件出ているのに無い
    }
    // 取得失敗・新着0件（描画完了で表示カードなし）は待たずに終える。読み込み中の文言のときだけ待つ。
    if(/取得できませんでした|表示できる未発売カードはありません/.test(doc.getElementById('newCardsStatus')?.textContent||''))return null;
    await sleep(intervalMs);
  }
  return null;
}

// ブラウザで読み込まれたときだけ配線する（node のテストでは document が無いので動かない）。
// deck-edit.js の _init（DOMContentLoaded）より後に走るよう、同じイベントを使う（登録順で後になる）。
if(typeof document!=='undefined'&&typeof window!=='undefined'){
  // 新着欄に無いカード用のフォールバック。ピッカーの起点＋「どのカードを追加するか」の表示。
  // ヘッダ・ナビに重ならないよう画面の中ほどに置き、ピッカーはその下に出る。
  // 配色は index の CSS 変数（--bg-card / --border / --text）。カード名は textContent で入れる。
  const makeChip=name=>{
    const chip=document.createElement('div');
    chip.className='add-to-deck-entry-chip';
    chip.style.cssText='position:fixed;top:40%;left:50%;transform:translateX(-50%);z-index:1100;max-width:min(320px,calc(100vw - 24px));'
      +'box-sizing:border-box;padding:8px 14px;border-radius:var(--radius);background:var(--bg-card);border:1px solid var(--border);'
      +'color:var(--text);font-size:.8rem;line-height:1.4;box-shadow:0 6px 20px rgba(0,0,0,.5);overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
    chip.textContent='追加するカード: '+name;
    chip.title=name;
    document.body.appendChild(chip);
    return chip;
  };
  // チップは「開いた時点のピッカー要素」を覚え、その要素が DOM から外れたら（追加先を選んだ・外側クリック・Esc）消す。
  // 別のピッカーが開いたときに残らないよう、クラスではなく要素そのものを見る。
  const bindChipToPicker=(chip,picker)=>{
    if(!picker){chip.remove();return;}
    const watcher=new MutationObserver(()=>{
      if(!picker.isConnected){chip.remove();watcher.disconnect();}
    });
    watcher.observe(document.body,{childList:true});
  };
  const openWithChip=name=>{
    const chip=makeChip(name);
    window.deckOpenAddPicker(name,chip);
    bindChipToPicker(chip,document.querySelector('.deck-add-picker'));
  };
  const openFor=async name=>{
    if(typeof window.deckOpenAddPicker!=='function'){
      console.warn('[add-to-deck-entry] 追加先ピッカー(deckOpenAddPicker)が未読込です');
      return;
    }
    try{
      // まず新着欄のそのカードを探し、画面中央へ寄せて、既存の「+ デッキに追加」ボタンを押したのと同じ処理で開く。
      const card=await findNewCard(document,name);
      const btn=card&&card.querySelector('.new-card-add');
      // 非表示（offsetParent===null）の枠は位置が取れずピッカーが画面外になるので使わない。
      if(btn&&card.offsetParent!==null){
        card.scrollIntoView({block:'center'});
        btn.click();
        return;
      }
      console.warn('[add-to-deck-entry] 新着欄に表示中の対象カードが無いため、起点チップで追加先を開きます:',name);
    }catch(error){
      console.warn('[add-to-deck-entry] 新着欄からの追加先表示に失敗したため、起点チップで開きます:',error);
    }
    openWithChip(name);
  };
  const start=()=>{receiveAddToDeck({open:openFor});};
  if(document.readyState==='complete')start();
  else document.addEventListener('DOMContentLoaded',start);
}
