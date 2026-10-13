// 価格推移の細い常設グラフ（カード詳細。店舗サマリー #sumCards の直後）。
// /api/price-history を別fetchで取り、ライブラリ不使用のインラインSVGで描く。
// 各日・各レアリティごとに全店舗の min(price) を取る（旧Chart.js実装と同じ統計量＝監査F9）。
// mercari-ref.js と同じ構成: 世代番号 gen で古い応答を捨て、setView() は再取得せず描き直す。
// 選択中のレアリティは index-main.js が setView() で渡す。「すべて」「(不明)」のときはグラフを描かず案内1行だけ出す。
(function(){
  let gen=0;                       // 世代番号。カードを切り替えたあとに届いた古い応答を捨てる
  let series=null;                 // {レアリティ: {日付: 最安値}}（未取得・失敗・クリア後は null）
  let view={rarity:''};            // rarity=選択中のレアリティ（空=すべて）
  let gaSent=false;                // 読み取り操作のGAは1ページ1回だけ

  const VB_W=1000, VB_H=100;       // SVGの内部座標（実寸は CSS で伸縮）
  const FALLBACK_COLOR='#8a8ca0';  // RARITY_CONFIG に色が無いときのグレー（--text-m 相当）
  const MSG_NONE='このレアリティの価格推移はまだありません';
  const MSG_PICK='レアリティを選ぶと価格推移を表示します';

  function box(){return document.getElementById('priceTrend');}
  function bodyEl(){return document.getElementById('priceTrendBody');}

  // このファイル単独で使うエスケープ（外部由来の文字列は必ず通す）
  function esc(s){
    return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }
  function yen(n){return '¥'+Number(n).toLocaleString();}

  function colorOf(r){
    const cfg=(window.RARITY_CONFIG||{})[r];
    const c=cfg&&typeof cfg.color==='string'?cfg.color:'';
    return /^[#a-zA-Z0-9(),.%\s-]+$/.test(c)?c:FALLBACK_COLOR;
  }

  // 'YYYY-MM-DD' → 通し日数（UTC基準。日付の間隔をX座標に反映するため）
  function dayNum(s){
    const m=/^(\d{4})-(\d{2})-(\d{2})/.exec(s||'');
    return m?Math.round(Date.UTC(+m[1],+m[2]-1,+m[3])/86400000):NaN;
  }
  function md(s){return (+s.slice(5,7))+'/'+(+s.slice(8,10));}

  // 生データ → {レアリティ: {日付: その日の全店舗の最安値}}
  function aggregate(rows){
    const out={};
    (rows||[]).forEach(r=>{
      const p=Number(r.price), rar=r.rarity||'(不明)';
      if(!(p>0)||!isFinite(dayNum(r.date))) return;
      const o=out[rar]||(out[rar]={});
      const d=r.date.slice(0,10);
      if(o[d]==null||p<o[d]) o[d]=p;
    });
    return out;
  }

  function isUnknown(r){return r==='(不明)'||r==='不明'||r==='';}

  // 選択レアリティの系列 [{d,n,p}]（日付昇順）。2日分未満なら null
  function pick(r){
    const o=series[r];
    if(!o) return null;
    const pts=Object.keys(o).sort().map(d=>({d:d,n:dayNum(d),p:o[d]}));
    return pts.length>=2?pts:null;
  }

  // 描画に必要な座標計算（DOMに触らない）。縦軸は線形
  function layout(pts){
    const n0=pts[0].n, n1=pts[pts.length-1].n;
    let lo=Infinity,hi=-Infinity;
    pts.forEach(p=>{if(p.p<lo)lo=p.p; if(p.p>hi)hi=p.p;});
    // Y軸は線の最小〜最大に上下少し余白。全部同値なら中央に水平線
    let ylo,yhi;
    if(hi===lo){ylo=lo-1;yhi=hi+1;}
    else{const pad=(hi-lo)*0.12;ylo=lo-pad;yhi=hi+pad;}
    const span=Math.max(1,n1-n0);
    const X=n=>(n-n0)/span*VB_W;
    const Y=v=>VB_H-(v-ylo)/(yhi-ylo)*VB_H;
    const d=pts.map((p,i)=>(i?'L':'M')+X(p.n).toFixed(1)+' '+Y(p.p).toFixed(1)).join(' ');
    return {n0:n0,n1:n1,lo:lo,hi:hi,d:d,days:n1-n0+1};
  }

  function pct(a,b){
    const v=(b-a)/a*100;
    return (v>0?'▲':v<0?'▼':'')+Math.abs(v).toFixed(1)+'%';
  }

  // 見出し右側の既定表示（初日→最新日と騰落率＋安値/高値）
  // 色は .movers-diff.up / .down（上昇=赤・下降=緑）。金色は店舗最安専用なので使わない
  function summaryHtml(pts,L){
    const a=pts[0].p, b=pts[pts.length-1].p;
    const cls=b>a?'up':b<a?'down':'';
    return `<span class="movers-diff ${cls}">${yen(a)} → ${yen(b)} ${pct(a,b)}</span>`+
      `<span class="pt-range">安 ${yen(L.lo)} / 高 ${yen(L.hi)}</span>`;
  }

  function show(html){
    const el=box(),body=bodyEl();
    if(!el||!body) return;
    el.classList.remove('hidden');
    body.innerHTML=html;
  }

  function hide(){
    const el=box(),body=bodyEl();
    if(el) el.classList.add('hidden');
    if(body) body.innerHTML='';
  }

  // 現在の series / view から描き直す（再取得はしない）
  function render(){
    if(!series||!Object.keys(series).length){hide();return;}
    const r=view.rarity||'';
    // 「すべて」「(不明)」は重ねて描かず、レアリティ選択を促す1行だけ
    if(isUnknown(r)){show(`<p class="pt-note">${esc(MSG_PICK)}</p>`);return;}
    const pts=pick(r);
    if(!pts){show(`<p class="pt-note">${esc(MSG_NONE)}</p>`);return;}
    const L=layout(pts);
    const y0=new Date(L.n0*86400000).getUTCFullYear(), y1=new Date(L.n1*86400000).getUTCFullYear();
    const fmt=s=>(y0!==y1?s.slice(0,4)+'/':'')+md(s);
    show(`<div class="pt-head"><span class="pt-title">価格推移（${esc(r)}・最安値・${L.days}日）</span><span class="pt-val" id="ptVal">${summaryHtml(pts,L)}</span></div>`+
      `<div class="pt-plot" id="ptPlot"><svg viewBox="0 0 ${VB_W} ${VB_H}" preserveAspectRatio="none" role="img" aria-label="価格推移グラフ">`+
      `<path d="${L.d}" fill="none" stroke="${esc(colorOf(r))}" stroke-width="1.6" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>`+
      `<line id="ptCursor" class="pt-cursor" x1="0" x2="0" y1="0" y2="${VB_H}" vector-effect="non-scaling-stroke" visibility="hidden"/></svg></div>`+
      `<div class="pt-axis"><span>${esc(fmt(pts[0].d))}</span><span>${esc(fmt(pts[pts.length-1].d))}</span></div>`);
    bindPointer(pts,L);
  }

  // カーソル位置に最も近い日付の点
  function nearest(pts,L,frac){
    const target=L.n0+Math.min(1,Math.max(0,frac))*(L.n1-L.n0);
    let best=pts[0];
    pts.forEach(p=>{if(Math.abs(p.n-target)<Math.abs(best.n-target)) best=p;});
    return best;
  }

  function bindPointer(pts,L){
    const plot=document.getElementById('ptPlot');
    if(!plot||!plot.addEventListener) return;
    const cur=document.getElementById('ptCursor'), val=document.getElementById('ptVal');
    const base=summaryHtml(pts,L);
    function move(e){
      const rc=plot.getBoundingClientRect();
      if(!rc.width) return;
      const h=nearest(pts,L,(e.clientX-rc.left)/rc.width);
      if(cur){
        const x=((h.n-L.n0)/Math.max(1,L.n1-L.n0)*VB_W).toFixed(1);
        cur.setAttribute('x1',x);cur.setAttribute('x2',x);cur.setAttribute('visibility','visible');
      }
      if(val) val.innerHTML=`<span class="pt-read">${esc(md(h.d))} ${yen(h.p)}</span>`;
      if(!gaSent){gaSent=true;if(typeof _ga==='function') _ga('price_trend_touch');} // カード名は送らない
    }
    function leave(){
      if(cur) cur.setAttribute('visibility','hidden');
      if(val) val.innerHTML=base;
    }
    plot.addEventListener('pointerdown',move);
    plot.addEventListener('pointermove',move);
    plot.addEventListener('pointerleave',leave);
    plot.addEventListener('pointercancel',leave);
  }

  // 選択レアリティが変わったとき（再取得せず描き直す）
  function setView(v){
    view={rarity:(v&&v.rarity)||''};
    if(series) render();
  }

  function clear(){
    gen++;
    series=null;
    hide();
  }

  function load(name){
    if(!box()||!name) return;
    const my=++gen;
    series=null;
    hide();
    fetch('/api/price-history?card='+encodeURIComponent(name)).then(async res=>{
      if(my!==gen) return;
      let d=null;
      try{d=await res.json();}catch(e){d=null;}
      if(my!==gen) return;
      if(!res.ok||!d||!Array.isArray(d.data)||!d.data.length){hide();return;} // 空・失敗は枠ごと非表示
      series=aggregate(d.data);
      render();
    }).catch(()=>{
      if(my!==gen) return;
      hide();
    });
  }

  window.PriceTrend={load:load,clear:clear,setView:setView};
})();
