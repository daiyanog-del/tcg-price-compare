/**
 * deck-edit-ui.js — シミュレーターの中でデッキを編集し、マイデッキへ保存する（2026-10-20）
 *
 * 置き場所（モックでユーザーと決めた形）:
 *   PC     … 左サイドバーの「カードを追加」（検索してデッキに入れる）
 *            デッキ欄の見出し（デッキ名・変更あり／マイデッキ未登録・保存ボタン）
 *            右クリックメニューの末尾（デッキに1枚足す／デッキから1枚抜く）
 *   スマホ … デッキシートの「回す｜編集」の「編集」（デッキ名と保存の行・検索・同じ名前を1マスにまとめた一覧・
 *            選んだカードだけ上の欄で増減）。長押しメニューにも PC と同じ2項目が出る
 *
 * 保存の考え方（deck-link.js）:
 *   - マイデッキから読み込んだデッキ … 「変更を保存」で、そのデッキの最新に今回の増減を重ねて上書き（取り消しあり）
 *   - それ以外（新着・環境デッキ・持ち込み等） … 「マイデッキに新規登録」で名前を付けて新しく登録し、以後ひも付く
 *
 * 抜けるのはデッキ欄（EXデッキ欄）に残っていて、リプレイ記録に出てこないカードだけ（deck-link.js の pickRemovable）。
 */

import {
  onDeckLinkChange, getDeckLink, setDeckLink, clearDeckLink, recordDelta, getDeltas,
  markSaved, restoreLinkState, captureLinkState,
  serializeDeck, deckContents, applyDeltas, collectDeckFromDom, pickRemovable, BOARD_WRAPPERS,
} from '../services/deck-link.js';
import { addCardsToDeck, savedDecksGet, savedDecksSet } from './deck-input-panel.js';
import { forgetCard, getLoggedCardIds } from '../services/replay-service.js';
import { setExtraMenuItemsProvider } from './context-menu.js';
import { showToast } from '../utils/toast.js';

const SUGGEST_DEBOUNCE_MS = 200; // カットインの検索と同じ待ち時間
const SUGGEST_MAX = 8;
const NAME_MAX = 60;             // マイデッキ名の上限（ニューロン取込の名前欄と同じ）
const UNDO_TOAST_MS = 6000;      // 「取り消し」を押せる時間

// ══════════════════ デッキの中身を数える・足す・抜く ══════════════════

function _wrappersByName(name) {
  return [...document.querySelectorAll(BOARD_WRAPPERS)].filter(w =>
    w.dataset.token !== '1' && w.querySelector('.tier-item')?.dataset.cardName === name);
}

/** デッキ全体（場・手札・墓地・除外も含む）でのその名前の枚数 */
export function countInDeck(name) {
  return _wrappersByName(name).length;
}

function _hasDeckCards() {
  return [...document.querySelectorAll(BOARD_WRAPPERS)].some(w => w.dataset.token !== '1');
}

/** 抜けるカード（無ければ null）。prefer を先に見る（右クリックしたそのカード） */
function _findRemovable(name, prefer = null) {
  const all = _wrappersByName(name);
  const ordered = prefer && all.includes(prefer) ? [prefer, ...all.filter(w => w !== prefer)] : all;
  return pickRemovable(ordered, getLoggedCardIds());
}

// 足す処理（通信あり）は順番に1つずつ行う。素早く「＋」を3回押したら3枚入る（捨てない）
let _addQueue = Promise.resolve();

/**
 * デッキに1枚足す。
 * @returns {Promise<boolean>} 足せたか
 */
export function addOne(name) {
  if (!name) return Promise.resolve(false);
  const run = _addQueue.then(() => _addOneNow(name));
  _addQueue = run.catch(() => false);
  return run;
}

async function _addOneNow(name) {
  const added = await addCardsToDeck([{ qty: 1, name }]);
  if (added === null) { showToast('デッキを読み込み中のため、入れられませんでした'); return false; }
  if (!added.length) { showToast(`「${name}」の画像を取得できず、デッキに入れられませんでした`); return false; }
  const sec = / ex$/.test(added[0].id) ? 'ex' : 'main';
  recordDelta(name, sec, +1);
  document.dispatchEvent(new CustomEvent('sol-deck-edited'));
  showToast(`「${name}」を${sec === 'ex' ? 'EXデッキ' : 'デッキ'}に入れました（${countInDeck(name)}枚）`);
  return true;
}

/**
 * デッキから1枚抜く。
 * @param {Element|null} prefer 右クリックしたカード（それがデッキ欄にあればそれを抜く）
 * @returns {boolean} 抜けたか
 */
export function removeOne(name, prefer = null) {
  const w = _findRemovable(name, prefer);
  if (!w) {
    showToast(_whyNotRemovable(name));
    return false;
  }
  const id = w.querySelector('.tier-item')?.id;
  const sec = / ex$/.test(w.id) ? 'ex' : 'main';
  w.remove();
  if (id) forgetCard(id);
  recordDelta(name, sec, -1);
  document.dispatchEvent(new CustomEvent('sol-deck-edited'));
  const left = countInDeck(name);
  showToast(left ? `「${name}」を1枚抜きました（残り${left}枚）` : `「${name}」をデッキから抜きました`);
  return true;
}

function _whyNotRemovable(name) {
  const inPile = _wrappersByName(name).some(w => ['poolRow', 'poolRow2'].includes(w.parentElement?.id));
  return inPile
    ? 'リプレイの記録で使っているカードのため抜けません'
    : 'デッキ欄に残っていないため抜けません（場・手札・墓地のカードは抜けません）';
}

// ══════════════════ 保存 ══════════════════

/** 名前を聞く（キャンセル・空は null）。既にある名前は聞き直す */
function _askName(initial) {
  const existing = new Set(savedDecksGet().map(d => d.name));
  let def = initial;
  for (;;) {
    const input = prompt('マイデッキに登録する名前', def);
    if (input === null) return null;
    const n = input.trim().slice(0, NAME_MAX);
    if (!n) return null;
    if (existing.has(n)) {
      alert(`「${n}」はすでにマイデッキにあります。別の名前にしてください。`);
      def = n;
      continue;
    }
    return n;
  }
}

/** 今の増減を、ひも付いたマイデッキへ上書き保存する（取り消しあり） */
export function saveChanges() {
  const link = getDeckLink();
  if (!link?.savedId) { registerAsNew(); return; }
  const list = savedDecksGet();
  const deck = list.find(d => d.id === link.savedId);
  if (!deck) {
    alert(`保存先のマイデッキ「${link.name}」が見つかりません（削除された可能性があります）。新しいデッキとして登録します。`);
    registerAsNew();
    return;
  }
  const before = captureLinkState();
  const prev = { text: deck.text, main: deck.main, ex: deck.ex };
  // テキストを正とする（トップの normalizeDeck と同じ。テキストが空の古いデータは配列を使う）
  const next = applyDeltas(deckContents(deck), getDeltas());
  deck.main = next.main;
  deck.ex = next.ex;
  deck.text = serializeDeck(next);
  deck.updated = Date.now();
  const written = deck.updated;
  savedDecksSet(list);
  markSaved({ savedId: deck.id, name: deck.name });
  window.renderSavedDecks?.();
  _showUndoToast(`「${deck.name || 'マイデッキ'}」に保存しました`, () => {
    const cur = savedDecksGet();
    const d = cur.find(x => x.id === deck.id);
    // 取り消しまでの間に別の場所で変えられていたら戻さない（その変更を消さないため）
    if (!d || d.updated !== written) { showToast('その後に変更されているため、取り消せませんでした'); return; }
    // 中身は保存前に戻し、更新時刻は新しくする（古い時刻に戻すと、端末間同期の結合で保存後の内容が勝ち、取り消しが消える）
    Object.assign(d, prev, { updated: Date.now() });
    savedDecksSet(cur);
    // 画面側の「変更あり」も戻す。トーストは読込・消去・編集で閉じるので、ここに来るのは保存直後の状態のときだけ
    const now = getDeckLink();
    if (now?.savedId === deck.id && !now.dirty) restoreLinkState(before);
    window.renderSavedDecks?.();
    showToast('保存を取り消しました');
  });
}

/** 新しいマイデッキとして登録する（未登録のデッキ／「別のデッキとして保存」） */
export function registerAsNew() {
  const link = getDeckLink();
  let contents;
  const base = link?.savedId ? savedDecksGet().find(d => d.id === link.savedId) : null;
  if (base) {
    // ひも付きのデッキを別名で保存: 元のデッキ（最新）に今回の増減を重ねたもの（並ばなかったカードも落とさない）
    contents = applyDeltas(deckContents(base), getDeltas());
  } else {
    contents = collectDeckFromDom(document);
  }
  if (!contents.main.length && !contents.ex.length) { alert('デッキにカードがありません'); return; }
  const name = _askName(base ? `${base.name || 'マイデッキ'} のコピー` : (link?.name || 'デッキ'));
  if (!name) return;
  const list = savedDecksGet();
  const deck = { id: 'd_' + Date.now(), name, text: serializeDeck(contents), main: contents.main, ex: contents.ex, updated: Date.now() };
  list.push(deck);
  savedDecksSet(list);
  markSaved({ savedId: deck.id, name });
  window.renderSavedDecks?.();
  showToast(`「${name}」をマイデッキに登録しました`);
}

// ══════════════════ 表示: トースト（取り消し付き） ══════════════════

let _closeUndoToast = null;

/** 取り消しを押せなくする（別のデッキの読込・消去・編集の後に押すと、関係ない盤面へ古い状態を戻してしまうため） */
function _dismissUndoToast() {
  if (_closeUndoToast) _closeUndoToast();
}

function _showUndoToast(msg, onUndo) {
  _dismissUndoToast();
  document.querySelectorAll('.sol-toast-action').forEach(el => el.remove());
  const el = document.createElement('div');
  el.className = 'sol-toast sol-toast-action';
  const text = document.createElement('span');
  text.textContent = msg;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'sol-toast-undo';
  btn.textContent = '取り消し';
  let done = false;
  const close = () => {
    done = true;
    if (_closeUndoToast === close) _closeUndoToast = null;
    el.classList.remove('sol-toast-visible');
    setTimeout(() => el.remove(), 400);
  };
  _closeUndoToast = close;
  btn.addEventListener('click', () => { if (done) return; close(); onUndo(); });
  el.append(text, btn);
  document.body.appendChild(el);
  requestAnimationFrame(() => el.classList.add('sol-toast-visible'));
  setTimeout(() => { if (!done) close(); }, UNDO_TOAST_MS);
}

// ══════════════════ 表示: デッキ名と保存の行（PC の見出し・スマホの編集） ══════════════════

/**
 * デッキ名・状態・保存ボタンを container に描く。デッキが無ければ空にする。
 * @param {HTMLElement} container
 */
function _renderSaveBar(container) {
  container.replaceChildren();
  const link = getDeckLink();
  if (!link && !_hasDeckCards()) { container.hidden = true; return; }
  container.hidden = false;
  const name = document.createElement('span');
  name.className = 'sol-deck-bar-name';
  name.textContent = link?.name || 'デッキ';
  name.title = name.textContent;
  container.appendChild(name);

  const state = document.createElement('span');
  state.className = 'sol-deck-bar-state';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'sol-deck-bar-save';
  if (link?.savedId) {
    if (!link.dirty) return; // 変更が無ければ名前だけ（押す必要の無いボタンは出さない）
    state.textContent = '変更あり';
    btn.textContent = '変更を保存';
    btn.addEventListener('click', saveChanges);
    const alt = document.createElement('button');
    alt.type = 'button';
    alt.className = 'sol-deck-bar-alt';
    alt.textContent = '別のデッキとして保存';
    alt.addEventListener('click', registerAsNew);
    container.append(state, btn, alt);
  } else {
    state.textContent = 'マイデッキ未登録';
    btn.textContent = 'マイデッキに新規登録';
    btn.addEventListener('click', registerAsNew);
    container.append(state, btn);
  }
}

function _renderPcBar() {
  const label = document.querySelector('#imagePool .pool-label');
  if (!label) return;
  let bar = label.querySelector('.sol-deck-bar');
  if (!bar) {
    bar = document.createElement('span');
    bar.className = 'sol-deck-bar';
  }
  // 枚数（main.js initZoneCounts の .zone-count）より後ろに置く。枚数の札は初期化の順で後から足されるため、毎回末尾へ移す
  label.appendChild(bar);
  _renderSaveBar(bar);
}

// ══════════════════ 検索してデッキに入れる（PC のサイドバー・スマホの編集で共通） ══════════════════

function _thumbUrl(v) {
  const url = typeof v === 'string' ? v : (v && v.kind === 'image' ? v.url : null);
  return typeof url === 'string' && /^https?:\/\//.test(url) ? url : null;
}

/**
 * 検索欄に候補一覧をつなぐ。候補を押すとデッキに入れ、検索欄は残す（続けて入れられるように）。
 * @param {HTMLInputElement} input
 * @param {HTMLElement} list  候補の ul
 * @param {{floating?:boolean}} opts floating=入力欄の下に重ねて出す（PC のサイドバー）
 */
function _wireCardSearch(input, list, { floating = false } = {}) {
  let timer = null;
  let abort = null;
  let active = -1;
  const items = () => [...list.querySelectorAll('.sol-deck-suggest-item')];
  const close = () => { list.hidden = true; list.replaceChildren(); active = -1; };
  const place = () => {
    if (!floating) return;
    const r = input.getBoundingClientRect();
    list.style.left = `${Math.round(r.left)}px`;
    list.style.top = `${Math.round(r.bottom + 4)}px`;
  };
  const pick = async (name) => {
    close();
    await addOne(name);
    input.focus();
  };
  const highlight = () => items().forEach((el, i) => el.classList.toggle('is-active', i === active));
  const render = (cards) => {
    list.replaceChildren();
    if (!cards.length) { close(); return; }
    for (const c of cards) {
      const li = document.createElement('li');
      li.className = 'sol-deck-suggest-item';
      li.setAttribute('role', 'option');
      li.dataset.name = c.name;
      const thumb = document.createElement('span');
      thumb.className = 'sol-deck-suggest-thumb';
      const nm = document.createElement('span');
      nm.className = 'sol-deck-suggest-name';
      nm.textContent = c.name;
      li.append(thumb, nm);
      if (c.unreleased) {
        const b = document.createElement('span');
        b.className = 'sol-deck-suggest-badge';
        b.textContent = '未発売';
        li.appendChild(b);
      }
      const add = document.createElement('span');
      add.className = 'sol-deck-suggest-add';
      add.textContent = '＋ デッキに入れる';
      li.appendChild(add);
      // 押した瞬間に入力欄のフォーカスを外さない（blur で一覧が閉じないように）。選ぶのは click
      li.addEventListener('pointerdown', (e) => e.preventDefault());
      li.addEventListener('click', () => pick(c.name));
      list.appendChild(li);
    }
    place();
    list.hidden = false;
    _loadThumbs(list);
  };
  const fetchSuggest = async (q) => {
    if (abort) abort.abort();
    abort = new AbortController();
    try {
      const res = await fetch(`/api/suggest?include_unreleased=1&q=${encodeURIComponent(q)}`, { signal: abort.signal });
      if (!res.ok) return;
      const data = await res.json();
      if (input.value.trim() !== q) return; // 打ち直した後に古い応答が来た
      const arr = Array.isArray(data) ? data : [];
      render(arr.map(it => (typeof it === 'string' ? { name: it, unreleased: false } : it))
        .filter(it => it && typeof it.name === 'string' && it.name)
        .slice(0, SUGGEST_MAX));
    } catch (err) {
      if (err?.name !== 'AbortError') console.warn('[deck-edit] カード名の候補の取得に失敗', err);
    }
  };
  input.addEventListener('input', () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (!q) { close(); return; }
    timer = setTimeout(() => fetchSuggest(q), SUGGEST_DEBOUNCE_MS);
  });
  input.addEventListener('blur', () => setTimeout(close, 150));
  input.addEventListener('keydown', (e) => {
    const els = items();
    if (e.key === 'Escape' && !list.hidden) { e.preventDefault(); e.stopPropagation(); close(); return; }
    if (!list.hidden && els.length && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
      e.preventDefault();
      active = e.key === 'ArrowDown' ? Math.min(active + 1, els.length - 1) : Math.max(active - 1, -1);
      highlight();
      return;
    }
    // 日本語入力の変換確定の Enter では選ばない
    if (e.key === 'Enter') {
      e.preventDefault();
      if (e.isComposing || e.keyCode === 229) return;
      const el = els[active >= 0 ? active : 0];
      if (!list.hidden && el) pick(el.dataset.name);
    }
  });
  // サイドバーや画面のスクロール・大きさの変化に合わせて位置を直す（PC は body 直下に重ねているため）
  if (floating) {
    window.addEventListener('resize', () => { if (!list.hidden) place(); });
    window.addEventListener('scroll', (e) => { if (!list.hidden && !list.contains(e.target)) place(); }, true);
  }
}

/** 候補のカード画像を /api/card-images でまとめて取って差し込む */
async function _loadThumbs(list) {
  const lis = [...list.querySelectorAll('.sol-deck-suggest-item')];
  const names = lis.map(li => li.dataset.name);
  if (!names.length) return;
  try {
    const res = await fetch('/api/card-images', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ names }),
    });
    if (!res.ok) return;
    const images = (await res.json())?.images || {};
    for (const li of lis) {
      if (!li.isConnected) continue;
      const url = _thumbUrl(images[li.dataset.name]);
      if (!url) continue;
      const img = document.createElement('img');
      img.src = url;
      img.alt = '';
      img.loading = 'lazy';
      li.querySelector('.sol-deck-suggest-thumb')?.replaceChildren(img);
    }
  } catch { /* サムネが無くても選べる */ }
}

// ══════════════════ スマホ: デッキシートの「編集」 ══════════════════

let _mobileSelected = null; // 選んでいるカード名

/** 「回す｜編集」を切り替える */
function _setMobileMode(mode) {
  const sheet = document.getElementById('solMobileDeckSheet');
  if (!sheet) return;
  const edit = mode === 'edit';
  sheet.classList.toggle('is-edit', edit);
  sheet.querySelectorAll('[data-deck-mode]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.deckMode === mode)));
  const pane = document.getElementById('solMobileDeckEdit');
  if (pane) pane.hidden = !edit;
  if (edit) { _mobileSelected = null; _renderMobileEdit(); }
}

/** デッキシートを開くたびに「回す」へ戻す（mobile-ui.js の openDeckSheet から呼ぶ） */
export function resetMobileDeckMode() {
  const input = document.getElementById('solMobileDeckEditSearch');
  if (input) input.value = '';
  _setMobileMode('play');
}

/** 名前ごとにまとめた一覧（メイン・EX の順。並びはデッキ欄と同じ種別順） */
function _groupedDeck() {
  const groups = { main: new Map(), ex: new Map() };
  const order = ['#poolRow', '#poolRow2', '.main-content', '.center-slot', '.side-slot', '#free-space'];
  const seen = new Set();
  for (const sel of order) {
    for (const w of document.querySelectorAll(`${sel} .tier-item-wrapper`)) {
      if (seen.has(w) || w.dataset.token === '1') continue;
      seen.add(w);
      const cardEl = w.querySelector('.tier-item');
      const name = cardEl?.dataset.cardName;
      if (!name) continue;
      const sec = / ex$/.test(w.id) ? 'ex' : 'main';
      const g = groups[sec].get(name) || { name, count: 0, wrapper: w };
      g.count++;
      groups[sec].set(name, g);
    }
  }
  return groups;
}

function _cellThumb(w) {
  const cardEl = w.querySelector('.tier-item');
  if (cardEl?.tagName === 'IMG' && cardEl.getAttribute('src')) {
    const img = document.createElement('img');
    img.className = 'sol-mobile-deck-thumb';
    img.src = cardEl.getAttribute('src');
    img.alt = cardEl.dataset.cardName || '';
    return img;
  }
  const div = document.createElement('div');
  div.className = 'sol-mobile-deck-thumb sol-mobile-deck-thumb-proxy';
  div.textContent = cardEl?.dataset.cardName || '?';
  return div;
}

function _renderMobileEdit() {
  const pane = document.getElementById('solMobileDeckEdit');
  if (!pane || pane.hidden) return;
  _renderSaveBar(document.getElementById('solMobileDeckEditBar'));

  const groups = _groupedDeck();
  if (_mobileSelected && !groups.main.has(_mobileSelected) && !groups.ex.has(_mobileSelected)) _mobileSelected = null;

  // 選んだカードの増減の欄
  const sel = document.getElementById('solMobileDeckEditSel');
  sel.replaceChildren();
  const g = _mobileSelected ? (groups.main.get(_mobileSelected) || groups.ex.get(_mobileSelected)) : null;
  sel.classList.toggle('is-empty', !g);
  if (!g) {
    const p = document.createElement('span');
    p.className = 'sol-deck-edit-sel-hint';
    p.textContent = 'カードを選ぶと枚数を変えられます';
    sel.appendChild(p);
  } else {
    const nm = document.createElement('span');
    nm.className = 'sol-deck-edit-sel-name';
    nm.textContent = g.name;
    const minus = document.createElement('button');
    minus.type = 'button';
    minus.className = 'sol-deck-edit-step';
    minus.textContent = '−';
    minus.setAttribute('aria-label', `${g.name}を1枚抜く`);
    minus.disabled = !_findRemovable(g.name);
    minus.addEventListener('click', () => { removeOne(g.name); });
    const cnt = document.createElement('b');
    cnt.className = 'sol-deck-edit-count';
    cnt.textContent = String(g.count);
    const plus = document.createElement('button');
    plus.type = 'button';
    plus.className = 'sol-deck-edit-step';
    plus.textContent = '＋';
    plus.setAttribute('aria-label', `${g.name}を1枚足す`);
    plus.addEventListener('click', () => { addOne(g.name); });
    sel.append(_cellThumb(g.wrapper), nm, minus, cnt, plus);
  }

  // 一覧
  const grid = document.getElementById('solMobileDeckEditGrid');
  grid.replaceChildren();
  for (const [sec, label] of [['main', 'メイン'], ['ex', 'EX']]) {
    const m = groups[sec];
    if (!m.size) continue;
    const total = [...m.values()].reduce((a, x) => a + x.count, 0);
    const h = document.createElement('p');
    h.className = 'sol-deck-edit-sec';
    h.textContent = `${label} ${total}`;
    const wrap = document.createElement('div');
    wrap.className = 'sol-mobile-deck-grid sol-deck-edit-grid';
    for (const item of m.values()) {
      const cell = document.createElement('button');
      cell.type = 'button';
      cell.className = 'sol-mobile-deck-cell';
      cell.dataset.cardName = item.name;
      cell.setAttribute('aria-label', `${item.name}（${item.count}枚）`);
      if (item.wrapper.classList.contains('wm-released')) cell.classList.add('wm-released');
      if (item.wrapper.classList.contains('sample-art')) cell.classList.add('sample-art');
      if (item.name === _mobileSelected) cell.classList.add('selected');
      cell.appendChild(_cellThumb(item.wrapper));
      const check = document.createElement('span');
      check.className = 'sol-mobile-deck-check';
      check.textContent = '✓';
      cell.appendChild(check);
      if (item.count >= 2) {
        const b = document.createElement('span');
        b.className = 'sol-deck-edit-badge';
        b.textContent = `×${item.count}`;
        cell.appendChild(b);
      }
      cell.addEventListener('click', () => {
        _mobileSelected = _mobileSelected === item.name ? null : item.name;
        _renderMobileEdit();
      });
      wrap.appendChild(cell);
    }
    grid.append(h, wrap);
  }
  if (!grid.childElementCount) {
    const p = document.createElement('p');
    p.className = 'sol-deck-edit-empty';
    p.textContent = 'デッキにカードがありません。上の検索から入れられます';
    grid.appendChild(p);
  }
}

// ══════════════════ 初期化 ══════════════════

/** 右クリック／長押しメニューに足す項目 */
function _menuItems(wrapper) {
  if (wrapper.dataset.token === '1') return [];
  const name = wrapper.querySelector('.tier-item')?.dataset.cardName;
  if (!name) return [];
  const removable = !!_findRemovable(name, wrapper);
  return [
    { label: 'デッキに1枚足す', sub: `今 ${countInDeck(name)}枚`, action: () => { addOne(name); } },
    { label: 'デッキから1枚抜く', disabled: !removable, action: () => { removeOne(name, wrapper); } },
  ];
}

export function initDeckEditUI() {
  setExtraMenuItemsProvider(_menuItems);

  // デッキの入れ替え・消去でひも付けを付け直す（読込元が成功後に setDeckLink で上書きする）
  // EX だけの追加読込（ニューロン取込で EX のみ等）は入れ替えではないので、増えたカードを増減として記録する
  let appendBefore = null;
  document.addEventListener('sol-deck-load-start', (e) => {
    _dismissUndoToast();
    if (e.detail?.replace !== false) { appendBefore = null; setDeckLink({}); return; }
    appendBefore = new Set(document.querySelectorAll('.tier-item-wrapper'));
  });
  document.addEventListener('sol-deck-load-end', () => {
    if (!appendBefore) return;
    const before = appendBefore;
    appendBefore = null;
    for (const w of document.querySelectorAll(BOARD_WRAPPERS)) {
      if (before.has(w) || w.dataset.token === '1') continue;
      const name = w.querySelector('.tier-item')?.dataset.cardName;
      if (name) recordDelta(name, / ex$/.test(w.id) ? 'ex' : 'main', +1);
    }
  });
  document.addEventListener('sol-board-cleared', () => { _dismissUndoToast(); clearDeckLink(); });
  document.addEventListener('sol-deck-edited', _dismissUndoToast);

  const rerender = () => { _renderPcBar(); _renderMobileEdit(); };
  onDeckLinkChange(rerender);
  // カードの増減（読込・共有リプレイの読込・復元）でも見出しを出し入れする
  for (const id of ['poolRow', 'poolRow2']) {
    const el = document.getElementById(id);
    if (el) new MutationObserver(rerender).observe(el, { childList: true });
  }
  rerender();

  // PC: 左サイドバーの「カードを追加」
  const pcInput = document.getElementById('solDeckAddSearch');
  const pcList = document.getElementById('solDeckAddSuggest');
  if (pcInput && pcList) {
    // サイドバーの外（盤面の上）まで広げて出すため body 直下に置く
    document.body.appendChild(pcList);
    _wireCardSearch(pcInput, pcList, { floating: true });
  }

  // スマホ: デッキシートの「回す｜編集」
  document.querySelectorAll('#solMobileDeckSheet [data-deck-mode]').forEach(btn => {
    btn.addEventListener('click', () => _setMobileMode(btn.dataset.deckMode));
  });
  const mInput = document.getElementById('solMobileDeckEditSearch');
  const mList = document.getElementById('solMobileDeckEditSuggest');
  if (mInput && mList) _wireCardSearch(mInput, mList);
}

/** 退避からの復元（main.js が loadSessionResume の後に呼ぶ） */
export { restoreLinkState, captureLinkState, setDeckLink };
