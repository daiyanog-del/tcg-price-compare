// デッキ（材料）とリプレイ（レシピ）のひも付け（static/shared/deck-replays.js）: 一覧・数・中身の写し・差・文
'use strict';
const assert = require('assert/strict');
const DR = require('../../static/shared/deck-replays.js');

const store = new Map();
const storage = { getItem: k => store.has(k) ? store.get(k) : null };
assert.deepEqual(DR.list(storage), [], '何も無ければ空');
store.set(DR.KEY, '{壊れた');
assert.deepEqual(DR.list(storage), [], '壊れていれば空');
const items = [
  { id: 'r1', title: 'A', savedAt: 2, steps: 18, deckId: 'd1' },
  { id: 'r2', title: 'B', savedAt: 3, steps: 24, deckId: 'd1' },
  { id: 'r3', title: 'C', savedAt: 1, steps: 5, deckId: 'd2' },
  { id: 'r4', title: 'D', savedAt: 4, steps: 1 },
  null, { title: 'idなし' },
];
store.set(DR.KEY, JSON.stringify(items));
const all = DR.list(storage);
assert.equal(all.length, 4, '壊れた要素は落とす');
assert.deepEqual(DR.forDeck(all, 'd1').map(r => r.id), ['r2', 'r1'], 'そのデッキの分を新しい順');
assert.deepEqual(DR.forDeck(all, null), [], 'デッキが無ければ空');
assert.deepEqual({ ...DR.countByDeck(all) }, { d1: 2, d2: 1 });

// 中身の写しはメインと EX を名前でまとめる・__proto__ も普通の名前
const snap = DR.snapshot({ main: [{ name: 'X', qty: 3 }, { name: '__proto__', qty: 1 }], ex: [{ name: 'X', qty: 1 }, { name: 'E', qty: 2 }] });
assert.deepEqual({ ...snap }, { X: 4, ['__proto__']: 1, E: 2 });

// 差: 今のほうが多い＝＋・少ない＝−・無くなった・増えたカード
const d = DR.diff({ X: 3, Y: 2, Z: 1 }, { main: [{ name: 'X', qty: 3 }, { name: 'Y', qty: 1 }, { name: 'W', qty: 2 }], ex: [] });
assert.deepEqual(d.added, [{ name: 'W', n: 2 }]);
assert.deepEqual(d.removed, [{ name: 'Y', n: 1 }, { name: 'Z', n: 1 }]);
assert.equal(d.changed, true);
assert.equal(DR.diff({ X: 3 }, { main: [{ name: 'X', qty: 3 }] }).changed, false, '同じなら変化なし');
assert.equal(DR.diff(undefined, { main: [] }), null, '写しの無い古いリプレイは比べない');
assert.equal(DR.describe(d), '＋W 2枚　−Y 1枚　−Z 1枚');
assert.equal(DR.describe(DR.diff({ a: 1, b: 1, c: 1, d: 1, e: 1 }, { main: [] }), 3), '−a 1枚　−b 1枚　−c 1枚　ほか2種類');
assert.equal(DR.meta({ steps: 18, savedAt: new Date(2026, 9, 20).getTime() }), '18手・10/20');
assert.equal(DR.meta({}), '0手');
console.log('デッキとリプレイのひも付け: 一覧・数・中身の写し・差・文を確認');
