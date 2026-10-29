// リプレイの字幕（services/replay-caption-model.js）の確認。
// 実行: node tests/js/replay_caption_model_check.mjs
import assert from 'node:assert/strict';
import { describeStep, zoneLabel } from '../../static/solitaire/js/services/replay-caption-model.js';

// ── 場所の名前 ──
assert.equal(zoneLabel('poolRow'), 'デッキ');
assert.equal(zoneLabel('poolRow2'), 'EXデッキ');
assert.equal(zoneLabel('center-slot'), '手札');
assert.equal(zoneLabel('side-slot-0'), '墓地');
assert.equal(zoneLabel('side-slot-1'), '除外');
assert.equal(zoneLabel('custom-slot-1'), 'EXモンスターゾーン');
assert.equal(zoneLabel('custom-slot-2'), 'EXモンスターゾーン');
assert.equal(zoneLabel('custom-slot-3'), 'フィールドゾーン');
assert.equal(zoneLabel('custom-slot-4'), 'モンスターゾーン');
assert.equal(zoneLabel('custom-slot-8'), 'モンスターゾーン');
assert.equal(zoneLabel('custom-slot-9'), '盤面', '9番は名前の無いマス');
assert.equal(zoneLabel('custom-slot-10'), '魔法・罠ゾーン');
assert.equal(zoneLabel('custom-slot-14'), '魔法・罠ゾーン');
// 相手の場（2026-10-29）
assert.equal(zoneLabel('custom-slot-15'), '相手のモンスターゾーン');
assert.equal(zoneLabel('custom-slot-19'), '相手のモンスターゾーン');
assert.equal(zoneLabel('custom-slot-20'), '相手のフィールドゾーン');
assert.equal(zoneLabel('custom-slot-21'), '相手の墓地');
assert.equal(zoneLabel('custom-slot-22'), '盤面', '22番以降のマスは無い');
assert.equal(zoneLabel('free-space'), '盤面');
assert.equal(zoneLabel(''), '');

const dict = {
  images: { a: 'https://x/a.png', b: 'https://x/b.png', e: 'https://x/e.png', t: 'data:image/jpeg;base64,xx' },
  names: { a: '召喚獣エリュシオン', b: '召喚魔術－「剣」', e: '真海竜騎－ダイダロス', t: '羊' },
  exCardIds: ['e'],
};
const mv = (cardId, zoneId, extra = {}) => ({ actionType: 'moveCard', cardId, zoneId, ...extra });
const at = (logs, i, setup = []) => describeStep(setup, logs, i, dict);

// ── 移動元は記録をたどって求める（記録には移動先しか無い） ──
let logs = [mv('a', 'center-slot'), mv('a', 'custom-slot-5'), mv('a', 'side-slot-0'), mv('a', 'side-slot-1')];
assert.deepEqual(at(logs, 0), { cardId: 'a', name: '召喚獣エリュシオン', tag: '', from: 'デッキ', to: '手札' });
assert.deepEqual(at(logs, 1), { cardId: 'a', name: '召喚獣エリュシオン', tag: '', from: '手札', to: 'モンスターゾーン' });
assert.equal(at(logs, 2).from, 'モンスターゾーン');
assert.equal(at(logs, 3).from, '墓地');
assert.equal(at(logs, 3).to, '除外');

// EX のカードは最初 EX デッキにいる
assert.deepEqual(at([mv('e', 'custom-slot-2')], 0), { cardId: 'e', name: '真海竜騎－ダイダロス', tag: '', from: 'EXデッキ', to: 'EXモンスターゾーン' });

// 初期配置（setup）を先にたどる（範囲共有）
assert.equal(at([mv('a', 'side-slot-0')], 0, [mv('a', 'custom-slot-6', { setup: true })]).from, 'モンスターゾーン');

// ── 範囲外・字幕にしない手 ──
assert.equal(at(logs, -1), null);
assert.equal(at(logs, 4), null);
assert.equal(at([{ actionType: 'comment', kind: 'cutin', text: 'x' }], 0), null, 'カットインは字幕にしない');
assert.equal(at([{ actionType: 'counterChange', counter: 3 }], 0), null, 'カード無しのカウンター');
assert.equal(at([{ actionType: 'resetDeck' }], 0), null);
assert.equal(describeStep([], null, 0, dict), null);

// ── 札 ──
logs = [mv('b', 'center-slot'), mv('b', 'custom-slot-12'), { actionType: 'activateEffect', cardId: 'b' }];
assert.deepEqual(at(logs, 2), { cardId: 'b', name: '召喚魔術－「剣」', tag: '効果発動', from: '', to: '魔法・罠ゾーン' });
assert.deepEqual(at([{ actionType: 'draw', cardId: 'a' }], 0), { cardId: 'a', name: '召喚獣エリュシオン', tag: 'ドロー', from: 'デッキ', to: '手札' });
assert.deepEqual(at([mv('a', 'center-slot'), { actionType: 'returnToDeck', cardId: 'a', isEx: false }], 1),
  { cardId: 'a', name: '召喚獣エリュシオン', tag: '', from: '手札', to: 'デッキ' });

// 同じマスで表示形式だけを変える
logs = [mv('a', 'custom-slot-5'), mv('a', 'custom-slot-5', { orientation: 'defense', inPlace: true }), mv('a', 'custom-slot-5', { inPlace: true })];
assert.deepEqual(at(logs, 1), { cardId: 'a', name: '召喚獣エリュシオン', tag: '守備表示', from: '', to: 'モンスターゾーン' });
assert.equal(at(logs, 2).tag, '攻撃表示');
logs = [mv('b', 'custom-slot-11', { face: 'down' }), mv('b', 'custom-slot-11', { inPlace: true })];
assert.equal(at(logs, 0).tag, 'セット', '別の場所へ裏向きで置く');
assert.equal(at(logs, 0).from, 'デッキ');
assert.equal(at(logs, 1).tag, '表にする');

// 重ねる（エクシーズ素材など）: 置く先のマスに、ほかのカードがいる
logs = [mv('a', 'custom-slot-5'), mv('e', 'custom-slot-5', { zIndex: 2 })];
assert.equal(at(logs, 1).tag, '重ねる');
assert.equal(at([mv('a', 'custom-slot-5')], 0).tag, '', '空いたマスは重ねるではない');

// 相手の場へ送る・相手の墓地に重ねる（相手の墓地は重ねる場所なので「重ねる」にしない）
logs = [mv('a', 'custom-slot-16'), mv('b', 'custom-slot-21'), mv('a', 'custom-slot-21')];
assert.deepEqual(at(logs, 0), { cardId: 'a', name: '召喚獣エリュシオン', tag: '', from: 'デッキ', to: '相手のモンスターゾーン' });
assert.equal(at(logs, 2).tag, '', '相手の墓地へ2枚目');
assert.equal(at(logs, 2).from, '相手のモンスターゾーン');
assert.equal(at(logs, 2).to, '相手の墓地');

// 全戻しの後はデッキ（EX は EX デッキ）から
logs = [mv('a', 'side-slot-0'), mv('e', 'side-slot-0'), { actionType: 'resetDeck', drawnIds: ['a'] }, mv('a', 'custom-slot-4'), mv('e', 'custom-slot-1')];
assert.equal(at(logs, 3).from, '手札', '全戻しで引いたカードは手札');
assert.equal(at(logs, 4).from, 'EXデッキ');

// ── トークン: 出すまでどこにもいない（記録上はデッキに足してから場へ動かす） ──
logs = [mv('t', 'custom-slot-6', { orientation: 'defense' }), mv('t', 'side-slot-0'), { actionType: 'removeCard', cardId: 't', zoneId: 'side-slot-0' }];
assert.deepEqual(at(logs, 0), { cardId: 't', name: '羊', tag: 'トークン・守備表示', from: '', to: 'モンスターゾーン' });
assert.equal(at(logs, 1).from, 'モンスターゾーン');
assert.deepEqual(at(logs, 2), { cardId: 't', name: '羊', tag: '取り除く', from: '墓地', to: '' });

// トークンでないカードの取り除きは再生で何も起きない（字幕にしない・居場所も変わらない）
logs = [mv('a', 'custom-slot-5'), { actionType: 'removeCard', cardId: 'a', zoneId: 'custom-slot-5' }, mv('a', 'side-slot-0')];
assert.equal(at(logs, 1), null);
assert.deepEqual(at(logs, 2), { cardId: 'a', name: '召喚獣エリュシオン', tag: '', from: 'モンスターゾーン', to: '墓地' });

// 全戻しの後のトークンは、また出すまで「トークン」
logs = [mv('t', 'custom-slot-6'), { actionType: 'resetDeck' }, mv('t', 'custom-slot-7')];
assert.equal(at(logs, 2).tag, 'トークン');
// 取り除いたトークンは全戻しでも戻らない（出し直すと「トークン」）
logs = [mv('t', 'custom-slot-6'), { actionType: 'removeCard', cardId: 't' }, { actionType: 'resetDeck' }, mv('t', 'custom-slot-7')];
assert.equal(at(logs, 3).tag, 'トークン');

// 辞書に無いカード・行き先の分からない移動は、再生で何も起きないので字幕にしない
assert.equal(describeStep([], [mv('zz', 'center-slot')], 0, {}), null);
assert.equal(at([mv('a', 'free-space')], 0), null);
assert.equal(at([mv('a', 'custom-slot-22')], 0), null);
assert.equal(at([mv('a', 'free-space'), mv('a', 'center-slot')], 1).from, 'デッキ', '分からない移動では居場所が変わらない');

// 名前の無いカード
assert.equal(describeStep([], [mv('a', 'center-slot')], 0, { images: { a: 'https://x/a.png' } }).name, 'カード');

// 古い記録（exCardIds が無い）は、デッキに戻す記録の isEx から EX のカードを推定する（replay-board.js と同じ）
const old = { images: dict.images, names: dict.names };
logs = [mv('e', 'custom-slot-1'), { actionType: 'returnToDeck', cardId: 'e', isEx: true }];
assert.equal(describeStep([], logs, 0, old).from, 'EXデッキ');

console.log('リプレイの字幕: 場所の名前・移動元のたどり方・札・トークン・範囲外を確認');
