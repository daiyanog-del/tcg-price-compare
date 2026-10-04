/* 取得失敗を正常0件や確定した最安値に見せないための共通表示。 */
const SearchStatus = {
  message(data){
    const failed=data.failed_shops||[];
    if(!failed.length)return '';
    if(data.status==='failed')return '全店舗の取得に失敗しました。時間をおいて再試行してください。';
    const ok=(data.successful_shops||[]).length;
    return `${data.shop_count}店舗中${ok}店舗取得・${failed.length}店舗で取得失敗。表示価格は取得できた範囲の参考値です（${failed.join('・')}）。`;
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
  show(data){
    const box=document.getElementById('searchCoverage');
    if(!box)return;
    const message=this.message(data);
    box.hidden=!message;
    box.querySelector('span').textContent=message;
  }
};
