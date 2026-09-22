// 元は templates/index.html のインライン <script>（2026-09-22 バッチCで外部化）。
// Jinja 依存の値は index.html 側の小さなインライン script が定義する。
// _ga()は<head>先頭で定義済み（GA4計測用の薄いラッパ。G-7: 早期クリック対策で<head>に移設）
let D=[],sort={k:'price',a:true},rarityFilter='',cardImgUrl='',cardImgSmall='',_konamiDbUrl='';
const TABLE_PAGE_SIZE=20; // F-11: 一覧の既定表示件数（もっと見るの増分）。散在していたマジックナンバー20を集約
let tableShowCount=TABLE_PAGE_SIZE; // B-4: 一覧の既定表示件数（20行+もっと見る）


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
  window.open('https://x.com/intent/tweet?text='+encodeURIComponent(text+' #TCGYM '+location.origin),'_blank');
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
    const text=nameEl.textContent;
    nameEl.innerHTML='<a href="'+_konamiDbUrl+'" target="_blank" rel="noopener" style="color:var(--text);text-decoration:none;border-bottom:1px dashed var(--text-d)">'+text+' <span style="font-size:.65rem;color:var(--text-d)">📋 公式DB</span></a>';
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
  ['buyInline','sumCards','rarityFilter','rfBanner','p-all','p-rarity'].forEach(id=>{
    const el=document.getElementById(id);
    if(el) el.style.display=visible?'':'none';
  });
  const controls=document.querySelector('#results .controls');
  if(controls) controls.style.display=visible?'':'none';
}

// 未発売カードを価格検索せずカード情報のみ表示する
// 効果テキストの改行正規化: 元データ(YGOResources)は改行を文字列 "<br>" で持つものが多く
// （2026-09-04 本番DB実測: 14,241枚中10,205枚）、esc() でそのまま文字として表示されていた。
// 一人回しの card-info-panel.js（E-1）と同じ変換。.card-detail-effect は pre-wrap なので改行文字で行分けされる。
// ※トップレベルに置くこと（_renderProxyDetail と _fetchCardInfo の両方から参照する）
function _normalizeBreaks(text){return String(text??'').replace(/<br\s*\/?>/gi,'\n');}

function showUnreleasedCard(name){
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
  cardImgUrl='';cardImgSmall='';D=[];rarityFilter='';tableShowCount=TABLE_PAGE_SIZE;
  _resetBuyInline(); // C-2: 前回検索の買取価格が残ったまま未発売カード表示に切り替わるのを防ぐ
  _updateRarityBanner(); // F-4: 前回検索の絞り込みバナーが残ったまま未発売カード表示に切り替わるのを防ぐ

  document.getElementById('empty').classList.add('hidden');
  document.getElementById('results').classList.remove('hidden');
  _setPriceSectionsVisible(false); // 価格テーブル等を隠す

  const hero=document.getElementById('cardHero');
  hero.classList.remove('hidden');
  document.getElementById('heroImgWrap').innerHTML='<div style="width:140px;height:204px;background:var(--bg-card)"></div>';
  const heroInfo=document.getElementById('heroInfo');
  heroInfo.innerHTML=`<div class="card-hero-name">${esc(name)} <span class="pack-unreleased-badge">発売前</span></div>
    <div class="card-hero-meta">
      <span style="color:var(--text-m)">このカードはまだ発売前のため、販売価格情報はありません。</span>
    </div>
    <!-- 2026-09-04: 既定で開く（B-2 のスマホ縦長対策で畳んでいたが、PC でも隠れて読めないため全幅で開く裁定） -->
    <details class="card-info-details" id="cardDetailDetails" open hidden>
      <summary>カード情報（効果テキスト）</summary>
      <div id="cardDetail" class="card-detail"></div>
    </details>
    <div class="share-btns">
      <button onclick="addToDeck('${escAttr(escJs(name))}')" style="padding:4px 12px;font:.72rem var(--font);font-weight:600;color:var(--accent-l);background:none;border:1px solid var(--accent);border-radius:4px;cursor:pointer">+ デッキに追加</button>
      <button onclick="wishAddFromBtn(this,'${escAttr(escJs(name))}',1)" style="padding:4px 12px;font:.72rem var(--font);font-weight:600;color:var(--accent-l);background:none;border:1px solid var(--accent);border-radius:4px;cursor:pointer">+ 購入候補に追加</button>
    </div>`;

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
  _fetchCardInfo(name);
}

// 未発売カードの proxy データからカード情報パネルを描画する
// F-6: <details id="cardDetailDetails">は既定hiddenにしてある。中身を書いた側でここを呼び、
// 「カード情報（効果テキスト）」の見出しだけ空で残る（情報DB未収録カード等）のを防ぐ
function _showCardDetail(){
  document.getElementById('cardDetailDetails')?.removeAttribute('hidden');
}

function _renderProxyDetail(p){
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
  if(p.pendulum_effect) html+=`<div class="card-detail-effect"><span class="card-detail-pendulum-label">【ペンデュラム効果】</span>${esc(_normalizeBreaks(p.pendulum_effect))}</div>`;
  if(p.effect_text) html+=`<div class="card-detail-effect">${esc(_normalizeBreaks(p.effect_text))}</div>`;
  const meta=[p.product_name,p.release_date].filter(Boolean).join(' / ');
  if(meta) html+=`<div class="card-detail-print" style="margin-top:6px">${esc(meta)}</div>`;
  det.innerHTML=html;
  _showCardDetail();
}

function _fetchCardInfo(cardName){
  fetch('/api/card-info?name='+encodeURIComponent(cardName))
    .then(r=>r.json())
    .then(data=>{
      // 未発売カード（proxy）は専用描画。found:false でもカード情報を表示する
      if(data.kind==='proxy'&&data.proxy){_renderProxyDetail(data.proxy);return;}
      if(!data.found){fetchCardImage(cardName);return;}
      if(data.konami_id) _applyKonamiLink(data.konami_id);
      const det=document.getElementById('cardDetail');
      if(!det) return;
      const printSpan=data.latest_print?`<span class="card-detail-print">${esc(data.latest_print)}</span>`:'';
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
        html+=`<div class="cdm-row cdm-sub"><span>${statLine}</span>${limitBadge}${printSpan}</div>`;
      } else {
        html+=`<div class="cdm-row">${esc(data.card_type+(data.property?` [${data.property}]`:'')||'')}${limitBadge}${printSpan}</div>`;
      }
      html+='</div>';
      if(data.pendulum_effect) html+=`<div class="card-detail-effect"><span class="card-detail-pendulum-label">【ペンデュラム効果】</span>${esc(_normalizeBreaks(data.pendulum_effect))}</div>`;
      if(data.effect_text) html+=`<div class="card-detail-effect">${esc(_normalizeBreaks(data.effect_text))}</div>`;
      det.innerHTML=html;
      _showCardDetail();
    })
    .catch(()=>{fetchCardImage(cardName);});
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

function _applyCardImages(heroSrc,thumbSrc,source){
  const wrap=document.getElementById('heroImgWrap');
  const safeHero=safeUrl(heroSrc);
  if(wrap&&safeHero&&!wrap.querySelector('img')){
    wrap.innerHTML='<img src="'+escAttr(safeHero)+'" alt="" loading="lazy">';
    // 発売済みクリーン画像にのみ自サイト透かしを付与（SAMPLE画像には付けない）
    if(source!=='official_sample'){ wrap.classList.add('wm-released'); }
    else { wrap.classList.remove('wm-released'); }
  }
  _fillSmallImages(thumbSrc);
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

function _fillSmallImages(src){
  src=safeUrl(src);
  if(!src)return;
  document.querySelectorAll('.ritem-img').forEach(function(el){
    el.innerHTML='<img src="'+escAttr(src)+'" alt="" loading="lazy">';
  });
  document.querySelectorAll('.tbl-img').forEach(function(el){
    el.innerHTML='<img src="'+escAttr(src)+'" alt="" loading="lazy">';
  });
}

// J-1と同様: 販売検索SSEにも世代管理を入れる。旧ストリームのイベントが新しい検索結果を
// 上書きしないよう、_searchGenを世代トークンとして使う（buyInlineの_buyInlineES/_buyInlineGen/
// _buyInlineTimeoutIdと同じ形）
let _searchES=null;        // 進行中のEventSource参照。新しい検索を張る前に確実に閉じるために保持する
let _searchGen=0;          // 世代トークン。close()後も配送されうる旧SSEのイベントを無効化する
let _searchTimeoutId=null; // タイムアウトタイマーの参照。新しい検索を張る前に確実にclearTimeoutする

function _newSearchGen(){ // 世代加算＝旧ストリーム破棄、を不可分にする（片方だけ進むと旧SSEが無限再接続する）
  if(_searchES){ try{_searchES.close();}catch(e){} _searchES=null; }
  if(_searchTimeoutId){ clearTimeout(_searchTimeoutId); _searchTimeoutId=null; }
  return ++_searchGen;
}

function doSearch(opts){
  // opts: {validated:true} サジェスト選択やvalidate済み, {confirmed:true} DB未登録を承認済み,
  //       {trigger:'user'|'pageload'|'popstate'|...} G-2: 計測用。省略時は'user'（検索窓からの手動検索）
  //       {_gen} /api/validateを挟む再帰呼び出しが、入口で採番した世代を引き継ぐための内部用引数
  opts=opts||{};
  const trigger=opts.trigger||'user';
  const q=document.getElementById('q').value.trim();
  if(!q)return;
  if(q.length<2){
    alert('カード名を2文字以上入力してください');
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
    fetch('/api/validate?q='+encodeURIComponent(q))
      .then(r=>r.json())
      .then(d=>{
        if(gen!==_searchGen) return; // 世代管理: 古い応答が#q/btnを触らないようにする
        btn.disabled=false;btn.textContent='検索';
        if(d.valid){
          document.getElementById('q').value=d.name;
          // 未発売カードは価格検索せず、カード情報のみ表示する
          // G-2: 検索意図はあったが未発売のため検索に至らなかったケースも計上する（逆方向の欠測対策）
          if(d.unreleased){_ga('search_submit',{mode:'sell',trigger:trigger,result:'unreleased'});showUnreleasedCard(d.name);return;}
          doSearch({validated:true,trigger:trigger,_gen:gen});
        }else if(d.suggestion){
          if(confirm('もしかして: '+d.suggestion+'\n\nこのカード名で検索しますか?')){
            document.getElementById('q').value=d.suggestion;
            doSearch({validated:true,trigger:trigger,_gen:gen});
          }
        }else{
          // TODO: _correct_cardname()が部分一致候補を返せるようになったらsuggestion分岐が有効になる
          if(confirm('該当するカード名が見つかりません。\nそのまま検索しますか?')){
            doSearch({confirmed:true,trigger:trigger,_gen:gen});
          }
        }
      })
      .catch(()=>{if(gen!==_searchGen)return;btn.disabled=false;btn.textContent='検索';});
    return;
  }
  // 世代管理: 古い検索の /api/validate の戻りが後から来た場合は、画面状態（ボタン無効化・
  // 結果クリア・進捗リセット）を触る前にここで打ち切る（新しい検索の表示を壊さない）
  if(gen!==_searchGen) return;
  _ga('search_submit',{mode:'sell',trigger:trigger});
  const btn=document.getElementById('btn');
  btn.disabled=true;btn.textContent='検索中...';
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
  if(cardHero) cardHero.classList.add('hidden');
  const sumCards=document.getElementById('sumCards');
  if(sumCards) sumCards.innerHTML='';

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

  D=[];rarityFilter='';tableShowCount=TABLE_PAGE_SIZE;
  _resetBuyInline(); // C-2: 新しい検索をしたら買取セクションは閉じた未取得状態に戻す（前のカードの買取価格が残ると誤情報になるため）
  _updateRarityBanner(); // F-4: 前回検索の絞り込みバナーが残ったまま次の検索結果が出るのを防ぐ
  const params=new URLSearchParams({q});
  if(opts.confirmed)params.append('confirmed','true');

  // 世代管理: 古いvalidateの戻りが後から来てここに到達しても、旧世代のストリームは張らない
  // （旧ストリームのclose/clearTimeoutは_newSearchGen()側で採番と不可分に行われている）
  if(gen!==_searchGen) return;

  const es=new EventSource('/api/search?'+params.toString());
  _searchES=es;
  // 60秒で応答がなければタイムアウト
  function _searchTimeoutFail(){
    if(gen!==_searchGen) return; // 世代管理: 別カードの検索で世代が進んでいたら無視
    es.close();_searchES=null;_searchTimeoutId=null;btn.disabled=false;btn.textContent='検索';
    prog.style.display='none';
    document.getElementById('empty').classList.remove('hidden');
    // Q-10: #emptyのinnerHTML全置換をやめ、専用要素だけを書き換える（ランキングDOMを壊さない）
    const _errEl=document.getElementById('emptyError');
    _errEl.textContent='検索がタイムアウトしました。再度お試しください。';
    _errEl.classList.remove('hidden');
  }
  _searchTimeoutId=setTimeout(_searchTimeoutFail,60000);
  let partialShown=false;
  es.onmessage=e=>{
    if(gen!==_searchGen) return; // 世代管理: closeしても配送されうる旧世代のイベントを無効化する
    clearTimeout(_searchTimeoutId);
    _searchTimeoutId=setTimeout(_searchTimeoutFail,60000);
    const d=JSON.parse(e.data);
    if(d.type==='corrected'){
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
    }else if(d.type==='shop_done'){
      const el=_searchProgStep(d.shop);
      el.className='pstep done';el.querySelector('.pstat').textContent=d.count+'件'+(d.cached?' (cache)':'');
      // 逐次表示: 結果が届いたらテーブルに追加
      if(d.results&&d.results.length>0){
        D=D.concat(d.results);
        if(!partialShown){
          document.getElementById('results').classList.remove('hidden');
          document.getElementById('empty').classList.add('hidden');
          partialShown=true;
        }
        renderTable();
      }
    }else if(d.type==='shop_error'){
      const el=_searchProgStep(d.shop);
      el.className='pstep error';el.querySelector('.pstat').textContent='エラー';
    }else if(d.type==='done'){
      clearTimeout(_searchTimeoutId);_searchTimeoutId=null;
      es.close();_searchES=null;D=d.results;
      // 補正後のカード名で検索欄を更新
      if(d.corrected_name) document.getElementById('q').value=d.corrected_name;
      // J-2: 買取セクションの取得キーは「入力欄の現在値」ではなく、ここで確定した表示カード名にする
      _currentCardName=document.getElementById('q').value.trim();
      renderAll(d);
      btn.disabled=false;btn.textContent='検索';
      setTimeout(()=>{prog.style.display='none';},1200);
    }
  };
  es.onerror=()=>{
    if(gen!==_searchGen) return; // 世代管理
    clearTimeout(_searchTimeoutId);_searchTimeoutId=null;
    es.close();_searchES=null;btn.disabled=false;btn.textContent='検索';
    prog.style.display='none';
    document.getElementById('empty').classList.remove('hidden');
    const _errEl2=document.getElementById('emptyError');
    _errEl2.textContent='エラーが発生しました。再度お試しください。';
    _errEl2.classList.remove('hidden');
  };
}
// ── Autocomplete ──
const qInput=document.getElementById('q');
const sugDrop=document.getElementById('suggestDrop');
let sugTimer=null, sugIdx=-1, sugItems=[];

qInput.addEventListener('input',()=>{
  clearTimeout(sugTimer);
  const v=qInput.value.trim();
  if(v.length<2){closeSuggest();return;}
  sugTimer=setTimeout(()=>fetchSuggest(v),250);
});

qInput.addEventListener('keydown',e=>{
  if(sugDrop.classList.contains('open')&&sugItems.length>0){
    if(e.key==='ArrowDown'){e.preventDefault();sugIdx=Math.min(sugIdx+1,sugItems.length-1);highlightSug();return;}
    if(e.key==='ArrowUp'){e.preventDefault();sugIdx=Math.max(sugIdx-1,-1);highlightSug();return;}
    if(e.key==='Enter'&&sugIdx>=0){e.preventDefault();selectSug(sugItems[sugIdx].name,sugItems[sugIdx].unreleased);return;}
    if(e.key==='Escape'){closeSuggest();return;}
  }
  if(e.key==='Enter')doSearch();
});

qInput.addEventListener('blur',()=>{setTimeout(closeSuggest,150)});

function fetchSuggest(q){
  // include_unreleased=1 で未発売カードも候補に含める（レスポンスは {name, unreleased} の配列）
  fetch('/api/suggest?q='+encodeURIComponent(q)+'&include_unreleased=1')
    .then(r=>r.json())
    .then(items=>{
      if(!items.length){closeSuggest();return;}
      sugItems=items;sugIdx=-1;
      sugDrop.innerHTML=items.map((it,i)=>{
        const n=it.name;
        const badge=it.unreleased?' <span class="pack-unreleased-badge">発売前</span>':'';
        return `<div class="suggest-item" data-i="${i}" onmousedown="selectSug('${escAttr(escJs(n))}',${it.unreleased?'true':'false'})"><span class="suggest-thumb" data-thumb-name="${escAttr(n)}"></span><span class="suggest-name">${esc(n)}${badge}</span></div>`;
      }).join('');
      sugDrop.classList.add('open');
      loadSuggestThumbnails(sugDrop);
    })
    .catch(()=>closeSuggest());
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

function selectSug(name,unreleased){
  qInput.value=name;
  closeSuggest();
  // 未発売カードは価格検索せず、カード情報のみ表示する
  if(unreleased){_ga('search_submit',{mode:'sell',trigger:'user',result:'unreleased'});showUnreleasedCard(name);return;}
  doSearch({validated:true,trigger:'user'});
}

function closeSuggest(){sugDrop.classList.remove('open');sugIdx=-1;sugItems=[];}

function renderAll(d){
  document.getElementById('results').classList.remove('hidden');
  const searchTerm0=document.getElementById('q').value.trim();
  // S-2: /buy/<カード名>からの検索は/buy/のURLのまま保つ（'buyback'指定時はプレフィックスが
  // 既に一致しておりpushStateされないため実質no-op。フラグは_maybeOpenBuyInlineOnLoadで消費する前に読む）
  if(d.total>0) _updateUrl(searchTerm0,_pendingBuyInlineOpen?'buyback':'search');
  if(d.total===0){
    document.getElementById('results').innerHTML='<div class="empty"><p>該当するカードが見つかりませんでした</p></div>';
    _maybeOpenBuyInlineOnLoad(); // 0件でも次回の検索に持ち越さない
    return;
  }
  // F-5: soCountはこの時点では検索直後の全件基準。絞り込み確定後に_updateHeroSummary()で上書きする

  // Card hero image
  const searchTerm=document.getElementById('q').value.trim();
  const hero=document.getElementById('cardHero');
  const heroInfo=document.getElementById('heroInfo');
  hero.classList.remove('hidden');
  document.getElementById('heroImgWrap').innerHTML='<div style="width:140px;height:204px;background:var(--bg-card)"></div>';
  const inStock=D.filter(r=>!r.sold_out);
  const best=inStock.length>0?inStock.reduce((a,b)=>a.price<b.price?a:b):null;
  const bestPrice=best?`¥${best.price.toLocaleString()}`:'価格不明';
  const bestShop=best?best.shop:'';
  heroInfo.innerHTML=`<div class="card-hero-name">${esc(searchTerm)}</div>
    <div class="card-hero-meta">
      <span id="heroBest">${best?`最安値: <strong style="color:var(--gold);font-size:1.1rem">${bestPrice}</strong> (${bestShop})`:'在庫なし'}</span><br>
      在庫あり ${d.in_stock_count}件 / 売切 ${d.sold_out_count}件 / 計 ${d.total}件<br>
      レアリティ: ${[...new Set(D.map(r=>r.rarity).filter(Boolean))].length}種類
    </div>
    <!-- 2026-09-04: 既定で開く（B-2 のスマホ縦長対策で畳んでいたが、PC でも隠れて読めないため全幅で開く裁定） -->
    <details class="card-info-details" id="cardDetailDetails" open hidden>
      <summary>カード情報（効果テキスト）</summary>
      <div id="cardDetail" class="card-detail"></div>
    </details>
    <div class="share-btns">
      <button onclick="addToDeck('${escAttr(escJs(searchTerm))}')" style="padding:4px 12px;font:.72rem var(--font);font-weight:600;color:var(--accent-l);background:none;border:1px solid var(--accent);border-radius:4px;cursor:pointer">+ デッキに追加</button>
      <button onclick="wishAddFromBtn(this,'${escAttr(escJs(searchTerm))}',1)" style="padding:4px 12px;font:.72rem var(--font);font-weight:600;color:var(--accent-l);background:none;border:1px solid var(--accent);border-radius:4px;cursor:pointer">+ 購入候補に追加</button>
      <div class="share-menu-wrap">
        <button class="share-btn share-btn-x" onclick="toggleShareMenu('shareMenuSell')">シェア</button>
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

  // 画像: YGOResources優先（カーナベルはサーバー側フォールバック）
  fetch('/api/card-image?name='+encodeURIComponent(searchTerm))
    .then(r=>r.json())
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
  }
  rarityFilter=defaultRarity;
  tableShowCount=TABLE_PAGE_SIZE;
  // G-4: 「変更していないのにchange」で集計が濁らないようイベント名を分ける（自動設定=rarity_filter_default）。
  // 発火はレアリティUIが実際に出るケース（realRarities.length>1）だけに限定し、
  // 単一レアリティのカードで毎回発火して分母が壊れるのを防ぐ。C-1(i)判断のため
  // rarity_count（"(不明)"含む）・real_rarity_count（実体のあるレアリティ数）を同梱する
  if(realRarities.length>1){
    _ga('rarity_filter_default',{rarity:defaultRarity||'__all__',rarity_count:rarities.length,real_rarity_count:realRarities.length});
  }
  const rfEl=document.getElementById('rarityFilter');
  if(rarities.length>1){
    rfEl.classList.remove('hidden');
    let btns=`<span class="rf-label">レアリティ:</span><button class="rf-btn${defaultRarity===''?' active':''}" data-r="" onclick="setRarityFilter('')">すべて</button>`;
    for(const r of rarities){
      const cnt=D.filter(x=>x.rarity===r).length;
      const isActive=r===defaultRarity;
      btns+=`<button class="rf-btn${isActive?' active':''}" data-r="${escAttr(r)}" onclick="setRarityFilter('${escAttr(escJs(r))}')"><span class="rb rb-${cssClass(r)}" style="background:none;padding:0">${esc(r)}</span> <span style="color:var(--text-d)">${cnt}</span></button>`;
    }
    rfEl.innerHTML=btns;
  }else{
    rfEl.classList.add('hidden');
  }
  _updateRarityBanner();
  _updateHeroSummary(); // F-5: 既定の絞り込み確定後にヒーローの最安値/売切件数を合わせる

  renderSummary();
  renderTable();
  // F-2: A8トラッキングピクセルは検索1回につき1回だけ（レアリティ切替では再発火させない）
  const pixelSlot=document.getElementById('affPixelSlot');
  if(pixelSlot) pixelSlot.innerHTML=affPixel('カーナベル','sell');

  // Rarity grid
  const entries=Object.entries(d.by_rarity).sort((a,b)=>a[1].price-b[1].price);
  document.getElementById('rgrid').innerHTML=entries.map(([r,item])=>{
    const itemUrl=safeUrl(item.url);
    const link=itemUrl?`<a href="${escAttr(itemUrl)}" target="_blank" rel="noopener">¥${item.price.toLocaleString()}</a>`:`¥${item.price.toLocaleString()}`;
    return`<div class="ritem fin">
      <div class="ritem-img"></div>
      <div style="flex:1"><span class="rb rb-${cssClass(r)}">${esc(r)}</span><div class="rshop">${esc(item.shop)}</div></div>
      <div style="text-align:right"><div class="rprice">${link}</div></div></div>`;
  }).join('');

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
  const inStock=D.filter(r=>!r.sold_out);
  const filtered=rarityFilter?inStock.filter(r=>r.rarity===rarityFilter):inStock;
  const byShop={};
  for(const r of filtered){
    if(!byShop[r.shop]||r.price<byShop[r.shop].price) byShop[r.shop]=r;
  }
  return Object.values(byShop).sort((a,b)=>a.price-b.price);
}

function renderSummary(){
  const items=_summaryShopItems();
  // F-10: 選んだレアリティに在庫が無いとき、無言の空欄にせず明示する
  if(items.length===0){
    const soldInRarity=(rarityFilter?D.filter(r=>r.rarity===rarityFilter):D).filter(r=>r.sold_out).length;
    const soldNote=soldInRarity>0?`（売切${soldInRarity}件）`:'';
    document.getElementById('sumCards').innerHTML=`<div class="empty" style="padding:16px 0"><p>このレアリティは在庫切れです${soldNote}</p></div>`;
    return;
  }
  const minPrice=items[0].price;
  // M-2: 先頭行（最安）の型番が「型番らしい形」を通るときだけ、差額の突き合わせ基準にする
  // （型番が違えば別物のため、別型番同士の引き算を表示しない。買取側の店舗別カードと同じ規律）
  const topCode=_looksLikeCardCode(items[0].code)?items[0].code:null;
  let h='';
  items.forEach((item,i)=>{
    const sameCode=!!topCode&&item.code===topCode;
    // J-5: rarityFilterが空（=「すべて」表示・別レアリティ同士の比較）のときはBESTバッジを付けない
    // （別レアリティの最安値に断言してしまう表示を防ぐ。F-9の差額抑止と同じ理由）
    // X-1: BESTバッジは「先頭行＝最安」の印であって型番の一致は無関係（型番一致は差額表示のみの条件）。
    // 先頭行の型番が使えない値でもバッジは先頭行に付く
    const isBest=!!rarityFilter&&i===0;
    const itemUrl=affLink(item.url, item.shop, 'sell');
    const nameLink=item.url?`<a href="${escAttr(itemUrl)}" target="_blank" rel="noopener nofollow">${esc(item.name)}</a>`:esc(item.name);
    const priceLink=item.url?`<a class="scard-link" href="${escAttr(itemUrl)}" target="_blank" rel="noopener nofollow">¥${item.price.toLocaleString()}</a>`:`¥${item.price.toLocaleString()}`;
    const bestBadge=isBest?'<span class="sbest-badge">最安</span>':'';
    // F-9: rarityFilterが空（=別レアリティ間の比較）のときは差額表示自体を出さない（比較として成立しないため）
    // F-8: 最安（先頭）は.sbest-badgeに任せ.sdiffには何も出さない（重複解消）。
    //      同額タイのときは「+¥0」ではなく「同額」と出す
    let diff='';
    if(rarityFilter&&!isBest&&sameCode){
      const d=item.price-minPrice;
      diff=d===0?'同額':`+¥${d.toLocaleString()}`;
    }
    // J-4: 状態（condition）が「-」以外（例: 状態C・中古キズあり・セール）なら、レアリティ・カード名と
    // 同じ扱いでここに出す。状態違いの価格を無条件価格に見せない
    const condStr=(item.condition&&item.condition!=='-')?`・${esc(item.condition)}`:'';
    // K-1/L-1: 型番（code）が「型番らしい形」を通るときだけ表示する（弾の略号や壊れた値をそのまま
    // 型番として見せない。迷ったら弾く側に倒す）
    const codeStr=_looksLikeCardCode(item.code)?`・${esc(item.code)}`:'';
    h+=`<div class="scard${isBest?' best':''} fin"><div class="slabel">${esc(item.shop)}</div>
      <div class="sprice">${priceLink}${bestBadge}<span class="sdiff">${diff}</span></div>
      <div class="sdetail">${esc(item.rarity||'-')}${condStr}${codeStr}<br>${nameLink}</div></div>`;
  });
  document.getElementById('sumCards').innerHTML=h;
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
  // /buy/<カード名> で着地したときの自動オープン。ユーザーが自分で開いたわけでは
  // ないので 'inline'（＝ユーザー操作）と混ぜない。混ぜると「買取を開いた人数」が
  // ページ着地で水増しされ、mode_switch / search_submit で潰したのと同じ汚染になる
  _buyInlineOpenTrigger='deeplink';
  det.open=true;
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
  const body=document.getElementById('buyInlineBody');
  if(body) body.innerHTML='';
}

// U-1: 表側の「買取価格も表示する」ボタン経由か、#buyInline summaryを直接開いたのかをGA上で
// 区別するためのフラグ。既定は'inline'。ボタン側がdet.open=trueする直前だけ'table_button'に
// 差し替え、toggleリスナーが読み取ったら'inline'に戻す（イベントの二重発火はさせない）
let _buyInlineOpenTrigger='inline';

document.getElementById('buyInline')?.addEventListener('toggle',function(){
  if(!this.open) return;
  _ga('buyback_inline_open',{trigger:_buyInlineOpenTrigger});
  // 1カード1回だけ取得。取得済み/取得中なら再取得しない（閉じて再度開いても再取得しない）
  if(!_buyInlineFetched&&!_buyInlineFetchInFlight) _fetchBuyInline(_buyInlineOpenTrigger);
  _buyInlineOpenTrigger='inline';
});

// U-1: 販売テーブル側のボタン。取得処理は自前で書かず、#buyInlineを開く経路（上のtoggle
// リスナー→_fetchBuyInline）をそのまま再利用する。二重取得・二重GA発火を避けるため
// det.open=trueを呼ぶだけで、_fetchBuyInline()を直接ここから呼ばない
function _openBuyInlineFromTableBtn(){
  const det=document.getElementById('buyInline');
  if(!det) return;
  if(det.open) return; // 既に開いている（=取得済みか取得中）なら何もしない
  _buyInlineOpenTrigger='table_button';
  det.open=true; // 'toggle'イベントが発火し、上のリスナーがGA計測と_fetchBuyInline()を担う
}

function _fetchBuyInline(trigger){
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
  // 従来通り'inline'扱いのまま（既存の意味を壊さない）
  _ga('search_submit',{mode:'buyback',trigger:trigger||'inline'});
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
    if(d.type==='progress'){
      const el=step(d.shop);el.className='pstep searching';el.querySelector('.pstat').textContent='検索中...';
    }else if(d.type==='shop_done'){
      const el=step(d.shop);el.className='pstep done';el.querySelector('.pstat').textContent=d.count+'件'+(d.cached?' (cache)':'');
    }else if(d.type==='shop_error'){
      const el=step(d.shop);el.className='pstep error';el.querySelector('.pstat').textContent='エラー';
      _buyInlineFailedShops.push(d.shop);
    }else if(d.type==='done'){
      clearTimeout(_buyInlineTimeoutId);_buyInlineTimeoutId=null;es.close();_buyInlineFetchInFlight=false;_buyInlineES=null;
      _buyInlineD=d.results;
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
  return failed+manzoku;
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
    return;
  }
  const filtered=_buyInlineFilteredData();
  if(!filtered.length){
    body.innerHTML=`<div class="empty" style="padding:16px 0"><p>このレアリティの買取価格は見つかりませんでした</p></div>${_buyInlineNotes()}`;
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
    const priceLink=item.url?`<a class="scard-link" href="${escAttr(itemUrl)}" target="_blank" rel="noopener nofollow">¥${item.price.toLocaleString()}</a>`:`¥${item.price.toLocaleString()}`;
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
    cardsHtml+=`<div class="scard${isBest?' best':''} fin"><div class="slabel">${esc(item.shop)}</div>
      <div class="sprice" style="color:var(--green)">${priceLink}${bestBadge}<span class="sdiff">${diff}</span></div>
      <div class="sdetail">${esc(item.rarity||'-')}${condStr}${codeStr}<br>${esc(item.name)}</div></div>`;
  });

  body.innerHTML=`${conditionNote}<div class="summary">${cardsHtml}</div>${_buyInlineNotes()}`;
}

// F-5/F-14: ヒーローの最安値表示・売切件数バッジをrarityFilterに合わせて再計算する
// （setRarityFilter()とrenderAll()の両方から呼ぶ。D全体基準のままだと絞り込み後も
// 表示が追従せず、画面の上と下で数字が矛盾していた）。
// 最安値は_summaryShopItems()（renderSummary()と同じ算出）の先頭要素を使い、同額タイのときに
// ヒーローとサマリー先頭カードの店舗名が食い違わないようにする
function _updateHeroSummary(){
  const heroBest=document.getElementById('heroBest');
  const soCountEl=document.getElementById('soCount');
  if(!heroBest&&!soCountEl) return;
  const items=_summaryShopItems();
  const best=items.length?items[0]:null;
  if(heroBest){
    heroBest.innerHTML=best
      ?`最安値: <strong style="color:var(--gold);font-size:1.1rem">¥${best.price.toLocaleString()}</strong> (${esc(best.shop)})`
      :'在庫なし';
  }
  // 売切件数はサーバー集計値（d.sold_out_count）とは別経路でクライアント側のD（rarityFilter適用後）
  // から再集計している。サーバー側の集計ロジックと将来ズレる可能性があるため要注意
  if(soCountEl){
    const scoped=rarityFilter?D.filter(r=>r.rarity===rarityFilter):D;
    soCountEl.textContent=scoped.filter(r=>r.sold_out).length;
  }
}

// C-1(iii): 「絞り込み中」を明示するバナー（解除ボタン付き）
function _updateRarityBanner(){
  const banner=document.getElementById('rfBanner');
  if(!banner) return;
  if(!rarityFilter){
    banner.classList.add('hidden');
    banner.innerHTML='';
    return;
  }
  // F-7/F-13: バナーの主役件数は「一覧(renderTable)が実際に画面に出している件数」と同じ基準に
  // 揃える。以前はDから直接数えていたため、売切トグルOFF時（既定）は一覧より多い数字が出ていた
  // （バナー=売切込み・一覧=在庫ありのみ、で同じ絞り込みの件数が食い違って見えていた）。
  // 売切の内訳は「（売切N件）」として併記し、トグルの状態に関わらず常に見えるようにする
  const showSO=document.getElementById('soCheck')?.checked;
  const inRarity=D.filter(r=>r.rarity===rarityFilter);
  const mainCount=showSO?inRarity.length:inRarity.filter(r=>!r.sold_out).length;
  const soldCount=inRarity.filter(r=>r.sold_out).length;
  const soldSuffix=soldCount>0?`（売切${soldCount}件）`:'';
  const totalRarities=[...new Set(D.map(r=>r.rarity).filter(Boolean))].length;
  banner.classList.remove('hidden');
  banner.innerHTML=`レアリティ「<strong>${esc(rarityFilter)}</strong>」で絞り込み中 — ${mainCount}件${soldSuffix} ／ 全${D.length}件・${totalRarities}種<button class="rf-banner-clear" onclick="setRarityFilter('')">すべて表示</button>`;
}

function renderTable(){
  const showSO=document.getElementById('soCheck').checked;
  let data=showSO?D:D.filter(r=>!r.sold_out);
  if(rarityFilter) data=data.filter(r=>r.rarity===rarityFilter);
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
    const rc=r.rarity?'rb-'+cssClass(r.rarity):'';
    const so=r.sold_out?'sold-out':'';
    const stockStr=r.sold_out?'<span class="sob">売切</span>':(r.stock>0?r.stock:'-');
    const linkUrl=affLink(r.url, r.shop, 'sell');
    const nameLink=r.url
      ?`<a class="row-link" href="${escAttr(linkUrl)}" target="_blank" rel="noopener nofollow">${esc(r.name)}</a>`
      :esc(r.name);
    const cond=r.condition==='セール'?'<span style="color:var(--gold)">セール</span>':esc(r.condition);
    const rowClass=[so,linkUrl?'row-clickable':''].filter(Boolean).join(' ');
    const rowHref=linkUrl?`data-href="${escAttr(linkUrl)}"`:'';
    // U-1: 買取が未取得のうちは列自体を出さない（thのhidden切り替えと対にする）。
    // X-2: 値は金額のみ（店名は出さない。行に既に「店舗」列があり、別店名を括弧書きすると紛らわしい上
    // スマホで折り返して崩れるため。どの店が最高額かは下の買取セクション一覧で分かる）。
    // buyMaxShop自体は_applyBuyMaxToDで算出済みのまま保持（ソート等では未使用のため計算は変えない）
    const buyMaxCell=_buyInlineFetched
      ?`<td data-label="最高買取" class="col-buymax">${r.buyMax!=null?'¥'+r.buyMax.toLocaleString():''}</td>`
      :'';
    return`<tr class="${rowClass}" ${rowHref}>
      <td data-label="" class="col-img"><div class="tbl-img"></div></td>
      <td data-label="レアリティ"><span class="rb ${rc}">${esc(r.rarity||'-')}</span></td>
      <td data-label="カード名" class="col-name">${nameLink}</td>
      <td data-label="店舗">${esc(r.shop)}</td>
      <td data-label="状態">${cond}</td>
      <td data-label="価格" class="col-price">¥${r.price.toLocaleString()}</td>
      <td data-label="在庫" class="col-stock">${stockStr}</td>
      <td data-label="コード" class="col-code">${esc(r.code||'')}</td>${buyMaxCell}
      <td>${wishBtnHtml(r.name,1)}</td></tr>`;
  }).join('');

  // U-1: 買取が未取得の間はヘッダー列自体を隠す（thのhiddenをtd側の出し分けと揃える）
  const thBuyMax=document.getElementById('thBuyMax');
  if(thBuyMax) thBuyMax.classList.toggle('hidden',!_buyInlineFetched);
  const buyBtn=document.getElementById('tblBuyBtn');
  if(buyBtn) buyBtn.classList.toggle('hidden',_buyInlineFetched);

  document.querySelectorAll('#tbl th').forEach(th=>{
    const arr=th.querySelector('.arr');
    if(arr) arr.textContent=th.dataset.s===sort.k?(sort.a?'▲':'▼'):'';
  });

  // Fill in card images if already loaded
  if(cardImgSmall){
    const safeSmall=safeUrl(cardImgSmall);
    document.querySelectorAll('.tbl-img').forEach(el=>{
      if(safeSmall&&!el.querySelector('img')) el.innerHTML=`<img src="${escAttr(safeSmall)}" alt="" loading="lazy">`;
    });
  }

  // もっと見る（残り件数を明示。無限スクロールは採用しない）
  const moreEl=document.getElementById('tblMore');
  if(moreEl){
    if(total===0){
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
  renderTable();
  _updateRarityBanner(); // F-13: 一覧の基準（売切トグル）が変わったらバナーの件数も追従させる
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

function tab(id){
  document.querySelectorAll('.tbtn').forEach(b=>b.classList.toggle('active',b.dataset.t===id));
  document.querySelectorAll('.tpanel').forEach(p=>p.classList.toggle('active',p.id==='p-'+id));
}

function setRarityFilter(r){
  // G-4: from_rarityで「自動絞り込みが的外れで、ユーザーがすぐ別のレアリティ／すべてに戻した」を追跡できるようにする
  _ga('rarity_filter_change',{rarity:r||'__all__',from_rarity:rarityFilter||'__all__'});
  rarityFilter=r;
  tableShowCount=TABLE_PAGE_SIZE; // B-4: レアリティ切替のたびに表示件数をリセット
  document.querySelectorAll('.rf-btn').forEach(b=>b.classList.toggle('active',b.dataset.r===r));
  _updateRarityBanner();
  _updateHeroSummary(); // F-5: ヒーローの最安値・売切件数を絞り込みに追従させる
  renderSummary();
  renderTable();
  _renderBuyInline(); // C-2: 買取セクションも同じレアリティに絞り込んで再描画する（未取得なら何もしない・再取得はしない）
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
let _topMoversDir='down';
function switchTopMovers(dir){
  _topMoversDir=dir;
  document.querySelectorAll('#topMoversSection .movers-tab').forEach(b=>b.classList.toggle('active',b.textContent.includes(dir==='up'?'上がり':'下がり')));
  loadTopMovers(dir);
}
function loadTopMovers(dir,retry){
  retry=retry||0;
  const sec=document.getElementById('topMoversSection');
  const list=document.getElementById('topMoversList');
  const period=document.getElementById('topMoversPeriod');
  sec.classList.remove('hidden');
  if(!retry) list.innerHTML='<div class="movers-fail">読込中...</div>';
  fetch(`/api/top-movers?direction=${dir||_topMoversDir}&limit=10`)
    .then(r=>r.json())
    .then(data=>{
      if(data.error){
        // 2026-09-01: 集計をDB側RPC(get_top_movers)へ寄せ、本番実測で初回1.1秒・
        // キャッシュ後0.2秒になったため、応急で8回×3秒に延ばしていた再試行を戻す。
        // 待っている間であることが分かる文言は残す（キャッシュ失効直後は数百ms待つため）。
        if(retry<3){
          list.innerHTML='<div class="movers-fail">集計中です。少しお待ちください…</div>';
          setTimeout(()=>loadTopMovers(dir,retry+1),2000);
          return;
        }
        list.innerHTML='<div class="movers-fail">価格推移ランキングの取得に失敗しました</div>';
        return;
      }
      const items=data.items||[];
      if(!items.length){sec.classList.add('hidden');return;}
      if(data.date_old&&data.date_new){
        const upd=data.updated_at?` · ${data.updated_at}収集`:'';
        // Q-11: 定着チェック（前日データ）が無い回は、精度が落ちていることが分かるよう明示する
        const stabilityNote=data.stability_checked===false?'（定着確認なし）':'';
        period.textContent=_fmtDate(data.date_old)+' → '+_fmtDate(data.date_new)+' の価格変動（共通店舗のみ）'+stabilityNote+upd;
      }
      list.innerHTML=items.map((item,i)=>{
        const isUp=item.diff>0;
        const sign=isUp?'+':'';
        const cls=isUp?'up':'down';
        // Q-1: どのレアリティの値段か分からないまま騰落だけ見せない（旧実装で表示していたバッジの復活）
        const rarityBadge=item.rarity?`<span class="rb rb-${cssClass(item.rarity)}">${esc(item.rarity)}</span>`:'';
        return `<div class="movers-item" onclick="openTopMover('${escAttr(escJs(item.name))}',${i+1})" data-card="${escAttr(item.name)}">
          <span class="movers-rank">${i+1}</span>
          <span class="movers-thumb"></span>
          <span class="movers-name"><span class="movers-name-text">${esc(item.name)}</span>${rarityBadge?`<span class="movers-rarity">${rarityBadge}</span>`:''}</span>
          <span class="movers-price">
            <span class="movers-price-now">&yen;${item.today.toLocaleString()}</span>
            <span class="movers-diff ${cls}">${sign}${item.diff.toLocaleString()} (${sign}${item.pct}%)</span>
          </span>
        </div>`;
      }).join('');
      loadRankingImages('#topMoversList .movers-item','movers-thumb');
    })
    .catch(()=>{
      if(retry<2){setTimeout(()=>loadTopMovers(dir,retry+1),2000);return;}
      list.innerHTML='<div class="movers-fail">価格推移ランキングの取得に失敗しました</div>';
    });
}
function openTopMover(name,rank){
  _ga('top_mover_click',{rank:rank});
  searchCard(name);
}

// ── OF(オーバーフレーム)／シリアル付きOF(グランドマスターレア)ランキング（2026-09-04追加） ──
// 1段目タブ: OF(値動き。get_top_movers流用) ⇔ シリアル付き(現在の最安値の高額順。get_top_priced)
let _ofMoversGroup='of';
let _ofMoversDir='down';
// タブ切替のたびに増やす世代番号。OF/シリアル付きは同じ #ofMoversList を共有しており、
// 切替前の読込（特に2秒後の再試行）が切替後の表示を上書きしてしまうため、
// 応答時に世代が変わっていたら捨てる（ローカル検証で実際に発生したのを確認）
let _ofMoversSeq=0;
function switchOfMoversGroup(group){
  _ofMoversGroup=group;
  _ofMoversSeq++;
  document.getElementById('ofMoversTabOf').classList.toggle('active',group==='of');
  document.getElementById('ofMoversTabGmr').classList.toggle('active',group==='gmr');
  // シリアル付き(高額順)には値上がり/値下がりの概念が無いため2段目タブを隠す
  document.getElementById('ofMoversDirTabs').classList.toggle('hidden',group==='gmr');
  loadOfMoversSection();
}
function switchOfMovers(dir){
  _ofMoversDir=dir;
  _ofMoversSeq++;
  document.querySelectorAll('#ofMoversDirTabs .movers-tab').forEach(b=>b.classList.toggle('active',b.dataset.dir===dir));
  loadOfMoversSection();
}
function loadOfMoversSection(retry){
  if(_ofMoversGroup==='gmr') loadGmrPriced(retry);
  else loadOfMovers(_ofMoversDir,retry);
}
function loadOfMovers(dir,retry){
  retry=retry||0;
  const seq=_ofMoversSeq;
  const sec=document.getElementById('ofMoversSection');
  const list=document.getElementById('ofMoversList');
  const period=document.getElementById('ofMoversPeriod');
  sec.classList.remove('hidden');
  if(!retry){list.innerHTML='<div class="movers-fail">読込中...</div>';period.textContent='';}  // L-4: 前タブの期間表示を引き継がない
  fetch(`/api/top-movers?group=of&direction=${dir||_ofMoversDir}&limit=10`)
    .then(r=>r.json())
    .then(data=>{
      if(seq!==_ofMoversSeq) return;  // 切替済み・この応答は捨てる
      if(data.error){
        if(retry<3){
          list.innerHTML='<div class="movers-fail">集計中です。少しお待ちください…</div>';
          setTimeout(()=>{ if(seq!==_ofMoversSeq) return; loadOfMovers(dir,retry+1); },2000);
          return;
        }
        list.innerHTML='<div class="movers-fail">ランキングの取得に失敗しました</div>';
        return;
      }
      const items=data.items||[];
      // M-1修正: 空データでもセクションごと隠さず、リスト内にメッセージを出す
      // （隠すと他タブへの導線ごと消えて復帰できなくなるため）
      if(!items.length){list.innerHTML='<div class="movers-fail">該当するカードがありません</div>';period.textContent='';return;}
      if(data.date_old&&data.date_new){
        const upd=data.updated_at?` · ${data.updated_at}収集`:'';
        const stabilityNote=data.stability_checked===false?'（定着確認なし）':'';
        period.textContent=_fmtDate(data.date_old)+' → '+_fmtDate(data.date_new)+' の価格変動（共通店舗のみ）'+stabilityNote+upd;
      }
      list.innerHTML=items.map((item,i)=>{
        const isUp=item.diff>0;
        const sign=isUp?'+':'';
        const cls=isUp?'up':'down';
        const rarityBadge=item.rarity?`<span class="rb rb-${cssClass(item.rarity)}">${esc(item.rarity)}</span>`:'';
        return `<div class="movers-item" onclick="openOfMover('${escAttr(escJs(item.name))}',${i+1})" data-card="${escAttr(item.name)}">
          <span class="movers-rank">${i+1}</span>
          <span class="movers-thumb"></span>
          <span class="movers-name"><span class="movers-name-text">${esc(item.name)}</span>${rarityBadge?`<span class="movers-rarity">${rarityBadge}</span>`:''}</span>
          <span class="movers-price">
            <span class="movers-price-now">&yen;${item.today.toLocaleString()}</span>
            <span class="movers-diff ${cls}">${sign}${item.diff.toLocaleString()} (${sign}${item.pct}%)</span>
          </span>
        </div>`;
      }).join('');
      loadRankingImages('#ofMoversList .movers-item','movers-thumb');
    })
    .catch(()=>{
      if(seq!==_ofMoversSeq) return;
      if(retry<2){setTimeout(()=>{ if(seq!==_ofMoversSeq) return; loadOfMovers(dir,retry+1); },2000);return;}
      list.innerHTML='<div class="movers-fail">ランキングの取得に失敗しました</div>';
    });
}
function loadGmrPriced(retry){
  retry=retry||0;
  const seq=_ofMoversSeq;
  const sec=document.getElementById('ofMoversSection');
  const list=document.getElementById('ofMoversList');
  const period=document.getElementById('ofMoversPeriod');
  sec.classList.remove('hidden');
  if(!retry){list.innerHTML='<div class="movers-fail">読込中...</div>';period.textContent='';}  // L-4: 前タブの期間表示を引き継がない
  fetch(`/api/top-priced?group=gmr&limit=10`)
    .then(r=>r.json())
    .then(data=>{
      if(seq!==_ofMoversSeq) return;  // 切替済み・この応答は捨てる
      if(data.error){
        if(retry<3){
          list.innerHTML='<div class="movers-fail">集計中です。少しお待ちください…</div>';
          setTimeout(()=>{ if(seq!==_ofMoversSeq) return; loadGmrPriced(retry+1); },2000);
          return;
        }
        list.innerHTML='<div class="movers-fail">ランキングの取得に失敗しました</div>';
        return;
      }
      const items=data.items||[];
      // M-1修正: 空データでもセクションごと隠さず、リスト内にメッセージを出す
      if(!items.length){list.innerHTML='<div class="movers-fail">該当するカードがありません</div>';period.textContent='';return;}
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
      list.innerHTML=items.map((item,i)=>{
        // diff が null（共通店舗が無く比較不能）と 0（7日前と同額）は区別して表示する。
        // 0 を「+0 (0%)」と出しても意味が無いため「変動なし」の文言に置き換える
        const hasDiff=item.diff!==null&&item.diff!==undefined&&item.diff!==0;
        const flatHtml=(item.diff===0)?`<span class="movers-diff" style="color:var(--text-d)">7日間 変動なし</span>`:'';
        const isUp=hasDiff&&item.diff>0;
        const sign=isUp?'+':'';
        const cls=isUp?'up':'down';
        const diffHtml=hasDiff?`<span class="movers-diff ${cls}">${sign}${item.diff.toLocaleString()} (${sign}${item.pct}%)</span>`:flatHtml;
        const rarityBadge=item.rarity?`<span class="rb rb-${cssClass(item.rarity)}">${esc(item.rarity)}</span>`:'';
        return `<div class="movers-item" onclick="openGmrPriced('${escAttr(escJs(item.name))}',${i+1})" data-card="${escAttr(item.name)}">
          <span class="movers-rank">${i+1}</span>
          <span class="movers-thumb"></span>
          <span class="movers-name"><span class="movers-name-text">${esc(item.name)}</span>${rarityBadge?`<span class="movers-rarity">${rarityBadge}</span>`:''}</span>
          <span class="movers-price">
            <span class="movers-price-now">&yen;${item.price.toLocaleString()}</span>
            ${diffHtml}
          </span>
        </div>`;
      }).join('');
      loadRankingImages('#ofMoversList .movers-item','movers-thumb');
    })
    .catch(()=>{
      if(seq!==_ofMoversSeq) return;
      if(retry<2){setTimeout(()=>{ if(seq!==_ofMoversSeq) return; loadGmrPriced(retry+1); },2000);return;}
      list.innerHTML='<div class="movers-fail">ランキングの取得に失敗しました</div>';
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
      if(!items.length){sec.classList.add('hidden');return;}
      sec.classList.remove('hidden');
      if(data.date_old&&data.date_new){
        const upd=data.updated_at?` · ${data.updated_at}収集`:'';
        period.textContent=_fmtDate(data.date_old)+' → '+_fmtDate(data.date_new)+' の買取価格変動'+upd;
      }
      list.innerHTML=items.map((item,i)=>{
        const isUp=item.diff>0;
        const sign=isUp?'+':'';
        // 買取は値上がり=緑(お得)、値下がり=赤 と色反転するため buyback-diff クラスを使用
        const cls=isUp?'up':'down';
        const rarityBadge=item.rarity?`<span class="rb rb-${cssClass(item.rarity)}">${esc(item.rarity)}</span>`:'';
        return `<div class="movers-item" onclick="doBuySearchCard('${escAttr(escJs(item.name))}')" data-card="${escAttr(item.name)}">
          <span class="movers-rank">${i+1}</span>
          <span class="movers-thumb"></span>
          <span class="movers-name"><span class="movers-name-text">${esc(item.name)}</span>${rarityBadge?`<span class="movers-rarity">${rarityBadge}</span>`:''}</span>
          <span class="movers-price">
            <span class="movers-price-now">&yen;${item.today.toLocaleString()}</span>
            <span class="movers-diff buyback-diff ${cls}">${sign}${item.diff.toLocaleString()} (${sign}${item.pct}%)</span>
          </span>
        </div>`;
      }).join('');
      loadRankingImages('#buybackMoversList .movers-item','movers-thumb');
    })
    .catch(()=>{if(retry<2)setTimeout(()=>loadBuybackMovers(dir,retry+1),2000);});
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
        if(thumb) thumb.innerHTML='<img src="'+escAttr(safeUrl(url))+'" alt="" loading="lazy">';
      });
    });
  })
  .catch(()=>{});
}

// 初期ロード・モード復元
// DOMContentLoaded で実行することで、スクリプト末尾の const/let 宣言が
// 全て初期化された後に switchMode() を呼び出し、TDZ ReferenceError を防ぐ。
document.addEventListener('DOMContentLoaded', () => {
  // 初期ロード
  loadTopMovers('down');
  loadOfMovers('down');
  loadBuybackMovers('up');

  // G-7: 「一人回し」リンクはinline onclickではなくaddEventListenerで配線
  // （将来のCSP導入でinline onclickが無効化されても壊れないように）
  document.getElementById('headerSoloLink')?.addEventListener('click', () => _ga('solo_play_open'));

  // 検索欄を手動で空にしたら結果を隠して初期状態（#empty）へ戻す
  document.getElementById('q')?.addEventListener('input', () => {
    const results=document.getElementById('results');
    if(document.getElementById('q').value.trim()===''&&!results.classList.contains('hidden')){
      results.classList.add('hidden');
      document.getElementById('empty').classList.remove('hidden');
    }
  });

  // SEO: カード個別ページの場合、自動検索を実行（サーバーが渡したカード名なのでvalidateスキップ）。
  // S-1: /buy/<カード名> も「価格」タブ（#q検索）に統合。S-2: 着地時は買取セクション（#buyInline）を
  // 開いた状態にする。固定タイマーで開くと検索完了が遅いときに打ち消される回帰があったため、
  // 検索結果が実際に届いた側（renderAll内の_maybeOpenBuyInlineOnLoad）から開く
  if(_pageCardName){
    document.getElementById('q').value=_pageCardName;
    if(_pageMode==='buyback') _pendingBuyInlineOpen=true;
    setTimeout(()=>doSearch({validated:true,trigger:'pageload'}),300);
  }

  // URLハッシュからモードを復元（カード表示ページでないときのみ）。
  // S-1: #buyback は買取タブが無くなったため「価格」タブ（search）へ正規化する
  if(!_pageCardName){
    const _hashMode=location.hash.slice(1);
    // #mydeck-import: 一人回しページの「マイデッキページで取り込む」ボタンからの遷移用。
    // マイデッキタブを開いた上で、ニューロン取込パネルを開いた状態で表示する
    const _isImportDeeplink = _hashMode === 'mydeck-import';
    const _normalizedHash = _isImportDeeplink ? 'mydeck' : _hashMode;
    if(['mydeck','meta','packs','wishlist','buyback'].includes(_normalizedHash)){
      switchMode(_normalizedHash==='buyback'?'search':_normalizedHash,'hash');
    }
    if(_isImportDeeplink){
      const _importDetails = document.getElementById('deckImportDetails');
      if(_importDetails) _importDetails.open = true;
      // モバイル幅では deck.css の .deck-panes.show-tools が無いと
      // 取込パネルが属する右ペイン(#deckPaneTools)ごと非表示になるため、
      // ペイン切替（タブのアクティブ状態も含む）を明示的に行う
      document.getElementById('deckPanes')?.classList.add('show-tools');
      if(typeof window.switchDeckPane === 'function') window.switchDeckPane('tools');
      if(_importDetails) _importDetails.scrollIntoView({block:'start'});
    }
  }

  // /featured URL でアクセスした場合は最新弾タブを開き新弾を自動選択
  if(_pageMode==='featured'){
    switchMode('packs','deeplink');
    loadPacks().then(()=>{
      const f=_packData.find(p=>p.featured);
      if(f) loadPackCards(f.name, f.wiki_page||f.name, f.tcg_name||'', true);
    });
  }

  // フラッシュ防止用の一時スタイルを削除（以降のタブ切替を正常に動かすため）
  const _fp=document.getElementById('fouc-prevent');
  if(_fp) _fp.remove();
});

// SEO: 検索完了時にURLをpushStateで書き換え（ブラウザ履歴対応）
function _updateUrl(cardName, mode){
  if(!cardName)return;
  const prefix=mode==='buyback'?'/buy/':'/card/';
  const newUrl=prefix+encodeURIComponent(cardName);
  if(window.location.pathname!==newUrl){
    history.pushState({card:cardName,mode:mode},'',newUrl);
  }
  // ページタイトルも更新
  const suffix=mode==='buyback'?' の買取価格比較':' の価格比較';
  document.title=cardName+suffix+' | TCGYM';
}

// ブラウザの戻る/進むボタン対応
window.addEventListener('popstate',function(e){
  if(e.state&&e.state.card){
    switchMode('search','popstate');
    document.getElementById('q').value=e.state.card;
    // S-2: /buy/<カード名>の履歴エントリに戻ったときは、買取セクションを開いた状態に戻す
    if(e.state.mode==='buyback') _pendingBuyInlineOpen=true;
    doSearch({validated:true,trigger:'popstate'});
  }
});

// G-5: スクロール量計測（C-3c ボトムナビ判断材料）。
// 現在のスクロール到達率（%）。スクロール不可（コンテンツが画面に収まる）なら0を返す
function _scrollPct(){
  const doc=document.documentElement;
  const scrollTop=window.scrollY||doc.scrollTop||0;
  const scrollable=doc.scrollHeight-doc.clientHeight;
  if(scrollable<=0) return 0;
  return Math.round(Math.min(100,scrollTop/scrollable*100));
}
// 25/50/75%到達で1回ずつ発火（GA4拡張計測の90%固定より粒度を細かくする）。
// 同一ページで重複発火しないよう到達済みしきい値をSetで管理する
const _scrollDepthFired=new Set();
window.addEventListener('scroll',function(){
  const pct=_scrollPct();
  [25,50,75].forEach(th=>{
    if(pct>=th && !_scrollDepthFired.has(th)){
      _scrollDepthFired.add(th);
      _ga('scroll_depth',{percent:th});
    }
  });
},{passive:true});

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
  const labels={search:'価格',mydeck:'マイデッキ',meta:'環境デッキ',packs:'最新弾'};
  document.querySelectorAll('.mode-tab').forEach(b=>{
    // 購入候補タブはテキスト先頭で判定（バッジspanが含まれるため）
    if(mode==='wishlist') b.classList.toggle('active',b.textContent.startsWith('購入候補'));
    else b.classList.toggle('active',b.textContent===(labels[mode]||mode));
  });
  ['search','mydeck','meta','packs','wishlist'].forEach(m=>{
    document.getElementById('mode-'+m).classList.toggle('hidden',m!==mode);
  });
  if(mode==='packs') loadPacks();
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
  if(mode==='mydeck'){
    renderSavedDecks();
    // 端末間同期（P2）: マイデッキタブを開いたときだけ pull する（カード個別ページ閲覧では走らせない）
    if(window.SyncClient) window.SyncClient.pullIfNeeded();
  }
  // リロード時にモードを復元できるようURLハッシュを更新
  const _modeHash = (mode==='search') ? '' : mode;
  if(location.hash.slice(1) !== _modeHash){
    history.replaceState(null,'', _modeHash ? ('#'+_modeHash) : (location.pathname+location.search));
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
  },
  // 購入候補リスト用：wishGet() を直列化して calcDeckEstimate に渡す
  wishlist:{
    results:'wishResults', list:'wishCardList', total:'wishTotalPrice',
    prog:'wishProgress', rowPrefix:'wish-row-',
    shareArea:'wishShareArea',
    getText:()=>(typeof wishGet==='function'?wishGet():[]).map(c=>c.qty>1?`${c.qty} ${c.name}`:c.name).join('\n'),
    getCards:()=>({main:(typeof wishGet==='function'?wishGet():[]).map(c=>({qty:c.qty,name:c.name})),ex:[]}),
    getName:()=>'購入候補リスト'
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

// 保存デッキをmain/ex構造に正規化（旧データ対応）
// deck.ex=[]（空配列）はfalsyになるためArray.isArrayで判定する
function normalizeDeck(deck){
  if(Array.isArray(deck.main)) return {main:deck.main,ex:Array.isArray(deck.ex)?deck.ex:[]};
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
let _metaLoadPromise=null; // Q-11: 呼び出しが重なった時に /api/meta を二重fetchしないためのin-flightガード

// R-1: 旧トップページの「デッキの金額」ランキング（/api/top-decks）を環境デッキタブへ統合。
// 既定は強い順（Tierグループ表示）、切替で安い順（フラット表示）にする。
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
  document.querySelectorAll('#metaSortTabs .movers-tab').forEach(b=>b.classList.toggle('active',b.textContent.includes(sort==='price'?'安い':'強い')));
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

function _metaDeckPriceHtml(name){
  const d=_metaPriceMap[name];
  if(!d) return '';
  // Q-11: totalは「取れた分だけの合計」であり正確な総額ではないため、
  // 一部未取得のときは下限であることが分かるよう「〜」を付ける（「約」は丸めに読める）
  const priceText=d.priced_count>0
    ?`&yen;${d.total.toLocaleString()}${d.missing_count>0?'〜':''}`
    :'価格情報なし';
  // Q-9: missing_countはカードの種類数（枚数ではない）。「枚」表記だと
  // 3枚積み1種の未取得が実態と異なり「1枚」に見えるため「種」と表記する
  const note=d.priced_count>0
    ?(d.missing_count>0?`${d.missing_count}種未取得`:'全カード取得済み')
    :'';
  return `<span class="meta-deck-price"><span class="meta-deck-price-now">${priceText}</span><span class="meta-deck-price-note">${esc(note)}</span></span>`;
}

function loadMetaTiers(){
  if(_metaLoaded) return Promise.resolve();
  if(_metaLoadPromise) return _metaLoadPromise; // 進行中のfetchに相乗りする
  _metaLoadPromise=_loadMetaTiersInner().finally(()=>{_metaLoadPromise=null;});
  return _metaLoadPromise;
}

async function _loadMetaTiersInner(){
  const el=document.getElementById('metaTierList');
  const CACHE_KEY='tcgym_meta_cache', TTL=3600*1000; // 1時間
  try{
    const raw=sessionStorage.getItem(CACHE_KEY);
    if(raw){
      const {ts,data}=JSON.parse(raw);
      if(Date.now()-ts<TTL){_metaTiers=data;_metaLoaded=true;renderMetaTiers();return;}
    }
  }catch{}
  el.innerHTML='<div class="meta-loading">環境データを読込中...</div>';
  try{
    const res=await fetch('/api/meta');
    _metaTiers=await res.json();
    _metaLoaded=true;
    try{sessionStorage.setItem(CACHE_KEY,JSON.stringify({ts:Date.now(),data:_metaTiers}));}catch{}
    renderMetaTiers();
  }catch(e){
    el.innerHTML='<div class="meta-loading">環境データの取得に失敗しました</div>';
  }
}

function renderMetaTiers(){
  const el=document.getElementById('metaTierList');
  if(!_metaTiers.length){el.innerHTML='<div class="meta-loading">データがありません</div>';return;}

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
      return;
    }
    const sorted=[...priced].sort((a,b)=>{
      const da=_metaPriceMap[a.name], db=_metaPriceMap[b.name];
      const relA=da.total<=0?2:(da.missing_count>0?1:0);
      const relB=db.total<=0?2:(db.missing_count>0?1:0);
      if(relA!==relB) return relA-relB;
      return da.total-db.total;
    });
    let html='<div class="meta-deck-list">';
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
      const dImgSafe=d.image?safeUrl(d.image):'';
      const imgHtml=dImgSafe?`<img src="${escAttr(dImgSafe)}" alt="" style="width:44px;height:62px;border-radius:2px;object-fit:cover;flex-shrink:0">`:'';
      html+=`<button class="meta-deck-btn" onclick="loadMetaDeck('${escAttr(escJs(d.name))}',${rank})" id="meta-btn-${escAttr(d.name)}">
        ${imgHtml} ${esc(d.name)} <span class="meta-share">${d.share}%</span>
        ${_metaDeckPriceHtml(d.name)}
      </button>`;
    }
    html+='</div>';
    html+='<div class="meta-note">環境デッキを今すぐ組むといくらか（一部未取得のカードあり・目安）／データ: TCG PORTAL 大会入賞データより（直近1ヶ月）</div>';
    el.innerHTML=html;
    return;
  }

  // 強い順（既定）: 従来どおりTierグループ表示。各デッキ行に価格データがあれば併記する
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
      <div class="meta-tier-label"><span class="tier-badge ${cls}">${label}</span></div>
      <div class="meta-deck-list">`;
    for(const d of groups[tier]){
      rank++;
      const dImgSafe=d.image?safeUrl(d.image):'';
      const imgHtml=dImgSafe?`<img src="${escAttr(dImgSafe)}" alt="" style="width:44px;height:62px;border-radius:2px;object-fit:cover;flex-shrink:0">`:'';
      html+=`<button class="meta-deck-btn" onclick="loadMetaDeck('${escAttr(escJs(d.name))}',${rank})" id="meta-btn-${escAttr(d.name)}">
        ${imgHtml} ${esc(d.name)} <span class="meta-share">${d.share}%</span>
        ${_metaDeckPriceHtml(d.name)}
      </button>`;
    }
    html+=`</div></div>`;
  }
  // S-3(1): シェア上位デッキだけ価格を計算しているため、金額が出ないデッキがある。
  // 画面から理由が分かるよう、実際に価格を出せたデッキ数を添える
  const pricedCount=Object.keys(_metaPriceMap).length;
  const priceLimitNote=(_metaPricesLoaded&&pricedCount>0&&pricedCount<_metaTiers.length)
    ?`価格は使用率が高い上位${pricedCount}デッキのみ計算しています／`:'';
  html+=`<div class="meta-note">${priceLimitNote}データ: TCG PORTAL 大会入賞データより（直近1ヶ月）</div>`;
  el.innerHTML=html;
}

async function loadMetaDeck(theme,rank){
  // R-1: 旧トップページの top_deck_click を統合先の環境デッキタブに合わせて改名。
  // カード名・デッキ名は送らずクリック順位のみ（従来方針を維持）
  if(rank) _ga('meta_deck_click',{rank:rank});
  const detailEl=document.getElementById('metaDeckDetail');
  const btn=document.getElementById('meta-btn-'+theme);
  if(btn) btn.classList.add('loading');

  // 選択状態を更新
  document.querySelectorAll('.meta-deck-btn').forEach(b=>b.classList.remove('selected'));
  if(btn) btn.classList.add('selected');

  detailEl.classList.remove('hidden');
  detailEl.innerHTML='<div class="meta-loading">主要カードを読込中...</div>';

  try{
    const res=await fetch('/api/meta/deck?theme='+encodeURIComponent(theme));
    const data=await res.json();

    const hasFullDeck=data.full_deck&&data.full_deck.length>0;
    if(!hasFullDeck&&(!data.cards||!data.cards.length)){
      detailEl.innerHTML='<div class="meta-loading">「'+esc(theme)+'」のデッキデータがありません</div>';
      return;
    }

    // デッキテキストと名前をセット
    _lastMetaDeckText=data.deck_text||'';
    _currentDeckName=theme;

    // レシピリンク
    let recipeHtml='';
    if(data.recipes&&data.recipes.length>0){
      recipeHtml='<div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border)">'
        +'<div style="font:.72rem var(--font);color:var(--text-d);margin-bottom:6px">入賞デッキレシピ:</div>';
      for(const r of data.recipes){
        const label=r.title||'レシピを見る';
        const date=r.date?' ('+r.date+')':'';
        recipeHtml+='<a href="'+escAttr(safeUrl(r.url))+'" target="_blank" rel="noopener" style="display:inline-block;margin:2px 4px 2px 0;padding:3px 8px;font:.7rem var(--font);color:var(--accent-l);border:1px solid var(--border);border-radius:4px;text-decoration:none">'+esc(label)+date+'</a>';
      }
      recipeHtml+='</div>';
    }

    const cardCount=hasFullDeck?data.full_deck.length:data.cards.length;
    const totalCards=hasFullDeck?data.full_deck.reduce((s,c)=>s+c.qty,0):0;
    const heading=hasFullDeck
      ?esc(theme)+' — デッキレシピ（'+cardCount+'種 / '+totalCards+'枚）'
      :esc(theme)+' — 主要カード（'+cardCount+'種）';
    const note=hasFullDeck
      ?'最新の入賞デッキレシピ（メイン+EXデッキ）'
      :'採用率40%以上のカードを平均枚数で表示 / 手札誘発・汎用カード含む';

    detailEl.innerHTML='<h4>'+heading+'</h4>'
      +'<div style="margin-bottom:8px">'
      +'<button class="deck-btn deck-btn-secondary" onclick="applyMetaDeckToTextarea()">マイデッキに送る</button>'
      +'</div>'
      +recipeHtml
      +'<div class="meta-note">'+note+'</div>';
    detailEl.scrollIntoView({behavior:'smooth',block:'start'});

    // デッキ選択後に即・見積もり開始（マイデッキと同じ挙動）
    calcDeckEstimate(DECK_CTX.meta);
  }catch(e){
    detailEl.innerHTML='<div class="meta-loading">読込に失敗しました</div>';
  }finally{
    if(btn) btn.classList.remove('loading');
  }
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
// 関数: loadPacks / renderPacks / loadPackCards / _loadFeaturedIntoPackDetail / bulkSearchPackPrices



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

function addToDeck(name){
  // deck-edit.js 読込時は構造化追加（is_ex振り分け＋グリッド即時更新）を使う
  if(window.deckAddCard){ window.deckAddCard(name); switchMode('mydeck','auto'); return; }
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
  const cards=[...sortedMain,...ex];
  const mainCount=sortedMain.length;

  renderDeckGrid(cards,ctx,mainCount);
  loadDeckGridImages(listEl);

  // プレビューのみ（デッキ選択・PDF取込・購入候補変換時）：価格計算は走らせず、カード一覧だけ表示する
  if(opts.previewOnly){
    listEl.querySelectorAll('.deck-grid-cell.loading').forEach(el=>el.classList.remove('loading'));
    progEl.textContent='「簡易計算（DB相場）」または「リアルタイム計算」を押すと価格を取得します';
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
      renderDeckShareUI(ctx,ctx.getName(),total,cards);
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
      renderDeckShareUI(ctx,ctx.getName(),total,cards);
      return;
    }

    // Phase 2: データなしカードだけリアルタイム検索
    totalEl.innerHTML=`¥${dbTotal.toLocaleString()} <span style="font-size:.7em;color:var(--text-d)">+ ${missing.length}枚検索中</span>`;
    progEl.textContent=`${found}枚を即時取得 / 残り${missing.length}枚をリアルタイム検索中...`;

    // 検索対象店舗はサーバ既定（DEFAULT_SHOPS）に一本化。shopsパラメータは送らない
    const missingParam=missing.map(i=>{const c=cards[i];return c.qty>1?`${c.qty} ${c.name}`:c.name}).join('|');
    const params=new URLSearchParams({cards:missingParam});

    const sseRes=await fetch('/api/deck?'+params.toString());
    const reader=sseRes.body.getReader();
    const decoder=new TextDecoder();
    let buffer='',sseFound=0,sseNotFound=0;

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

          if(d.best){
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
          renderDeckShareUI(ctx,ctx.getName(),total,cards);
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

function renderDeckShareUI(ctx,deckName,total,cards){
  _updateSectionSubtotals(ctx); // セクション小計を最新化（簡易計算・リアルタイム両方の確定時に通る）
  const el=document.getElementById(ctx.shareArea);
  if(!el)return;
  _deckShareData[ctx.shareArea]={deckName,total,cards};

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
      <button class="share-btn share-btn-img" onclick="generateDeckImageUI('${escAttr(escJs(ctx.shareArea))}')">デッキ画像を作成</button>
    </div>
    <div class="deck-img-area" id="imgarea-${escAttr(ctx.shareArea)}"></div>`;
}

async function generateDeckImageUI(shareAreaId){
  const data=_deckShareData[shareAreaId];
  if(!data)return;
  const area=document.getElementById('imgarea-'+shareAreaId);
  if(!area)return;

  area.innerHTML='<div class="deck-img-loading">デッキ画像を生成中（30〜60秒かかる場合があります）...</div>';
  const btn=area.previousElementSibling?.querySelector('.share-btn-img');
  if(btn)btn.disabled=true;

  try{
    const res=await fetch('/api/deck-image',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:data.deckName,cards:data.cards,total:data.total})
    });
    if(!res.ok)throw new Error(`HTTP ${res.status}`);
    const blob=await res.blob();
    const url=URL.createObjectURL(blob);
    const fname=`${data.deckName}_デッキ画像.png`;
    area.innerHTML=`
      <div class="deck-img-wrap">
        <img src="${url}" alt="デッキ画像" style="max-width:100%;border-radius:6px;display:block;margin-bottom:8px">
        <div style="font:.8rem var(--font);color:var(--text-d);margin-bottom:8px">この画像を保存してXの投稿画面で添付してください</div>
        <a href="${url}" download="${escAttr(fname)}" class="share-btn">画像をダウンロード</a>
      </div>`;
  }catch(e){
    area.innerHTML='<div style="color:var(--error,#f55);font-size:.85rem">画像の生成に失敗しました。再度お試しください。</div>';
  }finally{
    if(btn)btn.disabled=false;
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
    const res=await fetch(apiUrl+params.toString());
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

          if(d.best){
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
