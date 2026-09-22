// 元は templates/index.html のインライン <script>（2026-09-22 バッチCで外部化）。
// Jinja 依存の値は index.html 側の小さなインライン script が定義する。
// 端末間同期: トップページ（カード個別ページでない）を開いたときだけ pull する（§8）。
// 購入候補・保存デッキの両方を1回のAPI呼び出しで扱う（P2）。
// sync-client.js は defer 読み込みのため、この<script>ブロック自体（非defer）は
// パース中にsync-client.jsより先に実行されてしまい、直接呼ぶと window.SyncClient が
// 未定義になる。DOMContentLoaded（deferスクリプトの実行完了後に発火）の中へ移す
// （_pageCardName は先行スクリプトのトップレベル const だが、クラシックscript間では
// 同一グローバルレキシカル環境を共有するため参照できる）
document.addEventListener('DOMContentLoaded', function(){
  if(!_pageCardName && window.SyncClient) window.SyncClient.pullIfNeeded();
});

// ── 端末間同期: 「他の端末でも見る」ダイアログ・状態表示・解除（P3。設計文書 §7.1・§7.3）──
// ネットワーク呼び出し・localStorage操作のロジックは sync-client.js 側（SyncClient.*）に
// 集約されており、ここは配線（ダイアログの開閉・QR描画・カウントダウン表示）のみを行う。

let _syncShareTimerId=null;
let _syncShareInFlight=false; // 2026-08-17: 連打でリクエストを重ねて429を誘発しないためのガード

// qrcode.js は「他の端末でも見る」ダイアログでQRを描くときにしか使わないため、
// <script>タグでの静的読み込みをやめ、初回使用時だけ動的に挿入する（トップページの
// 初期読み込みを軽くするため）。_qrcodeLoadPromise で多重挿入を防ぐ。
// 読み込みに失敗してもQR無しでダイアログ自体は表示する既存のフォールバックを維持する
// （レビュー指摘Low: onerror時はPromiseをnullに戻し、次回呼び出しで再試行できるようにする）。
let _qrcodeLoadPromise=null;
function _ensureQrcodeLoaded(){
  if(window.qrcode) return Promise.resolve(true);
  if(_qrcodeLoadPromise) return _qrcodeLoadPromise;
  _qrcodeLoadPromise=new Promise(function(resolve){
    const s=document.createElement('script');
    s.src=_QRCODE_SRC;
    s.onload=function(){ resolve(!!window.qrcode); };
    s.onerror=function(){ _qrcodeLoadPromise=null; resolve(false); };
    document.head.appendChild(s);
  });
  return _qrcodeLoadPromise;
}

function openSyncShareDialog(){
  // 発行中に「他の端末でも見る」を連打しても新しいリクエストを重ねない
  // （429を自ら誘発しないため。司令塔指摘#1・#2に対応）
  if(_syncShareInFlight) return;

  const overlay=document.getElementById('syncShareOverlay');
  overlay.classList.add('active');
  document.body.style.overflow='hidden';
  document.getElementById('syncShareQr').innerHTML='';
  document.getElementById('syncShareUrl').value='';
  document.getElementById('syncShareUrl').placeholder='発行中…';
  document.getElementById('syncShareTimer').textContent='発行しています…';
  _setSyncShareError('');
  if(_syncShareTimerId){ clearInterval(_syncShareTimerId); _syncShareTimerId=null; }

  if(!window.SyncClient){
    document.getElementById('syncShareTimer').textContent='';
    _setSyncShareError('同期機能を読み込めませんでした。ページを再読み込みしてください。');
    return;
  }
  _syncShareInFlight=true;
  window.SyncClient.startLinkShare().then(function(res){
    document.getElementById('syncShareTimer').textContent='';
    if(!res || !res.ok){
      _setSyncShareError(_syncShareErrorText(res && res.reason));
      return;
    }
    document.getElementById('syncShareUrl').value=res.url;
    _ensureQrcodeLoaded().then(function(ok){
      if(!ok){
        // 読み込み失敗時はQR無しでダイアログを表示する（既存のフォールバック）。
        // 何も出ないと失敗に気づけないため、QR枠に文言を出してURLコピーへ誘導する
        document.getElementById('syncShareQr').innerHTML=
          '<span style="color:var(--text-m);font-size:.82rem">QRを表示できませんでした。上のURLをコピーしてください</span>';
        return;
      }
      const qr=qrcode(0,'M');
      qr.addData(res.url);
      qr.make();
      document.getElementById('syncShareQr').innerHTML=qr.createSvgTag(5);
    });
    _startSyncShareCountdown(res.expires_in);
    _refreshSyncStatusUI();
  }).catch(function(){
    // startLinkShare は原則rejectしない設計だが、想定外の例外でも画面に必ず何か表示する
    // （司令塔指摘#2: 「失敗しても画面に何も出ない」を二度と起こさないための最終防御）
    document.getElementById('syncShareTimer').textContent='';
    _setSyncShareError(_syncShareErrorText('network_error'));
  }).finally(function(){
    _syncShareInFlight=false;
  });
}

function closeSyncShareDialog(){
  document.getElementById('syncShareOverlay').classList.remove('active');
  document.body.style.overflow='';
  if(_syncShareTimerId){ clearInterval(_syncShareTimerId); _syncShareTimerId=null; }
}

function _startSyncShareCountdown(seconds){
  let remain=seconds;
  const el=document.getElementById('syncShareTimer');
  function tick(){
    if(remain<=0){
      clearInterval(_syncShareTimerId);
      _setSyncShareError('リンクの有効期限が切れました。もう一度「他の端末でも見る」を開いてください。');
      return;
    }
    const m=Math.floor(remain/60), s=remain%60;
    el.textContent='有効期限: あと '+m+':'+String(s).padStart(2,'0');
    remain--;
  }
  tick();
  _syncShareTimerId=setInterval(tick,1000);
}

function _setSyncShareError(msg){
  const el=document.getElementById('syncShareError');
  if(!msg){ el.style.display='none'; el.textContent=''; return; }
  el.style.display='block';
  el.textContent=msg;
}

// 端末間同期の操作（発行・解除）に共通の失敗理由→文言マップ。
// 司令塔指摘#2: 429でも「混み合っています」等、理由がユーザーに分かる表示を必ず出す
function _syncShareErrorText(reason){
  const map={
    no_local_data:'購入候補かデッキを1件以上登録してからお試しください。',
    db_unavailable:'現在サーバーに接続できません。しばらくしてからもう一度お試しください。',
    rate_limited:'混み合っています。数秒後にもう一度お試しください。',
    network_error:'通信に失敗しました。通信状況を確認してもう一度お試しください。',
    not_found:'同期の状態が見つかりませんでした。ページを再読み込みしてください。',
    init_failed:'同期の初期化に失敗しました。もう一度お試しください。',
    not_linked:'この端末は同期されていません。',
    invalid_response:'サーバーからの応答が不正でした。もう一度お試しください。',
  };
  return map[reason] || '操作に失敗しました。もう一度お試しください。';
}

function copySyncShareLink(){
  const input=document.getElementById('syncShareUrl');
  if(!input.value) return;
  input.select();
  const btn=document.getElementById('syncShareCopyBtn');
  const done=()=>{ const orig='コピー'; btn.textContent='コピーしました'; setTimeout(()=>{btn.textContent=orig;},1500); };
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(input.value).then(done).catch(()=>{ try{document.execCommand('copy');done();}catch(e){} });
  }else{
    try{ document.execCommand('copy'); done(); }catch(e){}
  }
}

// 購入候補タブの「他の端末と同期中」表示の出し分け（設計文書 §7.3）。
// 2026-08-18 実機バグの修正: sync_idの有無ではなく、state.linked（サーバーが
// 使用済みトークンの有無から判定した「他端末と連携済みか」）でのみ判定する。
// sync_idは購入候補・デッキを1件でも作れば自動発行される（§8）ため、
// 存在するだけでは「他端末と繋がっている」ことを意味しない
function _refreshSyncStatusUI(){
  const el=document.getElementById('wishSyncStatus');
  if(!el || !window.SyncClient) return;
  const state=window.SyncClient.getSyncState();
  el.classList.toggle('hidden', !(state && state.linked===true));
}

let _syncUnlinkInFlight=false; // 連打で複数リクエストを重ねないためのガード（司令塔指摘#1・#2）

function unlinkThisDeviceSync(){
  if(!window.SyncClient || _syncUnlinkInFlight) return;
  if(!confirm('この端末をほかの端末との同期から切り離しますか？（この端末のデータは消えません）')) return;
  _syncUnlinkInFlight=true;
  window.SyncClient.unlinkThisDevice().then(function(res){
    if(res && res.ok){
      _refreshSyncStatusUI();
    }else{
      // 司令塔指摘#2: 理由が分かる表示を必ず出す（429なら「混み合っています」等）
      alert('同期の解除に失敗しました: ' + _syncShareErrorText(res && res.reason));
    }
  }).catch(function(){
    alert('同期の解除に失敗しました: ' + _syncShareErrorText('network_error'));
  }).finally(function(){
    _syncUnlinkInFlight=false;
  });
}
