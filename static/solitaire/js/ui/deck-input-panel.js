/**
 * deck-input-panel.js
 * テキストデッキ / 環境メタデッキ読込UI
 * 既存API /api/card-image, /api/meta, /api/meta/deck を流用
 */

import { addCardToPool } from '../components/card-manager.js';
import { registerCardImage } from '../services/replay-service.js';

const API_CARD_IMAGE = '/api/card-image';
const API_META = '/api/meta';
const API_META_DECK = '/api/meta/deck';

/**
 * デッキリストテキストをパース
 * "3 灰流うらら" 形式 → [{qty, name}]
 * @param {string} text
 * @returns {{qty: number, name: string}[]}
 */
function parseDeckList(text) {
  return text.split('\n')
    .map(l => l.trim())
    .filter(Boolean)
    .map(line => {
      const m = line.match(/^(\d+)\s+(.+)$/);
      return m ? { qty: parseInt(m[1], 10), name: m[2].trim() } : { qty: 1, name: line };
    });
}

/**
 * カード名からAPIで画像URLを取得してプールに追加
 * @param {string} name   カード名
 * @param {number} qty    枚数
 * @param {boolean} isEx  EXデッキか
 */
async function addCardByName(name, qty, isEx) {
  let src;
  try {
    const res = await fetch(`${API_CARD_IMAGE}?name=${encodeURIComponent(name)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    src = data.url || null;
  } catch {
    src = null;
  }

  if (!src) {
    console.warn(`画像取得失敗: ${name}`);
    return;
  }

  for (let i = 0; i < qty; i++) {
    addCardToPool(src, false, isEx);
    // カードが追加された直後、最後に追加されたカードのIDを登録
    // card-manager 内でIDを採番しているため、追加後に最後の要素を取る
    const poolId = isEx ? 'poolRow2' : 'poolRow';
    const pool = document.getElementById(poolId);
    if (pool) {
      const last = pool.querySelector('.tier-item-wrapper:last-child img');
      if (last?.id) registerCardImage(last.id, src);
    }
  }
}

/**
 * テキストデッキを読み込んでプールに追加
 * @param {string} text  デッキリストテキスト
 */
export async function loadDeckFromText(text) {
  const cards = parseDeckList(text);
  if (cards.length === 0) { alert('デッキリストが空です'); return; }
  const msg = document.getElementById('deckLoadingMsg');
  if (msg) { msg.textContent = 'カード画像を読み込み中...'; msg.style.display = 'block'; }
  for (const { qty, name } of cards) {
    await addCardByName(name, qty, false);
  }
  if (msg) { msg.textContent = ''; msg.style.display = 'none'; }
}

/**
 * 環境デッキAPIから全デッキ情報を取得して読み込む
 * @param {string} theme  テーマ名
 */
export async function loadMetaDeck(theme) {
  const msg = document.getElementById('deckLoadingMsg');
  if (msg) { msg.textContent = `${theme} のデッキを読み込み中...`; msg.style.display = 'block'; }

  try {
    const res = await fetch(`${API_META_DECK}?theme=${encodeURIComponent(theme)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const fullDeck = data.full_deck;  // [{name, qty}]
    if (!fullDeck || fullDeck.length === 0) throw new Error('デッキリストが取得できませんでした');

    // EXデッキ判定: APIが返す full_deck に is_ex フィールドがない場合は
    // デッキテキストの "#EX" 区切りを利用（deck_text があれば）
    const deckText = data.deck_text || '';
    const exSection = deckText.match(/(?:#ex|#エクストラ)[\s\S]*/i);
    const exNames = new Set();
    if (exSection) {
      parseDeckList(exSection[0].replace(/^#[^\n]*/i, ''))
        .forEach(c => exNames.add(c.name));
    }

    for (const { name, qty } of fullDeck) {
      const isEx = exNames.has(name);
      await addCardByName(name, qty, isEx);
    }
  } catch (e) {
    alert(`環境デッキの読み込みに失敗しました: ${e.message}`);
  }

  if (msg) { msg.textContent = ''; msg.style.display = 'none'; }
}

/**
 * デッキ入力パネルを初期化
 * solitaire.html 内の #deckInputContainer に対して動作
 */
export function initDeckInputPanel() {
  const container = document.getElementById('deckInputContainer');
  if (!container) return;

  // タブ切り替え
  container.querySelectorAll('.deck-input-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      container.querySelectorAll('.deck-input-tab').forEach(t => t.classList.remove('active'));
      container.querySelectorAll('.deck-input-pane').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      const paneId = tab.dataset.pane;
      const pane = document.getElementById(paneId);
      if (pane) pane.classList.add('active');

      // 環境デッキタブに切り替えた時はティアリストを取得
      if (paneId === 'deckPanePreset') loadMetaTierList();
    });
  });

  // テキスト読み込みボタン
  document.getElementById('deckLoadTextBtn')?.addEventListener('click', () => {
    const text = document.getElementById('deckTextarea')?.value || '';
    loadDeckFromText(text);
  });
}

/**
 * 環境デッキのティアリストを取得してUIに表示
 */
async function loadMetaTierList() {
  const listEl = document.getElementById('metaTierList');
  if (!listEl) return;
  if (listEl.dataset.loaded === 'true') return; // 既にロード済み

  listEl.innerHTML = '<span class="deck-loading-msg">読み込み中...</span>';
  try {
    const res = await fetch(API_META);
    const data = await res.json();
    // data は [{theme, tier, ...}] を想定
    const tiers = Array.isArray(data) ? data : (data.tiers || data.themes || []);
    if (tiers.length === 0) throw new Error('データなし');

    listEl.innerHTML = '';
    tiers.forEach(item => {
      const theme = item.theme || item.name || item;
      const btn = document.createElement('button');
      btn.className = 'meta-tier-btn';
      btn.textContent = theme;
      btn.addEventListener('click', () => loadMetaDeck(theme));
      listEl.appendChild(btn);
    });
    listEl.dataset.loaded = 'true';
  } catch (e) {
    listEl.innerHTML = `<span class="deck-loading-msg">取得失敗: ${e.message}</span>`;
  }
}
