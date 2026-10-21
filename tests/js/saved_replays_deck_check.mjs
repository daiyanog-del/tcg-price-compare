// 端末保存のリプレイにデッキの ID と中身が残る（static/solitaire/js/services/saved-replays.js saveReplayEntry・2026-10-21）
import assert from 'assert/strict';
const store = new Map();
globalThis.localStorage = { getItem: k => store.has(k) ? store.get(k) : null, setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) };
const { saveReplayEntry, savedReplaysGet } = await import('../../static/solitaire/js/services/saved-replays.js');
const base = { title: 'A', main: 40, ex: 15, steps: 3, data: 'x' };
let r = saveReplayEntry({ ...base, deckId: 'd_1', deckSnap: { X: 3 } });
assert.equal(r.ok, true);
let it = savedReplaysGet().find(x => x.id === r.id);
assert.equal(it.deckId, 'd_1');
assert.deepEqual(it.deckSnap, { X: 3 });
r = saveReplayEntry({ ...base, deckSnap: { X: 3 } });
it = savedReplaysGet().find(x => x.id === r.id);
assert.equal('deckId' in it, false, 'デッキの無いリプレイには付けない');
assert.equal('deckSnap' in it, false, 'デッキの無いリプレイは中身も残さない');
console.log('端末保存のリプレイ: デッキの ID と中身を残す・デッキが無ければ残さない を確認');
