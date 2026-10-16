// replay-service の addComment / removeEventAt の単体チェック（カーソルのずれ・コメント以外を拒む・範囲のずれ・未来の手を切らない）。
// session_resume_check.js と同じく import 行をスタブに差し替えて data:URL で読み込む。comment-model.js は実物をそのまま使う。
'use strict';
const fs = require('fs'), path = require('path'), assert = require('assert/strict');
const root = path.join(__dirname, '../..');
const js = path.join(root, 'static/solitaire/js');
const b64 = (src) => 'data:text/javascript;base64,' + Buffer.from(src).toString('base64');

(async () => {
  const noop = '(...a)=>{}';
  const stubs = `
const returnAllCardsToDeck=${noop}; const attachCardImageListeners=${noop}; const sortPoolCards=${noop};
const applyCardState=${noop}; const getCardState=${noop}; const isTokenSrc=()=>false; const isTokenCard=()=>false; const markTokenIfSrc=${noop};
const createCardCounter=${noop}; const restackCustomSlot=${noop};
const playActivateEffect=${noop}; const flipMoveClone=${noop}; const playSetFlip=${noop}; const flyBetweenRects=${noop};
const prefersReducedMotion=()=>true; const createProxyCardElement=${noop}; const isMobilePortrait=()=>false;
const renderCommentView=${noop};
// カットイン: 出した本文を shown に、消した回数を hidden に数える
globalThis.__cut={shown:[],cards:[],hidden:0}; const showCutin=(t)=>{globalThis.__cut.shown.push(t.text);globalThis.__cut.cards.push(t.card);}; const hideCutin=()=>{globalThis.__cut.hidden++;};
`;
  const model = fs.readFileSync(path.join(js, 'services/comment-model.js'), 'utf8').replace(/^export /gm, '');
  let src = fs.readFileSync(path.join(js, 'services/replay-service.js'), 'utf8');
  src = src.replace(/^import [\s\S]*?from '[^']+';\r?$/gm, '');
  assert(!/^import /m.test(src));

  globalThis.document = { getElementById: () => null, querySelectorAll: () => [] };
  // 範囲のずれの確認用に、comment-model の関数も取り出せるようにする
  const svc = await import(b64(src + stubs + model + '\nexport { shiftRangeAfterInsert, shiftRangeAfterRemove };'));
  const { shiftRangeAfterInsert, shiftRangeAfterRemove } = svc;

  const mv = (cardId) => ({ actionType: 'moveCard', cardId, zoneId: 'custom-slot-1' });
  const fresh = () => { svc.initReplay(); for (const id of ['a', 'b', 'c']) svc.logEvent(mv(id)); }; // 3手 [a,b,c]、cursor=2

  // ── addComment: 末尾 ──
  fresh();
  const added = [];
  svc.subscribeReplay('commentAdded', (i) => added.push(i));
  assert.equal(svc.addComment({ kind: 'board', text: 'm1' }), true);
  assert.deepEqual(added, [3], '差し込んだ位置が通知される');
  assert.equal(svc.getLogLength(), 4);
  assert.equal(svc.getCursor(), 3, 'カーソルはコメントへ進む');
  assert.deepEqual(svc.getLogs().map((e) => e.seq), [0, 1, 2, 3], 'seq は 0 から連番');

  // ── addComment: 巻き戻した位置（カーソルの次に差し込み、後ろの手は切り捨てない） ──
  fresh();
  svc.seekTo(0); // [a(cursor), b, c]
  assert.equal(svc.addComment({ kind: 'card', cardId: 'a', text: 'bub' }), true);
  assert.deepEqual(svc.getLogs().map((e) => e.actionType + ':' + (e.cardId || '')), ['moveCard:a', 'comment:a', 'moveCard:b', 'moveCard:c']);
  assert.equal(svc.getCursor(), 1, 'カーソルは差し込んだコメント');
  assert.equal(svc.getLogLength(), 4, '未来の手が切り捨てられていない');
  assert.deepEqual(svc.getLogs().map((e) => e.seq), [0, 1, 2, 3]);
  // 0手目（-1）でも差し込める
  fresh(); svc.seekTo(-1);
  svc.addComment({ kind: 'board', text: 'first' });
  assert.equal(svc.getLogs()[0].text, 'first'); assert.equal(svc.getCursor(), 0); assert.equal(svc.getLogLength(), 4);
  // 空・上限・カード指定の欠け
  fresh();
  assert.equal(svc.addComment({ kind: 'board', text: '   ' }), false);
  assert.equal(svc.addComment({ kind: 'card', text: 'x' }), false, 'cardId 無しの吹き出しは付けない');
  svc.addComment({ kind: 'board', text: 'あ'.repeat(100) });
  assert.equal(Array.from(svc.getLogs()[3].text).length, 60);
  assert.equal(svc.getLogLength(), 4);

  // ── removeEventAt ──
  fresh(); // [a,b,c]
  svc.addComment({ kind: 'board', text: 'm1' }); // [a,b,c,m1] cursor3
  svc.addComment({ kind: 'card', cardId: 'b', text: 'bb' }); // [a,b,c,m1,bb] cursor4
  const removed = [];
  svc.subscribeReplay('removed', (i) => removed.push(i));
  // コメント以外は拒む（何も変わらない）
  assert.equal(svc.removeEventAt(1), false);
  assert.equal(svc.getLogLength(), 5); assert.equal(svc.getCursor(), 4); assert.deepEqual(removed, []);
  // 範囲外・不正
  assert.equal(svc.removeEventAt(9), false); assert.equal(svc.removeEventAt(-1), false); assert.equal(svc.removeEventAt(1.5), false);
  // 内容が違えば消さない（古い一覧の ✕・素早い2回押し）
  assert.equal(svc.removeEventAt(3, { kind: 'board', cardId: null, text: 'ちがう' }), false);
  assert.equal(svc.removeEventAt(3, { kind: 'card', cardId: 'b', text: 'm1' }), false);
  assert.equal(svc.getLogLength(), 5);
  // 一致すれば消える。カーソルより前なので1つ前へ
  assert.equal(svc.removeEventAt(3, { kind: 'board', cardId: null, text: 'm1' }), true);
  assert.equal(svc.getLogLength(), 4); assert.equal(svc.getCursor(), 3); assert.deepEqual(removed, [3]);
  assert.deepEqual(svc.getLogs().map((e) => e.seq), [0, 1, 2, 3]);
  // 同じ行をもう一度（素早い2回押し）: その位置のイベントは別物になっているので消えない
  assert.equal(svc.removeEventAt(3, { kind: 'board', cardId: null, text: 'm1' }), false);
  assert.equal(svc.getLogLength(), 4);
  // カーソルより後ろのコメントを消してもカーソルは動かない
  svc.seekTo(1);
  assert.equal(svc.removeEventAt(3, { kind: 'card', cardId: 'b', text: 'bb' }), true);
  assert.equal(svc.getCursor(), 1); assert.equal(svc.getLogLength(), 3);
  // カーソルの手そのもの（コメント）を消すと1つ前へ
  fresh(); svc.addComment({ kind: 'board', text: 'x' }); // cursor3 がコメント
  assert.equal(svc.removeEventAt(3), true); assert.equal(svc.getCursor(), 2);
  // コメント以外は expected 無しでも常に拒む
  fresh(); assert.equal(svc.removeEventAt(0), false);

  // ── 通知を受けた側が範囲をずらす流れ ──
  fresh(); // [a,b,c]
  let range = { recording: false, startIdx: 1, endIdx: 3 }; // 範囲 = b,c
  svc.subscribeReplay('commentAdded', (i) => { range = shiftRangeAfterInsert(range, i); });
  svc.subscribeReplay('removed', (i) => { range = shiftRangeAfterRemove(range, i); });
  svc.seekTo(0);
  svc.addComment({ kind: 'board', text: 'before' }); // 位置1 = startIdx と同じ → 範囲の先頭に入る
  assert.deepEqual(range, { recording: false, startIdx: 1, endIdx: 4 });
  svc.removeEventAt(1, { kind: 'board', cardId: null, text: 'before' });
  assert.deepEqual(range, { recording: false, startIdx: 1, endIdx: 3 }, '差し込み→削除で元の範囲');
  svc.seekTo(-1);
  svc.addComment({ kind: 'board', text: 'very first' }); // 位置0 < startIdx
  assert.deepEqual(range, { recording: false, startIdx: 2, endIdx: 4 });

  // ── カットイン: 前進でその手に来たときだけ出る ──
  const cut = globalThis.__cut;
  const reset = () => { cut.shown.length = 0; cut.cards.length = 0; cut.hidden = 0; };
  fresh(); reset();
  assert.equal(svc.addComment({ kind: 'cutin', text: 'ターン終了' }), true);
  assert.deepEqual(cut.shown, ['ターン終了'], '付けた直後に出る');
  assert.equal(svc.getLogs()[3].kind, 'cutin');
  assert.equal(svc.addComment({ kind: 'cutin', text: 'あ'.repeat(40) }), true);
  assert.equal(Array.from(svc.getLogs()[4].text).length, 20, '記録も20字で切る');
  assert.equal(cut.shown[1].length, 20);
  assert.equal(svc.addComment({ kind: 'cutin', text: '  ' }), false, '空は付けない');
  // 前へ・スライダー・取消では出ない（先頭へ戻ってから、手の上を通って戻る）
  reset();
  svc.stepBack(); svc.stepBack(); // cursor 4 → 2（コメントの手の手前）
  svc.seekTo(3);                  // スライダーでカットインの手へ飛ぶ
  assert.deepEqual(cut.shown, [], '前へ・スライダーでは出ない');
  svc.seekTo(0); svc.seekTo(4);   // 前進でも seekTo（スライダー）は出さない
  assert.deepEqual(cut.shown, [], 'スライダーで前に飛んでも出ない');
  svc.undoLast();                 // 取消
  assert.deepEqual(cut.shown, [], '取消では出ない');
  // 次へで1手進んだときは出る
  fresh(); svc.addComment({ kind: 'cutin', text: 'A' }); svc.seekTo(2); reset(); // [a,b,c,cut]、cursor は c
  svc.stepForward(); // cursor 2 → 3（カットインの手）
  assert.deepEqual(cut.shown, ['A'], '次へで進んだら出る');
  svc.stepForward(); // 次の手は普通の手（無ければ何もしない）
  assert.deepEqual(cut.shown, ['A']);
  // 前進以外でカーソルが動くと、出ているカットインを消す
  reset(); svc.stepBack();
  assert.equal(cut.hidden >= 1, true, '巻き戻しで出ているカットインを消す');
  // 盤面メモ・吹き出しの追加では出ない
  fresh(); reset();
  svc.addComment({ kind: 'board', text: 'm' }); svc.addComment({ kind: 'card', cardId: 'a', text: 'b' });
  assert.deepEqual(cut.shown, []);
  // セッション復元では出ない
  reset();
  svc.restoreReplaySession({ logs: [{ actionType: 'comment', kind: 'cutin', text: 'Z', seq: 0 }], setupLogs: [], cursor: 0, images: {}, names: {}, exCardIds: [], cardTypes: {} });
  assert.deepEqual(cut.shown, [], 'セッション復元では出ない');

  // ── 相手の妨害のカットイン（card つき）──
  fresh(); reset();
  assert.equal(svc.addComment({ kind: 'cutin', card: '灰流うらら' }), true);
  assert.deepEqual(svc.getLogs()[3], { actionType: 'comment', kind: 'cutin', card: '灰流うらら', text: '灰流うらら', seq: 3 },
    'card と、古い画面向けの text（同じ名前）を記録する');
  assert.deepEqual(cut.shown, ['灰流うらら'], '付けた直後に出る');
  assert.deepEqual(cut.cards, ['灰流うらら'], 'カードとして出す');
  const longName = 'ア'.repeat(30);
  assert.equal(svc.addComment({ kind: 'cutin', card: longName, text: '無視される' }), true);
  assert.equal(svc.getLogs()[4].card, longName, 'カード名は20字で切らない');
  assert.equal(Array.from(svc.getLogs()[4].text).length, 20, 'text は20字で切る（古い画面向け）');
  assert.equal(svc.addComment({ kind: 'cutin', card: '  ' }), false, '空のカード名は付けない');
  reset();
  svc.addComment({ kind: 'cutin', text: 'ターン終了' });
  assert.deepEqual(cut.cards, [null], '言葉のカットインは card なし');

  console.log('replay-service: addComment（カーソルの次へ差し込み・未来を切らない）、removeEventAt（コメントのみ・内容一致）、範囲のずれ、カットイン（進んだときだけ）を確認');
})().catch((e) => { console.error(e); process.exitCode = 1; });
