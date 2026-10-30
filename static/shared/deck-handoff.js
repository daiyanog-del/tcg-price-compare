/* 編集中デッキを同じタブ内で一度だけ渡す。保存デッキや同期データは変更しない。 */
export const HANDOFF_KEY='tcgym-deck-handoff';
// 既存マイデッキの枚数上限と、カードAPIの名前上限に合わせる。
const MAIN_MAX=60, EX_MAX=15, CARD_NAME_MAX=50;
// 全75行の名前・JSON構造・デッキ名を含む受付上限。解析前にも適用する。
const PAYLOAD_MAX=12000;

export function validateHandoff(value){
  if(!value||typeof value!=='object'||value.version!==1||typeof value.name!=='string'||value.name.length>200){
    throw new Error('引き継ぐデッキ情報が不正です。元の画面から開き直してください。');
  }
  const result={version:1,name:value.name,main:[],ex:[]};
  // 保存済みのマイデッキから開いたときだけ付く（シミュレーターの「変更を保存」の上書き先）。形が違えば付けないだけ
  if(typeof value.savedId==='string'&&/^[\w-]{1,64}$/.test(value.savedId))result.savedId=value.savedId;
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
  if(!raw)throw new Error('引き継ぐデッキがありません。元の画面から開き直してください。');
  if(raw.length>PAYLOAD_MAX)throw new Error('引き継ぐデッキ情報が大きすぎます。元の画面から開き直してください。');
  return {raw,data:validateHandoff(JSON.parse(raw))};
}

export async function receiveDeckHandoff({loadDeck,clearDeck,getCounts,onLoaded=()=>{},storage=sessionStorage,doc=document,loc=location,hist=history}){
  const url=new URL(loc.href);
  // 共有リプレイは最優先。通常のアクセスでは一時データがあっても取り込まない。
  // 受け取るのはマイデッキ（?from=mydeck）と環境デッキ（?from=meta。2026-10-21「シミュレーターで回す」）から開いたときだけ
  const from=url.searchParams.get('from');
  if(url.searchParams.has('replay')||url.searchParams.has('saved')||url.hash.startsWith('#replay=')||(from!=='mydeck'&&from!=='meta')){
    // サーバーは # を見られないので、?from=mydeck#replay= では空の欄が置かれている。使わないので外す
    doc.getElementById?.('deckHandoffStatus')?.remove();
    return false;
  }
  // 欄はサーバーが最初から置く（templates/solitaire.html・?from=mydeck のとき）。後から差し込むと盤面全体が押し下がり、
  // スマホで CLS が 0.19 出ていた（2026-10-21 計測）。高さは CSS（.sol-handoff-status）で固定し、文の長さでは変えない。
  let panel=doc.getElementById?.('deckHandoffStatus');
  if(!panel){panel=doc.createElement('div');panel.id='deckHandoffStatus';panel.setAttribute('role','status');doc.body.prepend(panel);}
  panel.className='sol-handoff-status';
  // 1行目＝デッキ名・2行目＝状態（スマホ縦）。PC は1行に並べる。はみ出しは CSS で「…」にする。
  // 枚数はスマホ縦では出さない（2行目に収まらず切れる。同じ画面の「デッキ N」「EXデッキ N」に出ている）
  const message=doc.createElement('span');message.className='sol-handoff-text';
  const nameEl=doc.createElement('span');nameEl.className='sol-handoff-name';
  const stateEl=doc.createElement('span');stateEl.className='sol-handoff-state';
  const countsEl=doc.createElement('span');countsEl.className='sol-handoff-counts';
  // 状態の印: 読み込み中は回転する輪（loading.css）、読み込めたら ✓。同じ幅の枠で切り替え、文の幅を変えない（2026-10-30）
  const markEl=doc.createElement('span');markEl.className='sol-handoff-mark';markEl.setAttribute('aria-hidden','true');
  message.append(nameEl,markEl,stateEl,countsEl);
  const retry=doc.createElement('button');retry.type='button';retry.textContent='再試行';retry.hidden=true;retry.className='sol-handoff-retry';
  const back=doc.createElement('a');back.href=from==='meta'?'/#meta':'/#mydeck';back.textContent=from==='meta'?'環境デッキに戻る':'マイデッキに戻る';back.className='sol-handoff-back';
  panel.append(message,retry,back);
  // 失敗の文は長いので折り返して全文を見せる（高さが変わるのは失敗時だけ）
  // 読み込み中も、完了の文（「読み込みました」と枚数）の幅を見えない形で取っておく。文の幅が変わると
  // 「マイデッキに戻る」が横に動き、PC で CLS に数えられていた（2026-10-21）。reserve は CSS の ::after で幅だけに使う
  const show=(name,state,{reserve='',counts='',pending=false,failed=false}={})=>{
    nameEl.textContent=name;stateEl.textContent=state;stateEl.setAttribute('data-reserve',reserve);
    countsEl.textContent=counts;countsEl.className='sol-handoff-counts'+(pending?' is-pending':'');
    markEl.className='sol-handoff-mark'+(pending?' ld-ring':'');markEl.textContent=pending||failed?'':'✓';markEl.hidden=failed;
    panel.classList?.toggle('is-error',failed);
  };
  let running=false;
  async function attempt(){
    if(running)return;
    running=true;retry.disabled=true;retry.hidden=true;
    try{
      const {raw,data}=readHandoff(storage);
      const name=data.name.trim()||'マイデッキ';
      const sum=cards=>cards.reduce((total,card)=>total+card.qty,0);
      const countsText=(main,ex)=>'（メイン'+main+'枚・EX'+ex+'枚）';
      show('「'+name+'」','読み込み中…',{reserve:'読み込みました',counts:countsText(sum(data.main),sum(data.ex)),pending:true});
      if(await clearDeck()===false)throw new Error('別のデッキを読み込み中です。完了後に再試行してください。');
      const text=cards=>cards.map(card=>card.qty+' '+card.name).join('\n');
      await loadDeck(text(data.main),text(data.ex));
      // 既存読込は画像取得失敗のカードをスキップするため、resolveだけで成功扱いしない。
      const actual=getCounts();
      if(actual.main!==sum(data.main)||actual.ex!==sum(data.ex)){
        throw new Error('一部のカードを読み込めませんでした。通信状態とメイン・EXの振り分けを確認して再試行してください。');
      }
      url.searchParams.delete('from');
      hist.replaceState(hist.state,'',url.pathname+url.search+url.hash);
      if(storage.getItem(HANDOFF_KEY)===raw)storage.removeItem(HANDOFF_KEY);
      show('「'+name+'」','読み込みました',{reserve:'読み込みました',counts:countsText(actual.main,actual.ex)});
      onLoaded({name,savedId:data.savedId||null});
    }catch(error){
      console.warn('[deck-handoff] デッキ引継ぎ失敗',error);
      show('',error instanceof SyntaxError?'デッキ情報を読み取れません。元の画面から開き直してください。':('デッキを引き継げませんでした。'+(error.message||'')),{failed:true});
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
