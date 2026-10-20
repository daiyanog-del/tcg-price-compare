/**
 * pc-recording.js
 * PC版「一人回し」の範囲選択（保存・共有する場面の切り出し。旧称「録画」）状態機械。
 * 画面上の呼び名は「範囲」（2026-10-15 整理③。「録画」は動画の書き出しと誤解されたため）。
 * 内部の変数名・関数名（recording 等）は退避データとの互換のため変えていない。
 * モバイル版の _recording 系（mobile-ui.js 879〜987行付近）を参考にした PC 専用実装。
 * モバイル版のコード（mobile-ui.js / mobile.css）には一切触れない。
 *
 * 「録画した範囲を再生」は開始位置から最後まで普通に再生する（モバイル版と一貫。
 * 範囲終了での自動停止は実装しない）。
 */

import { getCursor, getLogLength, seekTo, isPlaying, togglePlay, subscribeReplay } from '../services/replay-service.js';
import { shiftRangeAfterInsert, shiftRangeAfterRemove } from '../services/comment-model.js';

/** 録画の状態: {recording, startIdx, endIdx}（startIdx/endIdx は1-indexed手数） */
let _pcRecording = { recording: false, startIdx: null, endIdx: null };

/** 範囲が決まったときに呼ぶコールバック（通知の表示。initPcRecording で受け取る） */
let _onRangeDecided = null;

/**
 * 保存・共有シートを閉じるコールバック（initPcRecording で受け取る）。
 * 「この範囲を再生」の直前に使う（シートが盤面を覆ったままだと再生が見えないため）。
 */
let _closeShareSheet = null;

/** 録画が開始され、かつ停止済み（範囲が確定している）かどうか */
function _hasStoppedPcRecording() {
  return !_pcRecording.recording && _pcRecording.startIdx !== null && _pcRecording.endIdx !== null;
}

/**
 * @returns {{start:number, end:number}|null} 1-indexed 手数の範囲。録画（停止済み）が無ければ null（＝全範囲扱い）。
 * 停止後の取消（undo）で手数が減っていても壊れないよう、表示・送信の直前で
 * endIdx を現在の手数以下へクランプする（_pcRecording 自体は書き換えない）。
 * 取消や巻き戻した位置からの操作で録画範囲の手がすべて消えた場合は null（＝全範囲扱い）を返す。
 * 空範囲を返すと「2手目〜1手目（0手）」のような逆順表示になり、共有側（_buildRangeLogs）の丸めとも食い違うため。
 */
export function _getPcRecordingRange() {
  if (!_hasStoppedPcRecording()) return null;
  const total = getLogLength();
  const end = Math.min(_pcRecording.endIdx, total);
  if (_pcRecording.startIdx >= end) return null;
  return { start: _pcRecording.startIdx + 1, end };
}

/**
 * 記録にコメントが1つ差し込まれた（巻き戻した位置でも付けられる）ときに、範囲の開始・終了をずらす。
 * 範囲より前なら開始も終わりも1つ後ろへ、範囲の中なら終わりだけ1つ後ろへ、後ろなら変えない
 * （ずらし方の本体は comment-model.js の shiftRangeAfterInsert）。
 * @param {number} insertedIdx 差し込まれた位置（0始まり）
 */
function _shiftPcRangeAfterInsert(insertedIdx) {
  _pcRecording = shiftRangeAfterInsert(_pcRecording, insertedIdx);
  _updatePcRecordBtn();
  updatePcShareRecordingSection();
}

/**
 * 記録から手が1つ取り除かれた（コメントの削除）ときに、範囲の開始・終了をずらす。
 * 範囲より前を消したら開始も終わりも1つ前へ、範囲の中を消したら終わりだけ1つ前へ
 * （ずらし方の本体は comment-model.js の shiftRangeAfterRemove）。
 * @param {number} removedIdx 取り除かれた手の位置（0始まり）
 */
function _shiftPcRangeAfterRemove(removedIdx) {
  _pcRecording = shiftRangeAfterRemove(_pcRecording, removedIdx);
  _updatePcRecordBtn();
  updatePcShareRecordingSection();
}

/**
 * 決まった範囲が、取消（undo）・巻き戻しからの新しい操作で古くなっていないかを確かめて直す。
 *   - 開始位置（startIdx）が手数以上になった（範囲の手がすべて消えた）: 状態ごと捨てる。
 *     残すと、あとで手が増えたときに古い範囲が復活する。
 *   - 終了位置（endIdx）が手数を超えた: endIdx を手数まで下げる（クランプを表示時だけでなく状態にも反映）。
 * 手数が変わるたび（#replayCounter の監視）に呼ぶ。
 */
function _prunePcRange() {
  if (!_hasStoppedPcRecording()) return;
  const total = getLogLength();
  if (_pcRecording.startIdx >= total) {
    _resetPcRecording();
    updatePcShareRecordingSection();
  } else if (_pcRecording.endIdx > total) {
    _pcRecording.endIdx = total;
    updatePcShareRecordingSection();
  }
}

/** 録画状態の写しを返す（ページ遷移をまたぐ退避用） */
export function getPcRecordingState() {
  return { ..._pcRecording };
}

/** 退避した録画状態を戻し、ボタンと共有シート内の表示を更新する */
export function setPcRecordingState(state) {
  if (!state) return;
  // 2026-10-20 から範囲は「範囲を選ぶ」パネルで一度に決めるので、選んでいる途中（recording）の状態は戻さない
  _pcRecording = state.recording
    ? { recording: false, startIdx: null, endIdx: null }
    : { recording: false, startIdx: state.startIdx ?? null, endIdx: state.endIdx ?? null };
  _updatePcRecordBtn();
  updatePcShareRecordingSection();
}

/**
 * 録画状態を破棄する（デッキ再読込・JSON/共有リンク読込などで、別セッションの
 * 録画範囲が残留し誤った範囲を共有してしまうのを防ぐため、replay-ui.js から呼ぶ）。
 */
export function _resetPcRecording() {
  _pcRecording = { recording: false, startIdx: null, endIdx: null };
  _updatePcRecordBtn();
}

// ══════════════════ 範囲を選ぶパネル（2026-10-20 ユーザー承認の案A。モック _mocks/pc-range-20261020） ══════════════════
// 以前は「範囲を選ぶ」を押した時点の手が始まりになり（画面に出ない）、押した後に前の手へ戻ると始まりも一緒に
// 前へずれた。パネルで ①始まりの手 → ②終わりの手 を順に選ぶ。手はスライダーか ‹ › で動かす。
// 「この手から」「この手まで」は今見ている手（getCursor()+1 手目）をその端に含める（スマホの範囲を選ぶ画面と同じ）。
// 選んでいる間は盤面を触れなくする（途中の手で操作すると先の記録が消えるため。replay-ui.js が
// body.sol-pc-range-pick を見て再生中と同じロックを掛ける）。閉じたら開く前の手へ戻す。

const PICK_CLASS = 'sol-pc-range-pick';
/** @type {{s:number|null, e:number|null, step:1|2, cursorBefore:number}|null} 選んでいる途中（s/e は 1-indexed の手） */
let _pick = null;

function _now() {
  return getCursor() + 1; // 今見ている手（0 は何も進めていない最初の盤面）
}

function _el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function _buildPanel() {
  const panel = _el('div', 'sol-pc-range-panel');
  panel.id = 'solPcRangePanel';
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-label', '範囲を選ぶ');
  const head = _el('div', 'sol-pc-range-head');
  head.append(_el('span', 'sol-pc-range-title', '✂ 範囲を選ぶ'), Object.assign(_el('span', 'sol-pc-range-now'), { id: 'solPcRangeNow' }));
  const steps = _el('div', 'sol-pc-range-steps');
  const step = (n, label) => {
    const box = _el('div', 'sol-pc-range-step');
    box.id = `solPcRangeStep${n}`;
    box.append(_el('span', 'sol-pc-range-step-label', label), Object.assign(_el('b', 'sol-pc-range-step-val'), { id: `solPcRangeVal${n}` }));
    return box;
  };
  steps.append(step(1, '① 始まりの手'), step(2, '② 終わりの手'));
  const hint = _el('p', 'sol-pc-range-hint');
  hint.id = 'solPcRangeHint';
  const btns = _el('div', 'sol-pc-range-btns');
  const cancel = _el('button', 'sol-pc-range-cancel', 'やめる');
  cancel.type = 'button';
  cancel.addEventListener('click', () => closePcRangePicker());
  const ok = _el('button', 'sol-pc-range-ok');
  ok.type = 'button';
  ok.id = 'solPcRangeOk';
  ok.addEventListener('click', _onPickOk);
  btns.append(cancel, ok);
  panel.append(head, steps, hint, btns);
  return panel;
}

/** パネルを再生バーの上へ置き、選んだ範囲の金色の帯をスライダーの上に出す */
function _placePanel() {
  const panel = document.getElementById('solPcRangePanel');
  const bar = document.getElementById('replayBarContainer');
  if (!panel || !bar) return;
  const b = bar.getBoundingClientRect();
  panel.style.left = `${Math.round(b.left + 8)}px`;
  panel.style.top = `${Math.round(b.top - panel.offsetHeight - 14)}px`;
  const band = document.getElementById('solPcRangeBand');
  const slider = document.getElementById('replaySlider');
  if (!band || !slider || !_pick) return;
  const total = getLogLength();
  const s = _pick.s;
  const e = _pick.e ?? (s ? Math.max(_now(), s) : null);
  const r = slider.getBoundingClientRect();
  if (!s || !total || !r.width) { band.hidden = true; return; }
  // スライダーの値は 0〜手数（今見ている手）。つまみの幅の分だけ内側に寄せて、つまみの中心の位置に合わせる
  const THUMB = 16;
  const x = (v) => r.left + THUMB / 2 + (r.width - THUMB) * (v / total);
  band.hidden = false;
  band.style.left = `${Math.round(x(s - 1))}px`;
  band.style.width = `${Math.max(4, Math.round(x(e) - x(s - 1)))}px`;
  band.style.top = `${Math.round(r.top - 6)}px`;
}

function _renderPick() {
  if (!_pick) return;
  const now = _now();
  const total = getLogLength();
  const set = (id, text) => { const e = document.getElementById(id); if (e) e.textContent = text; };
  set('solPcRangeNow', `今 ${now} / ${total}手`);
  set('solPcRangeVal1', _pick.s ? `${_pick.s}手目` : '—');
  set('solPcRangeVal2', _pick.e ? `${_pick.e}手目` : '—');
  document.getElementById('solPcRangeStep1')?.classList.toggle('is-current', _pick.step === 1);
  document.getElementById('solPcRangeStep2')?.classList.toggle('is-current', _pick.step === 2);
  set('solPcRangeHint', _pick.step === 1
    ? 'スライダーか ‹ › で始めたい手に合わせて「この手から」を押します'
    : '終わりたい手に合わせて「この手まで」を押します（始まりより前の手で押すと、そこを始まりに選び直します）');
  const ok = document.getElementById('solPcRangeOk');
  if (ok) {
    ok.textContent = _pick.step === 1 ? 'この手から' : 'この手まで';
    ok.disabled = now === 0; // 0手目（何も進めていない盤面）は範囲の端にできない
  }
  _placePanel();
}

function _onPickOk() {
  if (!_pick) return;
  const now = _now();
  if (now === 0) return;
  if (_pick.step === 1) {
    _pick.s = now;
    _pick.e = null;
    _pick.step = 2;
    _renderPick();
    return;
  }
  if (now < _pick.s) { // 始まりより前で押した: そこを始まりに選び直す
    _pick.s = now;
    _renderPick();
    return;
  }
  _pick.e = now;
  const { s, e } = _pick;
  closePcRangePicker();
  _pcRecording = { recording: false, startIdx: s - 1, endIdx: e };
  _updatePcRecordBtn();
  updatePcShareRecordingSection();
  const range = _getPcRecordingRange();
  if (range && typeof _onRangeDecided === 'function') _onRangeDecided(range);
}

function _onPickKeydown(ev) {
  if (ev.key !== 'Escape' || !_pick) return;
  ev.stopPropagation();
  closePcRangePicker();
}

/** 「✂ 範囲を選ぶ」: パネルを開く（開いていれば閉じる） */
export function togglePcRecording() {
  if (_pick) { closePcRangePicker(); return; }
  if (getLogLength() === 0) return;
  if (isPlaying()) togglePlay();
  _pick = { s: null, e: null, step: 1, cursorBefore: getCursor() };
  document.body.classList.add(PICK_CLASS); // 盤面のロック（replay-ui.js）
  const band = _el('div', 'sol-pc-range-band');
  band.id = 'solPcRangeBand';
  band.hidden = true;
  document.body.append(_buildPanel(), band);
  document.addEventListener('keydown', _onPickKeydown, true);
  window.addEventListener('resize', _placePanel);
  _updatePcRecordBtn();
  _renderPick();
}

/** パネルを閉じる（範囲は変えない）。開く前に見ていた手へ戻す */
export function closePcRangePicker() {
  if (!_pick) return;
  const before = _pick.cursorBefore;
  _pick = null;
  document.getElementById('solPcRangePanel')?.remove();
  document.getElementById('solPcRangeBand')?.remove();
  document.removeEventListener('keydown', _onPickKeydown, true);
  window.removeEventListener('resize', _placePanel);
  document.body.classList.remove(PICK_CLASS);
  if (isPlaying()) togglePlay();
  seekTo(Math.min(before, getLogLength() - 1));
  _updatePcRecordBtn();
}

/**
 * 範囲ボタンの見た目を更新する。3状態:
 *   - 待機（範囲なし）: 「✂ 範囲を選ぶ」
 *   - 選んでいる間: 「■ ここまで（N手）」（.recording。accent の枠）
 *   - 範囲が決まった後: 「範囲 s〜e手目」（.has-range）＋ 隣の ✕（範囲を外す）
 * 選んでいる間に取消（undo）や巻き戻しで現在位置が startIdx より前に戻ったら、
 * startIdx をその値にクランプする。
 */
function _updatePcRecordBtn() {
  const btn = document.getElementById('pcRecordBtn');
  const label = document.getElementById('pcRecordLabel');
  const clearBtn = document.getElementById('pcRangeClearBtn');
  if (!btn || !label) return;
  const range = _pcRecording.recording ? null : _getPcRecordingRange();
  if (_pick) {
    label.textContent = '✂ 範囲を選んでいます';
  } else if (range) {
    label.textContent = `範囲 ${range.start}〜${range.end}手目`;
  } else {
    label.textContent = '✂ 範囲を選ぶ';
  }
  btn.classList.toggle('recording', !!_pick);
  btn.classList.toggle('has-range', !!range);
  if (clearBtn) clearBtn.hidden = !range;
}

/**
 * 保存・共有シート内の範囲表示（[data-pc-range-info]、文言の動詞は data-verb）と
 * 「この範囲を再生」（[data-pc-range-play]）を更新する。3状態:
 *   - 選んでいる間: 「範囲を選んでいます（N手）…」
 *   - 範囲あり: 「s〜e手目を◯◯します」＋「この範囲を再生」
 *   - 範囲なし: 「全部の手（N手）を◯◯します」
 * replay-ui.js がシートを開く直前にも呼ぶ。
 */
export function updatePcShareRecordingSection() {
  const range = _pcRecording.recording ? null : _getPcRecordingRange();
  document.querySelectorAll('[data-pc-range-info]').forEach((el) => {
    const verb = el.dataset.verb || '保存';
    if (range) {
      el.textContent = `${range.start}〜${range.end}手目を${verb}します`;
    } else {
      el.textContent = `全部の手（${getLogLength()}手）を${verb}します`;
    }
  });
  document.querySelectorAll('[data-pc-range-play]').forEach((el) => { el.hidden = !range; });
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
 * PC版の範囲選択を初期化する（#pcRecordBtn・#pcRangeClearBtn・[data-pc-range-play] の配線と、
 * #replayCounter の変化監視）。
 * @param {{ onRangeDecided?: (range:{start:number,end:number}) => void, closeShareSheet?: () => void }} [deps]
 *   onRangeDecided: 範囲が決まったときの通知
 *   closeShareSheet: 「この範囲を再生」の直前に保存・共有シートを閉じる
 */
export function initPcRecording(deps = {}) {
  _onRangeDecided = deps.onRangeDecided;
  _closeShareSheet = deps.closeShareSheet;

  subscribeReplay('commentAdded', _shiftPcRangeAfterInsert);
  subscribeReplay('removed', _shiftPcRangeAfterRemove);

  document.getElementById('pcRecordBtn')
    ?.addEventListener('click', togglePcRecording);
  document.getElementById('pcRangeClearBtn')
    ?.addEventListener('click', () => {
      _resetPcRecording();
      updatePcShareRecordingSection();
    });
  document.querySelectorAll('[data-pc-range-play]')
    .forEach((el) => el.addEventListener('click', _playPcRecordingRange));

  // #replayCounter のテキスト変化（stepForward/stepBack/seekTo/undo 等、既存の手数更新経路）を
  // 監視し、取消で範囲が縮んだときの表示を追従させる。
  const counterEl = document.getElementById('replayCounter');
  if (counterEl) {
    new MutationObserver(() => {
      _prunePcRange();
      if (_hasStoppedPcRecording()) _updatePcRecordBtn();
    }).observe(counterEl, { childList: true, characterData: true, subtree: true });
  }
  // 選んでいる間は、手を動かすたびにパネルの「今 N手」・金色の帯を合わせる
  subscribeReplay('changed', _renderPick);
  // 記録が消えた（リセット・別のデッキの読込）ら閉じる
  document.addEventListener('sol-replay-reset', () => closePcRangePicker());
  document.addEventListener('sol-deck-load-start', () => closePcRangePicker());
}
