// 購入候補の「どのデッキ用か」（static/shared/wish-deck.js）: 合計で数える・減らす順・内訳の上限・絞り込み・不足分
'use strict';
const assert = require('assert/strict');
const W = require('../../static/shared/wish-deck.js');

// 古い行（内訳なし）はデッキ指定なしとして読める
const old = W.normalizeItem({ name: 'A', qty: 2 });
assert.deepEqual(old, { name: 'A', qty: 2, rarity: '' });
assert.equal(W.unassigned(old), 2);
assert.equal(W.forDeck(old, 'd1'), 0);

// 内訳の合計は qty を超えない（後ろから削る）・不正な値は落とす
assert.deepEqual(W.normalizeItem({ name: 'A', qty: 3, decks: { d1: 2, d2: 5, '': 1, d3: 1.5 } }),
  { name: 'A', qty: 3, rarity: '', decks: { d1: 2, d2: 1 } });

// 2つのデッキで足りないときは合計で数える（ユーザー決定）
let list = W.add([], 'G', 2, 'd1');
list = W.add(list, 'G', 3, 'd2');
assert.deepEqual(list, [{ name: 'G', qty: 5, rarity: '', decks: { d1: 2, d2: 3 } }]);
// デッキ指定なしで足すと内訳は増えない
list = W.add(list, 'G', 1, null);
assert.equal(list[0].qty, 6);
assert.equal(W.unassigned(list[0]), 1);
// レアリティ指定ありの行には足さない（指定なしの行に足す）
let r = W.add([{ name: 'G', qty: 1, rarity: 'SE' }], 'G', 1, 'd1');
assert.deepEqual(r, [{ name: 'G', qty: 1, rarity: 'SE' }, { name: 'G', qty: 1, rarity: '', decks: { d1: 1 } }]);
// 上限
assert.throws(() => W.add([{ name: 'G', qty: 99, rarity: '' }], 'G', 1, 'd1'), /99枚/);
assert.throws(() => W.add(Array.from({ length: 200 }, (_, i) => ({ name: 'x' + i, qty: 1, rarity: '' })), 'new', 1, 'd1'), /200件/);
// 元の配列は変えない
const src = [{ name: 'G', qty: 1, rarity: '', decks: { d1: 1 } }];
W.add(src, 'G', 1, 'd1');
assert.deepEqual(src, [{ name: 'G', qty: 1, rarity: '', decks: { d1: 1 } }]);

// 減らす: そのデッキ用→デッキ指定なしの順。他のデッキ用の分は減らさない
let res = W.remove(list, 'G', 1, 'd1');
assert.deepEqual(res.list[0].decks, { d1: 1, d2: 3 });
assert.equal(res.removed, 1);
res = W.remove([{ name: 'G', qty: 4, rarity: '', decks: { d2: 3 } }], 'G', 2, 'd1');
assert.equal(res.removed, 1, 'デッキ指定なしの1枚だけ外れる');
assert.deepEqual(res.list, [{ name: 'G', qty: 3, rarity: '', decks: { d2: 3 } }]);
res = W.remove([{ name: 'G', qty: 1, rarity: '', decks: { d1: 1 } }], 'G', 1, 'd1');
assert.deepEqual(res.list, [], '0枚になった行は消える');

// 手で枚数を減らしたら内訳も削る／まとめると内訳を足す
assert.deepEqual(W.setQty({ name: 'G', qty: 5, rarity: '', decks: { d1: 2, d2: 3 } }, 3), { name: 'G', qty: 3, rarity: '', decks: { d1: 2, d2: 1 } });
assert.deepEqual(W.mergeInto({ name: 'G', qty: 2, rarity: 'SE', decks: { d1: 1 } }, { name: 'G', qty: 2, rarity: '', decks: { d1: 1, d2: 1 } }),
  { name: 'G', qty: 4, rarity: 'SE', decks: { d1: 2, d2: 1 } });

// 絞り込み（店の比較に渡す枚数はそのデッキ用の枚数）
const mixed = [{ name: 'G', qty: 6, rarity: '', decks: { d1: 2, d2: 3 } }, { name: 'U', qty: 1, rarity: '' }];
assert.deepEqual(W.filter(mixed, 'd2'), [{ name: 'G', qty: 3, rarity: '' }]);
assert.deepEqual(W.filter(mixed, ''), [{ name: 'G', qty: 1, rarity: '' }, { name: 'U', qty: 1, rarity: '' }]);
assert.equal(W.filter(mixed, null), mixed);
assert.deepEqual(W.deckIds(mixed), ['d1', 'd2']);

// 不足分: 候補にある＝そのデッキ用＋デッキ指定なし。未入力は足さない。メイン＋EX は呼び出し側で合わせて渡す
const totals = new Map([['G', 3], ['U', 3], ['N', 2], ['__proto__', 1]]);
const owned = Object.assign(Object.create(null), { G: 1, N: 0, ['__proto__']: 0 });
const wish = [{ name: 'G', qty: 4, rarity: '', decks: { d1: 1, d2: 3 } }];
const rows = W.plan(totals, owned, wish, 'd1');
const by = Object.fromEntries(rows.map(x => [x.name, x]));
assert.equal(by.G.candidate, 1, '他のデッキ用（d2 の3枚）は数えない');
assert.equal(by.G.add, 1);
assert.equal(by.U.owned, null);
assert.equal(by.U.add, 0, '未入力は足さない');
assert.equal(by.N.add, 2);
assert.equal(by.__proto__.add, 1, '__proto__ という名前のカードも普通に数える');
const next = W.supplement(wish, rows, 'd1');
assert.deepEqual(next.find(c => c.name === 'G'), { name: 'G', qty: 5, rarity: '', decks: { d1: 2, d2: 3 } });
assert.deepEqual(next.find(c => c.name === 'N'), { name: 'N', qty: 2, rarity: '', decks: { d1: 2 } });
// ひも付いていないデッキ（deckId なし）はデッキ指定なしの分だけを候補にあると数える
assert.equal(W.plan(new Map([['G', 3]]), { G: 0 }, wish, null)[0].candidate, 0);

// 削除したデッキ用の分はデッキ指定なしとして数える（reviewer 指摘6）
const withGone = [{ name: 'G', qty: 3, rarity: '', decks: { gone: 3 } }];
assert.deepEqual(W.forgetUnknown(withGone, ['d1']), [{ name: 'G', qty: 3, rarity: '' }]);
assert.equal(W.plan(new Map([['G', 3]]), { G: 0 }, W.forgetUnknown(withGone, ['d1']), 'd1')[0].add, 0, '削除したデッキ用の3枚を候補にあると数える');
// 保存に使うときはその行だけにかける（他の行の、まだ届いていないデッキ用の内訳は残す）
const two = [{ name: 'G', qty: 2, rarity: '', decks: { gone: 2 } }, { name: 'U', qty: 1, rarity: '', decks: { notYet: 1 } }];
assert.deepEqual(W.forgetUnknown(two, ['d1'], 'G'), [{ name: 'G', qty: 2, rarity: '' }, { name: 'U', qty: 1, rarity: '', decks: { notYet: 1 } }]);
// デッキを削除したときはそのデッキ用の内訳だけ外す
assert.deepEqual(W.dropDeck([{ name: 'G', qty: 3, rarity: '', decks: { d1: 1, notYet: 2 } }], 'd1'), [{ name: 'G', qty: 3, rarity: '', decks: { notYet: 2 } }]);

// 絞り込み中の −（そのデッキ用の分だけ減らす・reviewer 指摘5）
res = W.remove([{ name: 'G', qty: 5, rarity: '', decks: { d1: 2, d2: 3 } }], 'G', 1, 'd1', { onlyDeck: true });
assert.deepEqual(res.list, [{ name: 'G', qty: 4, rarity: '', decks: { d1: 1, d2: 3 } }]);
res = W.remove([{ name: 'G', qty: 4, rarity: '', decks: { d2: 3 } }], 'G', 1, 'd1', { onlyDeck: true });
assert.equal(res.removed, 0, 'そのデッキ用の分が無ければ指定なしの分も減らさない');
// 文字列の枚数・内訳の件数上限
assert.equal(W.normalizeItem({ name: 'A', qty: '3' }).qty, 3);
const many = {}; for (let i = 0; i < 60; i++) many['d_' + i] = 1;
assert.equal(Object.keys(W.normalizeItem({ name: 'A', qty: 99, decks: many }).decks).length, 50);

console.log('購入候補のデッキ内訳: 合計で数える・減らす順・上限・正規化・絞り込み・不足分を確認');
