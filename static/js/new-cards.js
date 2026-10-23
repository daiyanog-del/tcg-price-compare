/* 新着カードから既存の詳細・デッキ編集へつなぐ。保存処理はここに重複実装しない。 */
window.NewCards = (() => {
  // 最初に出す枚数（もっと見るの増分も同じ）。トップは PC の1列8枚（スマホは横に流れる1列）・一覧ページは PC の1段6枚
  // （2026-10-22 トップの見直し: 小さめのカード8枚にして値動きを最初の画面に入れる）
  const PAGE_SIZE=isListPage()?6:8;
  let pending=null, cards=[], shown=PAGE_SIZE, truncated=false;
  // どこから取った（取っている）一覧か。同じ HTML のままトップ・未発売一覧・海外先行一覧を行き来するので、
  // 取得中に別のページへ移ったら古い応答は捨てる（捨てないと未発売カードに「TCG先行」の札が付く。2026-10-23 reviewer 指摘）
  let pendingKey='', loadedKey='', loadGen=0;
  function sourceKey(){return isTcgPage()?'/api/tcg-preview-cards':isListPage()?'/api/new-cards?all=1':'/api/new-cards';}
  // 埋め込みの一覧で描いた時刻。直後に別の処理（index-main.js の _loadTopPanels）が load() を呼んでも、
  // この間は取り直さない（取り直すと全部描き直し、一覧ページでは最初の絵が後回しになって LCP が遅れた。2026-10-22）
  const INITIAL_FRESH_MS=30000;
  let initialUsedAt=-Infinity;
  const get=id=>document.getElementById(id);
  function addedDate(value){
    if(!value)return '';
    const d=new Date(value);
    if(Number.isNaN(d.getTime()))return '';
    return new Intl.DateTimeFormat('ja-JP',{timeZone:'Asia/Tokyo',month:'2-digit',day:'2-digit'}).format(d)+' 追加';
  }
  // 一覧ページ（/new-cards）かどうか。index-main.js が #mode-search のクラスで状態を持つ。
  function isListPage(){return !!document.getElementById('mode-search')?.classList.contains('new-cards-page');}
  // 海外先行カード一覧（/tcg-preview・2026-10-23）。英語版で先に出て日本に無いカード（TCG先行）を同じ部品で並べる
  function isTcgPage(){return !!document.getElementById('mode-search')?.classList.contains('tcg-preview-page');}
  function releaseLabel(value){
    const m=/^(\d{4})-(\d{2})-(\d{2})/.exec(value||'');
    if(!m)return '';
    // 英語版の発売日は過ぎた日のことが多いので、年を付けて「英語版」と明記する
    if(isTcgPage())return '英語版 '+m[1]+'/'+m[2]+'/'+m[3]+' 発売';
    // 翌年以降の発売日は月日だけだと今年と誤読されるため年を付ける（JST基準の今年と比較）。
    const thisYear=new Intl.DateTimeFormat('en',{timeZone:'Asia/Tokyo',year:'numeric'}).format(new Date());
    return (m[1]!==thisYear?m[1]+'/':'')+m[2]+'/'+m[3]+' 発売';
  }
  // カード1枚分（詳細リンク＋デッキ追加ボタン）。<a>の中にbuttonを入れないため外枠で包む。
  // eager=true は最初から見える枠（トップの最初の6枚）。LCP の要素なので遅延読み込みにしない。
  function cardElement(card,eager=false){
    const wrap=document.createElement('div');
    wrap.className='new-card';
    wrap.dataset.cardName=card.name; // /?addToDeck=<名> の受け口（add-to-deck-entry.js）が、対象カードの枠を探すのに使う
    const link=document.createElement('a');
    link.className='new-card-link';
    link.href='/card/'+encodeURIComponent(card.name);
    link.setAttribute('aria-label',card.name+'の詳細を見る');
    const picture=document.createElement('span');
    picture.className='new-card-picture';
    const placeholder=document.createElement('span');
    placeholder.className='new-card-placeholder';
    placeholder.textContent=card.name;
    picture.appendChild(placeholder);
    // 新着欄は小さく並べるので縮小版（WebP）を優先する。無いカードは原寸のまま。
    const thumb=safeUrl(card.thumb_url),full=safeUrl(card.image_url);
    const url=thumb||full;
    if(url){
      const img=document.createElement('img');
      img.src=url;img.alt='';img.loading=eager?'eager':'lazy';img.decoding='async';
      // 縮小版が読めなければ原寸に1回だけ切り替え、それも駄目ならカード名の札に戻す
      let fellBack=!(thumb&&full&&thumb!==full);
      img.addEventListener('error',()=>{
        if(!fellBack){fellBack=true;img.src=full;return;}
        img.remove();
      });
      picture.appendChild(img);
    }
    const meta=document.createElement('span');meta.className='new-card-meta';
    const badge=document.createElement('span');badge.className='new-card-badge';badge.textContent='発売前';
    if(isTcgPage()){badge.textContent='TCG先行';badge.classList.add('is-tcg');}
    const date=document.createElement('span');date.textContent=addedDate(card.added_at);
    meta.append(badge,date);
    const name=document.createElement('strong');name.textContent=card.name;
    link.append(picture,meta,name);
    link.addEventListener('click',event=>{
      if(!rankingLinkClick(event))return;
      document.getElementById('q').value=card.name;
      // 詳細を開く時点で発売日が来ていた場合も、既存の検証で正しく判定する。
      doSearch({trigger:'new_card'});
    });
    const add=document.createElement('button');
    add.type='button';
    add.className='new-card-add';
    add.textContent='+ デッキに追加';
    // 保存処理は既存の addToDeck（追加先ピッカー）に任せる。
    add.addEventListener('click',()=>{_ga('new_card_add_deck');addToDeck(card.name,add,{notOwned:true});});
    wrap.append(link,add);
    return wrap;
  }
  // 商品名ごとに分ける。並びは「グループ内で最も新しい追加日時」の新しい順、商品未定は最後。
  function groupByProduct(list){
    const map=new Map();
    list.forEach(card=>{
      const key=card.product_name||'';
      if(!map.has(key))map.set(key,{product:key,cards:[],latest:-Infinity,release:''});
      const g=map.get(key);
      g.cards.push(card);
      const t=card.added_at?Date.parse(card.added_at):NaN;
      if(!Number.isNaN(t)&&t>g.latest)g.latest=t;
      // 日付がばらつく場合は最も早い日付（ISO形式なので文字列比較でよい）。
      if(card.release_date&&(!g.release||card.release_date<g.release))g.release=card.release_date;
    });
    return [...map.values()].sort((a,b)=>{
      if(!a.product!==!b.product)return a.product?-1:1;
      // 海外先行カードは取り込みが週1回でまとまるので、追加日時ではなく英語版の発売日の新しい順
      if(isTcgPage())return (b.release||'').localeCompare(a.release||'');
      return b.latest-a.latest;
    });
  }
  function renderGrouped(grid){
    let eagerLeft=PAGE_SIZE;
    groupByProduct(cards).forEach(g=>{
      const section=document.createElement('section');
      section.className='new-card-group';
      const heading=document.createElement('h3');
      heading.className='new-card-group-title';
      const title=document.createElement('span');
      title.textContent=(g.product||'収録商品未定')+'（'+g.cards.length+'件）';
      heading.appendChild(title);
      const rel=releaseLabel(g.release);
      if(rel){
        const r=document.createElement('span');
        r.className='new-card-group-date';
        r.textContent=rel;
        heading.appendChild(r);
      }
      const inner=document.createElement('div');
      inner.className='new-cards-grid';
      // 一覧ページでも最初に見える枚数（PAGE_SIZE）はすぐ読む（LCP の要素。2026-10-22）
      g.cards.forEach(card=>inner.appendChild(cardElement(card,eagerLeft-->0)));
      section.append(heading,inner);
      grid.appendChild(section);
    });
  }
  function render(){
    const grid=get('newCardsGrid');
    const list=isListPage();
    grid.replaceChildren();
    grid.removeAttribute('aria-busy');
    grid.classList.toggle('is-grouped',list);
    get('newCardsHeading').textContent=isTcgPage()?'海外先行カード一覧':list?'未発売カード一覧':'新着の未発売カード';
    if(list){
      renderGrouped(grid);
    }else{
      cards.slice(0,shown).forEach((card,i)=>grid.appendChild(cardElement(card,i<PAGE_SIZE)));
    }
    get('newCardsMore').hidden=list||shown>=cards.length;
    // 36件を超える分がある時だけ、全件の一覧ページへ案内する。
    const showAll=!list&&cards.length>0&&truncated&&shown>=cards.length;
    get('newCardsAll').hidden=!showAll;
    get('newCardsStatus').textContent=cards.length
      ?''
      :isTcgPage()?'現在、表示できる海外先行カードはありません。':'現在、表示できる未発売カードはありません。';
  }
  function load(){
    const key=sourceKey();
    if(pending&&pendingKey===key)return pending;
    if(!pending&&cards.length&&loadedKey===key&&Date.now()-initialUsedAt<INITIAL_FRESH_MS)return Promise.resolve();
    const section=get('newCardsSection');
    if(!section)return Promise.resolve();
    section.hidden=false;
    get('newCardsRetry').hidden=true;
    // HTMLの仮枠（または前回の表示）が残っている間は文言を足さない（足すと枠の下が押し下げられる）
    if(!get('newCardsGrid').children.length)get('newCardsStatus').textContent='新着カードを読み込み中…';
    // サーバーが HTML に埋め込んだ最初の一覧（app.py index / new_cards_page）があれば、API を待たずに使う（一度だけ。
    // 再試行などでは API から取り直す）。DB を待たないと決まらないときは埋め込まれない（card_display.cached_new_cards）
    const initialEl=get('newCardsInitial');
    let initial=null;
    if(initialEl){try{initial=JSON.parse(initialEl.textContent);}catch{initial=null;}initialEl.remove();}
    const useInitial=!!(initial&&Array.isArray(initial.cards));
    if(useInitial)initialUsedAt=Date.now();
    const source=useInitial
      ?Promise.resolve({ok:true,json:async()=>initial})
      :fetch(key);
    const gen=++loadGen;
    pendingKey=key;
    const request=source.then(async response=>{
      if(gen!==loadGen)return; // 取得中に別のページへ移った（新しい取得が始まっている）
      if(!response.ok)throw new Error('新着カード取得失敗');
      const data=await response.json();
      if(gen!==loadGen)return;
      if(!Array.isArray(data.cards))throw new Error('新着カード形式不正');
      cards=data.cards;truncated=!!data.has_more;shown=PAGE_SIZE;loadedKey=key;render();
    }).catch(()=>{
      if(gen!==loadGen)return;
      // 失敗前の画像を最新情報のように残さない。
      cards=[];get('newCardsGrid').replaceChildren();get('newCardsGrid').removeAttribute('aria-busy');get('newCardsMore').hidden=true;
      get('newCardsAll').hidden=true;
      get('newCardsStatus').textContent=isTcgPage()?'海外先行カードを取得できませんでした。':'新着カードを取得できませんでした。';
      get('newCardsRetry').hidden=false;
    }).finally(()=>{if(pending===request)pending=null;});
    pending=request;
    return pending;
  }
  document.addEventListener('DOMContentLoaded',()=>{
    // 読込前（仮枠の間）に押されても空の一覧で描き直さない
    get('newCardsMore')?.addEventListener('click',()=>{if(!cards.length)return;shown+=PAGE_SIZE;render();});
    get('newCardsRetry')?.addEventListener('click',load);
    if(!document.getElementById('empty')?.classList.contains('hidden'))load();
  });
  return {load};
})();
