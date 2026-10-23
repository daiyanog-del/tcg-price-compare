// 日本に来たTCG先行カードの名前の書き換え（static/shared/tcg-rename.js・2026-10-23 段3）を実コードで確かめる。
// 本文・メイン・EX・所持枚数・下書き・購入候補の書き換え、同じ名前の合流、何度動かしても同じ結果、同期への保存経路。
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert/strict');
const root = path.join(__dirname, '../..');
const src = fs.readFileSync(path.join(root, 'static/shared/tcg-rename.js'), 'utf8');

const win = {};  // addEventListener が無いので読み込み時には動かない
vm.runInNewContext(src, { window: win, console });
const R = win.TcgRename;
// vm の中で作られた値は型の出どころが違うので、JSON を通して比べる
const plain = v => JSON.parse(JSON.stringify(v));
const same = (a, b) => assert.deepEqual(plain(a), b);
const renames = { 'Angelechy Destrier': '天使チェスの騎兵', 'Kerolia the Frolicsome': 'ケロリア' };
const look = R.lookup(renames);

// 照合のゆれ（大文字小文字・記号・空白）を吸収し、表に無い名前は undefined
assert.equal(look('angelechy  destrier'), '天使チェスの騎兵');
assert.equal(look('Angelechy-Destrier'), '天使チェスの騎兵');
assert.equal(look('灰流うらら'), undefined);
assert.equal(R.lookup({})('Angelechy Destrier'), undefined);

// 本文: 枚数・[EX]・行の並び・前後の空白はそのまま
{
  const text = '3 灰流うらら\n2 Angelechy Destrier \n[EX]\n1 kerolia the frolicsome\nAngelechy Destrier';
  const r = R.renameText(text, look);
  assert.equal(r.changed, true);
  assert.equal(r.text, '3 灰流うらら\n2 天使チェスの騎兵 \n[EX]\n1 ケロリア\n天使チェスの騎兵');
  assert.equal(R.renameText(r.text, look).changed, false);  // 2回目は何も変えない
}

// メイン: 名前だけを書き換え、行はまとめない（同じ名前の行が2つになっても照合は名前ごとの合計で行う）
{
  const rows = [{ qty: 1, name: '天使チェスの騎兵' }, { qty: 3, name: '灰流うらら' }, { qty: 2, name: 'Angelechy Destrier' }];
  same(R.renameRows(rows, look).rows, [{ qty: 1, name: '天使チェスの騎兵' }, { qty: 3, name: '灰流うらら' }, { qty: 2, name: '天使チェスの騎兵' }]);
  const untouched = [{ qty: 3, name: '灰流うらら' }];
  assert.equal(R.renameRows(untouched, look).rows, untouched);  // 変わらなければ同じ配列
  // TCG先行カードを含まない、同じ名前の行が2つあるデッキは「変わった」にしない（関係ないデッキを保存し直さない）
  const dup = { text: 'X\nX', main: [{ qty: 1, name: 'X' }, { qty: 1, name: 'X' }], ex: [] };
  assert.equal(R.renameDeck(dup, look).changed, false);
}

// 所持枚数: 英語名のキーを日本語名へ。日本語名の欄が既にあれば大きい方（同期の結合と同じ）
{
  same(R.renameOwned({ 'Angelechy Destrier': 2, '灰流うらら': 3 }, look).owned, { '灰流うらら': 3, '天使チェスの騎兵': 2 });
  same(R.renameOwned({ 'Angelechy Destrier': 2, '天使チェスの騎兵': 1 }, look).owned, { '天使チェスの騎兵': 2 });
  same(R.renameOwned({ 'Angelechy Destrier': 1, '天使チェスの騎兵': 3 }, look).owned, { '天使チェスの騎兵': 3 });
}

// 購入候補: 書き換えた行がぶつかったときだけまとめ、枚数・デッキごとの内訳は大きい方（足すと同期で倍になる）。
// レアリティ違いは別。書き換えの無い元からの重複はそのまま
{
  const list = [
    { name: '天使チェスの騎兵', rarity: 'UR', qty: 1, decks: { d1: 1 } },
    { name: 'Angelechy Destrier', rarity: 'UR', qty: 2, decks: { d1: 1, d2: 1 } },
    { name: 'Angelechy Destrier', rarity: '', qty: 1, decks: {} },
    { name: '灰流うらら', rarity: '', qty: 1, decks: {} },
    { name: '灰流うらら', rarity: '', qty: 2, decks: {} },
  ];
  same(R.renameWishlist(list, look).list, [
    { name: '天使チェスの騎兵', rarity: 'UR', qty: 2, decks: { d1: 1, d2: 1 } },
    { name: '天使チェスの騎兵', rarity: '', qty: 1, decks: {} },
    { name: '灰流うらら', rarity: '', qty: 1, decks: {} },
    { name: '灰流うらら', rarity: '', qty: 2, decks: {} },
  ]);
  // 同期で英語名の行が戻ってきても、書き換えのたびに増えない
  const once = R.renameWishlist([{ name: '天使チェスの騎兵', rarity: '', qty: 3, decks: {} }, { name: 'Angelechy Destrier', rarity: '', qty: 3, decks: {} }], look).list;
  same(once, [{ name: '天使チェスの騎兵', rarity: '', qty: 3, decks: {} }]);
  // 元からの重複は「変わった」にしない
  assert.equal(R.renameWishlist([{ name: 'X', qty: 1 }, { name: 'X', qty: 1 }], look).changed, false);
}

// 保存の経路: 保存デッキは savedDecksSet 経由・書き換えたデッキだけ updated を1つ進める・下書きは直接・購入候補は wishSave 経由
function memory(init) {
  const data = new Map(Object.entries(init).map(([k, v]) => [k, typeof v === 'string' ? v : JSON.stringify(v)]));
  return { getItem: k => (data.has(k) ? data.get(k) : null), setItem: (k, v) => data.set(k, String(v)), data };
}
{
  const storage = memory({
    cardprice_saved_decks: [
      { id: 'd1', name: '天使', text: '2 Angelechy Destrier', main: [], ex: [{ qty: 2, name: 'Angelechy Destrier' }], owned: { 'Angelechy Destrier': 1 }, updated: 100 },
      { id: 'd2', name: '他', text: '3 灰流うらら', main: [{ qty: 3, name: '灰流うらら' }], ex: [], updated: 50 },
    ],
    cardprice_deck_draft: { text: '1 Kerolia the Frolicsome', main: [{ qty: 1, name: 'Kerolia the Frolicsome' }], ex: [], savedId: 'd1' },
    cardprice_wishlist: [{ name: 'Angelechy Destrier', rarity: '', qty: 1, decks: {} }],
  });
  const calls = { decks: [], wish: [], render: 0 };
  const g = {
    savedDecksSet: list => { calls.decks.push(list); storage.setItem('cardprice_saved_decks', JSON.stringify(list)); },
    renderSavedDecks: () => { calls.render += 1; },
    wishSave: list => { calls.wish.push(list); storage.setItem('cardprice_wishlist', JSON.stringify(list)); },
  };
  const res = R.applyToStorage(renames, { global: g, storage });
  same(res, { decks: 1, draft: 1, wishlist: 1 });
  assert.equal(calls.decks.length, 1);
  const [d1, d2] = calls.decks[0];
  assert.equal(d1.text, '2 天使チェスの騎兵');
  same(d1.ex, [{ qty: 2, name: '天使チェスの騎兵' }]);
  same(d1.owned, { '天使チェスの騎兵': 1 });
  // 「今」ではなく1つだけ進める（別の端末で後から編集されていれば、同期の結合でそちらが残る）
  assert.equal(d1.updated, 101);
  assert.equal(d2.updated, 50);             // 書き換えていないデッキはそのまま
  assert.equal(calls.render, 1);
  same(JSON.parse(storage.getItem('cardprice_deck_draft')).main, [{ qty: 1, name: 'ケロリア' }]);
  assert.equal(calls.wish[0][0].name, '天使チェスの騎兵');

  // 2回目は何も保存しない（同期へ余計に送らない）
  const again = R.applyToStorage(renames, { global: g, storage });
  same(again, { decks: 0, draft: 0, wishlist: 0 });
  assert.equal(calls.decks.length, 1);
  assert.equal(calls.wish.length, 1);
}

// savedDecksSet の無い画面（シミュレーターの読み込み直後）: 同じキーに書いて同期にだけ知らせる
{
  const storage = memory({ cardprice_saved_decks: [{ id: 'd1', text: '1 Angelechy Destrier', main: [], ex: [], updated: 5000 }] });
  const sent = [];
  R.applyToStorage(renames, { global: { SyncClient: { onDecksSave: list => sent.push(list) } }, storage });
  assert.equal(JSON.parse(storage.getItem('cardprice_saved_decks'))[0].updated, 5001);
  assert.equal(sent.length, 1);
}

// 壊れたデータ・空でも止まらない
{
  const storage = memory({ cardprice_saved_decks: 'x', cardprice_deck_draft: [1] });
  same(R.applyToStorage(renames, { global: {}, storage }), { decks: 0, draft: 0, wishlist: 0 });
}

// 読み込み時: 控えの表があれば、そのとき（下書きの復元・同期の受信より前）に書き換える。表の取得は load の後
{
  const storage = memory({
    cardprice_tcg_renames: JSON.stringify(renames),
    cardprice_deck_draft: { text: '1 Angelechy Destrier', main: [{ qty: 1, name: 'Angelechy Destrier' }], ex: [] },
  });
  const listeners = {};
  let fetched = 0;
  const w = {
    localStorage: storage,
    document: { readyState: 'interactive' },
    addEventListener: (ev, fn) => { listeners[ev] = fn; },
  };
  vm.runInNewContext(src, { window: w, console, fetch: () => { fetched += 1; return new Promise(() => {}); } });
  assert.equal(JSON.parse(storage.getItem('cardprice_deck_draft')).text, '1 天使チェスの騎兵');
  assert.equal(fetched, 0);       // 表の取得はまだ（load の後）
  listeners.load();
  assert.equal(fetched, 1);
}

// 表の取得: 控えと同じなら何もしない／新しい表なら控えを直して書き換える／失敗なら何もしない
(async () => {
  const run = async (body, storage) => {
    const w = { localStorage: storage, document: { readyState: 'complete' }, addEventListener: () => {} };
    let resolve;
    const done = new Promise(r => { resolve = r; });
    vm.runInNewContext(src, { window: w, console, fetch: () => { const p = Promise.resolve(body); p.then(() => setTimeout(resolve, 0)); return p; } });
    await done;
    return storage;
  };
  const ok = renamesObj => ({ ok: true, json: () => Promise.resolve({ renames: renamesObj }) });
  {
    const s = memory({ cardprice_deck_draft: { text: '1 Kerolia the Frolicsome', main: [], ex: [] } });
    await run(ok(renames), s);
    assert.equal(JSON.parse(s.getItem('cardprice_deck_draft')).text, '1 ケロリア');
    assert.equal(s.getItem('cardprice_tcg_renames'), JSON.stringify(renames));
  }
  {
    const s = memory({ cardprice_deck_draft: { text: '1 Kerolia the Frolicsome', main: [], ex: [] } });
    await run({ ok: false, json: () => Promise.resolve({}) }, s);
    assert.equal(JSON.parse(s.getItem('cardprice_deck_draft')).text, '1 Kerolia the Frolicsome');
    assert.equal(s.getItem('cardprice_tcg_renames'), null);
  }
  console.log('ok');
})().catch(e => { console.error(e); process.exit(1); });
