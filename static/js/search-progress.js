/* 店舗検索の進み具合（2026-10-28）。店ごとの札（店名・件数・cache・順番待ち）をやめ、文言とバー1本で見せる。
   店ごとの状態は作り手の動作確認用で、利用者には読めても使い道が無かった。スマホでは店名が途中で折れていた。
   店の数も出さない（バーの長さと同じことを言うだけで、数が少なく見える）。取れなかった店は完了後に SearchStatus が書く。
   販売の検索では先出しのカード欄の名前の下（結果で最安の行が出る位置）に出す。見た目は static/css/card-result.css の .search-prog。
   バーは店が終わったときだけ伸びるので、遅い店が残ると止まって見える（同日ユーザー指摘）。
   探している間は空きの側（.sp-rest）に光を流し（.sp-running）、終盤に遅い店だけが残ったら文言でそう言う。 */
const SearchProgress={
  BUSY_LABEL:'混雑のため順番待ち中',
  SLOW_LABEL:'応答の遅い店を待っています',
  MIN_PCT:6, // 始めた直前にバーが空に見えないよう、最初から少しだけ伸ばしておく
  SLOW_MS:2000, // 終盤の店が終わらないまま、これだけ経ったら SLOW_LABEL にする。TODO: calibrate from data
  /** 終盤＝残りが1店、または全体の4分の1以下（店の数が増えても最後の数店で出す） */
  isTail(left,n){ return left>0 && (left===1 || left*4<=n); },
  /** host の末尾にバーを足して、操作用のオブジェクトを返す。label は通常時の文言 */
  create(host,label){
    const el=document.createElement('div');
    el.className='search-prog sp-running';
    el.innerHTML='<div class="sp-label"></div><div class="sp-track"><div class="sp-fill"></div><div class="sp-rest"></div></div>';
    host.appendChild(el);
    const labelEl=el.querySelector('.sp-label'), fill=el.querySelector('.sp-fill');
    const known=new Set(), done=new Set();
    let total=0, busy=false, slow=false, slowTimer=null;
    const paint=()=>{
      labelEl.textContent=busy?this.BUSY_LABEL:slow?this.SLOW_LABEL:label;
      const n=Math.max(total,known.size);
      const pct=n?Math.round(done.size/n*100):0;
      fill.style.width=Math.max(this.MIN_PCT,pct)+'%';
      el.classList.toggle('sp-running',!(n&&done.size>=n)); // 全店終わったら光を止める
    };
    // 店が終わるたびに数え直す。終盤なら SLOW_MS 後に文言を変える（その前に次の店が終われば取り消す）
    const armSlow=()=>{
      clearTimeout(slowTimer); slowTimer=null; slow=false;
      const n=Math.max(total,known.size);
      if(!this.isTail(n-done.size,n)) return;
      slowTimer=setTimeout(()=>{ slow=true; paint(); },this.SLOW_MS);
    };
    paint();
    return {
      el,
      /** 調べる店の一覧（販売の検索はサーバーが最初に送る。買取は送らないので start で数える） */
      setShops(names){ (names||[]).forEach(s=>known.add(s)); total=known.size; paint(); },
      start(shop){ known.add(shop); paint(); },
      /** 取れた・取れなかったに関わらず、その店の検索が終わった */
      finish(shop){ known.add(shop); done.add(shop); armSlow(); paint(); },
      setBusy(b){ busy=!!b; paint(); },
      /** 通常時の文言を変える（前回の結果を先に出している間の「○分前の価格を表示中」など） */
      setLabel(text){ label=text; paint(); },
    };
  },
};
