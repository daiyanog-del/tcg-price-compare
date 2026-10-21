/**
 * undo-point.js — 「すぐ実行して元に戻すを出す」の土台（2026-10-21・モック _mocks/sim-refresh-20261021 の A）。
 *
 *   以前はクリア・記録が消えるデッキの読み込み・リプレイを開くときに、ブラウザ標準の確認窓（confirm）を出していた。
 *   今は直前の盤面と記録を写しておき、実行してから「元に戻す」を6秒出す。
 *   写しはページを移るときの退避（save-load-service.js saveSessionResume）と同じ形で、別のキーに置く。
 *   元に戻すときは写しを退避のキーへ戻し、シミュレーターを開き直す（ページを移ったときの復元と同じ道を通す。
 *   盤面・記録・マイデッキとのひも付け・範囲の状態をまとめて戻せる。開き直すので一瞬画面が切り替わる）。
 */
import { captureReplaySession, isPlaying, isReplayAnimating } from '../services/replay-service.js';
import { saveSessionResume } from '../services/save-load-service.js';
import { captureLinkState } from './deck-edit-ui.js';
import { getPcRecordingState, isPcRangePicking, closePcRangePicker } from './pc-recording.js';
import { getMobileRecordingState, isMobilePlaybackMode } from './mobile-ui.js';
import { showActionToast } from './inline-ui.js';
import {
  clearSessionResume, moveResumeToUndo, moveUndoToResume, clearUndo, hasUndo, takeOnce,
  UNDO_OFFER_KEY, UNDO_RESTORED_KEY, NOTICE_KEY,
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
 * 元に戻すための写しを取る（消す・入れ替える操作の直前に呼ぶ）。盤面も記録も空なら写しは作らず false。
 * @returns {boolean}
 */
export function saveUndoPoint() {
  try {
    clearSessionResume(sessionStorage);
    captureSessionToResume();
    return moveResumeToUndo(sessionStorage);
  } catch (e) {
    console.warn('[undo-point] 元に戻すための写しを取れませんでした', e);
    return false;
  }
}

/** 写しを捨てる（元に戻さなかったとき・操作が失敗したとき） */
export function discardUndoPoint() {
  try { clearUndo(sessionStorage); } catch (e) { /* 何もしない */ }
}

/** 写しから元に戻す（退避のキーへ戻して開き直す） */
export function restoreUndoPoint() {
  try {
    if (!moveUndoToResume(sessionStorage)) return;
  } catch (e) {
    console.warn('[undo-point] 元に戻せませんでした', e);
    showActionToast('元に戻せませんでした', { error: true });
    return;
  }
  // 開き直す前の pagehide で今の盤面（消した後・入れ替えた後）を退避に書かないように
  window.__solSkipResumeSave = true;
  location.assign('/solitaire');
}

/** 「元に戻す」付きのお知らせを出す。押さずに時間が過ぎたら写しを捨てる */
export function offerUndo(msg) {
  showActionToast(msg, { label: '元に戻す', onClick: restoreUndoPoint, onExpire: discardUndoPoint });
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
 */
export function consumeStartupOffers() {
  let notice = null, offer = null, undoable = false;
  try {
    takeOnce(sessionStorage, UNDO_RESTORED_KEY); // 復元されなかったときの印を残さない
    notice = takeOnce(sessionStorage, NOTICE_KEY);
    offer = takeOnce(sessionStorage, UNDO_OFFER_KEY);
    undoable = hasUndo(sessionStorage);
  } catch (e) { return; }
  if (offer && undoable) offerUndo(notice ? `${notice}。${offer}` : offer);
  else {
    if (undoable && !offer) discardUndoPoint(); // 使われなかった古い写しを残さない
    if (notice) showActionToast(notice);
  }
}
