/* 取得失敗を正常0件や確定した最安値に見せないための共通表示。 */
const SearchStatus = {
  message(data){
    const failed=data.failed_shops||[];
    if(!failed.length)return '';
    // 混雑で順番が回ってこなかった店（busy_shops）は店の障害ではないので、取得失敗と分けて書く。
    // その店は手元に残っている前回の結果で埋めている（無ければ空）
    const busy=(data.busy_shops||[]).filter(s=>failed.includes(s));
    const broken=failed.filter(s=>!busy.includes(s));
    if(data.status==='failed'){
      return broken.length?'全店舗の取得に失敗しました。時間をおいて再試行してください。'
        :'混雑のため店舗から取得できませんでした。少し時間をおいて再度お試しください。';
    }
    const ok=(data.successful_shops||[]).length;
    if(!busy.length) return `${data.shop_count}店舗中${ok}店舗取得・${failed.length}店舗で取得失敗。表示価格は取得できた範囲の参考値です（${failed.join('・')}）。`;
    let text=`${data.shop_count}店舗中${ok}店舗取得`;
    if(broken.length) text+=`・${broken.length}店舗で取得失敗（${broken.join('・')}）`;
    text+=`・${busy.length}店舗は混雑で今回は取得できず、前回の結果があれば表示（${busy.join('・')}）`;
    return text+'。表示価格は取得できた範囲の参考値です。';
  },
  annotate(row,data){
    const message=this.message(data);
    if(!row)return;
    let note=row.querySelector('.search-failure-note');
    if(!message){if(note)note.remove();return;}
    if(!note){note=document.createElement('div');note.className='search-failure-note';row.appendChild(note);}
    note.textContent=message;
    note.setAttribute('role','status');
  },
  // 休止中の店（サーバーの SUSPENDED_SHOPS。body の data-suspended-shops）。2026-10-22 カーナベル休止。
  // 黙って1店消えると不審に見えるので、価格の対象から外していることを書く。exclude はその欄に元から無い店
  suspendedNote(exclude=[]){
    const v=(document.body&&document.body.dataset.suspendedShops)||'';
    const shops=v.split(',').filter(s=>s&&!exclude.includes(s));
    return shops.length?`${shops.join('・')}はサイトを休止中のため対象外です。`:'';
  },
  // 一覧の下に添える短い注記（一部の店が取れなかったとき）。どの店が一覧に入っていないかだけを書く
  listNote(data){
    const failed=data.failed_shops||[];
    if(!failed.length||data.status==='failed')return '';
    const busy=(data.busy_shops||[]).filter(s=>failed.includes(s));
    const broken=failed.filter(s=>!busy.includes(s));
    let text='';
    if(broken.length) text+=`${broken.join('・')}は今回取得できなかったため、この一覧に入っていません。`;
    if(busy.length) text+=`${busy.join('・')}は混雑で今回は取得できず、前回の結果があれば表示しています。`;
    return text;
  },
  // 一覧があるときは一覧の下（#crFailNote）に1行で添え、検索窓の下の箱（#searchCoverage）は使わない。
  // 箱はカードの欄を押し下げ、浮かべると一覧を覆ったため（2026-11-01 スマホで見たい情報を下へ追いやっていた）。
  // 一覧が無いとき（全店失敗・0件）は添える先が無いので、今までどおり箱で知らせる
  show(data){
    const hasList=data.status!=='failed'&&data.total>0;
    const box=document.getElementById('searchCoverage');
    if(box){
      const message=hasList?'':this.message(data);
      box.hidden=!message;
      box.querySelector('span').textContent=message;
    }
    const note=document.getElementById('crFailNote');
    if(note){
      const text=hasList?this.listNote(data):'';
      note.hidden=!text;
      note.querySelector('span').textContent=text;
    }
  }
};
