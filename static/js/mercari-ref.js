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

  // 列をそろえた表。見出し行は1回だけ。300円は最低出品価格なので「¥300*」にして注記へ
  function rowHtml(r,v){
    const min=yen(v.min)+(v.min_is_floor?'*':'');
    const med=v.median==null?'—':yen(v.median);
    return `<span class="rb rb-${escAttr(cssClass(r))}">${esc(r)}</span>`+
      `<span class="mref-min">${min}</span><span class="mref-med">${med}</span>`+
      `<span class="mref-n">${Number(v.count)}</span>`;
  }

  function render(d){
    const el=box();
    if(!el) return;
    el.classList.remove('hidden');
    let h='<div class="mref-title">メルカリ（出品中・参考）</div>';
    const rs=sortedRarities(d.by_rarity);
    const unclassified=Number(d.unclassified_count)||0;
    const fetched=Number(d.fetched_count)||0;
    if(rs.length===0&&unclassified===0){
      // 取得できたが全件が除外された場合は「出品なし」と区別する
      h+=`<div class="mref-note">${fetched>0?'条件に合う出品なし':'出品なし'}</div>`;
    }else{
      if(rs.length>0){
        h+='<div class="mref-grid"><span class="mref-h"></span><span class="mref-h">最安</span><span class="mref-h">中央</span><span class="mref-h">件数</span>'+
          rs.map(r=>rowHtml(r,d.by_rarity[r])).join('')+'</div>';
      }
      const notes=[];
      if(rs.some(r=>d.by_rarity[r].min_is_floor)) notes.push('* 300円は最低出品価格');
      // 最安は関連度上位の取得分の中の最安。全件でないときは母数を明示する
      if(fetched>0&&Number(d.num_found)>fetched) notes.push(`関連度上位${fetched}件から集計`);
      if(notes.length) h+=`<div class="mref-note">${esc(notes.join('・'))}</div>`;
      if(unclassified>0) h+=`<div class="mref-note">レアリティ判定できず ${unclassified}件</div>`;
    }
    const link=safeUrl(d.search_url);
    if(link) h+=`<div class="mref-link"><a href="${escAttr(link)}" target="_blank" rel="noopener">メルカリで見る</a></div>`;
    el.innerHTML=h;
  }

  function renderError(busy){
    const el=box();
    if(!el) return;
    el.classList.remove('hidden');
    el.innerHTML='<div class="mref-title">メルカリ（出品中・参考）</div>'+
      '<div class="mref-note">'+(busy?'混雑中です。少し待って再表示してください':'メルカリの情報を取得できませんでした')+'</div>';
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
      if(res.status===503||(d&&d.error==='busy')){renderError(true);return;} // 混雑中（取得失敗とは文言を分ける）
      if(!res.ok||!d||!d.ok||typeof d.by_rarity!=='object'||d.by_rarity===null){renderError(false);return;}
      render(d);
    }).catch(()=>{
      if(my!==gen) return;
      renderError(false);
    });
  }

  window.MercariRef={load:load,clear:clear,_render:render,_renderError:renderError};
})();
