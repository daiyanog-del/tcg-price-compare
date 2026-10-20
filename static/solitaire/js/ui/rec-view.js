/**
 * rec-view.js
 * 画面録画用の表示（2026-10-20 ユーザー承認: PC は案A＝上に帯、スマホは案C＝同じ帯を縦長の再生画面に載せる。
 * モック _mocks/rec-mode-20261020/rec-mode-mock.html）。
 *
 * 動画の書き出しはしない。操作ボタン類を隠し、上の帯（ロゴ・題名・手数）と下端の進み具合の線だけにして
 * 最初の手から自動再生する。利用者は端末の画面録画（iPhone の画面収録・Windows の Win+Alt+R 等）で撮る。
 *
 * - 再生の仕組みは replay-service.js のもの（seekTo・togglePlay）をそのまま使う。
 * - 縦向きスマホはスマホの再生モード（mobile-ui.js）に入ってから下部バーを隠す。終えても再生モードのまま戻る。
 * - PC は「始める」を押したときに全画面にする（アドレスバーが映らないように。失敗しても続ける）。
 * - 録画に映らないよう、操作（もう一度・終わる）は画面に触れた／マウスを動かしたときだけ少しの間出す。
 * - 題名は共有リンク・保存したリプレイの題名か、保存・共有シートに入力した題名。無ければ出さない。
 *   他人が書いた題名なので textContent で入れる。
 * - 計測: GA4 に recview_open（表示を開いた）／recview_start（再生を始めた）を送る。
 */

import {
  getLogLength,
  getSetupLogs,
  getCursor,
  seekTo,
  togglePlay,
  isPlaying,
  subscribeReplay,
} from '../services/replay-service.js';
import { isMobilePortrait } from '../utils/viewport.js';
import { showToast } from '../utils/toast.js';

const ROOT_CLASS = 'sol-recview';
const COUNTDOWN_FROM = 3;
const COUNTDOWN_STEP_MS = 700;
/** 操作を出してから自動で隠すまで */
const CONTROLS_HIDE_MS = 2500;
/** 縦向きスマホで下部バーの代わりに残す余白（進み具合の線の分） */
const MOBILE_BOTTOM_RESERVE_PX = 4;

let _active = false;
let _enterMobilePlayback = null; // mobile-ui.js から受け取る（import の循環を作らないため）
let _loadedTitle = '';           // 共有リンク・保存したリプレイを開いたときの題名
let _countdownTimer = null;
let _controlsTimer = null;
let _enteredFullscreen = false;
let _savedBottomReserve = null;
let _cursorBeforeEnter = null; // PC: 開く前の手。閉じるときにここへ戻す（0手目のまま操作すると先の記録が消えるため）
let _deckObserver = null;      // デッキ列（#poolRow）の中身の変化を見て「デッキ N」を数え直す
let _rowObserver = null;
let _wakeLock = null;          // 画面を消さない（Screen Wake Lock）。表示の間だけ持ち、閉じたら手放す       // 盤面・手札/EX の行の大きさの変化を見て、手札/EX の境目を合わせ直す
let _enteredPortrait = false;  // 縦向きスマホで開いたか（スマホは再生モードのまま戻るので手は戻さない）

function _ga(name, params) {
  try {
    if (typeof window.gtag === 'function') window.gtag('event', name, params || {});
  } catch (_) { /* 計測の失敗で表示を止めない */ }
}

function _title() {
  for (const el of document.querySelectorAll('[data-replay-title]')) {
    const v = el.value?.trim();
    if (v) return v;
  }
  return _loadedTitle;
}

function _logoSrc() {
  return document.querySelector('.nav-brand-image')?.getAttribute('src') || '/static/brand/wordmark-lockup.svg';
}

function _el(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text != null) el.textContent = text;
  return el;
}

/** 帯の手数と下端の線を今の位置に合わせる */
function _syncProgress() {
  if (!_active) return;
  const total = getLogLength();
  const done = Math.max(0, Math.min(total, getCursor() + 1));
  const badge = document.getElementById('solRecCount');
  if (badge) badge.textContent = `${done} / ${total}手`;
  const bar = document.getElementById('solRecProgress');
  if (bar) bar.style.width = total > 0 ? `${(done / total) * 100}%` : '0%';
  const deck = document.getElementById('solRecDeckCount');
  if (deck) deck.textContent = String(document.querySelectorAll('#poolRow .tier-item-wrapper').length);
}

function _buildBand() {
  const band = _el('div', 'sol-rec-band');
  band.id = 'solRecBand';
  const logo = _el('img', 'sol-rec-logo');
  logo.src = _logoSrc();
  logo.alt = 'GREEDY LAB — 強欲なラボ';
  const title = _el('div', 'sol-rec-title', _title());
  const count = _el('div', 'sol-rec-count');
  count.id = 'solRecCount';
  // デッキの残り枚数。デッキ列を隠すので、ドローなどの演出はこの札から飛ぶ（replay-service.js _getDeckAnchorEl）
  const deck = _el('div', 'sol-rec-deck', 'デッキ ');
  deck.id = 'solRecDeck';
  const deckCount = _el('span', 'sol-rec-deck-count');
  deckCount.id = 'solRecDeckCount';
  deck.append(deckCount);
  band.append(logo, title, deck, count);
  return band;
}

/** 開いた直後の案内（録画のやり方と「始める」） */
function _buildGuide() {
  const wrap = _el('div', 'sol-replay-start sol-rec-guide');
  wrap.id = 'solRecGuide';
  const backdrop = _el('div', 'sol-replay-start-backdrop');
  const card = _el('div', 'sol-replay-start-card');
  card.setAttribute('role', 'dialog');
  card.setAttribute('aria-modal', 'true');
  card.append(
    _el('p', 'sol-replay-start-label', '画面録画用の表示'),
    _el('p', 'sol-replay-start-title', '端末の画面録画で動画にできます'),
  );
  const steps = _el('ol', 'sol-rec-guide-steps');
  [
    '先に端末の画面録画を始めます（iPhone: コントロールセンターの画面収録／Android: クイック設定のスクリーンレコード／Windows: Win+Alt+R／Mac: Shift+⌘+5）',
    '「始める」を押すと、3秒数えてから最初の手から再生します',
    '画面に触れる（マウスを動かす）と「もう一度」「終わる」が出ます。録画中は触らずにおくときれいに撮れます',
    // 画面を消さない機能（_keepScreenOn）が使えない端末では、自動ロックで画面が消えて録画が止まることがある
    ...('wakeLock' in navigator ? [] : ['この端末では途中で画面が消えることがあります。端末の自動ロックを長め（または「なし」）にしてから撮ってください']),
  ].forEach((t) => steps.append(_el('li', '', t)));
  card.append(steps);
  const start = _el('button', 'sol-replay-start-play', '始める');
  start.type = 'button';
  start.addEventListener('click', _start);
  const cancel = _el('button', 'sol-replay-start-later', 'やめる');
  cancel.type = 'button';
  cancel.addEventListener('click', exitRecView);
  card.append(start, cancel);
  wrap.append(backdrop, card);
  return wrap;
}

function _buildControls() {
  const box = _el('div', 'sol-rec-controls');
  box.id = 'solRecControls';
  box.hidden = true;
  const again = _el('button', 'sol-rec-ctl', '↻ もう一度');
  again.type = 'button';
  again.addEventListener('click', () => { _hideControls(); _restart(); });
  const end = _el('button', 'sol-rec-ctl', '✕ 終わる');
  end.type = 'button';
  end.addEventListener('click', exitRecView);
  box.append(again, end);
  return box;
}

function _showControls() {
  if (!_active || document.getElementById('solRecGuide')) return;
  const box = document.getElementById('solRecControls');
  if (!box) return;
  box.hidden = false;
  clearTimeout(_controlsTimer);
  _controlsTimer = setTimeout(_hideControls, CONTROLS_HIDE_MS);
}

function _hideControls() {
  clearTimeout(_controlsTimer);
  _controlsTimer = null;
  const box = document.getElementById('solRecControls');
  if (box) box.hidden = true;
}

function _onPointer(ev) {
  if (ev.target.closest?.('#solRecControls')) return;
  _showControls();
}

function _onKeydown(ev) {
  if (ev.key !== 'Escape') return;
  ev.stopPropagation();
  exitRecView();
}

function _stopPlayback() {
  if (isPlaying()) togglePlay();
}

/** 最初の手の前へ戻す（共有リンクの初期配置＝setup はそのまま残る） */
function _rewind() {
  _stopPlayback();
  seekTo(-1);
  _syncProgress();
}

function _clearCountdown() {
  clearTimeout(_countdownTimer);
  _countdownTimer = null;
  document.getElementById('solRecCountdown')?.remove();
}

function _countdownThenPlay() {
  _clearCountdown();
  const num = _el('div', 'sol-rec-countdown');
  num.id = 'solRecCountdown';
  document.body.appendChild(num);
  let n = COUNTDOWN_FROM;
  const tick = () => {
    if (!_active) return;
    if (n <= 0) {
      _clearCountdown();
      if (!isPlaying()) togglePlay();
      return;
    }
    num.textContent = String(n);
    n -= 1;
    _countdownTimer = setTimeout(tick, COUNTDOWN_STEP_MS);
  };
  tick();
}

/**
 * 表示の間は画面を消さない（2026-10-20 ユーザー報告: スマホで録画中に画面が暗くなり録画が止まった）。
 * 録画中は画面に触れないので、端末の自動ロックが働いて画面が消える（iPhone は画面がロックされると画面収録も止まる。
 * 未検証の前提: 報告の現象はこれによるもの）。Screen Wake Lock に対応していない端末では何もしない
 * （案内に自動ロックの設定を書く。_buildGuide）。ページが裏に回ると手放されるので、戻ったときに取り直す（initRecView）。
 */
async function _keepScreenOn() {
  if (!_active || _wakeLock || !('wakeLock' in navigator) || document.visibilityState !== 'visible') return;
  try {
    const lock = await navigator.wakeLock.request('screen');
    if (!_active) { lock.release().catch(() => {}); return; } // 取れる前に閉じられた
    _wakeLock = lock;
    lock.addEventListener('release', () => { if (_wakeLock === lock) _wakeLock = null; });
  } catch (_) {
    _wakeLock = null; // 断られても（省電力モード等）表示は続ける
  }
}

function _releaseScreenOn() {
  const lock = _wakeLock;
  _wakeLock = null;
  lock?.release().catch(() => {});
}

async function _requestFullscreen() {
  if (isMobilePortrait()) return; // 縦向きスマホはページの全画面に対応しない端末が多い（iPhone の Safari 等）
  const el = document.documentElement;
  if (document.fullscreenElement || typeof el.requestFullscreen !== 'function') return;
  try {
    await el.requestFullscreen();
    _enteredFullscreen = true;
  } catch (_) {
    _enteredFullscreen = false; // 断られても表示はそのまま続ける
  }
}

function _start() {
  document.getElementById('solRecGuide')?.remove();
  _keepScreenOn(); // 開いたときに取れていなくても、押した操作の中でもう一度頼む
  _requestFullscreen(); // 押した操作の中で呼ぶ（全画面はユーザー操作が必要）
  _rewind();
  _ga('recview_start', { steps: getLogLength() });
  _countdownThenPlay();
}

function _restart() {
  _rewind();
  _countdownThenPlay();
}

function _setMobileReserve(on) {
  const root = document.documentElement.style;
  if (on) {
    _savedBottomReserve = root.getPropertyValue('--sol-mobile-bottom-reserve');
    root.setProperty('--sol-mobile-bottom-reserve', `${MOBILE_BOTTOM_RESERVE_PX}px`);
  } else if (_savedBottomReserve !== null) {
    if (_savedBottomReserve) root.setProperty('--sol-mobile-bottom-reserve', _savedBottomReserve);
    else root.removeProperty('--sol-mobile-bottom-reserve');
    _savedBottomReserve = null;
  }
}

/** 画面録画用の表示を開く（PC のリプレイバー・スマホの⋯メニューから） */
export function enterRecView() {
  if (_active) return;
  if (getLogLength() === 0) {
    showToast(getSetupLogs().length ? '再生する手がありません' : '記録がありません');
    return;
  }
  // 共有リンクを開いた直後の「▶ 再生する」案内が残っていれば閉じる（replay-start-prompt.js が受ける）
  document.dispatchEvent(new CustomEvent('sol-recview-open'));
  _enteredPortrait = isMobilePortrait();
  if (_enteredPortrait && _enterMobilePlayback) _enterMobilePlayback();

  _cursorBeforeEnter = getCursor();
  _active = true;
  // 盤面のロック（PC は replay-ui.js がこのクラスを見て再生中と同じく盤面を触れなくする）
  document.body.classList.add(ROOT_CLASS);
  _rewind();
  if (_enteredPortrait) _setMobileReserve(true);
  document.body.append(_buildBand(), _el('div', 'sol-rec-progress'), _buildControls(), _buildGuide());
  document.querySelector('.sol-rec-progress').id = 'solRecProgress';
  _syncProgress();
  // 山札へ戻す手は飛び終えてからデッキ列に入るので、手の切り替わり（changed）だけでは数え遅れる
  const pool = document.getElementById('poolRow');
  if (pool) {
    _deckObserver = new MutationObserver(_syncProgress);
    _deckObserver.observe(pool, { childList: true });
  }
  // 盤面の大きさが決まり直すたび（開いた直後・全画面・ウィンドウの大きさの変更）に境目を合わせ直す
  if (typeof ResizeObserver === 'function') {
    _rowObserver = new ResizeObserver(() => requestAnimationFrame(_alignHandExLine));
    ['.custom-layout', '.center-row'].forEach((s) => { const el = document.querySelector(s); if (el) _rowObserver.observe(el); });
  }
  document.addEventListener('keydown', _onKeydown, true);
  document.addEventListener('pointerdown', _onPointer, true);
  document.addEventListener('mousemove', _onPointer, true);
  window.dispatchEvent(new Event('resize')); // 盤面を広げ直す（main.js fitFieldToViewport）
  _keepScreenOn();
  _ga('recview_open', { steps: getLogLength() });
}

/**
 * PC: 手札と EX の境目（縦の線）を、真上の盤面の列の境目に合わせる（2026-10-20 ユーザー指摘）。
 * 手札と EX の幅は「手札＝カード6枚分、EX＝残り」の割合で決まり盤面の列と無関係なので、画面幅によって
 * 境目が真上の境目から数px〜数十pxずれる。一番近い盤面の境目（マスどうし・盤面と墓地・墓地と除外の隙間）の
 * 中心に、手札と EX の隙間の中心を合わせる（隙間の幅は盤面 --gap と手札/EX で違うので、両端ではなく中心をそろえる）。
 * 手札の幅だけを変え、EX は残りに広がる（今の flex の作りのまま）。
 */
function _alignHandExLine() {
  const hand = document.querySelector('.sol-hand-area');
  const ex = document.getElementById('imagePool2');
  if (!hand || !ex) return;
  hand.style.removeProperty('flex');
  if (!_active || isMobilePortrait()) return;
  const cols = [...document.querySelectorAll('.custom-slot, .side-slots-container > .sol-side-area')]
    .map((e) => e.getBoundingClientRect()).filter((b) => b.width > 0);
  const rights = [...new Set(cols.map((b) => Math.round(b.right)))].sort((a, b) => a - b);
  const lefts = [...new Set(cols.map((b) => Math.round(b.left)))].sort((a, b) => a - b);
  // 隙間 = ある列の右端と、その右にある一番近い列の左端
  const gaps = rights.map((r) => { const l = lefts.find((x) => x > r); return l === undefined ? null : (r + l) / 2; })
    .filter((g) => g !== null);
  const hb = hand.getBoundingClientRect();
  const eb = ex.getBoundingClientRect();
  if (!gaps.length || !hb.width || !eb.width) return;
  const line = (hb.right + eb.left) / 2;
  const target = gaps.reduce((a, g) => (Math.abs(g - line) < Math.abs(a - line) ? g : a), gaps[0]);
  // flex-basis は内側の幅で効く（content-box）ので、余白・枠の分を引いて指定する
  const cs = getComputedStyle(hand);
  const extra = cs.boxSizing === 'border-box' ? 0
    : ['paddingLeft', 'paddingRight', 'borderLeftWidth', 'borderRightWidth'].reduce((sum, k) => sum + (parseFloat(cs[k]) || 0), 0);
  const w = hb.width + (target - line) - extra;
  if (w > 0) hand.style.flex = `0 0 ${w}px`;
}

/** 画面録画用の表示を閉じる。盤面は今の手のまま（縦向きスマホは再生モードに戻る） */
export function exitRecView() {
  if (!_active) return;
  _active = false;
  _clearCountdown();
  _hideControls();
  _stopPlayback();
  // PC は開く前の手へ戻す（途中の手のまま通常の画面に戻ると、次の操作で先の記録が消える）
  if (!_enteredPortrait && _cursorBeforeEnter !== null) seekTo(_cursorBeforeEnter);
  _cursorBeforeEnter = null;
  ['solRecBand', 'solRecProgress', 'solRecControls', 'solRecGuide'].forEach((id) => document.getElementById(id)?.remove());
  _deckObserver?.disconnect();
  _deckObserver = null;
  _rowObserver?.disconnect();
  _rowObserver = null;
  _releaseScreenOn();
  document.body.classList.remove(ROOT_CLASS);
  _alignHandExLine(); // 閉じたので手札の幅を元の割合に戻す（_active が false なので戻すだけ）
  _setMobileReserve(false);
  document.removeEventListener('keydown', _onKeydown, true);
  document.removeEventListener('pointerdown', _onPointer, true);
  document.removeEventListener('mousemove', _onPointer, true);
  if (_enteredFullscreen && document.fullscreenElement) {
    document.exitFullscreen?.().catch(() => {});
  }
  _enteredFullscreen = false;
  window.dispatchEvent(new Event('resize'));
}

export function isRecViewActive() {
  return _active;
}

/**
 * 初期化。
 * @param {{enterMobilePlayback?: () => void}} [opts]
 *   enterMobilePlayback: 縦向きスマホで再生モードに入る関数（mobile-ui.js）
 */
export function initRecView(opts = {}) {
  _enterMobilePlayback = typeof opts.enterMobilePlayback === 'function' ? opts.enterMobilePlayback : null;
  subscribeReplay('changed', _syncProgress);
  // 画面を消さない頼みは、ページが裏に回る（アプリの切り替え等）と手放されるので、戻ったら取り直す
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') _keepScreenOn();
  });
  // PC で自分が入った全画面が解けたら（全画面中の Esc はページに届かず全画面の解除にだけ使われる）表示も閉じる
  document.addEventListener('fullscreenchange', () => {
    if (_active && _enteredFullscreen && !document.fullscreenElement) {
      _enteredFullscreen = false;
      exitRecView();
    }
  });
  document.addEventListener('sol-replay-loaded-from-url', (e) => {
    _loadedTitle = typeof e.detail?.title === 'string' ? e.detail.title : '';
  });
  // 記録が消えた（リセット・別のデッキ読込）ら閉じる
  document.addEventListener('sol-replay-reset', exitRecView);
  document.addEventListener('sol-deck-load-start', exitRecView);
}
