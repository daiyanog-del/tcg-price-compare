// メルカリ参考相場（カード詳細の折りたたみパネル。買取パネル #buyInline と同じ <details class="price-chart"> 形式）。
// 店舗の表とは別に /api/mercari を非同期で取り、描画する。店舗価格の描画は待たない。
// 外部由来の文字列は必ず esc / escAttr / safeUrl を通す。
// esc / escAttr / safeUrl / cssClass は index-main.js のグローバル関数を使う。
// レアリティの選択・店舗結果にあるレアリティは index-main.js が setView() で渡す（再取得せずクライアントで切替）。
(function(){
  let gen=0;              // 世代番号。カードを切り替えたあとに届いた古い応答を捨てる
  let data=null;          // 取得済みの応答（未取得・失敗・クリア後は null）
  let view={rarity:'',stores:[]}; // rarity=選択中のレアリティ（空=すべて）/ stores=店舗結果に存在するレアリティ

  const OTHER_SHOW=3;     // 「他のレアリティ」に並べる数。残りは「ほかN種」

  function box(){return document.getElementById('mercariRef');}
  function bodyEl(){return document.getElementById('mercariRefBody');}

  function yen(n){return '¥'+Number(n).toLocaleString();}

  function rarityOrder(r){
    const cfg=(window.RARITY_CONFIG||{})[r];
    return cfg&&typeof cfg.order==='number'?cfg.order:9999;
  }

  // 表示する行の並び（既存のレアリティ順序。未知のレアリティは末尾）
  function sortedRarities(byRarity){
    return Object.keys(byRarity||{}).sort((a,b)=>rarityOrder(a)-rarityOrder(b));
  }

  // 1枚あたりの最安（単品の最安とセットの1枚あたりの安い方）。どちらも無ければ null
  function bestUnit(v){
    const c=[];
    if(Number(v.count)>0) c.push(Number(v.min));
    if(v.set) c.push(Number(v.set.unit_min));
    return c.length?Math.min.apply(null,c):null;
  }

  // 店舗結果にあるレアリティだけを対象にする。店舗結果が無い（空）ときは絞らない
  function scope(d){
    const all=sortedRarities(d.by_rarity);
    const st=Array.isArray(view.stores)?view.stores:[];
    if(st.length===0) return {rs:all,excluded:[]};
    return {rs:all.filter(r=>st.indexOf(r)>=0),excluded:all.filter(r=>st.indexOf(r)<0)};
  }

  function headNote(d){
    const fetched=Number(d.fetched_count)||0;
    // 最安は関連度上位の取得分の中の最安。全件でないときは母数を明示する
    const part=(fetched>0&&Number(d.num_found)>fetched)?`（関連度上位${fetched}件から集計）`:'';
    return `<p class="buy-inline-note">メルカリで出品中の価格です${part}</p>`;
  }

  // 最下行: メルカリへのリンク（アフィリエイト時はPR表記と rel=sponsored）と他のレアリティ
  function tailNote(d,others){
    const unclassified=Number(d.unclassified_count)||0;
    const parts=[];
    const link=safeUrl(d.search_url);
    if(link){
      const aff=d.affiliate===true;
      parts.push(`<a href="${escAttr(link)}" target="_blank" rel="${aff?'sponsored noopener':'noopener'}">${aff?'メルカリで見る（PR）':'メルカリで見る'}</a>`);
    }
    const list=others.map(r=>({r:r,p:bestUnit(d.by_rarity[r])})).filter(x=>x.p!=null).sort((a,b)=>a.p-b.p);
    if(list.length){
      let s='他のレアリティ: '+list.slice(0,OTHER_SHOW).map(x=>`${esc(x.r)} ${yen(x.p)}〜`).join(' / ');
      if(list.length>OTHER_SHOW) s+=` ほか${list.length-OTHER_SHOW}種`;
      parts.push(s);
    }
    if(unclassified>0) parts.push(`レアリティ判定できず ${unclassified}件`);
    return parts.length?`<p class="buy-inline-note mref-tail">${parts.join('　')}</p>`:'';
  }

  // 店舗に無いレアリティ名を列挙（4種超は先頭3つ＋「ほかN種」）
  function excludedNote(names){
    if(!names.length) return '';
    let s=names.slice(0,OTHER_SHOW).map(esc).join('・');
    if(names.length>OTHER_SHOW) s+=`・ほか${names.length-OTHER_SHOW}種`;
    return `<p class="buy-inline-note">店舗に無いレアリティ（${s}）は表示していません</p>`;
  }

  function tile(label,price,unit,detail){
    return `<div class="scard fin"><div class="slabel">${label}</div>`+
      `<div class="sprice">${price}${unit?`<span class="mref-unit">${unit}</span>`:''}</div>`+
      `<div class="sdetail">${detail}</div></div>`;
  }
  function tileNone(label){
    return `<div class="scard fin"><div class="slabel">${label}</div><div class="sprice mref-none">出品なし</div></div>`;
  }

  // レアリティを1つ選んでいるとき: 「1枚で買う」「N枚組で買う」の2タイル
  function tilesHtml(r,v){
    const name=esc(r);
    let single;
    if(Number(v.count)>0){
      const med=v.median==null?'中央値 —（3件未満）':`中央値 ${yen(v.median)}`;
      single=tile('1枚で買う',yen(v.min),v.min_is_floor?'最低出品価格':'',`${name}・出品${Number(v.count)}件の最安<br>${med}`);
    }else{
      single=tileNone('1枚で買う');
    }
    let set;
    if(v.set){
      const q=Number(v.set.unit_min_qty);
      const total=v.set.unit_min_total!=null?Number(v.set.unit_min_total):Number(v.set.unit_min)*q;
      set=tile(`${q}枚組で買う`,yen(v.set.unit_min),'/枚',`${name}・${q}枚組 ${yen(total)}<br>同じカードだけの組 ${Number(v.set.count)}件`);
    }else{
      set=tileNone('3枚組で買う');
    }
    return `<div class="summary">${single}${set}</div>`;
  }

  // 「すべて」: 各レアリティ1行（札・単品最安・セットの1枚あたり）
  function rowHtml(r,v){
    const hasSingle=Number(v.count)>0;
    const min=hasSingle?yen(v.min)+(v.min_is_floor?'*':''):'—';
    const set=v.set?`<span class="mref-set">${yen(v.set.unit_min)}<small>${Number(v.set.unit_min_qty)}枚組</small></span>`:'<span class="mref-set">—</span>';
    return `<span class="rb rb-${escAttr(cssClass(r))}">${esc(r)}</span><span class="mref-min">${min}</span>${set}`;
  }
  function gridHtml(d,rs){
    let h='<div class="mref-grid"><span class="mref-h"></span><span class="mref-h">単品最安</span><span class="mref-h">セット(1枚)</span>'+
      rs.map(r=>rowHtml(r,d.by_rarity[r])).join('')+'</div>';
    if(rs.some(r=>d.by_rarity[r].min_is_floor&&Number(d.by_rarity[r].count)>0)) h+='<p class="buy-inline-note">* 300円は最低出品価格</p>';
    return h;
  }

  function emptyHtml(msg){
    return `<div class="empty" style="padding:16px 0"><p>${esc(msg)}</p></div>`;
  }

  function show(html){
    const el=box(),body=bodyEl();
    if(!el||!body) return;
    el.classList.remove('hidden');
    body.innerHTML=html;
  }

  function render(){
    const d=data;
    if(!d) return;
    const sc=scope(d);
    const fetched=Number(d.fetched_count)||0;
    // 選択が1つ、または表示対象が1種類だけのときはタイル。「(不明)」選択は比較できないので一覧
    let sel=view.rarity||'';
    if(!sel&&sc.rs.length===1) sel=sc.rs[0];
    if(sel==='(不明)') sel='';
    let h=headNote(d);
    if(sel){
      const v=sc.rs.indexOf(sel)>=0?d.by_rarity[sel]:null;
      h+=v?tilesHtml(sel,v):emptyHtml('このレアリティの出品は見つかりませんでした');
      h+=tailNote(d,sc.rs.filter(r=>r!==sel));
    }else if(sc.rs.length>0){
      h+=gridHtml(d,sc.rs)+tailNote(d,[]);
    }else{
      // 全件が判定不能のときは「出品なし」と言わず、判定できず件数とリンクを出す（取得できたが全件除外は「出品なし」と区別）
      if(!(Number(d.unclassified_count)>0)) h+=emptyHtml(fetched>0||sc.excluded.length>0?'条件に合う出品なし':'出品なし');
      h+=tailNote(d,[]);
    }
    h+=excludedNote(sc.excluded);
    show(h);
  }

  function renderError(busy){
    data=null;
    show('<p class="buy-inline-note">'+(busy?'混雑中です。少し待って再表示してください':'メルカリの情報を取得できませんでした')+'</p>');
  }

  // 選択レアリティ・店舗結果のレアリティが変わったとき（再取得せず描き直す）
  function setView(v){
    view={rarity:(v&&v.rarity)||'',stores:(v&&Array.isArray(v.stores))?v.stores:[]};
    if(data) render();
  }

  function reset(){
    const el=box(),body=bodyEl();
    data=null;
    if(el){el.classList.add('hidden');el.open=false;}
    if(body) body.innerHTML='';
  }

  function clear(){
    gen++;
    reset();
  }

  function load(name){
    if(!box()||!name) return;
    const my=++gen;
    reset();
    fetch('/api/mercari?name='+encodeURIComponent(name)).then(async res=>{
      if(my!==gen) return;
      if(res.status===404){clear();return;} // 未知のカード名は枠ごと出さない
      let d=null;
      try{d=await res.json();}catch(e){d=null;}
      if(my!==gen) return;
      if(res.status===503||(d&&d.error==='busy')){renderError(true);return;} // 混雑中（取得失敗とは文言を分ける）
      if(!res.ok||!d||!d.ok||typeof d.by_rarity!=='object'||d.by_rarity===null){renderError(false);return;}
      data=d;
      render();
    }).catch(()=>{
      if(my!==gen) return;
      renderError(false);
    });
  }

  window.MercariRef={load:load,clear:clear,setView:setView,_render:render,_renderError:renderError};
})();
