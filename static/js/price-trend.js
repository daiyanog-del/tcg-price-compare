// 価格推移の常設グラフ（カード詳細。名前の右の相場の箱 #heroMarket の先頭。2026-10-21 案C で小さくした）。
// /api/price-history を別fetchで取り、ライブラリ不使用のインラインSVGで描く。
// 各日・各レアリティごとに全店舗の min(price) を取る（旧Chart.js実装と同じ統計量＝監査F9）。
// mercari-ref.js と同じ構成: 世代番号 gen で古い応答を捨て、setView() は再取得せず描き直す。
// 選択中のレアリティは index-main.js が setView() で渡す。「すべて」「(不明)」のときはグラフを描かず案内1行だけ出す。
//
// 描画方式: 実際の幅を測ってピクセル座標で描く（viewBox 幅＝実幅）。
// 伸縮SVG(preserveAspectRatio=none)だと目盛り文字・丸・月ラベルが歪むため。幅が変わったら ResizeObserver で描き直す。
(function(){
  let gen=0;                       // 世代番号。カードを切り替えたあとに届いた古い応答を捨てる
  let series=null;                 // {レアリティ: {日付: {p:最安値, shops:[最安だった店名]}}}（未取得・失敗・クリア後は null）
  let view={rarity:''};            // rarity=選択中のレアリティ（空=すべて）
  let gaSent=false;                // 読み取り操作のGAは1ページ1回だけ
  let drawnW=0;                    // 直近に描いたときの幅（リサイズでの無駄な再描画を避ける）
  let docDown=null;                // グラフ外タップで吹き出しを消す document リスナー（描き直すたびに付け替え）
  let ro=null;                     // ResizeObserver（初回描画時に1度だけ作る）

  const H=96, PAD={l:0,t:10,b:18};        // グラフ高さと余白（下は月ラベル用。右余白は目盛り文字の長さで決める＝frame）
  const AX_MIN_R=58, AX_CHAR_W=6.2, AX_GAP=12;  // 右余白の下限・等幅10px文字の概算幅・左の隙間(6)＋右の余裕(6)
  const MIN_W=240;                         // 測れない・極端に狭いときの下限幅
  // 線色はレアリティ色にしない: レアリティ色には赤系があり、騰落の赤(上昇)と意味が衝突するため。
  // 金色(--gold)は店舗最安専用なので使わない。サイト共通のアクセント色(--accent-l)に統一する。
  // 色はSVG属性ではなくCSSクラス(.pt-line など)で指定する（index.html）
  const MSG_NONE='このレアリティの価格推移はまだありません';
  const MSG_PICK='レアリティを選ぶと価格推移を表示します';

  function box(){return document.getElementById('priceTrend');}
  function bodyEl(){return document.getElementById('priceTrendBody');}

  // このファイル単独で使うエスケープ（外部由来の文字列は必ず通す）
  function esc(s){
    return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }
  function yen(n){return '¥'+Math.round(Number(n)).toLocaleString('en-US');}

  // 'YYYY-MM-DD' → 通し日数（UTC基準。日付の間隔をX座標に反映するため）
  function dayNum(s){
    const m=/^(\d{4})-(\d{2})-(\d{2})/.exec(s||'');
    return m?Math.round(Date.UTC(+m[1],+m[2]-1,+m[3])/86400000):NaN;
  }
  function md(s){return (+s.slice(5,7))+'/'+(+s.slice(8,10));}

  // 生データ → {レアリティ: {日付: {p: その日の全店舗の最安値, shops: 最安だった店名(同額なら複数)}}}
  function aggregate(rows){
    const out={};
    (rows||[]).forEach(r=>{
      const p=Number(r.price), rar=r.rarity||'(不明)';
      if(!(p>0)||!isFinite(dayNum(r.date))) return;
      const o=out[rar]||(out[rar]={});
      const d=r.date.slice(0,10), shop=r.shop?String(r.shop):'';
      const cur=o[d];
      if(!cur||p<cur.p) o[d]={p:p,shops:shop?[shop]:[]};
      else if(p===cur.p&&shop&&cur.shops.indexOf(shop)<0) cur.shops.push(shop);
    });
    return out;
  }

  function isUnknown(r){return r==='(不明)'||r==='不明'||r==='';}

  // 選択レアリティの系列 [{d,n,p,shops}]（日付昇順）。2日分未満なら null
  function pick(r){
    const o=series[r];
    if(!o) return null;
    const pts=Object.keys(o).sort().map(d=>({d:d,n:dayNum(d),p:o[d].p,shops:o[d].shops.slice().sort()}));
    return pts.length>=2?pts:null;
  }

  // 目盛りにちょうどよい刻み（1,2,2.5,5×10^n）で約n本
  function niceTicks(lo,hi,n){
    const raw=(hi-lo)/n||1, mag=Math.pow(10,Math.floor(Math.log10(raw)));
    const step=Math.max(1,[1,2,2.5,5,10].map(k=>k*mag).find(s=>s>=raw)); // 円の最小単位は1（1円差で0.5刻みにならない）
    const t=[];
    for(let v=Math.ceil(lo/step)*step;v<=hi+1e-9;v+=step) t.push(v);
    return t;
  }

  // 座標計算（DOMに触らない）。縦軸は線形で最小〜最大に上下12%の余白。X は日付の間隔どおり
  function frame(pts,W){
    let lo=Infinity,hi=-Infinity;
    pts.forEach(p=>{if(p.p<lo)lo=p.p; if(p.p>hi)hi=p.p;});
    const rawLo=lo, rawHi=hi;
    let ylo=lo,yhi=hi;
    if(ylo===yhi){ylo-=1;yhi+=1;}
    const m=(yhi-ylo)*0.12; ylo-=m; yhi+=m;
    const n0=pts[0].n, n1=pts[pts.length-1].n, span=Math.max(1,n1-n0);
    // 右余白: 目盛り文字の最長幅に合わせる（¥1,000,000 以上でもはみ出さない）
    const ticks=niceTicks(ylo,yhi,3);
    const maxLen=ticks.reduce((m,v)=>Math.max(m,yen(v).length),0);
    const padR=Math.max(AX_MIN_R,Math.ceil(maxLen*AX_CHAR_W+AX_GAP));
    const X=n=>PAD.l+(n-n0)/span*(W-PAD.l-padR);
    const Y=v=>PAD.t+(1-(v-ylo)/(yhi-ylo))*(H-PAD.t-PAD.b);
    return {W:W,n0:n0,n1:n1,lo:rawLo,hi:rawHi,ylo:ylo,yhi:yhi,X:X,Y:Y,ticks:ticks,padR:padR};
  }

  // 階段線: 前日の値を水平に引き、日付が変わる位置で縦に段差
  function stepPath(pts,F){
    return pts.map((p,i)=>i?'H'+F.X(p.n).toFixed(1)+'V'+F.Y(p.p).toFixed(1):'M'+F.X(p.n).toFixed(1)+' '+F.Y(p.p).toFixed(1)).join('');
  }

  // 月の区切り（月初日。先頭日と同じ日は除く）。年をまたぐときは1月だけ年を付ける
  function monthMarks(F){
    const a=new Date(F.n0*86400000), b=new Date(F.n1*86400000);
    const cross=a.getUTCFullYear()!==b.getUTCFullYear();
    const out=[];
    let y=a.getUTCFullYear(), m=a.getUTCMonth()+1;
    for(;;){
      m++; if(m>12){m=1;y++;}
      const n=Math.round(Date.UTC(y,m-1,1)/86400000);
      if(n>F.n1) break;
      out.push({n:n,label:(cross&&m===1?y+'年':'')+m+'月'});
    }
    return out;
  }

  function gridSvg(F){
    const yl=F.ticks.map(v=>{
      const y=F.Y(v).toFixed(1);
      return `<line class="pt-grid" x1="${PAD.l}" x2="${F.W-F.padR}" y1="${y}" y2="${y}"/>`+
        `<text class="pt-axr" x="${F.W-F.padR+6}" y="${(F.Y(v)+3.5).toFixed(1)}">${esc(yen(v))}</text>`;
    }).join('');
    const ml=monthMarks(F).map(k=>{
      const x=F.X(k.n).toFixed(1);
      return `<line class="pt-grid" x1="${x}" x2="${x}" y1="0" y2="${H-16}"/>`+
        `<text class="pt-ax" x="${(F.X(k.n)+3).toFixed(1)}" y="${H-3}">${esc(k.label)}</text>`;
    }).join('');
    return yl+ml;
  }

  function pct(a,b){
    const v=(b-a)/a*100;
    return (v>0?'▲':v<0?'▼':'')+Math.abs(v).toFixed(1)+'%';
  }

  // 見出し右側の表示（初日→最新日と騰落率＋安値/高値）。なぞり中も変えない
  // 色は .movers-diff.up / .down（上昇=赤・下降=緑）。金色は店舗最安専用なので使わない
  function summaryHtml(pts,F){
    const a=pts[0].p, b=pts[pts.length-1].p;
    const cls=b>a?'up':b<a?'down':'';
    return `<span class="movers-diff ${cls}">${yen(a)} → ${yen(b)} ${pct(a,b)}</span>`+
      `<span class="pt-range">安 ${yen(F.lo)} / 高 ${yen(F.hi)}</span>`;
  }

  // 吹き出しの中身「9/12 ¥220 遊々亭」。同額で複数店なら「遊々亭 ほか」。店名は外部由来なので esc
  function tipHtml(p){
    const shop=p.shops.length?` <span class="pt-tip-shop">${esc(p.shops[0])}${p.shops.length>1?' ほか':''}</span>`:'';
    return `${esc(md(p.d))} <b>${yen(p.p)}</b>${shop}`;
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

  function rawWidth(){
    const el=bodyEl();
    return el&&typeof el.getBoundingClientRect==='function'?Math.round(el.getBoundingClientRect().width):0;
  }

  // 幅が変わったら描き直す（再取得はしない）。ResizeObserver が無い環境では何もしない
  function observe(){
    if(ro||typeof ResizeObserver==='undefined'||!box()) return;
    ro=new ResizeObserver(()=>{
      if(!series) return;
      const w=rawWidth();
      if(w>0&&Math.max(MIN_W,w)!==drawnW) render();
    });
    ro.observe(box());
  }

  // 現在の series / view から描き直す（再取得はしない）
  function render(){
    if(!series||!Object.keys(series).length){hide();return;}
    const r=view.rarity||'';
    // 「すべて」「(不明)」は重ねて描かず、レアリティ選択を促す1行だけ
    if(isUnknown(r)){show(`<p class="pt-note">${esc(MSG_PICK)}</p>`);return;}
    const pts=pick(r);
    if(!pts){show(`<p class="pt-note">${esc(MSG_NONE)}</p>`);return;}
    const el=box(); if(el) el.classList.remove('hidden');   // 表示してから幅を測る
    const W=Math.max(MIN_W,rawWidth()); drawnW=W;
    const F=frame(pts,W), last=pts[pts.length-1];
    const area=stepPath(pts,F)+`V${H-PAD.b}H${F.X(pts[0].n).toFixed(1)}Z`;
    show(`<div class="pt-head"><span class="pt-title">相場の動き（${esc(r)}・日ごとの最安）</span><span class="pt-val">${summaryHtml(pts,F)}</span></div>`+
      `<div class="pt-plot" id="ptPlot"><svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="価格推移グラフ">`+
      `<defs><linearGradient id="ptGrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0" class="pt-stop" stop-opacity=".28"/><stop offset="1" class="pt-stop" stop-opacity="0"/></linearGradient></defs>`+
      gridSvg(F)+
      `<path class="pt-area" d="${area}"/>`+
      `<path class="pt-line" d="${stepPath(pts,F)}"/>`+
      `<circle class="pt-last" cx="${F.X(last.n).toFixed(1)}" cy="${F.Y(last.p).toFixed(1)}" r="3.5"/>`+
      `<line id="ptCursor" class="pt-cursor" x1="0" x2="0" y1="${PAD.t}" y2="${H-PAD.b}" visibility="hidden"/>`+
      `<circle id="ptDot" class="pt-dot" r="4" cx="0" cy="0" visibility="hidden"/></svg>`+
      `<div id="ptTip" class="pt-tip"></div></div>`);
    bindPointer(pts,F);
    observe();
  }

  // カーソル位置(viewBox座標)に最も近い日付の点
  function nearest(pts,F,x){
    let best=pts[0];
    pts.forEach(p=>{if(Math.abs(F.X(p.n)-x)<Math.abs(F.X(best.n)-x)) best=p;});
    return best;
  }

  function bindPointer(pts,F){
    const plot=document.getElementById('ptPlot');
    if(!plot||!plot.addEventListener) return;
    const hi=pts.reduce((b,p)=>p.p>b.p?p:b,pts[0]), lastP=pts[pts.length-1];
    const keepPoints=[hi,lastP].map(p=>({x:F.X(p.n),y:F.Y(p.p)}));
    const cur=document.getElementById('ptCursor'), dot=document.getElementById('ptDot'), tip=document.getElementById('ptTip');
    function move(e){
      const rc=plot.getBoundingClientRect();
      if(!rc.width) return;
      const h=nearest(pts,F,(e.clientX-rc.left)/rc.width*F.W);
      const x=F.X(h.n), px=x/F.W*rc.width;
      if(cur){const s=x.toFixed(1);cur.setAttribute('x1',s);cur.setAttribute('x2',s);cur.setAttribute('visibility','visible');}
      if(dot){dot.setAttribute('cx',x.toFixed(1));dot.setAttribute('cy',F.Y(h.p).toFixed(1));dot.setAttribute('visibility','visible');}
      if(tip){
        tip.innerHTML=tipHtml(h);
        tip.style.display='block';
        const tw=tip.offsetWidth||100, th=tip.offsetHeight||22, half=tw/2;
        const left=Math.min(Math.max(px,half),Math.max(half,rc.width-half));   // 左右端ではみ出さないようクランプ
        tip.style.left=left+'px';
        // 縦位置: ホバー点の上（入らなければ下）。最高値・最新日の点は隠さない
        const sx=rc.width/F.W, keep=keepPoints.map(k=>({x:k.x*sx,y:k.y}));
        const hit=top=>keep.some(k=>k.x>left-half-6&&k.x<left+half+6&&k.y>top-6&&k.y<top+th+6);
        const yy=F.Y(h.p), above=yy-th-10, below=yy+10;
        let top=above>=0&&!hit(above)?above:(below+th<=H&&!hit(below)?below:(above>=0?above:below));
        tip.style.top=Math.max(0,top).toFixed(0)+'px';
      }
      if(!gaSent){gaSent=true;if(typeof _ga==='function') _ga('price_trend_touch');} // カード名は送らない
    }
    function leave(){
      if(cur) cur.setAttribute('visibility','hidden');
      if(dot) dot.setAttribute('visibility','hidden');
      if(tip) tip.style.display='none';
    }
    plot.addEventListener('pointerdown',move);
    plot.addEventListener('pointermove',move);
    // 指(touch)は離しただけでは消さない: 次のタップ（グラフ外も）か pointercancel（スクロール開始）で消す
    plot.addEventListener('pointerleave',e=>{if(!e||e.pointerType!=='touch') leave();});
    plot.addEventListener('pointercancel',leave);
    if(docDown&&document.removeEventListener) document.removeEventListener('pointerdown',docDown);
    docDown=e=>{if(!(e&&e.target&&plot.contains&&plot.contains(e.target))) leave();};
    if(document.addEventListener) document.addEventListener('pointerdown',docDown);
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

  window.PriceTrend={load:load,clear:clear,setView:setView,_niceTicks:niceTicks};
})();
