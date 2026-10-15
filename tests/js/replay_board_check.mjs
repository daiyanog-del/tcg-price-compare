// replay-board.js（共有ログから最後の盤面を求める純粋関数）の確認。
// 実行: node tests/js/replay_board_check.mjs
// DOM との突き合わせ（実ブラウザで共有処理の POST 本文と DOM 実盤面を比較）は 2026-10-15 に手動で実施済み。
import assert from 'node:assert/strict';
import { buildBoardFromLogs } from '../../static/solitaire/js/services/replay-board.js';

const dict = {
  images: { a: 'https://x/a.png', b: 'card://B', c: 'https://x/c.png', e: 'https://x/e.png', t: 'data:image/png;base64,AAAA', u: 'card://名無し' },
  names: { a: 'A', b: 'B', c: 'C', e: 'E' },
  exCardIds: ['e'],
};
const mv = (cardId, zoneId, extra = {}) => ({ actionType: 'moveCard', cardId, zoneId, zIndex: '1', orientation: '', face: '', ...extra });

// 初期: 非EX3枚はデッキ、EX1枚はEXデッキ
let b = buildBoardFromLogs([{ actionType: 'comment', text: 'x' }], dict);
assert.equal(b.deckCount, 5); assert.equal(b.ex.count, 1); assert.equal(b.field.length, 0); assert.equal(b.moves, 1);

// 守備・セット・重ね（下重ねは最背面）・墓地・除外・手札
b = buildBoardFromLogs([
  { actionType: 'draw', cardId: 'a' },
  mv('a', 'custom-slot-5', { zIndex: '1' }),
  mv('b', 'custom-slot-5', { zIndex: '1' }),               // 空でないマスへ zIndex<=1 → 最背面（上は a のまま）
  mv('c', 'custom-slot-6', { orientation: 'defense', face: 'down' }),
  mv('e', 'side-slot-0'),
  { actionType: 'draw', cardId: 'b' },                    // b を手札へ（slot5 は a 1枚に）
], dict);
const f5 = b.field.find(f => f.slot === 'custom-slot-5');
assert.equal(f5.name, 'A'); assert.equal(f5.n, undefined);
const f6 = b.field.find(f => f.slot === 'custom-slot-6');
assert.deepEqual([f6.pos, f6.face], ['def', 'down']);
assert.equal(b.grave.count, 1); assert.deepEqual(b.grave.cards.map(c => c.name), ['E']);
assert.deepEqual(b.hand.map(h => h.name), ['B']);
assert.equal(b.ex.count, 0); assert.equal(b.deckCount, 2);

// 下重ね: a の下に b を置くと上は a・n=2
b = buildBoardFromLogs([mv('a', 'custom-slot-5'), mv('b', 'custom-slot-5', { zIndex: '1' })], dict);
assert.equal(b.field[0].name, 'A'); assert.equal(b.field[0].n, 2);
// 通常（zIndex>=2）は最前面
b = buildBoardFromLogs([mv('a', 'custom-slot-5'), mv('b', 'custom-slot-5', { zIndex: '2' })], dict);
assert.equal(b.field[0].name, 'B');

// resetDeck（旧形式 drawnIds を含む）・returnToDeck・未知のカード/ゾーンは無視
b = buildBoardFromLogs([
  mv('a', 'custom-slot-5'), mv('e', 'custom-slot-1'),
  { actionType: 'resetDeck', drawnIds: ['c'] },
  mv('zzz', 'custom-slot-2'), mv('a', 'free-space'), mv('a', 'custom-slot-99x'),
], dict);
assert.equal(b.field.length, 0); assert.deepEqual(b.hand.map(h => h.name), ['C']);
assert.equal(b.deckCount, 4); assert.equal(b.ex.count, 1);
b = buildBoardFromLogs([mv('a', 'center-slot'), { actionType: 'returnToDeck', cardId: 'a', isEx: false }], dict);
assert.equal(b.hand.length, 0); assert.equal(b.deckCount, 5);

// setup ログは手数に数えない・空ログや辞書なしは null
b = buildBoardFromLogs([{ ...mv('a', 'custom-slot-5'), setup: true }, mv('b', 'custom-slot-6')], dict);
assert.equal(b.moves, 1); assert.equal(b.field.length, 2);
assert.equal(buildBoardFromLogs([], dict), null);
assert.equal(buildBoardFromLogs([mv('a', 'center-slot')], { images: {} }), null);
// 画像のURLは board に含めない。トークン（data: 画像）は token:true・名前なしは「トークン」。card:// は名前だけ取り出す
b = buildBoardFromLogs([mv('a', 'custom-slot-5'), mv('t', 'custom-slot-6'), mv('u', 'center-slot')], dict);
assert.ok(!JSON.stringify(b).includes('http') && !JSON.stringify(b).includes('data:') && !JSON.stringify(b).includes('img'));
assert.deepEqual(b.field.find(f => f.slot === 'custom-slot-6').token, true);
assert.equal(b.field.find(f => f.slot === 'custom-slot-6').name, 'トークン');
assert.equal(b.field.find(f => f.slot === 'custom-slot-5').token, undefined);
assert.deepEqual(b.hand, [{ name: '名無し' }]);

// 存在しないマス（15以上・0）は無視
b = buildBoardFromLogs([mv('a', 'custom-slot-15'), mv('b', 'custom-slot-0')], dict);
assert.equal(b.field.length, 0); assert.equal(b.deckCount, 5);

// 裏向きのまま墓地に置かれたカードが一番上なら face:'down'
b = buildBoardFromLogs([mv('a', 'side-slot-0', { face: 'down' }), mv('c', 'side-slot-1')], dict);
assert.equal(b.grave.cards[0].face, 'down'); assert.equal(b.banish.cards[0].face, undefined);

// 墓地は末尾（一番上）から最大4枚・奥から上の順。count は全枚数
b = buildBoardFromLogs([mv('a', 'side-slot-0'), mv('b', 'side-slot-0'), mv('c', 'side-slot-0'), mv('e', 'side-slot-0'), mv('t', 'side-slot-0', { face: 'down' })], dict);
assert.equal(b.grave.count, 5);
assert.deepEqual(b.grave.cards.map(c => c.name), ['B', 'C', 'E', 'トークン']);
assert.equal(b.grave.cards[3].face, 'down'); assert.equal(b.grave.cards[3].token, true);
assert.deepEqual(b.banish, { count: 0, cards: [] });

// 同じマスの中で表示形式だけを変えた記録（inPlace）は重なりの順番を動かさない。通常の記録は一番上へ移る
b = buildBoardFromLogs([
  mv('a', 'custom-slot-5'), mv('b', 'custom-slot-5', { zIndex: '1' }), mv('c', 'custom-slot-5', { zIndex: '3' }),   // 下から b, a, c
  mv('a', 'custom-slot-5', { zIndex: '2', orientation: 'defense', inPlace: true }),                                // 真ん中の a だけ守備
], dict);
assert.equal(b.field[0].name, 'C'); assert.equal(b.field[0].n, 3);   // 一番上は c のまま
b = buildBoardFromLogs([
  mv('a', 'custom-slot-5'), mv('b', 'custom-slot-5', { zIndex: '1' }), mv('c', 'custom-slot-5', { zIndex: '3' }),
  mv('a', 'custom-slot-5', { zIndex: '2', orientation: 'defense' }),                                               // inPlace なし（旧記録）= 一番上へ
], dict);
assert.equal(b.field[0].name, 'A'); assert.equal(b.field[0].pos, 'def');

// デッキ列の途中へ戻した記録（beforeCardId）。count は変わらず、途中でも末尾でも枚数は同じ
b = buildBoardFromLogs([mv('a', 'custom-slot-5'), mv('a', 'poolRow', { beforeCardId: 'c' })], dict);
assert.equal(b.deckCount, 5); assert.equal(b.field.length, 0);

// removeCard（トークン削除）: 盤面から消え、resetDeck でもデッキへ戻らない。辞書には残る
b = buildBoardFromLogs([mv('t', 'custom-slot-6'), { actionType: 'removeCard', cardId: 't', zoneId: 'custom-slot-6' }], dict);
assert.equal(b.field.length, 0); assert.equal(b.deckCount, 4);   // 5 枚中、トークンを除いた 4 枚がデッキ
b = buildBoardFromLogs([mv('t', 'custom-slot-6'), { actionType: 'removeCard', cardId: 't', zoneId: 'custom-slot-6' },
  { actionType: 'resetDeck' }, mv('t', 'center-slot')], dict);
assert.equal(b.hand.length, 0); assert.equal(b.deckCount, 4);   // 消えたカードへの後続イベントは無視
// counterChange（カウンターの追加・削除）は配置に影響しない
b = buildBoardFromLogs([mv('a', 'custom-slot-5'), { actionType: 'counterChange', cardId: 'a', op: 'add', index: 0, counter: 2 }], dict);
assert.equal(b.field[0].name, 'A');

console.log('replay_board_check OK');
