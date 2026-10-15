/**
 * comment-ui.js
 * コメント（カードの吹き出し・盤面のメモ・カットイン）の入力と一覧（PC。右パネルの「コメント」区画）。
 *
 * 記録への追加・削除は replay-service の addComment / removeEventAt に任せる
 * （自動再生の停止・末尾への移動・範囲の追従はそちらと、購読側の pc-recording.js が行う）。
 * 吹き出し・メモ・カットインの表示は services/comment-view.js が描く（吹き出し・メモは記録の位置から計算、カットインは
 * 前進でその手に来たときに replay-service が出す）ので、ここでは描かない。
 *
 * 「カードに吹き出し」の流れ:
 *   ボタン → 案内帯「コメントを付けるカードを選んでください（Escでやめる）」→ 次に押したカードが対象
 *   （この押下はカードの移動・詳細表示などの通常の動作を起こさない）→ カードの近くに小さな入力欄
 * 右クリックメニューの「吹き出しを付ける」は、カードを選ぶ段を飛ばして同じ入力欄を出す。
 * 縦向きスマホでは入力欄の代わりに mobile-ui.js のシートを使う（setCardCommentOpener で差し込まれる）。
 */

import {
  addComment,
  removeEventAt,
  getLogs,
  subscribeReplay,
} from '../services/replay-service.js';
import { COMMENT_MAX_LEN, CUTIN_MAX_LEN, CUTIN_PRESETS, listComments } from '../services/comment-model.js';
import { isCardInPool } from './context-menu.js';
import { showToast } from '../utils/toast.js';
import { isMobilePortrait } from '../utils/viewport.js';
import { hideCutin } from '../services/comment-view.js';

/** 縦向きスマホ用の差し込み口。true を返したら入力欄は出さない（mobile-ui.js が代わりに開いた） */
let _cardCommentOpener = null;

/** 縦向きスマホ用: 盤面メモの全文を下から出るシートで出す処理（mobile-ui.js から差し込む） */
let _memoFullOpener = null;
export function setMemoFullOpener(fn) {
  _memoFullOpener = fn;
}

/**
 * カードの吹き出し入力を別の UI で開く処理を登録する（mobile-ui.js から）。
 * @param {(wrapper: Element) => boolean} fn  開いたら true
 */
export function setCardCommentOpener(fn) {
  _cardCommentOpener = fn;
}

// ── カードの操作止め（選択モード中・カードの近くの入力欄が開いている間） ──
// 選んだ押下の続き（ダブルクリックの2回目など）が通常の動作（効果発動の記録・詳細表示・移動）を起こさないよう、
// 入力欄が閉じるまでカード上のイベントを止め続ける。
let _picking = false;
let _blocking = false;
let _holdTimer = null;       // 外を押して閉じたとき、その押下の続きを少しの間だけ止め続ける
let _popupOpenedAt = 0;

const _BLOCKED_EVENTS = ['pointerdown', 'mousedown', 'mouseup', 'click', 'dblclick', 'dragstart', 'contextmenu', 'touchstart'];
const _DOUBLE_CLICK_WINDOW_MS = 600;

function _wrapperOf(target) {
  return target instanceof Element ? target.closest('.tier-item-wrapper') : null;
}

function _needBlock() {
  return _picking || !!_popupEl || !!_holdTimer;
}

/** 止めが必要かを見直して、リスナーを付け外しする */
function _updateBlock() {
  const need = _needBlock();
  if (need === _blocking) return;
  _blocking = need;
  const fn = need ? 'addEventListener' : 'removeEventListener';
  _BLOCKED_EVENTS.forEach((t) => document[fn](t, _guard, true));
  document[fn]('keydown', _guardKeydown, true);
}

function _guard(e) {
  // 入力欄の外を押したら入力欄を閉じる。開いた直後（ダブルクリックの2回目）の押下では閉じない
  if (e.type === 'mousedown' && _popupEl && !_popupEl.contains(e.target)
      && Date.now() - _popupOpenedAt > _DOUBLE_CLICK_WINDOW_MS) {
    closeCardCommentPopup(_wrapperOf(e.target) ? { holdGesture: true } : undefined);
  }
  const wrapper = _wrapperOf(e.target);
  if (!wrapper) return;
  // touchstart を preventDefault すると、その後の click（カードを選ぶ押下）まで消える。pointerdown を preventDefault すると
  // 続く mousedown が出なくなる（入力欄の外を押したら閉じる判定が働かない）。この2つは止めるだけにする
  if (e.type !== 'touchstart' && e.type !== 'pointerdown') e.preventDefault();
  e.stopImmediatePropagation();
  if (e.type === 'click' && _picking) _pickCard(wrapper);
}

function _guardKeydown(e) {
  if (e.key !== 'Escape') return;
  if (_popupEl) { e.stopPropagation(); closeCardCommentPopup(); }
  else if (_picking) { e.stopPropagation(); stopPickingCard(); }
}

function _pickCard(wrapper) {
  if (isCardInPool(wrapper)) {
    showToast('デッキの中のカードには付けられません');
    return;
  }
  stopPickingCard({ keepBlock: true }); // 入力欄が閉じるまで止めは外さない（openCardCommentInput が入力欄を開く）
  openCardCommentInput(wrapper);
  _updateBlock();
}

function startPickingCard() {
  closeCommentInputs();
  _picking = true;
  document.getElementById('solCommentGuide')?.removeAttribute('hidden');
  document.body.classList.add('sol-comment-picking');
  document.getElementById('solCommentCardBtn')?.classList.add('is-active');
  _updateBlock();
}

function stopPickingCard({ keepBlock = false } = {}) {
  if (!_picking) return;
  _picking = false;
  document.getElementById('solCommentGuide')?.setAttribute('hidden', '');
  document.body.classList.remove('sol-comment-picking');
  document.getElementById('solCommentCardBtn')?.classList.remove('is-active');
  if (!keepBlock) _updateBlock();
}

// ── 入力欄 ────────────────────────────────────────────────

/** 日本語入力の変換確定の Enter では付けない */
function _isCommitEnter(e) {
  return e.key === 'Enter' && !e.isComposing && e.keyCode !== 229;
}

let _popupEl = null;

/**
 * カードの近くの入力欄を閉じる。
 * @param {{holdGesture?: boolean}} [opts] holdGesture: カードを押して閉じたとき。その押下の続き（ダブルクリックなど）が
 *   カードに届かないよう、少しの間だけ止めを残す
 */
function closeCardCommentPopup({ holdGesture = false } = {}) {
  if (_popupEl) { _popupEl.remove(); _popupEl = null; }
  if (holdGesture) {
    clearTimeout(_holdTimer);
    _holdTimer = setTimeout(() => { _holdTimer = null; _updateBlock(); }, _DOUBLE_CLICK_WINDOW_MS);
  }
  _updateBlock();
}

/** 開いている入力欄（カードの近く・盤面メモ）と選択モードをすべて閉じる */
function closeCommentInputs() {
  stopPickingCard();
  closeCardCommentPopup();
  const form = document.getElementById('solCommentBoardForm');
  if (form) form.hidden = true;
  document.getElementById('solCommentBoardBtn')?.classList.remove('is-active');
  const cutinForm = document.getElementById('solCommentCutinForm');
  if (cutinForm) cutinForm.hidden = true;
  document.getElementById('solCommentCutinBtn')?.classList.remove('is-active');
}

/** 入力欄をカードの近く（画面からはみ出さない位置）に置く */
function _placePopup(popup, wrapper) {
  const r = wrapper.getBoundingClientRect();
  const margin = 6;
  const w = popup.offsetWidth;
  const h = popup.offsetHeight;
  const left = Math.min(Math.max(margin, r.left), window.innerWidth - w - margin);
  let top = r.bottom + 4;
  if (top + h > window.innerHeight - margin) top = Math.max(margin, r.top - h - 4); // 下に収まらなければカードの上へ
  popup.style.left = `${left}px`;
  popup.style.top = `${top}px`;
}

/**
 * カードの吹き出しを入力する小さな欄を、そのカードの近くに出す（右クリックメニュー・カード選択モードから）。
 * デッキ内のカードには付けない（判定は今の盤面＝カーソル位置のもの）。縦向きスマホでは mobile-ui.js のシートが代わりに開く。
 * 欄が開いている間は、カードの操作（移動・詳細・効果発動など）を止める。
 * @param {Element} wrapper  .tier-item-wrapper
 */
export function openCardCommentInput(wrapper) {
  if (!wrapper || isCardInPool(wrapper)) return;
  const cardEl = wrapper.querySelector('.tier-item');
  if (!cardEl?.id) return;
  if (_cardCommentOpener && _cardCommentOpener(wrapper)) return;

  closeCommentInputs();
  const cardId = cardEl.id;

  const popup = document.createElement('form');
  popup.className = 'sol-comment-popup';
  popup.setAttribute('role', 'dialog');
  popup.setAttribute('aria-label', '吹き出しを付ける');
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'sol-comment-input';
  input.maxLength = COMMENT_MAX_LEN;
  input.placeholder = 'コメント';
  input.setAttribute('aria-label', '吹き出しのコメント');
  const btn = document.createElement('button');
  btn.type = 'submit';
  btn.className = 'sol-comment-btn sol-comment-submit';
  btn.textContent = '付ける';
  popup.append(input, btn);

  const submit = () => {
    if (!addComment({ kind: 'card', cardId, text: input.value })) { input.focus(); return; }
    closeCardCommentPopup();
  };
  popup.addEventListener('submit', (e) => { e.preventDefault(); submit(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeCardCommentPopup(); return; }
    if (_isCommitEnter(e)) { e.preventDefault(); submit(); }
  });

  document.body.appendChild(popup);
  _popupEl = popup;
  _popupOpenedAt = Date.now();
  _placePopup(popup, wrapper);
  _updateBlock();
  setTimeout(() => input.focus(), 0); // 右クリックメニューが閉じて消えた後にフォーカスを取る
}

// ── 盤面メモ（右パネル区画の中の入力欄） ──────────────────────
function _toggleBoardMemoForm() {
  const form = document.getElementById('solCommentBoardForm');
  if (!form) return;
  const open = form.hidden;
  closeCommentInputs();
  if (!open) return;
  form.hidden = false;
  document.getElementById('solCommentBoardBtn')?.classList.add('is-active');
  document.getElementById('solCommentBoardInput')?.focus();
}

function _submitBoardMemo() {
  const input = document.getElementById('solCommentBoardInput');
  if (!input) return;
  if (!addComment({ kind: 'board', text: input.value })) { input.focus(); return; }
  input.value = '';
  closeCommentInputs();
}

// ── カットイン（右パネル区画の中の、言葉の札＋自由入力） ──────────
function _toggleCutinForm() {
  const form = document.getElementById('solCommentCutinForm');
  if (!form) return;
  const open = form.hidden;
  closeCommentInputs();
  if (!open) return;
  form.hidden = false;
  document.getElementById('solCommentCutinBtn')?.classList.add('is-active'); // 開いている間は「入力中」と同じ accent の面
  document.getElementById('solCommentCutinInput')?.focus();
}

/** カットインを入れる。入れたら入力欄を閉じる（札は押すとその場で入る） */
function _addCutin(text) {
  if (!addComment({ kind: 'cutin', text })) return false;
  const input = document.getElementById('solCommentCutinInput');
  if (input) input.value = '';
  closeCommentInputs();
  return true;
}

// ── 一覧 ──────────────────────────────────────────────────

/** 区画の一覧を、いまの記録から作り直す（「N手目 💬/📝 本文」＋行末の ✕） */
function renderCommentList() {
  const ul = document.getElementById('solCommentList');
  if (!ul) return;
  const items = listComments(getLogs());
  const KIND_ICON = { card: '💬', board: '📝', cutin: '🎬' };
  ul.replaceChildren();
  if (items.length === 0) {
    const li = document.createElement('li');
    li.className = 'sol-comment-empty';
    li.textContent = 'まだコメントはありません';
    ul.appendChild(li);
    return;
  }
  for (const c of items) {
    const li = document.createElement('li');
    li.className = 'sol-comment-row';
    const text = document.createElement('span');
    text.className = 'sol-comment-row-text';
    text.textContent = `${c.index + 1}手目 ${KIND_ICON[c.kind] ?? '📝'} ${c.text}`;
    text.title = c.text;
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'sol-comment-del';
    del.textContent = '✕';
    del.setAttribute('aria-label', `${c.index + 1}手目のコメントを削除`);
    // 確認は出さない（コメントなので）。一覧の行の内容も渡し、記録の同じ位置が別のコメントに変わっていたら消さない
    del.addEventListener('click', () => removeEventAt(c.index, { kind: c.kind, cardId: c.cardId, text: c.text }));
    li.append(text, del);
    ul.appendChild(li);
  }
}

// ── 初期化 ────────────────────────────────────────────────

/** 右パネル「コメント」区画の配線。replay-ui.js の initReplayUI から呼ぶ */
export function initCommentUI() {
  const boardInput = document.getElementById('solCommentBoardInput');
  if (boardInput) boardInput.maxLength = COMMENT_MAX_LEN;

  document.getElementById('solCommentCardBtn')?.addEventListener('click', () => {
    if (_picking) stopPickingCard();
    else startPickingCard();
  });
  document.getElementById('solCommentBoardBtn')?.addEventListener('click', _toggleBoardMemoForm);

  // カットイン: 言葉の札（押すとその場で入る）・自由入力（20字）・［出す］
  document.getElementById('solCommentCutinBtn')?.addEventListener('click', _toggleCutinForm);
  const cutinInput = document.getElementById('solCommentCutinInput');
  if (cutinInput) cutinInput.maxLength = CUTIN_MAX_LEN;
  const chips = document.getElementById('solCommentCutinChips');
  if (chips) {
    for (const label of CUTIN_PRESETS) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'sol-cutin-chip';
      chip.textContent = label;
      chip.addEventListener('click', () => _addCutin(label));
      chips.appendChild(chip);
    }
  }
  document.getElementById('solCommentCutinInputRow')?.addEventListener('submit', (e) => {
    e.preventDefault();
    if (!_addCutin(cutinInput?.value ?? '')) cutinInput?.focus();
  });
  cutinInput?.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeCommentInputs(); return; }
    if (e.key === 'Enter') {
      e.preventDefault(); // 日本語入力の変換確定の Enter でフォームが送られないよう、既定の送信は止める
      if (_isCommitEnter(e) && !_addCutin(cutinInput.value)) cutinInput.focus();
    }
  });

  document.getElementById('solCommentBoardForm')?.addEventListener('submit', (e) => {
    e.preventDefault();
    _submitBoardMemo();
  });
  boardInput?.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeCommentInputs(); return; }
    if (_isCommitEnter(e)) { e.preventDefault(); _submitBoardMemo(); }
  });

  // 盤面メモの付箋: 押す（クリック・Enter・Space）と全文が開閉する（同じ付箋が広がって重なるだけで、盤面の配置は動かない）
  const memo = document.getElementById('solBoardMemo');
  const closeMemo = () => {
    memo?.classList.remove('is-open');
    memo?.setAttribute('aria-expanded', 'false');
  };
  const toggleMemo = () => {
    // 縦向きスマホは付箋を広げず、全文を下から出るシートで出す（見本の「押すと全文を下から出す」）
    if (isMobilePortrait() && _memoFullOpener) { _memoFullOpener(memo.querySelector('.sol-board-memo-text')?.textContent ?? ''); return; }
    const open = !memo.classList.contains('is-open');
    memo.classList.toggle('is-open', open);
    memo.setAttribute('aria-expanded', String(open));
  };
  memo?.addEventListener('click', toggleMemo);
  memo?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleMemo(); }
  });

  // 全文を開いたまま、下のカードをドラッグし始めたら閉じる（広がった付箋がドロップ先を覆わないように）
  document.addEventListener('dragstart', () => {
    if (memo?.classList.contains('is-open')) closeMemo();
  }, true);
  // 開いた付箋は、外を押したら閉じる
  document.addEventListener('pointerdown', (e) => {
    if (memo?.classList.contains('is-open') && !memo.contains(e.target)) closeMemo();
  }, true);
  // 画面サイズの変更・回転で、出ているカットインの帯は消す（位置が古くなるため）
  window.addEventListener('resize', hideCutin);
  window.addEventListener('orientationchange', hideCutin);

  subscribeReplay('changed', renderCommentList);
  renderCommentList();
}
