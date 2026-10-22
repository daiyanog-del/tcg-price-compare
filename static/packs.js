// 新商品タブ（パック）。2026-10-21 ユーザー承認のモック _mocks/packs-refresh-20261021 の A〜E。
// - 一覧（発売前／発売済み）と詳細を左右に並べる。900px 以下は詳細を全面で開く（#packPane.is-open。環境デッキと同じ作り）
// - 収録カードは画像・名前・価格の行。押すと購入候補と同じメニュー（_wishOpenMenu。index-wish.js）
// - 「価格を調べる」は /api/deck（販売。店ごと・レアリティごとの価格つき）/ /api/deck-buy（買取）をそのまま使う。
//   結果は「このパックの版」（型番の頭＝パックの code。例 DBGV-JP001）に絞って出す（同じ名前の他の商品の再録版を混ぜない）
// - 店舗×レアの表: 発売済みならどのパックでも毎朝の収集の値（/api/packs/matrix）ですぐ出し、まとめて検索（販売）の後はその結果に置き換える
//   （2026-10-22 新弾専用の表から寄せた。他の商品の版は畳んで「も見る」で開く）
// esc / escAttr / safeUrl / _setupThumbObserver / wishGet / wishSave / wishAdd / _wishOpenMenu / _wishCloseMenu / _ga は
// index 本体・index-wish.js のグローバル関数を参照する（このファイルは非モジュールで読み込むこと）。

let _packsLoaded=false;
let _packData=[];
let _pk=null;      // 選んでいるパックの状態（_pkState を参照）
let _pkReq=0;      // パックを選ぶたびに増やす。古い読み込み・検索の結果を捨てるのに使う

const _PK_SHEET_MQ='(max-width: 900px)';
const _PK_MORE_SVG='<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><circle cx="3.5" cy="8" r="1.4"/><circle cx="8" cy="8" r="1.4"/><circle cx="12.5" cy="8" r="1.4"/></svg>';
const _PK_SEARCH_SVG='<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><circle cx="7" cy="7" r="4.5"/><path d="m10.5 10.5 3 3"/></svg>';

function _pkToast(msg){ if(window.DeckHead&&typeof window.DeckHead.toast==='function') window.DeckHead.toast(msg); }
function _pkYen(n){ return '¥'+Number(n).toLocaleString(); }
// 日本時間の今日（YYYY-MM-DD）。発売前／発売済みの境目（発売日当日は発売済み）
function _pkToday(){ return new Date(Date.now()+9*3600e3).toISOString().slice(0,10); }
function _pkDaysUntil(date){ return Math.round((Date.parse(date)-Date.parse(_pkToday()))/864e5); }
function _pkMd(date){ const p=(date||'').split('-'); return p.length===3?(+p[1])+'/'+(+p[2]):''; }
function _pkHm(){ const d=new Date(); return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0'); }

async function loadPacks(){
  if(_packsLoaded) return;
  const el=document.getElementById('packList');
  // 2026-10-21 に一覧の中身（発売済みを戻す・型番・Wikiのページ）を変えたのでキーも変える（古い6時間キャッシュを読まない）
  const CACHE_KEY='tcgym_packs_cache_v2', TTL=6*3600*1000; // 6時間
  try{
    const raw=sessionStorage.getItem(CACHE_KEY);
    if(raw){
      const {ts,data}=JSON.parse(raw);
      // 配列かつ空でないキャッシュだけを採用する。壊れたキャッシュ（{"error":...}等）は
      // ここで弾かれ、下のfetchで新規取得されることで自然に回復する
      if(Date.now()-ts<TTL&&Array.isArray(data)&&data.length){_packData=data;_packsLoaded=true;renderPacks();return;}
    }
  }catch{}
  // /featured はサーバーが最後に作った一覧を埋め込む（app.py featured_page・2026-10-22 LCP）。あれば API を待たずに描く（一度だけ）
  const initialEl=document.getElementById('packsInitial');
  if(initialEl){
    let initial=null;
    try{initial=JSON.parse(initialEl.textContent);}catch{initial=null;}
    initialEl.remove();
    if(Array.isArray(initial)&&initial.length){
      _packData=initial;_packsLoaded=true;
      try{sessionStorage.setItem(CACHE_KEY,JSON.stringify({ts:Date.now(),data:_packData}));}catch{}
      renderPacks();return;
    }
  }
  el.innerHTML='<div class="meta-loading">パック情報を読込中...</div>';
  try{
    const res=await fetch('/api/packs');
    // 失敗レスポンスをキャッシュしない。res.ok を見ずに保存していたため、サーバーの
    // 一時的な500が sessionStorage に6時間焼き付き、復旧後もエラー表示が残っていた
    // （2026-09-01 実測）。
    if(!res.ok) throw new Error('packs fetch failed: '+res.status);
    const json=await res.json();
    if(!Array.isArray(json)||!json.length) throw new Error('packs response invalid');
    _packData=json;
    _packsLoaded=true;
    try{sessionStorage.setItem(CACHE_KEY,JSON.stringify({ts:Date.now(),data:_packData}));}catch{}
    renderPacks();
  }catch(e){
    el.innerHTML='<div class="meta-loading">パック情報の取得に失敗しました</div>';
  }
}

// A・B: 一覧。発売前（あと◯日）と発売済みに分け、行に発売日・状態・収録数
function renderPacks(){
  const el=document.getElementById('packList');
  if(!_packData.length){el.innerHTML='<div class="meta-loading">パックデータがありません</div>';return;}
  el.classList.remove('meta-loading');
  const today=_pkToday();
  const idx=_packData.map((p,i)=>i).sort((a,b)=>(_packData[b].date||'').localeCompare(_packData[a].date||''));
  const up=idx.filter(i=>(_packData[i].date||'')>today), rel=idx.filter(i=>(_packData[i].date||'')<=today);
  const row=i=>{
    const p=_packData[i];
    const n=p.date?_pkDaysUntil(p.date):0;
    const state=n>0?`<span class="pk-chip is-soon">あと${n}日</span>`:'<span class="pk-chip">発売済み</span>';
    const count=p.has_cards===false?'収録未掲載':(p.card_count?p.card_count+'種':'');
    const sel=_pk&&_pk.name===p.name?' selected':'';
    return `<button type="button" class="pk-row${sel}" data-pack="${i}"><span class="pk-row-main"><span class="pk-row-name">${esc(p.name)}</span>${count?`<span class="pk-row-sub">${esc(count)}</span>`:''}</span><span class="pk-row-state"><b>${esc(_pkMd(p.date))}</b>${state}</span></button>`;
  };
  let html='';
  if(up.length) html+=`<div class="pk-group"><div class="pk-group-h">発売前（60日以内）</div>${up.map(row).join('')}</div>`;
  if(rel.length) html+=`<div class="pk-group"><div class="pk-group-h">発売済み</div>${rel.map(row).join('')}</div>`;
  html+='<div class="pk-note">商品は遊戯王OCG公式サイト、収録カードは遊戯王Wikiから取得しています。</div>';
  el.innerHTML=html;
  // PC（左右2列）では、まだ何も選んでいなければ最新の発売済みパックを開いておく（右の欄を空で始めない。2026-10-22 ユーザー承認）。
  // 発売前のパックは値段が無いので選ばない。スマホは一覧から始める（全面で開くと一覧が見えなくなる）
  if(!_pk&&rel.length&&window.matchMedia&&!matchMedia(_PK_SHEET_MQ).matches){
    const p=_packData[rel[0]];
    if(p) setTimeout(()=>{ if(!_pk) loadPackCards(p.name,p.wiki_page||p.name,p.tcg_name||'',!!p.featured); },0);
  }
  if(!el.dataset.ready){
    el.dataset.ready='1';
    el.addEventListener('click',e=>{
      const b=e.target.closest('.pk-row');
      if(!b) return;
      const p=_packData[+b.dataset.pack];
      if(p) loadPackCards(p.name,p.wiki_page||p.name,p.tcg_name||'',!!p.featured);
    });
  }
}

// 選んでいるパックの状態を作る
function _pkState(packName,wikiPage,tcgName,isFeatured){
  const info=_packData.find(p=>p.name===packName)||{name:packName,date:''};
  const days=info.date?_pkDaysUntil(info.date):0;
  return {
    req:++_pkReq, name:packName, wiki:wikiPage, tcg:tcgName, featured:!!isFeatured, info,
    unreleased:days>0, days, loading:true, error:'', cards:[],
    view:'list', sort:'order', mode:'sale',
    results:{sale:null,buy:null}, // まとめて検索の結果（モードごと）
    run:null,                     // 検索中 {mode,done,total,abort}
    dbmx:null,                    // 毎朝の収集の店舗×レア（/api/packs/matrix）。null=未取得 / 'loading' / 'error' / {shops,rows,updated_at}
    dbModel:null, dbCode:'',      // dbmx から作った表の形と、型番の頭
  };
}

// パックを選ぶ（/featured からも呼ばれる。引数の形は以前のまま）
async function loadPackCards(packName, wikiPage, tcgName, isFeatured){
  if(_pk&&_pk.run&&_pk.run.abort) _pk.run.abort.abort(); // 前のパックの検索は止める
  if(typeof _wishCloseMenu==='function') _wishCloseMenu();
  const st=_pkState(packName,wikiPage,tcgName,isFeatured);
  _pk=st;
  document.querySelectorAll('#packList .pk-row').forEach(b=>{
    const p=_packData[+b.dataset.pack];
    b.classList.toggle('selected',!!p&&p.name===packName);
  });
  _pkRender();
  _openPackSheet();

  try{
    if(isFeatured) await _pkLoadFeatured(st);
    st.fromFeatured=st.cards.length>0;
    if(!st.cards.length&&!st.error) await _pkLoadWiki(st);
  }catch(e){
    if(st.req===_pkReq) st.error='読込に失敗しました';
  }
  if(st.req!==_pkReq) return;
  st.loading=false;
  _pkRender();
  // 発売済みは、データベースの最新値を先に出す（新弾は /api/featured の値があるので取り直さない）
  if(!st.unreleased&&st.cards.length&&!st.fromFeatured) _pkLoadDbPrices(st);
}

// 新弾: /api/featured の収録カード（データベースの価格・レアリティつき）
async function _pkLoadFeatured(st){
  for(let tries=0;tries<8;tries++){
    let data=null;
    // 取れなければ（502 の HTML など）遊戯王Wikiの収録リストへ進む（reviewer 中4）
    try{ const res=await fetch('/api/featured'); data=res.ok?await res.json():null; }catch(_){ return; }
    if(st.req!==_pkReq) return;
    if(data&&data.loading){ await new Promise(r=>setTimeout(r,2500)); continue; } // サーバー側の準備中
    if(data&&data.pack&&Array.isArray(data.cards)&&data.cards.length){
      st.cards=data.cards.map(c=>({name:c.name,rarity:c.rarity||'',db:(c.price!=null&&c.price!=='')?{price:Number(c.price)}:null}));
    }
    return; // 新弾の情報が取れなければ遊戯王Wikiの収録リストで出す
  }
}

// 遊戯王Wikiの収録リスト
async function _pkLoadWiki(st){
  let url='/api/packs/cards?name='+encodeURIComponent(st.name)+'&wiki='+encodeURIComponent(st.wiki);
  if(st.tcg) url+='&tcg='+encodeURIComponent(st.tcg);
  const res=await fetch(url);
  if(st.req!==_pkReq) return;
  if(res.status===429){ st.error='混み合っています。少し待ってからもう一度お試しください'; return; }
  if(!res.ok) throw new Error('pack cards fetch failed: '+res.status);
  const data=await res.json();
  if(st.req!==_pkReq) return;
  if(!data.cards||!data.cards.length){
    // has_cards は文言の出し分けにのみ使う（APIを呼ぶかどうかのガードには使わない）。
    // get_pack_list の結果は24時間キャッシュされるため、Wiki障害中に一覧を作ると
    // 全パックが has_cards=false になりうる。その場合にAPI呼び出しを止めると
    // 障害中は丸一日どのパックも開けなくなってしまう。
    st.error=st.info.has_cards===false?'収録カードは遊戯王Wikiにまだ載っていません':'カードリストが取得できませんでした';
    return;
  }
  st.cards=data.cards.map(n=>({name:n,rarity:'',db:null}));
}

// データベースの最新値と7日前比（/api/wish-prices。1回50種まで）
async function _pkLoadDbPrices(st){
  const names=st.cards.map(c=>c.name);
  for(let i=0;i<names.length;i+=50){
    let json;
    try{
      const res=await fetch('/api/wish-prices',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cards:names.slice(i,i+50)})});
      if(!res.ok) return;
      json=await res.json();
    }catch(_){ return; }
    if(st.req!==_pkReq) return;
    for(const r of (json.cards||[])){
      if(r.status!=='ready'||r.latest==null) continue;
      for(const c of st.cards) if(c.name===r.name) c.db={price:Number(r.latest),pct:typeof r.pct==='number'?r.pct:null};
    }
    _pkRefreshPrices();
  }
}

// ── 描画 ──

// 店舗×レアの表の元: まとめて検索（販売）を最後まで終えた結果があればそれ、新弾ならデータベース（reviewer 中5）
// 途中で止めた・失敗した検索の結果では置き換えない（送れなかったカードが「見つからない」に見えるので。reviewer 中5）
function _pkBulkMatrix(st){ const r=st.results.sale; return r&&r.matrix&&!r.stopped&&!r.error&&!(st.run&&st.run.mode==='sale')?r:null; }
function _pkMatrixAvailable(st){ return !st.unreleased&&st.cards.length>0; } // 発売済みなら毎朝の収集の値で出せる

function _pkRender(){
  const el=document.getElementById('packDetail');
  if(!el) return;
  _pkInitDetailEvents(el);
  const st=_pk;
  if(!st){ el.innerHTML='<div class="meta-empty">パックを選ぶと、収録カードと価格がここに出ます</div>'; return; }
  if(st.view==='matrix'&&!_pkMatrixAvailable(st)) st.view='list';
  const i=st.info;
  const meta=`${i.date?`<b>${esc(i.date.replace(/-/g,'/'))}</b> ${st.unreleased?'発売・あと'+st.days+'日':'発売'}`:''}${st.cards.length?` ・ 収録 <b>${st.cards.length}</b>種`:''}`;
  // 一覧と表は両方の入れ物を置き、切り替えは表示だけにする（作り直すとカード画像を読み直すので）
  let body;
  if(st.loading) body='<div class="meta-loading">カードリストを読込中...</div>';
  else if(st.error) body=`<div class="meta-empty">${esc(st.error)}</div>`;
  else body=`<div class="pk-cards" id="packDetailGrid"${st.view==='matrix'?' hidden':''}>${_pkOrder(st).map(k=>_pkRowHtml(st,k)).join('')}</div>`
    +`<div class="pk-matrix" id="packMatrix"${st.view==='matrix'?'':' hidden'}></div>`;
  st.mxKey='';
  _pkSyncLayout();
  el.innerHTML=`<div class="pk-head"><h3 class="pk-title">${esc(st.name)}</h3><div class="pk-meta">${meta}</div></div>`
    +`<div id="packTools">${st.loading?'':_pkToolsHtml(st)}</div>`+body;
  if(!st.loading&&!st.error){
    _setupThumbObserver('#packDetailGrid');
    if(st.view==='matrix') _pkRenderMatrix();
  }
}

// 一覧／店舗×レアの切り替え
// 表を開いている間は、一覧を細くして表に幅を回す（css/packs.css の #mode-packs.is-matrix）
function _pkSyncLayout(){
  const m=document.getElementById('mode-packs');
  if(m) m.classList.toggle('is-matrix',!!(_pk&&_pk.view==='matrix'));
}

function _pkSetView(v){
  const st=_pk;
  if(!st||st.view===v) return;
  st.view=v;
  _pkSyncLayout();
  const grid=document.getElementById('packDetailGrid'), mx=document.getElementById('packMatrix');
  if(grid) grid.hidden=v==='matrix';
  if(mx) mx.hidden=v!=='matrix';
  _pkRefreshPrices();
}

// 見出しの下の操作と、状態の一行
function _pkToolsHtml(st){
  // ⋯ の中身（全部を購入候補へ・Wiki・公式の商品ページ）が1つも無ければボタンも出さない
  const hasMore=st.cards.length||st.info.wiki_url||st.info.product_url;
  const more=hasMore?`<button type="button" class="pk-btn is-icon" id="packMoreBtn" aria-label="その他の操作" aria-haspopup="menu">${_PK_MORE_SVG}</button>`:'';
  if(!st.cards.length) return more?`<div class="pk-bar"><div class="pk-bar-l"></div><div class="pk-bar-r">${more}</div></div>`:'';
  if(st.unreleased){
    return `<div class="pk-bar"><div class="pk-bar-l"><button type="button" class="pk-btn" disabled>${_PK_SEARCH_SVG}価格を調べる</button></div><div class="pk-bar-r">${more}</div></div>`
      +`<p class="pk-hint">発売前のため、まだお店に価格がありません。発売日（${esc(_pkMd(st.info.date))}）から調べられます。</p>`;
  }
  const seg=(attr,items,cur,dis)=>`<div class="pk-seg">${items.map(([v,l])=>`<button type="button" data-${attr}="${v}" class="${cur===v?'is-on':''}"${dis?' disabled':''} aria-pressed="${cur===v}">${l}</button>`).join('')}</div>`;
  const running=!!st.run;
  // 新弾（/api/featured）の並びは収録順ではなく、サーバーが返した順（値段の高い順）なので「掲載順」と書く（reviewer 低3）
  const left=(_pkMatrixAvailable(st)?seg('view',[['list','一覧'],['matrix','店舗×レア']],st.view):'')+seg('sort',[['order',st.featured?'掲載順':'収録順'],['price','高い順']],st.sort);
  const right=seg('mode',[['sale','販売'],['buy','買取']],st.mode,running)
    +`<button type="button" class="pk-btn" id="packBulkBtn"${running?' disabled':''}>${_PK_SEARCH_SVG}価格を調べる</button>`+more;
  return `<div class="pk-bar"><div class="pk-bar-l">${left}</div><div class="pk-bar-r">${right}</div></div>`+_pkStatusHtml(st);
}

function _pkStatusHtml(st){
  const n=st.cards.length;
  if(st.run){
    const pct=st.run.total?Math.round(100*st.run.done/st.run.total):0;
    return `<div class="pk-prog" id="packProg"><span>お店から取り直し中 <b id="packProgN">${st.run.done}/${st.run.total}</b></span><i style="--w:${pct}%"></i><button type="button" class="pk-btn is-small" id="packBulkStop">止める</button></div>`;
  }
  if(st.view==='matrix'){
    const r=_pkBulkMatrix(st);
    if(r) return `<p class="pk-hint"><b>${esc(r.at)}</b> のまとめて検索の結果（販売）・${r.code?'このパックの版だけ（他の商品の再録版は除く）・':''}<span class="pk-green">緑</span>＝その行でいちばん安い店。価格を押すとその店の商品ページへ。`
      +(r.failedShops.length?`<br>${r.failedShops.map(esc).join('・')} は一部取得できませんでした（${r.failedCards}種）。その店の欄は「取得失敗」です。`:'')+'</p>';
    const m=st.dbmx&&typeof st.dbmx==='object'?st.dbmx:null;
    return `<p class="pk-hint">毎朝の収集の値${m&&m.updated_at?'（<b>'+esc(_pkMd(m.updated_at))+'</b> 時点）':''}・${st.dbCode?'このパックの版だけ（他の商品の版は畳んであります）・':''}<span class="pk-green">緑</span>＝その行でいちばん安い店。価格を押すとその店の商品ページへ。いまの値に取り直すときは「価格を調べる」（お店から取得）。</p>`;
  }
  const r=st.results[st.mode];
  const label=st.mode==='buy'?'買取':'販売';
  if(r){
    let s=`<b>${esc(r.at)}</b> にお店から取得・${n}種中 <b>${r.found}</b>種に${label}価格（各カードの${st.mode==='buy'?'最高値':'最安'}の店${r.code&&st.mode==='sale'?'・このパックの版':''}）`;
    if(r.stopped) s+=`・途中で止めました（${r.done}種まで）`;
    else if(r.found<n){
      // 取得失敗・このパックの版なし・見つからない を分けて数える（reviewer 低9）
      const its=Object.values(r.items).filter(it=>it.price==null);
      const fail=its.filter(it=>it.status==='failed').length, other=its.filter(it=>it.status!=='failed'&&it.other).length;
      const none=n-r.found-fail-other;
      if(fail) s+=`・${fail}種は取得失敗`;
      if(other) s+=`・${other}種はこのパックの版なし`;
      if(none>0) s+=`・${none}種は見つかりませんでした`;
    }
    if(r.failedShops.length) s+=`<br>${r.failedShops.map(esc).join('・')} は一部取得できませんでした（${r.failedCards}種）。その店を除いた範囲の価格です。`;
    if(r.error) s+=`<br>${esc(r.error)}`;
    return `<p class="pk-hint">${s}</p>`;
  }
  if(st.mode==='buy') return `<p class="pk-hint">買取価格は「価格を調べる」でお店から取ります（${n}種・少し時間がかかります）。</p>`;
  return `<p class="pk-hint">表示中の価格はデータベースの最新値です。「価格を調べる」は${n}種をお店から取り直すため、少し時間がかかります。</p>`;
}

// 並び順（カードの番号の配列）
function _pkOrder(st){
  const keys=st.cards.map((c,k)=>k);
  if(st.sort!=='price') return keys;
  const v=k=>{ const p=_pkShownPrice(st,st.cards[k]); return p==null?-1:p; };
  return keys.sort((a,b)=>v(b)-v(a));
}

// 行に出している価格（数値）。並べ替えに使う
function _pkShownPrice(st,c){
  if(st.unreleased) return null;
  const r=st.results[st.mode];
  if(r&&r.items[c.name]) return r.items[c.name].price;
  if(st.mode==='buy') return null;
  return c.db?c.db.price:null;
}

function _pkRowHtml(st,k){
  const c=st.cards[k];
  const r=st.results[st.mode], it=r&&r.items[c.name];
  const rarity=it&&it.rarity?it.rarity:c.rarity;
  return `<div class="pk-card" data-k="${k}" role="button" tabindex="0" aria-haspopup="menu"><span class="pk-num">${k+1}</span><span class="pk-thumb"><span class="pack-card-thumb" data-card="${escAttr(c.name)}"></span></span>`
    +`<span class="pk-cname">${esc(c.name)}${rarity?`<small>${esc(rarity)}</small>`:''}</span><span class="pk-cprice" id="packPrice${k}">${_pkPriceHtml(st,c)}</span></div>`;
}

function _pkPriceHtml(st,c){
  if(st.unreleased) return '<span class="is-none">未発売</span>';
  const r=st.results[st.mode];
  const it=r&&r.items[c.name];
  if(st.run&&st.run.mode===st.mode&&st.run.pending.has(c.name)) return '<span class="is-none">…</span>';
  if(it){
    if(it.price==null){
      const why=it.status==='failed'?'取得失敗':it.other?'このパックの版なし':(st.mode==='buy'?'買取なし':'在庫なし');
      return `<span class="is-none">${why}</span>`;
    }
    const sub=[it.shop||''];
    if(it.otherVersion) sub.push('他の商品の版');
    if(it.partial) sub.push('一部取得失敗');
    return `${_pkYen(it.price)}<small class="pk-shop">${esc(sub.filter(Boolean).join('・'))}</small>`;
  }
  if(st.mode==='buy'||!c.db) return '<span class="is-none">—</span>';
  let sub='';
  // 販売価格の値動き: 値上がり＝赤・値下がり＝緑（買取とは意味が逆。CLAUDE.md データ品質）
  if(typeof c.db.pct==='number'&&Math.abs(c.db.pct)>=1) sub=`<small class="${c.db.pct>0?'is-up':'is-down'}">${c.db.pct>0?'+':''}${Math.round(c.db.pct)}%</small>`;
  return _pkYen(c.db.price)+sub;
}

// 価格の欄と状態の一行だけ描き直す（行を作り直すと画像を読み直すので）
function _pkRefreshPrices(){
  const st=_pk;
  if(!st||st.loading||st.error) return;
  if(st.view==='matrix'&&!_pkMatrixAvailable(st)){ // 表の元が無くなった（検索をやり直し始めた等）ら一覧へ
    st.view='list';
    _pkSyncLayout();
    const g=document.getElementById('packDetailGrid'), mx=document.getElementById('packMatrix');
    if(g) g.hidden=false;
    if(mx) mx.hidden=true;
  }
  const tools=document.getElementById('packTools');
  if(tools) tools.innerHTML=_pkToolsHtml(st);
  if(st.view==='matrix') _pkRenderMatrix();
  const grid=document.getElementById('packDetailGrid');
  if(!grid) return;
  st.cards.forEach((c,k)=>{
    const cell=document.getElementById('packPrice'+k);
    if(cell) cell.innerHTML=_pkPriceHtml(st,c);
    const r=st.results[st.mode], it=r&&r.items[c.name];
    const name=cell&&cell.parentElement.querySelector('.pk-cname');
    if(name){
      const rarity=it&&it.rarity?it.rarity:c.rarity;
      name.innerHTML=esc(c.name)+(rarity?`<small>${esc(rarity)}</small>`:'');
    }
  });
  _pkApplyOrder();
}

// 並び順を行の入れ替えで反映する（検索の途中では動かさない。読んでいる途中で行が動かないように）
function _pkApplyOrder(){
  const st=_pk, grid=document.getElementById('packDetailGrid');
  if(!st||!grid||st.run) return;
  const rows={};
  grid.querySelectorAll('.pk-card').forEach(r=>{ rows[r.dataset.k]=r; });
  _pkOrder(st).forEach(k=>{ if(rows[k]) grid.appendChild(rows[k]); });
}

// ── 店舗×レアの表 ──
// 元は2つ: まとめて検索（販売）を最後まで終えた結果があればそれ、無ければ毎朝の収集（/api/packs/matrix。どの発売済みパックでも）。
// どちらも「品」（カード・レアリティ・店・値段・型番）の並びにしてから、同じ作り（_pkMatrixModel）で表にする。

// 毎朝の収集の表を読む（パックごとに1回。サーバーも1時間覚える）
async function _pkLoadDbMatrix(st){
  st.dbmx='loading';
  try{
    const res=await fetch('/api/packs/matrix',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cards:st.cards.map(c=>c.name)})});
    const data=res.ok?await res.json():null;
    st.dbmx=(data&&!data.error&&Array.isArray(data.rows))?data:'error';
  }catch(_){ st.dbmx='error'; }
}

async function _pkRenderMatrix(){
  const st=_pk, el=document.getElementById('packMatrix');
  if(!st||!el) return;
  const r=_pkBulkMatrix(st);
  let model=null, src='';
  if(r){ model=r.matrix; src='bulk:'+r.at; }
  else{
    if(st.dbmx===null||st.dbmx===undefined){
      el.innerHTML='<div class="meta-loading">店舗×レアの表を読込中...</div>';
      st.mxKey='';
      await _pkLoadDbMatrix(st);
      if(st!==_pk) return;
      const tools=document.getElementById('packTools');
      if(tools) tools.innerHTML=_pkToolsHtml(st);
      return _pkRenderMatrix();
    }
    if(st.dbmx==='loading') return;
    if(st.dbmx==='error'){ el.innerHTML='<div class="meta-empty">店舗×レアの表を取得できませんでした。少し待ってからもう一度開いてください</div>'; st.mxKey=''; st.dbmx=null; return; }
    if(!st.dbModel){
      const items=_pkItemsFromDb(st.dbmx);
      st.dbCode=_pkPackCode(st,items);
      st.dbModel=_pkMatrixModel(st,items,st.dbCode,st.dbmx.shops||[],{});
      st.dbModel.updatedAt=st.dbmx.updated_at||'';
      const tools=document.getElementById('packTools'); // 型番の頭が決まったので説明の一行を描き直す
      if(tools) tools.innerHTML=_pkToolsHtml(st);
    }
    model=st.dbModel; src='db';
  }
  // 同じ表を描き直さない（カード画像を読み直すので）。元のデータか並び順が変わったときだけ
  const key=src+':'+st.sort;
  if(st.mxKey===key) return;
  st.mxKey=key;
  if(!model||!model.shops.length||!model.groups.length){ el.innerHTML='<div class="meta-empty">店舗×レアの表のデータがまだありません</div>'; return; }
  el.innerHTML=_pkMatrixHtml(model,st.sort,src==='db');
  _setupThumbObserver('#packMatrix');
}

// 毎朝の収集の表（build_matrix の形: 行＝カード×レアリティ、セル＝店）を品の並びにする
function _pkItemsFromDb(m){
  const items=[];
  for(const r of (m.rows||[])){
    for(const [shop,c] of Object.entries(r.cells||{})){
      if(c&&c.price!=null) items.push({name:r.name,rarity:r.rarity||'',shop,price:Number(c.price),url:c.url||'',code:c.code||'',date:c.recorded_at||''});
    }
  }
  return items;
}

// まとめて検索の店ごと・レアリティごと・型番の頭ごとの結果を品の並びにする
function _pkItemsFromPerShop(perShop){
  const items=[];
  for(const [name,ps] of Object.entries(perShop||{})){
    for(const [shop,byKey] of Object.entries(ps||{})){
      for(const it of Object.values(byKey||{})){
        if(it&&it.price!=null) items.push({name,rarity:it.rarity||'',shop:it.shop||shop,price:Number(it.price),url:it.url||'',code:it.code||''});
      }
    }
  }
  return items;
}

// 品の並びから表の形を作る。
// - レアリティごとに、このパックの版（型番の頭が一致）・型番不明・他の商品の版に分ける
// - このパックの版があるか、他の商品の版が無いレアリティは「このパックの行」。店ごとに、このパックの版か型番不明の最安を出す。
//   その店に他の商品の版しか無ければ（毎朝の収集は店×レアリティで最安1件しか残さないので起きる）、値段に版の札を付けて最安の緑から外す
// - 他の商品の版だけのレアリティ（再録元の商品のレアリティ）は畳んでおく行にする。同じレアリティで両方ある店の他の版も、畳む行に入れる
function _pkMatrixModel(st,items,code,shopOrder,failedByCard){
  const shops=[];
  const addShop=s=>{ if(s&&!shops.includes(s)) shops.push(s); };
  (shopOrder||[]).forEach(addShop);
  const byCard={};
  for(const it of items) (byCard[it.name]=byCard[it.name]||[]).push(it);
  const minOf=cells=>Math.min(...Object.values(cells).map(c=>c.price));
  const groups=[];
  for(const c of st.cards){
    const list=byCard[c.name]||[];
    const byR={};
    for(const it of list){
      const rar=it.rarity||'レアリティ不明'; // レアリティの分からない品も残す（reviewer 中3）
      (byR[rar]=byR[rar]||[]).push(it);
    }
    const main=[], other=[];
    const kind=it=>{ const p=_pkPrefixOf(it.code); return !code?'this':!p?'unk':p===code?'this':'oth'; };
    // このカードに「このパックの版」の記録が1つでもあれば、型番不明だけのレアリティは「版不明」として畳む
    // （例: ノーマルだけ収録の再録カードで、型番の無いアルティメットの記録がこのパックの行に見えていた）。
    // 型番の情報がまったく無いカード（新しいパックでよくある）は、隠さずに出す
    const hasThisAny=list.some(it=>kind(it)==='this');
    for(const [rar,its] of Object.entries(byR)){
      const mineIts=its.filter(it=>kind(it)!=='oth'), othIts=its.filter(it=>kind(it)==='oth');
      const hasThis=its.some(it=>kind(it)==='this');
      const isMine=!code||hasThis||(!othIts.length&&!hasThisAny);
      const pick=(arr,shop)=>arr.filter(it=>it.shop===shop).sort((a,b)=>a.price-b.price)[0];
      if(isMine){
        const cells={};
        const shopsHere=[...new Set(its.map(it=>it.shop))];
        const leftover=[];
        for(const s of shopsHere){
          const m=pick(mineIts,s);
          if(m){ cells[s]={price:m.price,url:m.url,code:m.code,date:m.date}; const o=pick(othIts,s); if(o) leftover.push(o); }
          else{ const o=pick(othIts,s); cells[s]={price:o.price,url:o.url,code:o.code,date:o.date,other:true}; }
          addShop(s);
        }
        main.push({rarity:rar,cells});
        if(leftover.length){
          const oc={}; leftover.forEach(o=>{ oc[o.shop]={price:o.price,url:o.url,code:o.code}; });
          other.push({rarity:rar,cells:oc,vers:[...new Set(leftover.map(o=>_pkPrefixOf(o.code)))]});
        }
      }else{
        const oc={};
        for(const s of [...new Set(its.map(it=>it.shop))]){ const o=pick(its,s); oc[s]={price:o.price,url:o.url,code:o.code}; addShop(s); }
        const vers=[...new Set(othIts.map(o=>_pkPrefixOf(o.code)))];
        if(its.some(it=>kind(it)==='unk')) vers.push('版不明');
        other.push({rarity:rar,cells:oc,vers});
      }
    }
    main.sort((a,b)=>minOf(b.cells)-minOf(a.cells));
    other.sort((a,b)=>minOf(b.cells)-minOf(a.cells));
    const f=(failedByCard||{})[c.name];
    groups.push({name:c.name,main,other,failed:f||null});
  }
  // 取得に失敗した店も列に残す（列ごと消すと「その店には無い」に見える。reviewer 中2）
  const failedShops=new Set(Object.values(failedByCard||{}).flatMap(f=>f.shops));
  failedShops.forEach(addShop);
  return {shops:shops.filter(s=>failedShops.has(s)||groups.some(g=>g.main.some(r=>r.cells[s])||g.other.some(r=>r.cells[s]))),groups};
}

// 狭い画面で表の見出しに使う店の短い呼び名（無い店は正式名のまま）
// 表の見出しの店名は正式名のまま、狭いときは決まった位置で2行に折る（短い呼び名は名前が切れたように見えたのでやめた。2026-10-22 ユーザー指摘）
const _PK_SHOP_BREAK={'カードラッシュ':['カード','ラッシュ'],'トレコロCB':['トレコロ','CB'],'カードラボ':['カード','ラボ'],'まんぞく屋':['まんぞく','屋'],'カーナベル':['カー','ナベル']};
function _pkShopLabel(s){ const parts=_PK_SHOP_BREAK[s]; return parts?parts.map(esc).join('<wbr>'):esc(s); }

// カードごとにまとめ、行はレアリティ。各行でいちばん安い店だけ緑。他の商品の版は畳んで「も見る」で開く
function _pkMatrixHtml(model,sort,fromDb){
  const shops=model.shops;
  const groups=model.groups.slice();
  if(sort==='price'){
    const top=g=>Math.max(-1,...g.main.flatMap(r=>Object.values(r.cells).filter(c=>!c.other).map(c=>c.price)));
    groups.sort((a,b)=>top(b)-top(a));
  }
  const span=shops.length+1;
  const cell=(c,isMin)=>{
    const url=c.url&&safeUrl(c.url);
    const txt=_pkYen(c.price);
    const badge=c.other&&_pkPrefixOf(c.code)?`<span class="pmx-ver">${esc(_pkPrefixOf(c.code))}</span>`:'';
    // 見出しの日付より古い記録（その店の今日の収集がまだ）には日付を添える（reviewer 中3）
    const date=model.updatedAt&&c.date&&c.date<model.updatedAt?`<small class="pmx-date">${esc(_pkMd(c.date))}</small>`:'';
    return `<td class="${isMin?'is-min':''}${c.other?' is-other':''}">${url?`<a href="${escAttr(url)}" target="_blank" rel="noopener">${txt}</a>`:txt}${badge}${date}</td>`;
  };
  const why=g=>{
    const f=g.failed;
    return f&&f.all?'取得に失敗しました':g.other.length?'このパックの版は見つかりませんでした（他の商品の版だけ）':fromDb?'このパックの版の記録がありません':'価格が見つかりませんでした';
  };
  const minOfRow=r=>{ const ps=Object.values(r.cells).filter(c=>c&&!c.other).map(c=>c.price); return ps.length?Math.min(...ps):null; };
  const thumb=name=>`<span class="pmx-thumb"><span class="pack-card-thumb" data-card="${escAttr(name)}"></span></span>`;
  const verLabel=r=>r.vers.filter(Boolean).length?`<span class="pmx-ver">${esc(r.vers.filter(Boolean).join('・'))}</span>`:'';
  const moreBtn=(gi,n)=>`<button type="button" class="pmx-more" data-mx-toggle="${gi}" data-n="${n}" aria-expanded="false">他の商品の版 ${n}行も見る</button>`;

  // PC（601px 以上）: 行＝カード×レアリティ、列＝店の表
  let h=`<div class="pmx-scroll"><table class="pmx"><thead><tr><th class="pmx-sc">レアリティ</th>${shops.map(s=>`<th title="${escAttr(s)}">${_pkShopLabel(s)}</th>`).join('')}</tr></thead><tbody>`;
  groups.forEach((g,gi)=>{
    h+=`<tr class="pmx-g"><td colspan="${span}"><div class="pmx-gname">${thumb(g.name)}${esc(g.name)}</div></td></tr>`;
    const f=g.failed;
    if(!g.main.length) h+=`<tr><td class="pmx-sc is-dim" colspan="${span}">${why(g)}</td></tr>`;
    for(const r of g.main){
      const min=minOfRow(r);
      // 購入候補の「+」はレアリティの列（横にスクロールしても付いてくる列）に置く。右端の列だと幅が足りないと見切れた（2026-10-22 ユーザー指摘）
      h+=`<tr><td class="pmx-sc"><span class="pmx-rar"><span>${esc(r.rarity)}</span><button type="button" class="pmx-add-btn" data-name="${escAttr(g.name)}" data-rarity="${escAttr(r.rarity)}" aria-label="${escAttr('「'+r.rarity+'」を購入候補に入れる')}" title="購入候補に入れる">+</button></span></td>`;
      for(const s of shops){
        const c=r.cells[s];
        if(!c){ h+=f&&f.shops.includes(s)?'<td class="is-dim is-fail">取得失敗</td>':'<td class="is-dim">—</td>'; continue; }
        h+=cell(c,!c.other&&c.price===min);
      }
      h+='</tr>';
    }
    if(g.other.length){
      for(const r of g.other){
        h+=`<tr class="pmx-other" data-mx-of="${gi}" hidden><td class="pmx-sc">${esc(r.rarity)}${verLabel(r)}</td>`;
        for(const s of shops){ const c=r.cells[s]; h+=c?cell({...c,other:false},false):'<td class="is-dim">—</td>'; }
        h+='</tr>';
      }
      h+=`<tr class="pmx-more-row"><td colspan="${span}">${moreBtn(gi,g.other.length)}</td></tr>`;
    }
  });
  h+='</tbody></table></div>';

  // スマホ（600px 以下）: カードごとに、行＝店・列＝レアリティの小さな表（2026-10-22 ユーザー選択 案C）。
  // 購入候補はカード名の右の「+ 候補」1つから、レアリティを選ぶメニューで入れる（案C2。列ごとの「+」ははみ出して見えた）
  const mtable=(rows,other)=>{
    const used=shops.filter(s=>rows.some(r=>r.cells[s]));
    const mins=rows.map(minOfRow);
    let t=`<div class="pmc-sc"><table class="pmc${other?' is-other':''}"><thead><tr><th class="pmc-s"><span class="sr-only">店</span></th>${rows.map(r=>`<th>${esc(r.rarity)}${other?verLabel(r):''}</th>`).join('')}</tr></thead><tbody>`;
    for(const s of used){
      t+=`<tr><td class="pmc-s">${esc(s)}</td>${rows.map((r,i)=>{ const c=r.cells[s]; return c?cell(other?{...c,other:false}:c,!other&&!c.other&&c.price===mins[i]):'<td class="is-dim">—</td>'; }).join('')}</tr>`;
    }
    return t+'</tbody></table></div>';
  };
  let m='<div class="pmc-wrap">';
  groups.forEach((g,gi)=>{
    m+=`<div class="pmc-g"><div class="pmc-head">${thumb(g.name)}<span class="pmc-name">${esc(g.name)}</span>${g.main.length?`<button type="button" class="pmc-add" data-name="${escAttr(g.name)}" aria-haspopup="menu">+ 候補</button>`:''}</div>`;
    if(!g.main.length) m+=`<p class="pmc-why">${why(g)}</p>`;
    else m+=mtable(g.main,false);
    const fs=g.failed&&g.failed.shops&&g.failed.shops.length?g.failed.shops:null;
    if(fs&&g.main.length) m+=`<p class="pmc-why">${fs.map(esc).join('・')} は取得失敗</p>`;
    if(g.other.length) m+=`<div data-mx-of="${gi}" hidden>${mtable(g.other,true)}</div>${moreBtn(gi,g.other.length)}`;
    m+='</div>';
  });
  return h+m+'</div>';
}

// 型番がこのパックの版か（型番が分からないものは残す。パックの型番の頭が分からなければ絞らない）
// 型番の頭（DBGV-JP001 → DBGV）。略号だけの店（例 TTP1）はそれを頭とみなし、前に文字が付く形（例 アジアTTP1-JP079）は
// その文字ごと頭にする（このパックの頭と一致しないので「他の版」になる。reviewer 高1）
function _pkPrefixOf(code){
  const c=String(code||'').trim().toUpperCase();
  if(!c) return '';
  const m=/^([^-\s]+)-/.exec(c);
  if(m) return m[1];
  return /^[A-Z0-9]{2,6}$/.test(c)?c:'';
}
function _pkSameVersion(packCode,code){
  if(!packCode||!code) return true;
  const p=_pkPrefixOf(code);
  return !p||p===packCode; // 頭の完全一致（前方一致だと WPP と WPP5 を同じに扱う。reviewer 低1）
}

// パックの型番の頭を決める（items: 品の並び）。一覧の code（公式の商品ページの略号）が、型番の分かるカードの3割以上に
// 出ていればそれを使い、出ていなければ（略号と型番が違う商品）、半分を超えるカードに付いている頭を使う。どちらも無ければ絞らない
function _pkPackCode(st,items){
  const perCard={};
  for(const it of items){ const p=_pkPrefixOf(it.code); (perCard[it.name]=perCard[it.name]||new Set()); if(p) perCard[it.name].add(p); }
  const sets=Object.values(perCard);
  const withCode=sets.filter(s=>s.size).length;
  const code=String(st.info.code||'').toUpperCase();
  if(code&&withCode&&sets.filter(s=>s.has(code)).length*10>=withCode*3) return code;
  const count={};
  for(const s of sets) for(const p of s) count[p]=(count[p]||0)+1;
  const best=Object.entries(count).sort((a,b)=>b[1]-a[1])[0];
  return best&&best[1]>st.cards.length/2?best[0]:'';
}

// まとめて検索（販売）の結果から、表と各カードの最安（このパックの版）を作る
function _pkBuildFromPerShop(st,res){
  const items=_pkItemsFromPerShop(res.perShop);
  const code=_pkPackCode(st,items);
  res.code=code;
  const byCard={};
  for(const it of items) (byCard[it.name]=byCard[it.name]||[]).push(it);
  for(const c of st.cards){
    const item=res.items[c.name];
    const its=byCard[c.name];
    if(!item||!its) continue;
    const mine=its.filter(it=>_pkSameVersion(code,it.code)).sort((a,b)=>a.price-b.price);
    if(mine.length){ const b=mine[0]; item.price=b.price; item.shop=b.shop||''; item.rarity=b.rarity||''; item.otherVersion=false; }
    else if(its.length){ item.price=null; item.other=true; }
  }
  res.matrix=_pkMatrixModel(st,items,code,res.shopOrder,res.failedByCard||{});
  res.found=Object.values(res.items).filter(it=>it.price!=null).length;
}

// ── まとめて検索（D） ──

// 1回に送るカードの上限（サーバーの MAX_DECK_CARDS=80）。超える商品は分けて順に送る（reviewer 低4。100種の商品がある）
const _PK_BULK_CHUNK=80;
// 続けて送ると同じ受付上限（3秒に1回）に当たるので、429 のときは少し待って送り直す
const _PK_BULK_RETRY_MS=3500;

async function bulkSearchPackPrices(){
  const st=_pk;
  if(!st||st.run||st.unreleased||!st.cards.length) return;
  const mode=st.mode, isBuy=mode==='buy';
  const ctl=new AbortController();
  const names=st.cards.map(c=>c.name);
  st.run={mode,done:0,total:names.length,abort:ctl,pending:new Set(names)};
  const res={mode,at:'',items:{},perShop:{},perShopCount:0,shopOrder:[],found:0,done:0,
    failedShops:[],failedCards:0,failedByCard:{},stopped:false,error:'',code:'',matrix:null};
  const prev=st.results[mode];
  st.results[mode]=res;
  _pkRefreshPrices();
  _ga('pack_bulk_search',{mode,count:names.length});
  const failed=new Set();
  const finish=()=>{
    if(st.req!==_pkReq) return;
    res.at=_pkHm();
    res.failedShops=[...failed];
    if(!isBuy&&res.perShopCount) _pkBuildFromPerShop(st,res);
    else res.found=Object.values(res.items).filter(it=>it.price!=null).length;
    st.run=null;
    if(!res.done&&!res.error) st.results[mode]=prev; // 1件も返らずに止めたときは、前の表示に戻す
    _pkRefreshPrices();
  };
  const onCard=(d,offset)=>{
    // 並びの番号で対応づける（カード名はサーバーが正式名に直して返すことがあるため。reviewer 高2）
    const c=st.cards[offset+(d.index|0)];
    if(!c) return;
    const best=d.best||null;
    const fs=d.failed_shops||[];
    fs.forEach(s=>failed.add(s));
    if(fs.length){ res.failedCards++; res.failedByCard[c.name]={shops:fs,all:d.status==='failed'}; }
    (d.successful_shops||[]).forEach(s=>{ if(!res.shopOrder.includes(s)) res.shopOrder.push(s); });
    const it={price:best?best.price:null,shop:best?best.shop||'':'',rarity:best?best.rarity||'':'',status:d.status||'',partial:!!(best&&fs.length),other:false,otherVersion:false};
    // 買取は店ごとの結果が無いので絞れない。最高値が他の商品の版なら、そう添える
    if(isBuy&&best&&st.info.code&&best.code&&!_pkSameVersion(String(st.info.code).toUpperCase(),best.code)) it.otherVersion=true;
    res.items[c.name]=it;
    if(d.per_shop){ res.perShop[c.name]=d.per_shop; res.perShopCount++; }
    res.done++;
    st.run.done++;
    st.run.pending.delete(c.name);
    _pkUpdateProgress(c.name);
  };
  try{
    for(let offset=0;offset<names.length;offset+=_PK_BULK_CHUNK){
      const part=names.slice(offset,offset+_PK_BULK_CHUNK);
      // カード数が多い弾ではGETのURL長制限（gunicorn limit_request_line=4094）を超えて400になるため、POSTで送る。
      // 販売は店ごと・レアリティごと・型番の頭ごとの価格も受け取る（店舗×レアの表と「このパックの版」の絞り込みに使う。
      // お店へ取りに行く回数は同じ）
      const body=new URLSearchParams({cards:part.join('|')});
      if(!isBuy) body.set('include_per_shop','version');
      let r=null;
      for(let tries=0;tries<4;tries++){
        // 店舗検索の混雑応答は ShopBusy が「順番待ち」を出して数秒後に取り直す（失敗にしない）
        r=await ShopBusy.fetch(isBuy?'/api/deck-buy':'/api/deck',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:body.toString(),signal:ctl.signal},{
          onWait:()=>_pkShowBusy(true), onResume:()=>_pkShowBusy(false),
        });
        if(r.status!==429) break;
        await new Promise(ok=>setTimeout(ok,_PK_BULK_RETRY_MS));
      }
      if(!r.ok){
        // レート制限（_consume_rate_limit）等は SSE ではなく JSON エラーで返る
        res.error=r.status===429?'アクセスが集中しています。少し時間をおいて再度お試しください':'検索に失敗しました。時間をおいて再度お試しください';
        break;
      }
      const reader=r.body.getReader(), decoder=new TextDecoder();
      let buffer='', gotDone=false;
      while(true){
        const {done,value}=await reader.read();
        if(done) break;
        buffer+=decoder.decode(value,{stream:true});
        const chunks=buffer.split('\n\n');
        buffer=chunks.pop()||'';
        for(const chunk of chunks){
          if(!chunk.startsWith('data: ')) continue;
          let d; try{ d=JSON.parse(chunk.slice(6)); }catch{ continue; }
          if(st.req!==_pkReq) continue;
          if(d.type==='card_done') onCard(d,offset);
          else if(d.type==='done') gotDone=true;
        }
      }
      // 終わりの合図（done）が来ないまま切れたら、残りを「見つからない」と数えず失敗として出す（reviewer 中2）
      if(!gotDone){ res.error=`通信が途中で切れました（${res.done}/${names.length}種まで）。もう一度お試しください`; break; }
    }
  }catch(e){
    if(e&&e.name==='AbortError') res.stopped=true;
    else if(e&&e.message&&e.message.indexOf('混雑')>=0) res.error=e.message; // ShopBusy が上限まで待っても空かなかった
    else res.error='エラーが発生しました。再度お試しください';
  }
  _pkShowBusy(false); // 待ちの途中で止めた・あきらめたときも「順番待ち」の札を残さない
  finish();
}

// 店舗検索の混雑で順番待ちのあいだ、進み具合の数字の横に「順番待ち」を出す
function _pkShowBusy(on){
  const n=document.getElementById('packProgN');
  if(!n) return;
  let tag=document.getElementById('packProgBusy');
  if(!on){ if(tag) tag.remove(); return; }
  if(!tag){ tag=document.createElement('span'); tag.id='packProgBusy'; n.after(tag); }
  tag.textContent=' '+ShopBusy.WAIT_TEXT;
}

// 検索中の進み具合と、届いたカードの価格だけ書き換える
function _pkUpdateProgress(name){
  const st=_pk;
  if(!st||!st.run) return;
  const n=document.getElementById('packProgN');
  if(n) n.textContent=st.run.done+'/'+st.run.total;
  const bar=document.querySelector('#packProg i');
  if(bar) bar.style.setProperty('--w',Math.round(100*st.run.done/st.run.total)+'%');
  if(st.view!=='list'||st.mode!==st.run.mode) return;
  st.cards.forEach((c,k)=>{
    if(c.name!==name) return;
    const cell=document.getElementById('packPrice'+k);
    if(cell) cell.innerHTML=_pkPriceHtml(st,c);
  });
}

// ── 操作 ──

function _pkInitDetailEvents(el){
  if(el.dataset.ready) return;
  el.dataset.ready='1';
  el.addEventListener('click',e=>{
    const st=_pk;
    if(!st) return;
    const t=e.target;
    const seg=t.closest('[data-view],[data-sort],[data-mode]');
    if(seg&&!seg.disabled){
      if(seg.dataset.view){ _pkSetView(seg.dataset.view); return; }
      if(seg.dataset.sort) st.sort=seg.dataset.sort;
      if(seg.dataset.mode) st.mode=seg.dataset.mode;
      _pkRefreshPrices();
      return;
    }
    if(t.closest('#packBulkBtn')){ bulkSearchPackPrices(); return; }
    const tog=t.closest('[data-mx-toggle]');
    if(tog){ // 他の商品の版を開く／畳む（表は作り直さない。カード画像を読み直すので）。PC の表とスマホの表の両方を同じに揃える
      const gi=tog.dataset.mxToggle;
      const open=tog.getAttribute('aria-expanded')!=='true';
      el.querySelectorAll(`[data-mx-of="${gi}"]`).forEach(x=>{ x.hidden=!open; });
      el.querySelectorAll(`[data-mx-toggle="${gi}"]`).forEach(b=>{
        b.setAttribute('aria-expanded',String(open));
        b.textContent=open?'他の商品の版をたたむ':`他の商品の版 ${b.dataset.n}行も見る`;
      });
      return;
    }
    const madd=t.closest('.pmc-add');
    if(madd){ _pkOpenMatrixAddMenu(madd); return; }
    if(t.closest('#packBulkStop')){ if(st.run&&st.run.abort) st.run.abort.abort(); return; }
    const more=t.closest('#packMoreBtn');
    if(more){ _pkOpenMore(more); return; }
    const add=t.closest('.pmx-add-btn');
    if(add){
      wishAdd(add.dataset.name,1,add.dataset.rarity||'');
      _ga('wish_add',{source:'pack_matrix',count:1});
      _pkToast('「'+add.dataset.name+'」（'+(add.dataset.rarity||'レアリティ指定なし')+'）を購入候補に入れました');
      return;
    }
    const row=t.closest('.pk-card');
    if(row){
      if(typeof _wishMenu!=='undefined'&&_wishMenu&&_wishMenu.ownerEl===row){ _wishCloseMenu(); return; }
      _pkOpenCardMenu(row,0,0,row);
    }
  });
  el.addEventListener('contextmenu',e=>{
    const row=e.target.closest('.pk-card');
    if(!row) return;
    e.preventDefault();
    _pkOpenCardMenu(row,e.clientX,e.clientY,null);
  });
  el.addEventListener('keydown',e=>{
    if(e.key!=='Enter'&&e.key!==' ') return;
    const row=e.target.closest('.pk-card');
    if(!row||e.target!==row) return;
    e.preventDefault();
    _pkOpenCardMenu(row,0,0,row);
  });
}

// スマホの店舗×レアの表: カード名の右の「+ 候補」から、レアリティを選んで購入候補に入れる（購入候補と同じ部品 _wishOpenMenu）
function _pkOpenMatrixAddMenu(btn){
  const st=_pk;
  if(!st||typeof _wishOpenMenu!=='function') return;
  if(typeof _wishMenu!=='undefined'&&_wishMenu&&_wishMenu.fromBtn===btn){ _wishCloseMenu(); return; }
  const r=_pkBulkMatrix(st);
  const model=r?r.matrix:st.dbModel;
  const g=model&&model.groups.find(x=>x.name===btn.dataset.name);
  if(!g||!g.main.length) return;
  const items=g.main.map(row=>{
    const ps=Object.values(row.cells).filter(c=>c&&!c.other).map(c=>c.price);
    return {label:row.rarity,sub:ps.length?'最安 '+_pkYen(Math.min(...ps)):'',fn:()=>{
      wishAdd(g.name,1,row.rarity);
      _ga('wish_add',{source:'pack_matrix',count:1});
      _pkToast('「'+g.name+'」（'+row.rarity+'）を購入候補に入れました');
    }};
  });
  _wishOpenMenu({title:g.name+' を購入候補に',fromBtn:btn,ownerEl:btn,ownerClass:'is-open',items});
}

// C: カードのメニュー（購入候補と同じ部品）
function _pkOpenCardMenu(row,x,y,fromBtn){
  const st=_pk;
  if(!st||typeof _wishOpenMenu!=='function') return;
  const c=st.cards[+row.dataset.k];
  if(!c) return;
  const name=c.name;
  const img=row.querySelector('.pack-card-thumb img');
  const inWish=wishGet().filter(w=>w.name===name).reduce((n,w)=>n+(w.qty||0),0);
  _wishOpenMenu({title:name,fromBtn,x,y,ownerEl:row,ownerClass:'is-menu',items:[
    {label:'購入候補に入れる',sub:'候補に '+inWish+'枚',fn:()=>{
      wishAdd(name,1,'');
      _ga('wish_add',{source:'pack_card',count:1});
      _pkToast('「'+name+'」を購入候補に入れました');
    }},
    {label:'カードの詳細',fn:()=>{
      if(typeof _metaOpenCardInfo==='function') _metaOpenCardInfo(name,img?img.src:'');
      else window.open('/card/'+encodeURIComponent(name),'_blank','noopener');
    }},
    {label:'価格を比べる',fn:()=>window.open('/card/'+encodeURIComponent(name),'_blank','noopener')},
  ]});
}

// E: ⋯ のメニュー
function _pkOpenMore(btn){
  const st=_pk;
  if(!st||typeof _wishOpenMenu!=='function') return;
  if(typeof _wishMenu!=='undefined'&&_wishMenu&&_wishMenu.fromBtn===btn){ _wishCloseMenu(); return; }
  const items=[];
  if(st.cards.length) items.push({label:'収録カードを全部購入候補に入れる',sub:st.cards.length+'枚',fn:_pkAllToWish});
  const links=[];
  const wiki=st.info.wiki_url&&safeUrl(st.info.wiki_url);
  const official=st.info.product_url&&safeUrl(st.info.product_url);
  if(wiki) links.push({label:'遊戯王Wikiの収録リストを開く',fn:()=>window.open(wiki,'_blank','noopener')});
  if(official) links.push({label:'公式の商品ページを開く',fn:()=>window.open(official,'_blank','noopener')});
  if(items.length&&links.length) items.push('sep');
  items.push(...links);
  if(!items.length) return;
  _wishOpenMenu({fromBtn:btn,ownerEl:btn,ownerClass:'is-open',items});
}

// 「収録カードを全部購入候補に入れる」（各1枚。購入候補は200件・1行99枚まで。環境デッキと同じ上限）
function _pkAllToWish(){
  const st=_pk;
  if(!st||!st.cards.length) return;
  const list=wishGet();
  const names=[...new Set(st.cards.map(c=>c.name))];
  const newRows=names.filter(n=>!list.some(w=>w.name===n&&!(w.rarity||''))).length;
  if(list.length+newRows>200){ _pkToast('購入候補は200件までです（あと'+Math.max(0,200-list.length)+'件）。先に候補を整理してください'); return; }
  let added=0;
  for(const n of names){
    const e=list.find(w=>w.name===n&&!(w.rarity||''));
    if(e){ if(e.qty<99){ e.qty++; added++; } }
    else{ list.push({name:n,qty:1,rarity:''}); added++; }
  }
  wishSave(list);
  if(typeof _wishPrices!=='undefined') _wishPrices=null;
  if(typeof trackDeckCards==='function') trackDeckCards(names.map(n=>({name:n,qty:1}))); // 価格を集めるカードに登録（環境デッキと同じ。reviewer 低7）
  _ga('wish_add',{source:'pack_all',count:added});
  _pkToast(added?'「'+st.name+'」の'+added+'枚を購入候補に入れました':'すでに購入候補に入っています（1行99枚まで）');
}

// ── 900px 以下: 詳細を全面で開く（環境デッキの _openMetaSheet と同じ作り） ──

let _pkSheetHistory=false, _pkPopReady=false, _pkSheetKeyReady=false, _pkSheetMqReady=false;
function _pkOnPop(){
  if(!_pkSheetHistory) return;
  _pkSheetHistory=false;
  closePackSheet(true);
}
function _pkWatchSheetMq(){
  if(_pkSheetMqReady||!window.matchMedia) return;
  _pkSheetMqReady=true;
  const mq=matchMedia(_PK_SHEET_MQ);
  const onChange=()=>{ if(!mq.matches) closePackSheet(); }; // 広い幅になったら閉じる（ページのスクロール止めを残さない）
  if(mq.addEventListener) mq.addEventListener('change',onChange); else if(mq.addListener) mq.addListener(onChange);
}
function _openPackSheet(){
  const pane=document.getElementById('packPane');
  if(!pane) return;
  pane.scrollTop=0;
  if(!window.matchMedia||!matchMedia(_PK_SHEET_MQ).matches) return;
  _pkWatchSheetMq();
  if(!_pkPopReady){ _pkPopReady=true; window.addEventListener('popstate',_pkOnPop); }
  // ブラウザの「戻る」で全面の詳細だけを閉じる（開くときに履歴を1つ足す）
  if(!pane.classList.contains('is-open')&&history.pushState){
    try{ history.pushState({packSheet:1},''); _pkSheetHistory=true; }catch(_){}
  }
  pane.classList.add('is-open');
  if(!_pkSheetKeyReady){ _pkSheetKeyReady=true; document.addEventListener('keydown',_pkSheetKey,true); } // 引き出しの Esc より先に見る
  document.documentElement.classList.add('pack-sheet-open');
}
// fromPop: ブラウザの「戻る」・タブの移動で閉じたとき（履歴は戻さない）
function closePackSheet(fromPop){
  const pane=document.getElementById('packPane');
  const wasOpen=!!(pane&&pane.classList.contains('is-open'));
  if(pane) pane.classList.remove('is-open');
  document.documentElement.classList.remove('pack-sheet-open');
  if(fromPop){ _pkSheetHistory=false; }
  else if(wasOpen&&_pkSheetHistory){ _pkSheetHistory=false; try{ history.back(); }catch(_){} }
}
function _pkSheetKey(e){
  if(e.key!=='Escape') return;
  const pane=document.getElementById('packPane');
  // カードの詳細の引き出し・メニューを開いているときは、そちらだけ閉じる
  if(document.getElementById('deckCardDrawer')?.classList.contains('open')) return;
  if(pane&&pane.classList.contains('is-open')&&!document.querySelector('.wl-menu')) closePackSheet();
}
