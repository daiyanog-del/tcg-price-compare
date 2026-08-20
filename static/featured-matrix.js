// 新弾フィーチャー「店舗×レアリティ価格マトリクス」表示。
// packs.js の _loadFeaturedIntoPackDetail から呼ばれるトグルボタンで表示/非表示を切り替える。
// esc / escJs / escAttr / safeUrl / wishAdd / _wishShopSearchUrl は index.html 本体の
// グローバル関数を参照する（このファイルは非モジュールで読み込むこと）。
//
// 状態は極力DOM側（コンテナの classList / dataset）に持たせる。モジュールレベル変数で
// 表示状態を保持すると、packs.js が pack 切替のたびに detailEl.innerHTML を丸ごと
// 差し替える（＝コンテナ要素が作り直される）際に状態がリセットされず、1回目クリック
// 無反応→2回目で空コンテナ表示、という不整合が起きていた（2026-08-08 reviewer指摘）。

// カードリスト表示 ⇔ マトリクス表示のトグル
async function toggleFeaturedMatrix(){
  const grid=document.getElementById('packDetailGrid');
  const container=document.getElementById('featuredMatrixContainer');
  const btn=document.getElementById('fmxToggleBtn');
  if(!grid||!container) return;

  // 現在の表示状態はDOM（コンテナのhiddenクラス）から判定する
  const currentlyVisible=!container.classList.contains('hidden');
  if(!currentlyVisible){
    grid.classList.add('hidden');
    container.classList.remove('hidden');
    if(btn) btn.textContent='📋 収録カードリストに戻る';
    if(container.dataset.fmxLoaded!=='1'){
      container.innerHTML='<div class="meta-loading">価格マトリクスを読込中...</div>';
      await _loadFeaturedMatrix(container);
    }
  }else{
    grid.classList.remove('hidden');
    container.classList.add('hidden');
    if(btn) btn.textContent='📊 店舗×レア比較';
  }
}

async function _loadFeaturedMatrix(container){
  try{
    const res=await fetch('/api/featured/matrix');
    const data=await res.json();
    if(!data||data.error){
      container.innerHTML='<div class="meta-loading">価格マトリクスを取得できませんでした</div>';
      return;
    }
    if(data.loading){
      // サーバー側キャッシュ構築中（/api/featured と同じ非ブロッキング方式）。2.5秒後に再試行
      container.innerHTML='<div class="meta-loading">価格マトリクスを準備中... しばらくお待ちください</div>';
      setTimeout(()=>{
        // 再表示中（hiddenでない）の場合のみ再取得する（別パックに切り替えていたら止める）
        if(!container.classList.contains('hidden')) _loadFeaturedMatrix(container);
      }, 2500);
      return;
    }
    container.dataset.fmxLoaded='1';
    _renderFeaturedMatrix(data, container);
  }catch(e){
    container.innerHTML='<div class="meta-loading">価格マトリクスの取得に失敗しました</div>';
  }
}

// セルの背景色: 行内最安値を緑、そこから離れるほど薄い赤へグラデーション
function _fmxCellStyle(price, minPrice){
  if(price==null||minPrice==null||minPrice<=0) return '';
  if(price<=minPrice) return 'background:rgba(52,211,153,.16)'; // --green ベース
  const ratio=Math.min((price-minPrice)/minPrice, 1); // 最安からの乖離率（1=倍額で頭打ち）
  const alpha=0.06+ratio*0.22;
  return `background:rgba(220,38,38,${alpha.toFixed(2)})`; // --red ベース
}

function _fmxFormatDate(recordedAt){
  if(!recordedAt) return '';
  const parts=recordedAt.split('-');
  if(parts.length!==3) return '';
  return `${parseInt(parts[1],10)}/${parseInt(parts[2],10)}`;
}

function _renderFeaturedMatrix(data, container){
  const shops=data.shops||[];
  const rows=data.rows||[];
  // 「当日でないセルにだけ日付バッジを出す」の基準はクライアント時計ではなく
  // API応答の updated_at（マトリクス内の最新観測日）を使う（2026-08-08 reviewer指摘:
  // 深夜帯などクライアントとサーバのローカル日付がずれるケースを避ける）
  const updatedAt=data.updated_at||'';

  if(!shops.length||!rows.length){
    container.innerHTML='<div class="meta-loading">価格マトリクスのデータがまだありません</div>';
    return;
  }

  let html=`<div class="meta-note" style="margin-bottom:8px">更新日: ${esc(updatedAt)}</div>`;
  html+='<div class="fmx-scroll"><table class="fmx-table"><thead><tr>';
  html+='<th class="fmx-sticky-col">カード / レア</th>';
  for(const shop of shops){
    html+=`<th>${esc(shop)}</th>`;
  }
  html+='<th></th></tr></thead><tbody>';

  let prevName=null;
  rows.forEach((row)=>{
    const sameCardAsPrev=(row.name===prevName);
    prevName=row.name;
    const nameCell=sameCardAsPrev
      ? `<span class="fmx-name-repeat">${esc(row.name)}</span>`
      : `<span class="fmx-name">${esc(row.name)}</span>`;
    const rarityBadge=row.rarity?`<span class="fmx-rarity-badge">${esc(row.rarity)}</span>`:'';

    const cells=row.cells||{};
    const prices=shops.map(s=>cells[s]?cells[s].price:null).filter(p=>p!=null);
    const minPrice=prices.length?Math.min(...prices):null;

    html+=`<tr><td class="fmx-sticky-col">${nameCell}<br>${rarityBadge}</td>`;
    for(const shop of shops){
      const cell=cells[shop];
      if(!cell||cell.price==null){
        html+='<td class="fmx-cell fmx-cell-empty">—</td>';
        continue;
      }
      const style=_fmxCellStyle(cell.price, minPrice);
      const url=cell.url||_wishShopSearchUrl(shop, row.name);
      const priceStr=`¥${Number(cell.price).toLocaleString()}`;
      const dateStr=(cell.recorded_at&&updatedAt&&cell.recorded_at!==updatedAt)
        ?`<div class="fmx-cell-date">${esc(_fmxFormatDate(cell.recorded_at))}</div>`:'';
      const codeStr=cell.code?`<div class="fmx-cell-code">${esc(cell.code)}</div>`:'';
      const titleText=cell.code?`${shop} / ${cell.code}`:shop;
      const link=url
        ? `<a href="${escAttr(safeUrl(url))}" target="_blank" rel="noopener">${priceStr}</a>`
        : priceStr;
      html+=`<td class="fmx-cell" style="${style}" title="${escAttr(titleText)}">${link}${dateStr}${codeStr}</td>`;
    }
    html+=`<td class="fmx-cell-action"><button class="wish-add" onclick="_fmxAddWish(this,'${escAttr(escJs(row.name))}','${escAttr(escJs(row.rarity||''))}')">+ 候補</button></td>`;
    html+='</tr>';
  });

  html+='</tbody></table></div>';
  container.innerHTML=html;
}

// マトリクス行の「+ 候補」ボタン（rarity を渡す必要があるため wishAddFromBtn ではなく直接 wishAdd を呼ぶ）
function _fmxAddWish(btn, name, rarity){
  wishAdd(name, 1, rarity);
  const orig=btn.textContent;
  btn.classList.add('added');
  btn.textContent='✓ 追加';
  setTimeout(()=>{btn.classList.remove('added');btn.textContent=orig;},1200);
}
