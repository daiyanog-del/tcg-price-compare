/* 新着カードから既存の詳細・デッキ編集へつなぐ。保存処理はここに重複実装しない。 */
window.NewCards = (() => {
  const PAGE_SIZE=6; // PCの1段分。スマホでは3列×2段。
  let pending=null, cards=[], shown=PAGE_SIZE, truncated=false;
  const get=id=>document.getElementById(id);
  function addedDate(value){
    if(!value)return '';
    const d=new Date(value);
    if(Number.isNaN(d.getTime()))return '';
    return new Intl.DateTimeFormat('ja-JP',{timeZone:'Asia/Tokyo',month:'2-digit',day:'2-digit'}).format(d)+' 追加';
  }
  // 一覧ページ（/new-cards）かどうか。index-main.js が #mode-search のクラスで状態を持つ。
  function isListPage(){return !!document.getElementById('mode-search')?.classList.contains('new-cards-page');}
  function releaseLabel(value){
    const m=/^(\d{4})-(\d{2})-(\d{2})/.exec(value||'');
    if(!m)return '';
    // 翌年以降の発売日は月日だけだと今年と誤読されるため年を付ける（JST基準の今年と比較）。
    const thisYear=new Intl.DateTimeFormat('en',{timeZone:'Asia/Tokyo',year:'numeric'}).format(new Date());
    return (m[1]!==thisYear?m[1]+'/':'')+m[2]+'/'+m[3]+' 発売';
  }
  // カード1枚分（詳細リンク＋デッキ追加ボタン）。<a>の中にbuttonを入れないため外枠で包む。
  function cardElement(card){
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
      img.src=url;img.alt='';img.loading='lazy';img.decoding='async';
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
      return b.latest-a.latest;
    });
  }
  function renderGrouped(grid){
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
      g.cards.forEach(card=>inner.appendChild(cardElement(card)));
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
    get('newCardsHeading').textContent=list?'未発売カード一覧':'新着の未発売カード';
    if(list){
      renderGrouped(grid);
    }else{
      cards.slice(0,shown).forEach(card=>grid.appendChild(cardElement(card)));
    }
    get('newCardsMore').hidden=list||shown>=cards.length;
    // 36件を超える分がある時だけ、全件の一覧ページへ案内する。
    const showAll=!list&&cards.length>0&&truncated&&shown>=cards.length;
    get('newCardsAll').hidden=!showAll;
    get('newCardsStatus').textContent=cards.length
      ?''
      :'現在、表示できる未発売カードはありません。';
  }
  function load(){
    if(pending)return pending;
    const section=get('newCardsSection');
    if(!section)return Promise.resolve();
    section.hidden=false;
    get('newCardsRetry').hidden=true;
    // HTMLの仮枠（または前回の表示）が残っている間は文言を足さない（足すと枠の下が押し下げられる）
    if(!get('newCardsGrid').children.length)get('newCardsStatus').textContent='新着カードを読み込み中…';
    const list=isListPage();
    pending=fetch(list?'/api/new-cards?all=1':'/api/new-cards').then(async response=>{
      if(!response.ok)throw new Error('新着カード取得失敗');
      const data=await response.json();
      if(!Array.isArray(data.cards))throw new Error('新着カード形式不正');
      cards=data.cards;truncated=!!data.has_more;shown=PAGE_SIZE;render();
    }).catch(()=>{
      // 失敗前の画像を最新情報のように残さない。
      cards=[];get('newCardsGrid').replaceChildren();get('newCardsGrid').removeAttribute('aria-busy');get('newCardsMore').hidden=true;
      get('newCardsAll').hidden=true;
      get('newCardsStatus').textContent='新着カードを取得できませんでした。';
      get('newCardsRetry').hidden=false;
    }).finally(()=>{pending=null;});
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
