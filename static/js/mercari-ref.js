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

  // 最下行: メルカリへのリンク（アフィリエイト時は「PR」の印と rel=sponsored）。リンクが出ないときは出さない
  function linkNote(d){
    const link=safeUrl(d.search_url);
    if(!link) return '';
    const aff=d.affiliate===true;
    // 店舗リンクと同じ「PR」の印（AFF_PR_BADGE は index-main.js のグローバル定数。描画は両ファイル読込後に呼ばれる）
    return `<p class="buy-inline-note mref-tail"><a href="${escAttr(link)}" target="_blank" rel="${aff?'sponsored noopener':'noopener'}">メルカリで見る</a>${aff?AFF_PR_BADGE:''}</p>`;
  }

  // レアリティごとに店舗サマリーと同じ .scard タイル1枚。札は店舗表と同じ .rb クラス
  function tile(r,v){
    const label=`<span class="rb rb-${escAttr(cssClass(r))}">${esc(r)}</span>`;
    const set=v.set||null;
    let price,d1,d2='';
    if(Number(v.count)>0){
      price=yen(v.min);
      d1=v.median==null?`${Number(v.count)}件`:`中央 ${yen(v.median)}・${Number(v.count)}件`;
      if(set) d2=`${Number(set.unit_min_qty)}枚組 ${yen(set.unit_min)}/枚`;
    }else{
      // 単品が無くセットのみ: 1枚あたりを大きく、組の総額と件数を下に
      const q=Number(set.unit_min_qty);
      const total=set.unit_min_total!=null?Number(set.unit_min_total):Number(set.unit_min)*q;
      price=`${yen(set.unit_min)}<span class="mref-unit">/枚</span>`;
      d1=`${q}枚組 ${yen(total)}・${Number(set.count)}件`;
    }
    return `<div class="scard fin"><div class="slabel">${label}</div><div class="sprice">${price}</div>`+
      `<div class="sdetail">${d1}${d2?'<br>'+d2:''}</div></div>`;
  }

  function emptyHtml(msg){
    return `<div class="empty" style="padding:16px 0"><p>${esc(msg)}</p></div>`;
  }

  function show(html){
    const el=box(),body=bodyEl();
    if(!el||!body) return;
    el.classList.remove('hidden');
    // 表示するときは開いた状態にする。同じカードの間は1回だけ（ユーザーが閉じたら描き直しで開き直さない）
    if(!autoOpened){
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
      h+=v?`<div class="summary">${tile(sel,v)}</div>`:emptyHtml('このレアリティの出品は見つかりませんでした');
    }else if(sc.rs.length>0){
      h+=`<div class="summary">${sc.rs.map(r=>tile(r,d.by_rarity[r])).join('')}</div>`;
    }else{
      // 店舗検索の途中で全レアリティが確認中のときは「出品なし」と言わず、リンクだけ出す。
      // 全件が判定不能のときも同様（取得できたが全件除外は「出品なし」と区別）
      const allUnconfirmed=view.pending&&sc.excluded.length>0;
      if(!(Number(d.unclassified_count)>0)&&!allUnconfirmed) h+=emptyHtml(fetched>0||sc.excluded.length>0?'条件に合う出品なし':'出品なし');
    }
    h+=linkNote(d);
    show(h);
  }

  function renderError(busy){
    data=null;
    lastHtml='';
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
