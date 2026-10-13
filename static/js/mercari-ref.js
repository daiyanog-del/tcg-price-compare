// メルカリ参考相場（カード詳細の別枠）。店舗の表とは別に /api/mercari を非同期で取り、
// 描画する。店舗価格の描画は待たない。外部由来の文字列は必ず esc / escAttr / safeUrl を通す。
// esc / escAttr / safeUrl / cssClass は index-main.js のグローバル関数を使う。
(function(){
  let gen=0; // 世代番号。カードを切り替えたあとに届いた古い応答を捨てる

  function box(){return document.getElementById('mercariRef');}

  function yen(n){return '¥'+Number(n).toLocaleString();}

  function rarityOrder(r){
    const cfg=(window.RARITY_CONFIG||{})[r];
    return cfg&&typeof cfg.order==='number'?cfg.order:9999;
  }

  // 表示する行の並び（既存のレアリティ順序。未知のレアリティは末尾）
  function sortedRarities(byRarity){
    return Object.keys(byRarity||{}).sort((a,b)=>rarityOrder(a)-rarityOrder(b));
  }

  function rowHtml(r,v){
    const min=v.min_is_floor?yen(v.min)+'（最低出品価格）':yen(v.min);
    const med=v.median==null?'':`<span class="mref-med">中央 ${yen(v.median)}</span>`;
    return `<div class="mref-row"><span class="rb rb-${escAttr(cssClass(r))}">${esc(r)}</span>`+
      `<span class="mref-min">最安 ${min}</span>${med}<span class="mref-n">（${Number(v.count)}件）</span></div>`;
  }

  function render(d){
    const el=box();
    if(!el) return;
    el.classList.remove('hidden');
    let h='<div class="mref-title">メルカリ（出品中・参考）</div>';
    const rs=sortedRarities(d.by_rarity);
    const unclassified=Number(d.unclassified_count)||0;
    if(rs.length===0&&unclassified===0){
      h+='<div class="mref-note">出品なし</div>';
    }else{
      h+=rs.map(r=>rowHtml(r,d.by_rarity[r])).join('');
      if(unclassified>0) h+=`<div class="mref-note">レアリティ判定できず ${unclassified}件</div>`;
    }
    const link=safeUrl(d.search_url);
    if(link) h+=`<div class="mref-link"><a href="${escAttr(link)}" target="_blank" rel="noopener">メルカリで見る</a></div>`;
    el.innerHTML=h;
  }

  function renderError(){
    const el=box();
    if(!el) return;
    el.classList.remove('hidden');
    el.innerHTML='<div class="mref-title">メルカリ（出品中・参考）</div>'+
      '<div class="mref-note">メルカリの情報を取得できませんでした</div>';
  }

  function clear(){
    gen++;
    const el=box();
    if(!el) return;
    el.classList.add('hidden');
    el.innerHTML='';
  }

  function load(name){
    const el=box();
    if(!el||!name) return;
    const my=++gen;
    el.classList.add('hidden');
    el.innerHTML='';
    fetch('/api/mercari?name='+encodeURIComponent(name)).then(async res=>{
      if(my!==gen) return;
      if(res.status===404){clear();return;} // 未知のカード名は枠ごと出さない
      let d=null;
      try{d=await res.json();}catch(e){d=null;}
      if(my!==gen) return;
      if(!res.ok||!d||!d.ok||typeof d.by_rarity!=='object'||d.by_rarity===null){renderError();return;}
      render(d);
    }).catch(()=>{
      if(my!==gen) return;
      renderError();
    });
  }

  window.MercariRef={load:load,clear:clear,_render:render,_renderError:renderError};
})();
