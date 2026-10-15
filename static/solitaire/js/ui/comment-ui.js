/**
 * comment-ui.js
 * コメント（カードの吹き出し・盤面のメモ）の入力と一覧（PC。右パネルの「コメント」区画）。
 *
 * 記録への追加・削除は replay-service の addComment / removeEventAt に任せる
 * （自動再生の停止・末尾への移動・範囲の追従はそちらと、購読側の pc-recording.js が行う）。
 * 吹き出し・メモの表示は記録の位置から計算して描く（services/comment-view.js）ので、ここでは描かない。
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
import { COMMENT_MAX_LEN, listComments } from '../services/comment-model.js';
import { isCardInPool } from './context-menu.js';
import { showToast } from '../utils/toast.js';

/** 縦向きスマホ用の差し込み口。true を返したら入力欄は出さない（mobile-ui.js が代わりに開いた） */
let _cardCommentOpener = null;

/**
 * カードの吹き出し入力を別の UI で開く処理を登録する（mobile-ui.js から）。
 * @param {(wrapper: Element) => boolean} fn  開いたら true
 */
export function setCardCommentOpener(fn) {
  _cardCommentOpener = fn;
}

// ── カード選択モード ──────────────────────────────────────
let _picking = false;

/** 選択モード中に、カードへの通常の動作（移動・詳細・効果発動・右クリック）を起こさせないために止めるイベント */
const _PICK_BLOCKED_EVENTS = ['pointerdown', 'mousedown', 'mouseup', 'dblclick', 'dragstart', 'contextmenu', 'touchstart'];

function _wrapperOf(target) {
  return target instanceof Element ? target.closest('.tier-item-wrapper') : null;
}

/** 選択モード中、カード上のイベントをその場で止める（カードの listener に届かせない） */
function _blockCardEvent(e) {
  if (!_wrapperOf(e.target)) return;
  // タッチの touchstart は preventDefault すると、その後の click（カードを選ぶ押下）まで消えるので止めるだけにする
  if (e.type !== 'touchstart') e.preventDefault();
  e.stopImmediatePropagation();
}

function _onPickClick(e) {
  const wrapper = _wrapperOf(e.target);
  if (!wrapper) return; // カード以外は何もしない（Esc か、もう一度ボタンでやめる）
  e.preventDefault();
  e.stopImmediatePropagation();
  if (isCardInPool(wrapper)) {
    showToast('デッキの中のカードには付けられません');
    return;
  }
  stopPickingCard();
  openCardCommentInput(wrapper);
}

function _onPickKeydown(e) {
  if (e.key === 'Escape') stopPickingCard();
}

function startPickingCard() {
  closeCommentInputs();
  _picking = true;
  document.getElementById('solCommentGuide')?.removeAttribute('hidden');
  document.body.classList.add('sol-comment-picking');
  document.getElementById('solCommentCardBtn')?.classList.add('is-active');
  _PICK_BLOCKED_EVENTS.forEach((t) => document.addEventListener(t, _blockCardEvent, true));
  document.addEventListener('click', _onPickClick, true);
  document.addEventListener('keydown', _onPickKeydown, true);
}

function stopPickingCard() {
  if (!_picking) return;
  _picking = false;
  document.getElementById('solCommentGuide')?.setAttribute('hidden', '');
  document.body.classList.remove('sol-comment-picking');
  document.getElementById('solCommentCardBtn')?.classList.remove('is-active');
  _PICK_BLOCKED_EVENTS.forEach((t) => document.removeEventListener(t, _blockCardEvent, true));
  document.removeEventListener('click', _onPickClick, true);
  document.removeEventListener('keydown', _onPickKeydown, true);
}

// ── 入力欄 ────────────────────────────────────────────────

/** 日本語入力の変換確定の Enter では付けない */
function _isCommitEnter(e) {
  return e.key === 'Enter' && !e.isComposing && e.keyCode !== 229;
}

let _popupEl = null;
let _popupCleanup = null;

function closeCardCommentPopup() {
  if (_popupCleanup) { _popupCleanup(); _popupCleanup = null; }
  if (_popupEl) { _popupEl.remove(); _popupEl = null; }
}

/** 開いている入力欄（カードの近く・盤面メモ）と選択モードをすべて閉じる */
function closeCommentInputs() {
  stopPickingCard();
  closeCardCommentPopup();
  const form = document.getElementById('solCommentBoardForm');
  if (form) form.hidden = true;
  document.getElementById('solCommentBoardBtn')?.classList.remove('is-active');
}

/** 入力欄をカードの近く（画面からはみ出さない位置）に置く */
function _placePopup(popup, wrapper) {
  const r = wrapper.getBoundingClientRect();
  const margin = 6;
  const w = popup.offsetWidth;
  const h = popup.offsetHeight;
  let left = Math.min(Math.max(margin, r.left), window.innerWidth - w - margin);
  let top = r.bottom + 4;
  if (top + h > window.innerHeight - margin) top = Math.max(margin, r.top - h - 4); // 下に収まらなければカードの上へ
  popup.style.left = `${left}px`;
  popup.style.top = `${top}px`;
}

/**
 * カードの吹き出しを入力する小さな欄を、そのカードの近くに出す（右クリックメニュー・カード選択モードから）。
 * デッキ内のカードには付けない。縦向きスマホでは mobile-ui.js のシートが代わりに開く。
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
    if (e.key === 'Escape') { e.preventDefault(); closeCardCommentPopup(); return; }
    if (_isCommitEnter(e)) { e.preventDefault(); submit(); }
  });

  document.body.appendChild(popup);
  _popupEl = popup;
  _placePopup(popup, wrapper);

  // 欄の外を押したら閉じる（開いた押下そのもので閉じないよう、登録は次のタスクへ）
  const onOutside = (e) => { if (!popup.contains(e.target)) closeCardCommentPopup(); };
  const tid = setTimeout(() => document.addEventListener('mousedown', onOutside, true), 0);
  _popupCleanup = () => { clearTimeout(tid); document.removeEventListener('mousedown', onOutside, true); };
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

// ── 一覧 ──────────────────────────────────────────────────

/** 区画の一覧を、いまの記録から作り直す（「N手目 💬/📝 本文」＋行末の ✕） */
function renderCommentList() {
  const ul = document.getElementById('solCommentList');
  if (!ul) return;
  const items = listComments(getLogs());
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
    text.textContent = `${c.index + 1}手目 ${c.kind === 'card' ? '💬' : '📝'} ${c.text}`;
    text.title = c.text;
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'sol-comment-del';
    del.textContent = '✕';
    del.setAttribute('aria-label', `${c.index + 1}手目のコメントを削除`);
    del.addEventListener('click', () => removeEventAt(c.index)); // 確認は出さない（コメントなので）
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

  document.getElementById('solCommentBoardForm')?.addEventListener('submit', (e) => {
    e.preventDefault();
    _submitBoardMemo();
  });
  boardInput?.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); closeCommentInputs(); return; }
    if (_isCommitEnter(e)) { e.preventDefault(); _submitBoardMemo(); }
  });

  subscribeReplay('changed', renderCommentList);
  renderCommentList();
}
