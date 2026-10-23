/**
 * deck-wish-ui.js — シミュレーターから購入候補に入れる（2026-10-20・モック _mocks/wish-from-sim-20261020 でユーザー承認）
 *
 *   - カードの右クリック／長押しメニュー「購入候補に入れる」（1枚ずつ。いま候補にある枚数を横に出す）
 *   - デッキ欄の見出し（スマホは編集の行）の「足りない分を購入候補へ」→ 窓（PC）／下から出るシート（スマホ）
 *       持っている枚数はその窓で決める。各行に［全部ある］［持ってない］。
 *       今回足したカード（この画面で足した分）と、新着から入れたカード（所持0で記録済み）は最初から「持ってない」。
 *       元から入っていたカードで未入力のものは足さない（デッキ全部を勝手に候補へ入れないため）。
 *       決めた枚数はマイデッキのそのデッキの所持枚数（deck.owned）に残す（トップの所持枚数欄と同じもの）
 *   - デッキから1枚抜いたとき、そのカードが購入候補にあれば「候補から外す」を出す
 *
 * 購入候補の「どのデッキ用か」は shared/wish-deck.js（window.WishDeck）。
 * マイデッキにひも付いていないデッキで足した分は「デッキ指定なし」になる。
 */

import { getDeckLink, getDeltas, collectDeckFromDom } from '../services/deck-link.js';
import { savedDecksGet, savedDecksSet } from './deck-input-panel.js';
import { showToast } from '../utils/toast.js';

const WISH_KEY = 'cardprice_wishlist';                 // トップの index-wish.js と同じ
const WISH_PRICES_CACHE_KEY = 'cardprice_wish_prices_v1'; // トップの価格の写し（新しいカードを入れたら捨てる）
const OWN_MAX = 99;
const ACTION_TOAST_MS = 6000;

const WD = () => window.WishDeck;

// ══════════════════ 購入候補の読み書き ══════════════════

// このページにも購入候補の書き込み先（wishSave）を置く。sync-client.js は wishSave があるページでだけ
// 受け取った購入候補を適用して rev を進める。無いと、ここから送るたびに古い rev のまま「結合」になり、
// 結合（多い方を残す）で「候補から外す」が元に戻ってしまう（2026-10-20 reviewer 指摘）。
// トップの wishSave と同じキー・同じ形で書き、端末間同期へ渡す（同期の適用中は sync-client 側で送らない）
if (typeof window.wishSave !== 'function') {
  window.wishSave = function (list) {
    localStorage.setItem(WISH_KEY, JSON.stringify(list || []));
    try { window.SyncClient?.onWishSave?.(list || []); } catch (e) { console.warn('[deck-wish] 同期に渡せませんでした', e); }
  };
}

function _wishRead() {
  try {
    const list = JSON.parse(localStorage.getItem(WISH_KEY));
    return Array.isArray(list) ? list.filter(c => c && typeof c.name === 'string' && c.name).map(c => WD().normalizeItem(c)) : [];
  } catch {
    return [];
  }
}

/** 保存して端末間同期へ渡す。新しく入ったカードは価格収集の対象に登録する（トップの wishAdd と同じ） */
function _wishWrite(list, before) {
  try {
    window.wishSave(list);
  } catch (e) {
    console.warn('[deck-wish] 購入候補を保存できませんでした', e);
    showToast('購入候補を保存できませんでした（端末の保存領域がいっぱいの可能性があります）');
    return false;
  }
  const had = new Set(before.map(c => c.name));
  const fresh = [...new Set(list.map(c => c.name))].filter(n => !had.has(n));
  if (fresh.length) {
    try { localStorage.removeItem(WISH_PRICES_CACHE_KEY); } catch { /* 写しが残っても次の取得で直る */ }
    for (const card of fresh) {
      fetch('/api/track', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ card }) }).catch(() => {});
    }
  }
  return true;
}

function _deckId() {
  return getDeckLink()?.savedId || null;
}

/** 数える用の一覧。今は無いデッキ（削除したデッキ）用の分はデッキ指定なしとして数える */
function _wishView(list) {
  return WD().forgetUnknown(list, savedDecksGet().map(d => d.id));
}

/** そのカードが購入候補に何枚あるか（全デッキ・全レアリティの合計） */
export function wishCount(name) {
  if (!WD()) return 0;
  return _wishRead().filter(c => c.name === name).reduce((a, c) => a + c.qty, 0);
}

function _ga(name, params) {
  try { if (typeof window.gtag === 'function') window.gtag('event', name, params || {}); } catch { /* 計測の失敗で止めない */ }
}

// ══════════════════ 右クリック「購入候補に入れる」 ══════════════════

export function addOneToWish(name) {
  if (!WD()) return;
  const before = _wishRead();
  let next;
  try {
    next = WD().add(before, name, 1, _deckId());
  } catch (e) {
    showToast(e.message);
    return;
  }
  if (!_wishWrite(next, before)) return;
  _ga('wish_add', { source: 'sim_menu', count: 1 });
  _showActionToast(`「${name}」を購入候補に入れました（候補に${wishCount(name)}枚）`, '購入候補を見る', () => { location.href = '/#wishlist'; });
}

// ══════════════════ デッキから抜いたとき ══════════════════

/**
 * 抜いたカードが購入候補（このデッキ用かデッキ指定なし）にあれば「候補から外す」を出す。
 * @returns {boolean} 出したか（出さなければ呼び出し側がいつものお知らせを出す）
 */
export function offerRemoveFromWish(name, leftInDeck) {
  if (!WD()) return false;
  const deckId = _deckId();
  const c = WD().countByName(_wishView(_wishRead()), deckId).get(name);
  const mine = c ? c.deck + c.free : 0;
  if (!mine) return false;
  const what = leftInDeck ? `「${name}」を1枚抜きました（残り${leftInDeck}枚）` : `「${name}」をデッキから抜きました`;
  _showActionToast(`${what}。購入候補にも${mine}枚あります`, '候補から1枚外す', () => {
    const before = _wishRead();
    // 今は無いデッキ用の分はデッキ指定なしとして外せるようにする（外すのはこのカードの行だけ。
    // 他の行には、別の端末で作ってまだ届いていないデッキ用の内訳があるかもしれないので触らない）
    const view = WD().forgetUnknown(before, savedDecksGet().map(d => d.id), name);
    const { list, removed } = WD().remove(view, name, 1, deckId);
    if (!removed) { showToast('購入候補が変わっているため、外せませんでした'); return; }
    if (_wishWrite(list, before)) showToast(`購入候補から「${name}」を1枚外しました`);
  });
  return true;
}

// ══════════════════ 足りない分を購入候補へ（窓・シート） ══════════════════

let _panel = null;
let _closePanel = null;

/** デッキに入っている枚数（メイン＋EX・盤面・手札・墓地なども含む。トップの requiredCards と同じく名前でまとめる）
 *  TCG先行カード（英語版で先に出て日本に無いカード）は価格が無いので数えない（購入候補に入れない） */
function _requiredCards() {
  const deck = collectDeckFromDom(document);
  const totals = new Map();
  for (const c of [...deck.main, ...deck.ex]) {
    if (window.TcgPreview?.has(c.name)) continue;
    totals.set(c.name, (totals.get(c.name) || 0) + c.qty);
  }
  return totals;
}

/** この画面で足したカード（増減が正のもの） */
function _addedHere() {
  const s = new Set();
  for (const d of getDeltas()) if (d.n > 0) s.add(d.name);
  return s;
}

function _savedDeck() {
  const id = _deckId();
  return id ? savedDecksGet().find(d => d.id === id) || null : null;
}

/**
 * 窓を開く。
 * @param {{sheet?: boolean, anchor?: Element}} opts sheet=スマホの下から出るシート／anchor=PC で位置を合わせるボタン
 */
export function openShortagePanel(opts = {}) {
  if (!WD()) return;
  // TCG先行カードの名前の一覧が届くまで待つ（届く前に数えると英語名が購入候補に入る）
  if (window.TcgPreview && !window.TcgPreview.isReady()) { window.TcgPreview.ready.then(() => openShortagePanel(opts)); return; }
  closeShortagePanel();
  const totals = _requiredCards();
  if (!totals.size) { showToast('デッキを読み込むと、足りない分を購入候補に入れられます'); return; }

  const deck = _savedDeck();
  const savedOwned = deck && deck.owned && typeof deck.owned === 'object' && !Array.isArray(deck.owned) ? deck.owned : {};
  const added = _addedHere();
  const has = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
  // 画面の上での持っている枚数（未入力は持たない）。今回足したカードで未入力のものは「持ってない」から始める
  const owned = Object.create(null);
  for (const name of totals.keys()) {
    const v = savedOwned[name];
    if (has(savedOwned, name) && Number.isInteger(v) && v >= 0 && v <= OWN_MAX) owned[name] = v;
    else if (added.has(name)) owned[name] = 0;
  }
  // 並び: 今回足したカード・持っていないカード → 元から入っていたカード（それぞれデッキの並び順）
  const names = [...totals.keys()];
  const isFirst = n => added.has(n) || owned[n] === 0;
  const groups = [
    ['今回足したカード・持っていないカード', names.filter(isFirst)],
    ['元から入っていたカード', names.filter(n => !isFirst(n))],
  ].filter(([, list]) => list.length);

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
  const panel = el('div', opts.sheet ? 'sol-wish-panel is-sheet' : 'sol-wish-panel');
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-label', '足りない分を購入候補へ');
  const head = el('div', 'sol-wish-head');
  head.append(el('span', 'sol-wish-title', '足りない分を購入候補へ'));
  head.append(el('span', 'sol-wish-deck', deck ? (deck.name || 'マイデッキ') : 'マイデッキ未登録'));
  panel.appendChild(head);

  const table = el('table', 'sol-wish-table');
  const hr = el('tr');
  for (const [t, cls] of [['カード', 'c-name'], ['デッキ', 'c-need'], ['持っている', 'c-own'], ['候補にある', 'c-wish'], ['追加する', 'c-add']]) hr.appendChild(el('th', cls, t));
  table.appendChild(hr);
  const cells = new Map(); // name → {input, all, none, wish, add}
  for (const [label, list] of groups) {
    const g = el('tr', 'sol-wish-group');
    const td = el('td', '', label); td.colSpan = 5; g.appendChild(td);
    table.appendChild(g);
    for (const name of list) {
      const tr = el('tr');
      tr.appendChild(el('td', 'c-name', name));
      tr.appendChild(el('td', 'c-need', String(totals.get(name))));
      const own = el('td', 'c-own');
      const input = el('input', 'sol-wish-own');
      input.type = 'number'; input.min = '0'; input.max = String(OWN_MAX); input.step = '1'; input.inputMode = 'numeric';
      input.placeholder = '?';
      input.setAttribute('aria-label', `${name}を持っている枚数`);
      input.value = has(owned, name) ? String(owned[name]) : '';
      const all = el('button', 'sol-wish-q', '全部ある'); all.type = 'button';
      const none = el('button', 'sol-wish-q', '持ってない'); none.type = 'button';
      own.append(input, all, none);
      tr.appendChild(own);
      const wish = el('td', 'c-wish');
      const add = el('td', 'c-add');
      tr.append(wish, add);
      table.appendChild(tr);
      cells.set(name, { input, all, none, wish, add });
      input.addEventListener('input', () => {
        const n = Number(input.value);
        if (input.value === '') delete owned[name];
        else if (Number.isInteger(n) && n >= 0 && n <= OWN_MAX) owned[name] = n;
        else return;
        refresh();
      });
      all.addEventListener('click', () => { owned[name] = totals.get(name); input.value = String(owned[name]); refresh(); });
      none.addEventListener('click', () => { owned[name] = 0; input.value = '0'; refresh(); });
    }
  }
  panel.appendChild(table);
  panel.appendChild(el('p', 'sol-wish-hint', deck
    ? '元から入っていたカードは、持っている枚数を入れたものだけ追加します。入れた枚数はこのデッキの所持枚数として残り、トップのマイデッキでも同じものが出ます。'
    : '元から入っていたカードは、持っている枚数を入れたものだけ追加します。このデッキはマイデッキに登録していないため、持っている枚数は残らず、追加分は「デッキ指定なし」になります。'));
  const btns = el('div', 'sol-wish-btns');
  const cancel = el('button', 'sol-wish-cancel', '閉じる'); cancel.type = 'button';
  const ok = el('button', 'sol-wish-ok', ''); ok.type = 'button';
  btns.append(cancel, ok);
  panel.appendChild(btns);

  let rows = [];
  function refresh() {
    rows = WD().plan(totals, owned, _wishView(_wishRead()), _deckId());
    for (const r of rows) {
      const c = cells.get(r.name);
      if (!c) continue;
      c.wish.textContent = String(r.candidate);
      c.wish.classList.toggle('is-zero', !r.candidate);
      c.add.textContent = r.owned === null ? '未入力' : r.add ? `+${r.add}` : '0';
      c.add.classList.toggle('is-add', !!r.add);
      c.add.classList.toggle('is-zero', !r.add);
      c.all.classList.toggle('is-on', r.owned !== null && r.owned >= r.required);
      c.none.classList.toggle('is-on', r.owned === 0);
    }
    const total = rows.reduce((a, r) => a + r.add, 0);
    ok.textContent = total ? `${total}枚を購入候補に追加` : '追加するカードはありません';
    ok.disabled = !total;
  }
  refresh();

  cancel.addEventListener('click', () => closeShortagePanel());
  ok.addEventListener('click', () => {
    // 押した時点の購入候補でもう一度数える（窓を開いている間に別のタブで変わっていても二重に足さない）
    const before = _wishRead();
    const fresh = WD().plan(totals, owned, _wishView(before), _deckId());
    let next;
    try {
      next = WD().supplement(before, fresh, _deckId());
    } catch (e) {
      showToast(e.message);
      return;
    }
    const count = fresh.reduce((a, r) => a + r.add, 0);
    if (!count) { refresh(); return; }
    // 候補の保存に失敗したら所持枚数も残さない（片方だけ残らないよう、候補を先に保存する）
    if (!_wishWrite(next, before)) return;
    _saveOwned(owned, totals);
    _ga('wish_add', { source: 'sim_shortage', count });
    closeShortagePanel();
    _showActionToast(`${count}枚を購入候補に追加しました`, '購入候補を見る', () => { location.href = '/#wishlist'; });
  });

  document.body.appendChild(panel);
  if (!opts.sheet && opts.anchor) {
    const r = opts.anchor.getBoundingClientRect();
    const w = panel.offsetWidth;
    panel.style.left = Math.max(12, Math.min(window.innerWidth - w - 12, r.right - w)) + 'px';
    const top = r.top - 8 - panel.offsetHeight;
    panel.style.top = (top >= 12 ? top : Math.min(window.innerHeight - panel.offsetHeight - 12, r.bottom + 8)) + 'px';
  }
  // Esc は窓を閉じるだけにする（カード詳細を畳むなど、盤面側の Esc を同時に走らせない）
  const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); e.preventDefault(); closeShortagePanel(); } };
  const onDown = (e) => { if (!panel.contains(e.target) && e.target !== opts.anchor) closeShortagePanel(); };
  const onResize = () => closeShortagePanel(); // 位置がずれたまま残さない
  // 盤面の要素が押下の伝播を止めても外側の押下を拾えるよう、捕捉の段階で受ける
  document.addEventListener('keydown', onKey, true);
  // 開いた押下そのもので閉じないよう、次の押下から見る
  setTimeout(() => document.addEventListener('pointerdown', onDown, true), 0);
  if (!opts.sheet) window.addEventListener('resize', onResize);
  _panel = panel;
  _closePanel = () => {
    document.removeEventListener('keydown', onKey, true);
    document.removeEventListener('pointerdown', onDown, true);
    window.removeEventListener('resize', onResize);
    panel.remove();
  };
}

/**
 * マイデッキに保存するとき（deck-edit-ui.js の「変更を保存」「マイデッキに登録」）、この画面で足したカードのうち
 * 持っている枚数が未入力のものを「持っていない（0枚）」として残す。保存で増減の記録が消えても、
 * 次に「足りない分を購入候補へ」を開いたとき最初から「持ってない」側に並ぶようにするため
 * （新着の「+ デッキに追加」で入れたカードと同じ扱い）。deck を直接書き換える（保存は呼び出し側）。
 */
export function markAddedNotOwned(deck) {
  const added = _addedHere();
  if (!added.size) return;
  const owned = Object.assign(Object.create(null), deck.owned && typeof deck.owned === 'object' && !Array.isArray(deck.owned) ? deck.owned : {});
  let changed = false;
  for (const name of added) {
    if (!Object.prototype.hasOwnProperty.call(owned, name)) { owned[name] = 0; changed = true; }
  }
  if (changed) deck.owned = owned;
}

export function closeShortagePanel() {
  if (_closePanel) _closePanel();
  _panel = null;
  _closePanel = null;
}

/** 窓で決めた持っている枚数を、マイデッキのそのデッキに残す（ひも付いていなければ残さない） */
function _saveOwned(owned, totals) {
  const id = _deckId();
  if (!id) return;
  const list = savedDecksGet();
  const deck = list.find(d => d.id === id);
  if (!deck) return;
  // カード名が __proto__ でも普通のキーとして扱えるよう、プロトタイプの無いオブジェクトに写す
  const next = Object.assign(Object.create(null), deck.owned && typeof deck.owned === 'object' && !Array.isArray(deck.owned) ? deck.owned : {});
  let changed = false;
  for (const name of totals.keys()) {
    if (Object.prototype.hasOwnProperty.call(owned, name)) {
      if (next[name] !== owned[name]) { next[name] = owned[name]; changed = true; }
    } else if (Object.prototype.hasOwnProperty.call(next, name)) {
      delete next[name]; changed = true;
    }
  }
  // 盤面にも保存済みのデッキにも無いカードの分は消す（トップの所持枚数欄が保存時に今のカードだけ残すのと同じ。
  // 抜いたカードの分が溜まらない。まだ保存していない「抜いた」カードは保存済みのデッキにあるので残す）
  const inSaved = new Set([...(deck.main || []), ...(deck.ex || [])].map(c => c && c.name));
  for (const name of Object.keys(next)) {
    if (!totals.has(name) && !inSaved.has(name)) { delete next[name]; changed = true; }
  }
  if (!changed) return;
  deck.owned = next;
  deck.updated = Date.now();
  savedDecksSet(list);
}

// ══════════════════ お知らせ（操作つき） ══════════════════

function _showActionToast(msg, label, onClick) {
  document.querySelectorAll('.sol-toast-action').forEach(el => el.remove());
  const el = document.createElement('div');
  el.className = 'sol-toast sol-toast-action sol-toast-wrap';
  const text = document.createElement('span');
  text.textContent = msg;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'sol-toast-undo';
  btn.textContent = label;
  let done = false;
  const close = () => {
    done = true;
    el.classList.remove('sol-toast-visible');
    setTimeout(() => el.remove(), 400);
  };
  btn.addEventListener('click', () => { if (done) return; close(); onClick(); });
  el.append(text, btn);
  document.body.appendChild(el);
  requestAnimationFrame(() => el.classList.add('sol-toast-visible'));
  setTimeout(() => { if (!done) close(); }, ACTION_TOAST_MS);
}
