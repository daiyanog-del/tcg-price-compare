/**
 * pc-recording.js
 * PC版「一人回し」の録画（共有する範囲の切り出し）状態機械。
 * モバイル版の _recording 系（mobile-ui.js 879〜987行付近）を参考にした PC 専用実装。
 * モバイル版のコード（mobile-ui.js / mobile.css）には一切触れない。
 *
 * 「録画した範囲を再生」は開始位置から最後まで普通に再生する（モバイル版と一貫。
 * 範囲終了での自動停止は実装しない）。
 */

import { getCursor, getLogLength, seekTo, isPlaying, togglePlay } from '../services/replay-service.js';

/** 録画の状態: {recording, startIdx, endIdx}（startIdx/endIdx は1-indexed手数） */
let _pcRecording = { recording: false, startIdx: null, endIdx: null };

/** 録画停止時に呼ぶコールバック（共有シートを自動で開く。initPcRecording で受け取る） */
let _onStopCallback = null;

/**
 * 共有シートを閉じるコールバック（initPcRecording で受け取る）。
 * 「この範囲を再生」の直前、および共有シート内「録画を始める/やり直す」クリック時の
 * 双方から使う（どちらもシートを閉じてから本処理に進む必要があるため）。
 */
let _closeShareSheet = null;

/** 録画が開始され、かつ停止済み（範囲が確定している）かどうか */
function _hasStoppedPcRecording() {
  return !_pcRecording.recording && _pcRecording.startIdx !== null && _pcRecording.endIdx !== null;
}

/**
 * @returns {{start:number, end:number}|null} 1-indexed 手数の範囲。録画（停止済み）が無ければ null（＝全範囲扱い）。
 * 停止後の取消（undo）で手数が減っていても壊れないよう、表示・送信の直前で
 * endIdx を現在の手数以下へ、startIdx を endIdx 以下へクランプする（_pcRecording 自体は書き換えない）。
 */
export function _getPcRecordingRange() {
  if (!_hasStoppedPcRecording()) return null;
  const total = getLogLength();
  const end = Math.min(_pcRecording.endIdx, total);
  const start = Math.min(_pcRecording.startIdx, end);
  return { start: start + 1, end };
}

/**
 * 中2対応: 停止済み録画がある場合、endIdx を現在の手数へ追従させる。
 * PC版はリプレイバーの #replayCommentInput/#replayAddComment が録画停止後も常時使えるため、
 * コメント追加で手数が増えても endIdx が古いままだと共有範囲にコメントが含まれない
 * （モバイル版 mobile-ui.js の `if (_hasStoppedRecording()) _recording.endIdx = getLogLength();` と同じ発想）。
 * replay-ui.js の #replayAddComment クリック処理の後に呼ぶ。
 */
export function _syncPcRecordingEndIdxAfterComment() {
  if (_hasStoppedPcRecording()) {
    _pcRecording.endIdx = getLogLength();
    updatePcShareRecordingSection();
  }
}

/**
 * 録画状態を破棄する（デッキ再読込・JSON/共有リンク読込などで、別セッションの
 * 録画範囲が残留し誤った範囲を共有してしまうのを防ぐため、replay-ui.js から呼ぶ）。
 */
export function _resetPcRecording() {
  _pcRecording = { recording: false, startIdx: null, endIdx: null };
  _updatePcRecordBtn();
}

/**
 * 録画開始: この時点までの手数を基準にする。
 * 巻き戻し中（cursor がログ末尾より前）に開始した場合でも表示と一致させるため、
 * 全ログ長ではなく現在の再生位置（getCursor()+1）を基準にする。
 */
function _startPcRecording() {
  _pcRecording = { recording: true, startIdx: getCursor() + 1, endIdx: null };
  _updatePcRecordBtn();
  updatePcShareRecordingSection(); // 共有シートを開いたまま呼ばれた場合も表示を追従させる
}

/**
 * 録画停止: 停止時点までを録画範囲として確定し、共有シートを自動で開く。
 * 1手も録画されていない（endIdx <= startIdx）場合は不成立として状態を破棄するのみで、
 * 共有シートは開かない。
 */
function _stopPcRecording() {
  if (!_pcRecording.recording) return;
  const endIdx = getCursor() + 1;
  if (endIdx <= _pcRecording.startIdx) {
    _resetPcRecording();
    updatePcShareRecordingSection();
    return;
  }
  _pcRecording.endIdx = endIdx;
  _pcRecording.recording = false;
  _updatePcRecordBtn();
  updatePcShareRecordingSection();
  if (typeof _onStopCallback === 'function') _onStopCallback();
}

/** リプレイバーの「● 録画」ボタン（トグル） */
export function togglePcRecording() {
  if (_pcRecording.recording) _stopPcRecording();
  else _startPcRecording();
}

/**
 * 録画ボタンの見た目・手数バッジを更新する。
 * 録画中に取消（undo）や巻き戻し（前ボタン・スライダー）で現在位置が startIdx より
 * 前に戻ったら startIdx をその値にクランプする。
 */
function _updatePcRecordBtn() {
  const btn = document.getElementById('pcRecordBtn');
  const dot = document.getElementById('pcRecordDot');
  const label = document.getElementById('pcRecordLabel');
  if (!btn || !label) return;
  if (_pcRecording.recording) {
    const cur = getCursor() + 1;
    if (cur < _pcRecording.startIdx) _pcRecording.startIdx = cur; // クランプ
    const n = Math.max(0, cur - _pcRecording.startIdx);
    label.textContent = `■ ${n}手`;
    btn.classList.add('recording');
    if (dot) dot.hidden = false;
  } else {
    label.textContent = '録画';
    btn.classList.remove('recording');
    if (dot) dot.hidden = false;
  }
}

/**
 * 共有シート（#pcShareSheet）内の①録画セクションの表示を更新する。3状態:
 *   - 録画中（停止前）: 「録画中（N手）」＋「ここで停止して範囲を確定」
 *   - 録画（停止済み）あり: 「録画した範囲: s手目〜e手目（N手）」＋「録画をやり直す」＋「この範囲を再生」
 *   - 録画なし: 「全記録（N手）を共有します。…」＋「● 録画を始める」
 * replay-ui.js が共有シートを開く直前に呼ぶ。
 */
export function updatePcShareRecordingSection() {
  const infoEl = document.getElementById('pcShareRecordingInfo');
  const startBtn = document.getElementById('pcShareStartRecordingBtn');
  const stopBtn = document.getElementById('pcShareStopRecordingBtn');
  const redoBtn = document.getElementById('pcShareRedoRecordingBtn');
  const playBtn = document.getElementById('pcShareRangePlayBtn');
  if (!infoEl) return;

  if (_pcRecording.recording) {
    const n = Math.max(0, getCursor() + 1 - _pcRecording.startIdx);
    infoEl.textContent = `録画中（${n}手）`;
    if (startBtn) startBtn.hidden = true;
    if (stopBtn) stopBtn.hidden = false;
    if (redoBtn) redoBtn.hidden = true;
    if (playBtn) playBtn.hidden = true;
    return;
  }

  const range = _getPcRecordingRange();
  if (range) {
    const n = range.end - range.start + 1;
    infoEl.textContent = `録画した範囲: ${range.start}手目〜${range.end}手目（${n}手）`;
    if (startBtn) startBtn.hidden = true;
    if (stopBtn) stopBtn.hidden = true;
    if (redoBtn) redoBtn.hidden = false;
    if (playBtn) playBtn.hidden = false;
  } else {
    const total = getLogLength();
    infoEl.textContent = `全記録（${total}手）を共有します。特定の場面だけ共有するには上のリプレイバーの ● 録画 を使ってください。`;
    if (startBtn) startBtn.hidden = false;
    if (stopBtn) stopBtn.hidden = true;
    if (redoBtn) redoBtn.hidden = true;
    if (playBtn) playBtn.hidden = true;
  }
}

/**
 * 「この範囲を再生」: 開始位置の直前へシークしてから既存の #replayPlay（togglePlay()）を
 * クリックするだけ。開始位置から最後まで普通に再生する（範囲終了での自動停止はしない）。
 */
function _playPcRecordingRange() {
  const range = _getPcRecordingRange();
  if (!range) return;
  // 共有シートが盤面を覆ったままだと再生が見えないため、先に閉じる
  // （モバイル版の「再生して確認」＝closeShareSheet()→enterPlaybackModeと対称の挙動）。
  if (typeof _closeShareSheet === 'function') _closeShareSheet();
  // 重大3対応: #replayPlay はトグルボタンのため、既に自動再生中の状態で
  // クリック相当の操作をすると停止側に倒れてしまう（盤面だけ巻き戻って止まる）。
  // 再生中なら先に togglePlay() で止めてから seekTo → 再度 togglePlay() で開始する。
  if (isPlaying()) togglePlay();
  seekTo(range.start - 2);
  togglePlay();
}

/**
 * PC版録画機能を初期化する（#replayBarContainer の #pcRecordBtn・#pcShareSheet 内の
 * 録画セクションのボタン配線・#replayCounter の変化監視）。
 * @param {{ openShareSheet?: () => void, closeShareSheet?: () => void }} [deps]
 *   openShareSheet: 録画停止時に共有シートを自動で開くコールバック
 *   closeShareSheet: 「この範囲を再生」実行直前に共有シートを閉じるコールバック
 */
export function initPcRecording(deps = {}) {
  _onStopCallback = deps.openShareSheet;
  _closeShareSheet = deps.closeShareSheet;

  document.getElementById('pcRecordBtn')
    ?.addEventListener('click', togglePcRecording);

  // 「録画を始める/やり直す」: モバイル版（mobile-ui.js の
  // solMobileShareStartRecordingBtn 等）と同じく、共有シートを閉じてから録画を開始する。
  // 閉じないままだと全画面モーダルが盤面を覆い、録画開始直後に1手も打てない。
  const _startRecordingFromSheet = () => {
    if (typeof _closeShareSheet === 'function') _closeShareSheet();
    _startPcRecording();
  };
  document.getElementById('pcShareStartRecordingBtn')
    ?.addEventListener('click', _startRecordingFromSheet);
  document.getElementById('pcShareStopRecordingBtn')
    ?.addEventListener('click', _stopPcRecording);
  // 「録画をやり直す」も同じ経路にする（無条件で新規開始。
  // 停止済み録画は _startPcRecording が無条件で新しいオブジェクトに差し替えるため自動的に破棄される）。
  document.getElementById('pcShareRedoRecordingBtn')
    ?.addEventListener('click', _startRecordingFromSheet);
  document.getElementById('pcShareRangePlayBtn')
    ?.addEventListener('click', _playPcRecordingRange);

  // #replayCounter のテキスト変化（stepForward/stepBack/seekTo/undo 等、既存の手数更新経路）を
  // 監視し、録画中バッジの N手・クランプを追従させる（mobile-ui.js の initRecording と同じ手法）。
  const counterEl = document.getElementById('replayCounter');
  if (counterEl) {
    new MutationObserver(() => {
      if (_pcRecording.recording) _updatePcRecordBtn();
    }).observe(counterEl, { childList: true, characterData: true, subtree: true });
  }
}
