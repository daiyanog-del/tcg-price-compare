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
    if(window.DeckOwnership) existing.owned=window.DeckOwnership.snapshot();
    existing.updated=Date.now();
    window._currentSavedDeckId = existing.id;
  }else{
    const newDeck={id:'d_'+Date.now(),name:n,text,main,ex,updated:Date.now()};
    if(window.DeckOwnership) newDeck.owned=window.DeckOwnership.snapshot();
    list.push(newDeck);
    window._currentSavedDeckId = newDeck.id;
  }
  if(typeof _currentDeckName !== 'undefined') _currentDeckName = n;
  savedDecksSet(list);
  _ga('deck_save',{where:'top',kind:existing?'update':'new'});
  trackDeckCards([...main, ...ex]);
  renderSavedDecks();
}

function loadSavedDeck(id){
  const deck=savedDecksGet().find(d=>d.id===id);
  if(!deck)return;
  if(window.DeckOwnership) window.DeckOwnership.set(deck.owned);
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
  const libraryHeader='<div class="saved-deck-label"><span>保存済みデッキ</span><div class="deck-library-actions">'
    +'<button class="deck-btn-text saved-deck-new-btn" onclick="deckCreateNew()">＋ 新規作成</button>'
    +'<button class="deck-btn-text" onclick="openSyncShareDialog()">端末同期</button></div></div>';
  if(!list.length){
    el.innerHTML=libraryHeader+'<p class="saved-deck-empty">保存済みデッキはありません。</p>';
    return;
  }
  // 各デッキの先頭カード名をサムネイル用に取得
  const firstCards=list.map(d=>{
    const m=normalizeDeck(d).main;
    return m&&m.length?m[0].name:'';
  });
  el.innerHTML=libraryHeader
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
  // 古い比較結果は無効になるので片付ける
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
    // リストが空になったら比較結果も片付ける
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

// ── どこで買うと安いか（まとめ買い比較） ──
// 2026-10-19 作り直し: 旧「このリストで見積もり」（送料抜きでカードごとの最安店を足した額）と
// 「まとめ買いの最安店舗を調査」を1つに統合。先頭に「一番安くそろう買い方」（送料込みの最安。
// 複数店に分かれるなら最初から店ごとに並べる）、その下に店ごとの支払いを出す。
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
function _wishEntryKey(name,rarity){return (name||'')+''+(rarity||'');}

const _wishYen=n=>'¥'+n.toLocaleString();
const _WISH_CHEV='<svg class="wb-chev" width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M2 4l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';

// 「どこで買うと安いか」の結果から店のページへ移ったか（流れの最後の段の計測）
document.getElementById('wishShopRanking')?.addEventListener('click',e=>{
  const a=e.target.closest('a[target="_blank"]');
  if(!a)return;
  const shop=a.closest('.wb-shop')?.dataset.k?.replace(/^shop:/,'')||'';
  _ga('wish_shop_click',{link:a.classList.contains('wb-shop-link')?'shop':'item',shop});
});

// 「どこで買うと安いか調べる」ボタンのハンドラ
async function wishCheapestStores(){
  const list=wishGet();
  if(!list.length)return;
  _ga('wish_best_split',{kinds:list.length,count:list.reduce((n,c)=>n+(c.qty||1),0)});

  const rankEl=document.getElementById('wishShopRanking');
  rankEl.classList.remove('hidden');
  rankEl.innerHTML='<div class="wb-loading">調べています…</div>';
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
    rankEl.innerHTML='<div class="wb-error">調べられませんでした。もう一度お試しください。</div>';
    return;
  }

  // 照合キーはサーバ正本（entries）で統一する。list（localStorageの生データ）で
  // キーを作ると、サーバ側の名前補正・レアリティ正規化とズレて分割プランからカードが
  // 黙って脱落するバグがあった（2026-08-19 修正）。list は表示・localStorage操作用に残す
  const srvEntries=(dbData&&dbData.entries)||[];
  if(!dbData||!dbData.shops||!dbData.shops.length||!srvEntries.length){
    rankEl.innerHTML='<div class="wb-error">店の価格を取得できませんでした。</div>';
    return;
  }

  // 送料ルールはサーバから配られる（shipping.py が正本）。リアルタイム検索で合計が
  // 後から増え、送料無料しきい値をまたぐと送料が変わるため、フロントでも同じ計算をやり直す。
  const shippingRules=dbData.shipping_rules||{};

  // 商品代金の合計から、送料・注文可否をまとめて求める
  const _wishShipping=(shop,subtotal)=>{
    const r=shippingRules[shop];
    if(!r)return{fee:null,total:subtotal,freeAt:null,shortBy:null,orderable:true};
    // 1枚も揃わない店舗（subtotal=0）は送料も最低注文金額も判定対象外
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
      total_qty:s.total_qty||0,
      missing_set,
      items_map,
    };
  }
  // db_missing は {name, rarity} オブジェクト配列。リアルタイム検索の結果が出るまで「調べ中」
  const dbMissingEntries=(dbData.db_missing||[]).map(m=>({name:m.name,rarity:m.rarity||""}));
  const pendingKeys=new Set(dbMissingEntries.map(m=>_wishEntryKey(m.name,m.rarity)));
  const notFoundKeys=new Set();   // リアルタイム検索でもどの店にも無かったエントリ
  let shopFailed=false;           // 一部の店で取得に失敗した（比較が不完全かもしれない）
  // 枚数もサーバ正本（srvEntries）から算出する。list（localStorage生データ）は
  // サーバの重複統合（同名同レアリティのqty加算）を経ていないためズレうる
  const totalQty=srvEntries.reduce((s,c)=>s+c.qty,0);

  // エントリの逆引きテーブル（サーバ正本のentriesから作る）
  const entryByKey=new Map(srvEntries.map(c=>[_wishEntryKey(c.name,c.rarity||""),{name:c.name,qty:c.qty,rarity:c.rarity||""}]));
  const _missingLabel=(key)=>{
    const e=entryByKey.get(key);
    if(!e) return key;
    return e.rarity ? `${e.name}(${e.rarity})` : e.name;
  };
  // 照合キー不一致の検出を初回描画だけに抑えるフラグ（SSEで見つからなかった正当な欠落を誤検知しないため）
  let baselineDropCheckDone=false;

  const render=()=>{
    // 再描画（SSE更新）で開いている内訳が閉じないよう、開閉状態を data-k ごとに引き継ぐ
    const openKeys=new Set([...rankEl.querySelectorAll('details[data-k]')].filter(d=>d.open).map(d=>d.dataset.k));

    // entries/priceTable のキーはサーバ正本（srvEntries）から作る（2026-08-19 修正の本体）
    const entries=srvEntries.map(c=>({key:_wishEntryKey(c.name,c.rarity||""),qty:c.qty}));
    const shopNames=Object.keys(shopState);
    const priceTable={};
    for(const e of entries){
      priceTable[e.key]={};
      for(const shop of shopNames){
        const it=shopState[shop].items_map.get(e.key);
        if(it)priceTable[e.key][shop]=it.price;
      }
    }
    if(!baselineDropCheckDone){
      baselineDropCheckDone=true;
      const dropped=entries.filter(e=>Object.keys(priceTable[e.key]).length===0&&!pendingKeys.has(e.key));
      if(dropped.length){
        console.warn('[wish-split] priceTableでどの店にも価格が見つからないエントリがあります（照合キー不一致の疑い）:',dropped.map(e=>e.key));
      }
    }

    // 一番安くそろう買い方。computeBestSplit は1店だけの組合せも含めて全組合せを比べるので、
    // 1店で買うのが最安ならその1店が返る（wish-split.js）
    const best=window.WishSplit?WishSplit.computeBestSplit(entries,priceTable,shopNames,shippingRules):null;

    const shops=shopNames.map(shop=>{
      const st=shopState[shop];
      return {shop,...st,ship:_wishShipping(shop,st.total)};
    }).filter(s=>s.covered_qty>0);   // このリストのカードが1枚も無い店は並べない
    // 並び順: そろう枚数が多い順 → 送料込みの支払いが安い順
    const byCost=(a,b)=>(b.covered_qty-a.covered_qty)||(a.ship.total-b.ship.total);
    const orderable=shops.filter(s=>s.ship.orderable).sort(byCost);
    const unorderable=shops.filter(s=>!s.ship.orderable).sort(byCost);
    const top=orderable[0];

    let recoHtml='';
    if(best){
      const parts=best.used.map(u=>{
        const keys=[...best.assign.entries()].filter(([,s])=>s===u.shop).map(([k])=>k);
        const items=keys.map(k=>shopState[u.shop].items_map.get(k)).filter(Boolean);
        return {...u,items,qty:items.reduce((s,it)=>s+it.qty,0)};
      });
      const n=parts.length;
      const head=n===1?`${esc(parts[0].shop)}で買う`:`${parts.map(p=>esc(p.shop)).join(' と ')} の${n}店で買う`;
      const blocks=parts.map((p,i)=>{
        const feeTxt=p.shipping===0?'送料無料':`送料${_wishYen(p.shipping)}`;
        const headHtml=n===1?'':`<div class="wb-buy-head">
            <span class="wb-buy-no">${i+1}</span>
            <span class="wb-buy-name">${esc(p.shop)}</span>
            <span class="wb-buy-cnt">${p.items.length}種${p.qty}枚</span>
            <span class="wb-buy-sub">商品${_wishYen(p.subtotal)}＋${feeTxt}</span>
            <span class="wb-buy-tot">${_wishYen(p.subtotal+p.shipping)}</span>
          </div>`;
        return `<div class="wb-buy-shop">${headHtml}
          <details class="wb-buy-list" data-k="buy:${escAttr(p.shop)}"><summary>この店で買うカードを見る</summary>
            ${_wishItemRowsHtml(p.shop,p.items)}
            ${_wishShopLinkHtml(p.shop)}
          </details></div>`;
      }).join('');
      // 1店でまとめたい人向けの比較（最安が1店のときは出さない）
      let compare='';
      if(n>1){
        const qtyByKey=new Map(entries.map(e=>[e.key,e.qty]));
        const{hasAll,baseline,shop}=WishSplit.selectSingleShopBaseline(
          [...best.assign.keys()],qtyByKey,priceTable,shopNames,shippingRules);
        if(baseline!==null){
          compare=`1店でまとめるなら ${esc(shop)} ${_wishYen(baseline)}（<b>+${_wishYen(baseline-best.total)}</b>）`;
        }else if(!hasAll){
          compare='1店ではそろいません';
        }
      }
      recoHtml=`<div class="wb-reco">
        <span class="wb-reco-label">一番安くそろう買い方</span>
        <div class="wb-reco-main">
          <div class="wb-reco-head">${head}</div>
          <div class="wb-reco-price"><span class="wb-num">${_wishYen(best.total)}</span><span class="wb-reco-bd">送料込み</span></div>
        </div>
        ${notFoundKeys.size?`<div class="wb-reco-miss">見つからなかったカード：${[...notFoundKeys].map(k=>esc(_missingLabel(k))).join('、')}（この金額に含みません）</div>`:''}
        <div class="wb-buy-shops">${blocks}</div>
        ${compare?`<div class="wb-reco-alt">${compare}</div>`:''}
      </div>`;
    }

    // どの店にも無いカード（見つからなかった・まだ調べ中）は全店共通なので、店ごとの欠けには数えない
    const nowhere=new Set([...notFoundKeys,...pendingKeys]);
    const reachableQty=totalQty-[...nowhere].reduce((n,k)=>n+((entryByKey.get(k)||{}).qty||0),0);
    const rowHtml=(s,rank)=>{
      const full=s.covered_qty>=reachableQty;
      const diff=(rank>1&&full&&top&&top.covered_qty>=reachableQty)?`<span class="wb-diff">+${_wishYen(s.ship.total-top.ship.total)}</span>`:'';
      const lackKinds=[...s.missing_set].filter(k=>!nowhere.has(k)).length;
      const sub=full?'':`<small>${lackKinds?`${lackKinds}種そろいません`:`${reachableQty-s.covered_qty}枚そろいません`}</small>`;
      let breakdown;
      if(s.ship.fee===null)breakdown=`商品${_wishYen(s.total)}（送料不明）`;
      else breakdown=`商品${_wishYen(s.total)}＋${s.ship.fee===0?'送料無料':'送料'+_wishYen(s.ship.fee)}`;
      let note='';
      if(!s.ship.orderable){
        note=`<span class="wb-note ng">あと${_wishYen(s.ship.shortBy)}で注文できます（最低${_wishYen(s.total+s.ship.shortBy)}）</span>`;
      }else if(full&&best&&s.ship.freeAt!==null&&s.ship.fee&&s.ship.freeAt<s.ship.fee&&s.total+s.ship.freeAt<best.total){
        // 買い足すと一番安い買い方より安くなる店にだけ、送料無料までの額を出す
        note=`<span class="wb-note">あと${_wishYen(s.ship.freeAt)}買うと送料無料で${_wishYen(s.total+s.ship.freeAt)}</span>`;
      }
      return `<details class="wb-shop${s.ship.orderable?'':' ng'}" data-k="shop:${escAttr(s.shop)}">
        <summary class="wb-row">
          <span class="wb-rank">${rank||'–'}</span>
          <span class="wb-name">${esc(s.shop)}${sub}</span>
          <span class="wb-total"><span class="wb-num">${_wishYen(s.ship.total)}</span>${diff}<small>${breakdown}</small></span>
          <span class="wb-toggle">内訳${_WISH_CHEV}</span>
          ${note}
        </summary>
        <div class="wb-detail">${_wishShopDetailHtml(s,_missingLabel)}</div>
      </details>`;
    };

    const status=pendingKeys.size
      ?`<div class="wb-status">残り${pendingKeys.size}枚の価格を調べています…</div>`:'';
    const warns=[];
    if(notFoundKeys.size&&!best)warns.push(`見つからなかったカード：${[...notFoundKeys].map(k=>esc(_missingLabel(k))).join('、')}`);
    if(shopFailed&&!pendingKeys.size)warns.push('一部の店で価格を取れませんでした');
    const warnHtml=warns.map(w=>`<div class="wb-warn">${w}</div>`).join('');

    rankEl.innerHTML=`
      <div class="wb-head">
        <span class="wb-title">どこで買うと安いか</span>
        <button class="wb-close" onclick="document.getElementById('wishShopRanking').classList.add('hidden')" aria-label="閉じる">&times;</button>
      </div>
      ${status}
      ${recoHtml||(pendingKeys.size?'':'<div class="wb-error">どの店にも見つかりませんでした。</div>')}
      ${warnHtml}
      ${orderable.length?`<div class="wb-sec">店ごとの支払い（送料込み）</div><div class="wb-shops">${orderable.map((s,i)=>rowHtml(s,i+1)).join('')}</div>`:''}
      ${unorderable.length?`<div class="wb-sec">このままでは注文できない店</div><div class="wb-shops">${unorderable.map(s=>rowHtml(s,0)).join('')}</div>`:''}
      <div class="wb-foot">価格・送料は変わることがあります。購入前に各店で確かめてください。</div>
    `;
    rankEl.querySelectorAll('details[data-k]').forEach(d=>{if(openKeys.has(d.dataset.k))d.open=true;});
  };

  render();

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
            st.missing_set.delete(key);
          }
        }
        if(!anyShop)notFoundKeys.add(key);
        if((d.failed_shops||[]).length)shopFailed=true;
        pendingKeys.delete(key);
        render();
      }
    }
  }catch(e){
    shopFailed=true;
  }
  // 途中で切れた場合も「調べています」を残さない
  for(const k of pendingKeys)notFoundKeys.add(k);
  pendingKeys.clear();
  render();
}

// 明細の行（名前とレアリティを1つのセルに入れ、金額の列が次の行へ落ちないようにする）
function _wishItemRowsHtml(shop, items){
  return items.map(it=>{
    const url=safeUrl(it.url||_wishShopSearchUrl(shop,it.name));
    const rarity=it.rarity?`<span class="wb-rar">${esc(it.rarity)}</span>`:'';
    const unit=it.qty>1?`<small>${_wishYen(it.price)} × ${it.qty}</small>`:'';
    return `<div class="wb-item">
      <span class="wb-item-qty">${it.qty}枚</span>
      <span class="wb-item-name"><a href="${escAttr(url)}" target="_blank" rel="noopener">${esc(it.name)}</a>${rarity}</span>
      <span class="wb-item-price">${_wishYen(it.price*it.qty)}${unit}</span>
    </div>`;
  }).join('');
}

function _wishShopLinkHtml(shop){
  const u=_wishShopSearchUrl(shop,'');
  return u?`<div class="wb-link-row"><a class="wb-shop-link" href="${escAttr(safeUrl(u))}" target="_blank" rel="noopener">${esc(shop)}で探す ↗</a></div>`:'';
}

// 店舗の内訳（買うカード・送料・支払い・この店に無いカード）
function _wishShopDetailHtml(shopRow, missingLabel){
  let html=_wishItemRowsHtml(shopRow.shop,[...shopRow.items_map.values()]);
  const sh=shopRow.ship;
  if(sh&&shopRow.total>0){
    const feeText=sh.fee===null?'不明':(sh.fee===0?'無料':_wishYen(sh.fee));
    html+=`<div class="wb-sum"><span>商品の合計</span><span>${_wishYen(shopRow.total)}</span></div>
      <div class="wb-sum"><span>送料</span><span>${feeText}</span></div>
      <div class="wb-sum total"><span>支払い（送料込み）</span><span>${_wishYen(sh.total)}</span></div>`;
  }
  if(shopRow.missing_set.size>0){
    html+=`<div class="wb-missing">この店に無いカード：${[...shopRow.missing_set].map(k=>esc(missingLabel?missingLabel(k):k)).join('、')}</div>`;
  }
  return html+_wishShopLinkHtml(shopRow.shop);
}

// 追加ボタンのHTMLを生成するヘルパー
function wishBtnHtml(name,qty){
  return `<button class="wish-add" onclick="event.stopPropagation();wishAddFromBtn(this,'${escAttr(escJs(name))}',${qty||1})" title="購入候補に追加">+ 候補</button>`;
}
function wishAddFromBtn(btn,name,qty,rarity){
  wishAdd(name,qty,rarity);
  _ga('wish_add',{source:'card',count:qty||1});
  const orig=btn.textContent;
  btn.classList.add('added');
  btn.textContent='\u2713 \u8ffd\u52a0';
  setTimeout(()=>{btn.classList.remove('added');if(btn.id==='heroWishAdd')_updateRarityBanner();else btn.textContent=orig;},1200);
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

// 所持確認からの追加は不足数を計算済み。一括保存後に価格キャッシュだけ更新する。
function wishOwnershipAdded(rows){
  _ga('wish_add',{source:'deck_shortage',count:rows.reduce((n,r)=>n+(r.add||0),0)});
  _wishPrices=null;
  _clearWishPricesCache();
  trackDeckCards(rows);
}
