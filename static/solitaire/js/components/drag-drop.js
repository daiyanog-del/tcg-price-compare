import { initializeCounter } from './counter-manager.js';
import { applyDefense, applySet, toggleDefense, getCardState } from './card-state.js';
import { openCardContextMenu } from '../ui/context-menu.js';

/**
 * ドラッグ&ドロップ統合管理
 * デスクトップとタッチデバイスの両方に対応
 *
 * 修飾キー対応（Shift/Ctrl + ドロップ）:
 *   Shift のみ       … 守備表示トグル
 *   Shift + Ctrl     … セット（裏側表示）
 */

// ドロップゾーンの種類
const DROP_ZONE_TYPES = {
  POOL: 'tier-row',          // デッキ/EXデッキ
  CUSTOM_SLOT: 'custom-slot', // フィールドスロット
  CENTER_SLOT: 'center-slot', // 手札
  SIDE_SLOT: 'side-slot',     // 墓地/除外
};

/**
 * ドラッグ対象の種類を判定
 * @param {Element} element - ドラッグされた要素
 * @returns {Object} - {type: 'card'|'counter', element: Element}
 */
function getDraggedElementInfo(element) {
  const tierItem = element.closest('.tier-item-wrapper');
  if (tierItem) {
    return { type: 'card', element: tierItem };
  }

  const counter = element.closest('.counter-container');
  if (counter) {
    return { type: 'counter', element: counter };
  }

  return null;
}

/**
 * ドロップ先の情報を取得
 * @param {Element} target - ドロップ先の要素
 * @returns {Object|null} - {type: string, element: Element}
 */
function getDropZoneInfo(target) {
  for (const [type, className] of Object.entries(DROP_ZONE_TYPES)) {
    const element = target.closest(`.${className}`);
    if (element) {
      return { type, element };
    }
  }
  return null;
}

/**
 * カードのスタイルをリセット
 * @param {Element} card - カード要素
 * @param {boolean} resetTop - topプロパティもリセットするか
 */
function resetCardStyle(card, resetTop = true) {
  card.style.position = '';
  card.style.left = '';
  card.style.zIndex = '';
  if (resetTop) {
    card.style.top = '';
  }
}

/**
 * カスタムスロットにカードを配置（重ね配置）
 * @param {Element} slot  - スロット要素
 * @param {Element} card  - カード要素
 * @param {Object}  [opts]
 * @param {boolean} [opts.under=false] - true=最背面（下重ね）に配置
 */
function placeCardInCustomSlot(slot, card, { under = false } = {}) {
  const existingItems = Array.from(slot.querySelectorAll('.tier-item-wrapper'));

  // 同じ場所にドロップした場合は何もしない
  if (existingItems.length === 1 && existingItems[0] === card) {
    return;
  }

  const baseZIndex = 1;

  if (under && existingItems.length > 0) {
    // 下重ね: 既存カードを 2..N に底上げ、新規カードを 1（最背面）に
    const others = existingItems.filter(el => el !== card);
    // 既存カードを1段ずつ下へずらし、差し込むカードをスロット上端に置く。
    // 通常積みと同じ方向（上端に古いカード、下へ向かって新しいカード）になる。
    others.forEach((item, index) => {
      item.style.position = 'absolute';
      item.style.zIndex   = String(baseZIndex + 1 + index);
      item.style.top      = `calc(var(--slot-width) * 0.${index + 1})`;
    });
    card.style.position = 'absolute';
    card.style.zIndex   = String(baseZIndex);
    card.style.top      = '0';
    slot.insertBefore(card, slot.firstChild);
  } else {
    // 通常（上重ね）: 既存カードを整列し、新規カードを最前面に
    existingItems.forEach((item, index) => {
      item.style.position = 'absolute';
      item.style.zIndex   = `${baseZIndex + index}`;
    });

    card.style.position = 'absolute';
    card.style.top      = `calc(var(--slot-width) * 0.${existingItems.length})`;
    card.style.zIndex   = `${baseZIndex + existingItems.length}`;
    slot.appendChild(card);
  }
}

/**
 * プールにカードを配置（並び替え）
 * @param {Element} pool - プール要素
 * @param {Element} card - カード要素
 * @param {Element} targetCard - ドロップ先のカード要素（null可）
 */
function placeCardInPool(pool, card, targetCard) {
  resetCardStyle(card);

  if (targetCard && targetCard !== card) {
    pool.insertBefore(card, targetCard);
  } else {
    pool.appendChild(card);
  }
}

/**
 * 通常スロットにカードを配置
 * @param {Element} slot - スロット要素
 * @param {Element} card - カード要素
 */
function placeCardInNormalSlot(slot, card) {
  resetCardStyle(card);
  slot.appendChild(card);
}

/**
 * カウンターをカードに配置
 * @param {Element} card - カード要素
 * @param {Element} counter - カウンター要素
 */
function placeCounterOnCard(card, counter) {
  const srcTextbox = counter.querySelector('.counter-textbox');
  const currentValue = srcTextbox ? srcTextbox.value : '1';

  // カード上では [−][N][+] のフラット構造に再構成
  const newCounter = document.createElement('div');
  newCounter.className = 'counter-container';
  newCounter.id = `clonedCounter-${Date.now()}`;

  const downBtn = document.createElement('button');
  downBtn.className = 'triangle-button down';
  downBtn.textContent = '−';

  const textbox = document.createElement('input');
  textbox.type = 'text';
  textbox.className = 'counter-textbox';
  textbox.value = currentValue;
  textbox.readOnly = true;

  const upBtn = document.createElement('button');
  upBtn.className = 'triangle-button up';
  upBtn.textContent = '+';

  newCounter.appendChild(downBtn);
  newCounter.appendChild(textbox);
  newCounter.appendChild(upBtn);

  initializeCounter(newCounter);
  card.appendChild(newCounter);
}

/**
 * ゾーン要素からzoneIdを取得
 * @param {Element} zoneElement
 * @returns {string}
 */
export function getZoneId(zoneElement) {
  const cls = zoneElement.className || '';
  if (zoneElement.id === 'poolRow') return 'poolRow';
  if (zoneElement.id === 'poolRow2') return 'poolRow2';
  if (cls.includes('center-slot')) return 'center-slot';
  if (cls.includes('custom-slot')) {
    const slot = zoneElement.getAttribute('data-slot');
    return slot ? `custom-slot-${slot}` : 'custom-slot-?';
  }
  if (cls.includes('side-slot')) {
    // free-space内か外かで判別
    if (zoneElement.closest('#free-space')) return 'free-space';
    const allSideSlots = Array.from(document.querySelectorAll('.side-slot'));
    const idx = allSideSlots.indexOf(zoneElement);
    return idx >= 0 ? `side-slot-${idx}` : 'side-slot-?';
  }
  return 'unknown';
}

/**
 * ドロップ処理の実行
 * @param {Object} draggedInfo  - ドラッグ対象情報
 * @param {Object} dropZoneInfo - ドロップ先情報
 * @param {Element} dropTarget  - 具体的なドロップ先要素
 * @param {Object} [modifiers]  - 修飾キー {shift, ctrl}
 */
function executeDrop(draggedInfo, dropZoneInfo, dropTarget, modifiers = {}) {
  const { type: dragType, element: draggedElement } = draggedInfo;

  // カウンターのドロップ処理
  if (dragType === 'counter') {
    const targetCard = dropTarget.closest('.tier-item-wrapper');
    if (targetCard) {
      placeCounterOnCard(targetCard, draggedElement);
    }
    return;
  }

  // カードのドロップ処理
  if (dragType === 'card') {
    const { type: dropType, element: dropZone } = dropZoneInfo;
    let zIndex;

    switch (dropType) {
      case 'POOL': {
        const targetCard = dropTarget.closest('.tier-item-wrapper');
        placeCardInPool(dropZone, draggedElement, targetCard);
        break;
      }
      case 'CUSTOM_SLOT': {
        const before = dropZone.querySelectorAll('.tier-item-wrapper').length;
        // Ctrl単独=下重ね / Shift+Ctrl=セット（下重ねではない）
        const under = modifiers.ctrl && !modifiers.shift;
        placeCardInCustomSlot(dropZone, draggedElement, { under });
        zIndex = under ? '1' : String(before + 1);

        // 修飾キーによる状態変更（優先順位: Shift+Ctrl=セット > Shift=守備）
        // 初期配置なので状態は即時適用（アニメはリプレイ再生側が担う）
        if (modifiers.shift && modifiers.ctrl) {
          applySet(draggedElement, true);
        } else if (modifiers.shift) {
          toggleDefense(draggedElement);
        }
        break;
      }
      case 'CENTER_SLOT':
      case 'SIDE_SLOT':
        placeCardInNormalSlot(dropZone, draggedElement);
        break;
    }

    // リプレイログ記録
    if (typeof window.replayLog === 'function') {
      const cardId = draggedElement.querySelector('img')?.id;
      if (cardId) {
        const state = getCardState(draggedElement);
        window.replayLog({
          actionType:  'moveCard',
          cardId,
          zoneId:      getZoneId(dropZone),
          zIndex:      zIndex ?? '1',
          transform:   draggedElement.style.transform || '',
          orientation: state.orientation,
          face:        state.face,
        });
      }
    }
  }
}

/**
 * デスクトップドラッグ&ドロップの初期化
 */
export function initializeDesktopDragDrop() {
  // allowDrop（Ctrl押下時のコピーカーソルを move に固定）
  window.allowDrop = function(ev) {
    ev.preventDefault();
    ev.dataTransfer.dropEffect = 'move';
  };

  // drag（effectAllowed で常に move）
  window.drag = function(ev) {
    ev.dataTransfer.setData('text/plain', ev.target.id);
    ev.dataTransfer.effectAllowed = 'move';
  };

  // drop（Shift/Ctrl 修飾キーを executeDrop に渡す）
  window.drop = function(ev) {
    ev.preventDefault();

    const id = ev.dataTransfer.getData('text/plain');
    const draggedElement = document.getElementById(id);
    if (!draggedElement) return;

    const draggedInfo = getDraggedElementInfo(draggedElement);
    if (!draggedInfo) return;

    const dropZoneInfo = getDropZoneInfo(ev.target);
    if (!dropZoneInfo) return;

    const modifiers = { shift: ev.shiftKey, ctrl: ev.ctrlKey || ev.metaKey };
    executeDrop(draggedInfo, dropZoneInfo, ev.target, modifiers);
  };
}

/**
 * タッチドラッグ&ドロップの初期化（長押し=コンテキストメニュー対応）
 * @param {TouchEvent} ev - touchstartイベント
 */
export function enableTouchDrag(ev) {
  ev.preventDefault();

  const draggedInfo = getDraggedElementInfo(ev.target);
  if (!draggedInfo) return;

  const { element: draggingElem } = draggedInfo;
  const touch0 = ev.touches[0];
  const startX = touch0.clientX;
  const startY = touch0.clientY;
  let isDragging   = false;
  let shouldResetTop = true;

  // カードの長押しタイマー（500ms でコンテキストメニューを開く）
  let longPressTimer = null;
  if (draggedInfo.type === 'card') {
    longPressTimer = setTimeout(() => {
      longPressTimer = null;
      if (!isDragging) {
        // ドラッグ開始前に指が止まっていた → 長押しと判定してメニュー表示
        draggingElem.classList.remove('touch-dragging');
        resetCardStyle(draggingElem, true);
        document.removeEventListener('touchmove', handleTouchMove);
        document.removeEventListener('touchend', handleTouchEnd);
        const img = draggingElem.querySelector('img.tier-item');
        if (img) openCardContextMenu(draggingElem, img, startX, startY);
      }
    }, 500);
  }

  // タッチ移動時の処理
  const handleTouchMove = (ev) => {
    const touch = ev.touches[0];
    const dx = touch.clientX - startX;
    const dy = touch.clientY - startY;

    // 8px以上動いたらドラッグ開始（タイマーキャンセル）
    if (!isDragging && (Math.abs(dx) > 8 || Math.abs(dy) > 8)) {
      isDragging = true;
      clearTimeout(longPressTimer);
      longPressTimer = null;
    }

    if (isDragging) {
      draggingElem.classList.add('touch-dragging');
      draggingElem.style.left = `${touch.clientX - draggingElem.offsetWidth / 2}px`;
      draggingElem.style.top  = `${touch.clientY - draggingElem.offsetHeight / 2}px`;
    }
  };

  // タッチ終了時の処理
  const handleTouchEnd = (ev) => {
    clearTimeout(longPressTimer);
    longPressTimer = null;

    if (isDragging) {
      const touch = ev.changedTouches[0];
      const dropTarget = document.elementFromPoint(touch.clientX, touch.clientY);

      if (dropTarget) {
        const dropZoneInfo = getDropZoneInfo(dropTarget);
        if (dropZoneInfo) {
          // タッチは修飾キーなし（状態変更は長押しメニューを使う）
          shouldResetTop = dropZoneInfo.type !== 'CUSTOM_SLOT';
          executeDrop(draggedInfo, dropZoneInfo, dropTarget, {});
        }
      }
    }

    // クリーンアップ
    draggingElem.classList.remove('touch-dragging');
    resetCardStyle(draggingElem, shouldResetTop);
    document.removeEventListener('touchmove', handleTouchMove);
    document.removeEventListener('touchend', handleTouchEnd);
  };

  document.addEventListener('touchmove', handleTouchMove, { passive: false });
  document.addEventListener('touchend', handleTouchEnd);
}
