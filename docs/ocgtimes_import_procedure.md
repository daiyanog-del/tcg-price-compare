# 遊戯王OCGタイムズ 配信後の取込手順（Claude の定期タスク用）

2026-10-17 作成。ユーザーの依頼で、OCGタイムズの配信後に「本日の新規カードまとめ」の一覧画面を切り出し、
TCGYM の未発売カードとして承認待ちに入れる。自動承認はしない（取込後にユーザーが管理画面で照合して承認する）。

- 作業場所: `C:\Users\daiya\CardPriceChecker\tcg-web`（自宅PCの巡回用の作業ツリー。git 操作・ファイル編集はしない。スクリプトを実行するだけ）
- 道具: `python -I tools/ocgtimes_watch.py`（status / import）、内蔵ブラウザ（`mcp__Claude_Browser__*`）
- ユーザーの許可（2026-10-17、会話で確認済み）: 内蔵ブラウザで YouTube の公式配信を再生し、canvas で描いた一覧画面の画像をダウンロードフォルダに保存すること、それを本番の承認待ちに取込むこと。これ以外の操作（承認・公開・投稿・削除）はしない。
- 動画のダウンロード（yt-dlp 等）はしない。

## 1. 配信が終わったか確かめる

```
python -I tools/ocgtimes_watch.py status <動画ID>
```

- `ended` が false（配信前・配信中）→ この回は取込まない。`mcp__scheduled-tasks__create_scheduled_task` で
  30分後（`fireAt`）に同じ内容の1回限りのタスクを予約し直して終わる。taskId は `ocgtimes-import-<動画ID>-<HHMM>`、
  prompt は今回と同じ文面。予約し直しは配信予定の開始から6時間までにする（それを過ぎたら、取込めなかったと報告して終わる）。
- `summary_seconds` が null（概要欄の目次に「新規カード…まとめ/一覧」が無い）→ 動画ページの概要欄を読んで該当の時刻を探す。
  見つからなければ、動画の終盤（最後の10分）を 3. の方法で探す。それでも一覧画面が無ければ、報告して終わる。

## 2. 開始時刻を控える

取込は「この時刻より後にダウンロードフォルダに増えた画像」を集める。ダウンロードを始める前に、今の時刻を
タイムゾーンつき ISO 形式で控える（例: PowerShell `Get-Date -Format o`）。

## 3. 一覧画面を探す（内蔵ブラウザ）

1. `preview_start` に `{"url": "https://www.youtube.com/watch?v=<動画ID>&t=<summary_seconds>s"}` を渡して開く。
2. 次の JavaScript で画質を 1080p にして音を消し、少し再生してから止める:

```js
const v=document.querySelector('video'); const p=document.getElementById('movie_player');
try{p.setPlaybackQualityRange('hd1080','hd1080')}catch(e){}
v.muted=true; await v.play().catch(()=>{}); await new Promise(r=>setTimeout(r,4000)); v.pause();
JSON.stringify({w:v.videoWidth,h:v.videoHeight,ad:!!document.querySelector('.ad-showing')})
```

   `w` が 1920 でなければ、数秒待ってもう一度。広告（`ad: true`）なら終わるまで待つ。

3. 場面の区切りを調べる（一覧①〜④が約60秒の周期で繰り返し表示される。10-16 の例: ①6448s ②6458s ③6468s ④6480s）:

```js
const START=<summary_seconds>, END=START+120;
const v=document.querySelector('video'); const c=document.createElement('canvas'); c.width=48;c.height=27;
const g=c.getContext('2d'); const sig=async t=>{v.currentTime=t; await new Promise(r=>v.addEventListener('seeked',r,{once:true}));
  await new Promise(r=>setTimeout(r,400)); g.drawImage(v,0,0,48,27); return Array.from(g.getImageData(0,0,48,27).data);};
const diff=(a,b)=>{let s=0;for(let i=0;i<a.length;i+=4)s+=Math.abs(a[i]-b[i])+Math.abs(a[i+1]-b[i+1])+Math.abs(a[i+2]-b[i+2]);return s/(a.length/4)/3;};
const segs=[]; let prev=null;
for(let t=START;t<=END;t+=2){const s=await sig(t); if(!prev||diff(s,prev)>6){segs.push({start:t,end:t});} else segs[segs.length-1].end=t; prev=s;}
JSON.stringify(segs.map(x=>({...x,mid:Math.round((x.start+x.end)/2)})))
```

4. 各区切りの中ほど（`mid`）を表示して、`computer` の `screenshot` で中身を見る。見出しが「新規カード一覧①」「②」…の画面だけを選ぶ。
   同じ番号は1回だけ（周期の2巡目は使わない。1枚だけの画面は前半・後半の2区切りに割れることがある）。
   10-16 の実測では区切りが約10秒ごと（6439 KONAMI → 6449 ① → 6459 ② → 6469 ③ → 6479・6489 ④ → 6499 から2巡目）。
   番号が飛んでいたら区切りの間を2秒刻みで見直す。
   表示には次を使う（`window.__show(t)`）:

```js
window.__show = async t=>{const v=document.querySelector('video'); v.currentTime=t;
  await new Promise(r=>v.addEventListener('seeked',r,{once:true})); await new Promise(r=>setTimeout(r,1200));
  const c=document.createElement('canvas'); c.width=v.videoWidth;c.height=v.videoHeight; c.getContext('2d').drawImage(v,0,0);
  let o=document.getElementById('__ov'); if(!o){o=document.createElement('img');o.id='__ov';
  o.style.cssText='position:fixed;inset:0;width:100vw;height:100vh;object-fit:contain;z-index:99999;background:#000';document.body.appendChild(o);}
  o.src=c.toDataURL('image/jpeg',0.9); await new Promise(r=>setTimeout(r,800)); return v.currentTime;};
await window.__show(<mid>)
```

## 4. 選んだ画面を保存する

番号の順に、選んだ時刻を並べて次を実行する（PNG・1920×1080 でダウンロードフォルダに落ちる。名前が .tmp のままでもよい）:

```js
const TIMES=[<時刻1>,<時刻2>,...]; const v=document.querySelector('video'); const out=[];
for(const t of TIMES){v.currentTime=t; await new Promise(r=>v.addEventListener('seeked',r,{once:true}));
  await new Promise(r=>setTimeout(r,1200)); const c=document.createElement('canvas'); c.width=v.videoWidth;c.height=v.videoHeight;
  c.getContext('2d').drawImage(v,0,0); const b=await new Promise(r=>c.toBlob(r,'image/png'));
  const a=document.createElement('a'); a.href=URL.createObjectURL(b); a.download=`ocgtimes_t${t}.png`;
  document.body.appendChild(a); a.click(); a.remove(); out.push(`${t}:${c.width}x${c.height}:${b.size}`);
  await new Promise(r=>setTimeout(r,1500));}
out.join(' ')
```

幅が 1920 でない画面があれば、その画面だけ待ってから撮り直す（撮り直す前に 2. の時刻を控え直す）。

## 5. 取込む

```
python -I tools/ocgtimes_watch.py import <動画ID> --since <2. の時刻> --times <時刻1>,<時刻2>,... --note "<番組名>。<商品名と発売日（分かれば）>"
```

- 画像の数と `--times` の数が合わないと止まる。ダウンロードフォルダを確かめ、2. からやり直す。
- 同名のカードが登録済みなら追加しない（公式版が先に入っていればそれが正）。
- 結果は Discord にも送られる。`contact_sheet` に一覧画像のパスが出る。

## 6. 報告

最後のメッセージに次を書く（ユーザーはタスクの完了通知でこれを読む）:
- 追加した枚数とカード名（id つき）、追加しなかったカードと理由、画像の保存に失敗したカード
- `contact_sheet` のパス
- 承認前にユーザーが確かめること: 名前（ふりがな）・種別・効果文を画像と照合（動画の取込は効果文の読みが不安定。10-17 の試験では1回目と読み直しの一致が16枚中2枚）
- 取込めなかった場合は、どこで止まったかと、手で頼むときの言い方（「OCGタイムズ取り込んで <動画URL>」）
