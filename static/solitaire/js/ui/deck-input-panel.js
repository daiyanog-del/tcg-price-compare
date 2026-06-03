/**
 * deck-input-panel.js
 * テキストデッキ / 環境メタデッキ読込UI
 * 既存API /api/card-image, /api/meta, /api/meta/deck を流用
 */

import { addCardToPool } from '../components/card-manager.js';
import { registerCardImage, registerCardName } from '../services/replay-service.js';
import { setCardName } from './card-info-panel.js';

const API_CARD_IMAGE = '/api/card-image';
const API_CARD_INFO  = '/api/card-info';
const API_META = '/api/meta';
const API_META_DECK = '/api/meta/deck';

/**
 * デッキリストテキストをパース
 * "3 灰流うらら" 形式 → [{qty, name}]
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
 * 盤面・手札のカードをデッキに戻してから、プールの全カードを削除する
 * 新しいデッキを読み込む前に呼ぶ
 */
async function clearAllCards() {
  // フィールド・手札・墓地等のカードをすべてプールに戻す
  const { returnAllCardsToDeck } = await import('../components/card-manager.js');
  returnAllCardsToDeck();

  // プールの全カードを削除（ダミーも前デッキのカードも）
  ['poolRow', 'poolRow2'].forEach(poolId => {
    const pool = document.getElementById(poolId);
    if (pool) pool.innerHTML = '';
  });
}

/**
 * カード名からAPIで画像URLを取得してプールに追加
 * @param {string}  name   カード名
 * @param {number}  qty    枚数
 * @param {boolean} isEx   EXデッキか
 */
async function addCardByName(name, qty, isEx) {
  // 画像URLとカード種別を並列取得
  const [imageRes, infoRes] = await Promise.all([
    fetch(`${API_CARD_IMAGE}?name=${encodeURIComponent(name)}`).catch(() => null),
    fetch(`${API_CARD_INFO}?name=${encodeURIComponent(name)}`).catch(() => null),
  ]);

  let src = null;
  try {
    if (imageRes?.ok) {
      const data = await imageRes.json();
      src = data.url || null;
    }
  } catch { /* 無視 */ }

  // broad_type: "monster" | "spell" | "trap"
  let cardType = null;
  try {
    if (infoRes?.ok) {
      const data = await infoRes.json();
      if (data.found && data.broad_type) cardType = data.broad_type;
    }
  } catch { /* 無視 */ }

  if (!src) {
    console.warn(`画像取得失敗: ${name}`);
    return;
  }

  const poolId = isEx ? 'poolRow2' : 'poolRow';
  for (let i = 0; i < qty; i++) {
    addCardToPool(src, false, isEx);
    // 追加直後に最後の wrapper を取ってリプレイ辞書・カード名・種別を登録
    const pool = document.getElementById(poolId);
    if (pool) {
      const lastWrapper = pool.querySelector('.tier-item-wrapper:last-child');
      const lastImg     = lastWrapper?.querySelector('img');
      if (lastImg?.id) {
        registerCardImage(lastImg.id, src);
        registerCardName(lastImg.id, name);  // リプレイ共有用に名前を記録
        setCardName(lastImg, name);  // カード詳細パネル用に名前を付与
        // セット時のモンスター/魔法罠判定に使う（モンスター=裏側守備、魔法罠=伏せ）
        if (cardType && lastWrapper) lastWrapper.dataset.cardType = cardType;
      }
    }
  }
}

/**
 * テキストデッキを読み込んでプールに追加（事前にダミーをクリア）
 */
export async function loadDeckFromText(text) {
  const cards = parseDeckList(text);
  if (cards.length === 0) { alert('デッキリストが空です'); return; }

  await clearAllCards();

  const msg = document.getElementById('deckLoadingMsg');
  if (msg) { msg.textContent = 'カード画像を読み込み中...'; msg.style.display = 'block'; }
  for (const { qty, name } of cards) {
    await addCardByName(name, qty, false);
  }
  if (msg) { msg.textContent = ''; msg.style.display = 'none'; }
}

/**
 * 環境デッキAPIから全デッキ情報を取得して読み込む（事前にダミーをクリア）
 * @param {string} theme  テーマ名
 */
export async function loadMetaDeck(theme) {
  const msg = document.getElementById('deckLoadingMsg');
  if (msg) { msg.textContent = `${theme} のデッキを読み込み中...`; msg.style.display = 'block'; }

  try {
    const res = await fetch(`${API_META_DECK}?theme=${encodeURIComponent(theme)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const fullDeck = data.full_deck;  // [{name, qty, is_ex}]
    if (!fullDeck || fullDeck.length === 0) throw new Error('デッキリストが取得できませんでした');

    await clearAllCards();

    for (const { name, qty, is_ex } of fullDeck) {
      // is_ex フィールドを直接使用（meta_scraper.py 側で付与済み）
      await addCardByName(name, qty, !!is_ex);
    }
  } catch (e) {
    alert(`環境デッキの読み込みに失敗しました: ${e.message}`);
  }

  if (msg) { msg.textContent = ''; msg.style.display = 'none'; }
}

/**
 * デッキ入力パネルを初期化
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
  if (listEl.dataset.loaded === 'true') return;

  listEl.innerHTML = '<span class="deck-loading-msg">読み込み中...</span>';
  try {
    const res = await fetch(API_META);
    const data = await res.json();
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
