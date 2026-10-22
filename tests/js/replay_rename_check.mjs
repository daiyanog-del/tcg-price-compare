// replay_rename_check.mjs — リプレイの名前を変える（端末保存・共有履歴の控え）の検証
import assert from 'node:assert/strict';

const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => { store.set(k, String(v)); },
  removeItem: (k) => { store.delete(k); },
};

const { renameReplayEntry, savedReplaysGet } = await import('../../static/solitaire/js/services/saved-replays.js');
const { renameSharedReplay, sharedReplaysGet } = await import('../../static/solitaire/js/services/shared-replays.js');

// 端末保存: title だけ変わり、ほかの項目と data はそのまま
store.set('sol-saved-replays', JSON.stringify([
  { id: 'a', title: '旧', savedAt: 1, steps: 3, data: 'xyz', deckId: 'd1' },
  { id: 'b', title: '別', savedAt: 2, steps: 1, data: 'qqq' },
]));
assert.deepEqual(renameReplayEntry('a', '  新しい名前  '), { ok: true });
let saved = savedReplaysGet();
assert.deepEqual(saved[0], { id: 'a', title: '新しい名前', savedAt: 1, steps: 3, data: 'xyz', deckId: 'd1' });
assert.equal(saved[1].title, '別');
// 空は「無題」・100文字で切る・無い id は ok:false
renameReplayEntry('a', '   ');
assert.equal(savedReplaysGet()[0].title, '無題');
renameReplayEntry('a', 'あ'.repeat(150));
assert.equal(savedReplaysGet()[0].title.length, 100);
assert.deepEqual(renameReplayEntry('none', 'x'), { ok: false });

// 共有履歴の控え: title と renamedAt。鍵は残す
store.set('sol-shared-replays', JSON.stringify([
  { id: 'p', title: '旧', createdAt: 1, steps: 2, public: true, deleteToken: 'tok' },
]));
assert.deepEqual(renameSharedReplay('p', '新'), { ok: true });
const sh = sharedReplaysGet()[0];
assert.equal(sh.title, '新');
assert.equal(sh.deleteToken, 'tok');
assert.equal(sh.public, true);
assert.ok(Number.isFinite(sh.renamedAt));
assert.deepEqual(renameSharedReplay('none', 'x'), { ok: false });

console.log('replay_rename_check: OK');
