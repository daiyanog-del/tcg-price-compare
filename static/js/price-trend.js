// 価格推移の細い常設グラフ（カード詳細。店舗サマリー #sumCards の直後）。
// /api/price-history を別fetchで取り、ライブラリ不使用のインラインSVGで描く。
// 各日・各レアリティごとに全店舗の min(price) を取る（旧Chart.js実装と同じ統計量＝監査F9）。
// mercari-ref.js と同じ構成: 世代番号 gen で古い応答を捨て、setView() は再取得せず描き直す。
// レアリティの選択・店舗結果にあるレアリティは index-main.js が setView() で渡す。
(function(){
  let gen=0;                       // 世代番号。カードを切り替えたあとに届いた古い応答を捨てる
  let series=null;                 // {レアリティ: {日付: 最安値}}（未取得・失敗・クリア後は null）
  let view={rarity:'',stores:[]};  // rarity=選択中のレアリティ（空=すべて）/ stores=店舗結果に存在するレアリティ
  let gaSent=false;                // 読み取り操作のGAは1ページ1回だけ

  const VB_W=1000, VB_H=100;       // SVGの内部座標（実寸は CSS で伸縮）
  const FALLBACK_COLOR='#8a8ca0';  // RARITY_CONFIG に色が無いときのグレー（--text-m 相当）
  const MSG_NONE='このレアリティの価格推移はまだありません';

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

  // 表示対象を決める。{single:選択レアリティ|'' , lines:[{r,pts:[{d,n,p}]}]}。点が2日分未満の系列は描かない
  function pick(){
    let sel=view.rarity||'';
    if(isUnknown(sel)) sel=''; // '(不明)' は「すべて」と同じ扱い
    const st=Array.isArray(view.stores)?view.stores:[];
    let names;
    if(sel){
      names=[sel];
    }else{
      names=Object.keys(series).filter(r=>!isUnknown(r));
      if(st.length) names=names.filter(r=>st.indexOf(r)>=0); // 店舗結果が空なら絞らない
      const ord=r=>{const c=(window.RARITY_CONFIG||{})[r];return c&&typeof c.order==='number'?c.order:9999;};
      names.sort((a,b)=>ord(a)-ord(b));
    }
    const lines=[];
    names.forEach(r=>{
      const o=series[r];
      if(!o) return;
      const pts=Object.keys(o).sort().map(d=>({d:d,n:dayNum(d),p:o[d]}));
      if(pts.length>=2) lines.push({r:r,pts:pts});
    });
    return {single:sel,lines:lines};
  }

  // 描画に必要な座標計算（DOMに触らない）
  function layout(sel){
    const lines=sel.lines;
    let n0=Infinity,n1=-Infinity,lo=Infinity,hi=-Infinity;
    lines.forEach(l=>l.pts.forEach(p=>{
      if(p.n<n0)n0=p.n; if(p.n>n1)n1=p.n;
      if(p.p<lo)lo=p.p; if(p.p>hi)hi=p.p;
    }));
    // 「すべて」は価格帯が数桁違うため対数目盛（価格0以下は aggregate で除外済み）。1本選択は線形
    const f=sel.single?(v=>v):Math.log10;
    // Y軸は線の最小〜最大に上下少し余白。全部同値なら中央に水平線
    let ylo,yhi;
    if(hi===lo){ylo=f(lo)-1;yhi=f(hi)+1;}
    else{const pad=(f(hi)-f(lo))*0.12;ylo=f(lo)-pad;yhi=f(hi)+pad;}
    const span=Math.max(1,n1-n0);
    const X=n=>(n-n0)/span*VB_W;
    const Y=v=>VB_H-(f(v)-ylo)/(yhi-ylo)*VB_H;
    const paths=lines.map(l=>({
      r:l.r,
      d:l.pts.map((p,i)=>(i?'L':'M')+X(p.n).toFixed(1)+' '+Y(p.p).toFixed(1)).join(' '),
    }));
    return {n0:n0,n1:n1,lo:lo,hi:hi,paths:paths,days:n1-n0+1};
  }

  function pct(a,b){
    const v=(b-a)/a*100;
    return (v>0?'▲':v<0?'▼':'')+Math.abs(v).toFixed(1)+'%';
  }

  // 見出し右側の既定表示（選択1本: 初日→最新日と騰落率＋安値/高値。すべて: 安値/高値のみ）
  // 色は .movers-diff.up / .down（上昇=赤・下降=緑）。金色は店舗最安専用なので使わない
  function summaryHtml(sel,L){
    const range=`<span class="pt-range">安 ${yen(L.lo)} / 高 ${yen(L.hi)}</span>`;
    if(!sel.single) return range;
    const pts=sel.lines[0].pts, a=pts[0].p, b=pts[pts.length-1].p;
    const cls=b>a?'up':b<a?'down':'';
    return `<span class="movers-diff ${cls}">${yen(a)} → ${yen(b)} ${pct(a,b)}</span>`+range;
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
    const sel=pick();
    if(!sel.lines.length){
      show(`<p class="pt-note">${esc(sel.single?MSG_NONE:'価格推移はまだありません')}</p>`);
      return;
    }
    const L=layout(sel);
    const title=sel.single
      ?`価格推移（${esc(sel.single)}・最安値・${L.days}日）`
      :`価格推移（レアリティ別の最安値・${L.days}日）`;
    const paths=L.paths.map(p=>`<path d="${p.d}" fill="none" stroke="${esc(colorOf(p.r))}" stroke-width="1.6" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>`).join('');
    const legend=sel.single?'':'<div class="pt-legend">'+sel.lines.map(l=>
      `<span class="pt-chip"><i style="background:${esc(colorOf(l.r))}"></i>${esc(l.r)}</span>`).join('')+'</div>';
    const y0=new Date(L.n0*86400000).getUTCFullYear(), y1=new Date(L.n1*86400000).getUTCFullYear();
    let first='',last='';
    sel.lines.forEach(l=>{
      if(l.pts[0].n===L.n0) first=l.pts[0].d;
      const e=l.pts[l.pts.length-1];
      if(e.n===L.n1) last=e.d;
    });
    const fmt=s=>(y0!==y1?s.slice(0,4)+'/':'')+md(s);
    show(`<div class="pt-head"><span class="pt-title">${title}</span><span class="pt-val" id="ptVal">${summaryHtml(sel,L)}</span></div>`+
      legend+
      `<div class="pt-plot" id="ptPlot"><svg viewBox="0 0 ${VB_W} ${VB_H}" preserveAspectRatio="none" role="img" aria-label="価格推移グラフ">`+
      paths+`<line id="ptCursor" class="pt-cursor" x1="0" x2="0" y1="0" y2="${VB_H}" vector-effect="non-scaling-stroke" visibility="hidden"/></svg></div>`+
      `<div class="pt-axis"><span>${esc(fmt(first))}</span><span>${esc(fmt(last))}</span></div>`);
    bindPointer(sel,L);
  }

  // カーソル位置に最も近い日付と、その日の表示値（すべてモードは最安のレアリティ）
  function nearest(sel,L,frac){
    const target=L.n0+Math.min(1,Math.max(0,frac))*(L.n1-L.n0);
    let best=null;
    sel.lines.forEach(l=>l.pts.forEach(p=>{
      if(best==null||Math.abs(p.n-target)<Math.abs(best-target)) best=p.n;
    }));
    let d='',minP=Infinity,minR='';
    sel.lines.forEach(l=>l.pts.forEach(p=>{
      if(p.n===best&&p.p<minP){d=p.d;minP=p.p;minR=l.r;}
    }));
    return {n:best,d:d,p:minP,r:minR};
  }

  function bindPointer(sel,L){
    const plot=document.getElementById('ptPlot');
    if(!plot||!plot.addEventListener) return;
    const cur=document.getElementById('ptCursor'), val=document.getElementById('ptVal');
    const base=summaryHtml(sel,L);
    function move(e){
      const rc=plot.getBoundingClientRect();
      if(!rc.width) return;
      const h=nearest(sel,L,(e.clientX-rc.left)/rc.width);
      if(cur){
        const x=((h.n-L.n0)/Math.max(1,L.n1-L.n0)*VB_W).toFixed(1);
        cur.setAttribute('x1',x);cur.setAttribute('x2',x);cur.setAttribute('visibility','visible');
      }
      if(val) val.innerHTML=`<span class="pt-read">${esc(md(h.d))} ${sel.single?'':esc(h.r)+' '}${yen(h.p)}</span>`;
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

  // 選択レアリティ・店舗結果のレアリティが変わったとき（再取得せず描き直す）
  function setView(v){
    view={rarity:(v&&v.rarity)||'',stores:(v&&Array.isArray(v.stores))?v.stores:[]};
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
