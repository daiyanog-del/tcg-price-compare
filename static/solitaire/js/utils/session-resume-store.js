/**
 * session-resume-store.js
 * ページ遷移をまたぐ盤面＋リプレイ記録の退避（sessionStorage）の保存・取り出しロジック。
 *
 * 依存を持たない純粋なモジュール（storage / compress を引数で受ける）。
 * save-load-service.js から使い、node 単体テストでも直接読み込めるようにしている。
 */

export const SESSION_RESUME_KEY = 'sol-session-resume';
// 容量超過で記録だけ落として盤面のみ保存したことを、次回ロードへ伝えるフラグ
export const SESSION_REPLAY_DROPPED_KEY = 'sol-session-resume-replay-dropped';

// 記録込みで退避できたときだけ置く、記録の手数（リプレイ一覧ページが「置き換わります」確認に使う）
export const SESSION_RESUME_STEPS_KEY = 'sol-session-resume-steps';

/**
 * リプレイ一覧のリンクを開く前に「今の記録が置き換わる」確認を出すべきかの判定。
 * 修飾キーなしの左クリックのみ対象（新しいタブで開く場合は今のページが残るので確認しない）。
 * 退避に記録（steps キーがある）が無ければ確認しない。
 * @param {{button:number,ctrlKey?:boolean,metaKey?:boolean,shiftKey?:boolean,altKey?:boolean}} ev
 * @param {{getItem:Function}} storage
 * @returns {{ask:boolean, steps:number}}
 */
export function shouldConfirmOpen(ev, storage) {
  const none = { ask: false, steps: 0 };
  if (!ev || ev.button !== 0 || ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.altKey) return none;
  try {
    const raw = storage.getItem(SESSION_RESUME_STEPS_KEY);
    if (raw === null || raw === undefined || raw === '') return none;
    const steps = parseInt(raw, 10);
    if (!Number.isFinite(steps) || steps < 0) return none;
    return { ask: true, steps };
  } catch (e) {
    return none;
  }
}

/** 退避データから外す style の visibility 指定（演出中に hidden のまま保存されると復元後に見えないカードになる） */
export function stripVisibility(style) {
  return String(style || '')
    .split(';')
    .filter((d) => d.trim() && d.split(':')[0].trim().toLowerCase() !== 'visibility')
    .join(';')
    .trim();
}

/**
 * 退避したリプレイ記録に含まれるカードID（画像辞書のキー・各ログの cardId・resetDeck の drawnIds）を集める。
 * DOM に無いID（例: 範囲共有で取り込んだ記録）も、再利用されないようカウンターへ反映するために使う。
 * @param {Object|null} replay
 * @returns {string[]}
 */
export function collectReplayCardIds(replay) {
  if (!replay) return [];
  const ids = new Set(Object.keys(replay.images || {}));
  for (const list of [replay.logs, replay.setupLogs]) {
    for (const ev of Array.isArray(list) ? list : []) {
      if (!ev) continue;
      if (ev.cardId) ids.add(ev.cardId);
      if (Array.isArray(ev.drawnIds)) ev.drawnIds.forEach((id) => id && ids.add(id));
    }
  }
  return [...ids];
}

/** 退避データと「記録を落とした」フラグを両方消す（すべて消去・復元失敗時用） */
export function clearSessionResume(storage) {
  storage.removeItem(SESSION_RESUME_KEY);
  storage.removeItem(SESSION_REPLAY_DROPPED_KEY);
  storage.removeItem(SESSION_RESUME_STEPS_KEY);
}

/**
 * 盤面（state）とリプレイ記録（replay）を1つの圧縮データとして保存する。
 * setItem が例外（QuotaExceededError 等）になったら、記録を外した盤面だけで再試行し、
 * 成功したら「記録を落とした」フラグを立てる。盤面だけでも失敗したら例外をそのまま投げる。
 * @param {{getItem,setItem,removeItem}} storage
 * @param {(s:string)=>string} compress
 * @param {Object} state   盤面の写し
 * @param {Object|null} replay  リプレイ退避データ（空なら null/undefined で省略）
 * @returns {{replayDropped:boolean}}
 */
export function storeSessionResume(storage, compress, state, replay) {
  storage.removeItem(SESSION_REPLAY_DROPPED_KEY); // 前回の古いフラグを残さない
  storage.removeItem(SESSION_RESUME_STEPS_KEY);
  if (replay) {
    try {
      storage.setItem(SESSION_RESUME_KEY, compress(JSON.stringify({ ...state, replay })));
      // 手数は logs.length（setup だけの記録は 0 手）。存在自体が「記録あり」の印
      try { storage.setItem(SESSION_RESUME_STEPS_KEY, String((replay.logs || []).length)); } catch (e) { /* 確認表示が出ないだけ */ }
      return { replayDropped: false };
    } catch (e) {
      console.warn('セッション保存: 記録込みでは保存できません。盤面のみ保存します:', e);
    }
    storage.setItem(SESSION_RESUME_KEY, compress(JSON.stringify(state)));
    try { storage.setItem(SESSION_REPLAY_DROPPED_KEY, '1'); } catch (e) { /* 通知フラグだけ失敗しても盤面は残す */ }
    return { replayDropped: true };
  }
  storage.setItem(SESSION_RESUME_KEY, compress(JSON.stringify(state)));
  return { replayDropped: false };
}

/**
 * 退避データを取り出して削除する。「記録を落とした」フラグも同時に消費する。
 * @returns {{state:Object, replayDropped:boolean}|null} 退避が無ければ null
 */
export function takeSessionResume(storage, decompress) {
  const raw = storage.getItem(SESSION_RESUME_KEY);
  const dropped = storage.getItem(SESSION_REPLAY_DROPPED_KEY) === '1';
  storage.removeItem(SESSION_REPLAY_DROPPED_KEY);
  storage.removeItem(SESSION_RESUME_STEPS_KEY);
  if (!raw) return null;
  storage.removeItem(SESSION_RESUME_KEY);
  return { state: JSON.parse(decompress(raw)), replayDropped: dropped };
}

// ── 元に戻す（2026-10-21・ブラウザ標準の確認窓をやめて「すぐ実行して元に戻すを出す」にした。ui/undo-point.js） ──
// 消す・入れ替える操作の直前の盤面と記録を、退避と同じ形で別のキーに写しておき、
// 「元に戻す」で退避のキーへ戻してからシミュレーターを開き直す（ページを移ったときの復元と同じ道を通す）。
export const UNDO_RESUME_KEY = 'sol-undo-resume';
export const UNDO_REPLAY_DROPPED_KEY = 'sol-undo-resume-replay-dropped';
export const UNDO_STEPS_KEY = 'sol-undo-resume-steps';
// ページを移った先（シミュレーター）で「元に戻す」を出すための文言（リプレイ一覧から開いた等）
export const UNDO_OFFER_KEY = 'sol-undo-offer';
// 元に戻して開き直したことの印（復元のお知らせを「元に戻しました」にする）
export const UNDO_RESTORED_KEY = 'sol-undo-restored';
// ページを移る前に出せなかったお知らせ（移った先で出す）
export const NOTICE_KEY = 'sol-notice';

const _PAIRS = [
  [SESSION_RESUME_KEY, UNDO_RESUME_KEY],
  [SESSION_REPLAY_DROPPED_KEY, UNDO_REPLAY_DROPPED_KEY],
  [SESSION_RESUME_STEPS_KEY, UNDO_STEPS_KEY],
];

/** 元に戻すための写しを消す */
export function clearUndo(storage) {
  for (const [, u] of _PAIRS) storage.removeItem(u);
  storage.removeItem(UNDO_OFFER_KEY);
}

/** 元に戻すための写しがあるか */
export function hasUndo(storage) {
  return !!storage.getItem(UNDO_RESUME_KEY);
}

/**
 * 今の退避（セッション復元のキー）を、元に戻すための写しへ移す。退避が無ければ何もせず false。
 * 移したあと退避のキーは消す（次に開いたときに古い退避が復元されないように）。
 */
export function moveResumeToUndo(storage) {
  if (!storage.getItem(SESSION_RESUME_KEY)) return false;
  clearUndo(storage);
  for (const [r, u] of _PAIRS) {
    const v = storage.getItem(r);
    if (v !== null) storage.setItem(u, v);
    storage.removeItem(r);
  }
  return true;
}

/** 今の退避を残したまま、元に戻すための写しへ複製する（リプレイ一覧から開くとき。退避はシミュレーター側で上書きされる） */
export function copyResumeToUndo(storage) {
  if (!storage.getItem(SESSION_RESUME_KEY)) return false;
  clearUndo(storage);
  for (const [r, u] of _PAIRS) {
    const v = storage.getItem(r);
    if (v !== null) storage.setItem(u, v);
  }
  return true;
}

/** 元に戻すための写しを退避のキーへ戻す（次に開いたときに復元される）。写しが無ければ false */
export function moveUndoToResume(storage) {
  if (!storage.getItem(UNDO_RESUME_KEY)) return false;
  clearSessionResume(storage);
  for (const [r, u] of _PAIRS) {
    const v = storage.getItem(u);
    if (v !== null) storage.setItem(r, v);
    storage.removeItem(u);
  }
  storage.removeItem(UNDO_OFFER_KEY);
  storage.setItem(UNDO_RESTORED_KEY, '1');
  return true;
}

/** 一度だけ読む値（読んだら消す） */
export function takeOnce(storage, key) {
  const v = storage.getItem(key);
  if (v !== null) storage.removeItem(key);
  return v;
}
