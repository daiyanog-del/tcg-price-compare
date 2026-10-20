// デッキ画像（static/shared/deck-image.js）の計算部分の検証。
// 描画の見た目・書き出し・ダイアログの動きは Playwright（実ブラウザ）で確認する。ここは並べ方・位置・URL の選び方・合計を見る。
'use strict';
const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const root=path.join(__dirname,'../..');
const load=async file=>import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync(path.join(root,file),'utf8')).toString('base64'));

(async()=>{
  const m=await load('static/shared/deck-image.js');
  const L=m.LAYOUT;

  // 同じカードは1枚ずつ並べる（ニューロンと同じ）。0枚・壊れた枚数は数えない
  assert.deepEqual(m.expandCards([{name:'A',qty:3},{name:'B',qty:1},{name:'C',qty:0},{name:'D',qty:'2'},{name:'E',qty:NaN}]),['A','A','A','B','D','D']);
  assert.deepEqual(m.expandCards(null),[]);

  // 画像の読み先: CORS を許可している配信元はそのまま（canvas 用の印 dki=1 を付けて画面の画像とキャッシュを分ける）、
  // それ以外（カーナベル）と読めないものは null（札で描く）
  const yg='https://artworks-jp-n.ygoresources.com/1/29/50_1.png';
  assert.equal(m.imageSource(yg,'灰流うらら'),yg+'?dki=1');
  assert.equal(m.imageSource({kind:'image',source:'official_sample',url:'https://nlfnnbrusbiizuodomyf.supabase.co/storage/v1/object/public/official-card-images/unreleased/629.jpg?t=1'},'X'),
    'https://nlfnnbrusbiizuodomyf.supabase.co/storage/v1/object/public/official-card-images/unreleased/629.jpg?t=1&dki=1');
  // Supabase は公開バケットのパスで見る（別プロジェクトでも直接読む）。公開でないパスは札
  assert.equal(m.imageSource('https://other.supabase.co/storage/v1/object/public/a/b.jpg','Y'),'https://other.supabase.co/storage/v1/object/public/a/b.jpg?dki=1');
  assert.equal(m.imageSource('https://other.supabase.co/rest/v1/x','Z'),null);
  assert.equal(m.imageSource('https://www.ka-nabell.com/img/card/x.jpg','増殖するG'),null,'CORS の無い配信元は札');
  assert.equal(m.imageSource({kind:'proxy',url:null,proxy:{}},'未発売'),null);
  assert.equal(m.imageSource(null,'不明'),null);
  assert.equal(m.imageSource('http://artworks-jp-n.ygoresources.com/a.png','平文'),null,'http は読まない');
  assert.equal(m.imageSource('javascript:alert(1)','x'),null);

  // 同時数の制限: 同時に動くのは limit 件まで・結果は元の順
  {
    let running=0,peak=0;
    const res=await m.mapLimited([5,1,4,2,3],2,async x=>{running++;peak=Math.max(peak,running);await new Promise(r=>setTimeout(r,x*3));running--;return x*10;});
    assert.deepEqual(res,[50,10,40,20,30]);
    assert.equal(peak,2);
    assert.deepEqual(await m.mapLimited([],3,async x=>x),[]);
  }

  // 位置: 幅は10枚ぶん。区分の見出しの下からカードが並び、区分の間に隙間が入る
  const lay=m.computeLayout([{title:'メインデッキ',count:40},{title:'エクストラデッキ',count:15}]);
  assert.equal(lay.width,L.pad*2+L.cols*L.cardW+(L.cols-1)*L.gap);
  const grid=r=>r*L.cardH+(r-1)*L.gap;
  assert.equal(lay.sections[0].bandY,L.headH);
  assert.equal(lay.sections[0].gridY,L.headH+L.secH);
  assert.equal(lay.sections[1].bandY,L.headH+L.secH+grid(4)+L.secGap);
  assert.equal(lay.footY,lay.sections[1].gridY+grid(2));
  assert.equal(lay.height,lay.footY+L.footH);
  // EX が無いデッキは区分の間の隙間を足さない
  const only=m.computeLayout([{title:'メインデッキ',count:41}]);
  assert.equal(only.footY,L.headH+L.secH+grid(5));
  // 11枚目は2段目の左端
  assert.deepEqual(m.cardPosition(10,500),{x:L.pad,y:500+L.cardH+L.gap});
  assert.deepEqual(m.cardPosition(9,500),{x:L.pad+9*(L.cardW+L.gap),y:500});

  // 種別順（モンスター→魔法→罠→不明）。同じ種別の中は元の順を保つ
  const sorted=m.sortByType([{name:'罠1'},{name:'魔法1'},{name:'M1'},{name:'?'},{name:'M2'}],{罠1:'trap',魔法1:'spell',M1:'monster',M2:'monster'});
  assert.deepEqual(sorted.map(c=>c.name),['M1','M2','魔法1','罠1','?']);

  // 合計: 単価×枚数。相場の無いカードは種類数で数える
  assert.deepEqual(m.summarizeEstimate([{qty:3,best:{price:100}},{qty:1,best:null},{qty:2,best:{price:50}},{qty:1}]),{total:400,missingKinds:2});
  assert.deepEqual(m.summarizeEstimate(undefined),{total:0,missingKinds:0});

  // ファイル名: OS で使えない文字を除く・空ならデッキ
  assert.equal(m.fileNameFor('光と闇の儀式'),'光と闇の儀式_デッキ画像.jpg');
  assert.equal(m.fileNameFor('a/b:c*?"<>|'),'abc_デッキ画像.jpg');
  assert.equal(m.fileNameFor('  '),'デッキ_デッキ画像.jpg');

  console.log('deck_image_check: ok');
})().catch(e=>{console.error(e);process.exit(1);});
