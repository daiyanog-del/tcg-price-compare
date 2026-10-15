// shared_replays_trim_check.mjs — 共有履歴の上限（50件）で、公開中の項目（取り下げの鍵）を捨てないことの検証
import assert from 'node:assert/strict';
import { trimSharedReplays } from '../../static/solitaire/js/services/shared-replays.js';

const item = (i, pub = false) => (pub ? { id: `p${i}`, public: true, deleteToken: `t${i}` } : { id: `n${i}` });

// 上限以下はそのまま
let list = [item(1), item(2, true)];
assert.deepEqual(trimSharedReplays(list), list);

// 先頭（新しい）に公開、末尾（古い）に公開がある 55 件 → 通常の古いものから 5 件捨てる
list = [item(0, true)];
for (let i = 1; i <= 53; i++) list.push(item(i));
list.push(item(99, true));            // 一番古い公開中
assert.equal(list.length, 55);
let out = trimSharedReplays(list);
assert.equal(out.length, 50);
assert.ok(out.some(r => r.id === 'p99'), '一番古い公開中が残る');
assert.ok(out.some(r => r.id === 'p0'));
// 捨てられたのは通常の項目の古いほう（n53, n52, ...）
assert.ok(!out.some(r => r.id === 'n53') && !out.some(r => r.id === 'n49'));
assert.ok(out.some(r => r.id === 'n48'));

// 公開中だけで上限を超える場合は、最後の手段として一番古い公開中を捨てる
list = Array.from({ length: 52 }, (_, i) => item(i, true));
out = trimSharedReplays(list);
assert.equal(out.length, 50);
assert.deepEqual(out.map(r => r.id), list.slice(0, 50).map(r => r.id));

// 元の配列は変えない
assert.equal(list.length, 52);
console.log('shared_replays_trim_check: OK');
