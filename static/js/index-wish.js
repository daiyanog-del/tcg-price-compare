// 元は templates/index.html のインライン <script>（2026-09-22 バッチCで外部化）。
// Jinja 依存の値は index.html 側の小さなインライン script が定義する。
// ── 購入候補（localStorage）＋ 値下がりアラート ──
const WISH_KEY='cardprice_wishlist';

// 直近の /api/wish-prices 結果キャッシュ（name -> {latest, base_7d, diff, pct, status}）
// null = まだロードしていない。{} 以上 = ロード試行済み（成功・失敗を問わず）
let _wishPrices = null;
let _wishPricesTime = 0;
const _WISH_PRICES_TTL = 10 * 60 * 1000; // 10分でキャッシュ失効
const _WISH_PRICES_CACHE_KEY = 'cardprice_wish_prices_v1';

// ページロード時に localStorage から即時復元
(function(){
  try{
    const raw=localStorage.getItem(_WISH_PRICES_CACHE_KEY);
    if(!raw) return;
    const saved=JSON.parse(raw);
    if(saved&&saved.data&&saved.time){
      _wishPrices=saved.data;
      _wishPricesTime=saved.time;
    }
  }catch{}
})();

function _saveWishPricesCache(){
  try{
    localStorage.setItem(_WISH_PRICES_CACHE_KEY,JSON.stringify({data:_wishPrices,time:_wishPricesTime}));
  }catch{}
}
function _clearWishPricesCache(){
  try{ localStorage.removeItem(_WISH_PRICES_CACHE_KEY); }catch{}
}

// /api/wish-prices を呼んで _wishPrices を更新する
async function loadWishPrices(){
  const list=wishGet();
  // ロード試行済みとしてマーク（null → {} で「試行済み」になる）
  _wishPrices={};
  _wishPricesTime=Date.now();
  if(!list.length){ _saveWishPricesCache(); return; }
  try{
    const res=await fetch('/api/wish-prices',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cards:list.map(c=>c.name)})});
    const data=await res.json();
    (data.cards||[]).forEach(c=>{_wishPrices[c.name]=c;});
    _saveWishPricesCache();
  }catch(e){/* _wishPrices={} のまま（再試行しない）*/}
}

// 価格情報の表示 HTML を生成する
function _wishPriceHtml(info){
  if(!info||info.status==='pending'){
    return '<span class="wish-price-info">相場データ収集中</span>';
  }
  if(info.latest==null) return '';
  const priceStr='¥'+info.latest.toLocaleString();
  if(info.pct==null||info.diff==null){
    return `<span class="wish-price-info">${priceStr}</span>`;
  }
  const isDrop=info.pct<=-5&&info.diff<=-50;
  const sign=info.diff>=0?'+':'';
  const pctStr=`${sign}${info.pct}%`;
  if(isDrop){
    return `<span class="wish-price-info wish-price-drop">${priceStr} <small>(${pctStr})</small></span>`;
  }
  return `<span class="wish-price-info">${priceStr} <small style="color:var(--text-d)">(${pctStr})</small></span>`;
}

// ── マイデッキ保存（localStorage）──
const SAVED_DECKS_KEY='cardprice_saved_decks';

function savedDecksGet(){
  try{return JSON.parse(localStorage.getItem(SAVED_DECKS_KEY))||[];}catch{return[];}
}
function savedDecksSet(list){
  localStorage.setItem(SAVED_DECKS_KEY,JSON.stringify(list));
  // 端末間同期（P2・保存デッキ）。sync-client.js 側でデバウンス・無限ループ防止を行う
  if(window.SyncClient) window.SyncClient.onDecksSave(list);
}

// 保存デッキのカードを収集対象(tracked_cards)に登録する（fire-and-forget）。
// 簡易計算(DB相場)のカバー率を継続的に高めるため。失敗してもUIには影響させない。
function trackDeckCards(cards){
  const names=[...new Set((cards||[]).map(c=>c.name).filter(Boolean))];
  if(!names.length) return;
  fetch('/api/track-batch',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cards:names})
  }).catch(()=>{});
}

function saveCurrentDeck(){
  const text=document.getElementById('deckTextarea').value.trim();
  if(!text){alert('デッキが入力されていません');return;}
  const name=prompt('デッキ名を入力してください');
  if(!name||!name.trim())return;
  const n=name.trim();
  // テキストが変更されていればcardsを再解析（[EX]区切りがあればエクストラデッキを分離）
  if(text!==_currentMydeckText){
    _currentMydeckCards=parseDeckSections(text);
    _currentMydeckText=text;
  }
  const {main,ex}=_currentMydeckCards;
  const list=savedDecksGet();
  const existing=list.find(d=>d.name===n);
  if(existing){
    if(!confirm(`「${n}」はすでに保存されています。上書きしますか？`))return;
    existing.text=text;
    existing.main=main;
    existing.ex=ex;
    existing.updated=Date.now();
    window._currentSavedDeckId = existing.id;
  }else{
    const newDeck={id:'d_'+Date.now(),name:n,text,main,ex,updated:Date.now()};
    list.push(newDeck);
    window._currentSavedDeckId = newDeck.id;
  }
  if(typeof _currentDeckName !== 'undefined') _currentDeckName = n;
  savedDecksSet(list);
  trackDeckCards([...main, ...ex]);
  renderSavedDecks();
}

function loadSavedDeck(id){
  const deck=savedDecksGet().find(d=>d.id===id);
  if(!deck)return;
  _currentDeckName=deck.name||'マイデッキ';
  // 選択状態を更新
  document.querySelectorAll('.saved-deck-card-btn').forEach(b=>b.classList.remove('selected'));
  const btn=document.querySelector(`#sdcard-${CSS.escape(id)} .saved-deck-card-btn`);
  if(btn)btn.classList.add('selected');
  // main/ex を正規化（旧データ対応）してセット
  const{main,ex}=normalizeDeck(deck);
  _currentMydeckCards={main,ex};
  // CRLF(\r\n)をLF(\n)に正規化: textareaがLFに変換するため、比較ミスを防ぐ
  const combined=(deck.text||[...main,...ex].map(c=>c.qty>1?`${c.qty} ${c.name}`:c.name).join('\n')).replace(/\r/g,'');
  _currentMydeckText=combined;
  document.getElementById('deckTextarea').value=combined;
  // デッキ選択時は自動で価格計算しない。カード一覧のプレビューだけ表示する
  calcDeckEstimate(DECK_CTX.mydeck,{previewOnly:true});
}

function deleteSavedDeck(id){
  const deck=savedDecksGet().find(d=>d.id===id);
  if(!deck)return;
  if(!confirm(`「${deck.name}」を削除しますか？`))return;
  savedDecksSet(savedDecksGet().filter(d=>d.id!==id));
  renderSavedDecks();
}

function renderSavedDecks(){
  const el=document.getElementById('savedDeckList');
  if(!el)return;
  const list=savedDecksGet();
  const newBtn='<button class="deck-btn-text saved-deck-new-btn" onclick="deckCreateNew()">＋ 新規デッキを作成</button>';
  if(!list.length){
    el.innerHTML='<div class="saved-deck-label"><span>保存済みデッキ</span>'+newBtn+'</div>'
      +'<p class="saved-deck-empty">保存済みデッキがありません。<br>Chrome拡張またはPDFからデッキを取り込むか、「＋ 新規デッキを作成」から始められます。</p>';
    return;
  }
  // 各デッキの先頭カード名をサムネイル用に取得
  const firstCards=list.map(d=>{
    const m=Array.isArray(d.main)?d.main:normalizeDeck(d).main;
    return m&&m.length?m[0].name:'';
  });
  el.innerHTML='<div class="saved-deck-label"><span>保存済みデッキ</span>'+newBtn+'</div>'
    +'<div class="saved-deck-grid">'
    +list.map((d,i)=>`
    <div class="saved-deck-card" id="sdcard-${escAttr(d.id)}">
      <button class="saved-deck-card-btn" onclick="loadSavedDeck('${escAttr(escJs(d.id))}')">
        <div class="saved-deck-thumb"${firstCards[i]?` data-first-card="${escAttr(firstCards[i])}"`:''}></div>
        <div class="saved-deck-card-name">${esc(d.name)}</div>
      </button>
      <button class="saved-deck-remove" onclick="deleteSavedDeck('${escAttr(escJs(d.id))}')" title="削除">&times;</button>
    </div>`).join('')
    +'</div>';
  _loadSavedDeckThumbnails();
}

function _loadSavedDeckThumbnails(){
  const thumbs=[...document.querySelectorAll('.saved-deck-thumb[data-first-card]')];
  if(!thumbs.length)return;
  const names=[...new Set(thumbs.map(t=>t.dataset.firstCard).filter(Boolean))];
  fetch('/api/card-images',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({names})
  })
  .then(r=>r.json())
  .then(({images})=>{
    if(!images)return;
    thumbs.forEach(t=>{
      const url=_batchImgUrl(images[t.dataset.firstCard]);
      if(url&&safeUrl(url)) t.innerHTML=`<img src="${escAttr(safeUrl(url))}" alt="" loading="lazy">`;
    });
  })
  .catch(()=>{});
}

function wishGet(){
  try{
    const list=JSON.parse(localStorage.getItem(WISH_KEY))||[];
    // 旧形式 {name,qty} は rarity:"" として読み出し時に補完（マイグレーション不要）
    return list.map(c=>({name:c.name,qty:c.qty,rarity:c.rarity||""}));
  }catch{return[];}
}
function wishSave(list){
  localStorage.setItem(WISH_KEY,JSON.stringify(list));
  wishUpdateBadge();
  syncPushSubscription();
  // 端末間同期（P1・購入候補のみ）。sync-client.js 側でデバウンス・無限ループ防止を行う
  if(window.SyncClient) window.SyncClient.onWishSave(list);
}
// (name, rarity) ペアの一致を見るヘルパ。rarity は未指定="" として比較する
function _wishMatchKey(c,name,rarity){
  return c.name===name && (c.rarity||"")===(rarity||"");
}
function wishAdd(name,qty,rarity){
  rarity=rarity||"";
  const list=wishGet();
  const existing=list.find(c=>_wishMatchKey(c,name,rarity));
  if(existing){existing.qty+=qty||1;}
  else{list.push({name:name,qty:qty||1,rarity:rarity});_wishPrices=null;} // 新カードは価格キャッシュをリセット
  wishSave(list);
  // 価格収集対象に登録（fire-and-forget。失敗しても UI に影響しない）
  fetch('/api/track',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({card:name})}).catch(()=>{});
}
function wishRemove(name,rarity){
  rarity=rarity||"";
  const list=wishGet().filter(c=>!_wishMatchKey(c,name,rarity));
  wishSave(list);
  renderWishlist();
}
function wishClearAll(){
  if(!confirm('購入候補をすべて削除しますか？'))return;
  wishSave([]);
  _wishPrices=null;
  _clearWishPricesCache();
  renderWishlist();
}
function wishUpdateBadge(){
  const list=wishGet();
  const badge=document.getElementById('wishTabBadge');
  if(!badge)return;
  if(!list.length){badge.style.display='none';return;}
  // 値下がりカード数: pct <= -5% かつ diff <= -50円（未ロード時は 0）
  const dropCount=Object.values(_wishPrices||{}).filter(
    d=>d.pct!=null&&d.pct<=-5&&d.diff!=null&&d.diff<=-50
  ).length;
  if(dropCount>0){
    badge.textContent='↓'+dropCount;
    badge.className='wish-badge wish-badge-drop';
  }else{
    badge.textContent=list.length;
    badge.className='wish-badge';
  }
  badge.style.display='inline-flex';
}
function wishUpdateQty(name,newQty,rarity){
  rarity=rarity||"";
  if(newQty<1)return wishRemove(name,rarity);
  const list=wishGet();
  const item=list.find(c=>_wishMatchKey(c,name,rarity));
  if(item){item.qty=newQty;wishSave(list);renderWishlist();}
}
// 行のレアリティを変更する。空文字 = 未指定（最安レア）
function wishSetRarity(name,oldRarity,newRarity){
  oldRarity=oldRarity||""; newRarity=newRarity||"";
  if(oldRarity===newRarity)return;
  const list=wishGet();
  // 同名同レアリティ（新側）が既にあれば、現在の qty を合算してから旧行を削除
  const target=list.find(c=>_wishMatchKey(c,name,oldRarity));
  if(!target)return;
  const collide=list.find(c=>c!==target && _wishMatchKey(c,name,newRarity));
  if(collide){
    collide.qty=Math.min(collide.qty+target.qty,99);
    const idx=list.indexOf(target);
    list.splice(idx,1);
  }else{
    target.rarity=newRarity;
  }
  wishSave(list);
  // 古い見積結果・店舗ランキングは無効になるので片付ける
  const wr=document.getElementById('wishResults');if(wr)wr.classList.add('hidden');
  const wsr=document.getElementById('wishShopRanking');if(wsr){wsr.classList.add('hidden');wsr.innerHTML='';}
  renderWishlist();
}

// レアリティ候補キャッシュ ({name: [rarity,...]}). renderWishlist で必要時に取得
let _wishRarities = {};
let _wishRaritiesNames = ''; // キャッシュ対象名のシグネチャ（カードが追加されたら再取得）

function _loadWishRarities(names){
  // 既にキャッシュ済みのカードと一致するなら再取得しない
  const sig=[...names].sort().join('|');
  if(sig===_wishRaritiesNames) return Promise.resolve(_wishRarities);
  return fetch('/api/card-rarities',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cards:names})
  }).then(r=>r.json()).then(d=>{
    _wishRarities=d.rarities||{};
    _wishRaritiesNames=sig;
    return _wishRarities;
  }).catch(()=>_wishRarities);
}

// 1行ぶんのレアリティ ドロップダウンHTML
function _wishRaritySelectHtml(c){
  const candidates=_wishRarities[c.name]||[];
  const opts=['<option value="">未指定（最安レア）</option>']
    .concat(candidates.map(r=>`<option value="${escAttr(r)}"${r===(c.rarity||"")?' selected':''}>${esc(r)}</option>`));
  // 選択中なのに候補リストに無い rarity は欠落しないように追加表示する
  if(c.rarity && !candidates.includes(c.rarity)){
    opts.splice(1,0,`<option value="${escAttr(c.rarity)}" selected>${esc(c.rarity)}</option>`);
  }
  return `<select class="wish-rarity-select" data-name="${escAttr(c.name)}" data-old="${escAttr(c.rarity||"")}" onchange="wishSetRarity(this.dataset.name,this.dataset.old,this.value)" title="レアリティを指定（未指定なら最安レアリティ）">${opts.join('')}</select>`;
}

function renderWishlist(){
  const list=wishGet();
  updatePushUI(!!localStorage.getItem(PUSH_SUB_KEY));
  const el=document.getElementById('wishList');
  const actions=document.getElementById('wishActions');
  if(!list.length){
    el.innerHTML='<div class="wish-empty">購入候補はまだありません<br><span style="font-size:.78rem;margin-top:8px;display:block">検索結果やマイデッキから「+ 候補」ボタンで追加できます</span></div>';
    actions.style.display='none';
    // リストが空になったら見積結果・店舗ランキングも片付ける
    const wr=document.getElementById('wishResults');if(wr)wr.classList.add('hidden');
    const wsr=document.getElementById('wishShopRanking');if(wsr){wsr.classList.add('hidden');wsr.innerHTML='';}
    return;
  }
  actions.style.display='flex';
  // 未ロード or TTL切れ の場合にロードして再描画
  if(_wishPrices===null || Date.now()-_wishPricesTime > _WISH_PRICES_TTL){
    loadWishPrices().then(()=>{wishUpdateBadge();renderWishlist();});
  }
  // レアリティ候補をロード（カード名集合が変わったときだけリクエスト）。
  // 取得後は select の option だけ最新化する（行 DOM 全体を再描画しないことでフォーカスを保つ）。
  const uniqueNames=[...new Set(list.map(c=>c.name))];
  _loadWishRarities(uniqueNames).then(()=>{
    el.querySelectorAll('.wish-rarity-select').forEach(sel=>{
      const name=sel.dataset.name; const old=sel.dataset.old;
      sel.outerHTML=_wishRaritySelectHtml({name,rarity:old});
    });
  });
  el.innerHTML=list.map(c=>{
    const info=_wishPrices[c.name];
    const isDrop=info&&info.pct!=null&&info.pct<=-5&&info.diff!=null&&info.diff<=-50;
    const isPending=!info||info.status==='pending';
    const rowClass='wish-card-row'+(isDrop?' wish-drop':'')+(isPending?' wish-pending':'');
    const wishImgSafe=info&&info.image?safeUrl(info.image):'';
    const imgHtml=wishImgSafe?`<div class="wish-card-img"><img src="${escAttr(wishImgSafe)}" alt="" loading="lazy"></div>`:'<div class="wish-card-img"></div>';
    const rj=escAttr(escJs(c.rarity||""));
    return `
    <div class="${rowClass}">
      ${imgHtml}
      <span class="wish-card-qty">
        <button class="wish-card-remove" onclick="wishUpdateQty('${escAttr(escJs(c.name))}',${c.qty-1},'${rj}')" title="1枚減らす" style="font-size:.7rem">-</button>
        ${c.qty}x
        <button class="wish-card-remove" onclick="wishUpdateQty('${escAttr(escJs(c.name))}',${c.qty+1},'${rj}')" title="1枚増やす" style="font-size:.7rem">+</button>
      </span>
      <span class="wish-card-name">${esc(c.name)}</span>
      ${_wishRaritySelectHtml(c)}
      ${_wishPriceHtml(info)}
      <a class="wish-card-link" href="/card/${encodeURIComponent(c.name)}" target="_blank" rel="noopener">価格を見る</a>
      <button class="wish-card-remove" onclick="wishRemove('${escAttr(escJs(c.name))}','${rj}')" title="削除">&times;</button>
    </div>`}).join('');
}

function wishToEstimate(){
  const list=wishGet();
  if(!list.length)return;
  // 全店舗ランキングが既に開いていれば閉じる（同時表示で画面が混雑するのを避ける）
  const rankEl=document.getElementById('wishShopRanking');
  if(rankEl){rankEl.classList.add('hidden');rankEl.innerHTML='';}
  // 専用関数を呼ぶ。/api/wish-shop-totals + /api/deck SSE で rarity 対応の見積を出す
  calcWishEstimate();
}

// 購入候補リスト専用の見積。/api/wish-shop-totals の全店舗集計から各エントリの最安店を抽出し、
// DB未収録のエントリは /api/deck SSE で realtime 補完する（rarity フィルタ付き）。
async function calcWishEstimate(){
  const list=wishGet();
  if(!list.length)return;
  const resultsEl=document.getElementById('wishResults');
  const listEl=document.getElementById('wishCardList');
  const totalEl=document.getElementById('wishTotalPrice');
  const progEl=document.getElementById('wishProgress');
  const shareEl=document.getElementById('wishShareArea');
  const totalLabel=document.querySelector('#wishResults .deck-total-label');
  resultsEl.classList.remove('hidden');
  resultsEl.scrollIntoView({block:'start'});
  totalEl.textContent='取得中...';
  if(totalLabel) totalLabel.textContent='デッキ相場（直近データ）';
  progEl.textContent='';
  shareEl.innerHTML='';

  // 既存 renderDeckGrid を流用（rarity は wishlist 行UIで表示するのでグリッドはカード名+枚数のみ）
  const cards=list.map(c=>({name:c.name,qty:c.qty}));
  renderDeckGrid(cards,DECK_CTX.wishlist,cards.length);
  loadDeckGridImages(listEl);

  // Phase 1: DB集計（/api/wish-shop-totals）
  let resp;
  try{
    const res=await fetch('/api/wish-shop-totals',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cards:list.map(c=>({name:c.name,qty:c.qty,rarity:c.rarity||""}))})
    });
    resp=await res.json();
  }catch(e){
    progEl.textContent='エラーが発生しました。再度お試しください。';
    return;
  }
  if(resp.error){progEl.textContent=resp.error;return;}

  // 照合はサーバ正本（entries：名前補正・レアリティ正規化・重複統合済み）で行う。
  // list（localStorageの生データ）のまま突合すると、保存値がサーバの正規化形と
  // 食い違ったとき（例: rarity:"ウルトラレア"）DBに価格があるのに「データなし」扱いで
  // 総額から丸ごと落ちるバグがあった（2026-08-19 修正、まとめ買い機能と同根の原因）。
  // entries は重複統合で件数・順序が変わりうるため、list と完全一致しない場合だけ
  // グリッド（wish-row-<i>）を entries ベースで描き直す（一致するときは通常ケースとして
  // 描き直さず、画像の再取得やちらつきを避ける）。
  const srvEntries=resp.entries;
  let workingList=list;
  if(Array.isArray(srvEntries)&&srvEntries.length){
    const built=WishEstimate.buildWorkingList(list,srvEntries);
    workingList=built.workingList;
    if(built.needsRedraw){
      const redrawnCards=workingList.map(c=>({name:c.name,qty:c.qty}));
      renderDeckGrid(redrawnCards,DECK_CTX.wishlist,redrawnCards.length);
      loadDeckGridImages(listEl);
    }
  }else if(Array.isArray(srvEntries)){
    // entries が空配列で返るのは全カードがサーバ側バリデーションで弾かれた場合（カード名が
    // 長すぎる等）。突合キーが作れないので list をそのまま使うが、原因調査ができるよう警告する
    console.warn('[wish-estimate] サーバから entries が空で返りました。list をそのまま使います。',list);
  }

  // 各エントリ (name,rarity) について、全店舗の中で最安を抽出する（照合ロジックは
  // wish-estimate.js に集約。テストが本番ロジックを直接検証できるようにするため）
  const entryBest=workingList.map(e=>WishEstimate.findBestPrice(e,resp.shops||[]));
  const missingIdxs=[];
  entryBest.forEach((best,i)=>{ if(!best) missingIdxs.push(i); });

  // db_missing（サーバ側が正当に「どの店にも無い」と判定したキー）に無いのに突合できな
  // かったエントリが残っていたら、照合キー不一致の疑いがある。「落ちたことが分からない」
  // のが本来の問題なので、必ずコンソールに警告する
  const suspicious=WishEstimate.detectSuspiciousMisses(workingList,entryBest,resp.db_missing||[]);
  if(suspicious.length){
    console.warn('[wish-estimate] DBに価格があるはずなのに突合できなかったエントリがあります（照合キー不一致の疑い）:',suspicious);
  }

  // Phase 1 描画
  let total=0, found=0;
  for(let i=0;i<workingList.length;i++){
    const row=document.getElementById('wish-row-'+i);
    if(!row)continue;
    const best=entryBest[i];
    const c=workingList[i];
    if(best){
      const lineTotal=best.price*c.qty;
      total+=lineTotal; found++;
      row.className='deck-grid-cell';
      row.title=best.shop+(best.rarity?` (${best.rarity})`:'');
      row.querySelector('.deck-card-price').textContent=`¥${lineTotal.toLocaleString()}`;
      row.querySelector('.deck-card-shop').textContent=best.shop;
      row.querySelector('.deck-card-status').textContent=c.qty>1?`@¥${best.price.toLocaleString()}`:'';
      const u=_wishShopSearchUrl(best.shop,c.name);
      row.querySelector('.deck-card-name').innerHTML=u
        ?`<a href="${escAttr(u)}" target="_blank" rel="noopener">${esc(c.name)}</a>`:esc(c.name);
    }
  }
  totalEl.textContent=`¥${total.toLocaleString()}`;
  if(!missingIdxs.length){
    progEl.textContent=`完了 — ${found}/${workingList.length}枚の相場を取得`;
    return;
  }

  // Phase 2: 不足エントリだけリアルタイム検索
  totalEl.innerHTML=`¥${total.toLocaleString()} <span style="font-size:.7em;color:var(--text-d)">+ ${missingIdxs.length}枚検索中</span>`;
  progEl.textContent=`${found}枚を即時取得 / 残り${missingIdxs.length}枚をリアルタイム検索中...`;

  const missingCardsParam=missingIdxs.map(i=>{
    const c=workingList[i]; return c.qty>1?`${c.qty} ${c.name}`:c.name;
  }).join('|');
  const url='/api/deck?include_per_shop=1&cards='+encodeURIComponent(missingCardsParam);
  let sseFound=0, sseNotFound=0;
  try{
    const sseRes=await fetch(url);
    const reader=sseRes.body.getReader();
    const decoder=new TextDecoder();
    let buffer='';
    while(true){
      const{done,value}=await reader.read();
      if(done)break;
      buffer+=decoder.decode(value,{stream:true});
      const chunks=buffer.split('\n\n');
      buffer=chunks.pop()||'';
      for(const chunk of chunks){
        if(!chunk.startsWith('data: '))continue;
        const d=JSON.parse(chunk.slice(6));
        if(d.type==='card_done'){
          const entryIdx=missingIdxs[d.index];
          if(entryIdx==null)continue;
          const entry=workingList[entryIdx];
          const perShop=d.per_shop||{};
          // per_shop は {店舗名:{レアリティ:{price,url,rarity,...}}}。店ごとの採用候補は
          // pickPerShopItem に集約（rarity 指定があれば一致店舗だけ採用、未指定なら
          // その店の全 rarity から最安）。テストが本番ロジックを直接検証できるようにする
          let chosen=null;
          for(const shop of Object.keys(perShop)){
            const info=WishEstimate.pickPerShopItem(perShop[shop],entry.rarity);
            if(!info)continue;
            if(!chosen||info.price<chosen.price){
              chosen={price:info.price,shop,rarity:info.rarity||"",url:info.url||""};
            }
          }
          const row=document.getElementById('wish-row-'+entryIdx);
          if(!row)continue;
          if(chosen){
            const lineTotal=chosen.price*entry.qty;
            total+=lineTotal; sseFound++;
            row.className='deck-grid-cell';
            row.title=chosen.shop+(chosen.rarity?` (${chosen.rarity})`:'');
            row.querySelector('.deck-card-price').textContent=`¥${lineTotal.toLocaleString()}`;
            row.querySelector('.deck-card-shop').textContent=chosen.shop;
            row.querySelector('.deck-card-status').textContent=entry.qty>1?`@¥${chosen.price.toLocaleString()}`:'';
            row.querySelector('.deck-card-name').innerHTML=chosen.url
              ?`<a href="${escAttr(safeUrl(chosen.url))}" target="_blank" rel="noopener">${esc(entry.name)}</a>`:esc(entry.name);
          }else{
            sseNotFound++;
            row.className='deck-grid-cell error';
            const priceEl=row.querySelector('.deck-card-price');
            if(priceEl) priceEl.textContent=entry.rarity?`${entry.rarity}データなし`:'データなし';
          }
          const remaining=missingIdxs.length-(sseFound+sseNotFound);
          if(remaining>0){
            totalEl.innerHTML=`¥${total.toLocaleString()} <span style="font-size:.7em;color:var(--text-d)">+ ${remaining}枚検索中</span>`;
          }else{
            totalEl.textContent=`¥${total.toLocaleString()}`;
          }
          progEl.textContent=`${found}枚を即時取得 / 残り${missingIdxs.length}枚中${sseFound+sseNotFound}枚完了`;
        }else if(d.type==='done'){
          totalEl.textContent=`¥${total.toLocaleString()}`;
          const totalFound=found+sseFound;
          progEl.textContent=`完了 — ${totalFound}/${workingList.length}枚の価格を取得${sseNotFound>0?` (${sseNotFound}枚未取得)`:''}`;
        }
      }
    }
  }catch(e){
    progEl.textContent='リアルタイム検索でエラーが発生しました。';
  }
}

// ── 全店舗ランキング（まとめ買い最安店舗） ──
// 販売店舗ごとのカード検索URLを生成する。バックエンドの _shop_search_url と同じURLパターンを再現する。
function _wishShopSearchUrl(shop, name){
  const q=encodeURIComponent((name||'').normalize('NFKC'));
  const urls={
    '遊々亭':`https://yuyu-tei.jp/sell/ygo/s/search?search_word=${q}`,
    'カードラッシュ':`https://www.cardrush.jp/product-list?keyword=${q}`,
    'トレコロCB':`https://www.torecolo.jp/shop/goods/search.aspx?search=x&keyword=${q}&category=&oshiire_code=`,
    'カーナベル':`https://www.ka-nabell.com/?act=sell_search&genre=1&keyword=${q}`,
    'カードラボ':`https://www.c-labo-online.jp/product-list?keyword=${q}`,
    'まんぞく屋':`https://shopmanzokuya.com/products/list?category_id=1&name=${q}&orderby=price_l&disp_number=100`,
  };
  return urls[shop]||'';
}

// (name, rarity) から一意のマップキーを作るヘルパ
function _wishEntryKey(name,rarity){return (name||'')+''+(rarity||'');}

// まとめ買い最安店舗を調査するボタンのハンドラ
async function wishCheapestStores(){
  const list=wishGet();
  if(!list.length)return;

  // 見積結果が出ていれば閉じる（同時表示で画面が混雑するのを避ける）
  const resultsEl=document.getElementById('wishResults');
  if(resultsEl)resultsEl.classList.add('hidden');

  const rankEl=document.getElementById('wishShopRanking');
  rankEl.classList.remove('hidden');
  rankEl.innerHTML='<div class="wish-shop-ranking-loading">集計中...</div>';
  rankEl.scrollIntoView({block:'start'});

  // Step 1: DB相場で各店舗の合計を即時集計
  let dbData;
  try{
    const res=await fetch('/api/wish-shop-totals',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cards:list.map(c=>({name:c.name,qty:c.qty,rarity:c.rarity||""}))})
    });
    dbData=await res.json();
  }catch(e){
    rankEl.innerHTML='<div class="wish-shop-ranking-error">集計に失敗しました。再度お試しください。</div>';
    return;
  }

  // 照合キーはサーバ正本（entries）で統一する。list（localStorageの生データ）で
  // キーを作ると、サーバ側の名前補正・レアリティ正規化とズレて分割プランからカードが
  // 黙って脱落するバグがあった（2026-08-19 修正）。list は表示・localStorage操作用に残す
  const srvEntries=(dbData&&dbData.entries)||[];
  if(!dbData||!dbData.shops||!dbData.shops.length||!srvEntries.length){
    rankEl.innerHTML='<div class="wish-shop-ranking-error">店舗データが取得できませんでした。</div>';
    return;
  }

  // 送料ルールはサーバから配られる（shipping.py が正本）。リアルタイム検索で合計が
  // 後から増え、送料無料しきい値をまたぐと送料が変わるため、フロントでも同じ計算をやり直す。
  const shippingRules=dbData.shipping_rules||{};
  const shippingCheckedOn=dbData.shipping_checked_on||'';

  // 商品代金の合計から、送料・注文可否をまとめて求める
  const _wishShipping=(shop,subtotal)=>{
    const r=shippingRules[shop];
    if(!r)return{fee:null,total:subtotal,freeAt:null,shortBy:null,orderable:true};
    // 1枚も揃わない店舗（subtotal=0）は送料も最低注文金額も判定対象外。
    // 「金額が足りない」ではなく「買う物が無い」ためで、揃う枚数の表示で伝わる。
    if(subtotal<=0)return{fee:0,total:0,freeAt:null,shortBy:null,orderable:true};
    const free=(r.free_threshold!==null&&r.free_threshold!==undefined&&subtotal>=r.free_threshold);
    const fee=free?0:r.base_fee;
    let freeAt=null;
    if(r.free_threshold!==null&&r.free_threshold!==undefined&&subtotal<r.free_threshold){
      freeAt=r.free_threshold-subtotal;
    }
    let shortBy=null;
    if(r.min_order!==null&&r.min_order!==undefined&&subtotal<r.min_order){
      shortBy=r.min_order-subtotal;
    }
    return{fee,total:subtotal+fee,freeAt,shortBy,orderable:shortBy===null};
  };

  // 店舗別の集計状態。items_map / missing_set のキーは (name,rarity) の合成キーで、同名別レアリティを別エントリ扱い
  const shopState={};
  for(const s of dbData.shops){
    const items_map=new Map();
    for(const it of (s.items||[])){
      // キーは希望レアリティ（rarity_pref）で作る。採用レアリティ（it.rarity）で作ると
      // 未指定エントリ（""）のキーと一致せず、エントリとの突合が全て外れる
      const prefKey=(it.rarity_pref!==undefined&&it.rarity_pref!==null)?it.rarity_pref:(it.rarity||"");
      items_map.set(_wishEntryKey(it.name,prefKey),{
        name:it.name, qty:it.qty, price:it.price, rarity:it.rarity||""
      });
    }
    const missing_set=new Set((s.missing_cards||[]).map(mc=>_wishEntryKey(mc.name,mc.rarity||"")));
    shopState[s.shop]={
      total:s.total||0,
      covered_qty:s.covered_qty||0,
      covered_cards:s.covered_cards||0,
      total_qty:s.total_qty||0,
      total_cards:s.total_cards||0,
      missing_set,
      items_map,
    };
  }
  // db_missing は {name, rarity} オブジェクト配列
  const dbMissingEntries=(dbData.db_missing||[]).map(m=>({name:m.name,rarity:m.rarity||""}));
  const dbMissingKeys=new Set(dbMissingEntries.map(m=>_wishEntryKey(m.name,m.rarity)));
  // 枚数・種類数もサーバ正本（srvEntries）から算出する。list（localStorage生データ）は
  // サーバの重複統合（同名同レアリティのqty加算）を経ていないためズレうる
  const totalQty=srvEntries.reduce((s,c)=>s+c.qty,0);
  const totalCards=srvEntries.length;

  // 買い物指示書（分割プラン内訳）の開閉状態。SSE更新の再描画で閉じ戻らないよう閉包で持つ
  let splitOpen=false;
  // 照合キー不一致の検出（droppedEntries）を初回renderRanking呼び出しだけに抑えるフラグ。
  // dbMissingKeys はSSE経路でカードが見つかるたびに delete されるため、SSEの再描画時に
  // 判定するとリアルタイムで見つからなかった正当な欠落まで誤検知する（2026-08-19レビュー指摘）
  let baselineDropCheckDone=false;

  // エントリの逆引きテーブル（サーバ正本のentriesから作る）
  const entryByKey=new Map(srvEntries.map(c=>[_wishEntryKey(c.name,c.rarity||""),{name:c.name,qty:c.qty,rarity:c.rarity||""}]));
  const _missingLabel=(key)=>{
    const e=entryByKey.get(key);
    if(!e) return key;
    return e.rarity ? `${e.name}(${e.rarity})` : e.name;
  };

  const renderRanking=(opts)=>{
    const shops=Object.entries(shopState).map(([shop,st])=>{
      // total は商品代金の合計。SSE で増えるたびにここで送料を計算し直す
      const sh=_wishShipping(shop,st.total);
      return {shop,...st,ship:sh};
    });
    // 並び順: 全枚数そろう店舗を優先 → 注文できる店舗を優先 → 送料込み合計が安い順
    shops.sort((a,b)=>{
      const af=a.covered_qty===totalQty?0:1;
      const bf=b.covered_qty===totalQty?0:1;
      if(af!==bf)return af-bf;
      if(a.covered_qty!==b.covered_qty)return b.covered_qty-a.covered_qty;
      const ao=a.ship.orderable?0:1;
      const bo=b.ship.orderable?0:1;
      if(ao!==bo)return ao-bo;
      return a.ship.total-b.ship.total;
    });

    const realtimeBanner=opts.realtimePending
      ?`<div class="wish-shop-ranking-status">リアルタイム検索中... 残り${opts.realtimePending}枚`+(opts.realtimeError?` (一部失敗 ${opts.realtimeError}枚)`:'')+`</div>`
      :(opts.realtimeDone?`<div class="wish-shop-ranking-status">完了`+(opts.realtimeError?` (取得できなかったカード ${opts.realtimeError}枚)`:'')+`</div>`:'');

    const rowsHtml=shops.map((s,i)=>{
      const allCovered=s.covered_qty===s.total_qty;
      const badge=allCovered?'<span class="wish-shop-badge ok">✓ すべて揃う</span>':`<span class="wish-shop-badge ng">${s.missing_set.size}種類欠け</span>`;
      // 表示する合計は送料込み。内訳（商品＋送料）を下に添える
      const totalText=s.total>0?`¥${s.ship.total.toLocaleString()}`:'-';
      let breakdown='';
      if(s.total>0&&s.ship.fee!==null){
        breakdown=s.ship.fee===0
          ?`商品¥${s.total.toLocaleString()}＋送料無料`
          :`商品¥${s.total.toLocaleString()}＋送料¥${s.ship.fee.toLocaleString()}`;
      }else if(s.total>0){
        breakdown=`商品¥${s.total.toLocaleString()}（送料不明）`;
      }
      // 注文できない店舗と、あと少しで送料無料になる店舗は行内で理由を出す
      let notice='';
      if(s.total>0&&!s.ship.orderable){
        notice=`<div class="wish-shop-notice ng">最低注文金額に¥${s.ship.shortBy.toLocaleString()}足りないため注文できません</div>`;
      }else if(s.total>0&&s.ship.freeAt!==null){
        notice=`<div class="wish-shop-notice">あと¥${s.ship.freeAt.toLocaleString()}で送料無料</div>`;
      }
      return `
        <div class="wish-shop-row${s.ship.orderable?'':' unorderable'}" data-shop-idx="${i}">
          <div class="wish-shop-row-summary" onclick="_wishToggleShopDetail(${i})">
            <span class="wish-shop-rank">${i+1}</span>
            <span class="wish-shop-name">${esc(s.shop)}</span>
            <span class="wish-shop-total">${totalText}${breakdown?`<span class="wish-shop-breakdown">${breakdown}</span>`:''}</span>
            <span class="wish-shop-cov">${s.covered_qty}/${s.total_qty}枚</span>
            ${badge}
            <span class="wish-shop-toggle">▾</span>
          </div>
          ${notice}
          <div class="wish-shop-detail" id="wishShopDetail-${i}" hidden>${_wishShopDetailHtml(s,_missingLabel)}</div>
        </div>`;
    }).join('');

    const dbMissingHtml=dbMissingKeys.size>0
      ?`<div class="wish-shop-ranking-warn">どの店舗にもデータが無いカード: ${[...dbMissingKeys].map(k=>esc(_missingLabel(k))).join('、')}</div>`
      :'';

    // 複数店に分けた場合の最安プラン（wish-split.js）。ヒント行をクリックすると
    // 「どのカードをどの店で買うか」の買い物指示書が開く（2026-08-05 実データで
    // ¥1,000規模の差額を確認したため本実装）。
    // 表示額は実行可能なプランの合計なので過大表示にはならない（控えめに出る側にしか外れない）。
    let splitHtml='';
    if(window.WishSplit){
      // entries/priceTable のキーはサーバ正本（srvEntries）から作る。list（localStorage
      // 生データ）から作ると、サーバの名前補正・レアリティ正規化とキーが食い違い、
      // 分割プランからカードが警告無しで脱落する（2026-08-19 修正の本体）
      const entries=srvEntries.map(c=>({key:_wishEntryKey(c.name,c.rarity||""),qty:c.qty}));
      const priceTable={};
      for(const e of entries){
        priceTable[e.key]={};
        for(const [shop,st] of Object.entries(shopState)){
          const it=st.items_map.get(e.key);
          if(it)priceTable[e.key][shop]=it.price;
        }
      }
      // priceTableを組んだ直後に、どの店にも価格が無いエントリを検出する。DB側の正当な
      // 欠落（dbMissingKeys）は除外し、それ以外が残ったら照合キーの不一致を疑って警告する。
      // dbMissingKeys はSSEでカードが見つかるたびに delete される（4531行付近）ため、
      // SSEで削られた後に判定すると「リアルタイム検索で見つからなかった正当な欠落」まで
      // 誤検知してしまう。SSEが始まる前の初回renderRanking呼び出し（=dbMissingKeysが
      // まだ無傷）でのみ、1回だけ判定する（2026-08-19レビュー指摘）
      if(!baselineDropCheckDone){
        baselineDropCheckDone=true;
        const droppedEntries=entries.filter(e=>Object.keys(priceTable[e.key]).length===0&&!dbMissingKeys.has(e.key));
        if(droppedEntries.length){
          console.warn('[wish-split] priceTableでどの店にも価格が見つからないエントリがあります（照合キー不一致の疑い）:',droppedEntries.map(e=>e.key));
        }
      }
      const split=WishSplit.computeBestSplit(entries,priceTable,Object.keys(shopState),shippingRules);
      if(split&&split.used.length>=2){
        const shopNames=split.used.map(u=>esc(u.shop)).join('＋');
        // 比較対象（baseline）: 分割プランと同じカード集合（キー）を1店舗で全部揃えられる店の
        // うち、その集合ぶんの小計（店の全カード合計 s.total ではない）で送料・注文可否を判定
        // した上での最安（送料込み）。covered_qty の一致だけで選ぶと中身が違う店と比較して
        // しまう（2026-08-19 修正: カードラボの別集合8枚と比較していたバグ）。
        // 純関数へ切り出し済み（wish-split.js）。テストが本番ロジックを直接検証できるように
        // するため、index.htmlにはキー集合を組んで呼ぶだけの配線しか置かない
        const splitKeys=[...split.assign.keys()];
        const qtyByKey=new Map(entries.map(e=>[e.key,e.qty]));
        const{hasAll,baseline}=WishSplit.selectSingleShopBaseline(
          splitKeys,qtyByKey,priceTable,Object.keys(shopState),shippingRules);
        let hintText='';
        if(!hasAll){
          hintText=`1店舗ではそろいませんが、${shopNames} の${split.used.length}店舗に分ければ${split.coveredQty}枚そろって ¥${split.total.toLocaleString()}（送料込み）で買えます`;
        }else if(baseline===null){
          // splitKeysを全部持つ店はあるが、その集合ぶんの小計が最低注文金額に届かず注文
          // できない（＝比較対象が無い）。「1店舗ではそろいません」は事実と違うため使わない。
          // 節約額も比較対象が無い以上主張できないため「安くなる」とは書かない
          hintText=`1店舗でそろう店はありますが最低注文金額に届きません。${shopNames} の${split.used.length}店舗に分ければ${split.coveredQty}枚そろって ¥${split.total.toLocaleString()}（送料込み）で買えます`;
        }else if(baseline-split.total>0){
          hintText=`${shopNames} の${split.used.length}店舗に分けて買うと ¥${split.total.toLocaleString()}（送料込み・${split.coveredQty}枚）— 同じ${split.coveredQty}枚を1店舗でまとめる最安より¥${(baseline-split.total).toLocaleString()}安くなる買い方があります`;
        }
        if(hintText){
          splitHtml=`
            <div class="wish-shop-split-hint clickable" onclick="_wishToggleSplitDetail()">
              <span>${hintText}</span>
              <span class="wish-shop-toggle">${splitOpen?'▴':'▾'}</span>
            </div>
            <div class="wish-shop-detail wish-split-detail" id="wishSplitDetail" ${splitOpen?'':'hidden'}>${_wishSplitDetailHtml(split,shopState)}</div>`;
        }
      }
    }

    rankEl.innerHTML=`
      <div class="wish-shop-ranking-head">
        <div class="wish-shop-ranking-title">まとめ買い最安店舗（直近7日のDB相場${opts.realtimeDone||opts.realtimePending?'＋リアルタイム検索':''}）</div>
        <button class="wish-shop-ranking-close" onclick="document.getElementById('wishShopRanking').classList.add('hidden')" aria-label="閉じる">&times;</button>
      </div>
      ${realtimeBanner}
      <div class="wish-shop-list">${rowsHtml}</div>
      ${splitHtml}
      ${dbMissingHtml}
      <div class="wish-shop-ranking-foot">対象: ${totalCards}種類 / ${totalQty}枚${shippingCheckedOn?`　／　送料は各店で最も安い配送方法・最小の厚さを前提にした${esc(shippingCheckedOn)}時点の情報です（地域・枚数により実際は高くなる場合があります）`:''}</div>
      <div class="wish-shop-ranking-foot">価格は取得時点のものです。購入前に各店の商品ページでご確認ください。</div>
    `;
  };

  // window 公開（行クリック時の展開用）
  window._wishShopState=shopState;
  window._wishToggleShopDetail=(idx)=>{
    const el=document.getElementById('wishShopDetail-'+idx);
    if(!el)return;
    el.hidden=!el.hidden;
  };
  // 買い物指示書の開閉。開閉状態は閉包の splitOpen に保存し、SSE更新の再描画でも維持する
  window._wishToggleSplitDetail=()=>{
    splitOpen=!splitOpen;
    const el=document.getElementById('wishSplitDetail');
    if(el)el.hidden=!splitOpen;
    const arrow=document.querySelector('.wish-shop-split-hint .wish-shop-toggle');
    if(arrow)arrow.textContent=splitOpen?'▴':'▾';
  };

  renderRanking({realtimePending:dbMissingEntries.length});

  // Step 2: DB に無いエントリだけリアルタイム補完（rarity 一致でフィルタ）
  if(!dbMissingEntries.length)return;

  // SSE の d.index → エントリへの逆引き
  const missingList=dbMissingEntries.map(m=>{
    const e=entryByKey.get(_wishEntryKey(m.name,m.rarity));
    return {name:m.name, rarity:m.rarity, qty:e?e.qty:1};
  });
  const missingCardsParam=missingList.map(m=>m.qty>1?`${m.qty} ${m.name}`:m.name).join('|');
  const shopParams=Object.keys(shopState).map(s=>'shops='+encodeURIComponent(s)).join('&');
  const url='/api/deck?include_per_shop=1&cards='+encodeURIComponent(missingCardsParam)+(shopParams?'&'+shopParams:'');

  let sseError=0;
  try{
    const sseRes=await fetch(url);
    const reader=sseRes.body.getReader();
    const decoder=new TextDecoder();
    let buffer='';
    while(true){
      const{done,value}=await reader.read();
      if(done)break;
      buffer+=decoder.decode(value,{stream:true});
      const chunks=buffer.split('\n\n');
      buffer=chunks.pop()||'';
      for(const chunk of chunks){
        if(!chunk.startsWith('data: '))continue;
        const d=JSON.parse(chunk.slice(6));
        if(d.type!=='card_done')continue;
        const entry=missingList[d.index];
        if(!entry)continue;
        const perShop=d.per_shop||{};
        const key=_wishEntryKey(entry.name,entry.rarity);
        let anyShop=false;
        for(const shop of Object.keys(shopState)){
          // per_shop は {店舗名:{レアリティ:{price,url,rarity,...}}}。rarity 指定が
          // あれば一致店舗のみ採用、未指定ならその店の全 rarity から最安（pickPerShopItem）
          const info=WishEstimate.pickPerShopItem(perShop[shop],entry.rarity);
          if(!info)continue;
          anyShop=true;
          const st=shopState[shop];
          if(!st.items_map.has(key)){
            st.items_map.set(key,{name:entry.name,qty:entry.qty,price:info.price,rarity:info.rarity||entry.rarity||"",url:info.url||''});
            st.total+=info.price*entry.qty;
            st.covered_qty+=entry.qty;
            st.covered_cards+=1;
            st.missing_set.delete(key);
          }
        }
        if(!anyShop)sseError+=1;
        dbMissingKeys.delete(key);
        renderRanking({realtimePending:dbMissingKeys.size,realtimeError:sseError});
      }
    }
    renderRanking({realtimePending:0,realtimeDone:true,realtimeError:sseError});
  }catch(e){
    renderRanking({realtimePending:0,realtimeDone:true,realtimeError:sseError});
  }
}

// 買い物指示書のHTML（複数店分割プランの内訳。店舗ごとに買うカード・小計・送料を並べる）
function _wishSplitDetailHtml(split, shopState){
  const shopBlocks=split.used.map(u=>{
    const st=shopState[u.shop]||{items_map:new Map()};
    // この店舗に割り当てられたエントリ（assign の挿入順＝リスト順を保つ）
    const keys=[...split.assign.entries()].filter(([,s])=>s===u.shop).map(([k])=>k);
    let qtySum=0;
    const rows=keys.map(k=>{
      const it=st.items_map.get(k);
      if(!it)return '';
      qtySum+=it.qty;
      const url=safeUrl(it.url||_wishShopSearchUrl(u.shop,it.name));
      const rarityLabel=it.rarity?`<span class="wish-shop-detail-rarity">${esc(it.rarity)}</span>`:'';
      return `<div class="wish-shop-detail-row">
        <span class="wish-shop-detail-qty">${it.qty}x</span>
        <a class="wish-shop-detail-name" href="${escAttr(url)}" target="_blank" rel="noopener">${esc(it.name)}</a>
        ${rarityLabel}
        <span class="wish-shop-detail-price">¥${(it.price*it.qty).toLocaleString()}<span class="wish-shop-detail-unit">@¥${it.price.toLocaleString()}</span></span>
      </div>`;
    }).join('');
    const feeText=u.shipping===0?'送料無料':`送料¥${u.shipping.toLocaleString()}`;
    const shopUrl=_wishShopSearchUrl(u.shop,'');
    const shopLink=shopUrl?`<a class="wish-split-shop-link" href="${escAttr(safeUrl(shopUrl))}" target="_blank" rel="noopener">開く</a>`:'';
    return `<div class="wish-split-shop">
      <div class="wish-split-shop-head">
        <span class="wish-split-shop-name">${esc(u.shop)}</span>
        <span class="wish-split-shop-count">${u.entryCount}種${qtySum}枚</span>
        <span class="wish-split-shop-sub">商品¥${u.subtotal.toLocaleString()}＋${feeText}</span>
        ${shopLink}
      </div>
      ${rows}
    </div>`;
  }).join('');
  return shopBlocks+`
    <div class="wish-shop-detail-row wish-shop-detail-sum">
      <span class="wish-shop-detail-name">合計（送料込み・${split.used.length}店舗）</span>
      <span class="wish-shop-detail-price">¥${split.total.toLocaleString()}</span>
    </div>`;
}

// 店舗詳細パネルのHTML（カード一覧と購入リンク）
function _wishShopDetailHtml(shopRow, missingLabel){
  const entries=[...shopRow.items_map.entries()];  // [[key, {name,qty,price,rarity,url?}], ...]
  const itemsHtml=entries.map(([key,it])=>{
    const url=safeUrl(it.url||_wishShopSearchUrl(shopRow.shop,it.name));
    const rarityLabel=it.rarity?`<span class="wish-shop-detail-rarity">${esc(it.rarity)}</span>`:'';
    return `<div class="wish-shop-detail-row">
      <span class="wish-shop-detail-qty">${it.qty}x</span>
      <a class="wish-shop-detail-name" href="${escAttr(url)}" target="_blank" rel="noopener">${esc(it.name)}</a>
      ${rarityLabel}
      <span class="wish-shop-detail-price">¥${(it.price*it.qty).toLocaleString()}<span class="wish-shop-detail-unit">@¥${it.price.toLocaleString()}</span></span>
    </div>`;
  }).join('');
  // 送料・最低注文金額の行（商品明細の下に、支払額の内訳として出す）
  let shipHtml='';
  const sh=shopRow.ship;
  if(sh&&shopRow.total>0){
    const feeText=sh.fee===null?'不明':(sh.fee===0?'無料':`¥${sh.fee.toLocaleString()}`);
    shipHtml=`<div class="wish-shop-detail-row wish-shop-detail-ship">
      <span class="wish-shop-detail-name">送料</span>
      <span class="wish-shop-detail-price">${feeText}</span>
    </div>
    <div class="wish-shop-detail-row wish-shop-detail-sum">
      <span class="wish-shop-detail-name">合計（送料込み）</span>
      <span class="wish-shop-detail-price">¥${sh.total.toLocaleString()}</span>
    </div>`;
    if(!sh.orderable){
      shipHtml+=`<div class="wish-shop-detail-missing">この店舗は最低注文金額に¥${sh.shortBy.toLocaleString()}足りないため、このリストだけでは注文できません</div>`;
    }
  }
  const missingHtml=shopRow.missing_set.size>0
    ?`<div class="wish-shop-detail-missing">この店舗にデータが無いカード: ${[...shopRow.missing_set].map(k=>esc(missingLabel?missingLabel(k):k)).join('、')}</div>`
    :'';
  const shopTopUrl=_wishShopSearchUrl(shopRow.shop,'');
  const openHref=shopTopUrl?`<a class="wish-shop-detail-link" href="${escAttr(safeUrl(shopTopUrl))}" target="_blank" rel="noopener">${esc(shopRow.shop)}を開く</a>`:'';
  return itemsHtml+shipHtml+missingHtml+`<div class="wish-shop-detail-foot">${openHref}</div>`;
}

// 追加ボタンのHTMLを生成するヘルパー
function wishBtnHtml(name,qty){
  return `<button class="wish-add" onclick="event.stopPropagation();wishAddFromBtn(this,'${escAttr(escJs(name))}',${qty||1})" title="購入候補に追加">+ 候補</button>`;
}
function wishAddFromBtn(btn,name,qty){
  wishAdd(name,qty);
  const orig=btn.textContent;
  btn.classList.add('added');
  btn.textContent='\u2713 \u8ffd\u52a0';
  setTimeout(()=>{btn.classList.remove('added');btn.textContent=orig;},1200);
}

// ── プッシュ通知 ──
const PUSH_SUB_KEY = 'cardprice_push_subscribed'; // 購読済みフラグ(localStorage)

// 通知ボックスの表示状態を更新する
function updatePushUI(subscribed){
  // Push配信は停止中のためUIを隠す（2026-08-20決定）。復活時はこの行を外す
  return;
  const box = document.getElementById('pushNotifyBox');
  const btn = document.getElementById('pushNotifyBtn');
  const desc = document.getElementById('pushNotifyDesc');
  if(!box||!btn) return;
  if(!('serviceWorker' in navigator)||!('PushManager' in window)){
    // 非対応ブラウザは非表示
    return;
  }
  box.style.display='flex';
  if(subscribed){
    btn.textContent='通知をオフにする';
    btn.className='push-notify-btn active';
    desc.textContent='値下がり通知が設定されています（毎朝5時頃）';
  }else{
    btn.textContent='通知を受け取る';
    btn.className='push-notify-btn';
    desc.textContent='購入候補カードが値下がりしたら通知します（毎朝5時頃）';
  }
}

async function togglePushSubscription(){
  const btn = document.getElementById('pushNotifyBtn');
  if(btn) btn.disabled=true;
  const subscribed = !!localStorage.getItem(PUSH_SUB_KEY);
  if(subscribed){
    await unsubscribePush();
  }else{
    await subscribePush();
  }
  if(btn) btn.disabled=false;
}

async function subscribePush(){
  try{
    const perm = await Notification.requestPermission();
    if(perm!=='granted'){ alert('通知が許可されていません。ブラウザの設定で許可してください。'); return; }

    // VAPID 公開鍵を取得
    const keyRes = await fetch('/api/push/vapid-key');
    if(!keyRes.ok){ alert('通知サービスが準備中です。しばらくお待ちください。'); return; }
    const {publicKey} = await keyRes.json();

    // Service Worker が登録済みであることを確認
    const reg = await navigator.serviceWorker.ready;

    // Push 購読を作成
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: _urlBase64ToUint8Array(publicKey),
    });

    // サーバーに登録
    const cards = wishGet().map(c=>c.name);
    const res = await fetch('/api/push/subscribe',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({subscription: sub.toJSON(), cards}),
    });
    const data = await res.json();
    if(data.ok){
      localStorage.setItem(PUSH_SUB_KEY,'1');
      updatePushUI(true);
    }else{
      alert('登録に失敗しました。しばらくしてから再試行してください。');
    }
  }catch(e){
    console.error('[push] subscribe error:', e);
    alert('通知の設定に失敗しました: '+e.message);
  }
}

async function unsubscribePush(){
  try{
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if(sub){
      await fetch('/api/push/unsubscribe',{
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({endpoint: sub.endpoint}),
      });
      await sub.unsubscribe();
    }
    localStorage.removeItem(PUSH_SUB_KEY);
    updatePushUI(false);
  }catch(e){
    console.error('[push] unsubscribe error:', e);
  }
}

// wishlist が変わったら購読情報も同期する（カードの追加/削除に追従）
async function syncPushSubscription(){
  if(!localStorage.getItem(PUSH_SUB_KEY)) return;
  try{
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if(!sub) return;
    const cards = wishGet().map(c=>c.name);
    await fetch('/api/push/subscribe',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({subscription: sub.toJSON(), cards}),
    });
  }catch(e){ console.warn('[push] sync error:', e); }
}

// base64url → Uint8Array（applicationServerKey に必要な変換）
function _urlBase64ToUint8Array(b64){
  const pad = '='.repeat((4-b64.length%4)%4);
  const b = atob((b64+pad).replace(/-/g,'+').replace(/_/g,'/'));
  return Uint8Array.from(b,c=>c.charCodeAt(0));
}

// 起動時: 価格データをロードしてからバッジ更新（サイトを開いた時に値下がりをバッジ表示）
loadWishPrices().then(wishUpdateBadge);
// 通知UIの初期状態を反映
updatePushUI(!!localStorage.getItem(PUSH_SUB_KEY));
