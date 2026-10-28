// 元は templates/index.html のインライン <script>（2026-09-22 バッチCで外部化）。
// Jinja 依存の値は index.html 側の小さなインライン script が定義する。
// _ga()は<head>先頭で定義済み（GA4計測用の薄いラッパ。G-7: 早期クリック対策で<head>に移設）
let D=[],sort={k:'price',a:true},rarityFilter='',artFilter='',cardImgUrl='',cardImgSmall='',_konamiDbUrl='';
let listView='shop'; // 出品一覧の表示: 'shop'=店ごとの最安（既定・2026-10-21 ユーザー判断）/ 'all'=すべて
const TABLE_PAGE_SIZE=20; // F-11: 一覧の既定表示件数（もっと見るの増分）。散在していたマジックナンバー20を集約
let tableShowCount=TABLE_PAGE_SIZE; // B-4: 一覧の既定表示件数（20行+もっと見る）

// 検索中・詳細表示中は紹介文を畳み、結果に画面を使う。
// DOMの置換や監視ではなく、検索の状態遷移から明示的に切り替える。
function _setSearchLayout(hasResult){
  document.getElementById('mode-search').classList.toggle('has-search-result',hasResult);
  if(!hasResult)Promise.resolve().then(()=>{
    // 履歴復元中の一時的な初期状態ではなく、画面切替完了後の表示状態を見る。
    if(!document.getElementById('mode-search').classList.contains('hidden')&&
       !document.getElementById('empty').classList.contains('hidden'))_loadTopPanels();
  });
}

// トップ用の欄の読込。ランキングは初回だけ（従来どおり以後はタブ操作で更新）、新着は表示のたびに取り直す。
let _topMoversLoaded=false;
function _loadTopPanels(){
  if(!_topMoversLoaded){
    _topMoversLoaded=true;
    loadTopMovers('down');
    loadBuybackMovers('up');
    // 汎用カード列（staple-ranking.js）はこのファイルより後に読まれる。まだ無ければ印を残し、向こうの読込時に始める
    if(window.StapleRanking) window.StapleRanking.load(); else window._stapleRankingPending=true;
  }
  window.NewCards?.load();
}

// ── アフィリエイトリンク変換 ──
let _affConfig={};

// 起動時にアフィリエイト設定を取得
fetch('/api/config').then(r=>r.json()).then(c=>{
  _affConfig=c.affiliate||{};
}).catch(()=>{});

function affLink(url, shop, mode){
  url=safeUrl(url);
  if(!url) return '';
  // 駿河屋: 独自アフィリエイトリダイレクト
  if(shop==='駿河屋'&&_affConfig[shop]){
    const uid=_affConfig[shop].user_id;
    if(uid) return 'https://affiliate.suruga-ya.jp/modules/af/af_jump.php?user_id='+uid+'&goods_url='+encodeURIComponent(url);
  }
  // A8.net形式（カーナベル等）
  if(!_affConfig[shop]) return url;
  const a8mat=_affConfig[shop][mode];
  if(!a8mat) return url;
  return 'https://px.a8.net/svt/ejp?a8mat='+a8mat+'&a8ejpredirect='+encodeURIComponent(url);
}

// 広告表記: 店名ではなく「affLinkが実際に変換したか」で判定する（設定取得前の直リンクは対象外）
function _isAffUrl(orig, converted){
  return !!converted&&converted!==safeUrl(orig);
}
// 変換されたリンクは sponsored、直リンクは従来どおり nofollow
function _affRel(isAff){
  return isAff?'sponsored noopener':'noopener nofollow';
}
const AFF_PR_BADGE='<span class="aff-pr">PR</span>';

function affPixel(shop, mode){
  // A8.netのトラッキングピクセル
  if(!_affConfig[shop]) return '';
  const a8mat=_affConfig[shop][mode];
  if(!a8mat) return '';
  return '<img src="https://www17.a8.net/0.gif?a8mat='+a8mat+'" width="1" height="1" alt="" style="position:absolute">';
}

// ── シェア機能 ──
function copyShareText(text){
  navigator.clipboard.writeText(text).then(()=>{
    const btn=document.querySelector('.share-btn-copy');
    if(btn){const orig=btn.textContent;btn.textContent='コピーしました!';setTimeout(()=>{btn.textContent=orig;},1500);}
  }).catch(()=>{});
}

// シェアメニュー用: 検索結果からシェア選択肢を生成
function _buildShareOptions(cardName, data, results){
  const url=location.origin;
  const opts=[];
  const inStock=results.filter(r=>!r.sold_out);
  const best=inStock.length>0?inStock.reduce((a,b)=>a.price<b.price?a:b):null;
  // 1. 最安値
  if(best){
    opts.push({label:`最安値: ¥${best.price.toLocaleString()}（${best.shop}）`,
      text:`${cardName} 最安値 ¥${best.price.toLocaleString()}（${best.shop}）`});
  }
  // 2. レアリティ別の最高額（高い順に最大3つ）
  const rarities=[...new Set(results.filter(r=>r.rarity&&!r.sold_out).map(r=>r.rarity))];
  const byRarity=rarities.map(r=>{
    const items=inStock.filter(x=>x.rarity===r);
    const hi=items.reduce((a,b)=>a.price>b.price?a:b,{price:0});
    return {rarity:r, price:hi.price, shop:hi.shop};
  }).filter(x=>x.price>0).sort((a,b)=>b.price-a.price);
  for(const r of byRarity.slice(0,3)){
    opts.push({label:`${r.rarity}: ¥${r.price.toLocaleString()}（${r.shop}）`,
      text:`${cardName}【${r.rarity}】¥${r.price.toLocaleString()}（${r.shop}）`});
  }
  // 3. 在庫状況
  const stockCount=inStock.length;
  const shopCount=[...new Set(inStock.map(r=>r.shop))].length;
  if(stockCount>0){
    opts.push({label:`在庫あり ${stockCount}件（${shopCount}店舗）`,
      text:`${cardName} 在庫あり${stockCount}件！ ${shopCount}店舗で見つかりました`});
  }
  return opts;
}

function _shareToX(text){
  window.open('https://x.com/intent/tweet?text='+encodeURIComponent(text+' #強欲なラボ '+location.origin),'_blank');
}
function _shareToLine(text){
  window.open('https://social-plugins.line.me/lineit/share?url='+encodeURIComponent(location.origin)+'&text='+encodeURIComponent(text),'_blank');
}
function _shareCopy(text,btnEl){
  navigator.clipboard.writeText(text+' '+location.origin).then(()=>{
    if(btnEl){const orig=btnEl.textContent;btnEl.textContent='コピーしました!';setTimeout(()=>{btnEl.textContent=orig;},1500);}
  }).catch(()=>{});
}

// S-1: _buildBuyShareOptions（旧・買取タブのシェアメニュー専用）は買取タブ廃止に伴い削除。
// #buyInlineにシェアメニューは無いため、他から参照されていたロジックはない

function toggleShareMenu(menuId){
  const menu=document.getElementById(menuId);
  if(!menu)return;
  const isOpen=menu.classList.contains('open');
  // 全メニューを閉じる
  document.querySelectorAll('.share-menu.open').forEach(m=>m.classList.remove('open'));
  if(!isOpen) menu.classList.toggle('open');
}

// メニュー外クリックで閉じる
document.addEventListener('click',e=>{
  if(!e.target.closest('.share-menu-wrap')){
    document.querySelectorAll('.share-menu.open').forEach(m=>m.classList.remove('open'));
  }
});

// ── カード情報取得（YGOResources優先、YGOProDeckフォールバック）──
function _applyKonamiLink(kid){
  _konamiDbUrl='https://www.db.yugioh-card.com/yugiohdb/card_search.action?ope=2&cid='+kid+'&request_locale=ja';
  const nameEl=document.querySelector('.card-hero-name');
  if(nameEl&&!nameEl.querySelector('a')){
    const a=document.createElement('a');
    a.className='cr-db-link';a.href=_konamiDbUrl;a.target='_blank';a.rel='noopener';a.textContent='公式DB';
    nameEl.appendChild(a);
  }
  const wrap=document.getElementById('heroImgWrap');
  if(wrap){
    const img=wrap.querySelector('img');
    if(img&&!wrap.querySelector('a')){
      const a=document.createElement('a');
      a.href=_konamiDbUrl;a.target='_blank';a.rel='noopener';
      wrap.insertBefore(a,img);
      a.appendChild(img);
    }
  }
}

// 価格関連セクションの表示/非表示を切り替える（未発売カードは価格テーブル等を隠す）
function _setPriceSectionsVisible(visible){
  ['heroMarket','buyInline','priceTrend','rarityFilter','crList'].forEach(id=>{
    const el=document.getElementById(id);
    if(el) el.style.display=visible?'':'none';
  });
}

// 未発売カードを価格検索せずカード情報のみ表示する
// 効果テキストの改行正規化: 元データ(YGOResources)は改行を文字列 "<br>" で持つものが多く
// （2026-09-04 本番DB実測: 14,241枚中10,205枚）、esc() でそのまま文字として表示されていた。
// 一人回しの card-info-panel.js（E-1）と同じ変換。.card-detail-effect は pre-wrap なので改行文字で行分けされる。
// ※トップレベルに置くこと（_renderProxyDetail と _fetchCardInfo の両方から参照する）
function _normalizeBreaks(text){return String(text??'').replace(/<br\s*\/?>/gi,'\n');}

// tcgPreview: TCG先行カード（英語版で先に出て日本に無いカード）。札・説明を変え、購入候補のボタンを出さない（価格が無い）
function showUnreleasedCard(name,tcgPreview){
  if(!tcgPreview&&window.TcgPreview&&TcgPreview.has(name)) tcgPreview=true;
  SearchStatus.show({});
  _setSearchLayout(true);
  // 世代管理: selectSugの未発売分岐はdoSearchを通らず世代が進まないため、稼働中の
  // 販売検索ストリームがあればここで明示的に破棄する（そのままだと後から届くdoneが
  // この未発売カードの見出しの下に前のカードの価格表を描いてしまう）
  _newSearchGen();
  const btn=document.getElementById('btn');
  if(btn){btn.disabled=false;btn.textContent='検索';}
  const prog=document.getElementById('prog');
  if(prog) prog.style.display='none';
  document.getElementById('q').value=name;
  _updateUrl(name,'search');

  // 前回の検索結果をクリア
  cardImgUrl='';cardImgSmall='';D=[];rarityFilter='';artFilter='';tableShowCount=TABLE_PAGE_SIZE;
  _resetBuyInline(); // C-2: 前回検索の買取価格が残ったまま未発売カード表示に切り替わるのを防ぐ
  _updateRarityBanner(); // F-4: 前回検索の絞り込みバナーが残ったまま未発売カード表示に切り替わるのを防ぐ

  document.getElementById('empty').classList.add('hidden');
  document.getElementById('results').classList.remove('hidden');
  _setPriceSectionsVisible(false); // 価格テーブル等を隠す

  const hero=document.getElementById('cardHero');
  hero.classList.remove('hidden');
  hero.classList.add('cr-unreleased'); // 相場の箱を出さないので、その列を取らずに効果テキストを全幅で開く
  document.getElementById('heroImgWrap').innerHTML='<div class="cr-img-ph"></div>';
  const heroInfo=document.getElementById('heroInfo');
  heroInfo.innerHTML=`<div class="card-hero-name">${esc(name)} <span class="pack-unreleased-badge">${tcgPreview?'TCG先行':'発売前'}</span></div>
    <div class="card-hero-meta">
      <span style="color:var(--text-m)">${tcgPreview
        ?'英語版で先に出たカードです。日本ではまだ発売されていないため、価格情報はありません。'
        :'このカードはまだ発売前のため、販売価格情報はありません。'}</span>
    </div>
    <!-- 2026-09-04: 既定で開く（B-2 のスマホ縦長対策で畳んでいたが、PC でも隠れて読めないため全幅で開く裁定） -->
    <details class="card-info-details" id="cardDetailDetails" open hidden>
      <summary>カード情報（効果テキスト）</summary>
      <div id="cardDetail" class="card-detail"></div>
    </details>
    <!-- ボタンは効果テキストの欄（開いた状態で後から中身が入る）が決まってから出す。先に出すと欄が入ったときに
         約450px 押し下げられ、CLS が出ていた（2026-10-22 スマホ 0.019・PC 0.015）。下には他に見えるものが無い -->
    <div class="cr-acts" id="unrelShareBtns" style="display:none">
      <button type="button" class="cr-pill cr-pill-pri" onclick="addToDeck('${escAttr(escJs(name))}',this)">デッキに追加</button>
      ${tcgPreview?'':`<button type="button" class="cr-pill" onclick="wishAddFromBtn(this,'${escAttr(escJs(name))}',1)">購入候補に追加</button>`}
    </div>`;
  // カード情報の処理が終わったら（中身あり・無し・失敗のどれでも）出す。届かないときも5秒で出す
  const unrelBtns=document.getElementById('unrelShareBtns');
  const revealUnrelBtns=()=>{if(unrelBtns) unrelBtns.style.display='';};
  setTimeout(revealUnrelBtns,5000);

  // 画像（公式SAMPLE or プロキシ）
  fetch('/api/card-image?name='+encodeURIComponent(name))
    .then(r=>r.json())
    .then(data=>{
      if(data.url&&safeUrl(data.url)){
        cardImgUrl=safeUrl(data.url);cardImgSmall=cardImgUrl;
        _applyCardImages(cardImgUrl,cardImgSmall,data.source);
      }
    })
    .catch(()=>{});
  // カード情報（効果テキスト等）— proxy 対応
  _fetchCardInfo(name,revealUnrelBtns);
}

// 未発売カードの proxy データからカード情報パネルを描画する
// F-6: <details id="cardDetailDetails">は既定hiddenにしてある。中身を書いた側でここを呼び、
// 「カード情報（効果テキスト）」の見出しだけ空で残る（情報DB未収録カード等）のを防ぐ
function _showCardDetail(){
  document.getElementById('cardDetailDetails')?.removeAttribute('hidden');
  document.getElementById('crFxBtn')?.removeAttribute('hidden');
}
// 発売済みカードの欄は最初から出している（renderAll）。カード情報が無い・取れなかったときに隠す
function _hideCardDetail(){
  document.getElementById('cardDetailDetails')?.setAttribute('hidden','');
  document.getElementById('crFxBtn')?.setAttribute('hidden','');
}

// ── スマホ: 効果テキストを下から出るシートで開く（2026-10-22 ユーザー指定「ポップアップのような形でいい感じに」）。
// 履歴の扱いは環境デッキの全面の詳細（_openMetaSheet）と同じ: 開くときに履歴を1つ足し、ブラウザの「戻る」・Esc・✕・背景で閉じる
let _fxSheetHistory=false,_fxSheetReady=false,_fxSheetPendingBack=false;
// このページは「戻る・進む」のたびに検索をやり直す（_restoreLocation）。シートの開け閉めで足した・戻した履歴の
// popstate はそちらに渡さない（渡すと閉じるたびに検索結果が作り直される）。下の共通の popstate から呼ぶ
function _fxSheetConsumePop(e){
  if(_fxSheetPendingBack){ _fxSheetPendingBack=false; return true; }        // ✕・背景・Esc で閉じて履歴を戻した分
  if(_fxSheetHistory){ _fxSheetHistory=false; _closeFxSheet(true); return true; } // 開いたままブラウザの「戻る」
  return !!(e&&e.state&&e.state.fxSheet);                                    // 閉じた後の「進む」（シートの履歴に戻っただけ）
}
function _fxSheetEl(){
  let el=document.getElementById('crFxSheet');
  if(el) return el;
  el=document.createElement('div');
  el.id='crFxSheet';el.className='cr-fx-sheet';el.hidden=true;
  el.innerHTML='<div class="cr-fx-backdrop" id="crFxBackdrop"></div>'+
    '<div class="cr-fx-panel" role="dialog" aria-modal="true" aria-labelledby="crFxTitle">'+
    '<div class="cr-fx-head"><h2 id="crFxTitle"></h2><button type="button" class="cr-fx-close" id="crFxClose" aria-label="閉じる">✕</button></div>'+
    '<div class="cr-fx-body card-detail" id="crFxBody"></div></div>';
  document.body.appendChild(el);
  document.getElementById('crFxClose').addEventListener('click',()=>_closeFxSheet());
  document.getElementById('crFxBackdrop').addEventListener('click',()=>_closeFxSheet());
  return el;
}
function _openFxSheet(){
  const src=document.getElementById('cardDetail');
  if(!src||!src.innerHTML.trim()) return;
  const el=_fxSheetEl();
  document.getElementById('crFxTitle').textContent=_currentCardName||document.getElementById('q').value.trim();
  document.getElementById('crFxBody').innerHTML=src.innerHTML; // 中身は esc 済みの描画結果（_fetchCardInfo）をそのまま写す
  if(!_fxSheetReady){
    _fxSheetReady=true;
    document.addEventListener('keydown',e=>{ if(e.key==='Escape'&&!document.getElementById('crFxSheet')?.hidden) _closeFxSheet(); });
  }
  if(el.hidden&&history.pushState){ try{ history.pushState({fxSheet:1},''); _fxSheetHistory=true; }catch(_){} }
  el.hidden=false;
  document.getElementById('crFxBody').scrollTop=0;
  document.documentElement.classList.add('cr-fx-open'); // 後ろのページがスクロールしないように
  document.getElementById('crFxClose')?.focus();
  _ga('card_info_open',{trigger:'sheet'});
}
// fromPop: ブラウザの「戻る」で閉じたとき（履歴はもう戻っているので戻さない）
function _closeFxSheet(fromPop){
  const el=document.getElementById('crFxSheet');
  const wasOpen=!!(el&&el.hidden===false); // まだ作っていない・閉じている
  if(el) el.hidden=true;
  document.documentElement.classList.remove('cr-fx-open');
  if(fromPop){ _fxSheetHistory=false; }
  else if(wasOpen&&_fxSheetHistory){ _fxSheetHistory=false; _fxSheetPendingBack=true; try{ history.back(); }catch(_){ _fxSheetPendingBack=false; } }
  if(wasOpen) document.getElementById('crFxBtn')?.focus();
}

// ── スマホ: 出品を上へ（2026-10-22 案M1）。相場の箱と買取を出品の一覧の下へ移す。PC の幅に戻れば元の位置へ。
// CSS の並べ替えでは、相場の箱（名前の行の中）と一覧（その外）の順番を入れ替えられないため要素を動かす。ID は変えない
const _CR_SP_MQ='(max-width:640px)';
function _crPlaceForViewport(){
  const hero=document.getElementById('cardHero'),mk=document.getElementById('heroMarket');
  const buy=document.getElementById('buyInline'),list=document.getElementById('crList');
  if(!hero||!mk||!buy||!list) return;
  const sp=!!(window.matchMedia&&matchMedia(_CR_SP_MQ).matches);
  if(sp){
    if(mk.parentNode===hero){ list.after(mk); mk.after(buy); }
  }else{
    if(mk.parentNode!==hero){ hero.appendChild(mk); hero.after(buy); }
    _closeFxSheet();
  }
}
if(window.matchMedia){
  const _crMq=matchMedia(_CR_SP_MQ);
  if(_crMq.addEventListener) _crMq.addEventListener('change',_crPlaceForViewport); else if(_crMq.addListener) _crMq.addListener(_crPlaceForViewport);
}
_crPlaceForViewport();

// PC の検索結果で最初から開いている効果テキストの高さを、右の相場の箱（1023px 以下は画像）の底までに抑える。
// 超える分は「続きを読む」で開く。長い効果（ペンデュラム等）で名前の行だけが伸び、下のレアリティ欄と一覧が
// 押し下げられる（後から届くので CLS にもなる）のを防ぐ。未発売カード（ヒーロー直下の全幅表示）は対象外
function _clampCardDetail(){
  const det=document.getElementById('cardDetail');
  const box=document.getElementById('cardDetailDetails');
  document.getElementById('cardDetailMore')?.remove();
  if(!det||!box) return;
  det.classList.remove('cr-clamped');
  det.style.maxHeight='';
  if(!box.closest('.cr-acts')||!box.open||box.hidden||det.dataset.expanded==='1') return;
  if(!(window.matchMedia&&window.matchMedia('(min-width:641px)').matches)) return;
  const top=det.getBoundingClientRect().top;
  const img=document.getElementById('heroImgWrap');
  const market=document.getElementById('heroMarket');
  const wide=window.matchMedia('(min-width:1024px)').matches;
  const bottom=Math.max(img?img.getBoundingClientRect().bottom:0,wide&&market?market.getBoundingClientRect().bottom:0);
  if(det.scrollHeight<=bottom-top+1) return; // 全文が収まる
  const lh=parseFloat(getComputedStyle(det).lineHeight)||20;
  // ボタン1行ぶんを残し、行の途中で切れないよう行の高さの倍数にする（少なくとも5行は見せる）
  const avail=Math.max(lh*5,Math.floor((bottom-top-lh-8)/lh)*lh);
  det.style.maxHeight=avail+'px';
  det.classList.add('cr-clamped');
  const btn=document.createElement('button');
  btn.type='button';btn.id='cardDetailMore';btn.className='cr-more';btn.textContent='続きを読む';
  btn.addEventListener('click',()=>{
    det.dataset.expanded='1';
    det.classList.remove('cr-clamped');
    det.style.maxHeight='';
    btn.remove();
    _ga('card_info_more');
  });
  det.after(btn);
}
let _clampTimer=null;
window.addEventListener('resize',()=>{ clearTimeout(_clampTimer); _clampTimer=setTimeout(_clampCardDetail,150); });

function _renderProxyDetail(p,name){
  const det=document.getElementById('cardDetail');
  if(!det) return;
  let html='<div class="card-detail-meta">';
  if(p.broad_type==='monster'){
    const lvl=p.link_val!=null?`LINK-${p.link_val}`:p.rank!=null?`ランク${p.rank}`:(p.level!=null?`Lv${p.level}`:'');
    const scale=p.pendulum_scale!=null?`スケール${p.pendulum_scale}`:'';
    const statParts=[p.attribute,p.race,lvl,scale].filter(Boolean);
    const def=p.link_val!=null?'':` / DEF ${p.def||'-'}`;
    let statLine=`<span class="cdm-type">${esc(p.card_type||'')}</span>`;
    if(statParts.length) statLine+=` ${statParts.map(esc).join(' · ')}`;
    statLine+=` ATK ${p.atk??'-'}${def}`;
    html+=`<div class="cdm-row cdm-sub"><span>${statLine}</span></div>`;
  } else {
    html+=`<div class="cdm-row">${esc(p.card_type||'')}</div>`;
  }
  html+='</div>';
  // TCG先行カード（英語版で先に出て日本に無いカード）: 効果文は機械訳なので「非公式訳」と示し、英語の原文を折りたたみで添える
  const unofficial=p.tcg_preview&&p.unofficial_translation?'<span class="card-detail-unofficial">非公式訳（機械翻訳）</span>':'';
  if(p.pendulum_effect) html+=`<div class="card-detail-effect">${unofficial}<span class="card-detail-pendulum-label">【ペンデュラム効果】</span>${esc(_normalizeBreaks(p.pendulum_effect))}</div>`;
  if(p.effect_text) html+=`<div class="card-detail-effect">${p.pendulum_effect?'':unofficial}${esc(_normalizeBreaks(p.effect_text))}</div>`;
  if(p.tcg_preview&&(p.effect_text_en||p.pendulum_effect_en)){
    const en=[p.pendulum_effect_en?'[Pendulum Effect]\n'+_normalizeBreaks(p.pendulum_effect_en):'',_normalizeBreaks(p.effect_text_en||'')].filter(Boolean).join('\n');
    // 訳が無い（通常モンスターの説明文・未訳）ときは原文が唯一の本文なので開いておく
    html+=`<details class="card-detail-original"${p.unofficial_translation?'':' open'}><summary>英語の原文</summary><div class="card-detail-effect" lang="en">${esc(en)}</div></details>`;
  }
  const meta=p.tcg_preview
    ?[p.product_name,p.tcg_release_date?'英語版 '+p.tcg_release_date:''].filter(Boolean).join(' / ')
    :[p.product_name,p.release_date].filter(Boolean).join(' / ');
  if(meta) html+=`<div class="card-detail-print" style="margin-top:6px">${esc(meta)}</div>`;
  det.innerHTML=html;
  // 未発売カード専用: 読み取り・転記ミスを利用者が知らせる窓口（発売済みカードには出さない）。
  // TCG先行カードの訳は機械訳で、報告の受け先（未発売カードの登録）とは別なので出さない
  if(window.TextReport&&!p.tcg_preview) window.TextReport.attach(det,p.name||name);
  _showCardDetail();
}

// onSettled: 欄の中身を書いた（または隠した）直後に呼ぶ（未発売カードのボタンを出す。showUnreleasedCard）
function _fetchCardInfo(cardName,onSettled=()=>{}){
  fetch('/api/card-info?name='+encodeURIComponent(cardName))
    .then(r=>r.json())
    .then(data=>{
      // 未発売カード（proxy）は専用描画。found:false でもカード情報を表示する
      if(data.kind==='proxy'&&data.proxy){_renderProxyDetail(data.proxy,cardName);return;}
      if(!data.found){_hideCardDetail();fetchCardImage(cardName);return;}
      if(data.konami_id) _applyKonamiLink(data.konami_id);
      const det=document.getElementById('cardDetail');
      if(!det) return;
      // 最新の収録の型番（data.latest_print）は出さない。最安の行と一覧が「出品の型番」を出しており、
      // 見出しなしで2種類の型番が並ぶと取り違える（2026-10-22 ユーザー指摘）
      const limitCls={'禁止':'lb-forbidden','制限':'lb-limited','準制限':'lb-semi'};
      const limitBadge=data.limit?`<span class="lb ${limitCls[data.limit]||''}">${esc(data.limit)}</span>`:'';
      let html='<div class="card-detail-meta">';
      if(data.broad_type==='monster'){
        const lvl=data.link_val!=null?`LINK-${data.link_val}`:data.rank!=null?`ランク${data.rank}`:`Lv${data.level??'?'}`;
        const scale=data.pendulum_scale!=null?`スケール${data.pendulum_scale}`:'';
        const statParts=[data.attribute,data.race,lvl,scale].filter(Boolean);
        const def=data.link_val!=null?'':` / DEF ${data.def??'-'}`;
        let statLine=`<span class="cdm-type">${esc(data.card_type||'')}</span>`;
        if(statParts.length) statLine+=` ${statParts.map(esc).join(' · ')}`;
        statLine+=` ATK ${data.atk??'-'}${def}`;
        html+=`<div class="cdm-row cdm-sub"><span>${statLine}</span>${limitBadge}</div>`;
      } else {
        html+=`<div class="cdm-row">${esc(data.card_type+(data.property?` [${data.property}]`:'')||'')}${limitBadge}</div>`;
      }
      html+='</div>';
      if(data.pendulum_effect) html+=`<div class="card-detail-effect"><span class="card-detail-pendulum-label">【ペンデュラム効果】</span>${esc(_normalizeBreaks(data.pendulum_effect))}</div>`;
      if(data.effect_text) html+=`<div class="card-detail-effect">${esc(_normalizeBreaks(data.effect_text))}</div>`;
      det.innerHTML=html;
      _showCardDetail();
      _clampCardDetail();
    })
    .catch(()=>{_hideCardDetail();fetchCardImage(cardName);})
    .finally(onSettled);
}

function fetchCardImage(cardName){
  fetch('https://db.ygoprodeck.com/api/v7/cardinfo.php?name='+encodeURIComponent(cardName)+'&misc=yes&language=ja')
    .then(r=>r.json())
    .then(data=>{
      if(!data.data||!data.data[0]) return;
      const card=data.data[0];
      const konami_id=(card.misc_info&&card.misc_info[0])?card.misc_info[0].konami_id:null;
      if(konami_id&&!_konamiDbUrl) _applyKonamiLink(konami_id);

      // 画像が未取得の場合のみYGOProDeck画像を使用
      if(!cardImgUrl||cardImgUrl.length<5){
        const imgs=card.card_images?card.card_images[0]:null;
        if(imgs){
          cardImgUrl=imgs.image_url||'';
          cardImgSmall=imgs.image_url_small||cardImgUrl;
          _applyCardImages(cardImgUrl,cardImgSmall);
        }
      }
    })
    .catch(()=>{});
}

// カードの画像の URL を、検索を始めた時点で問い合わせて画像も読み始めておく（2026-10-22 LCP）。
// 以前は検索が終わってから URL を問い合わせ、そこから画像を取っていたので、カードのページの LCP（カード画像）が
// 検索の時間ぶん遅れていた（スマホ相当で取得開始まで 4〜10秒）。renderAll は同じ名前ならこの結果を使う
let _cardImgPrefetch=null; // {name, p: Promise<data>}
function _prefetchCardImage(name){
  const p=fetch('/api/card-image?name='+encodeURIComponent(name)).then(r=>r.json());
  const entry={name,p};
  _cardImgPrefetch=entry;
  _showSearchPreview(name);
  p.then(data=>{
    const u=data&&data.url&&safeUrl(data.url);
    if(!u)return;
    // 検索中の先出しの欄に画像を入れる（まだこの検索の先出しが出ているときだけ）
    const wrap=document.getElementById('searchPreviewImg');
    if(_cardImgPrefetch===entry&&wrap&&!document.getElementById('searchPreview')?.hidden&&!wrap.querySelector('img')){
      wrap.innerHTML='<img src="'+escAttr(u)+'" alt="" loading="eager" fetchpriority="high">';
    }else{const im=new Image();im.src=u;}
  }).catch(()=>{});
}
// 検索中に先に出すカードの欄（画像と名前だけ）。最終のカードの欄と同じ位置・大きさで出し、
// 検索が終わったら結果と同じフレームで隠す。カードのページの LCP（カード画像）を検索の終わりより前に出すため（2026-10-22）
function _showSearchPreview(name){
  const box=document.getElementById('searchPreview');
  if(!box)return;
  document.getElementById('searchPreviewImg').innerHTML='';
  document.getElementById('searchPreviewName').textContent=name;
  box.hidden=false;
}
function _hideSearchPreview(){
  const box=document.getElementById('searchPreview');
  if(box&&!box.hidden){box.hidden=true;document.getElementById('searchPreviewImg').innerHTML='';}
}
function _cardImageData(name){
  if(_cardImgPrefetch&&_cardImgPrefetch.name===name) return _cardImgPrefetch.p;
  return fetch('/api/card-image?name='+encodeURIComponent(name)).then(r=>r.json());
}

function _applyCardImages(heroSrc,thumbSrc,source){
  const wrap=document.getElementById('heroImgWrap');
  const safeHero=safeUrl(heroSrc);
  if(wrap&&safeHero&&!wrap.querySelector('img')){
    // 画面の上に出るカードの画像（多くのページで LCP）なので、遅らせずに優先して読む
    wrap.innerHTML='<img src="'+escAttr(safeHero)+'" alt="" loading="eager" fetchpriority="high">';
    // 発売済みクリーン画像にのみ自サイト透かしを付与（SAMPLE画像には付けない）
    if(source!=='official_sample'){ wrap.classList.add('wm-released'); }
    else { wrap.classList.remove('wm-released'); }
  }
  // 一覧の各行の小さい画像はやめた（同じ絵を全行にくり返さない・2026-10-21 案C）ので、thumbSrc は使わない
}

// /api/card-images バッチの値から表示用URLを取り出す。
// 値は 文字列URL（発売済み）/ {kind:'image',url,source}（未発売SAMPLE）/ {kind:'proxy'} / null。
// 画像でない（proxy/none）場合は null を返す。
function _batchImgUrl(v){
  if(!v) return null;
  if(typeof v==='string') return v;
  if(v.kind==='image'&&v.url) return v.url;
  return null;
}

// J-1と同様: 販売検索SSEにも世代管理を入れる。旧ストリームのイベントが新しい検索結果を
// 上書きしないよう、_searchGenを世代トークンとして使う（buyInlineの_buyInlineES/_buyInlineGen/
// _buyInlineTimeoutIdと同じ形）
let _searchES=null;        // 進行中のEventSource参照。新しい検索を張る前に確実に閉じるために保持する
let _searchGen=0;          // 世代トークン。close()後も配送されうる旧SSEのイベントを無効化する
let _searchTimeoutId=null; // タイムアウトタイマーの参照。新しい検索を張る前に確実にclearTimeoutする

let _mercariPending=false; // 店舗検索が終わっていない間 true（メルカリ参考相場を「確認中」表示にする）

function _newSearchGen(){ // 世代加算＝旧ストリーム破棄、を不可分にする（片方だけ進むと旧SSEが無限再接続する）
  if(_searchES){ try{_searchES.close();}catch(e){} _searchES=null; }
  _hideSearchPreview(); // 前の検索の先出しの欄を残さない
  if(_searchTimeoutId){ clearTimeout(_searchTimeoutId); _searchTimeoutId=null; }
  return ++_searchGen;
}

// 検索を始めずに終わった時、結果も初期表示も無い白紙を残さない（/card/ 直アクセスは #empty を隠して始めるため）
function _showEmptyIfNoResults(){
  if(!document.getElementById('results').classList.contains('hidden'))return;
  document.getElementById('empty').classList.remove('hidden');
  _setSearchLayout(false);
}
function doSearch(opts){
  closeSuggest();
  // opts: {validated:true} サジェスト選択やvalidate済み, {confirmed:true} DB未登録を承認済み,
  //       {trigger:'user'|'pageload'|'popstate'|...} G-2: 計測用。省略時は'user'（検索窓からの手動検索）
  //       {_gen} /api/validateを挟む再帰呼び出しが、入口で採番した世代を引き継ぐための内部用引数
  //       {_validation} 1文字の名前を確かめた /api/validate の応答（同じ問い合わせを2回しないよう引き継ぐ内部用引数）
  opts=opts||{};
  const trigger=opts.trigger||'user';
  const q=document.getElementById('q').value.trim();
  if(!q)return;
  // 1文字は既知のカード名と完全一致したときだけ通す（秋・山・闇 など1文字のカードが実在する）。
  // 打ちかけの1文字では検索しない。確かめる間は世代を進めない（無効な入力で進行中の検索を止めない）
  if(q.length<2&&!opts.validated&&!opts._validation){
    const reject=()=>{
      if(document.getElementById('q').value.trim()!==q) return; // 確かめる間に入力が変わった
      // URL から開いたときは警告を出さずトップの表示に戻す（手入力のときだけ従来の警告）
      if(trigger==='pageload'||trigger==='popstate') document.getElementById('q').value='';
      else alert('カード名を2文字以上入力してください');
      _showEmptyIfNoResults();
    };
    fetch('/api/validate?q='+encodeURIComponent(q))
      .then(r=>r.json())
      .then(d=>{
        if(document.getElementById('q').value.trim()!==q) return;
        if(d.valid&&d.name===q) doSearch({...opts,_validation:d});
        else reject();
      })
      .catch(reject);
    return;
  }
  // 世代は「ユーザー起点の呼び出し」（入口）でのみ進める。/api/validateの応答を待つ再帰呼び出しは
  // opts._genで元の世代を引き継ぎ、古い検索の応答が後から来ても新しい世代を追い越させない。
  // 無効な入力（空/2文字未満）で世代だけ進めてしまわないよう、上のチェックより後で採番する
  const gen=(opts._gen!==undefined)?opts._gen:_newSearchGen();
  // validate済み・confirmed済みでなければ、まず /api/validate でカード名を確認
  if(!opts.validated&&!opts.confirmed){
    const btn=document.getElementById('btn');
    btn.disabled=true;btn.textContent='確認中...';
    (opts._validation?Promise.resolve(opts._validation):fetch('/api/validate?q='+encodeURIComponent(q)).then(r=>r.json()))
      .then(d=>{
        if(gen!==_searchGen) return; // 世代管理: 古い応答が#q/btnを触らないようにする
        btn.disabled=false;btn.textContent='検索';
        if(d.valid){
          document.getElementById('q').value=d.name;
          // 未発売カードは価格検索せず、カード情報のみ表示する
          // G-2: 検索意図はあったが未発売のため検索に至らなかったケースも計上する（逆方向の欠測対策）
          if(d.unreleased){_ga('search_submit',{mode:'sell',trigger:trigger,result:'unreleased'});showUnreleasedCard(d.name,!!d.tcg_preview);return;}
          doSearch({validated:true,trigger:trigger,_gen:gen});
        }else if(d.suggestion){
          if(confirm('もしかして: '+d.suggestion+'\n\nこのカード名で検索しますか?')){
            document.getElementById('q').value=d.suggestion;
            doSearch({validated:true,trigger:trigger,_gen:gen});
          }else{ _mercariPending=false; if(window.MercariRef) MercariRef.clear(); _showEmptyIfNoResults(); }
        }else{
          // TODO: _correct_cardname()が部分一致候補を返せるようになったらsuggestion分岐が有効になる
          if(confirm('該当するカード名が見つかりません。\nそのまま検索しますか?')){
            doSearch({confirmed:true,trigger:trigger,_gen:gen});
          }else{ _mercariPending=false; if(window.MercariRef) MercariRef.clear(); _showEmptyIfNoResults(); }
        }
      })
      .catch(()=>{if(gen!==_searchGen)return;btn.disabled=false;btn.textContent='検索';_showEmptyIfNoResults();});
    return;
  }
  // 世代管理: 古い検索の /api/validate の戻りが後から来た場合は、画面状態（ボタン無効化・
  // 結果クリア・進捗リセット）を触る前にここで打ち切る（新しい検索の表示を壊さない）
  if(gen!==_searchGen) return;
  _ga('search_submit',{mode:'sell',trigger:trigger});
  const btn=document.getElementById('btn');
  btn.disabled=true;btn.textContent='検索中...';
  _setSearchLayout(true);
  document.getElementById('results').classList.add('hidden');
  document.getElementById('empty').classList.add('hidden');
  document.getElementById('emptyError').classList.add('hidden');  // Q-10: 前回のエラー表示を消す
  // 直前が未発売カード表示だった場合に隠した価格セクションを復元する
  _setPriceSectionsVisible(true);

  // 前回の検索結果をクリア
  cardImgUrl='';cardImgSmall='';
  const heroWrap=document.getElementById('heroImgWrap');
  if(heroWrap) heroWrap.innerHTML='';
  const cardHero=document.getElementById('cardHero');
  if(cardHero){ cardHero.classList.add('hidden'); cardHero.classList.remove('cr-unreleased'); }
  const tbody=document.getElementById('tbody');
  if(tbody) tbody.innerHTML='';

  // 検索対象店舗はサーバ側の既定（DEFAULT_SHOPS）が決める。フロントはトグルUIから
  // 店舗集合を組み立てず、SSEで届くprogress/shop_doneイベントに合わせて進捗行を
  // その場で作る（#buyInlineのstep()と同じ方式。1箇所＝サーバに一本化）。
  const prog=document.getElementById('prog');
  prog.innerHTML='';
  prog.style.display='flex';
  function _searchProgStep(shop){
    let el=document.getElementById('s-'+shop);
    if(!el){
      el=document.createElement('div');
      el.className='pstep';
      el.id='s-'+shop;
      el.innerHTML=`<span class="pname">${esc(shop)}</span><span class="pstat">待機中</span>`;
      prog.appendChild(el);
    }
    return el;
  }

  SearchStatus.show({});
  D=[];rarityFilter='';artFilter='';tableShowCount=TABLE_PAGE_SIZE;
  _resetBuyInline(); // C-2: 新しい検索をしたら買取セクションは閉じた未取得状態に戻す（前のカードの買取価格が残ると誤情報になるため）
  _updateRarityBanner(); // F-4: 前回検索の絞り込みバナーが残ったまま次の検索結果が出るのを防ぐ
  const params=new URLSearchParams({q});
  if(opts.confirmed)params.append('confirmed','true');

  // 世代管理: 古いvalidateの戻りが後から来てここに到達しても、旧世代のストリームは張らない
  // （旧ストリームのclose/clearTimeoutは_newSearchGen()側で採番と不可分に行われている）
  if(gen!==_searchGen) return;

  _prefetchCardImage(q); // カードの画像を検索と並行して読み始める（renderAll が使う）
  // メルカリ参考相場は店舗検索と並行して取得し、店舗検索の途中でもパネルを出す（レアリティは店舗結果が揃うまで「確認中」）
  // 取得はこの検索SSEの最初のイベント受信時に1回だけ開始する（/api/search が429等で失敗した検索・即破棄された検索では呼ばない）。
  // 名前の補正は /api/mercari がサーバー側で /api/search と同じ補正をかけるので、補正イベントでの取り直しはしない
  _mercariPending=true;
  _syncMercariView();
  let mercariStarted=false;
  let trendName='';
  // 店舗検索が混雑していると、サーバーは {"type":"busy"} の1件だけを返す（app.py _search_busy_response）。
  // そのときは失敗にせず「順番待ち」を出し、数秒後に同じ検索を張り直す（世代が進んだら張り直さない）
  const _openSearchStream=busyTries=>{
  const es=new EventSource('/api/search?'+params.toString());
  _searchES=es;
  // 60秒で応答がなければタイムアウト
  function _searchTimeoutFail(){
    if(gen!==_searchGen) return; // 世代管理: 別カードの検索で世代が進んでいたら無視
    _mercariPending=false;if(window.MercariRef) MercariRef.clear();
    es.close();_searchES=null;_searchTimeoutId=null;btn.disabled=false;btn.textContent='検索';
    prog.style.display='none';
    _hideSearchPreview();
    document.getElementById('empty').classList.remove('hidden');
    // Q-10: #emptyのinnerHTML全置換をやめ、専用要素だけを書き換える（ランキングDOMを壊さない）
    const _errEl=document.getElementById('emptyError');
    _errEl.textContent='検索がタイムアウトしました。再度お試しください。';
    _errEl.classList.remove('hidden');
    _setSearchLayout(!document.getElementById('results').classList.contains('hidden'));
  }
  _searchTimeoutId=setTimeout(_searchTimeoutFail,60000);
  es.onmessage=e=>{
    if(gen!==_searchGen) return; // 世代管理: closeしても配送されうる旧世代のイベントを無効化する
    clearTimeout(_searchTimeoutId);
    _searchTimeoutId=setTimeout(_searchTimeoutFail,60000);
    const d=JSON.parse(e.data);
    if(d.type==='busy'){
      es.close();_searchES=null;clearTimeout(_searchTimeoutId);_searchTimeoutId=null;
      if(busyTries+1>=ShopBusy.MAX_TRIES){
        _searchTimeoutFail();
        document.getElementById('emptyError').textContent='店舗検索が混雑しています。時間をおいて再度お試しください。';
        return;
      }
      _searchBusyStep(prog,true);
      _searchTimeoutId=setTimeout(()=>{ if(gen===_searchGen) _openSearchStream(busyTries+1); },ShopBusy.retryMs(d.retry_after));
      return;
    }
    if(!mercariStarted){
      mercariStarted=true;
      if(window.MercariRef) MercariRef.load(q);
      // 価格推移も店舗検索と並行して取る（完了後に取り始めると、表示済みの表の上に後から差し込まれて表が跳ねる）
      if(window.PriceTrend){ trendName=q; PriceTrend.load(q); }
    }
    if(d.type==='shops'){
      // 調べる店の一覧（サーバーが最初に送る）。進み具合の札を全店ぶん「待機中」で先に並べる。
      // 届くたびに1枚ずつ足すと、既存の札が細くなって店名が折り返し、画面がずれていた（2026-10-22）
      _searchBusyStep(prog,false);
      (d.shops||[]).forEach(s=>_searchProgStep(s));
    }else if(d.type==='corrected'){
      // カード名が自動補正された場合、検索欄を更新して通知
      document.getElementById('q').value=d.corrected;
      const notice=document.createElement('div');
      notice.className='correct-notice';
      notice.textContent='「'+d.original+'」→「'+d.corrected+'」に補正して検索しました';
      const prog=document.getElementById('prog');
      prog.parentNode.insertBefore(notice,prog);
      setTimeout(()=>notice.remove(),8000);
    }else if(d.type==='progress'){
      const el=_searchProgStep(d.shop);
      el.className='pstep searching';el.querySelector('.pstat').textContent='検索中...';
    }else if(d.type==='waiting'){
      // 店舗の取得口の順番待ち（数秒ごとに届く）。まだ順番が来ていない店だけ「順番待ち」にする
      _searchMarkQueued(prog,d.queued_shops||[]);
    }else if(d.type==='shop_done'){
      const el=_searchProgStep(d.shop);
      if(d.status==='busy'){
        // 混雑で順番が回ってこなかった店。前回の結果があればそれを出している
        el.className='pstep error';el.querySelector('.pstat').textContent=d.count?'混雑・前回の'+d.count+'件':'混雑で取得できず';
      }else{
      el.className=d.status==='partial'?'pstep error':'pstep done';el.querySelector('.pstat').textContent=d.status==='partial'?(d.count?d.count+'件・一部失敗':'取得失敗'):d.count+'件'+(d.cached?' (cache)':'');
      }
      // 結果の表は全店そろった done で一度だけ描く（店舗ごとに描き直すと、並べ替えと後から入る
      // カード画像・集計で表が上下に跳ねるため。2026-10-16 ユーザー裁定）。途中は上の進捗行で件数だけ見せる
    }else if(d.type==='shop_error'){
      const el=_searchProgStep(d.shop);
      el.className='pstep error';el.querySelector('.pstat').textContent=d.busy?'混雑':'エラー';
    }else if(d.type==='done'){
      clearTimeout(_searchTimeoutId);_searchTimeoutId=null;
      es.close();_searchES=null;D=d.results;
      // 補正後のカード名で検索欄を更新
      if(d.corrected_name) document.getElementById('q').value=d.corrected_name;
      // J-2: 買取セクションの取得キーは「入力欄の現在値」ではなく、ここで確定した表示カード名にする
      _currentCardName=document.getElementById('q').value.trim();
      SearchStatus.show(d);
      renderAll(d);
      _mercariPending=false;
      if(d.total>0) _syncMercariView(); // メルカリ参考相場は最初のイベント受信時に取得済み。最終の店舗レアリティで絞り直す
      else if(window.MercariRef) MercariRef.clear(); // 全店0件ならメルカリも出さない
      if(window.PriceTrend){
        // 開始時に並行取得済み。補正などで表示カード名が変わった時だけ取り直す。0件なら出さない
        if(d.total===0) PriceTrend.clear();
        else{ _syncPriceTrendView(); if(_currentCardName!==trendName) PriceTrend.load(_currentCardName); }
      }
      btn.disabled=false;btn.textContent='検索';
      _setSearchLayout(d.total>0);
      prog.style.display='none'; // 結果と同じフレームで消す（遅らせて消すと表示済みの結果が上へずれる）
      _hideSearchPreview(); // 先出しの欄も同じフレームで隠す（同じ位置に本物のカードの欄が出る）
    }
  };
  es.onerror=()=>{
    if(gen!==_searchGen) return; // 世代管理
    clearTimeout(_searchTimeoutId);_searchTimeoutId=null;
    es.close();_searchES=null;btn.disabled=false;btn.textContent='検索';
    _mercariPending=false;if(window.MercariRef) MercariRef.clear();
    prog.style.display='none';
    _hideSearchPreview();
    document.getElementById('empty').classList.remove('hidden');
    const _errEl2=document.getElementById('emptyError');
    _errEl2.textContent='エラーが発生しました。再度お試しください。';
    _errEl2.classList.remove('hidden');
    _setSearchLayout(!document.getElementById('results').classList.contains('hidden'));
  };
  };
  _openSearchStream(0);
}
// 混雑応答のあいだ、進み具合の欄に「順番待ち」の札を1枚出す（show=false で消す。店の札が並んだら消える）
function _searchBusyStep(prog,show){
  let el=document.getElementById('searchBusyStep');
  if(!show){ if(el) el.remove(); return; }
  if(!el){
    el=document.createElement('div');
    el.id='searchBusyStep';
    el.className='pstep searching';
    el.innerHTML='<span class="pname">順番待ち</span><span class="pstat"></span>';
    prog.appendChild(el);
  }
  el.querySelector('.pstat').textContent=ShopBusy.WAIT_TEXT;
}
// 取得中の店の札を「順番待ち」と「検索中...」に振り分ける（取得口の順番が来たら検索中に戻る）
function _searchMarkQueued(prog,queued){
  prog.querySelectorAll('.pstep.searching').forEach(el=>{
    if(!el.id||el.id.indexOf('s-')!==0) return;
    el.querySelector('.pstat').textContent=queued.includes(el.id.slice(2))?'順番待ち':'検索中...';
  });
}
// ── Autocomplete ──
const qInput=document.getElementById('q');
const sugDrop=document.getElementById('suggestDrop');
let sugTimer=null, sugIdx=-1, sugItems=[], sugGeneration=0;

qInput.addEventListener('input',()=>{
  closeSuggest();
  const v=qInput.value.trim();
  if(v.length<2){closeSuggest();return;}
  const generation=sugGeneration;
  sugTimer=setTimeout(()=>fetchSuggest(v,generation),250);
});

qInput.addEventListener('keydown',e=>{
  if(sugDrop.classList.contains('open')&&sugItems.length>0){
    if(e.key==='ArrowDown'){e.preventDefault();sugIdx=Math.min(sugIdx+1,sugItems.length-1);highlightSug();return;}
    if(e.key==='ArrowUp'){e.preventDefault();sugIdx=Math.max(sugIdx-1,-1);highlightSug();return;}
    if(e.key==='Enter'&&sugIdx>=0){e.preventDefault();selectSug(sugItems[sugIdx].name,sugItems[sugIdx].unreleased,sugItems[sugIdx].tcg_preview);return;}
    if(e.key==='Escape'){closeSuggest();return;}
  }
  if(e.key==='Enter')doSearch();
});

// 候補の選択は mousedown で blur より先に処理される。
qInput.addEventListener('blur',closeSuggest);

function fetchSuggest(q,generation=sugGeneration){
  const isCurrent=()=>generation===sugGeneration && document.activeElement===qInput && qInput.value.trim()===q;
  if(!isCurrent())return;
  // include_unreleased=1 で未発売カードも候補に含める（レスポンスは {name, unreleased} の配列）
  fetch('/api/suggest?q='+encodeURIComponent(q)+'&include_unreleased=1')
    .then(r=>r.json())
    .then(items=>{
      if(!isCurrent())return;
      if(!items.length){closeSuggest();return;}
      sugItems=items;sugIdx=-1;
      sugDrop.innerHTML=items.map((it,i)=>{
        const n=it.name;
        // TCG先行（英語版で先に出て日本に無いカード）も unreleased=true で届く
        if(it.tcg_preview&&window.TcgPreview) TcgPreview.note(n);
        const badge=it.unreleased?` <span class="pack-unreleased-badge">${it.tcg_preview?'TCG先行':'発売前'}</span>`:'';
        return `<div class="suggest-item" data-i="${i}" onmousedown="selectSug('${escAttr(escJs(n))}',${it.unreleased?'true':'false'},${it.tcg_preview?'true':'false'})"><span class="suggest-thumb" data-thumb-name="${escAttr(n)}"></span><span class="suggest-name">${esc(n)}${badge}</span></div>`;
      }).join('');
      sugDrop.classList.add('open');
      loadSuggestThumbnails(sugDrop);
    })
    .catch(()=>{if(isCurrent())closeSuggest();});
}

// 検索候補にカード画像サムネを一括取得して差し込む（販売/買取 共通。マイデッキの実装と同方式）
function loadSuggestThumbnails(dropEl){
  const thumbs=[...dropEl.querySelectorAll('.suggest-thumb[data-thumb-name]')];
  const names=[...new Set(thumbs.map(t=>t.dataset.thumbName).filter(Boolean))];
  if(!names.length)return;
  fetch('/api/card-images',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({names})})
    .then(r=>r.json())
    .then(({images})=>{
      if(!images)return;
      thumbs.forEach(t=>{
        const url=_batchImgUrl(images[t.dataset.thumbName]);
        if(url&&safeUrl(url)) t.innerHTML=`<img src="${escAttr(url)}" alt="" loading="lazy">`;
      });
    })
    .catch(()=>{});
}

function highlightSug(){
  sugDrop.querySelectorAll('.suggest-item').forEach((el,i)=>{
    el.classList.toggle('active',i===sugIdx);
  });
  if(sugIdx>=0){
    const el=sugDrop.children[sugIdx];
    if(el)el.scrollIntoView({block:'nearest'});
  }
}

function selectSug(name,unreleased,tcgPreview){
  qInput.value=name;
  closeSuggest();
  // 未発売カード（TCG先行カードを含む）は価格検索せず、カード情報のみ表示する
  if(unreleased){_ga('search_submit',{mode:'sell',trigger:'user',result:'unreleased'});showUnreleasedCard(name,!!tcgPreview);return;}
  doSearch({validated:true,trigger:'user'});
}

function closeSuggest(){
  clearTimeout(sugTimer);sugTimer=null;sugGeneration++;
  sugDrop.classList.remove('open');sugIdx=-1;sugItems=[];
}

function renderAll(d){
  _setSearchLayout(d.total>0);
  document.getElementById('results').classList.remove('hidden');
  const searchTerm0=document.getElementById('q').value.trim();
  // S-2: /buy/<カード名>からの検索は/buy/のURLのまま保つ（'buyback'指定時はプレフィックスが
  // 既に一致しておりpushStateされないため実質no-op。フラグは_maybeOpenBuyInlineOnLoadで消費する前に読む）
  if(d.total>0) _updateUrl(searchTerm0,_pendingBuyInlineOpen?'buyback':'search');
  if(d.total===0){
    // 0件でも詳細・比較表のDOMを保持し、次の検索で再利用する。
    document.getElementById('results').classList.add('hidden');
    document.getElementById('empty').classList.remove('hidden');
    const emptyError=document.getElementById('emptyError');
    emptyError.textContent='該当するカードが見つかりませんでした';
    emptyError.classList.toggle('hidden',!!SearchStatus.message(d));
    _maybeOpenBuyInlineOnLoad(); // 0件でも次回の検索に持ち越さない
    return;
  }
  // F-5: soCountはこの時点では検索直後の全件基準。絞り込み確定後に_updateHeroSummary()で上書きする

  // Card hero image
  const searchTerm=document.getElementById('q').value.trim();
  const hero=document.getElementById('cardHero');
  const heroInfo=document.getElementById('heroInfo');
  hero.classList.remove('hidden');
  document.getElementById('heroImgWrap').innerHTML='<div class="cr-img-ph"></div>';
  const inStock=D.filter(r=>!r.sold_out);
  const best=inStock.length>0?inStock.reduce((a,b)=>a.price<b.price?a:b):null;
  // PC では効果テキストを最初から開き、名前の下の空きに出す（高さの上限と「続きを読む」は _clampCardDetail）。
  // スマホは今までどおり閉じたボタン（縦に長くなりすぎるため）
  const detailOpen=!!(window.matchMedia&&window.matchMedia('(min-width:641px)').matches);
  // 最安の行（#heroBest）は既定のレアリティが決まった後に _updateHeroSummary() が埋める。
  // 在庫の件数・レアリティの種類数の行は、すぐ下のレアリティ欄（すべて N件・札の数）と重なるので出さない（2026-10-22）
  heroInfo.innerHTML=`<div class="card-hero-name">${esc(searchTerm)}</div>
    <div class="cr-best" id="heroBest">${best?'':'<span class="cr-best-none">在庫なし</span>'}</div>
    <!-- スマホだけ: 効果テキストは名前と最安の下（画像の横の空き）のボタンから全画面で開く（2026-10-22 ユーザー指定） -->
    <button type="button" class="cr-fx-btn" id="crFxBtn" onclick="_openFxSheet()">効果テキスト</button>
    <div class="cr-acts">
      <button type="button" id="heroWishAdd" class="cr-pill cr-pill-pri" onclick="wishAddFromBtn(this,'${escAttr(escJs(searchTerm))}',1,rarityFilter)">購入候補に追加</button>
      <button type="button" class="cr-pill" onclick="addToDeck('${escAttr(escJs(searchTerm))}',this)">デッキに追加</button>
      <!-- 価格比較を先に見せ、効果テキストは必要なときに開く。
           閉じた見出しは最初から出しておき、カード情報が無かったときだけ _hideCardDetail で隠す。
           情報が届いてから出すと、下の一覧が約42px 下がり CLS 0.02 が出ていた（2026-10-21） -->
      <details class="card-info-details" id="cardDetailDetails"${detailOpen?' open data-auto-open="1"':''}>
        <summary class="cr-pill">効果テキスト</summary>
        <div id="cardDetail" class="card-detail"></div>
      </details>
      <div class="share-menu-wrap">
        <button type="button" class="cr-pill share-btn-x" onclick="toggleShareMenu('shareMenuSell')">シェア</button>
        <div class="share-menu" id="shareMenuSell"></div>
      </div>
    </div>`;
  // シェアメニュー構築
  const shareOpts=_buildShareOptions(searchTerm,d,D);
  const smEl=document.getElementById('shareMenuSell');
  if(smEl&&shareOpts.length>0){
    let sm='<div class="share-menu-label">シェアする内容を選択</div>';
    shareOpts.forEach((o,i)=>{
      const t=escAttr(escJs(o.text));
      sm+=`<div class="share-menu-label" style="padding-top:${i?8:4}px">${esc(o.label)}</div>`;
      sm+=`<button class="share-menu-item" onclick="_shareToX('${t}')">𝕏 でポスト</button>`;
      sm+=`<button class="share-menu-item" onclick="_shareToLine('${t}')">LINE で送る</button>`;
      sm+=`<button class="share-menu-item" onclick="_shareCopy('${t}',this)">テキストをコピー</button>`;
    });
    smEl.innerHTML=sm;
  }

  // 画像: YGOResources優先（カーナベルはサーバー側フォールバック）。検索の開始時に読み始めた結果があれば使う
  _cardImageData(searchTerm)
    .then(data=>{
      if(data.url&&safeUrl(data.url)){
        cardImgUrl=safeUrl(data.url);
        cardImgSmall=cardImgUrl;
        _applyCardImages(cardImgUrl,cardImgSmall,data.source);
      }
    })
    .catch(()=>{});
  // カード情報: YGOResources（ATK/DEF/効果テキスト等）、未発見時はYGOProDeckフォールバック
  _fetchCardInfo(searchTerm);

  // Build rarity filter buttons
  // C-1(iii): 既定で「全体最安の在庫あり行が属するレアリティ」に絞り込む。
  // レアリティが1種類以下／最安行にレアリティが無い・不明な場合は従来どおり絞り込みなし
  const rarities=[...new Set(D.map(r=>r.rarity).filter(Boolean))].sort();
  // F-3: "(不明)"は抽出失敗行の疑似系列（aggregations.py/notify.py/featured_matrix.pyと同じ扱い）。
  // 自動絞り込みの発動判定からは除外する（実質1レアリティのカードで"(不明)"行が黙って消えるのを防ぐ）
  const realRarities=rarities.filter(r=>r!=='(不明)');
  let defaultRarity='';
  if(realRarities.length>1&&best&&best.rarity&&best.rarity!=='(不明)'){
    defaultRarity=best.rarity;
  }else if(realRarities.length===1){
    // 実体のあるレアリティが1種類だけのカードも、そのレアリティを選んだ状態にする（2026-10-22 ユーザー指摘）。
    // レアリティ欄は出ない（選ぶものが無い）のに未指定のままだと、購入候補が「レアリティ未指定」になり、
    // 相場の動きが「レアリティを選ぶと…」と選べないものを求めていた
    defaultRarity=realRarities[0];
  }
  rarityFilter=defaultRarity;
  artFilter=_defaultArt(defaultRarity);
  tableShowCount=TABLE_PAGE_SIZE;
  listView='shop'; // 新しいカードは「店ごとの最安」から見せる
  _syncListViewButtons();
  sort={k:'price',a:true}; // 一覧の先頭がヒーローの最安と同じ行になるよう、並べ替えも価格順に戻す（F-14）
  const soCheck=document.getElementById('soCheck');
  if(soCheck) soCheck.checked=false; // 「すべて」でしか見えないチェックを前のカードから持ち越さない
  // G-4: 「変更していないのにchange」で集計が濁らないようイベント名を分ける（自動設定=rarity_filter_default）。
  // 発火はレアリティUIが実際に出るケース（realRarities.length>1）だけに限定し、
  // 単一レアリティのカードで毎回発火して分母が壊れるのを防ぐ。C-1(i)判断のため
  // rarity_count（"(不明)"含む）・real_rarity_count（実体のあるレアリティ数）を同梱する
  if(realRarities.length>1){
    _ga('rarity_filter_default',{rarity:defaultRarity||'__all__',rarity_count:rarities.length,real_rarity_count:realRarities.length});
  }
  const rfEl=document.getElementById('rarityFilter');
  // レアリティが1種類でも絵が2種類以上あれば、絵の切り替えだけを出す
  rfEl.innerHTML=(rarities.length>1
    ?'<div class="cr-sec"><h2>レアリティ</h2><span>安い順・在庫ありの最安</span></div>'+
      `<div class="cr-rtiles">${_rarityTilesHtml(rarities,defaultRarity)}</div>`
    :'')+'<div id="artFilter" class="cr-art hidden"></div>';
  const hasArtFilter=_renderArtFilter();
  rfEl.classList.toggle('hidden',!(rarities.length>1||hasArtFilter));
  _updateRarityBanner();
  _updateHeroSummary(); // F-5: 既定の絞り込み確定後にヒーローの最安値/売切件数を合わせる

  renderSummary();
  renderTable();
  // F-2: A8トラッキングピクセルは検索1回につき1回だけ（レアリティ切替では再発火させない）
  const pixelSlot=document.getElementById('affPixelSlot');
  if(pixelSlot) pixelSlot.innerHTML=affPixel('カーナベル','sell');

  // S-2: 買取セクションを開く合図がある場合はここで消費する（_currentCardNameが確定済みの
  // タイミング＝データ到着側から開く。固定タイマーでの先行オープンは行わない）
  _maybeOpenBuyInlineOnLoad();
}

// B-1: 店舗サマリー。d.by_shop（全レアリティ横断のサーバー最安）は使わず、
// D（全行）から在庫あり＋rarityFilterを反映して組み直し、価格昇順＋最安との差額で描画する。
// レアリティフィルタ切替のたびに呼び直せるよう独立関数化（setRarityFilter()から呼ぶ）
// F-14: サマリーカードの店舗リスト（店舗ごとの最安値をグルーピング→価格昇順ソート）を
// renderSummary()とヒーローの最安値表示の両方から参照する共通関数に切り出す。
// 別々の計算式（reduceの巡回順 vs byShopグルーピング+ソート）だったため、同額タイのとき
// ヒーローとサマリー先頭カードで採用される店舗が食い違うことがあった
function _summaryShopItems(){
  const filtered=_scopedRows().filter(r=>!r.sold_out);
  const byShop={};
  for(const r of filtered){
    if(!byShop[r.shop]||r.price<byShop[r.shop].price) byShop[r.shop]=r;
  }
  return Object.values(byShop).sort((a,b)=>a.price-b.price);
}

// 出品一覧の見出し（レアリティ名・件数）と、出す行が無いときの案内を描く。
// 行そのものは renderTable() が listView に合わせて描く（旧・店舗カード #sumCards は一覧に統合した）
function renderSummary(){
  const shopItems=_summaryShopItems();
  const shownRarity=_shownRarity();
  const showSO=!!document.getElementById('soCheck')?.checked;
  const scoped=_scopedRows();
  const inStockCount=scoped.filter(r=>!r.sold_out).length;
  const soldCount=scoped.length-inStockCount;
  const allCount=showSO?scoped.length:inStockCount;
  const title=document.getElementById('crListTitle');
  if(title) title.textContent=shownRarity?`${shownRarity}${artFilter?'・'+artFilter:''}の出品`:'すべてのレアリティの出品';
  const cs=document.getElementById('crCountShop');
  if(cs) cs.textContent=shopItems.length;
  const ca=document.getElementById('crCountAll');
  if(ca) ca.textContent=allCount;
  // 売り切れの表示切り替えは「すべて」のときだけ（店ごとの最安は在庫ありだけで比べる）
  document.getElementById('soToggle')?.classList.toggle('hidden',listView!=='all');
  // F-10: 選んだレアリティに在庫が無いとき、無言の空欄にせず明示する
  const note=document.getElementById('crListNote');
  if(note){
    let msg='';
    if(inStockCount===0){
      msg=artFilter?`この絵（${artFilter}）は在庫切れです`:(rarityFilter?'このレアリティは在庫切れです':'在庫のある出品はありません');
      if(soldCount>0) msg+=`（売切${soldCount}件）`+(listView==='shop'?'。「すべて」の「売り切れも表示」で見られます':(!showSO?'。「売り切れも表示」で見られます':''));
    }
    note.textContent=msg;
    note.classList.toggle('hidden',!msg||(listView==='all'&&showSO));
  }
  const suspendNote=document.getElementById('crSuspendNote');
  if(suspendNote){
    suspendNote.textContent=typeof SearchStatus!=='undefined'?SearchStatus.suspendedNote():'';
    suspendNote.hidden=!suspendNote.textContent;
  }
}

// ── C-2: 販売ページに買取価格を併置（#buyInline、第2弾B）──
// 取得方式は(b)遅延ライブ取得で確定（決定は docs/decisions.md）。開いたときに初めて
// /api/buyback を叩き、1カードにつき1回だけ取得する。S-1で買取タブ自体は廃止済み
let _buyInlineD=null;          // 取得済みの買取結果（未取得はnull）
let _buyInlineFetched=false;    // 取得完了フラグ（1カード1回のみ取得するためのガード）
let _buyInlineFetchInFlight=false;
let _buyInlineFailedShops=[];   // 部分失敗した店舗名（shop_error）
let _buyInlineES=null;         // J-1: 進行中のEventSource参照。resetで確実に閉じるために保持する
let _buyInlineGen=0;           // J-1: 世代トークン。close()後も配送されうる旧SSEのイベントを無効化する
let _buyInlineTimeoutId=null;  // J-1: タイムアウトタイマーの参照。resetで確実にclearTimeoutする
let _currentCardName='';       // J-2: 「入力欄の現在の文字列」ではなく「いま画面に出ている確定済みの販売結果のカード名」
// S-2: /buy/<カード名>直リンク・#buybackハッシュ復元・戻る進む・買取ランキングクリック等、
// 「そのカードの買取を見に来た」経路で立てるフラグ。検索結果が実際に届いた時点
// （renderAll→_maybeOpenBuyInlineOnLoad）で消費し、買取セクションを開く
let _pendingBuyInlineOpen=false;

function _maybeOpenBuyInlineOnLoad(){
  if(!_pendingBuyInlineOpen) return;
  _pendingBuyInlineOpen=false;
  const det=document.getElementById('buyInline');
  if(!det) return;
  // /buy/<カード名> で着地したとき: 買取を取りに行き、欄は閉じたまま見出しに最高額を出す
  // （2026-10-22 ユーザー決定「既定は折りたたみ」。以前はここで開いていた）。
  // 開いていないので buyback_inline_open は送らない（ページ着地で「買取を開いた人数」を水増ししない）
  if(!_buyInlineFetched&&!_buyInlineFetchInFlight) _fetchBuyInline('deeplink');
}

function _resetBuyInline(){
  const det=document.getElementById('buyInline');
  if(det) det.open=false;
  _buyInlineGen++; // J-1: 世代を進め、これ以前に張ったSSEの遅延イベント（close後に届くdone等）を無効化する
  if(_buyInlineES){ try{_buyInlineES.close();}catch(e){} _buyInlineES=null; }
  if(_buyInlineTimeoutId){ clearTimeout(_buyInlineTimeoutId); _buyInlineTimeoutId=null; }
  _buyInlineD=null;
  _buyInlineFetched=false;
  _buyInlineFetchInFlight=false;
  _buyInlineFailedShops=[];
  _currentCardName='';
  _mercariPending=false;
  if(window.MercariRef) MercariRef.clear(); // 前のカードのメルカリ枠を残さない
  if(window.PriceTrend) PriceTrend.clear(); // 前のカードの価格推移グラフを残さない
  const body=document.getElementById('buyInlineBody');
  if(body) body.innerHTML='';
  _setBuyInlineHead(null);
}

// 買取の欄の見出し。閉じていても最高額が分かるよう、取れたら「〇〇の買取価格　最高 ¥X 店名」にする
function _setBuyInlineHead(best){
  const head=document.getElementById('buyInlineHead');
  if(!head) return;
  if(!best){ head.textContent='買取価格を見る（いくらで売れるか）'; return; }
  const title=(best.rarity?best.rarity+'の':'')+'買取価格';
  head.innerHTML=`<span class="cr-buy-t">${esc(title)}</span>`+(best.price!=null
    ?`<span class="cr-buy-max">最高 <b>¥${best.price.toLocaleString()}</b> ${esc(best.shop)}</span>`
    :`<span class="cr-buy-max">${esc(best.note||'')}</span>`);
}

// #buyInline を開いた経路を GA で区別するためのフラグ。既定は'inline'（利用者が見出しを押した）。
// /buy/ 直リンクの自動オープンだけ 'deeplink' に差し替え、toggleリスナーが読み取ったら'inline'に戻す。
// （表の「買取価格も表示する」ボタン＝'table_button' は 2026-10-21 案C で廃止）
let _buyInlineOpenTrigger='inline';

document.getElementById('buyInline')?.addEventListener('toggle',function(){
  if(!this.open) return;
  _ga('buyback_inline_open',{trigger:_buyInlineOpenTrigger});
  // 1カード1回だけ取得。取得済み/取得中なら再取得しない（閉じて再度開いても再取得しない）
  if(!_buyInlineFetched&&!_buyInlineFetchInFlight) _fetchBuyInline(_buyInlineOpenTrigger);
  _buyInlineOpenTrigger='inline';
});

// メルカリ参考相場パネルを開いたときの計測（買取の buyback_inline_open と同じ方式）
document.getElementById('mercariRef')?.addEventListener('toggle',function(){
  if(!this.open) return;
  if(this.dataset.autoOpen){ delete this.dataset.autoOpen; return; } // mercari-ref.js が自動で開いた分は利用者の操作ではないので計測しない
  _ga('mercari_inline_open',{trigger:'inline'});
});
// メルカリ欄の価格リンク（タイル内の .scard-link）のクリック計測（本文は動的描画なのでイベント委譲。インラインonclickはCSPで使えない）
document.getElementById('mercariRefBody')?.addEventListener('click',function(e){
  const a=e.target&&e.target.closest?e.target.closest('a[href]'):null;
  if(!a||!this.contains(a)) return;
  _ga('mercari_link_click',{affiliate:(' '+(a.getAttribute('rel')||'')+' ').indexOf(' sponsored ')>=0});
});

function _fetchBuyInline(trigger,busyTries){
  // J-3: ガードは呼び出し元（トグルハンドラ・再試行ボタン）に依存せず、この関数の先頭で必ず効かせる
  if(_buyInlineFetched||_buyInlineFetchInFlight) return;
  // J-2: 検索欄の現在値ではなく、いま画面に確定表示されているカード名を使う
  // （検索せずに入力欄だけ書き換えた状態で開かれても、別カードを取りに行かないようにする）
  const name=_currentCardName;
  if(!name) return;
  const gen=++_buyInlineGen; // J-1: このfetch呼び出し専用の世代番号
  _buyInlineFetchInFlight=true;
  _buyInlineFailedShops=[];
  const body=document.getElementById('buyInlineBody');
  body.innerHTML='<div class="progress on" id="buyInlineProg"></div>';
  const prog=document.getElementById('buyInlineProg');
  // G-2: search_submitに合算計上（カード名は送らない）。U-1: 再試行ボタン経由（引数無し）は
  // 従来通り'inline'扱いのまま（既存の意味を壊さない）。混雑で取り直す分（busyTries>0）は数えない
  busyTries=busyTries||0;
  if(!busyTries) _ga('search_submit',{mode:'buyback',trigger:trigger||'inline'});
  // confirmed=true: 既にsell側の検索で存在確認済みのカード名のため/api/validateを再度挟まない
  const es=new EventSource('/api/buyback?q='+encodeURIComponent(name)+'&confirmed=true');
  _buyInlineES=es;
  function step(shop){
    let el=document.getElementById('bi-'+shop);
    if(!el){
      el=document.createElement('div');
      el.className='pstep';
      el.id='bi-'+shop;
      const pname=document.createElement('span');
      pname.className='pname';pname.textContent=shop;
      const pstat=document.createElement('span');
      pstat.className='pstat';pstat.textContent='待機中';
      el.appendChild(pname);el.appendChild(pstat);
      prog.appendChild(el);
    }
    return el;
  }
  function fail(){
    if(gen!==_buyInlineGen) return; // J-1: resetや別カードのfetchで世代が進んでいたら無視
    es.close();_buyInlineFetchInFlight=false;_buyInlineES=null;_buyInlineTimeoutId=null;
    _renderBuyInlineError('検索がタイムアウトしました。');
  }
  _buyInlineTimeoutId=setTimeout(fail,60000);
  es.onmessage=e=>{
    if(gen!==_buyInlineGen) return; // J-1: closeしても配送されうる旧世代のイベントを無効化する
    clearTimeout(_buyInlineTimeoutId);_buyInlineTimeoutId=setTimeout(fail,60000);
    const d=JSON.parse(e.data);
    if(d.type==='busy'){
      // 店舗検索の混雑（販売の検索と同じ）。失敗にせず「順番待ち」を出し、数秒後に取り直す
      // 待っている間も _buyInlineFetchInFlight は true のまま（欄を開き直しても二重に取りに行かない）
      clearTimeout(_buyInlineTimeoutId);_buyInlineTimeoutId=null;es.close();_buyInlineES=null;
      if(busyTries+1>=ShopBusy.MAX_TRIES){ _buyInlineFetchInFlight=false; _renderBuyInlineError('店舗検索が混雑しています。'); return; }
      prog.textContent=ShopBusy.WAIT_TEXT;
      _buyInlineTimeoutId=setTimeout(()=>{
        if(gen!==_buyInlineGen) return; // 別のカードに移った（_resetBuyInline が目印も戻している）
        _buyInlineFetchInFlight=false; _fetchBuyInline(trigger,busyTries+1);
      },ShopBusy.retryMs(d.retry_after));
      return;
    }
    if(d.type==='progress'){
      const el=step(d.shop);el.className='pstep searching';el.querySelector('.pstat').textContent='検索中...';
    }else if(d.type==='waiting'){
      prog.querySelectorAll('.pstep.searching').forEach(el=>{
        el.querySelector('.pstat').textContent=(d.queued_shops||[]).includes(el.id.slice(3))?'順番待ち':'検索中...';
      });
    }else if(d.type==='shop_done'){
      const el=step(d.shop);
      if(d.status==='busy'){ el.className='pstep error';el.querySelector('.pstat').textContent=d.count?'混雑・前回の'+d.count+'件':'混雑で取得できず'; }
      else{ el.className=d.status==='partial'?'pstep error':'pstep done';el.querySelector('.pstat').textContent=d.status==='partial'?(d.count?d.count+'件・一部失敗':'取得失敗'):d.count+'件'+(d.cached?' (cache)':''); }
    }else if(d.type==='shop_error'){
      const el=step(d.shop);el.className='pstep error';el.querySelector('.pstat').textContent=d.busy?'混雑':'エラー';
      _buyInlineFailedShops.push(d.shop);
    }else if(d.type==='done'){
      clearTimeout(_buyInlineTimeoutId);_buyInlineTimeoutId=null;es.close();_buyInlineFetchInFlight=false;_buyInlineES=null;
      _buyInlineD=d.results;
      _buyInlineFailedShops=d.failed_shops||_buyInlineFailedShops;
      if(_buyInlineD.length===0&&_buyInlineFailedShops.length>0){
        // 全店舗失敗＝通信エラー相当。0件確定ではないので「見つかりませんでした」にはしない
        _renderBuyInlineError('買取価格の取得に失敗しました（'+_buyInlineFailedShops.join('・')+'）。');
        return;
      }
      _buyInlineFetched=true;
      _applyBuyMaxToD(); // U-1: 販売テーブルの「この型番の最高買取」列を確定させる
      renderTable();      // 列を出すため販売テーブルも再描画する（買取セクション自体はこの下で描画）
      _renderBuyInline();
    }
  };
  es.onerror=()=>{
    if(gen!==_buyInlineGen) return; // J-1
    clearTimeout(_buyInlineTimeoutId);_buyInlineTimeoutId=null;es.close();_buyInlineFetchInFlight=false;_buyInlineES=null;
    _renderBuyInlineError('通信エラーが発生しました。');
  };
}

// J-6: エスケープはここ1箇所だけに統一する。呼び出し側で事前にesc()しないこと
// （二重エスケープで&amp;等が画面にそのまま出るのを防ぐ／次の実装者がここを外の唯一の正とする）
function _renderBuyInlineError(msg){
  const body=document.getElementById('buyInlineBody');
  if(!body) return;
  body.innerHTML=`<div class="empty" style="padding:16px 0"><p>${esc(msg)}</p></div>`+
    `<button class="tbl-more-btn" onclick="_fetchBuyInline()">再試行</button>`;
}

// _buyInlineDは全レアリティ分。表示は販売側と同じレアリティに絞る（成立しない比較を防ぐ、C-2要件）
function _buyInlineFilteredData(){
  if(!_buyInlineD) return [];
  return rarityFilter?_buyInlineD.filter(r=>r.rarity===rarityFilter):_buyInlineD;
}

function _buyInlineNotes(){
  const failed=_buyInlineFailedShops.length
    ?`<p class="buy-inline-note">${_buyInlineFailedShops.map(esc).join('・')} は取得に失敗しました。</p>`:'';
  // 販売6店に対し買取5店（まんぞく屋は買取価格をネット非公開）。黙って1店消えると不審に見えるため明示する
  const manzoku='<p class="buy-inline-note">まんぞく屋は買取価格を公開していないため対象外です。</p>';
  const suspendedMsg=typeof SearchStatus!=='undefined'?SearchStatus.suspendedNote(['まんぞく屋','駿河屋']):''; // 買取に元から無い店は書かない
  const suspended=suspendedMsg?`<p class="buy-inline-note">${esc(suspendedMsg)}</p>`:'';
  return failed+manzoku+suspended;
}

// S-1: 「買取価格の詳細を見る」ボタン（旧・買取タブへの遷移）は廃止。
// 全レアリティの買取価格は、既存の「すべて」レアリティフィルタボタン（setRarityFilter('')）を
// 押すと#buyInlineが同じデータで絞り込み解除表示するため、別画面への導線は不要だった

// L-1: 店舗によって `code` の実体が「型番」ではないことが本番データで判明した
// （例: カーナベルは弾の略号のみ＝`TTP1`、まれに壊れた値＝`SDモ さ`。トレコロCBは
// `LPST-JP007PSE` のように余分な接尾辞や `23434538` のような内部ID的な数値が混じる）。
// K-1で「型番が一致する行だけ突き合わせる」ようにしたが、型番でない値同士が一致すると
// 別カードを同一視してしまい問題が別経路で復活するため、表示・突き合わせの両方で
// 「型番らしい形」かを共通の1関数で判定する。
// 通す例: LPST-JP007 / SD47-JP016 / 20TH-JPC82 / TT01-JPA10 / EXP4-JP037 / QCDB-JP015 / RC04-JP005
//   → 「英数2-6文字」+ハイフン+「英字2文字（言語コード。JP等）」+「任意で英字1文字（レアリティ等の枝番）」+「数字2-4桁」
// 弾く例: TTP1（ハイフンなしの略号）/ SDモ さ（日本語・空白混入）/ 23434538（数値のみ）/
//         LPST-JP007PSE・20TH-JPC8220SE（数字の後に余分な英字が付く接尾辞つき）
// トレコロCBの接尾辞を剥がす正規化はしない（未確認の接尾辞一覧を推測で剥がすと別カードを同一視する
// リスクがあるため）。迷ったら弾く側に倒す。根本対応（接尾辞の仕様確認）はTASKS.mdに別途起票する
function _looksLikeCardCode(code){
  if(!code) return false;
  return /^[A-Za-z0-9]{2,6}-[A-Z]{2}[A-Za-z]?\d{2,4}$/.test(String(code).trim());
}

// U-1: 販売テーブルの「この型番の最高買取」列。突合は同じ(code,rarity)の買取行のうち最高額で、
// codeは_looksLikeCardCode()を通るものだけ使う（弾の略号・壊れた値を型番として同一視しない）。
// この関数自体は取得を一切行わない。_buyInlineD（#buyInline取得結果）が揃ってから呼ぶこと
function _applyBuyMaxToD(){
  if(!_buyInlineD){
    for(const r of D){ r.buyMax=null; r.buyMaxShop=null; }
    return;
  }
  const validBuys=_buyInlineD.filter(b=>_looksLikeCardCode(b.code));
  for(const r of D){
    if(!_looksLikeCardCode(r.code)){ r.buyMax=null; r.buyMaxShop=null; continue; }
    const matches=validBuys.filter(b=>b.code===r.code&&b.rarity===r.rarity);
    if(!matches.length){ r.buyMax=null; r.buyMaxShop=null; continue; }
    const best=matches.reduce((a,b)=>b.price>a.price?b:a);
    r.buyMax=best.price;
    r.buyMaxShop=best.shop;
  }
}

function _renderBuyInline(){
  if(!_buyInlineFetched) return; // 未取得なら何もしない（再取得のトリガーにはしない）
  const body=document.getElementById('buyInlineBody');
  if(!body) return;
  if(!_buyInlineD.length){
    body.innerHTML=`<div class="empty" style="padding:16px 0"><p>このカードの買取価格は見つかりませんでした</p></div>${_buyInlineNotes()}`;
    _setBuyInlineHead({rarity:'',price:null,note:'見つかりませんでした'});
    return;
  }
  const filtered=_buyInlineFilteredData();
  if(!filtered.length){
    body.innerHTML=`<div class="empty" style="padding:16px 0"><p>このレアリティの買取価格は見つかりませんでした</p></div>${_buyInlineNotes()}`;
    _setBuyInlineHead({rarity:rarityFilter,price:null,note:'見つかりませんでした'});
    return;
  }

  // H-1: rarityFilterが空（=「すべて」表示）のときは、買取側の店舗間の差額・BESTバッジが
  // 別レアリティ同士になりうる（例: 買取¥72,000は20thシークレット、別行はウルトラ）。
  // 成立しない比較を出すと実害があるため、差額表示は出さない（F-9と同じ規律を買取側にも適用）
  const rarityActive=!!rarityFilter;

  // M-1: 「要点1行」（販売最安×同型番の買取最高の突き合わせ文）はユーザー判断で廃止した。
  // 理由は docs/decisions.md 参照（買値より買取が安いのは自明で情報として空／貼り紙で
  // 誤読を塞ぐ形の表現だった）。案内文も同時に不要になったため削除する
  // M-1: 注意書きは買取価格側の条件（状態・枚数）に絞って短く残す（販売側への言及は不要になった）
  const conditionNote='<p class="buy-inline-note">買取価格は店舗ごとの状態・枚数の条件により変わります</p>';

  // (b) 店舗別カード: 買取額の高い順。M-2: 差額（買取は高い方が良いため安い側がマイナス表記）は
  // 「先頭行（最高額）と同じ型番」の行にだけ出す（型番が違えば別物のため、別型番同士の
  // 引き算を表示しない）。先頭行の型番が「型番らしい形」を通らないときは、どの行にも出さない
  // X-1: BESTバッジは「先頭行＝最高額」の印であって型番の一致は無関係（型番一致は差額表示のみの条件）
  const byShop={};
  for(const r of filtered){ if(!byShop[r.shop]||r.price>byShop[r.shop].price) byShop[r.shop]=r; }
  const sortedShops=Object.values(byShop).sort((a,b)=>b.price-a.price);
  const maxPrice=sortedShops[0].price;
  const topCode=_looksLikeCardCode(sortedShops[0].code)?sortedShops[0].code:null;
  let cardsHtml='';
  sortedShops.forEach((item,i)=>{
    const sameCode=!!topCode&&item.code===topCode;
    const isBest=rarityActive&&i===0;
    // アフィリエイト: リンクはaffLink()を使うが、トラッキングピクセルaffPixel()はここでは発火させない
    // （レアリティ切替のたびにこの関数が再実行されるため。第1弾F-2で潰した重複発火の再発防止）
    const itemUrl=affLink(item.url,item.shop,'buy');
    const isAff=!!item.url&&_isAffUrl(item.url,itemUrl);
    const priceLink=item.url?`<a class="scard-link" href="${escAttr(itemUrl)}" target="_blank" rel="${_affRel(isAff)}">¥${item.price.toLocaleString()}</a>`:`¥${item.price.toLocaleString()}`;
    const bestBadge=isBest?'<span class="sbest-badge">最高額</span>':'';
    let diff='';
    if(!isBest&&rarityActive&&sameCode){
      const d=maxPrice-item.price;
      diff=d===0?'同額':`−¥${d.toLocaleString()}`;
    }
    // J-4: 買取側もconditionがあれば出す（例: カードラッシュの「強化買取中」＝期間限定の上乗せ価格を
    // 無条件価格のように見せない）
    const condStr=(item.condition&&item.condition!=='-')?`・${esc(item.condition)}`:'';
    // K-1/L-1: 型番（code）が「型番らしい形」を通るときだけ表示する（同一レアリティ内の価格差の
    // 理由を画面から分かるようにしつつ、弾の略号や壊れた値をそのまま型番として見せない）
    const codeStr=_looksLikeCardCode(item.code)?`・${esc(item.code)}`:'';
    // カード名は同じ名前をくり返さず、イラスト違いなど名前が違うときだけ出す（一覧の札と同じ）
    const variant=_variantLabel(item.name,_currentCardName);
    cardsHtml+=`<div class="scard${isBest?' best':''} fin"><div class="slabel">${esc(item.shop)}${isAff?AFF_PR_BADGE:''}</div>
      <div class="sprice" style="color:var(--green)">${priceLink}${bestBadge}<span class="sdiff">${diff}</span></div>
      <div class="sdetail">${esc(item.rarity||'-')}${condStr}${codeStr}${variant?'・'+esc(variant):''}</div></div>`;
  });

  body.innerHTML=`${conditionNote}<div class="summary">${cardsHtml}</div>${_buyInlineNotes()}`;
  // H-1 と同じ理由で、「すべて」のときは最高額と言い切らず見出しに出さない
  _setBuyInlineHead(rarityActive?{rarity:rarityFilter,price:sortedShops[0].price,shop:sortedShops[0].shop}:{rarity:'',price:null,note:'レアリティを選ぶと最高額を表示します'});
}

// F-5/F-14: ヒーローの最安値表示・売切件数バッジをrarityFilterに合わせて再計算する
// （setRarityFilter()とrenderAll()の両方から呼ぶ。D全体基準のままだと絞り込み後も
// 表示が追従せず、画面の上と下で数字が矛盾していた）。
// 最安値は_summaryShopItems()（renderSummary()と同じ算出）の先頭要素を使い、同額タイのときに
// ヒーローとサマリー先頭カードの店舗名が食い違わないようにする
function _updateHeroSummary(){
  const heroBest=document.getElementById('heroBest');
  const soCountEl=document.getElementById('soCount');
  const items=_summaryShopItems();
  const best=items.length?items[0]:null;
  const shownRarity=_shownRarity();
  if(heroBest){
    // 最安の行: ［最安］の札（一覧の最安の印と同じ金）・値段・レアリティの枠線の札・店名と型番（2026-10-22 ユーザー選定）。
    // 「すべて」のときは全レアリティの中の最安なので、その出品のレアリティを札に出す（別レアリティだと分かるように J-5）
    const rar=(shownRarity||(best?best.rarity:''))+(shownRarity&&artFilter?'・'+artFilter:'');
    const rarTag=rar?`<span class="cr-hb-rar">${esc(rar)}</span>`:'';
    if(best){
      const code=_looksLikeCardCode(best.code)?`・${esc(best.code)}`:'';
      heroBest.innerHTML=`<span class="cr-hb-best">最安</span><span class="cr-best-price">¥${best.price.toLocaleString()}</span>${rarTag}`+
        `<span class="cr-best-shop">${esc(best.shop)}${code}</span>`;
    }else{
      heroBest.innerHTML=`<span class="cr-best-none">在庫なし</span>${rarTag}`;
    }
  }
  // 売切件数はサーバー集計値（d.sold_out_count）とは別経路でクライアント側のD（rarityFilter適用後）
  // から再集計している。サーバー側の集計ロジックと将来ズレる可能性があるため要注意
  if(soCountEl){
    soCountEl.textContent=_scopedRows().filter(r=>r.sold_out).length;
  }
}

// 購入候補に追加ボタンの文言を選択中のレアリティに合わせる。
// 旧・「絞り込み中」の帯（#rfBanner）は、レアリティ欄の選択の印と一覧の見出しで伝えるようにしたので廃止
function _updateRarityBanner(){
  const wishButton=document.getElementById('heroWishAdd');
  if(wishButton)wishButton.textContent=rarityFilter?`${rarityFilter}を購入候補へ`:'レアリティ未指定で購入候補へ';
}

// レアリティの選択欄。各レアリティの在庫ありの最安を出し、安い順に並べる（押す前に値段の差が分かる）。
// 在庫が無いレアリティは後ろに名前順。先頭は「すべて」
function _rarityTilesHtml(rarities,active){
  const inStock=D.filter(r=>!r.sold_out);
  const minOf=list=>list.length?list.reduce((m,r)=>r.price<m?r.price:m,Infinity):null;
  const tiles=rarities.map(r=>{
    // 絵の切り替えが出るレアリティは、選んだときに最初に見せる絵（元のイラスト）で数える（上の最安と値段をそろえる）
    const art=_defaultArt(r);
    const rows=inStock.filter(x=>x.rarity===r&&(!art||x.art===art));
    return {r:r,min:minOf(rows),n:rows.length,sold:D.filter(x=>x.rarity===r&&x.sold_out&&(!art||x.art===art)).length};
  });
  tiles.sort((a,b)=>((a.min==null)-(b.min==null))||((a.min||0)-(b.min||0))||a.r.localeCompare(b.r,'ja'));
  const tile=(r,label,price,count,on)=>`<button type="button" class="cr-rtile${on?' active':''}" data-r="${escAttr(r)}" aria-pressed="${on}" onclick="setRarityFilter('${escAttr(escJs(r))}')"><span class="cr-rn">${esc(label)}</span><span class="cr-rp"><b>${price}</b><i>${count}</i></span></button>`;
  const allMin=minOf(inStock);
  let h=tile('','すべて',allMin!=null?`¥${allMin.toLocaleString()}〜`:'在庫なし',`${inStock.length}件`,active==='');
  for(const t of tiles){
    h+=tile(t.r,t.r,t.min!=null?`¥${t.min.toLocaleString()}`:'在庫なし',t.min!=null?`${t.n}件`:`売切${t.sold}件`,t.r===active);
  }
  return h;
}

// 画面に出すレアリティ名。選んでいるレアリティ、または出品が1種類のレアリティしか無いカードのその名前。
// 複数のレアリティが混ざる「すべて」のときは空（別レアリティ同士で最安と言い切らない J-5）
function _shownRarity(){
  if(rarityFilter) return rarityFilter;
  const kinds=[...new Set(D.map(r=>r.rarity||''))];
  return kinds.length===1&&kinds[0]&&kinds[0]!=='(不明)'?kinds[0]:'';
}

// ── イラストの切り替え（2026-10-28）──
// 同じレアリティに絵が2種類以上ある（例: 灰流うらら シークレットの元の絵 500円／イラスト違い 980円）とき、
// レアリティ欄の下に絵の切り替えを出し、最安・一覧・件数をその絵で比べる。r.art は検索の API（art_variant.py）が
// 付ける印で、判別できない行は空（「すべての絵」にだけ出す）
// 言語版（アジア版など）も絵の切り替えの選択肢として後ろに並ぶ（レアリティ名は日本版と同じ）
const ART_ORDER=['元のイラスト','イラスト違い','漫画カラー','漫画モノクロ','GREEN Ver.','PINK Ver.'];
function _artsOf(rarity){
  const kinds=new Set(D.filter(r=>r.rarity===rarity&&r.art).map(r=>r.art));
  return ART_ORDER.filter(a=>kinds.has(a)).concat([...kinds].filter(a=>!ART_ORDER.includes(a)));
}
// 既定は元のイラスト（多くの人が探す絵）。絵の違いが無い・元の絵に在庫が無いときは絞らない
function _defaultArt(rarity){
  const arts=rarity?_artsOf(rarity):[];
  if(arts.length<2||!arts.includes('元のイラスト')) return '';
  return D.some(r=>r.rarity===rarity&&r.art==='元のイラスト'&&!r.sold_out)?'元のイラスト':'';
}
// 選んでいるレアリティと絵で絞った行（売り切れも含む）
function _scopedRows(){
  let rows=rarityFilter?D.filter(r=>r.rarity===rarityFilter):D;
  if(artFilter) rows=rows.filter(r=>r.art===artFilter);
  return rows;
}
// 絵の切り替え欄を描く。出すものが無ければ隠して false
function _renderArtFilter(){
  const el=document.getElementById('artFilter');
  if(!el) return false;
  const arts=rarityFilter?_artsOf(rarityFilter):[];
  if(arts.length<2){
    if(artFilter) artFilter='';
    el.innerHTML='';
    el.classList.add('hidden');
    return false;
  }
  const rows=D.filter(r=>r.rarity===rarityFilter);
  const tile=(a,label)=>{
    const mine=a?rows.filter(r=>r.art===a):rows;
    const inStock=mine.filter(r=>!r.sold_out);
    const min=inStock.length?inStock.reduce((m,r)=>r.price<m?r.price:m,Infinity):null;
    const on=artFilter===a;
    return `<button type="button" class="cr-rtile${on?' active':''}" data-art="${escAttr(a)}" aria-pressed="${on}" onclick="setArtFilter('${escAttr(escJs(a))}')"><span class="cr-rn">${esc(label)}</span><span class="cr-rp"><b>${min!=null?'¥'+min.toLocaleString():'在庫なし'}</b><i>${min!=null?inStock.length+'件':'売切'+mine.length+'件'}</i></span></button>`;
  };
  el.innerHTML=`<div class="cr-sec"><h2>イラスト</h2><span>${esc(rarityFilter)}の中の絵の違い</span></div>`+
    `<div class="cr-rtiles">${tile('','すべての絵')}${arts.map(a=>tile(a,a)).join('')}</div>`;
  el.classList.remove('hidden');
  return true;
}
function setArtFilter(a){
  _ga('art_filter_change',{art:a||'__all__',from_art:artFilter||'__all__',rarity:rarityFilter||'__all__'});
  artFilter=a;
  tableShowCount=TABLE_PAGE_SIZE;
  _renderArtFilter();
  _updateHeroSummary();
  renderSummary();
  renderTable();
}

// 店舗の出品名がカード名と違うとき（「灰流うらら(既存イラスト)」など）だけ、違う部分を札にする
function _variantLabel(name,cardName){
  if(!name||!cardName||name===cardName) return '';
  if(name.startsWith(cardName)){
    return name.slice(cardName.length).trim().replace(/^[（(]/,'').replace(/[)）]$/,'').trim();
  }
  return name;
}

function renderTable(){
  const showSO=document.getElementById('soCheck').checked;
  let data;
  if(listView==='shop'){
    data=_summaryShopItems(); // 各店の在庫ありの一番安い出品（ヒーローの最安と同じ算出 F-14）
  }else{
    data=_scopedRows();
    if(!showSO) data=data.filter(r=>!r.sold_out);
  }
  // J-5: 「最安」の印は同じレアリティ同士のときだけ（別レアリティ同士で最安と断言しない）
  const shownRarity=_shownRarity();
  const bestRow=shownRarity?(_summaryShopItems()[0]||null):null;
  const cardName=_currentCardName||document.getElementById('q').value.trim();
  const sorted=[...data].sort((a,b)=>{
    // U-1: 「この型番の最高買取」は値の無い行（buyMax=null）を、昇順・降順どちらでも
    // 必ず末尾に送る（通常の型比較だと空文字と数値が混ざって順序が安定しない）
    if(sort.k==='buyMax'){
      const va=a.buyMax,vb=b.buyMax;
      if(va==null&&vb==null)return 0;
      if(va==null)return 1;
      if(vb==null)return -1;
      return sort.a?va-vb:vb-va;
    }
    let va=a[sort.k]??'',vb=b[sort.k]??'';
    if(typeof va==='number'&&typeof vb==='number')return sort.a?va-vb:vb-va;
    return sort.a?String(va).localeCompare(String(vb),'ja'):String(vb).localeCompare(String(va),'ja');
  });
  // B-4: 既定20行+もっと見る（無限スクロールにはしない）
  const total=sorted.length;
  const visible=sorted.slice(0,tableShowCount);
  const tbody=document.getElementById('tbody');
  tbody.innerHTML=visible.map(r=>{
    const isBest=r===bestRow;
    const so=r.sold_out?'sold-out':'';
    const stockStr=r.sold_out?'<span class="sob">売切</span>':(r.stock>0?r.stock:'');
    const linkUrl=affLink(r.url, r.shop, 'sell');
    const isAff=!!r.url&&_isAffUrl(r.url,linkUrl);
    const go=r.url
      ?`<a class="row-link cr-go" href="${escAttr(linkUrl)}" target="_blank" rel="${_affRel(isAff)}">店で見る</a>`
      :'';
    // J-4: 状態が「-」以外（状態C・中古キズあり・セールなど）だけ出す。セールは目印の色
    const cond=r.condition==='セール'?'<span class="cr-sale">セール</span>':(r.condition&&r.condition!=='-'?esc(r.condition):'');
    const variant=_variantLabel(r.name,cardName);
    const tags=(isBest?'<span class="cr-best-tag">最安</span>':'')+
      (shownRarity?'':`<span class="cr-tag">${esc(r.rarity||'-')}</span>`)+
      (variant?`<span class="cr-tag">${esc(variant)}</span>`:'');
    // K-1/L-1: 型番は「型番らしい形」の値だけ出す（カーナベルの弾の略号・壊れた値を型番として見せない）
    const code=_looksLikeCardCode(r.code)?esc(r.code):'';
    // スマホでは状態・型番・在庫の列を隠し、店名の下の1行（.cr-sub）にまとめて出す
    const sub=[code,cond,r.sold_out?'':(r.stock>0?'在庫'+r.stock:'')].filter(Boolean).join('・');
    const rowClass=[so,isBest?'cr-best-row':'',linkUrl?'row-clickable':''].filter(Boolean).join(' ');
    const rowHref=linkUrl?`data-href="${escAttr(linkUrl)}"`:'';
    // U-1: 買取が未取得のうちは列自体を出さない（thのhidden切り替えと対にする）。
    // X-2: 値は金額のみ（店名は出さない。どの店が最高額かは買取の欄で分かる）
    const buyMaxCell=_buyInlineFetched
      ?`<td class="cr-num cr-buymax" data-label="最高買取">${r.buyMax!=null?'¥'+r.buyMax.toLocaleString():''}</td>`
      :'';
    return`<tr class="${rowClass}" ${rowHref}>
      <td class="cr-num cr-price">¥${r.price.toLocaleString()}</td>
      <td class="cr-shop"><span class="cr-shop-name">${esc(r.shop)}${isAff?AFF_PR_BADGE:''}</span>${tags}${sub?`<span class="cr-sub">${sub}</span>`:''}</td>
      <td class="cr-cond">${cond}</td>
      <td class="cr-code">${code}</td>
      <td class="cr-num cr-stock">${stockStr}</td>${buyMaxCell}
      <td class="cr-act">${go}${wishBtnHtml(r.name,1)}</td></tr>`;
  }).join('');

  // U-1: 買取が未取得の間はヘッダー列自体を隠す（thのhiddenをtd側の出し分けと揃える）
  const thBuyMax=document.getElementById('thBuyMax');
  if(thBuyMax) thBuyMax.classList.toggle('hidden',!_buyInlineFetched);

  document.querySelectorAll('#tbl th').forEach(th=>{
    const arr=th.querySelector('.arr');
    if(arr) arr.textContent=th.dataset.s===sort.k?(sort.a?'▲':'▼'):'';
  });

  // もっと見る（残り件数を明示。無限スクロールは採用しない）。店ごとの最安は店の数しか無いので出さない
  const moreEl=document.getElementById('tblMore');
  if(moreEl){
    if(total===0||listView==='shop'){
      moreEl.innerHTML='';
    }else if(total>visible.length){
      const remain=total-visible.length;
      moreEl.innerHTML=`<div class="tbl-more-info">${total}件中 ${visible.length}件を表示</div><button class="tbl-more-btn" onclick="_showMoreRows()">もっと見る（残り${remain}件）</button>`;
    }else{
      moreEl.innerHTML=`<div class="tbl-more-info">${total}件中 ${visible.length}件を表示</div>`;
    }
  }
  // G-7: フィルタロジックの二重管理をやめ、_showMoreRows()がGA計測に使えるよう件数を返す
  return {total, visibleCount: visible.length};
}

// B-4: 「もっと見る」でTABLE_PAGE_SIZE行ずつ追加表示
function _showMoreRows(){
  tableShowCount+=TABLE_PAGE_SIZE;
  const {total, visibleCount}=renderTable();
  _ga('show_more_rows',{visible_count:visibleCount,total_count:total});
}

// 売り切れ表示トグル: 表示件数をリセットしてから再描画
function _resetTableAndRender(){
  tableShowCount=TABLE_PAGE_SIZE;
  renderSummary(); // 見出しの件数（すべて N）を売り切れの表示に合わせる
  renderTable();
}

// 出品一覧の「店ごとの最安」⇔「すべて」
function setListView(v){
  if((v!=='shop'&&v!=='all')||listView===v) return;
  listView=v;
  tableShowCount=TABLE_PAGE_SIZE;
  _ga('list_view_change',{view:v});
  _syncListViewButtons();
  renderSummary();
  renderTable();
}
function _syncListViewButtons(){
  document.querySelectorAll('.cr-seg-btn').forEach(b=>{
    const on=b.dataset.v===listView;
    b.classList.toggle('active',on);
    b.setAttribute('aria-pressed',String(on));
  });
}

// 検索結果テーブルの行クリックで商品ページへ（a・button クリックは除外）
// renderTable() の外で一度だけ登録し、再描画ごとに重複追加されないようにする
document.getElementById('tbl')?.addEventListener('click',e=>{
  if(e.target.closest('a,button')) return;
  const tr=e.target.closest('tr.row-clickable');
  if(tr?.dataset.href) window.open(tr.dataset.href,'_blank','noopener');
},{capture:false});

document.querySelectorAll('#tbl th[data-s]').forEach(th=>{
  th.addEventListener('click',()=>{
    const k=th.dataset.s;
    if(sort.k===k)sort.a=!sort.a;
    else sort={k,a:k==='price'||k==='stock'};
    tableShowCount=TABLE_PAGE_SIZE; // B-4: ソート変更のたびに表示件数をリセット
    renderTable();
  });
});

function setRarityFilter(r){
  // G-4: from_rarityで「自動絞り込みが的外れで、ユーザーがすぐ別のレアリティ／すべてに戻した」を追跡できるようにする
  _ga('rarity_filter_change',{rarity:r||'__all__',from_rarity:rarityFilter||'__all__'});
  rarityFilter=r;
  artFilter=_defaultArt(r);
  tableShowCount=TABLE_PAGE_SIZE; // B-4: レアリティ切替のたびに表示件数をリセット
  document.querySelectorAll('.cr-rtile').forEach(b=>{
    const on=b.dataset.r===r;
    b.classList.toggle('active',on);
    b.setAttribute('aria-pressed',String(on));
  });
  _renderArtFilter();
  _updateRarityBanner();
  _updateHeroSummary(); // F-5: ヒーローの最安値・売切件数を絞り込みに追従させる
  renderSummary();
  renderTable();
  _renderBuyInline(); // C-2: 買取セクションも同じレアリティに絞り込んで再描画する（未取得なら何もしない・再取得はしない）
  _syncMercariView(); // メルカリ参考相場も同じレアリティで描き直す（再取得はしない）
  _syncPriceTrendView(); // 価格推移グラフも同じレアリティで描き直す（再取得はしない）
}

// メルカリ参考相場へ「選択中のレアリティ」と「店舗結果（売り切れ含む）に存在するレアリティ」を渡す。
// 店舗に無いレアリティ（他言語版・誤表記の混入）はメルカリ側で表示対象から外す。店舗結果が無いときは絞らない
function _syncMercariView(){
  if(!window.MercariRef) return;
  const stores=[...new Set(D.map(r=>r.rarity).filter(r=>r&&r!=='(不明)'))];
  MercariRef.setView({rarity:rarityFilter,stores:stores,pending:_mercariPending});
}

// 価格推移グラフへ「選択中のレアリティ」を渡す（再取得せず描き直す）
function _syncPriceTrendView(){
  if(!window.PriceTrend) return;
  PriceTrend.setView({rarity:rarityFilter});
}

function esc(s){const d=document.createElement('div');d.textContent=s||'';return d.innerHTML}
// 2026-08-20: \n \r のエスケープを追加（属性値中に生の改行/復帰が入ると、onclick等の
// HTML属性内に埋め込んだJS文字列リテラルが途中で改行され SyntaxError で丸ごと死ぬため。
// 既存の変換に追記するだけなので安全側の変更＝挙動が壊れる方向にはならない）。
function escJs(s){return(s+'').replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/\n/g,'\\n').replace(/\r/g,'\\r').replace(/</g,'\\x3c')}
// escAttr: HTML属性値（href/src/id/data-*等の "..." で囲まれる文脈）用のエスケープ。
// esc()はtextContent経由のためテキストノードは安全だが & < > のみでダブルクォート(")・
// シングルクォート(')を通すため、属性値にそのまま使うと属性の外へ脱出できてしまう
// （2026-08-20 XSS対策）。onclick属性の中でJS文字列（例: 関数呼び出しの引数に
// escJs(x) を渡す形）を組み立てる二重文脈では、escJs(x)の結果をさらにescAttr()で
// 包む（JS文字列としての安全性はescJsが、属性値としての安全性はescAttrが担当する）。
function escAttr(s){
  return (s+'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
// raritySlug: 正規名 → CSS スラグ（rarity.py の slug に一致、ASCII のみ）
function raritySlug(s){
  const cfg=(window.RARITY_CONFIG||{})[s];
  return cfg?cfg.slug:String(s||'').replace(/[^a-zA-Z0-9_-]/g,'_');
}
// cssClass は後方互換のために残す（rarity以外の用途で使われている箇所がある場合用）
function cssClass(s){return raritySlug(s)}
function safeUrl(u){
  // 空文字ガード: new URL('', location.origin) は例外を投げずサイトトップURLに解決される
  // ため、空文字を渡すと誤ってトップページのURLが返っていた（2026-08-20 reviewer指摘）。
  // URL不明の店でカード名リンクがサイトトップへ誤誘導される実害があったため先に弾く。
  const s=String(u||'').trim();
  if(!s) return '';
  try{
    const url=new URL(s, location.origin);
    return (url.protocol==='http:'||url.protocol==='https:') ? url.href : '';
  }catch{return ''}
}

// 修飾キーと中ボタンはブラウザ標準の別タブ操作に任せる。
function rankingLinkClick(event){
  if(event.defaultPrevented||event.button!==0||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return false;
  event.preventDefault();
  return true;
}

function searchCard(name){
  switchMode('search','ranking');
  document.getElementById('q').value=name;
  document.getElementById('q').scrollIntoView({behavior:'smooth',block:'center'});
  doSearch({trigger:'ranking'});
}

// S-1: 買取の値動きランキングから開いた場合は、検索結果に加えて買取セクション（#buyInline）も
// 自動で開く（そのカードの買取を見に来た人のため）。開くのは検索結果が実際に届いた時点
// （renderAll内の_maybeOpenBuyInlineOnLoad）——固定タイマーで開くと検索完了が遅いときに
// 打ち消される回帰があったため、その方式は使わない
function doBuySearchCard(name){
  _pendingBuyInlineOpen=true;
  searchCard(name);
}

function _fmtDate(s){if(!s)return'';const d=new Date(s+'T00:00:00');return (d.getMonth()+1)+'/'+d.getDate();}

// ── 価格推移ランキング（最初の画面・N-2: 共通店舗ガードのみ・最安¥1,000以上） ──
// 2026-10-21 「値動き」列: 販売価格（すべて）とオーバーフレーム・グランドマスターを1列にまとめた。
// 1段目＝種類（all/of/gmr）、2段目＝向き（グランドマスターは高額順なので隠す）。一覧の要素は1組を共有。
let _topMoversDir='down';
let _moversGroup='all';
// タブ切替のたびに増やす世代番号。3種類が同じ #topMoversList を共有しており、切替前の読込
// （特に2秒後の再試行）が切替後の表示を上書きしないよう、応答時に世代が変わっていたら捨てる
let _moversSeq=0;
function switchMoversGroup(group){
  _moversGroup=group;
  _moversSeq++;
  document.querySelectorAll('#moversGroupTabs .movers-tab').forEach(b=>b.classList.toggle('active',b.dataset.group===group));
  document.getElementById('topMoversDirTabs').classList.toggle('hidden',group==='gmr');
  loadMoversPanel();
}
function switchTopMovers(dir){
  _topMoversDir=dir;
  _moversSeq++;
  document.querySelectorAll('#topMoversDirTabs .movers-tab').forEach(b=>b.classList.toggle('active',b.dataset.dir===dir));
  loadMoversPanel();
}
function loadMoversPanel(retry){
  if(_moversGroup==='gmr') loadGmrPriced(retry);
  else if(_moversGroup==='of') loadOfMovers(_topMoversDir,retry);
  else loadTopMovers(_topMoversDir,retry);
}
// ランキング枠の中身を差し替える（読込中の薄表示・仮枠の aria-busy もここで解く）
function _setMoversHtml(list,html){
  list.classList.remove('is-loading');
  list.removeAttribute('aria-busy');
  list.innerHTML=html;
}
// 読込開始。既に行（仮枠を含む）があれば高さを保ったまま薄くするだけにし、空の時だけ文言を出す
function _setMoversLoading(list){
  if(list.querySelector('.movers-item'))list.classList.add('is-loading');
  else list.innerHTML='<div class="movers-fail">読込中...</div>';
}
function loadTopMovers(dir,retry){
  retry=retry||0;
  const seq=_moversSeq;
  const sec=document.getElementById('topMoversSection');
  const list=document.getElementById('topMoversList');
  const period=document.getElementById('topMoversPeriod');
  sec.classList.remove('hidden');
  if(!retry){_setMoversLoading(list);period.textContent='';}
  fetch(`/api/top-movers?direction=${dir||_topMoversDir}&limit=10`)
    .then(r=>r.json())
    .then(data=>{
      if(seq!==_moversSeq) return;  // 切替済み・この応答は捨てる
      if(data.error){
        // 2026-09-01: 集計をDB側RPC(get_top_movers)へ寄せ、本番実測で初回1.1秒・
        // キャッシュ後0.2秒になったため、応急で8回×3秒に延ばしていた再試行を戻す。
        // 待っている間であることが分かる文言は残す（キャッシュ失効直後は数百ms待つため）。
        if(retry<3){
          _setMoversHtml(list,'<div class="movers-fail">集計中です。少しお待ちください…</div>');
          setTimeout(()=>{ if(seq!==_moversSeq) return; loadTopMovers(dir,retry+1); },2000);
          return;
        }
        _setMoversHtml(list,'<div class="movers-fail">価格推移ランキングの取得に失敗しました</div>');
        return;
      }
      const items=data.items||[];
      // 種類のタブを1列にまとめたので、空でも列ごと隠さず文言を出す（隠すと他のタブへ移れなくなる）
      if(!items.length){_setMoversHtml(list,'<div class="movers-fail">該当するカードがありません</div>');return;}
      if(data.date_old&&data.date_new){
        const upd=data.updated_at?` · ${data.updated_at}収集`:'';
        // Q-11: 定着チェック（前日データ）が無い回は、精度が落ちていることが分かるよう明示する
        const stabilityNote=data.stability_checked===false?'（定着確認なし）':'';
        period.textContent=_fmtDate(data.date_old)+' → '+_fmtDate(data.date_new)+' の価格変動（共通店舗のみ）'+stabilityNote+upd;
      }
      _setMoversHtml(list,items.map((item,i)=>{
        const isUp=item.diff>0;
        const sign=isUp?'+':'';
        const cls=isUp?'up':'down';
        // Q-1: どのレアリティの値段か分からないまま騰落だけ見せない（旧実装で表示していたバッジの復活）
        const rarityBadge=item.rarity?`<span class="rb rb-${cssClass(item.rarity)}">${esc(item.rarity)}</span>`:'';
        return `<a class="movers-item" href="/card/${escAttr(encodeURIComponent(item.name))}" onclick="if(!rankingLinkClick(event))return true;openTopMover('${escAttr(escJs(item.name))}',${i+1})" data-card="${escAttr(item.name)}">
          <span class="movers-rank">${i+1}</span>
          <span class="movers-thumb"></span>
          <span class="movers-name"><span class="movers-name-text">${esc(item.name)}</span>${rarityBadge?`<span class="movers-rarity">${rarityBadge}</span>`:''}</span>
          <span class="movers-price">
            <span class="movers-price-now">&yen;${item.today.toLocaleString()}</span>
            <span class="movers-diff ${cls}">${sign}${item.diff.toLocaleString()} (${sign}${item.pct}%)</span>
          </span>
        </a>`;
      }).join(''));
      loadRankingImages('#topMoversList .movers-item','movers-thumb');
    })
    .catch(()=>{
      if(seq!==_moversSeq) return;
      if(retry<2){setTimeout(()=>{ if(seq!==_moversSeq) return; loadTopMovers(dir,retry+1); },2000);return;}
      _setMoversHtml(list,'<div class="movers-fail">価格推移ランキングの取得に失敗しました</div>');
    });
}
function openTopMover(name,rank){
  _ga('top_mover_click',{rank:rank});
  searchCard(name);
}

// ── OF(オーバーフレーム)／シリアル付きOF(グランドマスターレア)ランキング（2026-09-04追加） ──
// 2026-10-21 から「値動き」列の種類タブ（of/gmr）。表示先は #topMoversList（世代番号 _moversSeq を共有）
function loadOfMovers(dir,retry){
  retry=retry||0;
  const seq=_moversSeq;
  const sec=document.getElementById('topMoversSection');
  const list=document.getElementById('topMoversList');
  const period=document.getElementById('topMoversPeriod');
  sec.classList.remove('hidden');
  if(!retry){_setMoversLoading(list);period.textContent='';}  // L-4: 前タブの期間表示を引き継がない
  fetch(`/api/top-movers?group=of&direction=${dir||_topMoversDir}&limit=10`)
    .then(r=>r.json())
    .then(data=>{
      if(seq!==_moversSeq) return;  // 切替済み・この応答は捨てる
      if(data.error){
        if(retry<3){
          _setMoversHtml(list,'<div class="movers-fail">集計中です。少しお待ちください…</div>');
          setTimeout(()=>{ if(seq!==_moversSeq) return; loadOfMovers(dir,retry+1); },2000);
          return;
        }
        _setMoversHtml(list,'<div class="movers-fail">ランキングの取得に失敗しました</div>');
        return;
      }
      const items=data.items||[];
      // M-1修正: 空データでもセクションごと隠さず、リスト内にメッセージを出す
      // （隠すと他タブへの導線ごと消えて復帰できなくなるため）
      if(!items.length){_setMoversHtml(list,'<div class="movers-fail">該当するカードがありません</div>');period.textContent='';return;}
      if(data.date_old&&data.date_new){
        const upd=data.updated_at?` · ${data.updated_at}収集`:'';
        const stabilityNote=data.stability_checked===false?'（定着確認なし）':'';
        period.textContent=_fmtDate(data.date_old)+' → '+_fmtDate(data.date_new)+' の価格変動（共通店舗のみ）'+stabilityNote+upd;
      }
      _setMoversHtml(list,items.map((item,i)=>{
        const isUp=item.diff>0;
        const sign=isUp?'+':'';
        const cls=isUp?'up':'down';
        const rarityBadge=item.rarity?`<span class="rb rb-${cssClass(item.rarity)}">${esc(item.rarity)}</span>`:'';
        return `<a class="movers-item" href="/card/${escAttr(encodeURIComponent(item.name))}" onclick="if(!rankingLinkClick(event))return true;openOfMover('${escAttr(escJs(item.name))}',${i+1})" data-card="${escAttr(item.name)}">
          <span class="movers-rank">${i+1}</span>
          <span class="movers-thumb"></span>
          <span class="movers-name"><span class="movers-name-text">${esc(item.name)}</span>${rarityBadge?`<span class="movers-rarity">${rarityBadge}</span>`:''}</span>
          <span class="movers-price">
            <span class="movers-price-now">&yen;${item.today.toLocaleString()}</span>
            <span class="movers-diff ${cls}">${sign}${item.diff.toLocaleString()} (${sign}${item.pct}%)</span>
          </span>
        </a>`;
      }).join(''));
      loadRankingImages('#topMoversList .movers-item','movers-thumb');
    })
    .catch(()=>{
      if(seq!==_moversSeq) return;
      if(retry<2){setTimeout(()=>{ if(seq!==_moversSeq) return; loadOfMovers(dir,retry+1); },2000);return;}
      _setMoversHtml(list,'<div class="movers-fail">ランキングの取得に失敗しました</div>');
    });
}
function loadGmrPriced(retry){
  retry=retry||0;
  const seq=_moversSeq;
  const sec=document.getElementById('topMoversSection');
  const list=document.getElementById('topMoversList');
  const period=document.getElementById('topMoversPeriod');
  sec.classList.remove('hidden');
  if(!retry){_setMoversLoading(list);period.textContent='';}  // L-4: 前タブの期間表示を引き継がない
  fetch(`/api/top-priced?group=gmr&limit=10`)
    .then(r=>r.json())
    .then(data=>{
      if(seq!==_moversSeq) return;  // 切替済み・この応答は捨てる
      if(data.error){
        if(retry<3){
          _setMoversHtml(list,'<div class="movers-fail">集計中です。少しお待ちください…</div>');
          setTimeout(()=>{ if(seq!==_moversSeq) return; loadGmrPriced(retry+1); },2000);
          return;
        }
        _setMoversHtml(list,'<div class="movers-fail">ランキングの取得に失敗しました</div>');
        return;
      }
      const items=data.items||[];
      // M-1修正: 空データでもセクションごと隠さず、リスト内にメッセージを出す
      if(!items.length){_setMoversHtml(list,'<div class="movers-fail">該当するカードがありません</div>');period.textContent='';return;}
      if(data.date_new){
        const upd=data.updated_at?` · ${data.updated_at}収集`:'';
        // M-4修正: 差額(diffがnullでない、または「変動なし」の0)を持つ行が
        // 1件でもあれば比較日(date_old)も併記する
        const hasAnyDiff=items.some(it=>it.diff!==null&&it.diff!==undefined);
        // 文言は1行（枠343px）に収める。旧文言「9/4 時点の最安値（高い順）・差額は 8/28 比（共通店舗のみ） · 20:16収集」は
        // 2行に折り返し、隣の2枚と一覧の開始位置が16pxずれた（2026-09-04 本番実測）
        const compareNote=hasAnyDiff?`・差額は${_fmtDate(data.date_old)}比`:'';
        period.textContent=_fmtDate(data.date_new)+' の最安値（高い順'+compareNote+'）'+upd;
      }
      _setMoversHtml(list,items.map((item,i)=>{
        // diff が null（共通店舗が無く比較不能）と 0（7日前と同額）は区別して表示する。
        // 0 を「+0 (0%)」と出しても意味が無いため「変動なし」の文言に置き換える
        const hasDiff=item.diff!==null&&item.diff!==undefined&&item.diff!==0;
        const flatHtml=(item.diff===0)?`<span class="movers-diff" style="color:var(--text-d)">7日間 変動なし</span>`:'';
        const isUp=hasDiff&&item.diff>0;
        const sign=isUp?'+':'';
        const cls=isUp?'up':'down';
        const diffHtml=hasDiff?`<span class="movers-diff ${cls}">${sign}${item.diff.toLocaleString()} (${sign}${item.pct}%)</span>`:flatHtml;
        const rarityBadge=item.rarity?`<span class="rb rb-${cssClass(item.rarity)}">${esc(item.rarity)}</span>`:'';
        return `<a class="movers-item" href="/card/${escAttr(encodeURIComponent(item.name))}" onclick="if(!rankingLinkClick(event))return true;openGmrPriced('${escAttr(escJs(item.name))}',${i+1})" data-card="${escAttr(item.name)}">
          <span class="movers-rank">${i+1}</span>
          <span class="movers-thumb"></span>
          <span class="movers-name"><span class="movers-name-text">${esc(item.name)}</span>${rarityBadge?`<span class="movers-rarity">${rarityBadge}</span>`:''}</span>
          <span class="movers-price">
            <span class="movers-price-now">&yen;${item.price.toLocaleString()}</span>
            ${diffHtml}
          </span>
        </a>`;
      }).join(''));
      loadRankingImages('#topMoversList .movers-item','movers-thumb');
    })
    .catch(()=>{
      if(seq!==_moversSeq) return;
      if(retry<2){setTimeout(()=>{ if(seq!==_moversSeq) return; loadGmrPriced(retry+1); },2000);return;}
      _setMoversHtml(list,'<div class="movers-fail">ランキングの取得に失敗しました</div>');
    });
}
function openOfMover(name,rank){
  _ga('of_mover_click',{rank:rank});
  searchCard(name);
}
function openGmrPriced(name,rank){
  _ga('gmr_priced_click',{rank:rank});
  searchCard(name);
}

// ── 買取値上がり/値下がりランキング（色反転: UP=緑/DOWN=赤） ──
let _buybackMoversDir='up';
function switchBuybackMovers(dir){
  _buybackMoversDir=dir;
  document.querySelectorAll('#buybackMoversSection .movers-tab').forEach(b=>b.classList.toggle('active',b.textContent.includes(dir==='up'?'UP':'DOWN')));
  loadBuybackMovers(dir);
}
function loadBuybackMovers(dir,retry){
  retry=retry||0;
  fetch(`/api/buyback-movers?direction=${dir||_buybackMoversDir}&limit=10`)
    .then(r=>r.json())
    .then(data=>{
      const items=data.items||[];
      const sec=document.getElementById('buybackMoversSection');
      const list=document.getElementById('buybackMoversList');
      const period=document.getElementById('buybackMoversPeriod');
      if(!items.length){_setMoversHtml(list,'');sec.classList.add('hidden');return;}
      sec.classList.remove('hidden');
      if(data.date_old&&data.date_new){
        const upd=data.updated_at?` · ${data.updated_at}収集`:'';
        period.textContent=_fmtDate(data.date_old)+' → '+_fmtDate(data.date_new)+' の買取価格変動'+upd;
      }
      _setMoversHtml(list,items.map((item,i)=>{
        const isUp=item.diff>0;
        const sign=isUp?'+':'';
        // 買取は値上がり=緑(お得)、値下がり=赤 と色反転するため buyback-diff クラスを使用
        const cls=isUp?'up':'down';
        const rarityBadge=item.rarity?`<span class="rb rb-${cssClass(item.rarity)}">${esc(item.rarity)}</span>`:'';
        return `<a class="movers-item" href="/buy/${escAttr(encodeURIComponent(item.name))}" onclick="if(!rankingLinkClick(event))return true;doBuySearchCard('${escAttr(escJs(item.name))}')" data-card="${escAttr(item.name)}">
          <span class="movers-rank">${i+1}</span>
          <span class="movers-thumb"></span>
          <span class="movers-name"><span class="movers-name-text">${esc(item.name)}</span>${rarityBadge?`<span class="movers-rarity">${rarityBadge}</span>`:''}</span>
          <span class="movers-price">
            <span class="movers-price-now">&yen;${item.today.toLocaleString()}</span>
            <span class="movers-diff buyback-diff ${cls}">${sign}${item.diff.toLocaleString()} (${sign}${item.pct}%)</span>
          </span>
        </a>`;
      }).join(''));
      loadRankingImages('#buybackMoversList .movers-item','movers-thumb');
    })
    .catch(()=>{
      if(retry<2){setTimeout(()=>loadBuybackMovers(dir,retry+1),2000);return;}
      // 最後まで失敗したら仮枠を残さず、他の2枠と同じく文言に置き換える
      _setMoversHtml(document.getElementById('buybackMoversList'),'<div class="movers-fail">買取ランキングの取得に失敗しました</div>');
    });
}

// ── ランキング用カード画像の一括取得（旧: 1件ずつ200ms間隔→バッチで1回→
//    さらに: 同一タイミングで起動する複数セクション分の呼び出しを100msデバウンスで
//    束ね、/api/card-images へのPOSTを通常1〜2回にまとめる（応答が100ms超ばらつくと
//    2回に分かれることがある）。初期ロード時に3セクションが連続して呼び出しても、
//    名前のunionで1回のPOSTになる）
let _rankingImageQueue=[];
let _rankingImageTimer=null;
function loadRankingImages(itemSelector, thumbClass){
  const key=itemSelector+'|'+thumbClass;
  if(_rankingImageQueue.some(q=>q.key===key)) return;  // 同一セレクタの重複キューイングを防ぐ
  _rankingImageQueue.push({itemSelector,thumbClass,key});
  if(_rankingImageTimer) return;
  _rankingImageTimer=setTimeout(_flushRankingImageQueue,100);
}
function _flushRankingImageQueue(){
  const queue=_rankingImageQueue;
  _rankingImageQueue=[];
  _rankingImageTimer=null;
  // DOM要素はflush時点（POST応答を待たず）に集めておく。応答が届く頃には
  // タブ切替等でリストが再描画されている可能性があるため
  const entries=queue.map(({itemSelector,thumbClass})=>({
    els:[...document.querySelectorAll(itemSelector+'[data-card]')],
    thumbClass
  })).filter(e=>e.els.length);
  if(!entries.length) return;
  const names=[...new Set(entries.flatMap(e=>e.els.map(el=>el.dataset.card).filter(Boolean)))];
  fetch('/api/card-images',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({names})
  })
  .then(r=>r.json())
  .then(({images})=>{
    if(!images) return;
    entries.forEach(({els,thumbClass})=>{
      els.forEach(el=>{
        const url=_batchImgUrl(images[el.dataset.card]);
        if(!url||!safeUrl(url)) return;
        const thumb=el.querySelector('.'+thumbClass);
        if(thumb) _deferRankingImage(thumb,safeUrl(url));
      });
    });
  })
  .catch(()=>{});
}
// ランキングの縮小画像は、画面に近づいてから読み込む。
// loading="lazy" だと低速回線では画面から約2500px先まで先読みされ、スマホのトップで
// 画面外のランキング画像（別ホストのため優先度で譲らない）が新着カード画像と回線を
// 取り合い、LCP を数秒遅らせていた（2026-10-16 実測）。
let _rankingImageObserver=null;
function _deferRankingImage(thumb,src){
  const html='<img src="'+escAttr(src)+'" alt="">';
  if(!('IntersectionObserver' in window)){thumb.innerHTML=html.replace('<img','<img loading="lazy"');return;}
  if(!_rankingImageObserver){
    _rankingImageObserver=new IntersectionObserver(entries=>{
      entries.forEach(entry=>{
        // タブ切替などで一覧から外れた枠は、登録直後の最初の通知で見張りから外す
        if(!entry.target.isConnected){_rankingImageObserver.unobserve(entry.target);return;}
        if(!entry.isIntersecting) return;
        _rankingImageObserver.unobserve(entry.target);
        entry.target._showRankingImage?.();
      });
    },{rootMargin:'200px'});
  }
  // 同じ枠に後から別の URL が来たら最後のものを出すため、差し込み処理は要素に持たせる
  thumb._showRankingImage=()=>{thumb.innerHTML=html;};
  _rankingImageObserver.observe(thumb);
}

// 初期ロード・モード復元
// DOMContentLoaded で実行することで、スクリプト末尾の const/let 宣言が
// 全て初期化された後に switchMode() を呼び出し、TDZ ReferenceError を防ぐ。
document.addEventListener('DOMContentLoaded', () => {
  // G-7: 「一人回し」リンクはinline onclickではなくaddEventListenerで配線
  // （将来のCSP導入でinline onclickが無効化されても壊れないように）
  document.getElementById('headerSoloLink')?.addEventListener('click', () => _ga('solo_play_open'));

  // 検索欄を手動で空にしたら結果を隠して初期状態（#empty）へ戻す
  document.getElementById('q')?.addEventListener('input', () => {
    const results=document.getElementById('results');
    if(document.getElementById('q').value.trim()===''&&!results.classList.contains('hidden')){
      results.classList.add('hidden');
      document.getElementById('empty').classList.remove('hidden');
      _setSearchLayout(false);
    }
  });

  // フラッシュ防止用の一時スタイル（#mydeck では本文も隠している）を外す。以降のタブ切替を正常に動かすため、
  // 復元の途中で例外が出ても必ず外す
  try{
    _restoreLocation('pageload');
  }finally{
    document.getElementById('fouc-prevent')?.remove();
  }

  // 管理者のブラウザ（localStorage に admin_key がある）だけフッターに「管理」リンクを出す。
  // キー名は static/admin/admin.js の STORAGE_KEY と同じ。
  // プライベートモード等で localStorage が例外を投げても何もしない（非表示のまま）
  try{
    if(localStorage.getItem('admin_key')){
      const _adminLink=document.getElementById('footerAdminLink');
      if(_adminLink) _adminLink.hidden=false;
    }
  }catch(e){ /* 参照不可なら非表示のまま */ }

});

// SEO: 検索完了時にURLをpushStateで書き換え（ブラウザ履歴対応）
function _updateUrl(cardName, mode){
  if(!cardName)return;
  const prefix=mode==='buyback'?'/buy/':'/card/';
  const newUrl=prefix+encodeURIComponent(cardName);
  if(window.location.pathname+window.location.search+window.location.hash!==newUrl){
    history.pushState({card:cardName,mode:mode},'',newUrl);
  }
  // ページタイトルも更新
  const suffix=mode==='buyback'?' の買取価格比較':' の価格比較';
  document.title=cardName+suffix+' | 強欲なラボ';
}

// URLを正本にして、初期表示と戻る・進むを同じ処理で復元する。
// searching=true は直後に検索を始める場合（/card/・/buy/ の復元）。トップ用の欄を一瞬出して
// すぐ隠す往復（表示のズレと不要な取得）を避けるため、#empty は隠したまま検索中の配置にする。
function _resetSearchView(searching){
  SearchStatus.show({});
  _newSearchGen();
  _resetBuyInline();
  closeSuggest();
  _pendingBuyInlineOpen=false;
  document.getElementById('btn').disabled=false;
  document.getElementById('btn').textContent='検索';
  document.getElementById('q').value='';
  document.getElementById('results').classList.add('hidden');
  document.getElementById('empty').classList.toggle('hidden',!!searching);
  document.getElementById('emptyError').classList.add('hidden');
  document.getElementById('prog').style.display='none';
  _setSearchLayout(!!searching);
}
// 未発売カード一覧ページ（/new-cards）・海外先行カード一覧（/tcg-preview）の表示状態。ランキング等のトップ要素を隠し、一覧を主役にする。
function _setNewCardsPage(on,tcgPreview=false){
  document.getElementById('mode-search').classList.toggle('new-cards-page',on);
  document.getElementById('mode-search').classList.toggle('tcg-preview-page',on&&tcgPreview);
}
// #mydeck:<デッキID> のデッキを開く。ページを開いたときはマイデッキの下書きの復元（deck-edit.js）より後に開くよう、
// 読み込みの処理が一通り終わってから行う。開いたら URL は #mydeck に戻す（再読込で開き直さない）
function _openDeckFromHash(id){
  setTimeout(()=>{
    try{history.replaceState(null,'',location.pathname+location.search+'#mydeck');}catch{}
    const deck=(typeof savedDecksGet==='function'?savedDecksGet():[]).find(d=>d.id===id);
    if(!deck){alert('このデッキはマイデッキにありません（削除したか、別の端末のデッキです）');return;}
    // 「マイデッキ ▾」の窓から開くのと同じ手順（登録していない編集の確認・スマホはデッキのタブへ）
    if(window.DeckLibrary&&typeof window.DeckLibrary.openDeck==='function'){window.DeckLibrary.openDeck(id);return;}
    if(window._currentSavedDeckId===id)return;
    if(typeof loadSavedDeck==='function')loadSavedDeck(id);
  },0);
}
function _restoreLocation(trigger){
  _setNewCardsPage(location.pathname==='/new-cards'||location.pathname==='/tcg-preview',location.pathname==='/tcg-preview');
  const hash=location.hash.slice(1);
  // #mydeck:<デッキID> … そのマイデッキを開く（シミュレーターの「記録の後にデッキが変わっています」の［今のデッキを見る］から）
  let openDeckId=null;
  if(hash.startsWith('mydeck:')){try{openDeckId=decodeURIComponent(hash.slice(7))||null;}catch{openDeckId=null;}}
  if(hash.startsWith('mydeck:')&&!openDeckId){try{history.replaceState(null,'',location.pathname+location.search+'#mydeck');}catch{}}
  const mode=(hash==='mydeck-import'||hash.startsWith('mydeck:'))?'mydeck':hash;
  const cardPath=location.pathname.match(/^\/(card|buy)\/(.+)$/);
  let cardName=null;
  if(cardPath&&!['mydeck','meta','packs','wishlist'].includes(mode)){
    try{cardName=decodeURIComponent(cardPath[2]);}catch{cardName=null;}
    if(cardName!==null&&!cardName.trim())cardName=null; // 1文字は doSearch が既知のカード名か確かめる
  }
  _resetSearchView(!!cardName);
  if(['mydeck','meta','packs','wishlist'].includes(mode)){
    switchMode(mode,trigger);
    if(hash==='mydeck-import'){
      document.getElementById('deckPanes')?.classList.add('show-tools');
      if(typeof window.switchDeckPane==='function')window.switchDeckPane('tools');
      // 取り込みは右の欄のボタンで開く窓（static/mydeck/deck-tools.js）
      if(window.DeckTools)window.DeckTools.openImport();
    }
    if(openDeckId)_openDeckFromHash(openDeckId);
    document.title='強欲なラボ';
    return;
  }
  if(location.pathname==='/new-cards'||location.pathname==='/tcg-preview'){
    switchMode('search',trigger);
    document.title=location.pathname==='/tcg-preview'?'海外先行カード一覧 | 強欲なラボ':'未発売カード一覧 | 強欲なラボ';
    return;
  }
  if(location.pathname==='/featured'){
    switchMode('packs',trigger);
    loadPacks().then(()=>{
      if(location.pathname!=='/featured'||location.hash)return;
      // すでにパックを選んでいるとき（ブラウザの戻る・進むで来たとき）は選び直さない。スマホの全面の詳細を閉じるたびに
      // 新弾を開き直して履歴を足し、戻るで /featured から出られなくなっていた（2026-10-21 reviewer 指摘）
      if(typeof _pk!=='undefined'&&_pk)return;
      const f=_packData.find(p=>p.featured);
      if(f)loadPackCards(f.name,f.wiki_page||f.name,f.tcg_name||'',true);
    });
    return;
  }
  switchMode('search',trigger);
  if(cardPath){
    // 復号できないURLは検索せずトップの表示のまま。1文字で既知のカード名でなければ doSearch がトップの表示に戻す
    if(cardName===null)return;
    const name=cardName;
    document.getElementById('q').value=name;
    _pendingBuyInlineOpen=cardPath[1]==='buy';
    doSearch({trigger:trigger});
  }else{
    document.title='強欲なラボ';
  }
}
window.addEventListener('popstate',e=>{ if(_fxSheetConsumePop(e)) return; _restoreLocation('popstate'); });

// G-5: スクロール量計測（C-3c ボトムナビ判断材料）。
// 現在のスクロール到達率（%）。スクロール不可（コンテンツが画面に収まる）なら0を返す
function _scrollPct(){
  const doc=document.documentElement;
  const scrollTop=window.scrollY||doc.scrollTop||0;
  const scrollable=doc.scrollHeight-doc.clientHeight;
  if(scrollable<=0) return 0;
  return Math.round(Math.min(100,scrollTop/scrollable*100));
}
// 25/50/75%到達で1回ずつ発火。90%は GA4 拡張計測の scroll イベントが受け持つので、
// ここでは重ねて送らない（役割分担・2026-10-16 整理）。
// スクロールのたびに高さを読むとレイアウト計算が走るため、1フレームに1回へ間引き、
// 3つとも送り終えたらリスナーごと外す。
const _SCROLL_DEPTH_THRESHOLDS=[25,50,75];
const _scrollDepthFired=new Set();
let _scrollDepthQueued=false;
function _onScrollDepth(){
  if(_scrollDepthQueued) return;
  _scrollDepthQueued=true;
  requestAnimationFrame(()=>{
    _scrollDepthQueued=false;
    const pct=_scrollPct();
    _SCROLL_DEPTH_THRESHOLDS.forEach(th=>{
      if(pct>=th && !_scrollDepthFired.has(th)){
        _scrollDepthFired.add(th);
        _ga('scroll_depth',{percent:th});
      }
    });
    if(_scrollDepthFired.size===_SCROLL_DEPTH_THRESHOLDS.length) window.removeEventListener('scroll',_onScrollDepth);
  });
}
window.addEventListener('scroll',_onScrollDepth,{passive:true});

// ── Mode switch ──
let _currentMode='search'; // GA4計測用: 直前のモードを保持（mode_switchのfrom_mode）
// G-1: mode_switchはswitchMode()の呼び出し元ごとに明示的なtriggerを渡したときだけ発火する
// （tab=タブクリック／hash=URLハッシュ復元／deeplink=/buy等の直アクセス／popstate=戻る進む／
// auto=「デッキに追加」等のUI操作による自動遷移／ranking=人気・値動きランキングのクリック）。
// trigger省略時は計測しない＝ユーザーがタブを押した経路（trigger:'tab'）だけを素の指標にできる
function switchMode(mode, trigger){
  if(trigger && mode!==_currentMode){
    _ga('mode_switch',{mode:mode,from_mode:_currentMode,trigger:trigger,scroll_pct:_scrollPct()});
  }
  _currentMode=mode;
  document.querySelectorAll('.mode-tab').forEach(b=>{
    // 文言変更や購入候補の件数バッジに依存せず、遷移先をキーに選択状態を同期する。
    const active=b.dataset.mode===mode;
    b.classList.toggle('active',active);
    if(active) b.setAttribute('aria-current','page');
    else b.removeAttribute('aria-current');
  });
  ['search','mydeck','meta','packs','wishlist'].forEach(m=>{
    document.getElementById('mode-'+m).classList.toggle('hidden',m!==mode);
  });
  if(mode==='packs') loadPacks();
  // マイデッキタブ: 端末同期で届いていない変更の表示を出し直す（pull はページを開いたときに済んでいる）
  if(mode==='mydeck' && typeof _refreshSyncStatusUI==='function') _refreshSyncStatusUI();
  if(mode==='wishlist'){
    renderWishlist();
    // 端末間同期: 購入候補タブを開いたときだけ pull する（カード個別ページ閲覧では走らせない）。
    // pullIfNeeded() は購入候補・保存デッキの両方を1回のAPI呼び出しで扱う（P2）
    if(window.SyncClient){
      const _pullPromise=window.SyncClient.pullIfNeeded();
      // 同期状態の表示（P3・§7.3「他の端末と同期中」）はpullが非同期のため、
      // pull完了後に更新する。タブを開いた直後は「他端末と連携済みか」が未確定であり、
      // 誤表示（未連携なのに表示される）より無表示を優先する
      // （2026-08-18 実機バグの修正: sync_idの有無だけで判定していたため、購入候補を
      // 1件持っただけの未連携端末にも表示されていた）。
      // _refreshSyncStatusUI はページ末尾のスクリプトで定義されるため、初回ページ読み込み時
      // （#wishlist直リンク等）はまだ未定義のことがある。typeof で安全に判定する
      // （sync-client.js の <script> より前で呼ぶと壊れる、というP1の教訓と同じ理由のガード）
      if(_pullPromise && typeof _pullPromise.then==='function'){
        _pullPromise.then(function(){
          if(typeof _refreshSyncStatusUI==='function') _refreshSyncStatusUI();
        });
      }
    }
  }
  if(mode==='meta'){loadMetaTiers();loadMetaDeckPrices();}
  else if(typeof closeMetaSheet==='function') closeMetaSheet(true); // スマホの全面の詳細は閉じる（履歴は戻さない）
  if(mode!=='packs'&&typeof closePackSheet==='function') closePackSheet(true); // 新商品の全面の詳細も同じ（packs.js）
  if(mode==='mydeck'){
    renderSavedDecks();
    // 端末間同期（P2）: マイデッキタブを開いたときだけ pull する（カード個別ページ閲覧では走らせない）
    if(window.SyncClient) window.SyncClient.pullIfNeeded();
  }
  // タブ操作のみ履歴を積む。復元とランキング検索では中間URLを作らない。
  if(trigger && !['pageload','popstate','hash','deeplink','ranking'].includes(trigger)){
    const url=mode==='search'?'/':'/#'+mode;
    if(location.pathname+location.search+location.hash!==url){
      _resetSearchView();
      history.pushState(null,'',url);
      // 一覧ページからタブ操作でトップ等へ移った場合は、一覧専用の表示状態を解除する。
      _setNewCardsPage(false);
      document.title='強欲なラボ';
    }
  }
}

// ── Deck Builder ──

// コンテキスト定義（マイデッキ / 環境デッキ）
// マイデッキのmain/ex状態（loadSavedDeck / onDeckImported で設定、テキスト変更時に再解析）
let _currentMydeckCards={main:[],ex:[]};
let _currentMydeckText=''; // _currentMydeckCardsに対応するテキスト（変更検知用）
let _currentDeckName=''; // 現在のデッキ名（シェア文・画像生成用）

const DECK_CTX = {
  mydeck:{
    results:'deckResults', list:'deckCardList', total:'deckTotalPrice',
    prog:'deckProgress', rowPrefix:'deck-row-',
    shareArea:'deckShareArea',
    getText:()=>document.getElementById('deckTextarea').value,
    getCards:()=>_currentMydeckCards,
    getName:()=>_currentDeckName||'マイデッキ'
  },
  meta:{
    results:'metaDeckResults', list:'metaDeckCardList', total:'metaDeckTotalPrice',
    prog:'metaDeckProgress', rowPrefix:'meta-row-',
    shareArea:'metaDeckShareArea',
    getText:()=>_lastMetaDeckText,
    getCards:()=>parseDeckSections(_lastMetaDeckText),
    getName:()=>_currentDeckName||'環境デッキ'
  }
};

function parseDeckList(text){
  // [EX]区切り行はカード名ではないので除外
  const lines=text.split('\n').map(l=>l.trim()).filter(l=>l&&l!=='[EX]');
  const cards=[];
  for(const line of lines){
    const m=line.match(/^(\d+)\s+(.+)$/);
    if(m) cards.push({qty:parseInt(m[1]),name:m[2].trim()});
    else cards.push({qty:1,name:line});
  }
  return cards;
}

// テキストを [EX] 区切りでメイン/エクストラに分割して返す
// [EX] がなければ全枚数をメインとして返す
function parseDeckSections(text){
  const lines=text.split('\n');
  const sep=lines.findIndex(l=>l.trim()==='[EX]');
  if(sep<0) return {main:parseDeckList(text),ex:[]};
  return {
    main:parseDeckList(lines.slice(0,sep).join('\n')),
    ex:parseDeckList(lines.slice(sep+1).join('\n'))
  };
}

// カード配列（main+ex）をカード名ごとの合計枚数Mapにする（名前はtrim、qtyは数値化）
function _deckCardCounts(main,ex){
  const map=new Map();
  for(const c of [].concat(Array.isArray(main)?main:[],Array.isArray(ex)?ex:[])){
    if(!c) continue;
    const name=String(c.name==null?'':c.name).trim();
    if(!name) continue;
    const q=Number(c.qty);
    map.set(name,(map.get(name)||0)+(Number.isFinite(q)?q:1));
  }
  return map;
}

// main/ex 配列が text（[EX]区切り）と同じ内容か（カード名ごとの合計枚数で比較）
function _deckArraysMatchText(deck){
  const text=String(deck.text||'').replace(/\r/g,'');
  const parsed=parseDeckSections(text);
  const a=_deckCardCounts(parsed.main,parsed.ex);
  const b=_deckCardCounts(deck.main,deck.ex);
  if(a.size!==b.size) return false;
  for(const [k,v] of a){ if(b.get(k)!==v) return false; }
  return true;
}

// 保存デッキをmain/ex構造に正規化（旧データ対応）
// deck.ex=[]（空配列）はfalsyになるためArray.isArrayで判定する
function normalizeDeck(deck){
  if(Array.isArray(deck.main)){
    const text=String(deck.text||'').replace(/\r/g,'');
    // text が空なら配列をそのまま採用
    if(!text.trim()||_deckArraysMatchText(deck)) return {main:deck.main,ex:Array.isArray(deck.ex)?deck.ex:[]};
    // 配列が text と食い違う（上書き保存で配列が古いまま等）→ text を正とする
    console.warn('保存デッキの main/ex が text と食い違っているため text を採用（'+(deck.name||'')+'）');
    return parseDeckSections(text);
  }
  // 旧形式 or main未設定 → テキストを[EX]区切りで解析
  return parseDeckSections(deck.text||'');
}

// カード種別ソート順（モンスター→魔法→罠）
const _DECK_TYPE_ORDER={monster:0,spell:1,trap:2};

// カード配列を種別順にソートして返す（/api/card-typesで取得、サーバー側キャッシュ済み）
async function sortCardsByType(cards){
  if(!cards.length) return cards;
  try{
    const names=[...new Set(cards.map(c=>c.name))];
    const res=await fetch('/api/card-types',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({names})
    });
    if(!res.ok) return cards;
    const {types}=await res.json();
    return [...cards].sort((a,b)=>{
      const ta=_DECK_TYPE_ORDER[types[a.name]]??3;
      const tb=_DECK_TYPE_ORDER[types[b.name]]??3;
      return ta-tb;
    });
  }catch{
    return cards;
  }
}

// デッキをカード画像グリッドとして描画（main/ex 2セクション）
function renderDeckGrid(cards, ctx, mainCount){
  const listEl=document.getElementById(ctx.list);
  const rowPrefix=ctx.rowPrefix;

  function cellHtml(c, i){
    const badge=c.qty>1?`<span class="deck-grid-badge">${c.qty}</span>`:'';
    const cardUrl=`/card/${encodeURIComponent(c.name)}`;
    return `<div class="deck-grid-cell loading" id="${rowPrefix}${i}" data-card="${escAttr(c.name)}" title="">
      <a class="deck-grid-img-wrap" href="${cardUrl}" target="_blank" rel="noopener" title="${escAttr(c.name)}の価格を確認">
        <div class="deck-grid-placeholder"></div>
        ${badge}
      </a>
      <div class="deck-grid-footer">
        <span class="deck-card-name">${esc(c.name)}</span>
        <span class="deck-card-price"></span>
      </div>
      <span class="deck-card-shop" hidden></span>
      <span class="deck-card-status" hidden></span>
    </div>`;
  }

  const mainCards=cards.slice(0,mainCount);
  const exCards=cards.slice(mainCount);
  let html='';

  if(mainCards.length){
    const total=mainCards.reduce((s,c)=>s+c.qty,0);
    html+=`<div class="deck-grid-section">
      <div class="deck-grid-section-label">メインデッキ（${total}枚）</div>
      <div class="deck-grid">${mainCards.map((c,i)=>cellHtml(c,i)).join('')}</div>
    </div>`;
  }
  if(exCards.length){
    const total=exCards.reduce((s,c)=>s+c.qty,0);
    html+=`<div class="deck-grid-section">
      <div class="deck-grid-section-label">エクストラデッキ（${total}枚）</div>
      <div class="deck-grid">${exCards.map((c,i)=>cellHtml(c,mainCount+i)).join('')}</div>
    </div>`;
  }
  if(!html) html='<div style="padding:12px;color:var(--text-d);font-size:.8rem">カードが入力されていません</div>';
  listEl.innerHTML=html;
  // 編集UI（操作バー・ドラッグ）の注入はdeck-edit.jsに委譲（マイデッキ＋編集モード時のみ動作）
  if(window._deckAfterRender) window._deckAfterRender(listEl,cards,mainCount,ctx);
}

// グリッドセルにカード画像をバッチ取得して一括表示する（高速化版）
// 旧実装: 1枚ずつ120ms間隔で個別fetch（40枚で最大4.8秒）
// 新実装: ユニーク名を集めて1リクエストで一括取得
function loadDeckGridImages(listEl){
  const cells=[...listEl.querySelectorAll('[data-card]')];
  const names=[...new Set(cells.map(el=>el.dataset.card).filter(Boolean))];
  if(!names.length) return;

  fetch('/api/card-images',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({names})
  })
  .then(r=>r.json())
  .then(({images})=>{
    if(!images) return;
    cells.forEach(el=>{
      const url=_batchImgUrl(images[el.dataset.card]);
      if(!url) return;
      const wrap=el.querySelector('.deck-grid-img-wrap');
      const placeholder=wrap&&wrap.querySelector('.deck-grid-placeholder');
      if(placeholder&&safeUrl(url)) placeholder.outerHTML=`<img src="${escAttr(url)}" alt="" loading="lazy">`;
    });
  })
  .catch(()=>{});
}

// PDFドラッグ&ドロップハンドラ
function handleDeckDrop(event){
  const file=event.dataTransfer.files[0];
  if(!file) return;
  if(file.type!=='application/pdf'){
    alert('PDFファイルを選択してください');
    return;
  }
  window.NeuronPDF?.handleFile(file);
}

// PDF取り込み・デッキロード後のグリッドプレビュー起動
window.onDeckImported=function(deck){
  const{main,ex}=normalizeDeck(deck);
  _currentMydeckCards={main,ex};
  // CRLF(\r\n)をLF(\n)に正規化: textareaがLFに変換するため、比較ミスを防ぐ
  const combined=(deck.text||[...main,...ex].map(c=>c.qty>1?`${c.qty} ${c.name}`:c.name).join('\n')).replace(/\r/g,'');
  _currentMydeckText=combined;
  const ta=document.getElementById('deckTextarea');
  if(ta) ta.value=combined;
  // PDF取込時は自動で価格計算しない。カード一覧のプレビューだけ表示する
  calcDeckEstimate(DECK_CTX.mydeck,{previewOnly:true});
};

// ── 環境デッキ（TCG PORTAL連携）──
let _metaLoaded=false;
let _metaTiers=[];
let _metaMetadata={};
let _metaLoadPromise=null; // Q-11: 呼び出しが重なった時に /api/meta を二重fetchしないためのin-flightガード

// R-1: 旧トップページの「デッキの金額」ランキング（/api/top-decks）を環境デッキタブへ統合。
// 既定は入賞傾向（Tierグループ表示）、切替で安い順（フラット表示）にする。
// /api/top-decks はシェア上位8件（top_page.TOP_DECKS_LIMIT）しかスクレイプ・価格計算しないため、
// _metaPriceMap に入っていないデッキ（Tier一覧には出るが上位8件に入らないもの）もある。
// その場合は価格行を出さない（「価格情報なし」は取得を試みて失敗した場合の表記のため、
// そもそも取得していないデッキと意味を混同しないよう区別する）。
let _metaSort='tier';
let _metaPriceMap={}; // name -> {total,priced_count,missing_count,tier,share}
let _metaPricesLoaded=false;
let _metaPricesLoadPromise=null;

function switchMetaSort(sort){
  _metaSort=sort;
  document.querySelectorAll('#metaSortTabs .movers-tab').forEach(b=>b.classList.toggle('active',b.textContent.includes(sort==='price'?'安い':'入賞傾向')));
  renderMetaTiers();
  loadMetaDeckPrices();
}

let _metaPricesError=false; // 価格データの取得を試みて失敗した（=取得を試みていないデッキとは区別する）
function loadMetaDeckPrices(retry){
  retry=retry||0;
  if(_metaPricesLoaded) return Promise.resolve();
  if(_metaPricesLoadPromise) return _metaPricesLoadPromise;
  _metaPricesLoadPromise=fetch('/api/top-decks?sort=tier')
    .then(r=>r.json())
    .then(data=>{
      if(data.error&&!(data.decks&&data.decks.length)){
        // 旧loadTopDecksと同じ方針: 2回まで再試行してから諦める
        if(retry<2){setTimeout(()=>loadMetaDeckPrices(retry+1),2000);return;}
        _metaPricesLoaded=true;
        _metaPricesError=true;
        if(_metaTiers.length) renderMetaTiers();
        return;
      }
      for(const d of (data.decks||[])) _metaPriceMap[d.name]=d;
      _metaPricesLoaded=true;
      if(_metaTiers.length) renderMetaTiers(); // 一覧が既に表示済みなら価格を反映して再描画
    })
    .catch(()=>{
      if(retry<2){setTimeout(()=>loadMetaDeckPrices(retry+1),2000);return;}
      _metaPricesLoaded=true;
      _metaPricesError=true;
      if(_metaTiers.length) renderMetaTiers();
    })
    .finally(()=>{_metaPricesLoadPromise=null;});
  return _metaPricesLoadPromise;
}

// 価格（一覧の行の右端。2026-10-21 モック meta-refresh-20261021 の D）:
//   全部取れた → 金額だけ（以前は全行に「全カード取得済み」が付いて目立っていた）／一部未取得 → 「〜」と「N種未取得」／
//   取得を試みて失敗 → 「価格なし」／価格を計算していないデッキ（上位以外）→「—」（読込中は何も出さない）
function _metaDeckPriceHtml(name){
  const d=_metaPriceMap[name];
  // 取得に失敗したときは「—」を出さない（計算していないデッキと区別する。失敗は一覧の下の注記で知らせる）
  if(!d) return _metaPricesLoaded&&!_metaPricesError?'<span class="mt-price is-none" title="価格は使用率の高い上位デッキだけ計算しています">—</span>':'<span class="mt-price"></span>';
  // Q-11: totalは「取れた分だけの合計」であり正確な総額ではないため、一部未取得のときは下限であることが分かるよう「〜」を付ける
  // Q-9: missing_countはカードの種類数（枚数ではない）なので「種」と表記する
  if(d.priced_count>0){
    return `<span class="mt-price">&yen;${d.total.toLocaleString()}${d.missing_count>0?`〜<small>${d.missing_count}種未取得</small>`:''}</span>`;
  }
  return '<span class="mt-price is-none">価格なし</span>';
}

// 一覧の1行（D）: 画像・名前・使用率（短い棒）・価格。選んでいるデッキは金の線（.selected）
let _currentMetaTheme='';
function _metaRowHtml(d,rank){
  const img=d.image?safeUrl(d.image):'';
  const max=Math.max.apply(null,_metaTiers.map(t=>Number(t.share)||0))||1;
  const w=Math.max(3,Math.round(34*(Number(d.share)||0)/max));
  return `<button class="meta-deck-btn mt-row${d.name===_currentMetaTheme?' selected':''}" onclick="loadMetaDeck('${escAttr(escJs(d.name))}',${rank})" id="meta-btn-${escAttr(d.name)}">
        ${img?`<img src="${escAttr(img)}" alt="" loading="lazy">`:'<span class="mt-noimg"></span>'}
        <span class="mt-name">${esc(d.name)}</span>
        <span class="mt-share"><i style="width:${w}px"></i>${esc(d.share)}%</span>
        ${_metaDeckPriceHtml(d.name)}
      </button>`;
}

function loadMetaTiers(){
  if(_metaLoaded) return Promise.resolve();
  if(_metaLoadPromise) return _metaLoadPromise; // 進行中のfetchに相乗りする
  _metaLoadPromise=_loadMetaTiersInner().finally(()=>{_metaLoadPromise=null;});
  return _metaLoadPromise;
}

async function _loadMetaTiersInner(){
  const el=document.getElementById('metaTierList');
  const CACHE_KEY='tcgym_meta_cache_v2', TTL=3600*1000; // 1時間
  try{
    const raw=sessionStorage.getItem(CACHE_KEY);
    if(raw){
      const {ts,data}=JSON.parse(raw);
      if(Date.now()-ts<TTL&&Array.isArray(data.tiers)&&data.tiers.length&&!data.metadata?.stale&&!data.metadata?.refresh_error){
        _metaTiers=data.tiers;_metaMetadata=data.metadata||{};_metaLoaded=true;renderMetaTiers();return;
      }
    }
  }catch{}
  el.innerHTML='<div class="meta-loading">環境データを読込中...</div>';
  try{
    const res=await fetch('/api/meta?include_metadata=1');
    if(!res.ok) throw new Error('環境データ取得失敗');
    const data=await res.json();
    // 段階的な公開更新中は旧配列形式も許容し、取得条件は不明と表示する。
    _metaTiers=Array.isArray(data)?data:data.tiers;
    if(!Array.isArray(_metaTiers)) throw new Error('環境データ形式不正');
    _metaMetadata=Array.isArray(data)?{}:(data.metadata||{});
    _metaLoaded=_metaTiers.length>0&&!_metaMetadata.stale&&!_metaMetadata.refresh_error;
    try{
      if(_metaLoaded) sessionStorage.setItem(CACHE_KEY,JSON.stringify({ts:Date.now(),data:{tiers:_metaTiers,metadata:_metaMetadata}}));
      else sessionStorage.removeItem(CACHE_KEY);
    }catch{}
    renderMetaTiers();
  }catch(e){
    el.innerHTML='<div class="meta-loading">環境データの取得に失敗しました</div>';
  }
}

// 日付を日本時間の「10/21」「10/21 21:36」の形にする（2026-10-21 モックの E。以前は 2026-10-21T12:36:19.800136+00:00 のまま出ていた）
function _metaFmtDate(s,withTime){
  if(!s) return '';
  const d=new Date(/T/.test(s)?s:s+'T00:00:00+09:00');
  if(isNaN(d)) return String(s);
  const j=new Date(d.getTime()+9*3600e3);
  const md=(j.getUTCMonth()+1)+'/'+j.getUTCDate();
  return withTime?md+' '+String(j.getUTCHours()).padStart(2,'0')+':'+String(j.getUTCMinutes()).padStart(2,'0'):md;
}
// 不明な期間・取得日時・母数はそのまま「不明」と出す（CLAUDE.md「集計期間・取得日時はデータ由来の値を表示し、不明な母数を明示する」）
function _metaProvenanceHtml(){
  const m=_metaMetadata;
  const period=m.from&&m.to?`${_metaFmtDate(m.from)}〜${_metaFmtDate(m.to)}`:'集計期間不明';
  const updated=m.fetched_at?`${_metaFmtDate(m.fetched_at,true)} 取得（日本時間）`:'取得日時不明';
  const sample=Number.isInteger(m.sample_size)&&m.sample_size>=0?`母数 ${m.sample_size}件`:'母数不明';
  const status=m.stale?'（更新に失敗したため、以前取得したデータを表示しています）':
    (m.refresh_error?'（最新データを取得できませんでした）':'');
  return esc(`TCG PORTAL の大会入賞データ・${period}・${updated}・${sample}${status}`);
}

function renderMetaTiers(){
  const el=document.getElementById('metaTierList');
  if(!_metaTiers.length){el.innerHTML=`<div class="meta-loading">${_metaMetadata.refresh_error?'環境データの取得に失敗しました':'データがありません'}</div><div class="meta-note">${_metaProvenanceHtml()}</div>`;return;}

  let rank=0; // GA用の表示順（クリック計測。カード名/デッキ名は送らない）

  if(_metaSort==='price'){
    // 安い順: 価格データを取得できたデッキ（上位シェア8件）だけをフラットに、
    // 全カード取得済み優先→一部未取得→価格情報なしの順、同グループ内は金額昇順
    // （app.py側 top_page.rank_decks の並び基準と揃える）
    const priced=_metaTiers.filter(t=>_metaPriceMap[t.name]);
    if(!priced.length){
      el.innerHTML=_metaPricesError
        ?'<div class="meta-loading">環境デッキの価格情報の取得に失敗しました</div>'
        :'<div class="meta-loading">価格データを読込中...</div>';
      el.innerHTML+=`<div class="meta-note">${_metaProvenanceHtml()}</div>`;
      return;
    }
    const sorted=[...priced].sort((a,b)=>{
      const da=_metaPriceMap[a.name], db=_metaPriceMap[b.name];
      const relA=da.total<=0?2:(da.missing_count>0?1:0);
      const relB=db.total<=0?2:(db.missing_count>0?1:0);
      if(relA!==relB) return relA-relB;
      return da.total-db.total;
    });
    let html='<div class="meta-deck-list mt-list">';
    // S-3(2): 未取得カードがあるデッキ（rel=1）・価格を計算できなかったデッキ（rel=2）は
    // 金額の大小に関わらず末尾へ回している。安い順の一覧だけを見ると理由が分からないため、
    // グループが切り替わる箇所に1行だけ区切りを入れる
    let prevRel=null;
    for(const d of sorted){
      const dd=_metaPriceMap[d.name];
      const rel=dd.total<=0?2:(dd.missing_count>0?1:0);
      if(prevRel!==null&&rel!==prevRel){
        const sepText=rel===1
          ?'ここから下は、一部のカードが未取得で実際はもう少し高くなります'
          :'ここから下は、価格を計算できませんでした';
        html+=`<div class="meta-deck-sep">${esc(sepText)}</div>`;
      }
      prevRel=rel;
      rank++;
      html+=_metaRowHtml(d,rank);
    }
    html+='</div>';
    html+=`<div class="meta-note">環境デッキを今すぐ組むといくらか（一部未取得のカードあり・目安）。<br>${_metaProvenanceHtml()}</div>`;
    el.innerHTML=html;
    return;
  }

  // 入賞傾向（既定）: 従来どおりTierグループ表示。各デッキ行に価格データがあれば併記する
  const groups={};
  for(const t of _metaTiers){
    const key=t.tier||99;
    if(!groups[key]) groups[key]=[];
    groups[key].push(t);
  }

  let html='';
  for(const tier of Object.keys(groups).sort((a,b)=>a-b)){
    const label=tier<=4?`Tier ${tier}`:'Other';
    const cls=tier<=4?`tier-${tier}`:'tier-4';
    html+=`<div class="meta-tier-group">
      <div class="mt-group-h"><span class="tier-badge ${cls}">${label}</span><small>${groups[tier].length}デッキ</small></div>
      <div class="meta-deck-list mt-list">`;
    for(const d of groups[tier]){
      rank++;
      html+=_metaRowHtml(d,rank);
    }
    html+=`</div></div>`;
  }
  // S-3(1): シェア上位デッキだけ価格を計算しているため、金額が出ないデッキがある。
  // 画面から理由が分かるよう、実際に価格を出せたデッキ数を添える
  const pricedCount=Object.keys(_metaPriceMap).length;
  const priceLimitNote=(_metaPricesLoaded&&pricedCount>0&&pricedCount<_metaTiers.length)
    ?`価格は使用率の高い上位${pricedCount}デッキだけ計算しています。<br>`
    :(_metaPricesError?'デッキの価格を取得できませんでした。時間をおいて開き直してください。<br>':'');
  html+=`<div class="meta-note">${priceLimitNote}${_metaProvenanceHtml()}</div>`;
  el.innerHTML=html;
}

// ── デッキの詳細（2026-10-21 モック meta-refresh-20261021 の A・B・C） ──
// A: PC は右の欄に出す（一覧の下へ飛ばない）。900px 以下は全面で開き、「一覧へ」で閉じる（一覧の位置はそのまま）
// B: 見出しにデッキ名・Tier・使用率・枚数・相場、主操作「シミュレーターで回す」（CLAUDE.md の入口設計「環境デッキは価格感の確認から一人回しへ」）、
//    ほかは「マイデッキに保存」と ⋯（デッキ全部を購入候補へ・デッキ画像・共有）
// C: カードの並びの右クリック（指はタップ）で「購入候補に入れる・カードの詳細・価格を比べる」
const _META_SHEET_MQ='(max-width: 900px)';
const _META_EXT_SVG='<svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M9 3h4v4M13 3 7 9M11 9v4H3V5h4"/></svg>';
const _META_PLAY_SVG='<svg width="13" height="13" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M4 2.5v11l9-5.5z"/></svg>';
const _META_MORE_SVG='<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><circle cx="3.5" cy="8" r="1.4"/><circle cx="8" cy="8" r="1.4"/><circle cx="12.5" cy="8" r="1.4"/></svg>';
function _metaToast(msg){ if(window.DeckHead&&typeof window.DeckHead.toast==='function') window.DeckHead.toast(msg); }

// 900px 以下で詳細を開いたまま広い幅になったら閉じる（全面表示の指定は狭い幅のときだけなので、ページのスクロール止めが残らないように。reviewer 指摘）
let _metaSheetMqReady=false;
function _metaWatchSheetMq(){
  if(_metaSheetMqReady||!window.matchMedia) return;
  _metaSheetMqReady=true;
  const mq=matchMedia(_META_SHEET_MQ);
  const onChange=()=>{ if(!mq.matches) closeMetaSheet(); };
  if(mq.addEventListener) mq.addEventListener('change',onChange); else if(mq.addListener) mq.addListener(onChange);
}
// ブラウザの「戻る」で全面の詳細だけを閉じる（開くときに履歴を1つ足す。reviewer 指摘。足していないと /#meta に直接来た人はサイトの外へ出ていた）
let _metaSheetHistory=false, _metaPopReady=false;
function _metaOnPop(){
  if(!_metaSheetHistory) return;
  _metaSheetHistory=false;
  closeMetaSheet(true);
}
function _openMetaSheet(){
  const pane=document.getElementById('metaPane');
  if(!pane) return;
  pane.scrollTop=0; // 別のデッキを選んだら、詳細は先頭から（PC の右の欄も）
  if(!window.matchMedia||!matchMedia(_META_SHEET_MQ).matches) return;
  _metaWatchSheetMq();
  if(!_metaPopReady){ _metaPopReady=true; window.addEventListener('popstate',_metaOnPop); } // 最初に開いたときに1回だけ登録
  if(!pane.classList.contains('is-open')&&history.pushState){
    try{ history.pushState({metaSheet:1},''); _metaSheetHistory=true; }catch(_){}
  }
  pane.classList.add('is-open');
  if(!_metaSheetKeyReady){ _metaSheetKeyReady=true; document.addEventListener('keydown',_metaSheetKey,true); } // 引き出しの Esc（deck-card-info.js）より先に見る
  document.documentElement.classList.add('meta-sheet-open'); // 後ろの一覧がスクロールしないように
}
// fromPop: ブラウザの「戻る」で閉じたとき（履歴はもう戻っているので戻さない）
function closeMetaSheet(fromPop){
  const pane=document.getElementById('metaPane');
  const wasOpen=!!(pane&&pane.classList.contains('is-open'));
  if(pane) pane.classList.remove('is-open');
  document.documentElement.classList.remove('meta-sheet-open');
  // 「一覧へ」・Esc で閉じたら、開いたときに足した履歴を戻す（「戻る」を2回押させない）。
  // 「戻る」で閉じたとき・タブの移動で閉じたとき（fromPop=true）は戻さない（タブの移動で変わったアドレスを戻してしまうため）
  if(fromPop){ _metaSheetHistory=false; }
  else if(wasOpen&&_metaSheetHistory){ _metaSheetHistory=false; try{ history.back(); }catch(_){} }
}
// Esc で全面の詳細を閉じる（最初に開いたときに1回だけ登録）。メニューを開いているときはメニューだけ閉じる（メニューの Esc は先に受けて止めている）
let _metaSheetKeyReady=false;
function _metaSheetKey(e){
  if(e.key!=='Escape') return;
  const pane=document.getElementById('metaPane');
  // カードの詳細の引き出し・デッキ画像の窓を開いているときは、そちらだけ閉じる（同じ Esc で全面の詳細まで閉じない）
  if(document.getElementById('deckCardDrawer')?.classList.contains('open')) return;
  if(document.querySelector('.dki-overlay')) return;
  if(pane&&pane.classList.contains('is-open')&&!document.querySelector('.wl-menu')) closeMetaSheet();
}

// 計算した合計（#metaDeckTotalPrice。calcDeckEstimate が書く）を見出しにも写す
let _metaTotalObserver=null;
function _metaWatchTotal(){
  const src=document.getElementById('metaDeckTotalPrice');
  if(!src) return;
  const copy=()=>{ const head=document.getElementById('metaHeadTotal'); if(head) head.textContent=src.textContent&&src.textContent!=='-'?src.textContent:'計算中…'; };
  copy();
  if(_metaTotalObserver) return;
  _metaTotalObserver=new MutationObserver(copy);
  _metaTotalObserver.observe(src,{childList:true,characterData:true,subtree:true});
}

async function loadMetaDeck(theme,rank){
  // R-1: 旧トップページの top_deck_click を統合先の環境デッキタブに合わせて改名。
  // カード名・デッキ名は送らずクリック順位のみ（従来方針を維持）
  if(rank) _ga('meta_deck_click',{rank:rank});
  _currentMetaTheme=theme;
  const detailEl=document.getElementById('metaDeckDetail');
  const btn=document.getElementById('meta-btn-'+theme);
  if(btn) btn.classList.add('loading');

  // 選択状態を更新（金の線）
  document.querySelectorAll('.meta-deck-btn').forEach(b=>b.classList.remove('selected'));
  if(btn) btn.classList.add('selected');

  document.getElementById('metaDeckEmpty')?.setAttribute('hidden','');
  detailEl.classList.remove('hidden');
  detailEl.innerHTML='<div class="meta-loading">デッキを読込中...</div>';
  // 前のデッキの計算結果・共有の材料を片付ける（読込中・データなし・失敗のときに前のカードが残らないように）
  _lastMetaDeckText='';
  DECK_CTX.meta._runId=(DECK_CTX.meta._runId||0)+1; // 計算中だった前のデッキの価格計算を止める
  document.getElementById('metaDeckResults')?.classList.add('hidden');
  {const l=document.getElementById('metaDeckCardList'); if(l) l.innerHTML='';}
  delete _deckShareData[DECK_CTX.meta.shareArea];
  {const sh=document.getElementById(DECK_CTX.meta.shareArea); if(sh) sh.innerHTML='';}
  _openMetaSheet();

  try{
    const res=await fetch('/api/meta/deck?theme='+encodeURIComponent(theme));
    const data=await res.json();
    if(_currentMetaTheme!==theme) return; // 読み込む間に別のデッキを選んだ

    const hasFullDeck=data.full_deck&&data.full_deck.length>0;
    if(!hasFullDeck&&(!data.cards||!data.cards.length)){
      detailEl.innerHTML='<div class="meta-loading">「'+esc(theme)+'」のデッキデータがありません</div>';
      return;
    }

    // デッキテキストと名前をセット
    _lastMetaDeckText=data.deck_text||'';
    _currentDeckName=theme;

    const t=_metaTiers.find(x=>x.name===theme)||{};
    const img=t.image?safeUrl(t.image):'';
    const cardCount=hasFullDeck?data.full_deck.length:data.cards.length;
    const totalCards=hasFullDeck?data.full_deck.reduce((s,c)=>s+c.qty,0):0;
    const meta=[t.tier&&t.tier<=4?'Tier '+t.tier:'',t.share!=null?`使用率 <b>${esc(t.share)}%</b>`:'',
      hasFullDeck?`<b>${cardCount}</b>種 <b>${totalCards}</b>枚`:`主要カード <b>${cardCount}</b>種`].filter(Boolean).join(' ・ ');
    // レシピ: 今のデータ（/api/meta/deck の recipes）は日付・大会名が空なので、番号で区別する（入っていれば出す）
    const recipes=(data.recipes||[]).filter(r=>safeUrl(r.url)).map((r,i)=>{
      const label=(r.title||('入賞レシピ '+(i+1)))+(r.date?' ('+r.date+')':'');
      return `<a class="mt-recipe" href="${escAttr(safeUrl(r.url))}" target="_blank" rel="noopener">${esc(label)} ${_META_EXT_SVG}</a>`;
    }).join('');

    detailEl.innerHTML=`
      <div class="mt-head">
        ${img?`<img src="${escAttr(img)}" alt="">`:'<span></span>'}
        <div class="mt-head-text"><h3 class="mt-title">${esc(theme)}</h3><div class="mt-meta">${meta}</div></div>
        <div class="mt-total"><small>デッキ相場</small><b id="metaHeadTotal">計算中…</b></div>
      </div>
      <div class="mt-actions">
        ${hasFullDeck?`<button type="button" class="mt-btn is-primary" onclick="openMetaDeckInSolitaire()">${_META_PLAY_SVG}シミュレーターで回す</button>
        <button type="button" class="mt-btn" onclick="saveMetaDeckToMydeck()">マイデッキに保存</button>`:''}
        <button type="button" class="mt-btn is-icon" id="metaMoreBtn" onclick="openMetaMore(this)" aria-label="その他の操作" aria-haspopup="menu">${_META_MORE_SVG}</button>
      </div>
      ${recipes?`<div class="mt-recipes">${recipes}</div>`:''}
      ${hasFullDeck?'':'<div class="meta-note">入賞レシピを取得できなかったため、採用率40%以上のカードを平均枚数で表示しています（手札誘発・汎用カード含む）。メインと EX の区別が無いので、シミュレーター・マイデッキへの保存はできません。</div>'}`;
    _metaWatchTotal();
    _metaInitCardMenu();

    // デッキ選択後に即・見積もり開始（マイデッキと同じ挙動）
    calcDeckEstimate(DECK_CTX.meta);
  }catch(e){
    if(_currentMetaTheme===theme) detailEl.innerHTML='<div class="meta-loading">読込に失敗しました</div>';
  }finally{
    if(btn) btn.classList.remove('loading');
  }
}

function _metaDeckCards(){
  const {main,ex}=parseDeckSections(_lastMetaDeckText||'');
  return {main,ex,all:[].concat(main,ex)};
}

// 「シミュレーターで回す」: マイデッキと同じ引き継ぎ（static/shared/deck-handoff.js）で、?from=meta として開く
async function openMetaDeckInSolitaire(){
  try{
    if(!_lastMetaDeckText) throw new Error('デッキを選んでください。');
    const {main,ex}=_metaDeckCards();
    const {storeHandoff}=await import('/static/shared/deck-handoff.js');
    storeHandoff({version:1,name:_currentDeckName||'環境デッキ',main,ex});
    _ga('sim_open',{from:'meta'});
    location.assign('/solitaire?from=meta');
  }catch(error){
    console.warn('[deck-handoff] 引継ぎデータを保存できませんでした',error);
    _metaToast('シミュレーターを開けませんでした。'+(error.message||''));
  }
}

// 「マイデッキに保存」: 新しいマイデッキとして保存する（同じ名前があれば「名前 (2)」。マイデッキの取り消しで使う付け方と同じ）
function saveMetaDeckToMydeck(){
  if(!_lastMetaDeckText) return;
  const {main,ex,all}=_metaDeckCards();
  const list=savedDecksGet();
  if(list.length>=50){ _metaToast('マイデッキは50件までです。先に使わないデッキを削除してください'); return; }
  const names=new Set(list.map(d=>d.name));
  let name=_currentDeckName||'環境デッキ';
  if(names.has(name)){ let k=2; while(names.has(name+' ('+k+')')) k++; name=name+' ('+k+')'; }
  list.push({id:'d_'+Date.now(),name,text:_lastMetaDeckText,main,ex,updated:Date.now()});
  savedDecksSet(list);
  trackDeckCards(all);
  _ga('deck_save',{where:'meta',kind:'new'});
  _metaToast('「'+name+'」をマイデッキに保存しました');
}

// 「デッキ全部を購入候補に入れる」（環境デッキには所持枚数が無いので「足りない分」ではなく全部）。1行99枚まで
function metaDeckAllToWish(){
  const {all}=_metaDeckCards();
  if(!all.length) return;
  const list=wishGet();
  const newRows=new Set(all.filter(c=>!list.some(w=>w.name===c.name&&!(w.rarity||''))).map(c=>c.name)).size;
  if(list.length+newRows>200){ _metaToast('購入候補は200件までです（あと'+Math.max(0,200-list.length)+'件）。先に候補を整理してください'); return; }
  let added=0;
  for(const c of all){
    const e=list.find(w=>w.name===c.name&&!(w.rarity||''));
    if(e){ const before=e.qty; e.qty=Math.min(99,e.qty+c.qty); added+=e.qty-before; }
    else{ const q=Math.min(99,c.qty); list.push({name:c.name,qty:q,rarity:''}); added+=q; }
  }
  wishSave(list);
  _wishPrices=null;
  trackDeckCards(all);
  _ga('wish_add',{source:'meta_deck',count:added});
  _metaToast(added?'「'+(_currentDeckName||'環境デッキ')+'」の'+added+'枚を購入候補に入れました':'すでに購入候補に入っています（1行99枚まで）');
}

// 詳細の ⋯（メニューの部品は購入候補と同じ _wishOpenMenu。index-wish.js）
function openMetaMore(btn){
  if(typeof _wishOpenMenu!=='function') return;
  if(_wishMenu&&_wishMenu.fromBtn===btn){ _wishCloseMenu(); return; }
  const n=_metaDeckCards().all.reduce((s,c)=>s+c.qty,0);
  // 共有は今までの欄（#metaDeckShareArea。renderDeckShareUI）のボタンを使う。合計が出るまでは画像だけ
  const share=document.getElementById('metaDeckShareArea');
  const pick=sel=>share?share.querySelector(sel):null;
  const x=pick('.share-btn-x'), line=pick('.share-btn-line'), copy=pick('.share-btn-copy'), image=pick('.share-btn-img');
  const items=[{label:'デッキ全部を購入候補に入れる',sub:n+'枚',fn:metaDeckAllToWish}];
  if(image) items.push({label:'デッキ画像を作る',fn:()=>image.click()});
  if(x||line||copy){
    items.push('sep');
    if(x) items.push({label:'X に投稿',fn:()=>x.click()});
    if(line) items.push({label:'LINE で送る',fn:()=>line.click()});
    if(copy) items.push({label:'投稿文をコピー',fn:()=>{ copy.click(); _metaToast('投稿文をコピーしました'); }});
  }
  _wishOpenMenu({fromBtn:btn,ownerEl:btn,ownerClass:'is-open',items});
}

// カードの詳細: マイデッキの引き出し（#deckCardDrawer）を使う。マイデッキの画面の中に置かれていて、
// 環境デッキを開いている間は隠れているので、画面の一番外へ移してから開く（固定位置の部品なのでマイデッキ側は変わらない）
function _metaOpenCardInfo(name,src){
  const drawer=document.getElementById('deckCardDrawer');
  if(drawer&&drawer.parentElement!==document.body) document.body.appendChild(drawer);
  // 押したクリックが引き出しの「外を押したら閉じる」に届いて、開いた直後に閉じないよう次の瞬間に開く（マイデッキの deck-card-ops.js と同じ）
  if(typeof window.openDeckCardInfo==='function') setTimeout(()=>window.openDeckCardInfo(name,src||''),0);
  else window.open('/card/'+encodeURIComponent(name),'_blank','noopener');
}

// C: カードの並び（#metaDeckCardList。行は描き直されるので箱で受ける）。PC は右クリック、指はタップでメニュー
function _metaInitCardMenu(){
  const list=document.getElementById('metaDeckCardList');
  if(!list||list.dataset.menuReady) return;
  list.dataset.menuReady='1';
  let touch=false;
  list.addEventListener('pointerdown',e=>{ touch=e.pointerType==='touch'; });
  const open=(cell,x,y,fromBtn)=>{
    if(typeof _wishOpenMenu!=='function') return;
    const name=cell.dataset.card;
    if(!name) return;
    const img=cell.querySelector('img');
    const inWish=wishGet().filter(c=>c.name===name).reduce((n,c)=>n+(c.qty||0),0);
    _wishOpenMenu({title:name,fromBtn,x,y,ownerEl:cell,ownerClass:'is-menu',items:[
      {label:'購入候補に入れる',sub:'候補に '+inWish+'枚',fn:()=>{
        wishAdd(name,1,'');
        _ga('wish_add',{source:'meta_card',count:1});
        _metaToast('「'+name+'」を購入候補に入れました');
      }},
      {label:'カードの詳細',fn:()=>_metaOpenCardInfo(name,img?img.src:'')},
      {label:'価格を比べる',fn:()=>window.open('/card/'+encodeURIComponent(name),'_blank','noopener')},
    ]});
  };
  list.addEventListener('contextmenu',e=>{
    const cell=e.target.closest('.deck-grid-cell');
    // 店へのリンク（カード名）の上ではブラウザのメニューを残す
    if(!cell||e.target.closest('.deck-card-name a')) return;
    e.preventDefault();
    open(cell,e.clientX,e.clientY,null);
  });
  list.addEventListener('click',e=>{
    if(!touch) return; // マウスの左クリックは今までどおりカードの価格ページへ
    const cell=e.target.closest('.deck-grid-cell');
    if(!cell||e.target.closest('.deck-card-name a')) return;
    e.preventDefault();
    if(_wishMenu&&_wishMenu.ownerEl===cell){ _wishCloseMenu(); return; }
    open(cell,0,0,cell);
  });
}

let _lastMetaDeckText='';

function applyMetaDeck(){
  // 環境デッキタブ内で見積もりを完結させる（他タブに依存しない）
  if(!_lastMetaDeckText) return;
  calcDeckEstimate(DECK_CTX.meta);
}

function applyMetaDeckToTextarea(){
  // マイデッキタブへレシピを送り、編集できるようにする
  document.getElementById('deckTextarea').value=_lastMetaDeckText;
  switchMode('mydeck','auto');
  // テキストエリアをハイライトして変化を伝える
  const ta=document.getElementById('deckTextarea');
  ta.style.borderColor='var(--accent)';
  ta.style.boxShadow='0 0 0 3px rgba(74,124,255,.2)';
  ta.scrollIntoView({behavior:'smooth',block:'center'});
  setTimeout(()=>{ta.style.borderColor='';ta.style.boxShadow='';},1500);
}

// ── 最新弾パック ──
// 指定コンテナ内の .pack-card-thumb[data-card] に画像遅延ロードを設定する汎用関数
let _thumbObserver=null;
function _setupThumbObserver(containerSelector){
  // 既存のオブザーバーに新しいコンテナの要素を追加（複数コンテナ対応）
  if(!_thumbObserver){
    _thumbObserver=new IntersectionObserver((entries)=>{
      entries.forEach(entry=>{
        if(!entry.isIntersecting) return;
        const el=entry.target;
        const name=el.dataset.card;
        if(!name||el.dataset.loaded) return;
        el.dataset.loaded='1';
        _thumbObserver.unobserve(el);
        fetch('/api/card-image?name='+encodeURIComponent(name))
          .then(r=>r.json())
          .then(d=>{
            if(d.url&&safeUrl(d.url)){
              const img=document.createElement('img');
              img.src=safeUrl(d.url);
              img.alt='';
              img.loading='lazy';
              img.onerror=function(){this.parentElement.style.display='none';};
              el.appendChild(img);
            }else{
              el.style.display='none';
            }
          })
          .catch(()=>{el.style.display='none';});
      });
    },{rootMargin:'200px'});
  }
  const container=document.querySelector(containerSelector);
  if(!container) return;
  container.querySelectorAll('.pack-card-thumb[data-card]').forEach(el=>{
    _thumbObserver.observe(el);
  });
}


// 最新弾タブ（パック）関連の処理は static/packs.js へ切り出し済み。
// 一括価格検索（bulkSearchPackPrices）もそちらに実装。
// 関数: loadPacks / renderPacks / loadPackCards / bulkSearchPackPrices / closePackSheet



// ── 旧プリセット（後方互換用・非表示）──
const PRESETS={};

function loadPreset(key){
  if(PRESETS[key]){
    document.getElementById('deckTextarea').value=PRESETS[key];
    switchMode('mydeck','auto');
  }
}

function clearDeck(){
  document.getElementById('deckTextarea').value='';
  document.getElementById('deckResults').classList.add('hidden');
  _currentMydeckCards={main:[],ex:[]};
  _currentMydeckText='';
  _currentDeckName='';
}

// opts.notOwned: 新着から入れる（まだ持っていないので所持枚数を0にしておく。deck-edit.js deckOpenAddPicker）
function addToDeck(name,btn,opts){
  // deck-edit.js 読込時は追加先ピッカー（編集中/保存済み/新規を選択・タブ遷移なし）を使う
  if(window.deckOpenAddPicker){ window.deckOpenAddPicker(name,btn,opts); return; }
  const ta=document.getElementById('deckTextarea');
  const lines=ta.value.split('\n').filter(l=>l.trim());
  // Check if already in list
  const exists=lines.some(l=>{
    const m=l.match(/^(\d+)\s+(.+)$/);
    return (m?m[2].trim():l.trim())===name;
  });
  if(exists){
    // Increment quantity
    ta.value=lines.map(l=>{
      const m=l.match(/^(\d+)\s+(.+)$/);
      if(m&&m[2].trim()===name) return `${parseInt(m[1])+1} ${m[2].trim()}`;
      if(l.trim()===name) return `2 ${name}`;
      return l;
    }).join('\n');
  }else{
    lines.push(name);
    ta.value=lines.join('\n');
  }
  // Flash feedback
  switchMode('mydeck','auto');
}

let _deckBuyMode=false;

async function calcDeckEstimate(ctx, opts){
  ctx = ctx || DECK_CTX.mydeck;
  opts = opts || {};  // {previewOnly:価格計算せず一覧のみ, dbOnly:DB相場のみ・リアルタイム補完なし}

  // main/ex を取得（mydeckはテキスト変更を検知して再解析、metaはテキストから生成）
  let main,ex;
  if(ctx===DECK_CTX.mydeck){
    const text=ctx.getText();
    if(text!==_currentMydeckText){
      _currentMydeckCards=parseDeckSections(text);
      _currentMydeckText=text;
    }
    main=_currentMydeckCards.main;
    ex=_currentMydeckCards.ex;
  }else{
    const parsed=parseDeckSections(ctx.getText());
    main=parsed.main;
    ex=parsed.ex;
  }
  if(![...main,...ex].length) return;
  // 計算ごとの番号（2026-10-21 reviewer 指摘）。待ちの間に別のデッキの計算が始まったら、古い計算はそこで止めて何も書かない
  // （環境デッキは一覧から続けて選べるようになり、前のデッキの価格・合計・共有の文が新しいデッキの表示に入っていた）
  const runId=ctx._runId=(ctx._runId||0)+1;
  const stale=()=>ctx._runId!==runId;
  // デッキ価格計算の利用計測（一覧表示だけのプレビューは計算していないので数えない）
  // 前のデッキの共有欄・画像データを消す（この計算が失敗したときに、前のデッキの画像を作れてしまわないように）
  delete _deckShareData[ctx.shareArea];
  {const shareEl=document.getElementById(ctx.shareArea); if(shareEl) shareEl.innerHTML='';}
  if(!opts.previewOnly) _ga('deck_calc',{
    deck:ctx===DECK_CTX.meta?'meta':'mydeck',
    method:opts.dbOnly?'db':'db_then_shop', side:'sell',
    card_count:[...main,...ex].reduce((s,c)=>s+(c.qty||1),0)});

  const resultsEl=document.getElementById(ctx.results);
  const listEl=document.getElementById(ctx.list);
  const totalEl=document.getElementById(ctx.total);
  const progEl=document.getElementById(ctx.prog);
  const totalLabel=document.querySelector('#'+ctx.results+' .deck-total-label');
  resultsEl.classList.remove('hidden');
  if(opts.previewOnly){
    // デッキ選択・取込時は価格計算しないので「取得中」を出さず未計算表示にする
    totalEl.textContent='-';
    totalLabel.textContent='デッキ合計（未計算）';
  }else{
    totalEl.textContent='取得中...';
    totalLabel.textContent='デッキ相場（直近データ）';
  }

  // メインデッキを種別ソート（モンスター→魔法→罠）してグリッド描画
  // マイデッキの編集モード中はユーザーの手動並び順を尊重し、自動ソートをスキップする
  // プレビュー時は種別ソート用のAPI(/api/card-types)も呼ばず、保存順のまま即描画する
  const skipSort=opts.previewOnly||(window._deckEditMode&&ctx===DECK_CTX.mydeck);
  const sortedMain=skipSort?main:await sortCardsByType(main);
  if(stale()) return;
  const cards=[...sortedMain,...ex];
  const mainCount=sortedMain.length;

  renderDeckGrid(cards,ctx,mainCount);
  loadDeckGridImages(listEl);

  // プレビューのみ（デッキ選択・PDF取込・購入候補変換時）：価格計算は走らせず、カード一覧だけ表示する
  if(opts.previewOnly){
    listEl.querySelectorAll('.deck-grid-cell.loading').forEach(el=>el.classList.remove('loading'));
    progEl.textContent=ctx===DECK_CTX.mydeck
      ?'「相場で計算」または「店舗から取得」で価格を表示します'
      :'「簡易計算（DB相場）」または「リアルタイム計算」を押すと価格を取得します';
    // 価格を計算する前でもデッキ画像は作れる（合計は画像作成側が DB 相場から取る。並びは保存順なので種別順に直す）
    // マイデッキの編集モード中は手で並べた順を尊重する（計算後の経路と同じ扱い）
    renderDeckShareUI(ctx,ctx.getName(),null,cards,{mainCount,sort:!(window._deckEditMode&&ctx===DECK_CTX.mydeck)});
    return;
  }

  const cardsParam=cards.map(c=>c.qty>1?`${c.qty} ${c.name}`:c.name).join('|');
  let total=0,found=0;
  const missing=[];// DBにデータがなかったカードのインデックス

  try{
    // Phase 1: DBから一括取得（POST でカード名を送り URL 長制限を回避）
    const res=await fetch('/api/deck-estimate',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cards:cardsParam})
    });
    const data=await res.json();
    if(stale()) return;
    if(data.error){progEl.textContent=data.error;return;}

    data.results.forEach((r,i)=>{
      const c=cards[i];
      const row=document.getElementById(ctx.rowPrefix+i);
      if(!row)return;
      if(r.best){
        const lineTotal=r.best.price*c.qty;
        total+=lineTotal;
        found++;
        row.className='deck-grid-cell';
        row.title=r.best.shop;
        row.querySelector('.deck-card-price').textContent=`¥${lineTotal.toLocaleString()}`;
        row.querySelector('.deck-card-shop').textContent=r.best.shop;
        row.querySelector('.deck-card-status').textContent=c.qty>1?`@¥${r.best.price.toLocaleString()}`:'';
        row.querySelector('.deck-card-name').innerHTML=r.best.url
          ?`<a href="${escAttr(safeUrl(r.best.url))}" target="_blank" rel="noopener">${esc(c.name)}</a>`:esc(c.name);
      }else{
        missing.push(i);
      }
    });
    const dbTotal=total;
    totalEl.textContent=`¥${dbTotal.toLocaleString()}`;

    if(!missing.length){
      progEl.textContent=`完了 — ${found}/${cards.length}枚の相場を取得`;
      renderDeckShareUI(ctx,ctx.getName(),total,cards,{mainCount,missingKinds:0});
      return;
    }

    // 簡易計算（DBオンリー）：DBに無いカードはリアルタイム補完せず「データなし」と表示する
    if(opts.dbOnly){
      missing.forEach(i=>{
        const row=document.getElementById(ctx.rowPrefix+i);
        if(!row)return;
        row.classList.remove('loading');
        row.classList.add('error');
        const priceEl=row.querySelector('.deck-card-price');
        if(priceEl) priceEl.textContent='データなし';
      });
      progEl.textContent=`完了 — ${found}/${cards.length}枚の相場を取得（${missing.length}枚はDB未収録）`;
      renderDeckShareUI(ctx,ctx.getName(),total,cards,{mainCount,missingKinds:missing.length});
      return;
    }

    // Phase 2: データなしカードだけリアルタイム検索
    totalEl.innerHTML=`¥${dbTotal.toLocaleString()} <span style="font-size:.7em;color:var(--text-d)">+ ${missing.length}枚検索中</span>`;
    progEl.textContent=`${found}枚を即時取得 / 残り${missing.length}枚をリアルタイム検索中...`;

    // 検索対象店舗はサーバ既定（DEFAULT_SHOPS）に一本化。shopsパラメータは送らない
    const missingParam=missing.map(i=>{const c=cards[i];return c.qty>1?`${c.qty} ${c.name}`:c.name}).join('|');
    const params=new URLSearchParams({cards:missingParam});

    // 混雑応答なら「順番待ち」を出して数秒後に取り直す（ShopBusy）
    const sseRes=await ShopBusy.fetch('/api/deck?'+params.toString(),undefined,{
      isStale:stale,
      onWait:()=>{ progEl.textContent=`${found}枚を即時取得 / ${ShopBusy.WAIT_TEXT}`; },
      onResume:()=>{ progEl.textContent=`${found}枚を即時取得 / 残り${missing.length}枚をリアルタイム検索中...`; },
    });
    if(stale()){ try{ sseRes.body.cancel(); }catch(_){} return; }
    const reader=sseRes.body.getReader();
    const decoder=new TextDecoder();
    let buffer='',sseFound=0,sseNotFound=0;

    while(true){
      const{done,value}=await reader.read();
      if(stale()){ reader.cancel().catch(()=>{}); return; }
      if(done)break;
      buffer+=decoder.decode(value,{stream:true});
      const chunks=buffer.split('\n\n');
      buffer=chunks.pop()||'';

      for(const chunk of chunks){
        if(!chunk.startsWith('data: '))continue;
        const d=JSON.parse(chunk.slice(6));

        if(d.type==='card_start'){
          // SSE indexをUI indexにマッピング
          const uiIdx=missing[d.index];
          const row=document.getElementById(ctx.rowPrefix+uiIdx);
          if(row)row.querySelector('.deck-card-status').textContent='検索中...';
        }
        else if(d.type==='card_done'){
          const uiIdx=missing[d.index];
          const c=cards[uiIdx];
          const row=document.getElementById(ctx.rowPrefix+uiIdx);
          if(!row)continue;

          if(d.tcg_preview){
            // TCG先行カード（英語版で先に出て日本に無いカード）は価格が無い（サーバーは店舗へ取りに行かない）
            sseNotFound++;
            row.className='deck-grid-cell';
            row.querySelector('.deck-card-status').textContent='TCG先行・価格なし';
          }else if(d.best){
            const lineTotal=d.best.price*c.qty;
            total+=lineTotal;
            sseFound++;
            row.className='deck-grid-cell';
            row.title=d.best.shop;
            row.querySelector('.deck-card-price').textContent=`¥${lineTotal.toLocaleString()}`;
            row.querySelector('.deck-card-shop').textContent=d.best.shop;
            row.querySelector('.deck-card-status').textContent=c.qty>1?`@¥${d.best.price.toLocaleString()}`:'';
            row.querySelector('.deck-card-name').innerHTML=d.best.url
              ?`<a href="${escAttr(safeUrl(d.best.url))}" target="_blank" rel="noopener">${esc(c.name)}</a>`:esc(c.name);
          }else{
            sseNotFound++;
            row.className='deck-grid-cell error';
          }
          SearchStatus.annotate(row,d);
          const remaining=missing.length-(sseFound+sseNotFound);
          if(remaining>0){
            totalEl.innerHTML=`¥${total.toLocaleString()} <span style="font-size:.7em;color:var(--text-d)">+ ${remaining}枚検索中</span>`;
          }else{
            totalEl.textContent=`¥${total.toLocaleString()}`;
          }
          progEl.textContent=`${found}枚を即時取得 / 残り${missing.length}枚中${sseFound+sseNotFound}枚完了`;
        }
        else if(d.type==='done'){
          totalEl.textContent=`¥${total.toLocaleString()}`;
          const totalFound=found+sseFound;
          const totalMissing=sseNotFound;
          progEl.textContent=`完了 — ${totalFound}/${cards.length}枚の価格を取得${totalMissing>0?` (${totalMissing}枚未取得)`:''}`;
          renderDeckShareUI(ctx,ctx.getName(),total,cards,{mainCount,missingKinds:sseNotFound});
        }
      }
    }
  }catch(e){
    progEl.textContent='エラーが発生しました。再度お試しください。';
  }
}

// ── デッキシェアUI ──
// ctx別のシェアデータを保持（画像生成ボタン用）
const _deckShareData={};

// メイン/EX 各セクションの小計（表示中の行価格の合計）をセクション見出しに付与する。
// 計算ロジックには手を入れず、描画済みの行価格を読み取って集計する（DB相場/リアルタイム両対応）。
function _updateSectionSubtotals(ctx){
  const listEl=document.getElementById(ctx.list);
  if(!listEl)return;
  listEl.querySelectorAll('.deck-grid-section').forEach(sec=>{
    let sum=0,priced=0;
    sec.querySelectorAll('.deck-card-price').forEach(p=>{
      const n=(p.textContent||'').replace(/[^0-9]/g,'');
      if(n){sum+=parseInt(n,10);priced++;}
    });
    const label=sec.querySelector('.deck-grid-section-label');
    if(!label)return;
    let st=label.querySelector('.deck-section-subtotal');
    if(priced>0){
      if(!st){st=document.createElement('span');st.className='deck-section-subtotal';label.appendChild(st);}
      st.textContent=` ／ 小計 ¥${sum.toLocaleString()}`;
    }else if(st){st.remove();}
  });
}

function renderDeckShareUI(ctx,deckName,total,cards,info){
  info=info||{};
  _updateSectionSubtotals(ctx); // セクション小計を最新化（簡易計算・リアルタイム両方の確定時に通る）
  const el=document.getElementById(ctx.shareArea);
  if(!el)return;
  const mainCount=Number.isInteger(info.mainCount)?info.mainCount:cards.length;
  _deckShareData[ctx.shareArea]={
    deckName,total,
    main:cards.slice(0,mainCount),ex:cards.slice(mainCount),
    missingKinds:info.missingKinds||0,sort:!!info.sort,
  };
  const imgBtn=`<button class="share-btn share-btn-img" onclick="generateDeckImageUI('${escAttr(escJs(ctx.shareArea))}')">デッキ画像を作成</button>`;

  // 価格を計算する前（デッキを選んだだけ）は、合計の入る投稿文は出さず画像だけ作れるようにする
  if(total==null){
    // マイデッキは見出しの「デッキ画像」から作れるので、計算前は出さない（deck-head.js。_deckShareData は上で用意済み）
    if(ctx===DECK_CTX.mydeck){el.innerHTML='';return;}
    el.innerHTML=`
    <div class="deck-share-title">シェア・投稿</div>
    <div class="share-btns">${imgBtn}</div>`;
    return;
  }

  // 投稿テキスト: デッキ名 + 合計 + サービスURL
  const totalCards=cards.reduce((s,c)=>s+c.qty,0);
  const shareText=`【${deckName}】デッキ相場 ¥${total.toLocaleString()}（${totalCards}枚） #カード相場 #遊戯王`;
  const shareUrl=location.origin;
  const tweetHref=`https://x.com/intent/tweet?text=${encodeURIComponent(shareText)}&url=${encodeURIComponent(shareUrl)}`;
  const lineHref=`https://social-plugins.line.me/lineit/share?url=${encodeURIComponent(shareUrl)}&text=${encodeURIComponent(shareText)}`;

  el.innerHTML=`
    <div class="deck-share-title">シェア・投稿</div>
    <div class="share-btns">
      <a class="share-btn share-btn-x" href="${tweetHref}" target="_blank" rel="noopener">𝕏 テキスト投稿</a>
      <a class="share-btn share-btn-line" href="${lineHref}" target="_blank" rel="noopener">LINE</a>
      <button class="share-btn share-btn-copy" onclick="_shareCopy('${escAttr(escJs(shareText))}',this)">コピー</button>
      ${imgBtn}
    </div>`;
}

// デッキ画像はブラウザの中で作る（static/shared/deck-image.js。以前のサーバー合成 /api/deck-image は廃止）
async function generateDeckImageUI(shareAreaId){
  const data=_deckShareData[shareAreaId];
  if(!data)return;
  try{
    const {openDeckImageDialog}=await import('/static/shared/deck-image.js');
    openDeckImageDialog({
      deckName:data.deckName,main:data.main,ex:data.ex,
      total:data.total,missingKinds:data.missingKinds,sort:data.sort,
    });
  }catch(e){
    alert('デッキ画像の機能を読み込めませんでした。ページを再読み込みしてお試しください。');
  }
}

async function calcDeck(buyMode){
  if(buyMode!==undefined) _deckBuyMode=buyMode;
  const isBuy=_deckBuyMode;

  const text=document.getElementById('deckTextarea').value;
  // テキストが変更されていれば再解析（[EX]区切りがあればエクストラデッキを分離）
  if(text!==_currentMydeckText){
    _currentMydeckCards=parseDeckSections(text);
    _currentMydeckText=text;
  }
  const {main,ex}=_currentMydeckCards;
  if(![...main,...ex].length) return;
  // デッキ価格計算の利用計測（店舗から直接取得）
  _ga('deck_calc',{deck:'mydeck', method:'shop', side:isBuy?'buy':'sell',
    card_count:[...main,...ex].reduce((s,c)=>s+(c.qty||1),0)});

  const ctx=DECK_CTX.mydeck;
  const resultsEl=document.getElementById('deckResults');
  const listEl=document.getElementById('deckCardList');
  const totalEl=document.getElementById('deckTotalPrice');
  const progEl=document.getElementById('deckProgress');
  const totalLabel=document.querySelector('#deckResults .deck-total-label');
  resultsEl.classList.remove('hidden');
  totalEl.textContent='計算中...';
  totalLabel.textContent=isBuy?'買取合計（推定）':'デッキ合計最安値（推定）';

  // メインデッキを種別ソート（モンスター→魔法→罠）してグリッド描画
  // マイデッキの編集モード中はユーザーの手動並び順を尊重し、自動ソートをスキップする
  const sortedMain=(window._deckEditMode&&ctx===DECK_CTX.mydeck)?main:await sortCardsByType(main);
  const cards=[...sortedMain,...ex];
  const mainCount=sortedMain.length;

  renderDeckGrid(cards,ctx,mainCount);
  loadDeckGridImages(listEl);

  // Build request
  // 検索対象店舗はサーバ既定（isBuy: DEFAULT_BUYBACK_SHOPS / 販売: DEFAULT_SHOPS）に一本化。
  // shopsパラメータは送らない（非表示DOMからの読み取りをやめる、W-1）
  const cardsParam=cards.map(c=>c.qty>1?`${c.qty} ${c.name}`:c.name).join('|');
  const params=new URLSearchParams({cards:cardsParam});

  const apiUrl=isBuy?'/api/deck-buy?':'/api/deck?';
  let total=0,found=0,notFound=0;

  try{
    const res=await ShopBusy.fetch(apiUrl+params.toString(),undefined,{
      onWait:()=>{ progEl.textContent=ShopBusy.WAIT_TEXT; },
    });
    const reader=res.body.getReader();
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

        if(d.type==='card_start'){
          const row=document.getElementById('deck-row-'+d.index);
          if(row){row.className='deck-grid-cell loading';row.querySelector('.deck-card-status').textContent='検索中...';}
          progEl.textContent=`${d.index+1}/${cards.length} 検索中...`;
        }
        else if(d.type==='card_done'){
          const c=cards[d.index];
          const row=document.getElementById('deck-row-'+d.index);
          if(!row)continue;

          if(d.tcg_preview){
            // TCG先行カード（英語版で先に出て日本に無いカード）は価格が無い（サーバーは店舗へ取りに行かない）
            notFound++;
            row.className='deck-grid-cell';
            row.querySelector('.deck-card-status').textContent='TCG先行・価格なし';
          }else if(d.best){
            const unitPrice=d.best.price;
            const lineTotal=unitPrice*c.qty;
            total+=lineTotal;
            found++;
            row.className='deck-grid-cell';
            row.title=d.best.shop;
            const priceEl=row.querySelector('.deck-card-price');
            priceEl.textContent=`¥${lineTotal.toLocaleString()}`;
            if(isBuy) priceEl.style.color='var(--green)';
            row.querySelector('.deck-card-shop').textContent=d.best.shop;
            row.querySelector('.deck-card-status').textContent=c.qty>1?`@¥${unitPrice.toLocaleString()}`:'';
            row.querySelector('.deck-card-name').innerHTML=d.best.url
              ?`<a href="${escAttr(safeUrl(d.best.url))}" target="_blank" rel="noopener">${esc(c.name)}</a>`
              :esc(c.name);
          }else{
            notFound++;
            row.className='deck-grid-cell error';
          }
          SearchStatus.annotate(row,d);
          totalEl.textContent=`¥${total.toLocaleString()}`;
        }
        else if(d.type==='done'){
          const modeLabel=isBuy?'買取価格':'販売価格';
          progEl.textContent=`完了 — ${found}/${cards.length}枚の${modeLabel}を取得${notFound>0?` (${notFound}枚未取得)`:''}`;
          _updateSectionSubtotals(ctx); // セクション小計を最新化（リアルタイム計算の確定時）
          const deckShareText=isBuy
            ?`買取合計 ¥${total.toLocaleString()}（${cards.length}枚）！ 5店舗最高値で見積もりました`
            :`デッキ合計 ¥${total.toLocaleString()}（${cards.length}枚）！ 6店舗最安値で見積もりました`;
          const deckShareUrl=location.origin;
          progEl.innerHTML+=`<div class="share-btns" style="margin-top:8px">
            <a class="share-btn share-btn-x" href="https://x.com/intent/tweet?text=${encodeURIComponent(deckShareText)}&url=${encodeURIComponent(deckShareUrl)}" target="_blank" rel="noopener">𝕏 シェア</a>
            <a class="share-btn share-btn-line" href="https://social-plugins.line.me/lineit/share?url=${encodeURIComponent(deckShareUrl)}&text=${encodeURIComponent(deckShareText)}" target="_blank" rel="noopener">LINE</a>
            <button class="share-btn share-btn-copy" onclick="copyShareText('${escAttr(escJs(deckShareText))} ${escAttr(escJs(deckShareUrl))}')">コピー</button>
          </div>`;
        }
      }
    }
  }catch(e){
    progEl.textContent='エラーが発生しました。再度お試しください。';
  }
}

// S-1: 買取専用の検索窓・店舗トグル・結果表示・結果テーブル（#buyQ/#buyBtn/#buySuggestDrop/
// #buyProg/#buyResults/#buyHero/#buySumCards/#buyTbl/#buyTbody とその描画JS）は廃止した。
// カード個別の買取価格は #buyInline（検索結果に併置、C-2）が担う。
// W-1（2026-09-01）: マイデッキ/環境デッキの「買取合計」計算(isBuy分岐)も含め、
// 店舗集合の非表示DOM読み取りを全廃しサーバ既定（DEFAULT_SHOPS/DEFAULT_BUYBACK_SHOPS）
// に一本化した。getSelectedShops()/getBuySelectedShops()相当の関数はもう存在しない

// 保存済みデッキを変更せず、クリック時の編集内容を同じタブへ渡す。
async function openCurrentDeckInSolitaire(){
  try{
    const text=document.getElementById('deckTextarea').value;
    if(text.length>12000)throw new Error('デッキ情報が大きすぎます。メイン60枚・EX15枚以内にしてください。');
    const snapshot={version:1,name:_currentDeckName||'マイデッキ',...parseDeckSections(text)};
    // 保存済みデッキを開いているなら、シミュレーターの「変更を保存」がそのデッキへ上書きできるよう ID も渡す
    if(window._currentSavedDeckId)snapshot.savedId=window._currentSavedDeckId;
    const {storeHandoff}=await import('/static/shared/deck-handoff.js');
    storeHandoff(snapshot);
    _ga('sim_open',{from:'mydeck',linked:snapshot.savedId?1:0});
    location.assign('/solitaire?from=mydeck');
  }catch(error){
    console.warn('[deck-handoff] 引継ぎデータを保存できませんでした',error);
    alert('シミュレーターを開けませんでした。'+(error.message||'ブラウザの一時保存を確認してください。'));
  }
}
