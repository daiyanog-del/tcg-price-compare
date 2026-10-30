// 元は templates/index.html のインライン <script>（2026-09-22 バッチCで外部化）。
// Jinja 依存の値は index.html 側の小さなインライン script が定義する。
// ── 購入候補（localStorage）＋ 値下がりアラート ──
const WISH_KEY='cardprice_wishlist';

// 直近の /api/wish-prices 結果キャッシュ（name -> {latest, base_7d, diff, pct, status}）
// null = まだロードしていない。{} 以上 = ロード試行済み（成功・失敗を問わず）
let _wishPrices = null;
let _wishPricesTime = 0;
// 最後に取りに行ったカード名（2026-10-21 reviewer 指摘）。今の一覧にここに無い名前があれば古いとみなして取り直す
// （すべて削除の取り消し・同期で届いたカードなど、wishAdd を通らずに増えた行の価格が10分出なかったため）
let _wishPricesNames = new Set();
let _wishPricesFailed = false; // 最後の取得が通信・応答の失敗で終わったか（「読み込めませんでした」はこのときと、サーバーが status "error" を返したカードだけ）
const _WISH_PRICES_TTL = 10 * 60 * 1000; // 10分でキャッシュ失効
// 読み込みに失敗したカード（通信の失敗・status "error"）があったときは、この時間で取り直す
// （2026-10-22: 失敗の結果も10分とっておき、その間ずっと「価格が見つかりませんでした」と出ていた）。
// 続けて失敗するたびに倍に延ばし、10分で止める（DB の時間切れは毎回起きうるので、30秒ごとに重い読み込みを繰り返さない。reviewer 高1）
const _WISH_PRICES_RETRY = 30 * 1000; // TODO: calibrate from data
let _wishPricesTtl = _WISH_PRICES_TTL; // 最後の取得の結果に応じた有効期間
let _wishRetryTimer = null;
let _wishRetryCount = 0; // 続けて失敗した回数（失敗の無い取得で 0 に戻す）
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
      _wishPricesNames=new Set(saved.names||Object.keys(saved.data)); // 古い形（names なし）は届いた名前で代える
    }
  }catch{}
})();

function _saveWishPricesCache(){
  try{
    // 読み込みに失敗したカードは端末にとっておかない（次に開いたときに取り直す。names からも外す）
    const prices=_wishPrices||{}, isErr=v=>!!(v&&v.status==='error');
    const data={};
    for(const [k,v] of Object.entries(prices)) if(!isErr(v)) data[k]=v;
    // 失敗を含む結果では、値の届かなかった名前も外す（送った名前と直した名前が引き当てられないときに、
    // 失敗が「情報なし」として残らないように。reviewer 低8）
    const anyErr=Object.values(prices).some(isErr);
    const names=[..._wishPricesNames].filter(n=>anyErr?(data[n]!==undefined):!isErr(prices[n]));
    localStorage.setItem(_WISH_PRICES_CACHE_KEY,JSON.stringify({data,time:_wishPricesTime,names}));
  }catch{}
}
function _clearWishPricesCache(){
  try{ localStorage.removeItem(_WISH_PRICES_CACHE_KEY); }catch{}
}

// /api/wish-prices を呼んで _wishPrices を更新する
// 取得中の Promise。同時に2回取りに行かないためと、取得の途中で描いた一覧を届いたときに描き直すため
// （2026-10-21: 起動時の取得の途中に購入候補を開くと、届いても一覧が「相場データ収集中」のまま止まっていた）
let _wishPricesLoading=null;
function loadWishPrices(){
  if(_wishPricesLoading) return _wishPricesLoading;
  const p=_loadWishPricesNow();
  _wishPricesLoading=p;
  // 先に登録したこの後始末が、呼び出し側の then より先に走る（届いた後の描き直しでは「取得中でない」と見える）
  p.then(()=>{ if(_wishPricesLoading===p) _wishPricesLoading=null; });
  return p;
}
async function _loadWishPricesNow(){
  const list=wishGet();
  // ロード試行済みとしてマーク（null → {} で「試行済み」になる）
  const got={};
  const names=[...new Set(list.map(c=>c.name))];
  _wishPrices=got;
  _wishPricesTime=Date.now();
  _wishPricesNames=new Set(names);
  _wishPricesFailed=false;
  _wishPricesTtl=_WISH_PRICES_TTL;
  if(!list.length){ _saveWishPricesCache(); return; }
  try{
    const res=await fetch('/api/wish-prices',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cards:names})});
    if(!res.ok)throw new Error('HTTP '+res.status);
    const data=await res.json();
    const cards=data.cards||[];
    cards.forEach(c=>{got[c.name]=c;});
    // サーバーは表記ゆれを直した名前で返す。送った順に返るので、数がそろっていれば送った名前でも引けるようにする
    if(cards.length===names.length) cards.forEach((c,i)=>{ if(!got[names[i]]) got[names[i]]=c; });
    // 取得中にカードが足されて価格が捨てられた（null に戻った）ときは、古い一覧の結果を残さない
    if(_wishPrices===got){
      _saveWishPricesCache();
      if(cards.some(c=>c.status==='error')) _wishScheduleRetry();
      else _wishRetryCount=0;
    }
  }catch(e){
    if(_wishPrices===got){ _wishPricesFailed=true; _wishScheduleRetry(); }
  }
}
// 読み込めなかったカードがあれば、少し待って取り直す（一覧が見えていれば描き直しで、見えていなければ次に開いたときに）
function _wishScheduleRetry(){
  const wait=Math.min(_WISH_PRICES_RETRY*2**_wishRetryCount,_WISH_PRICES_TTL);
  _wishRetryCount++;
  _wishPricesTtl=wait;
  if(_wishRetryTimer) clearTimeout(_wishRetryTimer);
  _wishRetryTimer=setTimeout(()=>{
    _wishRetryTimer=null;
    const el=typeof document!=='undefined'?document.getElementById('wishList'):null;
    if(el&&el.offsetParent!==null) renderWishlist();
  },wait+100);
}
// 一覧を描くときに呼ぶ: 価格がまだ・古い・取得中なら（取りに行って）届いたら一覧を描き直す。描き直しの予約は1回だけ
let _wishRerenderPending=false;
function _wishEnsurePrices(){
  const stale=_wishPrices===null || Date.now()-_wishPricesTime > _wishPricesTtl
    || wishGet().some(c=>!_wishPricesNames.has(c.name)); // まだ取りに行っていない名前がある
  if(!_wishPricesLoading && !stale) return;
  if(_wishRerenderPending){ if(!_wishPricesLoading) loadWishPrices(); return; }
  _wishRerenderPending=true;
  loadWishPrices().then(()=>{ _wishRerenderPending=false; wishUpdateBadge(); renderWishlist(); });
}

// まだ店に並ばないカードの言い方（サーバーの印 {release_date, tcg_preview}。/api/wish-prices・/api/wish-shop-totals 共通）
// 発売日の当日0時（JST）からはサーバーが印を付けなくなり、普通のカードとして価格を探す（2026-10-30）
// いつ買えるか（「11月1日発売」「発売日未定」「日本では未発売」）
function _wishUnreleasedWhen(m){
  if(m.tcg_preview) return '日本では未発売';
  const d=/^(\d{4})-(\d{2})-(\d{2})/.exec(m.release_date||'');
  return d?`${+d[2]}月${+d[3]}日発売`:'発売日未定';
}
// 一覧の行の言い方（「未発売」「11月1日発売」／「日本では未発売」）。欄が狭いスマホでは語の途中で
// 折り返さないよう、まとまりごとに分けて返す（折り返すのはまとまりの間だけ）
function _wishUnreleasedParts(m){
  return m.tcg_preview?[_wishUnreleasedWhen(m)]:['未発売',_wishUnreleasedWhen(m)];
}

// 価格情報の表示 HTML を生成する
// 2026-10-21 モック wish-refresh-20261021 の D:
//   読み込み中は読み込み中の印（以前は「相場データ収集中」と同じ文で、まだ集めていないカードと区別できなかった）
//   動きが無いときの「(+0%)」は出さない。値下がり（5%以上かつ50円以上・判定は今までと同じ）だけ緑の ↓ 札
function _wishPriceHtml(info){
  if(!info){
    if(_wishPricesLoading) return '<span class="wl-skel" role="status" aria-label="価格を読み込み中"></span>';
    // 失敗と言い切るのは通信・応答の失敗のときだけ。応答にそのカードが無かったときは控えめに
    return _wishPricesFailed
      ? '<span class="wish-price-info wl-noprice">価格を読み込めませんでした</span>'
      : '<span class="wish-price-info wl-noprice">価格が見つかりませんでした</span>';
  }
  if(info.status==='error') return '<span class="wish-price-info wl-noprice">価格を読み込めませんでした</span>';
  if(info.status==='pending') return '<span class="wish-price-info wl-noprice">相場をまだ集めていません</span>';
  if(info.status==='unreleased') return `<span class="wish-price-info wl-noprice">${_wishUnreleasedParts(info).map(t=>`<span class="wl-nowrap">${esc(t)}</span>`).join(' ')}</span>`;
  if(info.latest==null) return '<span class="wish-price-info wl-noprice">価格が見つかりませんでした</span>';
  const priceStr='¥'+info.latest.toLocaleString();
  const isDrop=info.pct!=null&&info.diff!=null&&info.pct<=-5&&info.diff<=-50;
  if(isDrop){
    return `<span class="wish-price-info">${priceStr}</span><span class="wl-drop" title="7日前から ${info.pct}%">↓${Math.abs(info.diff).toLocaleString()}円</span>`;
  }
  return `<span class="wish-price-info">${priceStr}</span>`;
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
  // そのデッキ用として入れた購入候補は「デッキ指定なし」に戻す（候補自体は消さない）
  if(window.WishDeck){
    const list=wishGet();
    if(list.some(c=>WishDeck.forDeck(c,id))){
      wishSave(WishDeck.dropDeck(list,id));
      renderWishlist();
    }
  }
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
    // decks（どのデッキ用か・shared/wish-deck.js）は残す。内訳の無い行はデッキ指定なし
    return list.map(c=>window.WishDeck?WishDeck.normalizeItem(c):({name:c.name,qty:c.qty,rarity:c.rarity||""}));
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
// 行を外して、6秒間「取り消す」を出す（2026-10-21 モック wish-refresh-20261021 の B。マイデッキの削除と同じお知らせ）。
// 取り消すと元の位置に戻す。そのあいだに同じ行を足し直していたら戻さない
function wishRemoveWithUndo(name,rarity){
  rarity=rarity||"";
  const before=wishGet();
  _wishRemoveAt(before,before.findIndex(c=>_wishMatchKey(c,name,rarity)));
}
// before[idx] の行を外して取り消しを出す。戻すのは外す前の行そのもの（どのデッキ用かの内訳も含む）
function _wishRemoveAt(before,idx){
  if(idx<0){renderWishlist();return;}
  const item=before[idx], name=item.name, rarity=item.rarity||"";
  wishSave(before.filter((_,i)=>i!==idx));
  const wsr=document.getElementById('wishShopRanking');if(wsr){wsr.classList.add('hidden');wsr.innerHTML='';}
  renderWishlist();
  const toast=window.DeckHead&&window.DeckHead.undoToast;
  if(!toast){console.warn('[wish] DeckHead.undoToast が読めないため、取り消しを出せませんでした');return;}
  toast(`「${name}」を購入候補から外しました`,()=>{
    const cur=wishGet();
    if(cur.some(c=>_wishMatchKey(c,name,rarity))){window.DeckHead.toast('すでに購入候補に入っています');return;}
    cur.splice(Math.min(idx,cur.length),0,item);
    // 外している間に価格を取り直していたら、そのカードの価格が無い。wishAdd と同じく取り直させる
    if(!(_wishPrices&&_wishPrices[name]))_wishPrices=null;
    wishSave(cur);
    renderWishlist();
  });
}
// すべて削除: すぐ消して6秒間「取り消す」（2026-10-21 モックの C。以前はブラウザの確認窓）。
// 取り消すと元の並びに戻す（そのあいだに足したカードは後ろに残す）。価格の取っておきは期限が来たら捨てる
function wishClearAll(){
  const before=wishGet();
  if(!before.length)return;
  const total=before.reduce((n,c)=>n+(c.qty||0),0);
  wishSave([]);
  const wsr=document.getElementById('wishShopRanking');if(wsr){wsr.classList.add('hidden');wsr.innerHTML='';}
  renderWishlist();
  const toast=window.DeckHead&&window.DeckHead.undoToast;
  if(!toast){console.warn('[wish] DeckHead.undoToast が読めないため、取り消しを出せませんでした');_wishPrices=null;_clearWishPricesCache();return;}
  toast(`購入候補（${before.length}種 ${total}枚）をすべて削除しました`,()=>{
    const cur=wishGet();
    const merged=before.map(b=>{
      const again=cur.find(c=>_wishMatchKey(c,b.name,b.rarity));
      return again?(window.WishDeck?WishDeck.mergeInto(b,again):{...b,qty:Math.min(b.qty+again.qty,99)}):b;
    });
    wishSave(merged.concat(cur.filter(c=>!before.some(b=>_wishMatchKey(b,c.name,c.rarity)))));
    renderWishlist();
  },()=>{
    if(wishGet().length)return; // 期限までに足し直していたら、その価格は残す
    _wishPrices=null;_clearWishPricesCache();
  });
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
  if(newQty<1)return wishRemoveWithUndo(name,rarity);
  if(newQty>99){if(window.DeckHead)window.DeckHead.toast('購入候補は1行99枚までです');return;}
  const list=wishGet();
  const item=list.find(c=>_wishMatchKey(c,name,rarity));
  if(item){
    // 減らしたときに内訳（デッキ用の枚数）の合計が枚数を超えないよう削る
    const next=window.WishDeck?WishDeck.setQty(item,newQty):{...item,qty:newQty};
    list[list.indexOf(item)]=next;wishSave(list);renderWishlist();
  }
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
    // 内訳（どのデッキ用か）も足し合わせる
    list[list.indexOf(collide)]=window.WishDeck?WishDeck.mergeInto(collide,target):{...collide,qty:Math.min(collide.qty+target.qty,99)};
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

let _wishRaritiesLoading=null; // { sig, p }（取得中の同じ名前の集合は1回にまとめる）
function _loadWishRarities(names){
  // 既にキャッシュ済みのカードと一致するなら再取得しない
  const sig=[...names].sort().join('|');
  if(sig===_wishRaritiesNames) return Promise.resolve(_wishRarities);
  if(_wishRaritiesLoading&&_wishRaritiesLoading.sig===sig) return _wishRaritiesLoading.p;
  const p=fetch('/api/card-rarities',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cards:names})
  }).then(r=>r.json()).then(d=>{
    _wishRarities=d.rarities||{};
    _wishRaritiesNames=sig;
    return _wishRarities;
  }).catch(()=>_wishRarities).finally(()=>{ if(_wishRaritiesLoading&&_wishRaritiesLoading.p===p)_wishRaritiesLoading=null; });
  _wishRaritiesLoading={sig,p};
  return p;
}

// 1行ぶんのレアリティの札。押すと小窓で選ぶ（_wishOpenRarityMenu。2026-10-21 モックの E）
const _WISH_CHEV_SM='<svg width="10" height="10" viewBox="0 0 12 12" aria-hidden="true"><path d="M2 4l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
function _wishRarityChipHtml(c){
  const label=c.rarity||'最安のレア';
  return `<button type="button" class="wl-rar" data-name="${escAttr(c.name)}" data-rarity="${escAttr(c.rarity||"")}" aria-haspopup="menu" title="レアリティを選ぶ（指定しなければ最安のレア）"><span>${esc(label)}</span>${_WISH_CHEV_SM}</button>`;
}

function renderWishlist(){
  // 一覧の中のメニュー（行の ⋯・右クリック・レアリティ）は描き直しで行が作り直されるので閉じる（価格・同期の描き直しでも）。
  // 見出しの ⋯ は作り直されないので開いたまま
  if(_wishMenu&&!(_wishMenu.fromBtn&&_wishMenu.fromBtn.id==='wishHeadMore'))_wishCloseMenu();
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
  // 見出しの数（絞り込みに関係なく全体）
  const cnt=document.getElementById('wishCount');
  if(cnt)cnt.innerHTML=`<b>${list.length}</b>種 <b>${list.reduce((n,c)=>n+(c.qty||0),0)}</b>枚`;
  // 未ロード・TTL切れ・取得中（起動時の取得など）なら、届いたあとに描き直す
  _wishEnsurePrices();
  // レアリティ候補を先に読んでおく（札を押したときにすぐ出せるように。カード名の集合が変わったときだけ取りに行く）
  _loadWishRarities([...new Set(list.map(c=>c.name))]);
  const shown=_renderWishDeckFilter(_wishView(list));
  el.innerHTML=shown.map(c=>{
    const info=_wishPrices[c.name];
    const isDrop=info&&info.pct!=null&&info.pct<=-5&&info.diff!=null&&info.diff<=-50;
    const isPending=!info||info.status==='pending';
    // 2026-10-21 列のそろった詰めた行に（モック wish-refresh-20261021 の A＋B）。見た目は css/wish-list.css
    const rowClass='wl-row'+(isDrop?' wish-drop':'')+(isPending?' wish-pending':'');
    const wishImgSafe=info&&info.image?safeUrl(info.image):'';
    const imgHtml=`<div class="wl-img">${wishImgSafe?`<img src="${escAttr(wishImgSafe)}" alt="" loading="lazy">`:''}</div>`;
    const nj=escAttr(escJs(c.name)), rj=escAttr(escJs(c.rarity||""));
    // デッキで絞り込んでいるときは、そのデッキ用の枚数を出し、−/＋ もその分だけ動かす
    const shownQty=_wishDeckFilter===null?c.qty:(_wishDeckFilter===''?WishDeck.unassigned(c):WishDeck.forDeck(c,_wishDeckFilter));
    const tags=_wishDeckTagsHtml(c);
    return `
    <div class="${rowClass}" data-name="${escAttr(c.name)}" data-rarity="${escAttr(c.rarity||"")}">
      ${imgHtml}
      <div class="wl-main">
        <a class="wl-name" href="/card/${encodeURIComponent(c.name)}" target="_blank" rel="noopener" title="価格を比べる">${esc(c.name)}</a>
        ${tags?`<span class="wl-tags">${tags}</span>`:''}
      </div>
      ${_wishRarityChipHtml(c)}
      <div class="wl-price">${_wishPriceHtml(info)}</div>
      <span class="wl-step">
        <button type="button" onclick="wishQtyStep('${nj}','${rj}',-1)" aria-label="1枚減らす" title="1枚減らす">−</button>
        <b>${shownQty}</b>
        <button type="button" onclick="wishQtyStep('${nj}','${rj}',1)" aria-label="1枚増やす" title="1枚増やす">＋</button>
      </span>
      <button type="button" class="wl-more" aria-label="その他の操作" aria-haspopup="menu">${_WISH_MORE_SVG}</button>
    </div>`}).join('');
  _wishInitRowMenu(el);
}

// ── 小さなメニュー（行の ⋯・見出しの ⋯・レアリティの札）。見た目はマイデッキのカードの右クリック（deck.css の .deck-card-menu） ──
const _WISH_MORE_SVG='<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><circle cx="3.5" cy="8" r="1.4"/><circle cx="8" cy="8" r="1.4"/><circle cx="12.5" cy="8" r="1.4"/></svg>';
const _WISH_CHECK_SVG='<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M3 8.5l3.2 3L13 4.5"/></svg>';
let _wishMenu=null; // { el, fromBtn, ownerEl, ownerClass, cleanup }
function _wishCloseMenu(){
  if(!_wishMenu)return;
  const m=_wishMenu; _wishMenu=null;
  m.cleanup();
  m.el.remove();
  if(m.ownerEl)m.ownerEl.classList.remove(m.ownerClass);
}
/**
 * メニューを開く。spec:
 *   title       見出し（カード名など）
 *   items       [{label, sub?, danger?, checked?, fn}] と区切り 'sep'
 *   fromBtn     押したボタン（その下に右端をそろえて出す。null なら x, y＝右クリックの位置）
 *   ownerEl / ownerClass  開いている間に印を付ける要素（行の金の枠・札の線）
 *   align       'left' ならボタンの左端にそろえる（レアリティの札）
 *   cls         足すクラス（レアリティは長いので .wl-menu-scroll）
 *   afterPick(top)  項目を選んだあと（フォーカスを戻すため。top は選ぶ前のボタンの位置）
 */
function _wishOpenMenu(spec){
  _wishCloseMenu();
  if(!spec.fromRarity)_wishCancelRarityWait();
  const fromBtn=spec.fromBtn||null;
  const el=document.createElement('div');
  el.className='deck-head-pop deck-more-menu deck-card-menu wl-menu'+(spec.cls?' '+spec.cls:'');
  el.setAttribute('role','menu');
  if(spec.title){
    const head=document.createElement('div');
    head.className='deck-card-menu-head';
    head.textContent=spec.title;
    el.appendChild(head);
  }
  const items=[];
  spec.items.forEach(it=>{
    if(it==='sep'){const s=document.createElement('div');s.className='deck-more-sep';el.appendChild(s);return;}
    const b=document.createElement('button');
    b.type='button'; b.className='deck-more-item'+(it.danger?' is-danger':''); b.setAttribute('role',it.checked===undefined?'menuitem':'menuitemradio');
    if(it.checked!==undefined)b.setAttribute('aria-checked',String(!!it.checked));
    const l=document.createElement('span'); l.className='wl-menu-label';
    if(it.checked!==undefined){const ck=document.createElement('span');ck.className='wl-ck';ck.innerHTML=it.checked?_WISH_CHECK_SVG:'';l.appendChild(ck);}
    l.appendChild(document.createTextNode(it.label)); // 名前・レアリティは textContent で入れる
    b.appendChild(l);
    if(it.sub){const s=document.createElement('span');s.className='deck-more-sub';s.textContent=it.sub;b.appendChild(s);}
    b.addEventListener('click',()=>{
      const top=fromBtn?fromBtn.getBoundingClientRect().top:0; // 外すと行が消えるので、位置は先に測る
      _wishCloseMenu(); it.fn();
      if(spec.afterPick)spec.afterPick(top);
    });
    el.appendChild(b); items.push(b);
  });
  document.body.appendChild(el);
  // ボタンの下（右端をそろえる。align:'left' なら左端）、右クリックならマウスの位置。はみ出す向きなら反対側へ
  const m=8, vw=document.documentElement.clientWidth, vh=window.innerHeight, w=el.offsetWidth, h=el.offsetHeight;
  let left=spec.x||0, top=spec.y||0;
  if(fromBtn){
    const r=fromBtn.getBoundingClientRect();
    left=spec.align==='left'?r.left:r.right-w; top=r.bottom+6;
    if(top+h+m>vh&&r.top-6-h>=m)top=r.top-6-h;
  }else{ if(left+w+m>vw)left=left-w; if(top+h+m>vh)top=top-h; }
  el.style.left=Math.max(m,Math.min(left,vw-w-m))+'px';
  el.style.top=Math.max(m,Math.min(top,vh-h-m))+'px';
  if(spec.ownerEl)spec.ownerEl.classList.add(spec.ownerClass);
  const onDown=e=>{if(!el.contains(e.target)&&!(fromBtn&&fromBtn.contains(e.target)))_wishCloseMenu();};
  const onKey=e=>{
    if(e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();_wishCloseMenu();if(fromBtn&&document.body.contains(fromBtn))fromBtn.focus({preventScroll:true});return;}
    const i=items.indexOf(document.activeElement);
    if(e.key==='ArrowDown'){e.preventDefault();(items[i+1]||items[0]).focus();}
    else if(e.key==='ArrowUp'){e.preventDefault();(items[i-1]||items[items.length-1]).focus();}
  };
  const onScroll=e=>{if(!el.contains(e.target))_wishCloseMenu();};
  document.addEventListener('pointerdown',onDown,true);
  document.addEventListener('keydown',onKey,true);
  window.addEventListener('scroll',onScroll,true);
  window.addEventListener('resize',_wishCloseMenu);
  _wishMenu={el,fromBtn,ownerEl:spec.ownerEl||null,ownerClass:spec.ownerClass,cleanup:()=>{
    document.removeEventListener('pointerdown',onDown,true);
    document.removeEventListener('keydown',onKey,true);
    window.removeEventListener('scroll',onScroll,true);
    window.removeEventListener('resize',_wishCloseMenu);
  }};
  if(fromBtn){
    // 選んでいるもの（レアリティ）があればそこへ、なければ先頭へ
    const cur=items.find(b=>b.getAttribute('aria-checked')==='true')||items[0];
    if(cur){cur.focus({preventScroll:true});cur.scrollIntoView({block:'nearest'});}
  }
}
// 行の ⋯（PC は行の右クリックでも）
function _wishOpenRowMenu(row,x,y,fromBtn){
  const name=row.dataset.name, rarity=row.dataset.rarity||'';
  _wishOpenMenu({title:name,fromBtn,x,y,ownerEl:row,ownerClass:'is-menu',
    items:[
      {label:'カードの価格ページを開く',fn:()=>window.open('/card/'+encodeURIComponent(name),'_blank','noopener')},
      'sep',
      {label:'購入候補から外す',danger:true,fn:()=>wishRemoveWithUndo(name,rarity)},
    ],
    afterPick:top=>_wishRefocus(name,fromBtn,top)});
}
// レアリティの札（2026-10-21 モック wish-refresh-20261021 の E。ブラウザのプルダウンをやめた）
let _wishRarityWait=null; // 候補を読み込む間、開くのを待っている札 { name, rarity, cancel }
function _wishCancelRarityWait(){ if(_wishRarityWait){ const w=_wishRarityWait; _wishRarityWait=null; w.cancel(); } }
function _wishFindRarityChip(name,rarity){
  return [...document.querySelectorAll('#wishList .wl-rar')].find(b=>b.dataset.name===name&&(b.dataset.rarity||'')===rarity)
    ||[...document.querySelectorAll('#wishList .wl-rar')].find(b=>b.dataset.name===name)||null;
}
function _wishOpenRarityMenu(chip0){
  const name=chip0.dataset.name, cur=chip0.dataset.rarity||'';
  // 同じ札を待っている間にもう一度押したら取りやめる
  if(_wishRarityWait&&_wishRarityWait.name===name&&_wishRarityWait.rarity===cur){_wishCancelRarityWait();return;}
  _wishCancelRarityWait();
  const open=()=>{
    // 候補を読み込む間に描き直されたら、作り直された同じ札から開く
    const chip=document.body.contains(chip0)?chip0:_wishFindRarityChip(name,cur);
    if(!chip)return;
    let picked=cur;
    const cands=(_wishRarities[name]||[]).slice();
    if(cur&&!cands.includes(cur))cands.unshift(cur); // 選んでいるのに候補に無いものも落とさない
    _wishOpenMenu({fromRarity:true,title:'レアリティ',fromBtn:chip,align:'left',ownerEl:chip,ownerClass:'is-open',cls:'wl-menu-scroll',
      items:[{label:'最安のレア（指定しない）',sub:'いちばん安いものを探す',checked:!cur,fn:()=>{picked='';wishSetRarity(name,cur,'');}}]
        .concat(cands.length?['sep']:[])
        .concat(cands.map(r=>({label:r,checked:r===cur,fn:()=>{picked=r;wishSetRarity(name,cur,r);}}))),
      // 選び直した行（新しいレアリティ）の札へ戻す。同じカードが別のレアリティで並んでいても取り違えない
      afterPick:()=>{const c=_wishFindRarityChip(name,picked);if(c)c.focus({preventScroll:true});}});
  };
  if(_wishRarities[name]){open();return;}
  let canceled=false;
  const onDown=e=>{ if(!e.target.closest('#wishList .wl-rar'))_wishCancelRarityWait(); };
  const onKey=e=>{ if(e.key==='Escape')_wishCancelRarityWait(); };
  document.addEventListener('pointerdown',onDown,true);
  document.addEventListener('keydown',onKey,true);
  const w={name,rarity:cur,cancel:()=>{canceled=true;document.removeEventListener('pointerdown',onDown,true);document.removeEventListener('keydown',onKey,true);}};
  _wishRarityWait=w;
  _loadWishRarities([...new Set(wishGet().map(c=>c.name))]).then(()=>{
    if(canceled||_wishRarityWait!==w)return;
    _wishRarityWait=null; w.cancel(); canceled=false;
    open();
  });
}
// 見出しの ⋯（2026-10-21 モックの C。主操作だけ見出しに残し、ほかはここへ）
function _wishOpenHeadMenu(btn){
  _wishOpenMenu({fromBtn:btn,
    items:[
      {label:'他の端末でも見る',fn:()=>{if(typeof openSyncShareDialog==='function')openSyncShareDialog();}},
      'sep',
      {label:'すべて削除',danger:true,fn:()=>wishClearAll()},
    ]});
}
// メニューの項目を選んだあと、キーボードで続けて操作できるようにフォーカスを戻す
// （開く: 元の ⋯ へ。外す: 同じ位置に来た次の行の ⋯ へ。行が無くなったら何もしない）
function _wishRefocus(name,fromBtn,top){
  if(!fromBtn)return;
  if(document.body.contains(fromBtn)){fromBtn.focus({preventScroll:true});return;}
  const rows=[...document.querySelectorAll('#wishList .wl-row')];
  const next=rows.find(r=>r.dataset.name!==name&&r.getBoundingClientRect().bottom>top)||rows[rows.length-1];
  const b=next&&next.querySelector('.wl-more');
  if(b)b.focus({preventScroll:true});
}
// 一覧の箱と見出しに1回だけ付ける（行は描き直しで作り直すので、箱で受ける）
function _wishInitRowMenu(box){
  const head=document.getElementById('wishHeadMore');
  if(head&&!head.dataset.menuReady){
    head.dataset.menuReady='1';
    head.addEventListener('click',()=>{ if(_wishMenu&&_wishMenu.fromBtn===head){_wishCloseMenu();return;} _wishOpenHeadMenu(head); });
  }
  if(box.dataset.menuReady)return;
  box.dataset.menuReady='1';
  box.addEventListener('click',e=>{
    const btn=e.target.closest('.wl-more,.wl-rar');
    if(!btn)return;
    if(_wishMenu&&_wishMenu.fromBtn===btn){_wishCloseMenu();return;}
    if(btn.classList.contains('wl-rar'))_wishOpenRarityMenu(btn);
    else _wishOpenRowMenu(btn.closest('.wl-row'),0,0,btn);
  });
  box.addEventListener('contextmenu',e=>{
    const row=e.target.closest('.wl-row');
    // リンク・入力欄の上ではブラウザのメニューを残す（新しいタブで開く・貼り付けなど）
    if(!row||e.target.closest('a,select,input'))return;
    e.preventDefault();
    _wishOpenRowMenu(row,e.clientX,e.clientY,null);
  });
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
    'カーナベル':`https://www.ka-nabell.com/ec/sell/yugioh/search?keyword=${q}`,
    'カードラボ':`https://www.c-labo-online.jp/product-list?keyword=${q}`,
    'まんぞく屋':`https://shopmanzokuya.com/products/list?category_id=1&name=${q}&orderby=price_l&disp_number=100`,
  };
  return urls[shop]||'';
}

// (name, rarity) から一意のマップキーを作るヘルパ
function _wishEntryKey(name,rarity){return (name||'')+''+(rarity||'');}

const _wishYen=n=>'¥'+n.toLocaleString();
const _WISH_CHEV='<svg class="wb-chev" width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M2 4l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';

// ── どのデッキ用か（shared/wish-deck.js の decks）の絞り込みと札（2026-10-20） ──
// null = すべて／'' = デッキ指定なし／それ以外 = そのデッキID。ページを開き直したら「すべて」に戻す
let _wishDeckFilter=null;
// 表示・計算に使う一覧。今は無いデッキ（別の端末で削除した等）用の分はデッキ指定なしとして数える
function _wishView(list){
  return window.WishDeck?WishDeck.forgetUnknown(list,savedDecksGet().map(d=>d.id)):list;
}
// 行の −/＋。絞り込んでいなければ行全体の枚数、絞り込み中はそのデッキ用の分だけを動かす
function wishQtyStep(name,rarity,delta){
  if(_wishDeckFilter===null){
    const item=wishGet().find(c=>_wishMatchKey(c,name,rarity||''));
    if(item)wishUpdateQty(name,item.qty+delta,rarity);
    return;
  }
  wishDeckStep(name,rarity,delta);
}
// 絞り込み中の −/＋: そのデッキ用（デッキ指定なしで絞っているならデッキ指定なし）の分だけを増減する
function wishDeckStep(name,rarity,delta){
  // 表示と同じく、今は無いデッキ用の分はデッキ指定なしとして扱う（外すのは押したカードの行だけ。
  // 他の行には、別の端末で作ってまだ届いていないデッキ用の内訳があるかもしれないので触らない）
  const list=window.WishDeck.forgetUnknown(wishGet(),savedDecksGet().map(d=>d.id),name), id=_wishDeckFilter||null;
  let next;
  try{
    if(delta>0){
      // レアリティ指定のある行で＋したときも、その行に足す（指定なしの行を新しく作らない）
      const item=list.find(c=>_wishMatchKey(c,name,rarity));
      if(!item)return;
      if(item.qty>=99){if(window.DeckHead)window.DeckHead.toast('購入候補は1行99枚までです');return;}
      item.qty+=1;
      if(id){item.decks=item.decks||{};item.decks[id]=(item.decks[id]||0)+1;}
      next=list;
    }else{
      // 押した行から減らす。そのデッキ用の分が無ければ何もしない
      const idx=list.findIndex(c=>_wishMatchKey(c,name,rarity));
      if(idx<0)return;
      const r=WishDeck.remove([list[idx]],name,1,id,{onlyDeck:true});
      if(!r.removed)return;
      next=list.slice(0,idx).concat(r.list,list.slice(idx+1));
      // 行全体が0枚になって消えるなら、取り消し付きで外す（戻すのは −を押す前の行。デッキ用の内訳も戻る）
      if(!next.some(c=>_wishMatchKey(c,name,rarity))){
        const orig=wishGet();
        _wishRemoveAt(orig,orig.findIndex(c=>_wishMatchKey(c,name,rarity)));
        return;
      }
    }
  }catch(e){if(window.DeckHead)window.DeckHead.toast(e.message);else console.warn('[wish]',e);return;}
  wishSave(next);
  const wsr=document.getElementById('wishShopRanking');if(wsr){wsr.classList.add('hidden');wsr.innerHTML='';}
  renderWishlist();
}
function _wishDeckName(id){
  const d=savedDecksGet().find(x=>x.id===id);
  return d?(d.name||'マイデッキ'):'削除したデッキ';
}
// 絞り込みの札を一覧の上に描き、表示する行を返す。内訳を持つ行が無ければ札は出さない
function _renderWishDeckFilter(list){
  const box=document.getElementById('wishDeckFilter');
  const ids=window.WishDeck?WishDeck.deckIds(list):[];
  if(!box)return list;
  if(!ids.length){box.hidden=true;box.innerHTML='';_wishDeckFilter=null;return list;}
  if(_wishDeckFilter!==null&&_wishDeckFilter!==''&&!ids.includes(_wishDeckFilter))_wishDeckFilter=null;
  const hasFree=list.some(c=>WishDeck.unassigned(c)>0);
  if(_wishDeckFilter===''&&!hasFree)_wishDeckFilter=null;
  const opts=[[null,'すべて']].concat(ids.map(id=>[id,_wishDeckName(id)]));
  if(hasFree)opts.push(['','デッキ指定なし']);
  box.hidden=false;
  box.innerHTML=opts.map(([id,label])=>`<button type="button" class="rf-btn${id===_wishDeckFilter?' active':''}" data-deck="${id===null?'*':escAttr(id)}">${esc(label)}</button>`).join('');
  box.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{
    _wishDeckFilter=b.dataset.deck==='*'?null:b.dataset.deck;
    const wsr=document.getElementById('wishShopRanking');if(wsr){wsr.classList.add('hidden');wsr.innerHTML='';}
    renderWishlist();
  }));
  if(_wishDeckFilter===null)return list;
  return list.filter(c=>(_wishDeckFilter===''?WishDeck.unassigned(c):WishDeck.forDeck(c,_wishDeckFilter))>0);
}
// 行の名前の横の札（どのデッキ用に何枚か）。内訳の無い行は出さない
function _wishDeckTagsHtml(c){
  if(!window.WishDeck||!c.decks)return '';
  const tags=Object.entries(c.decks).map(([id,n])=>[_wishDeckName(id),n]);
  const free=WishDeck.unassigned(c);
  if(free)tags.push(['指定なし',free]);
  return tags.map(([label,n])=>`<span class="wish-deck-tag">${esc(label)} ${n}</span>`).join('');
}

// 「どこで買うと安いか」の結果から店のページへ移ったか（流れの最後の段の計測）
document.getElementById('wishShopRanking')?.addEventListener('click',e=>{
  const a=e.target.closest('a[target="_blank"]');
  if(!a)return;
  const shop=a.closest('.wb-shop')?.dataset.k?.replace(/^shop:/,'')||'';
  _ga('wish_shop_click',{link:a.classList.contains('wb-shop-link')?'shop':'item',shop});
});

// 「どこで買うと安いか調べる」ボタンのハンドラ
async function wishCheapestStores(){
  // デッキで絞り込んでいるときは、そのデッキ用の枚数だけで調べる
  const list=window.WishDeck?WishDeck.filter(_wishView(wishGet()),_wishDeckFilter):wishGet();
  if(!list.length)return;
  _ga('wish_best_split',{kinds:list.length,count:list.reduce((n,c)=>n+(c.qty||1),0)});

  const rankEl=document.getElementById('wishShopRanking');
  rankEl.classList.remove('hidden');
  rankEl.innerHTML='<div class="wb-loading"><span class="ld-ring" aria-hidden="true"></span>調べています…</div>';
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

  // まだ店に並ばないカード（未発売・TCG先行）。サーバーが店の検索の対象（db_missing）から外して返す。
  // 一番安い買い方の計算・「そろいません」・店ごとの「この店に無いカード」に数えず、別の1行で知らせる（2026-10-30）
  const unreleasedByKey=new Map((dbData.unreleased||[]).map(m=>[_wishEntryKey(m.name,m.rarity||""),m]));

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
    const missing_set=new Set((s.missing_cards||[]).map(mc=>_wishEntryKey(mc.name,mc.rarity||"")).filter(k=>!unreleasedByKey.has(k)));
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
    const entries=srvEntries.map(c=>({key:_wishEntryKey(c.name,c.rarity||""),qty:c.qty})).filter(e=>!unreleasedByKey.has(e.key));
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

    // 未発売のカードの1行（一番安い買い方があればその枠の中に、無ければ単独で出す）
    const unreleasedHtml=_wishUnreleasedNoteHtml(unreleasedByKey,_missingLabel);
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
        ${unreleasedHtml}
        <div class="wb-buy-shops">${blocks}</div>
        ${compare?`<div class="wb-reco-alt">${compare}</div>`:''}
      </div>`;
    }

    // どの店にも無いカード（見つからなかった・まだ調べ中・未発売）は全店共通なので、店ごとの欠けには数えない
    const nowhere=new Set([...notFoundKeys,...pendingKeys,...unreleasedByKey.keys()]);
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
      ${recoHtml||(pendingKeys.size?'':(entries.length?'<div class="wb-error">どの店にも見つかりませんでした。</div>':''))}
      ${recoHtml?'':unreleasedHtml}
      ${warnHtml}
      ${orderable.length?`<div class="wb-sec">店ごとの支払い（送料込み）</div><div class="wb-shops">${orderable.map((s,i)=>rowHtml(s,i+1)).join('')}</div>`:''}
      ${unorderable.length?`<div class="wb-sec">このままでは注文できない店</div><div class="wb-shops">${unorderable.map(s=>rowHtml(s,0)).join('')}</div>`:''}
      <div class="wb-foot">価格・送料は変わることがあります。購入前に各店で確かめてください。${typeof SearchStatus!=='undefined'?esc(SearchStatus.suspendedNote(['駿河屋'])):''}</div>
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
    // 混雑応答なら数秒後に取り直す（ShopBusy。待つ間は店舗の欄が「取得中」のまま）
    const sseRes=await ShopBusy.fetch(url);
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

// 未発売のカードの知らせ。2種までは名前をそのまま並べ、それより多いときは数と発売日だけの1行にして
// 名前は開いたときに発売日ごとに出す（新弾の発売前は数十種になり、名前の羅列で一番安い買い方が埋もれた。2026-10-30）
const _WISH_UNREL_INLINE_MAX=2;
function _wishUnreleasedNoteHtml(byKey, label){
  if(!byKey.size) return '';
  const groups=new Map();   // いつ買えるか → 名前の並び（届いた順）
  for(const [k,m] of byKey){
    const when=_wishUnreleasedWhen(m);
    if(!groups.has(when)) groups.set(when,[]);
    groups.get(when).push(label(k));
  }
  if(byKey.size<=_WISH_UNREL_INLINE_MAX){
    const items=[...groups].flatMap(([when,names])=>names.map(n=>`${esc(n)}（${esc(when)}）`));
    return `<div class="wb-reco-unrel">未発売のカード：${items.join('、')}（この金額に含みません）</div>`;
  }
  const whenTxt=groups.size===1?[...groups.keys()][0]:[...groups].map(([when,names])=>`${when} ${names.length}種`).join('・');
  const lists=[...groups].map(([when,names])=>
    `<div class="wb-unrel-group">${groups.size>1?`<span class="wb-unrel-when">${esc(when)}</span>`:''}${names.map(esc).join('、')}</div>`).join('');
  // 開閉は「この店で買うカードを見る」と同じ部品（wb-buy-list）。data-k で描き直しても開いたまま
  return `<div class="wb-reco-unrel">未発売の${byKey.size}種（${esc(whenTxt)}）はこの金額に含みません
    <details class="wb-buy-list" data-k="unreleased"><summary>カード名を見る</summary>${lists}</details>
  </div>`;
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
