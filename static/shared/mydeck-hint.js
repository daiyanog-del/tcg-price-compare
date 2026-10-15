/* シミュレーターの「自分のデッキに入れて回せる」案内。
   /solitaire?newcards=1 で新着カードを読み込んだ直後だけ出す（通常起動・持込・リプレイでは出さない）。
   - PC: デッキ欄の見出し行に一文＋リンク（対象は focus のカード）
   - スマホ: 未発売カードの詳細シート下部に箱（対象は開いているカード）
   押したときは /?addToDeck=<名> でトップを開く（追加は既存の追加先ピッカーが行う）。 */
import {buildAddToDeckUrl} from './add-to-deck-entry.js';

export const HINT_CLASS='sol-mydeck-hint';
export const BOX_CLASS='sol-mydeck-hint-box';
// 別デッキの読込・盤面消去で案内は役目を終える（新着の文脈ではなくなる）。
const END_EVENTS=['sol-deck-load-start','sol-board-cleared'];

let mobileActive=false;
let endHandler=null;

function link(doc,cls,text,name){
  const href=buildAddToDeckUrl(name);
  if(!href)return null;
  const a=doc.createElement('a');
  a.className=cls;a.href=href;a.textContent=text;
  return a;
}

/** 案内をすべて外す。 */
export function endHint(doc=document){
  mobileActive=false;
  doc.querySelectorAll('.'+HINT_CLASS+', .'+BOX_CLASS).forEach(el=>el.remove());
  if(endHandler){for(const t of END_EVENTS)doc.removeEventListener(t,endHandler);endHandler=null;}
}

/**
 * 新着の読込成功後に呼ぶ。
 * @param focus PC の対象カード名（無ければ PC の一文は出さない＝追加するカードが決まらない）
 * @param showPc PC 配置（デッキ欄の見出し）を出すか。スマホ縦は false（見出しが縦書きの小タブで置けない）
 */
export function startHint({focus,showPc,doc=document}){
  endHint(doc);
  mobileActive=true;
  if(showPc&&focus){
    const label=doc.querySelector('#imagePool .pool-label');
    const a=link(doc,HINT_CLASS+'-link','＋ マイデッキに追加 ›',focus);
    if(label&&a){
      const wrap=doc.createElement('span');
      wrap.className=HINT_CLASS;
      const text=doc.createElement('span');
      text.className=HINT_CLASS+'-text';
      text.textContent='新着の未発売カードを並べています。自分のデッキで回すなら ';
      wrap.append(text,a);
      label.appendChild(wrap);
    }
  }
  endHandler=()=>endHint(doc);
  for(const t of END_EVENTS)doc.addEventListener(t,endHandler);
}

/**
 * スマホの詳細シートの下部に箱を足す（mobile-ui.js の showMobileCardInfo が未発売カードを描いた後に呼ぶ）。
 * 新着で来たあとに開いた別の未発売カードの詳細にも出す（同じ「新しいカードを自分のデッキで回す」文脈のため）。
 * PC の左詳細パネルには呼ばない（幅が狭い）。
 */
export function appendMobileHintBox(body,name,doc=document){
  if(!mobileActive||!body)return;
  body.querySelectorAll('.'+BOX_CLASS).forEach(el=>el.remove());
  const a=link(doc,BOX_CLASS+'-btn','＋ マイデッキに追加',name);
  if(!a)return;
  const box=doc.createElement('div');
  box.className=BOX_CLASS;
  const text=doc.createElement('span');
  text.className=BOX_CLASS+'-text';
  text.textContent='自分のデッキに入れて回すなら';
  box.append(text,a);
  body.appendChild(box);
}
