// replay_rename_service_check.mjs — 名前を変える相手の見つけ方と、変える処理（services/replay-rename.js）の検証
import assert from 'node:assert/strict';

const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => { store.set(k, String(v)); },
  removeItem: (k) => { store.delete(k); },
};
let fetchCalls = [];
let nextResponse = null;
globalThis.fetch = async (url, opts) => {
  fetchCalls.push({ url, body: JSON.parse(opts.body) });
  if (nextResponse instanceof Error) throw nextResponse;
  return new Response(JSON.stringify(nextResponse.body), { status: nextResponse.status });
};

const { findRenamableReplay, renameReplay, renameMaxLength } = await import('../../static/solitaire/js/services/replay-rename.js');

store.set('sol-saved-replays', JSON.stringify([{ id: 's1', title: '保存', savedAt: 1, data: 'x' }]));
store.set('sol-shared-replays', JSON.stringify([
  { id: 'pub', title: '公開', createdAt: 1, steps: 1, public: true, deleteToken: 'tok' },
  { id: 'link', title: '非公開', createdAt: 1, steps: 1 },
]));

// 見つけ方: 端末保存・公開中（鍵あり）だけ。非公開の共有リンク・他人のリンク・何も無いときは null
assert.deepEqual(findRenamableReplay({ savedReplayId: 's1' }), { kind: 'saved', id: 's1', title: '保存' });
assert.deepEqual(findRenamableReplay({ replayId: 'pub' }), { kind: 'public', id: 'pub', title: '公開', token: 'tok' });
assert.equal(findRenamableReplay({ replayId: 'link' }), null);
assert.equal(findRenamableReplay({ replayId: 'other' }), null);
assert.equal(findRenamableReplay({ savedReplayId: 'none' }), null);
assert.equal(findRenamableReplay({}), null);
assert.equal(renameMaxLength({ kind: 'saved' }), 100);
assert.equal(renameMaxLength({ kind: 'public' }), 40);

// 端末保存: fetch しない
let r = await renameReplay({ kind: 'saved', id: 's1' }, '新しい保存');
assert.deepEqual(r, { ok: true, title: '新しい保存' });
assert.equal(JSON.parse(store.get('sol-saved-replays'))[0].title, '新しい保存');
assert.equal(fetchCalls.length, 0);

// 公開中: 鍵と名前を送り、サーバーが返した（整えた後の）名前で控えを書き換える
const target = findRenamableReplay({ replayId: 'pub' });
nextResponse = { status: 200, body: { ok: true, title: '整えた名前' } };
r = await renameReplay(target, '  整えた名前 ');
assert.deepEqual(r, { ok: true, title: '整えた名前' });
assert.deepEqual(fetchCalls[0], { url: '/api/solitaire/replay/pub/rename', body: { token: 'tok', title: '  整えた名前 ' } });
let pub = JSON.parse(store.get('sol-shared-replays'))[0];
assert.equal(pub.title, '整えた名前');
assert.ok(pub.renamedAt);

// サーバーの断り（400）: 文をそのまま返し、控えは変えない
nextResponse = { status: 400, body: { error: '名前に使えない言葉が含まれています' } };
r = await renameReplay(target, 'x');
assert.deepEqual(r, { ok: false, message: '名前に使えない言葉が含まれています' });
assert.equal(JSON.parse(store.get('sol-shared-replays'))[0].title, '整えた名前');

// 目印の無い 403（プロキシ等）: 鍵は捨てない
nextResponse = { status: 403, body: {} };
r = await renameReplay(target, 'x');
assert.equal(r.ok, false);
assert.ok(!r.gone);
assert.equal(JSON.parse(store.get('sol-shared-replays'))[0].deleteToken, 'tok');

// 通信の失敗
nextResponse = new Error('offline');
r = await renameReplay(target, 'x');
assert.equal(r.ok, false);
assert.match(r.message, /通信に失敗/);

// not_renamable の 403: 公開中でなくなっていた → 控えの鍵を外して gone
nextResponse = { status: 403, body: { error: '名前を変えられません', code: 'not_renamable' } };
r = await renameReplay(target, 'x');
assert.equal(r.ok, false);
assert.equal(r.gone, true);
pub = JSON.parse(store.get('sol-shared-replays'))[0];
assert.equal(pub.deleteToken, undefined);
assert.equal(pub.public, undefined);
assert.equal(findRenamableReplay({ replayId: 'pub' }), null);

console.log('replay_rename_service_check: OK');
