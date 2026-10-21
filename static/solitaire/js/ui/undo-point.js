/**
 * undo-point.js — 「すぐ実行して元に戻すを出す」の土台（2026-10-21・モック _mocks/sim-refresh-20261021 の A）。
 *
 *   以前はクリア・記録が消えるデッキの読み込み・リプレイを開くときに、ブラウザ標準の確認窓（confirm）を出していた。
 *   今は直前の盤面と記録を写しておき、実行してから「元に戻す」を6秒出す。
 *   写しはページを移るときの退避（save-load-service.js saveSessionResume）と同じ形で、別のキーに置く。
 *   元に戻すときは写しを退避のキーへ戻し、シミュレーターを開き直す（ページを移ったときの復元と同じ道を通す。
 *   盤面・記録・マイデッキとのひも付け・範囲の状態をまとめて戻せる。開き直すので一瞬画面が切り替わる）。
 *
 *   写しには世代（UNDO_ID_KEY）を付ける。新しい写しを取った後に前の「元に戻す」が期限切れになっても、新しい写しは捨てない。
 *   写しを取れないとき（容量超過・記録が大きすぎて退避から落ちた）は、元に戻せないので確認の小窓に戻す（guardUndoable）。
 */
import { captureReplaySession, isPlaying, isReplayAnimating, subscribeReplay } from '../services/replay-service.js';
import { saveSessionResume } from '../services/save-load-service.js';
import { captureLinkState } from './deck-edit-ui.js';
import { getPcRecordingState, isPcRangePicking, closePcRangePicker } from './pc-recording.js';
import { getMobileRecordingState, isMobilePlaybackMode } from './mobile-ui.js';
import { showActionToast, dismissActionToast, confirmAsync } from './inline-ui.js';
import {
  SESSION_RESUME_KEY, SESSION_REPLAY_DROPPED_KEY,
  clearSessionResume, moveResumeToUndo, moveUndoToResume, clearUndo, hasUndo, takeOnce,
  UNDO_OFFER_KEY, UNDO_RESTORED_KEY, UNDO_ID_KEY, NOTICE_KEY,
} from '../utils/session-resume-store.js';

/**
 * 今の盤面・記録・ひも付けを退避のキーへ書く（ページを離れるとき＝pagehide と、元に戻すための写しの両方で使う）。
 * 範囲を選ぶパネルを開いたままなら、先に閉じて開く前の手へ戻す（途中の手のままロックなしで戻らないように）。
 */
export function captureSessionToResume() {
  const wasPicking = isPcRangePicking();
  if (wasPicking) closePcRangePicker();
  const replay = captureReplaySession();
  if (replay) {
    replay.pcRecording = getPcRecordingState();
    replay.mobileRecording = getMobileRecordingState();
    replay.mobilePlayback = isMobilePlaybackMode();
    // 自動再生中・再生の演出の途中なら、盤面が中間状態の可能性がある。復元時に作り直させる
    if (isPlaying() || isReplayAnimating() || wasPicking) replay.needsRebuild = true;
  }
  saveSessionResume(replay, captureLinkState());
}

/**
 * 元に戻すための写しを取る（消す・入れ替える操作の直前に呼ぶ）。
 * @returns {{id:string}|{empty:true}|{failed:true}}
 *   id … 写しの世代（offerUndo / discardUndoPoint に渡す）
 *   empty … 盤面も記録も空（消しても失うものが無い）
 *   failed … 写しを取れなかった・記録が大きすぎて写しから落ちた（元に戻せない）
 */
export function saveUndoPoint() {
  try {
    clearSessionResume(sessionStorage);
    captureSessionToResume();
    if (!sessionStorage.getItem(SESSION_RESUME_KEY)) return { empty: true };
    // 記録が大きすぎて盤面だけで退避した場合は、元に戻しても記録が戻らないので「元に戻せない」として扱う
    if (sessionStorage.getItem(SESSION_REPLAY_DROPPED_KEY) === '1') {
      clearSessionResume(sessionStorage);
      return { failed: true };
    }
    if (!moveResumeToUndo(sessionStorage)) return { failed: true };
    const id = String(Date.now()) + Math.random().toString(36).slice(2, 6);
    sessionStorage.setItem(UNDO_ID_KEY, id);
    return { id };
  } catch (e) {
    console.warn('[undo-point] 元に戻すための写しを取れませんでした', e);
    try { clearUndo(sessionStorage); clearSessionResume(sessionStorage); } catch (_) { /* 何もしない */ }
    return { failed: true };
  }
}

/**
 * 消す・入れ替える操作の前に呼ぶ。写しを取れれば { ok:true, id }。
 * 写しを取れない（元に戻せない）ときは確認の小窓を出し、やめたら { ok:false }。
 * @param {Element|null} anchor  確認の小窓の起点
 * @param {string} message       元に戻せないときの確認の文
 * @param {string} okLabel
 * @returns {Promise<{ok:boolean, id:(string|null)}>}
 */
export async function guardUndoable(anchor, message, okLabel) {
  const r = saveUndoPoint();
  if (r.id) return { ok: true, id: r.id };
  if (r.empty) return { ok: true, id: null };
  const yes = await confirmAsync(anchor, message, okLabel, true);
  return { ok: yes, id: null };
}

/** 写しを捨てる。id を渡したときは、その世代の写しのときだけ捨てる（新しい写しを消さない） */
export function discardUndoPoint(id) {
  try {
    if (id && sessionStorage.getItem(UNDO_ID_KEY) !== id) return;
    clearUndo(sessionStorage);
  } catch (e) { /* 何もしない */ }
}

/** 写しから元に戻す（退避のキーへ戻して開き直す） */
export function restoreUndoPoint() {
  let ok = false;
  try { ok = moveUndoToResume(sessionStorage); } catch (e) { console.warn('[undo-point] 元に戻せませんでした', e); }
  if (!ok) { showActionToast('元に戻せませんでした', { error: true }); return; }
  // 開き直す前の pagehide で今の盤面（消した後・入れ替えた後）を退避に書かないように
  window.__solSkipResumeSave = true;
  // 履歴に残さない（戻るボタンで「消した後」のページへ戻らないように）
  location.replace('/solitaire');
}

// 戻るボタンなどで前のページがそのまま戻った（bfcache）ときは、退避を書く状態に戻す
window.addEventListener('pageshow', (e) => { if (e.persisted) window.__solSkipResumeSave = false; });

// 盤面の「元に戻す」を出している間に、別のデッキの読み込み・クリア・カードを動かす操作が入ったら引っ込める
// （押すとその間の操作ごと開き直しで失われるため。deck-edit-ui.js の保存の取り消しと同じ考え方）
// 手数の変化では見ない: 開いた先でリプレイを読み込み終えたときにも手数が変わり、出した直後に消えてしまうため（reviewer 指摘）
let _offerShown = false;
function _withdrawBoardUndo() {
  if (!_offerShown) return;
  _offerShown = false;
  dismissActionToast(true);
}
document.addEventListener('sol-deck-load-start', _withdrawBoardUndo);
document.addEventListener('sol-board-cleared', _withdrawBoardUndo);
subscribeReplay('edited', _withdrawBoardUndo);

/**
 * 「元に戻す」付きのお知らせを出す。押さずに時間が過ぎたら、その世代の写しを捨てる。
 * @param {string} msg
 * @param {string|null} id  saveUndoPoint の世代（null なら今の写し）
 */
export function offerUndo(msg, id) {
  let gen = id;
  if (!gen) { try { gen = sessionStorage.getItem(UNDO_ID_KEY); } catch (e) { gen = null; } }
  showActionToast(msg, {
    label: '元に戻す',
    onClick: () => { _offerShown = false; restoreUndoPoint(); },
    // 世代が分からない写しは捨てない（新しい写しを消さないため。残った古い写しは次の起動で consumeStartupOffers が掃除する）
    onExpire: () => { _offerShown = false; if (gen) discardUndoPoint(gen); },
  });
  _offerShown = true;
}

/** ページを移った先で「元に戻す」を出すための印を付ける（写しは saveUndoPoint で取っておく） */
export function markUndoOffer(msg) {
  try { sessionStorage.setItem(UNDO_OFFER_KEY, msg); } catch (e) { /* 出せないだけ */ }
}

/** 元に戻して開き直した直後か（一度だけ true） */
export function takeUndoRestored() {
  try { return takeOnce(sessionStorage, UNDO_RESTORED_KEY) === '1'; } catch (e) { return false; }
}

/**
 * 起動の最後に呼ぶ: 前のページから引き継いだお知らせと「元に戻す」を出す。
 * 「元に戻す」は写しが残っているときだけ（写しが無ければ印だけ捨てる）。
 * 元に戻して開き直したのに復元されなかった（印が残っている）ときは、そのことを知らせる。
 */
export function consumeStartupOffers() {
  let notice = null, offer = null, undoable = false, restoreFailed = false;
  try {
    restoreFailed = takeOnce(sessionStorage, UNDO_RESTORED_KEY) === '1';
    notice = takeOnce(sessionStorage, NOTICE_KEY);
    offer = takeOnce(sessionStorage, UNDO_OFFER_KEY);
    undoable = hasUndo(sessionStorage);
  } catch (e) { return; }
  if (restoreFailed) {
    showActionToast(notice ? `元に戻せませんでした（前の盤面を読み込めませんでした）。${notice}` : '元に戻せませんでした（前の盤面を読み込めませんでした）', { error: true });
    return;
  }
  if (offer && undoable) offerUndo(notice ? `${notice}。${offer}` : offer, null);
  else {
    if (undoable && !offer) discardUndoPoint(); // 使われなかった古い写しを残さない
    if (notice) showActionToast(notice);
  }
}
