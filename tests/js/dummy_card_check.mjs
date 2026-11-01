// ダミーカード（static/solitaire/js/utils/dummy-card.js・2026-11-01）の確認。
// 字幕（replay-caption-model.js）・共有の盤面（replay-board.js）・画像の方針（replay-image-policy.js）での扱いも見る。
// 実行: node tests/js/dummy_card_check.mjs
import assert from 'node:assert/strict';
import { DUMMY_KINDS, dummyKindOfName, dummyKindOfSrc, dummySrcOf, dummyDisplayOf } from '../../static/solitaire/js/utils/dummy-card.js';
import { describeStep } from '../../static/solitaire/js/services/replay-caption-model.js';
import { buildBoardFromLogs } from '../../static/solitaire/js/services/replay-board.js';
import { classifyReplayImage } from '../../static/solitaire/js/utils/replay-image-policy.js';

// ── 予約名と src ──
assert.deepEqual(Object.keys(DUMMY_KINDS), ['monster', 'spell', 'trap']);
assert.equal(dummySrcOf('spell'), 'card://ダミー（魔法）');
assert.equal(dummyKindOfSrc('card://ダミー（罠）'), 'trap');
assert.equal(dummyKindOfName('ダミー（モンスター）'), 'monster');
assert.equal(dummyKindOfName('ダミー'), null, '予約名と完全に一致したときだけ');
assert.equal(dummyKindOfSrc('https://x/a.png'), null);
assert.equal(dummyKindOfSrc('data:image/jpeg;base64,xx'), null, 'トークンはダミーではない');
assert.equal(dummyKindOfSrc(undefined), null);
const disp = dummyDisplayOf('monster');
assert.equal(disp.kind, 'image');
assert.equal(disp.source, 'dummy');
assert.ok(disp.url.endsWith('images/dummy-monster.webp?v=1'));

// 共有リンク・保存の画像の方針はそのまま通る（card:// のまま）
assert.deepEqual(classifyReplayImage('card://ダミー（魔法）', 'ダミー（魔法）'), { kind: 'card', src: 'card://ダミー（魔法）' });

const dict = {
  images: { a: 'https://x/a.png', d: 'card://ダミー（魔法）' },
  names: { a: '召喚獣エリュシオン', d: 'ダミー（魔法）' },
};
const mv = (cardId, zoneId, extra = {}) => ({ actionType: 'moveCard', cardId, zoneId, zIndex: '1', orientation: '', face: '', ...extra });

// ── 字幕: 出すまではどこにもいない（トークンと同じ）。出した手は「ダミー」の札 ──
let logs = [mv('d', 'center-slot'), mv('d', 'side-slot-0'), { actionType: 'removeCard', cardId: 'd', zoneId: 'side-slot-0' }];
assert.deepEqual(describeStep([], logs, 0, dict), { cardId: 'd', name: 'ダミー（魔法）', tag: 'ダミー', from: '', to: '手札' });
assert.deepEqual(describeStep([], logs, 1, dict), { cardId: 'd', name: 'ダミー（魔法）', tag: '', from: '手札', to: '墓地' });
assert.deepEqual(describeStep([], logs, 2, dict), { cardId: 'd', name: 'ダミー（魔法）', tag: '取り除く', from: '墓地', to: '' });
// 普通のカードは今までどおりデッキから
assert.equal(describeStep([], [mv('a', 'center-slot')], 0, dict).from, 'デッキ');

// ── 共有の盤面: 削除したダミーは盤面から消え、全戻しでも戻らない（replay-service と同じ） ──
let b = buildBoardFromLogs([mv('d', 'center-slot')], dict);
assert.deepEqual(b.hand, [{ name: 'ダミー（魔法）' }]);
b = buildBoardFromLogs([mv('d', 'center-slot'), { actionType: 'removeCard', cardId: 'd', zoneId: 'center-slot' }, { actionType: 'resetDeck' }], dict);
assert.equal(b.hand.length, 0);
assert.equal(b.deckCount, 1, '残るのは普通のカードだけ');

console.log('dummy_card_check: ok');
