/**
 * deck-replays-ui.js — シミュレーターの「このデッキのリプレイ」（2026-10-21・モック _mocks/deck-replay-link-20261021 でユーザー承認）
 *
 *   - PC: 左サイドバー「デッキを選択」の下の欄（#solDeckReplays）。マイデッキにひも付いているときだけ出す
 *   - スマホ: デッキシートの頭の下の1行（#solMobileDeckReplaysBtn）→ 下から出る一覧
 *   - 行を押すと /solitaire?saved=<id> で開く（今の記録が置き換わるときは確認する。リプレイ一覧ページと同じ）
 *   - 保存したリプレイを開いたとき（sol-replay-loaded-from-url）、そのリプレイがマイデッキ用なら左の欄にそのデッキのリプレイを出す。
 *     記録したときのデッキと今のデッキが同じならひも付けも戻す。違えば戻さず（盤面は記録したときの構成なのに、保存は
 *     「保存済みの最新＋増減」で行うので、足す・抜く・保存の結果が食い違うため。reviewer 指摘）、上に差の帯を出す
 *     （［今のデッキを見る］＝トップのマイデッキを別タブで開く）。そのときデッキ欄は未登録の扱いなので、記録したときの構成を
 *     「マイデッキに登録」で別に残せる
 *
 * 一覧・差の計算は shared/deck-replays.js（window.DeckReplays）。リプレイは端末の中だけ。
 */

import { getDeckLink, setDeckLink, onDeckLinkChange, deckContents } from '../services/deck-link.js';
import { savedDecksGet } from './deck-input-panel.js';
import { getLogLength } from '../services/replay-service.js';
import { saveUndoPoint, markUndoOffer } from './undo-point.js';
import { confirmAsync } from './inline-ui.js';

const DR = () => window.DeckReplays;

function _el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

// 開いているリプレイのデッキ（記録したときと中身が違うのでひも付けはしていない）。左の欄はこれを出す。
// マイデッキにひも付いたら（別のデッキを読み込んだ等）そちらを優先して、これは忘れる
let _viewDeckId = null;

function _linkedDeck() {
  const id = getDeckLink()?.savedId || _viewDeckId;
  return id ? savedDecksGet().find(d => d.id === id) || null : null;
}

/**
 * リプレイを開く。今の記録が置き換わるときは、以前は確認窓を出していたが、今は直前の写しを取っておき、
 * 開いた先で6秒間「元に戻す」を出す（undo-point.js）。
 */
async function _open(item, anchor) {
  let steps = 0;
  try { steps = getLogLength(); } catch (e) { steps = 0; }
  if (steps > 0) {
    const r = saveUndoPoint();
    if (r.id) markUndoOffer(`リプレイ「${item.title || '無題'}」を開きました（前の記録に戻せます）`);
    else if (r.failed && !(await confirmAsync(anchor || null, `記録が大きいため、開くと今の記録（${steps}手）には戻せません。このリプレイを開きますか？`, '開く'))) return;
  }
  location.href = '/solitaire?saved=' + encodeURIComponent(item.id);
}

/** 1行（題名・手数と日付・記録の後にデッキが変わっていれば金の文字） */
function _row(item, deck) {
  const b = _el('button', 'sol-drp-row');
  b.type = 'button';
  b.appendChild(_el('span', 'sol-drp-title', item.title || '無題'));
  b.appendChild(_el('span', 'sol-drp-meta', DR().meta(item)));
  const d = DR().diff(item.deckSnap, deckContents(deck));
  if (d && d.changed) b.appendChild(_el('span', 'sol-drp-meta is-drift', DR().DRIFT_SHORT));
  b.title = item.title || '無題';
  b.addEventListener('click', () => _open(item, b));
  return b;
}

function _items(deck) {
  return deck ? DR().forDeck(DR().list(), deck.id) : [];
}

// ══════════════════ PC: 左サイドバー ══════════════════

function _renderSidebar() {
  const sec = document.getElementById('solDeckReplays');
  const body = document.getElementById('solDeckReplaysBody');
  if (!sec || !body) return;
  const deck = _linkedDeck();
  sec.hidden = !deck;
  body.replaceChildren();
  if (!deck) return;
  const items = _items(deck);
  body.appendChild(_el('span', 'sol-drp-deck', `${deck.name || 'マイデッキ'}・${items.length}件`));
  if (!items.length) {
    body.appendChild(_el('p', 'sol-drp-empty', 'まだありません。回したあと「保存」→「端末に保存」すると、ここに入ります。'));
    return;
  }
  for (const it of items) body.appendChild(_row(it, deck));
}

// ══════════════════ スマホ: デッキシートの1行と一覧 ══════════════════

function _renderMobileBtn() {
  const btn = document.getElementById('solMobileDeckReplaysBtn');
  if (!btn) return;
  const deck = _linkedDeck();
  btn.hidden = !deck;
  const cnt = btn.querySelector('.sol-side-btn-count');
  const n = deck ? _items(deck).length : 0;
  if (cnt) { cnt.textContent = String(n); cnt.hidden = false; }
}

let _sheet = null;
function _closeSheet() {
  if (_sheet) { _sheet.remove(); _sheet = null; }
}

function _openSheet() {
  _closeSheet();
  const deck = _linkedDeck();
  if (!deck) return;
  const items = _items(deck);
  const wrap = _el('div', 'sol-drp-sheet');
  wrap.setAttribute('role', 'dialog');
  wrap.setAttribute('aria-label', 'このデッキのリプレイ');
  const scrim = _el('div', 'sol-drp-scrim');
  scrim.addEventListener('click', _closeSheet);
  const panel = _el('div', 'sol-drp-panel');
  const head = _el('div', 'sol-drp-head');
  head.appendChild(_el('span', 'sol-drp-head-title', 'このデッキのリプレイ'));
  head.appendChild(_el('span', 'sol-drp-head-deck', deck.name || 'マイデッキ'));
  const x = _el('button', 'sol-drp-x', '×');
  x.type = 'button';
  x.setAttribute('aria-label', '閉じる');
  x.addEventListener('click', _closeSheet);
  head.appendChild(x);
  panel.appendChild(head);
  if (!items.length) panel.appendChild(_el('p', 'sol-drp-empty', 'まだありません。回したあと「保存」→「端末に保存」すると、ここに入ります。'));
  for (const it of items) panel.appendChild(_row(it, deck));
  wrap.append(scrim, panel);
  document.body.appendChild(wrap);
  _sheet = wrap;
}

// ══════════════════ 保存したリプレイを開いたとき ══════════════════

function _showDriftBanner(deck, d) {
  document.getElementById('solDrpBanner')?.remove();
  const bar = _el('div', 'sol-drp-banner');
  bar.id = 'solDrpBanner';
  bar.setAttribute('role', 'status');
  bar.appendChild(_el('span', 'sol-drp-banner-text', '記録したときのデッキと、今のデッキが違います：'));
  bar.appendChild(_el('b', 'sol-drp-banner-diff', DR().describe(d)));
  const see = _el('button', 'sol-drp-banner-btn', '今のデッキを見る');
  see.type = 'button';
  // 再生中の盤面を残すため、トップのマイデッキは別のタブで開く（#mydeck:<id> でそのデッキを開く）
  see.addEventListener('click', () => { window.open('/#mydeck:' + encodeURIComponent(deck.id), '_blank', 'noopener'); });
  const x = _el('button', 'sol-drp-banner-x', '×');
  x.type = 'button';
  x.setAttribute('aria-label', '閉じる');
  x.addEventListener('click', () => bar.remove());
  bar.append(see, x);
  document.body.appendChild(bar);
}

function _onReplayLoaded(e) {
  document.getElementById('solDrpBanner')?.remove();
  const id = e.detail?.savedReplayId;
  if (!id || !DR()) return;
  const item = DR().list().find(r => r.id === id);
  if (!item?.deckId) return;
  const deck = savedDecksGet().find(dk => dk.id === item.deckId);
  if (!deck) return; // デッキを消したあとのリプレイは「デッキなし」として開く
  const d = DR().diff(item.deckSnap, deckContents(deck));
  if (d && d.changed) {
    // 中身が違うのでひも付けはしない。左の欄だけそのデッキのリプレイを出す
    _viewDeckId = deck.id;
    _render();
    _showDriftBanner(deck, d);
    return;
  }
  // 同じ（または比べられない古いリプレイ）ならひも付け直す（左の欄にこのデッキのリプレイが並び、「変更を保存」もそのデッキへ）
  _viewDeckId = null;
  setDeckLink({ savedId: deck.id, name: deck.name });
}

// ══════════════════ 初期化 ══════════════════

function _render() {
  if (!DR()) return;
  _renderSidebar();
  _renderMobileBtn();
}

export function initDeckReplaysUI() {
  if (!DR()) return;
  onDeckLinkChange(() => { if (getDeckLink()?.savedId) _viewDeckId = null; _render(); });
  // 別のデッキを読み込む・すべて消去すると、開いていたリプレイのデッキは忘れる（環境デッキ・取り込み等は savedId が無いので
  // ひも付けの変化だけでは分からない。カードを1枚足しただけでは忘れない）
  const forget = () => { if (_viewDeckId) { _viewDeckId = null; _render(); } };
  document.addEventListener('sol-deck-load-start', (e) => { if (e.detail?.replace !== false) forget(); });
  document.addEventListener('sol-board-cleared', forget);
  // 保存したリプレイが増えた・消えた（saved-replays.js）とき、別のタブで変わったとき
  document.addEventListener('sol-saved-replays-changed', _render);
  window.addEventListener('storage', (e) => { if (e.key === DR().KEY || e.key === 'cardprice_saved_decks') _render(); });
  document.addEventListener('sol-replay-loaded-from-url', _onReplayLoaded);
  document.getElementById('solMobileDeckReplaysBtn')?.addEventListener('click', _openSheet);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && _sheet) _closeSheet(); });
  _render();
}
