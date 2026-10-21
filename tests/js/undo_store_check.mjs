// 「元に戻す」の写し（utils/session-resume-store.js の moveResumeToUndo / copyResumeToUndo / moveUndoToResume）の検証。
// 2026-10-21 シミュレーターの確認窓をやめて「すぐ実行して元に戻すを出す」にした（ui/undo-point.js）。
import assert from 'node:assert/strict';
import {
  SESSION_RESUME_KEY, SESSION_REPLAY_DROPPED_KEY, SESSION_RESUME_STEPS_KEY,
  UNDO_RESUME_KEY, UNDO_REPLAY_DROPPED_KEY, UNDO_STEPS_KEY, UNDO_OFFER_KEY, UNDO_RESTORED_KEY, UNDO_ID_KEY,
  moveResumeToUndo, copyResumeToUndo, moveUndoToResume, clearUndo, hasUndo, takeOnce,
} from '../../static/solitaire/js/utils/session-resume-store.js';

const mk = () => {
  const m = new Map();
  return { m, getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: k => m.delete(k) };
};

// 退避が無ければ写さない
let s = mk();
assert.equal(moveResumeToUndo(s), false);
assert.equal(copyResumeToUndo(s), false);
assert.equal(hasUndo(s), false);

// 移す: 退避の3つのキーが写しへ移り、退避は消える（次に開いたとき古い退避を復元しない）
s = mk();
s.setItem(SESSION_RESUME_KEY, 'STATE'); s.setItem(SESSION_RESUME_STEPS_KEY, '5');
assert.equal(moveResumeToUndo(s), true);
assert.equal(s.getItem(UNDO_RESUME_KEY), 'STATE');
assert.equal(s.getItem(UNDO_STEPS_KEY), '5');
assert.equal(s.getItem(UNDO_REPLAY_DROPPED_KEY), null);
assert.equal(s.getItem(SESSION_RESUME_KEY), null, '移したら退避は消す');
assert.equal(s.getItem(SESSION_RESUME_STEPS_KEY), null);

// 戻す: 写しが退避へ戻り、元に戻した印が付き、写しと「元に戻す」の文言は消える
s.setItem(UNDO_OFFER_KEY, 'メッセージ');
s.setItem(SESSION_RESUME_KEY, '後の盤面'); // 戻す前の退避（消した後の盤面）は上書きされる
assert.equal(moveUndoToResume(s), true);
assert.equal(s.getItem(SESSION_RESUME_KEY), 'STATE');
assert.equal(s.getItem(SESSION_RESUME_STEPS_KEY), '5');
assert.equal(s.getItem(UNDO_RESUME_KEY), null);
assert.equal(s.getItem(UNDO_OFFER_KEY), null);
assert.equal(s.getItem(UNDO_RESTORED_KEY), '1');
assert.equal(moveUndoToResume(s), false, '写しが無ければ何もしない');

// 複製: 退避は残したまま写しを作る（リプレイ一覧から開くとき）
s = mk();
s.setItem(SESSION_RESUME_KEY, 'S2'); s.setItem(SESSION_RESUME_STEPS_KEY, '3');
assert.equal(copyResumeToUndo(s), true);
assert.equal(s.getItem(SESSION_RESUME_KEY), 'S2');
assert.equal(s.getItem(UNDO_RESUME_KEY), 'S2');
assert.equal(s.getItem(UNDO_STEPS_KEY), '3');
assert.ok(s.getItem(UNDO_ID_KEY), '複製した写しにも世代を付ける（reviewer 指摘）');
// 記録を容量超過で落とした退避は写さない（元に戻しても記録が戻らないため。reviewer 指摘）
const s3 = mk();
s3.setItem(SESSION_RESUME_KEY, 'S3'); s3.setItem(SESSION_REPLAY_DROPPED_KEY, '1');
assert.equal(copyResumeToUndo(s3), false);
assert.equal(s3.getItem(UNDO_RESUME_KEY), null);
assert.equal(s3.getItem(UNDO_REPLAY_DROPPED_KEY), null);
// 前の写しの手数が残らない（退避に手数が無いとき）
s.removeItem(SESSION_RESUME_STEPS_KEY);
s.setItem(UNDO_STEPS_KEY, '9');
copyResumeToUndo(s);
assert.equal(s.getItem(UNDO_STEPS_KEY), null, '前の写しの手数を残さない');

// 捨てる・一度だけ読む
clearUndo(s);
assert.equal(hasUndo(s), false);
s.setItem('k', 'v');
assert.equal(takeOnce(s, 'k'), 'v');
assert.equal(takeOnce(s, 'k'), null);

console.log('元に戻すの写し: 移す・戻す・複製・捨てる・一度だけ読むを確認');
