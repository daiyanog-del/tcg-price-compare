// 盤面セーブ（スロット）→ 0手リプレイ移行（slot-migration.js / saved-replays.js）の単体チェック。
// LZString は圧縮しない代用品（同じ文字列を返す）。localStorage は Map の代用品で、容量不足も再現する。
'use strict';
const fs = require('fs'), path = require('path'), assert = require('assert/strict');
const js = path.join(__dirname, '../../static/solitaire/js');
const b64 = (src) => 'data:text/javascript;base64,' + Buffer.from(src).toString('base64');

(async () => {
  const warns = [];
  console.warn = (...a) => { warns.push(a.map(String).join(' ')); }; // 失敗時の警告は想定内なので出力しない
  globalThis.LZString = { compress: (s) => s, decompress: (s) => s };
  const store = new Map();
  let quota = false;
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { if (quota && k === 'sol-saved-replays') throw new Error('quota'); store.set(k, String(v)); },
    removeItem: (k) => { store.delete(k); },
  };

  const saved = await import(b64(fs.readFileSync(path.join(js, 'services/saved-replays.js'), 'utf8')));
  let src = fs.readFileSync(path.join(js, 'services/slot-migration.js'), 'utf8');
  src = src.replace(/from '\.\/saved-replays\.js'/, `from '${b64(fs.readFileSync(path.join(js, 'services/saved-replays.js'), 'utf8'))}'`);
  const mig = await import(b64(src));

  const card = (id, extra = {}) => ({ id, src: `https://example.invalid/${id}.png`, style: '', cardName: 'N-' + id, state: { orientation: '', face: '' }, ...extra });
  const slotState = () => ({
    timestamp: 1700000000000, slotName: '練習',
    slots: {
      // デッキの並びが「種類→ID順」と違う（item-003 の魔法が先頭）→ 並び替えのログが付く
      poolRow: [card('item-003 normal', { cardType: 'spell' }), card('item-001 normal', { cardType: 'monster' })],
      poolRow2: [card('item-000 ex', { cardType: 'monster' })],
      'custom-slot-5': [card('item-004 normal'), card('item-005 normal', { state: { orientation: 'defense', face: '' } })],
      'side-slot-0': [card('item-006 normal'), { id: 'initial', src: 'x' }],
      'side-slot-2': [card('item-007 normal')],
      'free-space': [card('item-007 normal')], // side-slot-2 と同じカード（二重に載る）
      'center-slot': [],
    },
    counters: [
      { value: '3', parentId: 'item-005 normal', parentDataSlot: null, style: '' },
      { value: '1', parentId: 'item-005 normal', parentDataSlot: null, style: '' },
      { value: '9', parentId: 'tier-item-wrapper', parentDataSlot: null, style: '' }, // 旧形式（class 名）→ 移せない
    ],
  });

  // ── 変換 ──
  const { payload, warnings } = mig.convertSlotToPayload(slotState(), 1);
  assert.equal(payload.version, 1);
  assert.equal(payload.title, '練習');
  assert.deepEqual(Object.keys(payload.images).sort(), ['item-000', 'item-001', 'item-003', 'item-004', 'item-005', 'item-006', 'item-007']);
  assert.deepEqual(payload.exCardIds, ['item-000']);
  assert.equal(payload.cardTypes['item-003'], 'spell');
  assert.equal(payload.names['item-004'], 'N-item-004 normal');
  assert(payload.logs.every(e => e.setup === true && e.seq === -1), '全ログが setup');
  const moves = payload.logs.filter(e => e.actionType === 'moveCard');
  assert.deepEqual(moves.slice(0, 2).map(e => [e.cardId, e.zoneId]), [['item-003', 'poolRow'], ['item-001', 'poolRow']], 'デッキの並びの再現');
  assert.equal(moves.filter(e => e.cardId === 'item-007').length, 1, '二重に載るカードは1回だけ');
  assert.deepEqual(moves.filter(e => e.zoneId === 'custom-slot-5').map(e => [e.cardId, e.zIndex, e.orientation]), [['item-004', '1', ''], ['item-005', '2', 'defense']]);
  assert.deepEqual(payload.logs.filter(e => e.actionType === 'counterChange').map(e => [e.cardId, e.counter, e.op]), [['item-005', 3, 'add'], ['item-005', 1, 'add']]);
  assert(warnings.some(w => w.includes('カウンター 1 個')), '移せないカウンターの警告');
  // 自然な並び（種類→ID順）のデッキには並び替えログを足さない
  const nat = slotState(); nat.slots.poolRow = [card('item-001 normal', { cardType: 'monster' }), card('item-003 normal', { cardType: 'spell' })];
  assert.equal(mig.convertSlotToPayload(nat, 1).payload.logs.filter(e => e.zoneId === 'poolRow').length, 0);
  // 空の盤面は payload なし・cardCount 0
  const empty = mig.convertSlotToPayload({ slots: { poolRow: [], poolRow2: [] }, counters: [] }, 2);
  assert.equal(empty.payload, null); assert.equal(empty.cardCount, 0);
  // 構造が読めなければ例外
  assert.throws(() => mig.convertSlotToPayload({ foo: 1 }, 1));

  // ── 移行: 既存20件を押し出さない・成功したキーだけ消す ──
  const twenty = Array.from({ length: 20 }, (_, i) => ({ id: 'r_e' + i, title: 'e' + i, savedAt: 1800000000000 + i, main: 1, ex: 0, steps: 1, data: '{}' }));
  store.set('sol-saved-replays', JSON.stringify(twenty));
  store.set('card-sim-save-slot-1', JSON.stringify(slotState()));
  store.set('card-sim-save-slot-2', 'これは壊れたデータ');
  store.set('card-sim-save-slot-3', JSON.stringify({ slots: { poolRow: [], poolRow2: [] }, counters: [] })); // 空
  let r = mig.migrateSlotsToReplays();
  assert.deepEqual(r, { migrated: 1, failed: 1 });
  let list = saved.savedReplaysGet();
  assert.equal(list.length, 21, '上限20を超えて登録し、既存は消えない');
  for (let i = 0; i < 20; i++) assert(list.some(x => x.id === 'r_e' + i), '既存が残る ' + i);
  const mine = list.find(x => x.origin);
  assert.equal(mine.steps, 0); assert.equal(mine.title, '練習'); assert.equal(mine.savedAt, 1700000000000);
  assert.equal(mine.main, 6); assert.equal(mine.ex, 1);
  assert(!store.has('card-sim-save-slot-1') && !store.has('card-sim-save-slot-3'), '成功・空のキーは消える');
  assert(store.has('card-sim-save-slot-2'), '失敗したキーは残る');
  // 冪等: もう一度呼んでも増えない
  r = mig.migrateSlotsToReplays();
  assert.deepEqual(r, { migrated: 0, failed: 1 });
  assert.equal(saved.savedReplaysGet().length, 21);

  // ── キーを消す前に中断された場合（登録済みの印があればキーだけ消す） ──
  store.set('card-sim-save-slot-4', JSON.stringify(slotState())); // 同じ timestamp だが番号が違うので別の印
  store.set('card-sim-save-slot-1', JSON.stringify(slotState()));  // slot1 の印は登録済み
  r = mig.migrateSlotsToReplays();
  assert.deepEqual(r, { migrated: 1, failed: 1 }, 'slot1 は登録済みなので数えず、slot4 だけ移る');
  assert(!store.has('card-sim-save-slot-1') && !store.has('card-sim-save-slot-4'));
  assert.equal(saved.savedReplaysGet().length, 22);

  // ── 容量不足: 既存を削らず失敗として残す ──
  store.set('card-sim-save-slot-5', JSON.stringify({ ...slotState(), timestamp: 1700000009999 }));
  quota = true;
  r = mig.migrateSlotsToReplays();
  assert.deepEqual(r, { migrated: 0, failed: 2 });
  assert(store.has('card-sim-save-slot-5'));
  assert.equal(saved.savedReplaysGet().length, 22, '容量不足でも既存を落として再試行しない');
  quota = false;
  r = mig.migrateSlotsToReplays();
  assert.deepEqual(r, { migrated: 1, failed: 1 });
  assert(!store.has('card-sim-save-slot-5'));

  // ── 通常の保存は上限20件に整理するが、移行元の印（origin）付きは消さない ──
  const originIds = saved.savedReplaysGet().filter(x => x.origin).map(x => x.id);
  assert.equal(originIds.length, 3);
  const res = saved.saveReplayEntry({ title: 'x', main: 1, ex: 0, steps: 1, data: '{}' });
  assert.equal(res.ok, true);
  const after = saved.savedReplaysGet();
  for (const id of originIds) assert(after.some(x => x.id === id), '通常の保存のあとも移行分が残る');
  assert.equal(after.length, 20, '印の無い既存分が古い順に整理され、合計が20件になる');
  assert(after.some(x => x.id === res.id), '今保存した項目は残る');
  // 印の無い項目が尽きても、今の項目は消えず移行分も残る
  store.set('sol-saved-replays', JSON.stringify(after.filter(x => x.origin)));
  const res2 = saved.saveReplayEntry({ title: 'y', main: 1, ex: 0, steps: 1, data: '{}' });
  assert.equal(res2.ok, true); assert.equal(res2.removedCount, 0);
  assert.equal(saved.savedReplaysGet().length, originIds.length + 1);

  // ── ファイルの検証 ──
  assert.throws(() => saved.assertReplayPayload(null));
  assert.throws(() => saved.assertReplayPayload({ foo: 1 }));
  assert.throws(() => saved.assertReplayPayload({ images: { a: 5 }, logs: [] }));
  assert.throws(() => saved.assertReplayPayload({ images: {}, logs: 'x' }));
  saved.assertReplayPayload({ images: { a: 'u' }, logs: [] });

  assert(warns.some(w => w.includes('スロット2の移行に失敗')) && warns.some(w => w.includes('登録できず残しました')), '失敗時の console.warn');
  console.log('slot_migration_check OK');
})().catch((e) => { console.error(e); process.exit(1); });
