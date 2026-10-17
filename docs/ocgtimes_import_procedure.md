# 遊戯王OCGタイムズ 配信後の取込手順（Claude の定期タスク用）

2026-10-17 作成、同日に紹介場面方式へ改訂。OCGタイムズの配信後に、各カードの**紹介場面**（左に大きいカード・
右下の黒地に効果文の全文が白い字幕で出る画面）と、終盤の**一覧画面**（新規カード一覧①〜④）を切り出し、
TCGYM の未発売カードとして承認待ちに入れる。自動承認はしない（取込後にユーザーが管理画面で照合して承認する）。

- 作業場所: `C:\Users\daiya\CardPriceChecker\tcg-web`（自宅PCの巡回用の作業ツリー。git 操作・ファイル編集はしない。スクリプトを実行するだけ）
- 道具: `python -I tools/ocgtimes_watch.py`（pending / status / import / done）、内蔵ブラウザ（`mcp__Claude_Browser__*`）
- 呼び出し元: 毎晩 23:30 の取込タスク `ocgtimes-import`（取込待ちが無ければすぐ終わる）。手で頼まれたときも同じ手順（その場合は 1. の `pending` を飛ばし、指定の動画で `status` を見て 2. から）。
- ユーザーの許可（2026-10-17、会話で確認済み）: 内蔵ブラウザで YouTube の公式配信を再生し、canvas で描いた画面の画像をダウンロードフォルダに保存すること、それを本番の承認待ちに取込むこと。これ以外の操作（承認・公開・投稿・削除）はしない。
- 動画のダウンロード（yt-dlp 等）はしない（YouTube の規約）。

なぜ紹介場面か（10-16 配信での比較。docs/decisions.md 2026-10-17）:
一覧画面（カード幅約300px）からの読み取りは効果文の1回目が空になりやすく、承認済み16枚中6枚に誤りが残った。
紹介場面の字幕から読むと 16/16 が正解と一致した（`tools/eval_ocgtimes_reveal.py`）。一覧画面は網羅確認と数値の突き合わせに使う。

## 1. 取込む動画を決める

```
python -I tools/ocgtimes_watch.py pending
```

- `pending` が空、または `ready` が true のものが無い → 「取込待ちなし（または配信がまだ終わっていない）」と1行報告して終わる。
  無人の回ではアプリが定期タスクの作成を拒否するので、予約し直しはしない（翌晩にまた見る）。
- `ready` が true の動画ごとに 2. 以降を行う。
  - `reveal_sections`: 紹介場面の章（概要欄の目次で「…カード…公開」の章。`start`〜`end` 秒）。
    10-16 の例: 772〜1155（新規「六花」カード公開）・4561〜5402（「ハーピィ」「アマゾネス」）・5922〜6200（「パワー・オブ・ザ・デュエリスト」）。
  - `summary_seconds`: 一覧画面のある「新規カードまとめ」の時刻。
- `reveal_sections` が空 → 動画ページの概要欄を読んで、カードを紹介している章を探す。見つからなければ一覧画面だけで取込む（5. の `--layout list`）。
- `summary_seconds` が null → 動画の終盤（最後の10分）で一覧画面を探す。無ければ一覧画面なしで取込む（網羅確認ができないことを報告に書く）。
- 「おさらい」の章（例「IMMORTAL PHOENIX」情報おさらい）はカードを並べた画面で字幕が無いので使わない。
- `added_at` から14日を過ぎても `ready` にならない動画は、報告して `done` で外す（配信の延期・中止の可能性）。
- 商品名（`--product` に渡す）は、目次の「おさらい」「商品CM」の章名や動画の題名から取る（例 IMMORTAL PHOENIX）。分からなければ空でよい（承認時に人が入れる）。

## 2. 開始時刻を控える

取込は「この時刻より後にダウンロードフォルダに増えた画像」を、保存した順に集める。ダウンロードを始める前に、今の時刻を
タイムゾーンつき ISO 形式で控える（例: PowerShell `Get-Date -Format o`）。

## 3. 動画を開いて準備する（内蔵ブラウザ）

1. `preview_start` に `{"url": "https://www.youtube.com/watch?v=<動画ID>"}` を渡して開く。
2. 次の JavaScript で画質を 1080p にして音を消し、少し再生してから止める:

```js
const v=document.querySelector('video'); const p=document.getElementById('movie_player');
try{p.setPlaybackQualityRange('hd1080','hd1080')}catch(e){}
v.muted=true; await v.play().catch(()=>{}); await new Promise(r=>setTimeout(r,4000)); v.pause();
JSON.stringify({w:v.videoWidth,h:v.videoHeight,ad:!!document.querySelector('.ad-showing')})
```

   `w` が 1920 でなければ、数秒待ってもう一度。広告（`ad: true`）なら終わるまで待つ。

3. 次の JavaScript で、場面を調べる関数を用意する（実行するだけで、まだ何も調べない）:

```js
// t 秒ごとに、右下の字幕欄（横30〜98%・縦62〜88%）が「黒地に白文字」か、左のカード欄（横2〜26%・縦12〜76%）の見た目を調べる
window.__sample = async (START, END, STEP) => {
  const v=document.querySelector('video'); v.pause();
  const W=192,H=108; const c=document.createElement('canvas'); c.width=W; c.height=H;
  const g=c.getContext('2d',{willReadFrequently:true}); const out=[];
  for (let t=START; t<END; t+=STEP) {
    v.currentTime=t; await new Promise(r=>v.addEventListener('seeked',r,{once:true})); await new Promise(r=>setTimeout(r,300));
    g.drawImage(v,0,0,W,H); const d=g.getImageData(0,0,W,H).data;
    let dark=0,white=0,n=0;
    for (let y=Math.round(H*.62); y<Math.round(H*.88); y++) for (let x=Math.round(W*.30); x<Math.round(W*.98); x++) {
      const i=(y*W+x)*4, r=d[i], gg=d[i+1], b=d[i+2]; const mx=Math.max(r,gg,b), mn=Math.min(r,gg,b);
      if (mx<45) dark++; if (mn>170 && mx-mn<40) white++; n++; }
    const sig=[];
    for (let gy=0; gy<9; gy++) for (let gx=0; gx<6; gx++) { let s=0,k=0;
      for (let y=Math.round(H*(.12+.64*gy/9)); y<Math.round(H*(.12+.64*(gy+1)/9)); y++)
        for (let x=Math.round(W*(.02+.24*gx/6)); x<Math.round(W*(.02+.24*(gx+1)/6)); x++) { const i=(y*W+x)*4; s+=d[i]+d[i+1]+d[i+2]; k++; }
      sig.push(Math.round(s/k/3)); }
    out.push({t, dark:+(dark/n).toFixed(2), white:+(white/n).toFixed(3), sig});
  }
  return out;
};
// 字幕のある時刻を、左のカードの見た目が変わったところで区切る（4秒未満の区間は捨てる）
window.__scenes = async (START, END, STEP=2) => {
  const rows = await window.__sample(START, END, STEP);
  const isPanel = x => x.dark > 0.45 && x.white > 0.02 && x.white < 0.4;
  const diff = (a,b) => a.reduce((s,v,i)=>s+Math.abs(v-b[i]),0)/a.length;
  const segs=[]; let cur=null, prev=null;
  for (const x of rows) {
    if (!isPanel(x)) { cur=null; prev=null; continue; }
    if (!cur || diff(x.sig, prev.sig) > 6) { cur={start:x.t,end:x.t}; segs.push(cur); } else cur.end=x.t;
    prev=x;
  }
  return segs.filter(s=>s.end-s.start>=4).map(s=>({...s, mid:Math.round((s.start+s.end)/2)}));
};
'ok'
```

   判定の目安（10-16 配信の実測）: 紹介場面は dark 0.63〜0.70・white 0.11〜0.17、それ以外（スタジオ・デュエル・おさらい・CM）は dark 0.25 未満。

## 4. 紹介場面の時刻を決める

JavaScript ツールは1回45秒で打ち切られるので、ページの中で走らせて結果を後から読む。
**打ち切られた呼び出しもページ内で動き続け、次の走査と同時に動画を動かして結果を壊す。走らせるのは1回だけにする。**

```js
window.__res = {running:true};
(async()=>{ const out={};
  for (const [a,b] of [[<start1>,<end1>],[<start2>,<end2>]]) out[a]=await window.__scenes(a,b,2);   // reveal_sections の範囲
  window.__res={running:false, out}; })().catch(e=>window.__res={error:String(e)});
'started'
```

40秒ほど待っては `JSON.stringify(window.__res)` で読む（10-16 配信の3区間・約23分ぶんで数分）。
`running:false` になったら、各区間の `mid` が保存する時刻。

- 1区間＝1回の紹介（同じカードが何度も出るのは普通。取込のときに名前でまとめる）。10-16 配信では約24区間。
- 区間が見つからない章があれば、その章の中ほどを 3. の `__sample(start, end, 10)` で見て、`dark`・`white` の値から判定を見直す。
- 字幕が長くページ送りされるカードでは、`mid` の画面に全文が出ていないことがある（読み取りで「途中」と判定され、不確かに回る）。
  区間が40秒を超えるときは、`start`〜`end` の 1/3 と 2/3 の2枚にするとよい。
- 内蔵ブラウザで seek の直後に screenshot すると1つ前の画面が写ることがある。どのカードかは取込の読み取り結果（名前）で突き合わせるので、
  ここで目視で確かめる必要はない。

一覧画面の時刻も決める（一覧①〜④が約60秒の周期で繰り返し表示される。10-16 の例: ①6448 ②6458 ③6468 ④6480）:

```js
window.__res2={running:true};
(async()=>{ const START=<summary_seconds>, END=START+120;
  const v=document.querySelector('video'); const c=document.createElement('canvas'); c.width=48;c.height=27;
  const g=c.getContext('2d'); const sig=async t=>{v.currentTime=t; await new Promise(r=>v.addEventListener('seeked',r,{once:true}));
    await new Promise(r=>setTimeout(r,400)); g.drawImage(v,0,0,48,27); return Array.from(g.getImageData(0,0,48,27).data);};
  const diff=(a,b)=>{let s=0;for(let i=0;i<a.length;i+=4)s+=Math.abs(a[i]-b[i])+Math.abs(a[i+1]-b[i+1])+Math.abs(a[i+2]-b[i+2]);return s/(a.length/4)/3;};
  const segs=[]; let prev=null;
  for(let t=START;t<=END;t+=2){const s=await sig(t); if(!prev||diff(s,prev)>6){segs.push({start:t,end:t});} else segs[segs.length-1].end=t; prev=s;}
  window.__res2={running:false, segs:segs.map(x=>({...x,mid:Math.round((x.start+x.end)/2)}))}; })();
'started'
```

各区切りの中ほど（`mid`）を次の `__show(t)` で表示して `computer` の `screenshot` で見て、見出しが「新規カード一覧①」「②」…の画面だけを選ぶ
（同じ番号は1回だけ。1枚だけの画面は前半・後半の2区切りに割れることがある）:

```js
window.__show = async t=>{const v=document.querySelector('video'); v.currentTime=t;
  await new Promise(r=>v.addEventListener('seeked',r,{once:true})); await new Promise(r=>setTimeout(r,1200));
  const c=document.createElement('canvas'); c.width=v.videoWidth;c.height=v.videoHeight; c.getContext('2d').drawImage(v,0,0);
  let o=document.getElementById('__ov'); if(!o){o=document.createElement('img');o.id='__ov';
  o.style.cssText='position:fixed;inset:0;width:100vw;height:100vh;object-fit:contain;z-index:99999;background:#000';document.body.appendChild(o);}
  o.src=c.toDataURL('image/jpeg',0.9); await new Promise(r=>setTimeout(r,800)); return v.currentTime;};
await window.__show(<mid>)
```

## 5. 画面を保存する

紹介場面の時刻 → 一覧画面の時刻の順に並べ、次をページ内で走らせる（PNG・1920×1080 でダウンロードフォルダに落ちる。
名前が .tmp のままでもよい。取込は**保存した順**に `--times` と対応させる）:

```js
window.__saved={running:true};
(async()=>{ const TIMES=[<紹介場面の時刻...>, <一覧画面の時刻...>]; const v=document.querySelector('video'); const out=[];
  for(const t of TIMES){v.currentTime=t; await new Promise(r=>v.addEventListener('seeked',r,{once:true}));
    await new Promise(r=>setTimeout(r,1200)); const c=document.createElement('canvas'); c.width=v.videoWidth;c.height=v.videoHeight;
    c.getContext('2d').drawImage(v,0,0); const b=await new Promise(r=>c.toBlob(r,'image/png'));
    const a=document.createElement('a'); a.href=URL.createObjectURL(b); a.download=`ocgtimes_t${t}.png`;
    document.body.appendChild(a); a.click(); a.remove(); out.push(`${t}:${c.width}x${c.height}:${b.size}`);
    await new Promise(r=>setTimeout(r,1500));}
  window.__saved={running:false, out}; })();
'started'
```

`window.__saved` を読み、全部 1920x1080 で保存できたかを見る。幅が 1920 でない画面があれば、2. の時刻を控え直して全部を撮り直す。

## 6. 取込む

```
python -I tools/ocgtimes_watch.py import <動画ID> --since <2. の時刻> --times <紹介場面の時刻,...>,L<一覧の時刻>,... --product "<商品名>" --note "<番組名>。<商品名と発売日（分かれば）>"
```

- `--times` は保存した順。一覧画面の時刻には `L` を付ける（例 `--times 816,885,930,L6448,L6458,L6468,L6480`）。
- 画像の数と `--times` の数が合わないと止まる。ダウンロードフォルダを確かめ、2. からやり直す。
- 紹介場面の取込でしていること（`ocgtimes_reveal.py`）:
  - 効果文は字幕から、種別・数値・名前・ふりがなはカード画像と名前の札から読む。
  - 同じカードの場面を名前でまとめ、場面ごと（と一覧画面）で数値が食い違う欄は多数決にしたうえで不確かにする。
  - カード画像の効果欄を拡大して読み直し、字幕と突き合わせる。一致して他に不確かな欄が無いカードは
    `extraction_raw.reveal.auto_approve_candidate` を true にする（記録だけ。自動承認はしない）。食い違いは効果文を不確かにして、管理画面に差分を出す。
  - 一覧画面にあって紹介場面に無いカードは、その一覧画面を従来の読み方で読んで、そのカードだけを登録する（`list_fallback`）。
- 紹介場面が無い配信（目次に「カード公開」の章が無い）は、一覧画面だけを `--layout list --times 6448,6458,...`（L なし）で取込む（従来の方式）。
- 同名のカードが登録済みなら追加しない（公式版が先に入っていればそれが正）。
- 結果は Discord にも送られる。`contact_sheet` に保存画像を並べた画像のパスが出る。

## 7. 取込待ちから外す

取込が済んだら（一部のカードだけ失敗した場合も含む）:

```
python -I tools/ocgtimes_watch.py done <動画ID> --note "<追加N枚など>"
```

取込そのものが失敗した（`error` が出た）ときは外さない（翌晩に再試行される）。同じ失敗が2晩続いたら外して、手で頼むよう報告する。

## 8. 報告

最後のメッセージに次を書く（ユーザーはタスクの完了通知でこれを読む）:
- 追加した枚数とカード名（id つき）。カードごとに、字幕とカード画像の突き合わせ（`card_text_check`）と不確かな欄（`uncertain_fields`）
- 追加しなかったカードと理由、画像の保存に失敗したカード、一覧画面にだけあったカード（`list_only`）と補った結果（`list_fallback`）
- `contact_sheet` のパス
- 承認前にユーザーが確かめること: 不確かな欄（特に数値の食い違い・突き合わせの食い違い）を画像と照合。突き合わせが一致したカードも、名前（ふりがな）・収録商品・発売日は見る
- 取込めなかった場合は、どこで止まったかと、手で頼むときの言い方（「OCGタイムズ取り込んで <動画URL>」）
