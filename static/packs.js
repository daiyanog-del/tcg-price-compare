// 最新弾タブ（パック）関連の処理。
// CLAUDE.md「巨大ファイル段階的分割（ついで方式）」に従い index.html から切り出し。
// esc / escJs / _setupThumbObserver / searchCard などのヘルパは index.html 本体の
// グローバル関数を参照する（このファイルは非モジュールで読み込むこと）。

let _packsLoaded=false;
let _packData=[];
// 現在パック詳細に表示中の収録カード名（一括検索の対象）
let _currentPackCards=[];
// 一括検索の多重実行ガード
let _bulkSearching=false;

async function loadPacks(){
  if(_packsLoaded) return;
  const el=document.getElementById('packList');
  const CACHE_KEY='tcgym_packs_cache', TTL=6*3600*1000; // 6時間
  try{
    const raw=sessionStorage.getItem(CACHE_KEY);
    if(raw){
      const {ts,data}=JSON.parse(raw);
      if(Date.now()-ts<TTL){_packData=data;_packsLoaded=true;renderPacks();return;}
    }
  }catch{}
  el.innerHTML='<div class="meta-loading">パック情報を読込中...</div>';
  try{
    const res=await fetch('/api/packs');
    _packData=await res.json();
    _packsLoaded=true;
    try{sessionStorage.setItem(CACHE_KEY,JSON.stringify({ts:Date.now(),data:_packData}));}catch{}
    renderPacks();
  }catch(e){
    el.innerHTML='<div class="meta-loading">パック情報の取得に失敗しました</div>';
  }
}

function renderPacks(){
  const el=document.getElementById('packList');
  if(!_packData.length){el.innerHTML='<div class="meta-loading">パックデータがありません</div>';return;}

  const today=new Date().toISOString().slice(0,10);
  // 日付で降順ソート
  const sorted=[..._packData].sort((a,b)=>(b.date||'').localeCompare(a.date||''));

  let html='<div class="meta-deck-list" style="margin-bottom:12px">';
  for(const p of sorted){
    const dateStr=p.date?p.date.replace(/-/g,'/'):'';
    const unreleased=p.date&&p.date>today;
    const badge=unreleased?'<span class="pack-unreleased-badge">発売前</span>':'';
    const featuredFlag=p.featured?'true':'false';
    html+=`<button class="meta-deck-btn" onclick="loadPackCards('${esc(p.name)}','${esc(p.wiki_page||p.name)}','${esc(p.tcg_name||'')}',${featuredFlag})" id="pack-btn-${esc(p.code||p.name)}">
      <span class="pack-btn-name">${esc(p.name)}${badge}</span><span class="meta-share">${dateStr}</span>
    </button>`;
  }
  html+='</div>';
  html+='<div class="meta-note">遊戯王Wikiより収録カードリストを取得</div>';
  el.innerHTML=html;
}

// 収録カード1行のHTMLを生成（一括検索で価格を上書きできるよう行ID・価格spanを付与）。
// price が与えられた場合は初期表示（新弾フィーチャーのDB価格）として表示し data-price もセット。
function _packCardRow(i, name, rarity, price){
  const hasPrice=(price!==null&&price!==undefined&&price!=='');
  const priceStr=hasPrice?`¥${Number(price).toLocaleString()}`:'';
  const dataPrice=hasPrice?` data-price="${Number(price)}"`:'';
  return `<div class="pack-card-item clickable" id="pack-row-${i}"${dataPrice} onclick="searchCard('${escJs(name)}')">`
    +`<span class="pack-card-thumb" data-card="${esc(name)}"></span>`
    +`<span class="pack-card-num">${i+1}</span>`
    +`<span class="pack-card-name">${esc(name)}</span>`
    +`<span class="pack-card-rarity meta-share">${rarity?esc(rarity):''}</span>`
    +`<span class="pack-card-price meta-share" style="margin-left:auto">${priceStr}</span>`
    +`</div>`;
}

// 一括検索バー（ボタン＋販売/買取トグル＋進捗）のHTML。
function _packBulkBarHtml(){
  return `<div class="pack-bulk-bar" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:4px 0 8px">`
    +`<button id="packBulkBtn" class="share-btn share-btn-copy" onclick="bulkSearchPackPrices()">💴 新カードの価格をまとめて検索</button>`
    +`<label class="meta-note" style="display:flex;align-items:center;gap:3px;cursor:pointer"><input type="radio" name="packBulkMode" value="sale" checked>販売最安</label>`
    +`<label class="meta-note" style="display:flex;align-items:center;gap:3px;cursor:pointer"><input type="radio" name="packBulkMode" value="buy">買取最高</label>`
    +`<span id="packBulkProgress" class="meta-note"></span>`
    +`</div>`;
}

async function loadPackCards(packName, wikiPage, tcgName, isFeatured){
  const detailEl=document.getElementById('packDetail');
  // 選択状態を更新
  document.querySelectorAll('#packList .meta-deck-btn').forEach(b=>b.classList.remove('selected'));
  const packBtn=document.getElementById('pack-btn-'+(packName));
  if(packBtn) packBtn.classList.add('selected');

  detailEl.classList.remove('hidden');
  detailEl.innerHTML='<div class="meta-loading">カードリストを読込中...</div>';

  // 新弾（featured フラグあり）の場合は価格付きデータを /api/featured から取得
  if(isFeatured){
    _loadFeaturedIntoPackDetail(packName, detailEl);
    return;
  }

  try{
    let apiUrl='/api/packs/cards?name='+encodeURIComponent(packName)+'&wiki='+encodeURIComponent(wikiPage);
    if(tcgName) apiUrl+='&tcg='+encodeURIComponent(tcgName);
    const res=await fetch(apiUrl);
    const data=await res.json();

    if(!data.cards||!data.cards.length){
      detailEl.innerHTML='<div class="meta-loading">「'+esc(packName)+'」のカードリストが取得できませんでした</div>';
      return;
    }

    _currentPackCards=data.cards.slice();

    // カードチップ表示（クリックで個別検索、リスト形式）
    let listHtml='';
    data.cards.forEach((name,i)=>{
      listHtml+=_packCardRow(i, name, '', '');
    });

    detailEl.innerHTML='<h4>'+esc(packName)+' — 収録カード（'+data.count+'種）</h4>'
      +_packBulkBarHtml()
      +'<div class="meta-note" style="margin-bottom:8px">カード名をタップすると個別検索／まとめて検索でリアルタイム相場を取得します</div>'
      +'<div class="pack-card-grid" id="packDetailGrid" style="max-height:400px;overflow-y:auto">'+listHtml+'</div>';
    _setupThumbObserver('#packDetailGrid');
  }catch(e){
    detailEl.innerHTML='<div class="meta-loading">読込に失敗しました</div>';
  }
}

async function _loadFeaturedIntoPackDetail(packName, detailEl){
  try{
    const res=await fetch('/api/featured');
    const data=await res.json();
    if(data&&data.loading){
      // サーバー側キャッシュ構築中。2.5秒後に再試行
      detailEl.innerHTML='<div class="meta-loading">新弾情報を準備中... しばらくお待ちください</div>';
      setTimeout(()=>_loadFeaturedIntoPackDetail(packName, detailEl), 2500);
      return;
    }
    if(!data||!data.pack){
      detailEl.innerHTML='<div class="meta-loading">新弾情報を取得できませんでした</div>';
      return;
    }
    const pack=data.pack;
    const days=data.days_since_release>=0?`発売${data.days_since_release+1}日目`:'発売日';
    const cards=data.cards||[];

    let html=`<h4 style="margin:0 0 4px">${esc(pack.pack_name)}</h4>`
      +`<div class="meta-note" style="margin-bottom:8px">${days} — 収録カード ${cards.length}種の価格一覧</div>`;

    if(!cards.length){
      html+='<div class="meta-loading">収録カード情報を取得中です（しばらくお待ちください）</div>';
      detailEl.innerHTML=html;
      return;
    }

    _currentPackCards=cards.map(c=>c.name);

    // 一括検索バーを見出し直下に表示（DB価格を初期表示、ボタンでリアルタイム上書き）
    html+=_packBulkBarHtml();

    let listHtml='<div class="pack-card-grid" id="packDetailGrid" style="max-height:400px;overflow-y:auto">';
    cards.forEach((c,i)=>{
      // 発売日当日などDB価格が無い場合は price=null → 空表示。一括検索で埋める。
      listHtml+=_packCardRow(i, c.name, c.rarity||'', c.price);
    });
    listHtml+='</div>';
    listHtml+='<div class="meta-note" style="margin-top:6px">カード名をタップすると個別検索／まとめて検索でリアルタイム相場を取得します</div>';
    detailEl.innerHTML=html+listHtml;
    _setupThumbObserver('#packDetailGrid');
  }catch(e){
    detailEl.innerHTML='<div class="meta-loading">読込に失敗しました</div>';
  }
}

// 収録カードを価格降順に並べ替える（価格未取得は最下部）。
function _sortPackRowsByPrice(grid){
  const rows=[...grid.querySelectorAll('.pack-card-item')];
  rows.sort((a,b)=>{
    const pa=a.dataset.price!==undefined?parseFloat(a.dataset.price):-1;
    const pb=b.dataset.price!==undefined?parseFloat(b.dataset.price):-1;
    return pb-pa;
  });
  rows.forEach(r=>grid.appendChild(r));
}

// 表示中パックの収録カードを一括でリアルタイム検索する。
// 既存の /api/deck（販売最安）/ /api/deck-buy（買取最高）SSE をそのまま流用し、
// card_done を受けるたびに該当行の価格を上書きする。
async function bulkSearchPackPrices(){
  if(_bulkSearching) return;
  const cards=_currentPackCards||[];
  if(!cards.length) return;

  const modeEl=document.querySelector('input[name="packBulkMode"]:checked');
  const isBuy=!!(modeEl&&modeEl.value==='buy');
  const btn=document.getElementById('packBulkBtn');
  const progEl=document.getElementById('packBulkProgress');
  const grid=document.getElementById('packDetailGrid');
  if(!grid) return;

  _bulkSearching=true;
  if(btn) btn.disabled=true;
  const total=cards.length;
  let done=0,found=0;
  if(progEl) progEl.textContent=`${total}種を一括検索します…（数分かかる場合があります）`;

  // カード数が多い弾ではGETのURL長制限（gunicorn limit_request_line=4094）を
  // 超えて400になるため、POSTでカード名をボディに載せて送る。
  const params=new URLSearchParams({cards:cards.join('|')});
  const apiUrl=isBuy?'/api/deck-buy':'/api/deck';

  try{
    const res=await fetch(apiUrl,{
      method:'POST',
      headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:params.toString()
    });
    if(!res.ok){
      // レート制限（_consume_rate_limit）等は SSE ではなく JSON エラーで返る
      let msg='検索に失敗しました。時間をおいて再度お試しください';
      if(res.status===429) msg='アクセスが集中しています。少し時間をおいて再度お試しください';
      if(progEl) progEl.textContent=msg;
      return;
    }
    const reader=res.body.getReader();
    const decoder=new TextDecoder();
    let buffer='';

    while(true){
      const{done:rdone,value}=await reader.read();
      if(rdone) break;
      buffer+=decoder.decode(value,{stream:true});
      const chunks=buffer.split('\n\n');
      buffer=chunks.pop()||'';

      for(const chunk of chunks){
        if(!chunk.startsWith('data: ')) continue;
        let d;
        try{ d=JSON.parse(chunk.slice(6)); }catch{ continue; }

        if(d.type==='card_start'){
          const row=document.getElementById('pack-row-'+d.index);
          if(row){
            const priceEl=row.querySelector('.pack-card-price');
            if(priceEl) priceEl.textContent='…';
          }
        }
        else if(d.type==='card_done'){
          done++;
          const row=document.getElementById('pack-row-'+d.index);
          if(row){
            const priceEl=row.querySelector('.pack-card-price');
            const rarityEl=row.querySelector('.pack-card-rarity');
            if(d.best){
              found++;
              const p=d.best.price;
              row.dataset.price=p;
              if(priceEl){
                priceEl.textContent=`¥${Number(p).toLocaleString()}`;
                priceEl.title=d.best.shop||'';
                priceEl.style.color=isBuy?'var(--green)':'';
              }
              if(rarityEl&&!rarityEl.textContent&&d.best.rarity) rarityEl.textContent=d.best.rarity;
            }else{
              row.dataset.price='-1';
              if(priceEl){ priceEl.textContent='--'; priceEl.style.color=''; priceEl.title=''; }
            }
          }
          if(progEl) progEl.textContent=`${done}/${total} 検索中…`;
        }
        else if(d.type==='done'){
          // ループ脱出後にまとめてソート・完了表示
        }
      }
    }

    // 完了: 価格降順に並べ替え
    _sortPackRowsByPrice(grid);
    const label=isBuy?'買取価格':'販売価格';
    if(progEl) progEl.textContent=`完了 — ${found}/${total}種の${label}を取得${found<total?`（${total-found}種は在庫/取得なし）`:''}`;
  }catch(e){
    if(progEl) progEl.textContent='エラーが発生しました。再度お試しください';
  }finally{
    _bulkSearching=false;
    if(btn) btn.disabled=false;
  }
}
