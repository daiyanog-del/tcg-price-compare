/* 店舗検索の混雑応答を受けて、数秒後に自動で取り直す共通部品（2026-10-22）。
   サーバーは店舗へ取りに行く検索の同時数が上限のとき、すぐ「混雑中」を返す（app.py _search_busy_response）。
   - fetch で読む画面: 応答ヘッダー X-Search-Busy: 1 で判別し、本文は読まずに Retry-After 秒後に取り直す（ShopBusy.fetch）
   - EventSource で読む画面: {"type":"busy","retry_after":秒} の1件だけが届く（各画面で閉じて取り直す）
   どちらも「順番待ち」を出して待つ。失敗表示にはしない。 */
const ShopBusy={
  MAX_TRIES:30,          // 4秒×30回＝約2分待っても空かなければあきらめる
  DEFAULT_RETRY_SEC:4,
  WAIT_TEXT:'混雑のため順番待ち中です（自動で再開します）',
  isBusy(res){ return !!(res&&res.headers&&res.headers.get('X-Search-Busy')==='1'); },
  retryMs(sec){ const n=Number(sec); return (n>0?n:this.DEFAULT_RETRY_SEC)*1000; },
  sleep(ms,signal){
    return new Promise((ok,ng)=>{
      const abortError=()=>{ try{ return new DOMException('Aborted','AbortError'); }catch(_){ const e=new Error('Aborted'); e.name='AbortError'; return e; } };
      if(signal&&signal.aborted){ ng(abortError()); return; }
      const onAbort=()=>{ clearTimeout(t); ng(abortError()); };
      const t=setTimeout(()=>{ if(signal) signal.removeEventListener('abort',onAbort); ok(); },ms);
      if(signal) signal.addEventListener('abort',onAbort,{once:true});
    });
  },
  /** fetch して、混雑応答なら待って取り直す。
      opts.onWait(回数) で「順番待ち」を出し、opts.onResume() で元の表示に戻す。
      opts.isStale() が true になったら取り直しをやめ、最後の応答を返す（呼び出し側の古さ判定で抜ける）。
      init.signal で中止できる（待ちの途中でも AbortError）。MAX_TRIES を超えたら Error を投げる。 */
  async fetch(url,init,opts){
    opts=opts||{};
    const signal=init&&init.signal;
    for(let tries=1;;tries++){
      const res=await fetch(url,init);
      if(!this.isBusy(res)) return res;
      try{ if(res.body) res.body.cancel(); }catch(_){}
      if(opts.isStale&&opts.isStale()) return res;
      if(tries>=this.MAX_TRIES) throw new Error('店舗検索が混雑しています。時間をおいて再度お試しください');
      if(opts.onWait) opts.onWait(tries);
      await this.sleep(this.retryMs(res.headers.get('Retry-After')),signal);
      if(opts.isStale&&opts.isStale()) return res;
      if(opts.onResume) opts.onResume();
    }
  }
};
