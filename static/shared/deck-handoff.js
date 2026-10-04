/* 編集中デッキを同じタブ内で一度だけ渡す。保存デッキや同期データは変更しない。 */
export const HANDOFF_KEY='tcgym-deck-handoff';
// 既存マイデッキの枚数上限と、カードAPIの名前上限に合わせる。
const MAIN_MAX=60, EX_MAX=15, CARD_NAME_MAX=50;
// 全75行の名前・JSON構造・デッキ名を含む受付上限。解析前にも適用する。
const PAYLOAD_MAX=12000;

export function validateHandoff(value){
  if(!value||typeof value!=='object'||value.version!==1||typeof value.name!=='string'||value.name.length>200){
    throw new Error('引き継ぐデッキ情報が不正です。マイデッキから開き直してください。');
  }
  const result={version:1,name:value.name,main:[],ex:[]};
  for(const [section,limit] of [['main',MAIN_MAX],['ex',EX_MAX]]){
    const cards=value[section];
    if(!Array.isArray(cards)||cards.length>limit)throw new Error('メインは60枚、EXは15枚以内にしてください。');
    let total=0;
    for(const card of cards){
      if(!card||typeof card.name!=='string'||!card.name.trim()||[...card.name].length>CARD_NAME_MAX||/[\r\n]/.test(card.name)||!Number.isInteger(card.qty)||card.qty<1){
        throw new Error('カード名と枚数を確認してください。カード名は50文字以内、枚数は1以上の整数です。');
      }
      total+=card.qty;
      if(total>limit)throw new Error('メインは60枚、EXは15枚以内にしてください。');
      result[section].push({name:card.name.trim(),qty:card.qty});
    }
  }
  if(!result.main.length&&!result.ex.length)throw new Error('デッキにカードを追加してください。');
  return result;
}

export function storeHandoff(value,storage=sessionStorage){
  const data=validateHandoff(value);
  const raw=JSON.stringify(data);
  if(raw.length>PAYLOAD_MAX)throw new Error('引き継ぐデッキ情報が大きすぎます。');
  storage.setItem(HANDOFF_KEY,raw);
}

function readHandoff(storage){
  const raw=storage.getItem(HANDOFF_KEY);
  if(!raw)throw new Error('引き継ぐデッキがありません。マイデッキから開き直してください。');
  if(raw.length>PAYLOAD_MAX)throw new Error('引き継ぐデッキ情報が大きすぎます。マイデッキから開き直してください。');
  return {raw,data:validateHandoff(JSON.parse(raw))};
}

export async function receiveDeckHandoff({loadDeck,clearDeck,getCounts,storage=sessionStorage,doc=document,loc=location,hist=history}){
  const url=new URL(loc.href);
  // 共有リプレイは最優先。通常のアクセスでは一時データがあっても取り込まない。
  if(url.searchParams.has('replay')||url.hash.startsWith('#replay=')||url.searchParams.get('from')!=='mydeck')return false;
  const panel=doc.createElement('div');panel.setAttribute('role','status');
  panel.id='deckHandoffStatus';
  panel.style.cssText='padding:8px 12px;font-size:14px;line-height:1.6;overflow-wrap:anywhere;background:var(--bg-card,#171f31);color:var(--main-color,#eff3fb)';
  const message=doc.createElement('span');
  // 一人回しの汎用span色を上書きし、通知パネルの文字色を引き継ぐ。
  message.style.color='inherit';
  const retry=doc.createElement('button');retry.type='button';retry.textContent='再試行';retry.hidden=true;
  const back=doc.createElement('a');back.href='/#mydeck';back.textContent='マイデッキに戻る';
  back.style.cssText='margin-left:12px;color:var(--accent,#aec5ff);text-decoration:underline;display:inline-block;padding:8px 0;outline-offset:3px';
  retry.style.cssText='margin-left:12px;padding:8px 12px;min-height:40px;color:var(--main-color,#eff3fb);background:transparent;border:1px solid currentColor;border-radius:4px;font:inherit;cursor:pointer;outline-offset:3px';
  panel.append(message,retry,back);doc.body.prepend(panel);
  let running=false;
  async function attempt(){
    if(running)return;
    running=true;retry.disabled=true;retry.hidden=true;
    try{
      const {raw,data}=readHandoff(storage);
      const name=data.name.trim()||'マイデッキ';
      message.textContent='「'+name+'」を読み込み中…';
      if(await clearDeck()===false)throw new Error('別のデッキを読み込み中です。完了後に再試行してください。');
      const text=cards=>cards.map(card=>card.qty+' '+card.name).join('\n');
      await loadDeck(text(data.main),text(data.ex));
      // 既存読込は画像取得失敗のカードをスキップするため、resolveだけで成功扱いしない。
      const actual=getCounts();
      const sum=cards=>cards.reduce((total,card)=>total+card.qty,0);
      if(actual.main!==sum(data.main)||actual.ex!==sum(data.ex)){
        throw new Error('一部のカードを読み込めませんでした。通信状態とメイン・EXの振り分けを確認して再試行してください。');
      }
      url.searchParams.delete('from');
      hist.replaceState(hist.state,'',url.pathname+url.search+url.hash);
      if(storage.getItem(HANDOFF_KEY)===raw)storage.removeItem(HANDOFF_KEY);
      message.textContent='「'+name+'」を読み込みました（メイン'+actual.main+'枚・EX'+actual.ex+'枚）。';
    }catch(error){
      console.warn('[deck-handoff] デッキ引継ぎ失敗',error);
      message.textContent=error instanceof SyntaxError?'デッキ情報を読み取れません。マイデッキから開き直してください。':('デッキを引き継げませんでした。'+(error.message||''));
      retry.hidden=false;
    }finally{
      running=false;retry.disabled=false;
    }
  }
  retry.addEventListener('click',attempt);
  await attempt();
  // 明示した持込が失敗しても、別の古い盤面を代わりに復元しない。
  return true;
}
