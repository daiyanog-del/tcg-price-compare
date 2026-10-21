/**
 * inline-ui.js — ブラウザ標準の窓（prompt / confirm / alert）の代わりに使う、ページ内の小さな部品。
 * 2026-10-21 ユーザーがモックを見て承認（モック _mocks/sim-refresh-20261021 の A）。マイデッキ（static/mydeck/deck-head.js）と同じ形。
 *
 *   - showActionToast(msg, opts) … 画面下のお知らせ。ボタン（「元に戻す」「リンクを開く」など）を1つ付けられる
 *   - openNameDialog(anchor, opts) … 名前を入れる小窓（空欄・重複はその場で知らせる）
 *   - openConfirm(anchor, opts)   … 取り消せない操作の確認の小窓（サーバー側の操作など、元に戻せないものだけに使う）
 *
 * シミュレーター本体とリプレイ一覧ページの両方から使う。見た目は css/inline-ui.css（両方の画面で読み込む）。
 * シミュレーターには `span { color:#333 }` の一律指定があるので、文字色は CSS で必ず明示する。
 * ユーザー由来の文字列（名前・題名）は textContent で入れる。
 */

const TOAST_MS = 6000; // ボタン付きのお知らせを出している時間（マイデッキの取り消しと同じ）
const PLAIN_MS = 3500;

let _toast = null; // { el, timer, onExpire }

/** 出ているボタン付きのお知らせを閉じる（expired=true なら期限切れの後始末を走らせる） */
export function dismissActionToast(expired = true) {
  _closeToast(expired);
}

function _closeToast(expired) {
  if (!_toast) return;
  const t = _toast;
  _toast = null;
  clearTimeout(t.timer);
  t.el.remove();
  if (expired && t.onExpire) { try { t.onExpire(); } catch (e) { console.warn('[inline-ui] お知らせの後始末に失敗しました', e); } }
}

/**
 * 画面下のお知らせ。新しいお知らせを出すと前のものは消える（前のボタン付きは期限切れ扱い＝onExpire が走る）。
 * @param {string} msg
 * @param {{label?:string, onClick?:Function, onExpire?:Function, ms?:number, error?:boolean}} [opts]
 */
export function showActionToast(msg, opts = {}) {
  _closeToast(true);
  // 既存の短いお知らせ（utils/toast.js の .sol-toast）と同じ位置に重ならないよう、出ているものは消す
  // （取り消し付きの .sol-toast-action は消さない。そちらの取り消しが押せなくなるため）
  document.querySelectorAll('.sol-toast:not(.sol-toast-action)').forEach(t => t.remove());
  const el = document.createElement('div');
  el.className = 'sol-inline-toast' + (opts.error ? ' is-error' : '');
  el.setAttribute('role', opts.error ? 'alert' : 'status');
  const text = document.createElement('span');
  text.className = 'sol-inline-toast-msg';
  text.textContent = msg;
  el.appendChild(text);
  const me = { el, onExpire: opts.onExpire || null, timer: 0 };
  if (opts.label && typeof opts.onClick === 'function') {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'sol-inline-toast-btn';
    b.textContent = opts.label;
    b.addEventListener('click', () => {
      if (_toast !== me) return;
      me.onExpire = null; // 押したら期限切れの後始末はしない
      _closeToast(false);
      opts.onClick();
    });
    el.appendChild(b);
  }
  document.body.appendChild(el);
  _toast = me;
  me.timer = setTimeout(() => { if (_toast === me) _closeToast(true); }, opts.ms || (opts.label ? TOAST_MS : PLAIN_MS));
}

// ── 浮かせる小窓（名前・確認） ──
let _pop = null; // { el, anchor, cleanup }

function _closePop(restoreFocus) {
  if (!_pop) return;
  const p = _pop;
  _pop = null;
  p.cleanup();
  if (p.onClose) { try { p.onClose(); } catch (e) { /* 何もしない */ } }
  p.el.remove();
  if (restoreFocus && p.anchor && document.body.contains(p.anchor)) {
    try { p.anchor.focus({ preventScroll: true }); } catch (e) { /* 何もしない */ }
  }
}

/** 起点の下に置く（下に入らなければ上、横は画面の中に寄せる）。起点が無い・隠れているときは画面の中央 */
function _place(el, anchor) {
  const m = 8;
  const vw = document.documentElement.clientWidth;
  const vh = window.innerHeight;
  const w = el.offsetWidth;
  const h = el.offsetHeight;
  const r = anchor && anchor.isConnected ? anchor.getBoundingClientRect() : null;
  if (!r || (!r.width && !r.height)) {
    el.style.left = Math.max(m, (vw - w) / 2) + 'px';
    el.style.top = Math.max(m, (vh - h) / 3) + 'px';
    return;
  }
  const left = Math.min(Math.max(m, r.left), Math.max(m, vw - w - m));
  let top = r.bottom + 6;
  if (top + h > vh - m) {
    const above = r.top - 6 - h;
    top = above >= m ? above : Math.max(m, vh - h - m);
  }
  el.style.left = left + 'px';
  el.style.top = top + 'px';
}

function _openPop(el, anchor) {
  _closePop(false);
  document.body.appendChild(el);
  _place(el, anchor);
  const onDown = (e) => { if (!el.contains(e.target) && !(anchor && anchor.contains && anchor.contains(e.target))) _closePop(false); };
  const onKey = (e) => {
    if (e.key !== 'Escape') return;
    // ほかの窓（右クリックメニュー・シート）より先に受けて、この小窓だけを閉じる
    e.preventDefault();
    e.stopImmediatePropagation();
    _closePop(true);
  };
  const onMove = (e) => { if (e && e.type === 'scroll' && el.contains(e.target)) return; _place(el, anchor); };
  document.addEventListener('pointerdown', onDown, true);
  document.addEventListener('keydown', onKey, true);
  window.addEventListener('scroll', onMove, true);
  window.addEventListener('resize', onMove);
  _pop = {
    el, anchor, cleanup: () => {
      document.removeEventListener('pointerdown', onDown, true);
      document.removeEventListener('keydown', onKey, true);
      window.removeEventListener('scroll', onMove, true);
      window.removeEventListener('resize', onMove);
    },
  };
}

function _pill(label, primary, danger) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'sol-inline-pill' + (primary ? ' is-primary' : '') + (danger ? ' is-danger' : '');
  b.textContent = label;
  return b;
}

/**
 * 名前を入れる小窓。onOk(name) が文字列を返したら、その文を出して閉じない（重複など）。
 * @param {Element|null} anchor
 * @param {{title:string, okLabel:string, initial?:string, maxLength?:number, onOk:(name:string)=>(string|void)}} opts
 */
export function openNameDialog(anchor, opts) {
  const el = document.createElement('div');
  el.className = 'sol-inline-pop sol-inline-name';
  el.setAttribute('role', 'dialog');
  el.setAttribute('aria-label', opts.title);
  const form = document.createElement('form');
  const label = document.createElement('label');
  label.className = 'sol-inline-label';
  const cap = document.createElement('span');
  cap.className = 'sol-inline-label-text';
  cap.textContent = opts.title;
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'sol-inline-input';
  input.maxLength = opts.maxLength || 60;
  input.autocomplete = 'off';
  input.value = opts.initial || '';
  label.append(cap, input);
  const err = document.createElement('p');
  err.className = 'sol-inline-err';
  err.setAttribute('role', 'alert');
  err.hidden = true;
  const row = document.createElement('div');
  row.className = 'sol-inline-row';
  const cancel = _pill('やめる');
  cancel.addEventListener('click', () => _closePop(true));
  const ok = _pill(opts.okLabel, true);
  ok.type = 'submit';
  row.append(cancel, ok);
  form.append(label, err, row);
  el.appendChild(form);
  input.addEventListener('input', () => { err.hidden = true; });
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const n = input.value.trim().slice(0, input.maxLength);
    if (!n) { err.textContent = '名前を入れてください'; err.hidden = false; input.focus(); return; }
    const msg = opts.onOk(n);
    if (typeof msg === 'string') { err.textContent = msg; err.hidden = false; input.focus(); input.select(); return; }
    _closePop(true);
  });
  _openPop(el, anchor);
  input.focus();
  input.select();
}

/**
 * 確認の小窓（元に戻せない操作だけに使う。戻せる操作は「すぐ実行して元に戻すを出す」にする）。
 * @param {Element|null} anchor
 * @param {{message:string, okLabel:string, cancelLabel?:string, danger?:boolean, onOk:Function, onCancel?:Function}} opts
 *   onCancel … やめる・Esc・外を押して閉じたとき
 */
export function openConfirm(anchor, opts) {
  let decided = false;
  const el = document.createElement('div');
  el.className = 'sol-inline-pop sol-inline-confirm';
  el.setAttribute('role', 'alertdialog');
  const p = document.createElement('p');
  p.className = 'sol-inline-msg';
  p.textContent = opts.message;
  const row = document.createElement('div');
  row.className = 'sol-inline-row';
  const cancel = _pill(opts.cancelLabel || 'やめる');
  cancel.addEventListener('click', () => _closePop(true));
  const ok = _pill(opts.okLabel, !opts.danger, !!opts.danger);
  ok.addEventListener('click', () => { decided = true; _closePop(true); opts.onOk(); });
  row.append(cancel, ok);
  el.append(p, row);
  el.setAttribute('aria-label', opts.message);
  _openPop(el, anchor);
  if (_pop) _pop.onClose = () => { if (!decided && typeof opts.onCancel === 'function') opts.onCancel(); };
  cancel.focus();
}

/**
 * 確認の小窓を開き、選んだ結果を返す（true＝実行）。
 * @returns {Promise<boolean>}
 */
export function confirmAsync(anchor, message, okLabel, danger = true) {
  return new Promise((resolve) => {
    openConfirm(anchor, { message, okLabel, danger, onOk: () => resolve(true), onCancel: () => resolve(false) });
  });
}
