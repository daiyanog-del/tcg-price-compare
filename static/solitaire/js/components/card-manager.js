import { enableTouchDrag } from './drag-drop.js';
import { applyCardState } from './card-state.js';
import { openCardContextMenu } from '../ui/context-menu.js';
import { playActivateEffect } from './card-effects.js';
import { createProxyCardElement } from './proxy-card.js';

/**
 * カード管理モジュール
 * カードの作成、追加、削除などを管理
 */

let itemCount = 0;
let initialImageSrcs = [];

/**
 * カード要素（img または proxy-card div）にイベントリスナーを一括登録
 * createCardElement と save-load-service.js の restoreCardsToSlot の
 * 重複登録を解消した共通ヘルパ。dblclick による効果発動もここで登録する。
 *
 * プロキシカード（div.tier-item）にも同じリスナーが付くよう、
 * 第二引数は img.tier-item または div.tier-item どちらでも受け付ける。
 *
 * @param {Element} wrapper  - .tier-item-wrapper
 * @param {Element} cardEl   - img.tier-item または div.tier-item（proxy-card）
 */
export function attachCardImageListeners(wrapper, cardEl) {
  cardEl.addEventListener('dragstart', window.drag);
  cardEl.addEventListener('touchstart', enableTouchDrag, { passive: false });

  // 右クリック: コンテキストメニューを開く
  cardEl.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    openCardContextMenu(wrapper, cardEl, e.clientX, e.clientY);
  });

  // ダブルクリック: 効果発動アニメーションを再生してリプレイに記録
  cardEl.addEventListener('dblclick', (e) => {
    e.preventDefault();
    playActivateEffect(wrapper);
    const cardId = cardEl.id;
    if (cardId && typeof window.replayLog === 'function') {
      window.replayLog({ actionType: 'activateEffect', cardId });
    }
  });
}

/**
 * カードIDを生成
 * @returns {string} - 一意のカードID
 */
function generateCardId() {
  return `item-${String(itemCount++).padStart(3, '0')}`;
}

/**
 * カード要素を作成（多態対応版）
 *
 * 第一引数の種類に応じてカード要素を切り替える:
 *   - 文字列 (URL)         → <img src=url class="tier-item">（従来どおり。既存呼び出しは無修正で動く）
 *   - {kind:'image', url}  → <img src=url class="tier-item">
 *   - {kind:'proxy', proxy}→ createProxyCardElement(proxy)（div.tier-item）
 *   - それ以外 or null     → <img>（src=''、旧来の挙動に合わせて wrapper を返す）
 *
 * wrapper 構造・ID 付与・attachCardImageListeners はすべての種類で共通。
 *
 * @param {string|Object} srcOrDisplay  URL 文字列 or {kind, url?, proxy?}
 * @param {boolean}       isEx          EXデッキのカードか
 * @returns {Element}                   .tier-item-wrapper
 */
function createCardElement(srcOrDisplay, isEx = false) {
  const wrapper = document.createElement('div');
  wrapper.classList.add('tier-item-wrapper');
  wrapper.id = `${generateCardId()} ${isEx ? 'ex' : 'normal'}`;

  let cardEl;

  // 発売済みクリーン画像にのみ自サイト透かしを重ねる（SAMPLE画像・プロキシには付けない）
  let isReleasedImage = false;

  if (typeof srcOrDisplay === 'string') {
    // 従来の文字列（URL）引数: 完全後方互換。文字列は発売済みクリーン画像
    const img = document.createElement('img');
    img.src = srcOrDisplay;
    img.classList.add('tier-item');
    img.setAttribute('draggable', 'true');
    cardEl = img;
    isReleasedImage = true;
  } else if (srcOrDisplay && srcOrDisplay.kind === 'image') {
    // {kind:'image', url, source?} オブジェクト。source==='official_sample' は未発売SAMPLE
    const img = document.createElement('img');
    img.src = srcOrDisplay.url || '';
    img.classList.add('tier-item');
    img.setAttribute('draggable', 'true');
    cardEl = img;
    isReleasedImage = srcOrDisplay.source !== 'official_sample';
  } else if (srcOrDisplay && srcOrDisplay.kind === 'proxy') {
    // {kind:'proxy', proxy: {...}} オブジェクト → プロキシ要素を生成
    const proxyEl = createProxyCardElement(srcOrDisplay.proxy || {});
    // draggable は createProxyCardElement 内で設定済みだが念のため確認
    proxyEl.setAttribute('draggable', 'true');
    cardEl = proxyEl;
  } else {
    // フォールバック（null / 未知の形式）: src='' の img を作る（旧来の挙動）
    const img = document.createElement('img');
    img.src = '';
    img.classList.add('tier-item');
    img.setAttribute('draggable', 'true');
    cardEl = img;
  }

  // 発売済みクリーン画像の wrapper に透かしクラスを付与（CSS の data-self-wm で出し分け）
  if (isReleasedImage) {
    wrapper.classList.add('wm-released');
  }

  // ID はすべての種類で wrapper の先頭トークンを使う
  cardEl.id = wrapper.id.split(' ')[0];

  // イベントリスナーを一括登録（dblclick による効果発動含む）
  attachCardImageListeners(wrapper, cardEl);

  wrapper.appendChild(cardEl);
  return wrapper;
}

/**
 * カードをプールに追加
 * @param {string} src - 画像のURL
 * @param {boolean} toFirst - 先頭に追加するか
 * @param {boolean} isEx - EXデッキのカードか
 */
export function addCardToPool(src, toFirst = false, isEx = false) {
  const wrapper = createCardElement(src, isEx);
  const targetPool = isEx ? document.getElementById('poolRow2') : document.getElementById('poolRow');

  if (toFirst && targetPool.firstChild) {
    targetPool.insertBefore(wrapper, targetPool.firstChild);
  } else {
    targetPool.appendChild(wrapper);
  }
}

/**
 * 複数のカードをプールに追加
 * @param {string[]} cards - 画像URLの配列
 * @param {boolean} isEx - EXデッキのカードか
 */
export function addCardsToPool(cards, isEx = false) {
  cards.forEach(src => addCardToPool(src, false, isEx));
}

/**
 * 初期カードを設定
 * @param {string[]} imageSrcs - 初期画像のURL配列
 */
export function initializeCards(imageSrcs) {
  // 初期画像のsrcを絶対パスで保持
  initialImageSrcs = imageSrcs.map(src => {
    const a = document.createElement('a');
    a.href = src;
    return a.href;
  });

  imageSrcs.forEach(src => addCardToPool(src));

  const poolRow = document.getElementById('poolRow');
  poolRow.querySelectorAll('.tier-item-wrapper').forEach(item => {
    item.id = 'initial';
  });
}

/**
 * プール内のカードをソート
 * @param {string} poolId - ソート対象のプールID
 */
export function sortPoolCards(poolId) {
  const pool = document.getElementById(poolId);
  if (!pool) return;

  const TYPE_ORDER = {monster: 0, spell: 1, trap: 2};

  const sortedItems = Array.from(pool.children).sort((a, b) => {
    const ta = TYPE_ORDER[a.dataset.cardType] ?? 3;
    const tb = TYPE_ORDER[b.dataset.cardType] ?? 3;
    if (ta !== tb) return ta - tb;
    // 同種別内は元のID順を維持
    // img.tier-item または div.tier-item（プロキシ）どちらでも .tier-item で取れる
    const idA = a.querySelector('.tier-item')?.id.toLowerCase() || '';
    const idB = b.querySelector('.tier-item')?.id.toLowerCase() || '';
    return idA.localeCompare(idB);
  });

  sortedItems.forEach(item => pool.appendChild(item));
}

/**
 * 指定されたタイプのカードをプールから取得
 * @param {string} poolId - プールID
 * @param {string} type - カードタイプ ('normal' または 'ex')
 * @returns {Element[]} - カード要素の配列
 */
export function getCardsFromPool(poolId, type) {
  const pool = document.getElementById(poolId);
  if (!pool) return [];

  return Array.from(pool.querySelectorAll('.tier-item-wrapper'))
    .filter(item => item.id.includes(type));
}

/**
 * カードをスロットに戻す（スタイルリセット付き）
 * @param {Element} card - カード要素
 * @param {string} poolId - 戻し先のプールID
 */
export function returnCardToPool(card, poolId) {
  card.style = '';
  applyCardState(card, {}); // 守備・セット状態をクリア
  const pool = document.getElementById(poolId);
  if (pool) {
    pool.appendChild(card);
  }
}

/**
 * すべてのカードをデッキに戻す
 */
export function returnAllCardsToDeck() {
  const poolRow = document.getElementById('poolRow');
  const poolRow2 = document.getElementById('poolRow2');
  const mainContent = document.querySelector('.main-content');
  const centerSlot = document.querySelector('.center-slot');

  // メインコンテンツ内のカードを戻す
  const allItemsMain = mainContent.querySelectorAll('.tier-item-wrapper');
  allItemsMain.forEach(item => {
    item.style = '';
    applyCardState(item, {}); // 守備・セット状態をクリア
    if (item.id.includes('ex')) {
      poolRow2.appendChild(item);
    } else {
      poolRow.appendChild(item);
    }
  });

  // 手札のカードを戻す
  const allItemsCenter = centerSlot.querySelectorAll('.tier-item-wrapper');
  allItemsCenter.forEach(item => {
    item.style = '';
    applyCardState(item, {}); // 守備・セット状態をクリア
    if (item.id.includes('ex')) {
      poolRow2.appendChild(item);
    } else {
      poolRow.appendChild(item);
    }
  });

  // ソート
  sortPoolCards('poolRow');
  sortPoolCards('poolRow2');
}

/**
 * ランダムにカードを選択
 * @param {string} poolId - プールID
 * @param {number} count - 選択枚数
 * @returns {Element[]} - 選択されたカード要素の配列
 */
export function drawRandomCards(poolId, count) {
  const poolItems = getCardsFromPool(poolId, 'normal');
  const selectedItems = [];

  while (selectedItems.length < count && poolItems.length > 0) {
    const randomIndex = Math.floor(Math.random() * poolItems.length);
    const selectedItem = poolItems.splice(randomIndex, 1)[0];
    selectedItem.style = '';
    selectedItems.push(selectedItem);
  }

  return selectedItems;
}
