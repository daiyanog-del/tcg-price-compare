// メルカリ参考相場（カード詳細の折りたたみパネル。買取パネル #buyInline と同じ <details class="price-chart"> 形式）。
// 店舗の表とは別に /api/mercari を非同期で取り、描画する。店舗価格の描画は待たない。
// 外部由来の文字列は必ず esc / escAttr / safeUrl を通す。
// esc / escAttr / safeUrl / cssClass は index-main.js のグローバル関数を使う。
// レアリティの選択・店舗結果にあるレアリティは index-main.js が setView() で渡す（再取得せずクライアントで切替）。
(function(){
  let gen=0;              // 世代番号。カードを切り替えたあとに届いた古い応答を捨てる
  let data=null;          // 取得済みの応答（未取得・失敗・クリア後は null）
  let view={rarity:'',stores:[],pending:false}; // rarity=選択中のレアリティ（空=すべて）/ stores=店舗結果に存在するレアリティ / pending=店舗検索が終わっていない
  let autoOpened=false;   // このカードで既に自動で開いたか（ユーザーが閉じた後に開き直さない）
  let lastHtml='';        // 直近に描いたHTML。同じ内容なら DOM を触らない（開いたまま描き直してもちらつき・選択解除を起こさない）

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

  // 店舗結果にあるレアリティだけを対象にする。店舗検索が終わった後に店舗結果が無い（空）ときは絞らない。
  // 店舗検索の途中（pending）は、まだ店舗で確認できていないレアリティを「確認中」として分ける（空でも全部が確認中）
  function scope(d){
    const all=sortedRarities(d.by_rarity);
    const st=Array.isArray(view.stores)?view.stores:[];
    if(st.length===0&&!view.pending) return {rs:all,excluded:[]};
    return {rs:all.filter(r=>st.indexOf(r)>=0),excluded:all.filter(r=>st.indexOf(r)<0)};
  }

  // 価格の数字をメルカリ検索へのリンクにする（店舗タイルの .scard-link と同じ作り。アフィリエイト時は rel=sponsored）。
  // リンクが安全でないときは数字だけ出す
  function priceLink(d,text){
    const link=safeUrl(d.search_url);
    if(!link) return text;
    return `<a class="scard-link" href="${escAttr(link)}" target="_blank" rel="${d.affiliate===true?'sponsored noopener':'noopener'}">${text}</a>`;
  }

  function tileHtml(label,price,detail){
    return `<div class="scard fin"><div class="slabel">${esc(label)}</div><div class="sprice">${price}</div><div class="sdetail">${detail}</div></div>`;
  }

  // レアリティごとに1グループ: 見出し（レアリティ名・札にしない）＋「1枚」「N枚組」の2セル。
  // 片方が無いときは空セル（何も描かない）で位置を保ち、全グループで1枚が左・N枚組が右に揃う
  // 見出しは「すべて」等の複数レアリティ表示のときだけ（特定レアリティ選択時はページ上部の絞り込みバナーに出ているため省く）
  function group(d,r,withHead){
    const v=d.by_rarity[r];
    const set=v.set||null;
    let one='<div class="mref-empty"></div>',multi='<div class="mref-empty"></div>';
    if(Number(v.count)>0){
      const detail=v.median==null?`${Number(v.count)}件`:`中央 ${yen(v.median)}・${Number(v.count)}件`;
      one=tileHtml('1枚',priceLink(d,yen(v.min)),detail);
    }
    if(set){
      const q=Number(set.unit_min_qty);
      const total=set.unit_min_total!=null?Number(set.unit_min_total):Number(set.unit_min)*q;
      multi=tileHtml(`${q}枚組`,priceLink(d,yen(set.unit_min))+'<span class="mref-unit">/枚</span>',`${q}枚 ${yen(total)}・${Number(set.count)}件`);
    }
    return `<div class="mref-group">${withHead?`<div class="mref-ghead">${esc(r)}</div>`:''}<div class="mref-pair">${one}${multi}</div></div>`;
  }

  // 表示するタイルが無いときの1行リンク（アフィリエイト時は rel=sponsored。クリックは index-main.js の委譲でGA計測）
  function linkRow(d){
    const link=safeUrl(d.search_url);
    if(!link) return '';
    return `<p class="buy-inline-note mref-tail"><a class="scard-link" href="${escAttr(link)}" target="_blank" rel="${d.affiliate===true?'sponsored noopener':'noopener'}">メルカリで見る</a></p>`;
  }

  // 見出し: 「メルカリ」＋アフィリエイト時だけ店舗リンクと同じ「PR」の印（AFF_PR_BADGE は index-main.js のグローバル定数）
  let lastAff=null;
  function setHead(aff){
    const head=document.getElementById('mercariRefHead');
    if(!head||lastAff===aff) return;
    lastAff=aff;
    head.innerHTML='メルカリ'+(aff?AFF_PR_BADGE:'');
  }

  function emptyHtml(msg){
    return `<div class="empty" style="padding:16px 0"><p>${esc(msg)}</p></div>`;
  }

  function show(html){
    const el=box(),body=bodyEl();
    if(!el||!body) return;
    el.classList.remove('hidden');
    // 店舗検索の途中（pending）は閉じた見出しだけ（開くと店舗の表を押し下げるため）。確定した時点で開いた状態にする。
    // 自動で開くのは同じカードで1回だけ（ユーザーが閉じたら描き直しで開き直さない。pending中にユーザーが開いたものは閉じない）
    if(!autoOpened&&!view.pending){
      autoOpened=true;
      if(!el.open){ el.dataset.autoOpen='1'; el.open=true; } // 自動で開いた分は index-main.js のGA計測から除く
    }
    if(html===lastHtml) return;
    body.innerHTML=html;
    lastHtml=html;
  }

  function render(){
    const d=data;
    if(!d) return;
    const sc=scope(d);
    const fetched=Number(d.fetched_count)||0;
    // 「(不明)」選択は比較できないので全レアリティのタイル
    let sel=view.rarity||'';
    if(sel==='(不明)') sel='';
    let h='';
    if(sel){
      // 選択したレアリティだけ。出品が無ければ1行のみ
      const v=sc.rs.indexOf(sel)>=0?d.by_rarity[sel]:null;
      h+=v?`<div class="summary">${group(d,sel,false)}</div>`:emptyHtml('このレアリティの出品は見つかりませんでした')+linkRow(d);
    }else if(sc.rs.length>0){
      h+=`<div class="summary">${sc.rs.map(r=>group(d,r,true)).join('')}</div>`;
    }else{
      // 店舗検索の途中で全レアリティが確認中のときは「出品なし」と言わない（リンク1行だけ出す）。
      // 全件が判定不能のときも同様（取得できたが全件除外は「出品なし」と区別）
      const allUnconfirmed=view.pending&&sc.excluded.length>0;
      if(!(Number(d.unclassified_count)>0)&&!allUnconfirmed) h+=emptyHtml(fetched>0||sc.excluded.length>0?'条件に合う出品なし':'出品なし');
      h+=linkRow(d);
    }
    setHead(d.affiliate===true);
    show(h);
  }

  function renderError(busy){
    data=null;
    lastHtml='';
    setHead(false);
    show('<p class="buy-inline-note">'+(busy?'混雑中です。少し待って再表示してください':'メルカリの情報を取得できませんでした')+'</p>');
  }

  // 選択レアリティ・店舗結果のレアリティが変わったとき（再取得せず描き直す）
  function setView(v){
    view={rarity:(v&&v.rarity)||'',stores:(v&&Array.isArray(v.stores))?v.stores:[],pending:!!(v&&v.pending)};
    if(data) render();
  }

  function reset(){
    const el=box(),body=bodyEl();
    data=null;
    lastHtml='';
    autoOpened=false;
    setHead(false);
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
