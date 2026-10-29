/**
 * start-choice.js
 * 起動時の2択（デッキを回す／リプレイを開く）。盤面が空のときだけ盤面の上に出す
 * （2026-10-29 案A・モック _mocks/sim-start-choice-20261029。スマホ縦の旧「デッキを読み込む」1つだけの案内を置き換えた）。
 *
 * - 出す条件: 起動時の復元が終わった後（main.js の 'sol-session-restored'）・カードが1枚も無い・URL にリプレイ指定が無い・
 *   デッキの読み込み中でない・✕で閉じていない。カードが出たら消え、すべて消去などで空に戻ればまた出す。
 * - デッキ側: マイデッキが1件以上あれば「マイデッキから選ぶ」。0件なら「サンプルデッキで始める」と、
 *   遊戯王ニューロンの取り込み（トップの /#mydeck-import）を出す。どちらにも「カードを選んで組む」（検索欄へ移る）。
 * - リプレイ側: このブラウザに保存したリプレイの新しい順3件。0件なら、みんなのリプレイ（GET /api/solitaire/public）を3件。
 *   公開も0件・取得失敗なら説明文だけ。
 * - 計測: GA4 に start_choice_show（出した）／start_choice_click（押した。action で何を押したか）を送る。
 * ユーザー由来の文字列（リプレイの題名）は textContent で入れる（innerHTML には入れない）。
 */

import { savedDecksGet, openDeckPicker } from './deck-input-panel.js';
import { openSidebarSheet, openDeckSheet } from './mobile-ui.js';
import { savedReplaysGet } from '../services/saved-replays.js';
import { isMobilePortrait } from '../utils/viewport.js';

const SHOW_REPLAYS = 3;
const TOP_MARGIN = 16;

let _ready = false;       // 起動時の復元が終わったか
let _dismissed = false;   // ✕で閉じた（このページを開いている間は出さない）
let _loading = false;     // デッキの読み込み中
let _suppressed = false;  // URL にリプレイ指定がある
let _shownOnce = false;   // 計測の「出した」は1回だけ送る
let _publicPromise = null; // みんなのリプレイは1回だけ取りに行く
let _el = null;

function _ga(name, params) {
  try {
    if (typeof window.gtag === 'function') window.gtag('event', name, params || {});
  } catch (_) { /* 計測の失敗で画面を止めない */ }
}

function _node(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function _hasCards() {
  return document.querySelector('.tier-item-wrapper') !== null;
}

function _fmtDate(ms) {
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return '';
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

/** リプレイ1件の行（題名・手数・日付）。押すとそのリプレイを開く本物のリンク */
function _replayRow(href, title, steps, when) {
  const a = _node('a', 'sol-start-replay');
  a.href = href;
  a.appendChild(_node('span', 'sol-start-replay-title', title || '無題'));
  const meta = [Number.isFinite(steps) ? `${steps}手` : '', when].filter(Boolean).join('・');
  a.appendChild(_node('span', 'sol-start-replay-meta', meta));
  return a;
}

/** 押したら閉じずにその操作へ進める（カードが出れば自然に消える） */
function _button(cls, label, action, onClick) {
  const b = _node('button', cls, label);
  b.type = 'button';
  b.addEventListener('click', () => { _ga('start_choice_click', { action }); onClick(); });
  return b;
}

/** PC: 左サイドバーが畳まれていれば開く（中の窓・検索欄の位置はサイドバーのボタンから決まるため） */
function _ensureSidebarOpen() {
  if (document.getElementById('solSidebar')?.classList.contains('collapsed')) {
    document.getElementById('solRailLeft')?.click();
  }
}

function _openPicker(paneId) {
  if (isMobilePortrait()) {
    openSidebarSheet();
    openDeckPicker(paneId);
  } else {
    _ensureSidebarOpen();
    openDeckPicker(paneId);
  }
}

/** カード名の検索欄へ移る（PC=左サイドバー「カードを追加」・スマホ縦=デッキ画面の「編集」） */
function _openCardSearch() {
  if (isMobilePortrait()) {
    openDeckSheet();
    document.querySelector('#solMobileDeckSheet [data-deck-mode="edit"]')?.click();
    document.getElementById('solMobileDeckEditSearch')?.focus();
  } else {
    _ensureSidebarOpen();
    const input = document.getElementById('solDeckAddSearch');
    input?.scrollIntoView({ block: 'nearest' });
    input?.focus();
  }
}

function _buildDeckSide() {
  const opt = _node('div', 'sol-start-opt');
  opt.appendChild(_node('p', 'sol-start-title', 'デッキを回す'));
  opt.appendChild(_node('p', 'sol-start-desc', 'デッキを読み込んで、新しい動きを試す'));
  if (savedDecksGet().length > 0) {
    opt.appendChild(_button('sol-start-btn is-primary', 'マイデッキから選ぶ', 'mydeck', () => _openPicker('deckPaneMyDeck')));
    opt.appendChild(_button('sol-start-btn', 'サンプルデッキから選ぶ', 'sample', () => _openPicker('deckPanePreset')));
    opt.appendChild(_button('sol-start-link', 'カードを選んで組む ›', 'build', _openCardSearch));
  } else {
    opt.appendChild(_button('sol-start-btn is-primary', 'サンプルデッキで始める', 'sample', () => _openPicker('deckPanePreset')));
    opt.appendChild(_button('sol-start-btn', 'カードを選んで組む', 'build', _openCardSearch));
    const neuron = _node('a', 'sol-start-link', '遊戯王ニューロンのデッキを取り込む ›');
    neuron.href = '/#mydeck-import';
    neuron.addEventListener('click', () => _ga('start_choice_click', { action: 'neuron' }));
    opt.appendChild(neuron);
  }
  return opt;
}

function _listLink(label) {
  const a = _node('a', 'sol-start-link', label);
  a.href = '/solitaire/replays';
  a.addEventListener('click', () => _ga('start_choice_click', { action: 'list' }));
  return a;
}

function _fetchPublic() {
  if (!_publicPromise) {
    _publicPromise = fetch(`/api/solitaire/public?limit=${SHOW_REPLAYS}`)
      .then(r => (r.ok ? r.json() : { items: [] }))
      .then(j => (Array.isArray(j?.items) ? j.items.slice(0, SHOW_REPLAYS) : []))
      .catch(() => []);
  }
  return _publicPromise;
}

function _buildReplaySide() {
  const opt = _node('div', 'sol-start-opt');
  const own = savedReplaysGet().slice().sort((a, b) => (b.savedAt || 0) - (a.savedAt || 0)).slice(0, SHOW_REPLAYS);
  if (own.length > 0) {
    opt.appendChild(_node('p', 'sol-start-title', 'リプレイを開く'));
    opt.appendChild(_node('p', 'sol-start-desc', '保存したリプレイを見直す・続きから編集する'));
    for (const r of own) {
      const row = _replayRow('/solitaire?saved=' + encodeURIComponent(r.id), r.title, r.steps, _fmtDate(r.savedAt));
      row.addEventListener('click', () => _ga('start_choice_click', { action: 'saved' }));
      opt.appendChild(row);
    }
    opt.appendChild(_listLink('一覧をすべて見る ›'));
    return opt;
  }
  opt.appendChild(_node('p', 'sol-start-title', 'リプレイを見る'));
  const desc = _node('p', 'sol-start-desc', '保存したリプレイはまだありません。ほかの人の回し方を見てみる');
  opt.appendChild(desc);
  // みんなのリプレイは届いてから差し込む（場所を先に取ると、0件・失敗のとき空きが残るため）
  const slot = _node('div', 'sol-start-public');
  // 届いて高さが変わったら、上端はそのままで高さの上限だけ付け直す（ボタンを動かさない）
  opt.appendChild(slot);
  opt.appendChild(_listLink('一覧をすべて見る ›'));
  _fetchPublic().then(items => {
    if (!items.length) {
      desc.textContent = '保存したリプレイはまだありません。回した手は「保存」で一覧に残せます';
      return;
    }
    slot.appendChild(_node('p', 'sol-start-sub', 'みんなのリプレイ'));
    for (const it of items) {
      const row = _replayRow('/solitaire?replay=' + encodeURIComponent(it.id), it.title, it.steps, _fmtDate(Date.parse(it.published_at)));
      row.addEventListener('click', () => _ga('start_choice_click', { action: 'public' }));
      slot.appendChild(row);
    }
    _place(true);
  });
  return opt;
}

function _build() {
  const el = _node('div', 'sol-start-choice');
  el.id = 'solStartChoice';
  el.setAttribute('role', 'region');
  el.setAttribute('aria-label', 'はじめかた');
  const close = _node('button', 'sol-start-close', '×');
  close.type = 'button';
  close.setAttribute('aria-label', '閉じる');
  close.addEventListener('click', () => { _ga('start_choice_click', { action: 'close' }); dismissStartChoice(); });
  el.appendChild(close);
  // 入りきらないときは中だけスクロールする（✕は外の角に出すので、外の箱は切らない）
  const body = _node('div', 'sol-start-body');
  body.appendChild(_buildDeckSide());
  body.appendChild(_buildReplaySide());
  el.appendChild(body);
  return el;
}

/** 下の限界（スマホ縦は下部バーの上端。2択は下部バーより下の重なり順なので、潜ると押せない） */
function _bottomLimit() {
  const bar = document.getElementById('solMobileBar');
  if (isMobilePortrait() && bar && !bar.hidden) {
    const top = bar.getBoundingClientRect().top;
    if (top > 0) return top;
  }
  return window.innerHeight;
}

/**
 * 盤面列（.sol-field-area）の真ん中より少し上に、上端を決めて置く（後から中身が増えても下へ伸びるだけで、ボタンがずれない）。
 * keepTop: 上端はそのままで、はみ出さない高さの上限だけ付け直す（みんなのリプレイが届いたとき）。
 * 入りきらない高さは中でスクロールする。
 */
function _place(keepTop = false) {
  if (!_el) return;
  const margin = 8;
  const bottom = _bottomLimit() - margin;
  if (!keepTop) {
    const area = document.querySelector('.sol-field-area')?.getBoundingClientRect();
    const vw = window.innerWidth, vh = window.innerHeight;
    const cx = area && area.width > 0 ? area.left + area.width / 2 : vw / 2;
    const cy = area && area.height > 0 ? area.top + Math.min(area.height, vh - area.top) * 0.42 : vh * 0.42;
    _el.style.maxHeight = '';
    const h = _el.offsetHeight;
    _el.style.left = `${cx}px`;
    // 上は ✕（外の角に14pxはみ出す）が切れない分を空ける
    _el.style.top = `${Math.max(TOP_MARGIN, Math.min(cy - h / 2, bottom - h))}px`;
  }
  const top = parseFloat(_el.style.top) || TOP_MARGIN;
  _el.style.maxHeight = `${Math.max(120, bottom - top)}px`;
}

/** 出す・消すを今の状態から決め直す（中身は出すたびに作り直す。デッキやリプレイが増減しているかもしれないため） */
export function updateStartChoice() {
  const show = _ready && !_dismissed && !_loading && !_suppressed && !_hasCards();
  if (!show) {
    if (_el) { _el.remove(); _el = null; }
    return;
  }
  if (_el) { _place(); return; }
  _el = _build();
  // body 直下に置く（盤面の中だと、盤面の大きさ合わせ・重なり順の影響を受けるため）。位置は _place で盤面列に合わせる
  document.body.appendChild(_el);
  _place();
  if (!_shownOnce) {
    _shownOnce = true;
    _ga('start_choice_show', { has_deck: savedDecksGet().length > 0, has_replay: savedReplaysGet().length > 0 });
  }
}

/** ✕・Esc で閉じる（このページを開いている間は出さない） */
export function dismissStartChoice() {
  _dismissed = true;
  updateStartChoice();
}

/**
 * main.js の initializeApp の先頭で呼ぶ（URL のリプレイ指定は、読み込み処理が URL を書き換える前に判定したものを渡す）。
 * @param {{hasReplayInUrl: boolean}} opts
 */
export function initStartChoice({ hasReplayInUrl }) {
  _suppressed = !!hasReplayInUrl;
  // URL のリプレイを読み終えたら（成否を問わず）止めるのをやめる。読めていればカードがあるので出ず、
  // 読めなかった（削除済み・この端末に無い）ときだけ出る。開いた後にすべて消去したときも出る
  document.addEventListener('sol-replay-url-settled', () => { _suppressed = false; updateStartChoice(); });
  document.addEventListener('sol-session-restored', () => { _ready = true; updateStartChoice(); });
  document.addEventListener('sol-deck-load-start', () => { _loading = true; updateStartChoice(); });
  document.addEventListener('sol-deck-load-end', () => { _loading = false; updateStartChoice(); });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || !_el || e.defaultPrevented) return;
    // ほかの窓（デッキの窓・確認の小窓など）にフォーカスがあるときの Esc はそちらを閉じるためのものなので、2択は閉じない
    const a = document.activeElement;
    if (a && a !== document.body && !_el.contains(a)) return;
    dismissStartChoice();
  });
  // カードの出し入れは盤面のどこでも起きるので、カードの置き場所全体を見る（判定は1フレームにまとめる）
  let pending = false;
  new MutationObserver(() => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; updateStartChoice(); });
  }).observe(document.querySelector('.sol-main') || document.body, { childList: true, subtree: true });
  window.addEventListener('resize', () => _place());
  // サイドバーの開閉でも盤面列が動くので、盤面列の大きさの変化でも置き直す
  const area = document.querySelector('.sol-field-area');
  if (area && typeof ResizeObserver === 'function') new ResizeObserver(() => _place()).observe(area);
}
