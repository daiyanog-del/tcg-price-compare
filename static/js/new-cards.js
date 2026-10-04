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
  function render(){
    const grid=get('newCardsGrid');
    grid.replaceChildren();
    cards.slice(0,shown).forEach(card=>{
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
      const url=safeUrl(card.image_url);
      if(url){
        const img=document.createElement('img');
        img.src=url;img.alt='';img.loading='lazy';img.decoding='async';
        img.addEventListener('error',()=>img.remove(),{once:true});
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
      grid.appendChild(link);
    });
    get('newCardsMore').hidden=shown>=cards.length;
    get('newCardsStatus').textContent=cards.length
      ?(truncated&&shown>=cards.length?'新しく追加された36件を表示しています。':'')
      :'現在、表示できる未発売カードはありません。';
  }
  function load(){
    if(pending)return pending;
    const section=get('newCardsSection');
    if(!section)return Promise.resolve();
    section.hidden=false;
    get('newCardsRetry').hidden=true;
    if(!cards.length)get('newCardsStatus').textContent='新着カードを読み込み中…';
    pending=fetch('/api/new-cards').then(async response=>{
      if(!response.ok)throw new Error('新着カード取得失敗');
      const data=await response.json();
      if(!Array.isArray(data.cards))throw new Error('新着カード形式不正');
      cards=data.cards;truncated=!!data.has_more;shown=PAGE_SIZE;render();
    }).catch(()=>{
      // 失敗前の画像を最新情報のように残さない。
      cards=[];get('newCardsGrid').replaceChildren();get('newCardsMore').hidden=true;
      get('newCardsStatus').textContent='新着カードを取得できませんでした。';
      get('newCardsRetry').hidden=false;
    }).finally(()=>{pending=null;});
    return pending;
  }
  document.addEventListener('DOMContentLoaded',()=>{
    get('newCardsMore')?.addEventListener('click',()=>{shown+=PAGE_SIZE;render();});
    get('newCardsRetry')?.addEventListener('click',load);
    if(!document.getElementById('empty')?.classList.contains('hidden'))load();
  });
  return {load};
})();
