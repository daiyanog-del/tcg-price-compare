/**
 * セーブ&ロードサービス
 * カード配置とゲーム状態をlocalStorageに保存・復元
 */

import { getCardState, applyCardState } from '../components/card-state.js';
import { attachCardImageListeners } from '../components/card-manager.js';
import { createProxyCardElement } from '../components/proxy-card.js';

const API_CARD_IMAGES = '/api/card-images';

const SAVE_KEY_PREFIX = 'card-sim-save-slot-';
const MAX_SLOTS = 5;
const SESSION_RESUME_KEY = 'sol-session-resume';

/**
 * スロット内のカード情報を取得
 * @param {Element} slot - スロット要素
 * @returns {Array} - カード情報の配列
 */
function getCardsFromSlot(slot) {
  const cards = [];
  const wrappers = slot.querySelectorAll('.tier-item-wrapper');

  wrappers.forEach(wrapper => {
    // img.tier-item（発売済み）または div.tier-item（プロキシ）どちらも .tier-item で取れる
    const cardEl = wrapper.querySelector('.tier-item');
    if (cardEl) {
      const cardName = cardEl.dataset.cardName || '';

      // src の決定:
      //   発売済み img: img.src（実URL）
      //   プロキシ div: 'card://カード名'（再解決センチネル）
      //
      // ロード時は restoreCardsToSlot で card:// を検出して /api/card-images で
      // 現時点の解決結果に変換する（発売後は正規画像、画像削除後はプロキシ）。
      let src;
      if (cardEl.tagName === 'IMG') {
        src = cardEl.src || '';
      } else {
        // div.tier-item（プロキシカード）: カード名があれば card:// センチネルで保存
        src = cardName ? `card://${cardName}` : '';
      }

      const state = getCardState(wrapper);
      cards.push({
        id:       wrapper.id,
        src,
        style:    wrapper.getAttribute('style') || '',
        cardName,
        state,   // { orientation, face }
      });
    }
  });

  return cards;
}

/**
 * カウンター情報を取得
 * @returns {Array} - カウンター情報の配列
 */
function getCounterStates() {
  const counters = [];
  const counterElements = document.querySelectorAll('.counter-container');

  counterElements.forEach((counter, index) => {
    // 元のカウンターはスキップ（親要素のidがparent）
    if (counter.id === 'parent') {
      return;
    }

    const textbox = counter.querySelector('.counter-textbox');
    const parentSlot = counter.parentElement;

    counters.push({
      value: textbox ? textbox.value : '1',
      parentId: parentSlot ? parentSlot.className : '',
      parentDataSlot: parentSlot ? parentSlot.getAttribute('data-slot') : null,
      style: counter.getAttribute('style') || '',
    });
  });

  return counters;
}

/**
 * 現在のゲーム状態を保存
 * @param {number} slotNumber - 保存スロット番号（1-5）
 * @param {string} slotName - スロット名（オプション）
 * @returns {boolean} - 保存成功/失敗
 */
export function saveGameState(slotNumber, slotName = '') {
  if (slotNumber < 1 || slotNumber > MAX_SLOTS) {
    throw new Error('無効なスロット番号です');
  }

  try {
    const gameState = {
      timestamp: Date.now(),
      slotName: slotName || `スロット ${slotNumber}`,
      slots: {},
      counters: getCounterStates(),
    };

    // デッキプール
    gameState.slots.poolRow = getCardsFromSlot(document.getElementById('poolRow'));
    gameState.slots.poolRow2 = getCardsFromSlot(document.getElementById('poolRow2'));

    // カスタムスロット（フィールド）
    const customSlots = document.querySelectorAll('.custom-slot');
    customSlots.forEach(slot => {
      const slotNum = slot.getAttribute('data-slot');
      if (slotNum) {
        gameState.slots[`custom-slot-${slotNum}`] = getCardsFromSlot(slot);
      }
    });

    // サイドスロット（墓地、除外など）
    const sideSlots = document.querySelectorAll('.side-slot');
    sideSlots.forEach((slot, index) => {
      gameState.slots[`side-slot-${index}`] = getCardsFromSlot(slot);
    });

    // 手札スロット
    const centerSlot = document.querySelector('.center-slot');
    if (centerSlot) {
      gameState.slots['center-slot'] = getCardsFromSlot(centerSlot);
    }

    // フリースペース
    const freeSpace = document.getElementById('free-space');
    if (freeSpace) {
      gameState.slots['free-space'] = getCardsFromSlot(freeSpace);
    }

    // localStorageに保存（圧縮して容量を削減）
    const key = SAVE_KEY_PREFIX + slotNumber;
    const jsonString = JSON.stringify(gameState);
    const compressed = LZString.compress(jsonString);
    localStorage.setItem(key, compressed);

    console.log(`ゲーム状態をスロット${slotNumber}に保存しました（圧縮前: ${(jsonString.length / 1024).toFixed(2)}KB, 圧縮後: ${(compressed.length / 1024).toFixed(2)}KB）`);
    return true;
  } catch (error) {
    console.error('保存エラー:', error);
    return false;
  }
}

/**
 * card:// センチネルを /api/card-images バッチ POST で解決する
 *
 * @param {Array} cards  getCardsFromSlot の返り値（srcに card:// を含む可能性）
 * @returns {Promise<Map<string, Object>>}
 *   カード名 -> { kind, url?, proxy? } の Map
 *   解決不要（実URL）のカードは含まない
 */
async function _resolveCardSentinels(cards) {
  const sentinelNames = [];
  for (const c of (cards || [])) {
    if (c.src && c.src.startsWith('card://')) {
      const name = c.src.slice('card://'.length);
      if (name && !sentinelNames.includes(name)) {
        sentinelNames.push(name);
      }
    }
  }

  if (sentinelNames.length === 0) return new Map();

  try {
    const res = await fetch(API_CARD_IMAGES, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ names: sentinelNames }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const images = data.images || {};

    const result = new Map();
    for (const name of sentinelNames) {
      // images[name] は文字列URL / {kind, url?, proxy?} / null
      const val = images[name] ?? null;
      if (!val) {
        result.set(name, { kind: 'none' });
      } else if (typeof val === 'string') {
        result.set(name, { kind: 'image', url: val });
      } else {
        result.set(name, val);
      }
    }
    return result;
  } catch (e) {
    console.warn('[restoreCardsToSlot] card:// 解決失敗（プロキシ風表示にフォールバック）:', e);
    // 失敗時は kind='none' 扱い（名前のみ表示）
    const fallback = new Map();
    for (const name of sentinelNames) {
      fallback.set(name, { kind: 'none' });
    }
    return fallback;
  }
}

/**
 * カードを復元（非同期版: card:// センチネルを再解決してから DOM を構築）
 * @param {Element} slot - 復元先スロット
 * @param {Array} cards - カード情報の配列
 */
async function restoreCardsToSlot(slot, cards) {
  if (!slot || !cards) return;

  // 既存のカードをクリア（初期カードは残す）
  const existingWrappers = slot.querySelectorAll('.tier-item-wrapper:not(#initial)');
  existingWrappers.forEach(wrapper => wrapper.remove());

  // card:// センチネルをバッチ解決（該当なしなら即 return した空 Map）
  const resolved = await _resolveCardSentinels(cards);

  // カードを復元
  for (const cardData of cards) {
    // 初期カードはスキップ（既に存在するため）
    if (cardData.id === 'initial') continue;

    const wrapper = document.createElement('div');
    wrapper.className = 'tier-item-wrapper';
    wrapper.id = cardData.id;
    if (cardData.style) {
      wrapper.setAttribute('style', cardData.style);
    }

    let cardEl;

    if (cardData.src && cardData.src.startsWith('card://')) {
      // card:// センチネル: 再解決結果を使って描画
      const name = cardData.src.slice('card://'.length);
      const displayResult = resolved.get(name) || { kind: 'none' };

      if (displayResult.kind === 'image' && displayResult.url) {
        // 発売済み or linked 後の正規画像
        const img = document.createElement('img');
        img.src = displayResult.url;
        img.className = 'tier-item';
        img.setAttribute('draggable', 'true');
        img.id = cardData.id.split(' ')[0];
        if (cardData.cardName) img.dataset.cardName = cardData.cardName;
        cardEl = img;
        // 発売済みクリーン画像にのみ透かし（SAMPLE画像は除外）
        if (displayResult.source !== 'official_sample') wrapper.classList.add('wm-released');
      } else if (displayResult.kind === 'proxy' && displayResult.proxy) {
        // 未発売プロキシ（まだ発売されていない）
        const proxyEl = createProxyCardElement(displayResult.proxy);
        proxyEl.setAttribute('draggable', 'true');
        proxyEl.id = cardData.id.split(' ')[0];
        if (cardData.cardName) proxyEl.dataset.cardName = cardData.cardName;
        cardEl = proxyEl;
      } else {
        // kind='none'（却下・削除済み等）: カード名のみのフォールバックプロキシ
        const fallbackProxy = { name: name || cardData.cardName || '不明なカード' };
        const proxyEl = createProxyCardElement(fallbackProxy);
        proxyEl.setAttribute('draggable', 'true');
        proxyEl.id = cardData.id.split(' ')[0];
        if (cardData.cardName) proxyEl.dataset.cardName = cardData.cardName;
        cardEl = proxyEl;
      }
    } else {
      // 通常の img（実URL）: 従来どおり。保存済みの実URLは発売済みクリーン画像
      const img = document.createElement('img');
      // img.src は常に元画像（裏面は CSS ::after で描画するため src は変更しない）
      img.src = cardData.src;
      img.className = 'tier-item';
      img.setAttribute('draggable', 'true');
      img.id = cardData.id.split(' ')[0];
      if (cardData.cardName) img.dataset.cardName = cardData.cardName;
      cardEl = img;
      wrapper.classList.add('wm-released');
    }

    // イベントリスナーを再設定（dblclick による効果発動含む）
    attachCardImageListeners(wrapper, cardEl);

    wrapper.appendChild(cardEl);
    slot.appendChild(wrapper);

    // カード状態を復元（守備表示・セット）
    if (cardData.state) {
      applyCardState(wrapper, cardData.state);
    }
  }
}

/**
 * カウンターを復元
 * @param {Array} counters - カウンター情報の配列
 */
function restoreCounters(counters) {
  if (!counters) return;

  // 既存のカウンターをクリア（親要素のもの以外）
  const existingCounters = document.querySelectorAll('.counter-container:not(#parent)');
  existingCounters.forEach(counter => counter.remove());

  // カウンターを復元
  counters.forEach(counterData => {
    // 親となるスロットを検索
    let parentSlot = null;
    if (counterData.parentDataSlot) {
      parentSlot = document.querySelector(`[data-slot="${counterData.parentDataSlot}"]`);
    } else if (counterData.parentId) {
      parentSlot = document.querySelector(`.${counterData.parentId.split(' ')[0]}`);
    }

    if (!parentSlot) return;

    // カウンターを作成
    const counter = document.createElement('div');
    counter.className = 'counter-container';
    counter.setAttribute('draggable', 'true');
    if (counterData.style) {
      counter.setAttribute('style', counterData.style);
    }

    const textbox = document.createElement('input');
    textbox.type = 'text';
    textbox.className = 'counter-textbox';
    textbox.value = counterData.value;
    textbox.readOnly = true;

    const isOnCard = parentSlot.classList.contains('tier-item-wrapper');

    if (isOnCard) {
      // カード上: [−][数値][+] のフラット構造
      const downBtn = document.createElement('button');
      downBtn.className = 'triangle-button down';
      downBtn.textContent = '−';

      const upBtn = document.createElement('button');
      upBtn.className = 'triangle-button up';
      upBtn.textContent = '+';

      counter.appendChild(downBtn);
      counter.appendChild(textbox);
      counter.appendChild(upBtn);
    } else {
      // パネル内: ボタンコンテナ（縦積み）＋数値
      const buttonContainer = document.createElement('div');
      buttonContainer.className = 'triangle-button-container';

      const upButton = document.createElement('button');
      upButton.className = 'triangle-button up';
      upButton.textContent = '+';

      const downButton = document.createElement('button');
      downButton.className = 'triangle-button down';
      downButton.textContent = '−';

      buttonContainer.appendChild(upButton);
      buttonContainer.appendChild(downButton);
      counter.appendChild(buttonContainer);
      counter.appendChild(textbox);
    }

    // イベントリスナーを再設定
    counter.addEventListener('dragstart', window.drag);

    parentSlot.appendChild(counter);
  });
}

/**
 * ゲーム状態をロード（非同期版: card:// センチネルの再解決を含む）
 * @param {number} slotNumber - ロードスロット番号（1-5）
 * @returns {Promise<boolean>} - ロード成功/失敗
 */
export async function loadGameState(slotNumber) {
  if (slotNumber < 1 || slotNumber > MAX_SLOTS) {
    throw new Error('無効なスロット番号です');
  }

  try {
    const key = SAVE_KEY_PREFIX + slotNumber;
    const data = localStorage.getItem(key);

    if (!data) {
      throw new Error('セーブデータが見つかりません');
    }

    // 圧縮データを解凍
    const decompressed = LZString.decompress(data);

    // 旧形式（圧縮されていない）との互換性を保つ
    const gameState = JSON.parse(decompressed || data);

    await _applyState(gameState);

    console.log(`ゲーム状態をスロット${slotNumber}からロードしました`);
    return true;
  } catch (error) {
    console.error('ロードエラー:', error);
    throw error;
  }
}

/**
 * 保存されているスロット情報を取得
 * @returns {Array} - スロット情報の配列
 */
export function getSaveSlots() {
  const slots = [];

  for (let i = 1; i <= MAX_SLOTS; i++) {
    const key = SAVE_KEY_PREFIX + i;
    const data = localStorage.getItem(key);

    if (data) {
      try {
        // 圧縮データを解凍（旧形式との互換性も保つ）
        const decompressed = LZString.decompress(data);
        const gameState = JSON.parse(decompressed || data);
        const date = new Date(gameState.timestamp);
        slots.push({
          number: i,
          exists: true,
          timestamp: gameState.timestamp,
          dateString: date.toLocaleString('ja-JP'),
          slotName: gameState.slotName || `スロット ${i}`,
        });
      } catch {
        slots.push({
          number: i,
          exists: false,
          slotName: `スロット ${i}`,
        });
      }
    } else {
      slots.push({
        number: i,
        exists: false,
        slotName: `スロット ${i}`,
      });
    }
  }

  return slots;
}

/**
 * スロット名を更新
 * @param {number} slotNumber - スロット番号（1-5）
 * @param {string} newName - 新しいスロット名
 */
export function updateSlotName(slotNumber, newName) {
  if (slotNumber < 1 || slotNumber > MAX_SLOTS) {
    throw new Error('無効なスロット番号です');
  }

  const key = SAVE_KEY_PREFIX + slotNumber;
  const data = localStorage.getItem(key);

  if (!data) {
    throw new Error('セーブデータが見つかりません');
  }

  try {
    // 圧縮データを解凍（旧形式との互換性も保つ）
    const decompressed = LZString.decompress(data);
    const gameState = JSON.parse(decompressed || data);
    gameState.slotName = newName || `スロット ${slotNumber}`;

    // 圧縮して保存
    const compressed = LZString.compress(JSON.stringify(gameState));
    localStorage.setItem(key, compressed);
    console.log(`スロット${slotNumber}の名前を「${gameState.slotName}」に変更しました`);
    return true;
  } catch (error) {
    console.error('スロット名更新エラー:', error);
    throw error;
  }
}

/**
 * セーブスロットをクリア
 * @param {number} slotNumber - クリアするスロット番号（1-5）
 */
export function clearSaveSlot(slotNumber) {
  if (slotNumber < 1 || slotNumber > MAX_SLOTS) {
    throw new Error('無効なスロット番号です');
  }

  const key = SAVE_KEY_PREFIX + slotNumber;
  localStorage.removeItem(key);
  console.log(`スロット${slotNumber}をクリアしました`);
}

// ── セッション復元（ページ遷移をまたいだ盤面の自動保存・復元） ──

/**
 * 現在の盤面状態をsessionStorageに保存（pagehide時に呼ぶ）
 * デッキが空なら保存しない
 */
export function saveSessionResume() {
  try {
    const state = _captureCurrentState();
    const hasContent = (
      state.slots.poolRow?.length > 0 ||
      state.slots.poolRow2?.length > 0 ||
      Object.keys(state.slots).some(k => k.startsWith('custom-slot-') && state.slots[k].length > 0) ||
      state.slots['center-slot']?.length > 0
    );
    if (!hasContent) return;

    sessionStorage.setItem(SESSION_RESUME_KEY, LZString.compress(JSON.stringify(state)));
  } catch (e) {
    console.warn('セッション保存失敗:', e);
  }
}

/**
 * sessionStorageから盤面を復元して盤面に反映する（非同期版）
 * 呼び出し後にデータは削除される
 * @returns {Promise<boolean>} 復元できたか
 */
export async function loadSessionResume() {
  try {
    const raw = sessionStorage.getItem(SESSION_RESUME_KEY);
    if (!raw) return false;
    sessionStorage.removeItem(SESSION_RESUME_KEY);

    const state = JSON.parse(LZString.decompress(raw));
    await _applyState(state);
    return true;
  } catch (e) {
    console.warn('セッション復元失敗:', e);
    sessionStorage.removeItem(SESSION_RESUME_KEY);
    return false;
  }
}

function _captureCurrentState() {
  const state = { slots: {}, counters: getCounterStates() };
  state.slots.poolRow  = getCardsFromSlot(document.getElementById('poolRow'));
  state.slots.poolRow2 = getCardsFromSlot(document.getElementById('poolRow2'));
  document.querySelectorAll('.custom-slot').forEach(slot => {
    const n = slot.getAttribute('data-slot');
    if (n) state.slots[`custom-slot-${n}`] = getCardsFromSlot(slot);
  });
  document.querySelectorAll('.side-slot').forEach((slot, i) => {
    state.slots[`side-slot-${i}`] = getCardsFromSlot(slot);
  });
  const center = document.querySelector('.center-slot');
  if (center) state.slots['center-slot'] = getCardsFromSlot(center);
  const free = document.getElementById('free-space');
  if (free) state.slots['free-space'] = getCardsFromSlot(free);
  return state;
}

async function _applyState(state) {
  // 全スロットを並列復元（card:// 解決の待機がスロット数分重ならないよう Promise.all を使う）
  const tasks = [];

  tasks.push(restoreCardsToSlot(document.getElementById('poolRow'),  state.slots.poolRow));
  tasks.push(restoreCardsToSlot(document.getElementById('poolRow2'), state.slots.poolRow2));

  document.querySelectorAll('.custom-slot').forEach(slot => {
    const n = slot.getAttribute('data-slot');
    if (n) tasks.push(restoreCardsToSlot(slot, state.slots[`custom-slot-${n}`]));
  });
  document.querySelectorAll('.side-slot').forEach((slot, i) => {
    tasks.push(restoreCardsToSlot(slot, state.slots[`side-slot-${i}`]));
  });

  const center = document.querySelector('.center-slot');
  if (center && state.slots['center-slot']) {
    tasks.push(restoreCardsToSlot(center, state.slots['center-slot']));
  }
  const free = document.getElementById('free-space');
  if (free && state.slots['free-space']) {
    tasks.push(restoreCardsToSlot(free, state.slots['free-space']));
  }

  await Promise.all(tasks);
  restoreCounters(state.counters);
}
