// コメント（吹き出し・盤面メモ・カットイン）の純粋ロジック（services/comment-model.js）の確認。
// 実行: node tests/js/comment_model_check.mjs
import assert from 'node:assert/strict';
import {
  COMMENT_MAX_LEN, clampCommentText, commentKind, computeCommentView, listComments,
  cursorAfterRemove, shiftRangeAfterRemove, shiftRangeAfterInsert, isSameComment,
  CUTIN_MAX_LEN, CUTIN_DURATION_MS, CUTIN_PRESETS, clampCutinText, commentText, cutinToShow, playPauseMs,
} from '../../static/solitaire/js/services/comment-model.js';

const mv = (cardId, extra = {}) => ({ actionType: 'moveCard', cardId, zoneId: 'custom-slot-1', ...extra });
const bub = (cardId, text) => ({ actionType: 'comment', kind: 'card', cardId, text });
const memo = (text) => ({ actionType: 'comment', kind: 'board', text });
const view = (logs, cursor = logs.length - 1, setup = []) => computeCommentView(setup, logs, cursor);

// ── 文字数 ──
assert.equal(COMMENT_MAX_LEN, 60);
assert.equal(clampCommentText('  あ  '), 'あ');
assert.equal(clampCommentText('あ'.repeat(61)).length, 60);
assert.equal(Array.from(clampCommentText('😀'.repeat(70))).length, 60, '絵文字を途中で割らない');
assert.equal(clampCommentText(undefined), '');
assert.equal(clampCommentText(123), '');

// ── 種類: kind 無しの古いコメントは盤面メモ ──
assert.equal(commentKind({ actionType: 'comment', text: 'x' }), 'board');
assert.equal(commentKind(bub('a', 'x')), 'card');
assert.equal(commentKind({ actionType: 'comment', kind: 'card', text: 'x' }), 'board', 'cardId 無しの吹き出しは盤面メモ扱い');
let v = view([{ actionType: 'comment', text: '古いコメント' }]);
assert.equal(v.memo, '古いコメント');
assert.deepEqual(v.bubbles, []);

// ── 吹き出し: 動くまで表示 ──
v = view([mv('a'), bub('a', 'G受け')]);
assert.deepEqual(v.bubbles, [{ cardId: 'a', text: 'G受け' }]);
// 付いた後に同じカードが動いたら消える
v = view([mv('a'), bub('a', 'G受け'), mv('a', { zoneId: 'side-slot-0' })]);
assert.deepEqual(v.bubbles, []);
// 他のカードが動いても消えない
v = view([mv('a'), bub('a', 'G受け'), mv('b')]);
assert.equal(v.bubbles.length, 1);
// 巻き戻し（カーソルが動く手より前）では戻る
const logs = [mv('a'), bub('a', 'G受け'), mv('a', { zoneId: 'side-slot-0' })];
assert.deepEqual(view(logs, 2).bubbles, []);
assert.deepEqual(view(logs, 1).bubbles, [{ cardId: 'a', text: 'G受け' }]);
assert.deepEqual(view(logs, 0).bubbles, [], 'コメントより前では出ない');
assert.deepEqual(view(logs, -1).bubbles, []);

// inPlace（同じスロット内の表示形式の変更）では消えない
v = view([mv('a'), bub('a', 'x'), mv('a', { inPlace: true, orientation: 'defense' })]);
assert.equal(v.bubbles.length, 1, 'inPlace で消えた');
// removeCard・draw・returnToDeck・resetDeck も「動いた」（カードがその場に居なくなる）
v = view([bub('t', 'x'), { actionType: 'removeCard', cardId: 't', zoneId: 'custom-slot-1' }]);
assert.deepEqual(v.bubbles, []);
v = view([bub('a', 'x'), { actionType: 'returnToDeck', cardId: 'a' }]);
assert.deepEqual(v.bubbles, []);
v = view([bub('a', 'x'), { actionType: 'draw', cardId: 'a' }]);
assert.deepEqual(v.bubbles, []);
v = view([bub('a', 'x'), bub('b', 'y'), { actionType: 'resetDeck' }]);
assert.deepEqual(v.bubbles, []);
// 効果発動・カウンターでは消えない
v = view([bub('a', 'x'), { actionType: 'activateEffect', cardId: 'a' }, { actionType: 'counterChange', cardId: 'a', counter: 1, op: 'add' }]);
assert.equal(v.bubbles.length, 1);
// 同じカードの吹き出しは最後の1件
v = view([bub('a', '一つ目'), bub('a', '二つ目')]);
assert.deepEqual(v.bubbles, [{ cardId: 'a', text: '二つ目' }]);
// 動いた後に付け直した吹き出しは出る
v = view([bub('a', '一つ目'), mv('a'), bub('a', '付け直し')]);
assert.deepEqual(v.bubbles, [{ cardId: 'a', text: '付け直し' }]);

// ── 盤面メモ: 次のメモが来るまで表示・次で置き換わる ──
v = view([memo('一つ目'), mv('a'), mv('b')]);
assert.equal(v.memo, '一つ目', 'カードが動いてもメモは残る');
v = view([memo('一つ目'), mv('a'), memo('二つ目')]);
assert.equal(v.memo, '二つ目');
assert.equal(view([memo('一つ目'), mv('a'), memo('二つ目')], 1).memo, '一つ目', 'カーソルまでで決まる');
assert.equal(view([mv('a'), memo('一つ目')], 0).memo, null);
// 本文は上限で切って表示
assert.equal(Array.from(view([memo('あ'.repeat(100))]).memo).length, 60);

// ── setup（範囲共有で開始前に回された記録）も同じ計算に入る ──
const setup = [{ ...bub('a', '前から'), setup: true }, { ...memo('前のメモ'), setup: true }, { ...mv('b'), setup: true }];
v = computeCommentView(setup, [], -1);
assert.deepEqual(v.bubbles, [{ cardId: 'a', text: '前から' }], 'setup の吹き出しが受け手にも見える');
assert.equal(v.memo, '前のメモ');
// setup の後に動いたカードの吹き出しは出ない
v = computeCommentView(setup, [mv('a')], 0);
assert.deepEqual(v.bubbles, []);
// setup 内で動いたあとのものは出ない
v = computeCommentView([{ ...bub('a', 'x'), setup: true }, { ...mv('a'), setup: true }], [], -1);
assert.deepEqual(v.bubbles, []);
// 通常ログのメモが setup のメモを置き換える
v = computeCommentView(setup, [memo('新しい')], 0);
assert.equal(v.memo, '新しい');

// ── 一覧 ──
assert.deepEqual(listComments([mv('a'), bub('a', 'x'), memo('y'), { actionType: 'comment', text: 'z' }]), [
  { index: 1, kind: 'card', cardId: 'a', text: 'x' },
  { index: 2, kind: 'board', cardId: null, text: 'y' },
  { index: 3, kind: 'board', cardId: null, text: 'z' },
]);

// ── 削除で位置がずれる ──
// カーソル
assert.equal(cursorAfterRemove(5, 2), 4, 'カーソルより前を消したら1つ前へ');
assert.equal(cursorAfterRemove(5, 5), 4, 'カーソルの手そのものを消したら1つ前へ');
assert.equal(cursorAfterRemove(5, 6), 5, 'カーソルより後ろは動かない');
assert.equal(cursorAfterRemove(-1, 0), -1, '0手目（-1）より後ろ（0）を消してもそのまま');
// 範囲（startIdx=範囲より前の手数 / endIdx=範囲の終わりまでの手数）
const range = { recording: false, startIdx: 3, endIdx: 7 };
assert.deepEqual(shiftRangeAfterRemove(range, 1), { recording: false, startIdx: 2, endIdx: 6 }, '範囲より前を消す: 開始も終わりも1つ前');
assert.deepEqual(shiftRangeAfterRemove(range, 2), { recording: false, startIdx: 2, endIdx: 6 });
assert.deepEqual(shiftRangeAfterRemove(range, 3), { recording: false, startIdx: 3, endIdx: 6 }, '範囲の最初の手を消す: 終わりだけ1つ前');
assert.deepEqual(shiftRangeAfterRemove(range, 5), { recording: false, startIdx: 3, endIdx: 6 }, '範囲の中を消す: 終わりだけ1つ前');
assert.deepEqual(shiftRangeAfterRemove(range, 6), { recording: false, startIdx: 3, endIdx: 6 }, '範囲の最後の手を消す');
assert.deepEqual(shiftRangeAfterRemove(range, 7), range, '範囲より後ろは動かない');
assert.deepEqual(shiftRangeAfterRemove({ recording: true, startIdx: 3, endIdx: null }, 1), { recording: true, startIdx: 2, endIdx: null }, '範囲を選んでいる最中は開始だけ');
assert.deepEqual(shiftRangeAfterRemove({ recording: true, startIdx: 3, endIdx: null }, 4), { recording: true, startIdx: 3, endIdx: null });
assert.deepEqual(shiftRangeAfterRemove({ recording: false, startIdx: null, endIdx: null }, 0), { recording: false, startIdx: null, endIdx: null }, '範囲が無ければそのまま');
assert.notEqual(shiftRangeAfterRemove(range, 1), range, '元のオブジェクトは変えない');
assert.equal(range.startIdx, 3);

// 削除後の記録で表示が正しく再計算される（コメントを消すと、その吹き出しが消える）
let ls = [mv('a'), bub('a', 'x'), memo('m')];
assert.equal(view(ls).bubbles.length, 1);
ls = ls.filter((_, i) => i !== 1);
assert.equal(view(ls, cursorAfterRemove(2, 1)).bubbles.length, 0);
assert.equal(view(ls, cursorAfterRemove(2, 1)).memo, 'm');

// ── 差し込みで位置がずれる（削除の逆） ──
const r2 = { recording: false, startIdx: 3, endIdx: 7 };
assert.deepEqual(shiftRangeAfterInsert(r2, 1), { recording: false, startIdx: 4, endIdx: 8 }, '範囲より前に差し込む: 開始も終わりも1つ後ろ');
assert.deepEqual(shiftRangeAfterInsert(r2, 2), { recording: false, startIdx: 4, endIdx: 8 });
assert.deepEqual(shiftRangeAfterInsert(r2, 3), { recording: false, startIdx: 3, endIdx: 8 }, '範囲の先頭に差し込む: 範囲に入る（終わりだけ後ろ）');
assert.deepEqual(shiftRangeAfterInsert(r2, 5), { recording: false, startIdx: 3, endIdx: 8 }, '範囲の中: 終わりだけ1つ後ろ');
assert.deepEqual(shiftRangeAfterInsert(r2, 7), { recording: false, startIdx: 3, endIdx: 8 }, '範囲の終わり直後（末尾で付けた）: 範囲に入る');
assert.deepEqual(shiftRangeAfterInsert(r2, 8), r2, '範囲より後ろ: そのまま');
assert.deepEqual(shiftRangeAfterInsert({ recording: true, startIdx: 3, endIdx: null }, 1), { recording: true, startIdx: 4, endIdx: null }, '選んでいる最中: 開始より前なら開始が後ろへ');
assert.deepEqual(shiftRangeAfterInsert({ recording: true, startIdx: 3, endIdx: null }, 3), { recording: true, startIdx: 3, endIdx: null }, '開始と同じ位置: 開始はそのまま');
assert.deepEqual(shiftRangeAfterInsert({ recording: true, startIdx: 3, endIdx: null }, 5), { recording: true, startIdx: 3, endIdx: null });
assert.deepEqual(shiftRangeAfterInsert({ recording: false, startIdx: null, endIdx: null }, 0), { recording: false, startIdx: null, endIdx: null });
assert.notEqual(shiftRangeAfterInsert(r2, 1), r2);
assert.equal(r2.startIdx, 3);
// 差し込みと削除は逆の操作
for (const i of [0, 2, 3, 5, 7, 9]) {
  assert.deepEqual(shiftRangeAfterRemove(shiftRangeAfterInsert(r2, i), i), r2, `差し込み→削除で元に戻る(${i})`);
}

// ── 一覧の行と記録の手が同じか ──
assert.equal(isSameComment(bub('a', 'x'), { kind: 'card', cardId: 'a', text: 'x' }), true);
assert.equal(isSameComment(bub('a', 'x'), { kind: 'card', cardId: 'b', text: 'x' }), false);
assert.equal(isSameComment(bub('a', 'x'), { kind: 'board', cardId: null, text: 'x' }), false);
assert.equal(isSameComment(memo('y'), { kind: 'board', cardId: null, text: 'z' }), false);
assert.equal(isSameComment(mv('a'), { kind: 'board', cardId: null, text: '' }), false, 'コメント以外は別物');
assert.equal(isSameComment(undefined, { kind: 'board', cardId: null, text: 'x' }), false);
assert.equal(isSameComment({ actionType: 'comment', text: 'z' }, { kind: 'board', cardId: null, text: 'z' }), true, '古いコメント（kind 無し）は盤面メモ');

// ── カットイン ──
const cut = (text) => ({ actionType: 'comment', kind: 'cutin', text });
assert.equal(CUTIN_MAX_LEN, 20);
assert.equal(CUTIN_DURATION_MS, 1900);
assert.deepEqual(CUTIN_PRESETS, ['ターン終了', '相手ターン', '自分ターン', 'バトルフェイズ', 'メインフェイズ2', 'エンドフェイズ']);
assert.equal(commentKind(cut('ターン終了')), 'cutin');
assert.equal(commentKind({ actionType: 'comment', kind: 'unknown', text: 'x' }), 'board', '未知の kind は今までどおり盤面メモ');
assert.equal(commentKind({ actionType: 'comment', kind: 'cutin', cardId: 'a', text: 'x' }), 'cutin');
// 20字で切る（絵文字を割らない・前後の空白は除く）
assert.equal(clampCutinText('あ'.repeat(21)).length, 20);
assert.equal(Array.from(clampCutinText('😀'.repeat(30))).length, 20, '絵文字を途中で割らない');
assert.equal(clampCutinText('  ターン終了  '), 'ターン終了');
assert.equal(clampCutinText(undefined), '');
assert.equal(commentText(cut('あ'.repeat(50))).length, 20, '記録に長い本文があっても表示は20字');
assert.equal(commentText(memo('あ'.repeat(50))).length, 50, '盤面メモは60字まで（20字に切らない）');
// 盤面に残らない: 吹き出し・盤面メモの表示に影響しない（盤面メモを置き換えない）
v = view([mv('a'), memo('メモ'), bub('a', 'G受け'), cut('ターン終了')]);
assert.equal(v.memo, 'メモ', 'カットインが盤面メモを置き換えない');
assert.deepEqual(v.bubbles, [{ cardId: 'a', text: 'G受け' }], 'カットインは吹き出しを消さない');
// 一覧: kind='cutin'・本文は20字
assert.deepEqual(listComments([mv('a'), cut('あ'.repeat(30))]), [{ index: 1, kind: 'cutin', cardId: null, text: 'あ'.repeat(20) }]);
// 一覧の行との照合（削除の取り違え防止）
assert.equal(isSameComment(cut('ターン終了'), { kind: 'cutin', cardId: null, text: 'ターン終了' }), true);
assert.equal(isSameComment(cut('ターン終了'), { kind: 'board', cardId: null, text: 'ターン終了' }), false, '種類が違えば別物');
assert.equal(isSameComment(cut('相手ターン'), { kind: 'cutin', cardId: null, text: 'ターン終了' }), false);
assert.equal(isSameComment(cut('あ'.repeat(30)), { kind: 'cutin', cardId: null, text: 'あ'.repeat(20) }), true, '長い本文は切った形で照合');

// 「進んだときだけ出す」判定: 前進でその手に来たときだけ
assert.equal(cutinToShow(cut('ターン終了'), true), 'ターン終了', '前進（付けた直後・次へ・自動再生）で来たら出す');
assert.equal(cutinToShow(cut('ターン終了'), false), null, '巻き戻し・スライダー・取消・復元・読み込み直後は出さない');
assert.equal(cutinToShow(cut('あ'.repeat(40)), true).length, 20, '出す本文も20字');
assert.equal(cutinToShow(cut('   '), true), null, '空のカットインは出さない');
assert.equal(cutinToShow(memo('メモ'), true), null, '盤面メモは出さない');
assert.equal(cutinToShow(bub('a', 'x'), true), null, '吹き出しは出さない');
assert.equal(cutinToShow(mv('a'), true), null, 'コメント以外の手は出さない');
assert.equal(cutinToShow(undefined, true), null, 'カーソルが先頭（手が無い）でも落ちない');
assert.equal(cutinToShow({ actionType: 'comment', kind: 'unknown', text: 'x' }, true), null, '未知の kind は盤面メモ扱いなので出さない');

// 自動再生の待ち時間: カットインは動きが終わるまで、吹き出し・メモは倍率、ふつうの手は基本
assert.equal(playPauseMs(mv('a'), 900, 2.5), 900);
assert.equal(playPauseMs(memo('m'), 900, 2.5), 2250);
assert.equal(playPauseMs(bub('a', 'x'), 900, 2.5), 2250);
assert.equal(playPauseMs(cut('ターン終了'), 900, 2.5), 1900, 'カットイン: 動きの時間（約1.9秒）');
assert.equal(playPauseMs(cut('ターン終了'), 450, 2.5), 1900, '2倍速でも動きの時間は縮めない');
assert.equal(playPauseMs(cut('ターン終了'), 1800, 2.5), 1900, '0.5倍速（1.8秒）でも動きが終わるまで');
assert.equal(playPauseMs(cut('ターン終了'), 3000, 2.5), 3000, '基本の待ちが長ければそちら');

console.log('comment-model: 吹き出し（動いたら消える・inPlace・setup）、盤面メモ、カットイン（20字・進んだときだけ・待ち時間）、削除でのずれを確認');
