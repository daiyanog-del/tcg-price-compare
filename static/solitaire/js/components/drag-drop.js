import { initializeCounter } from './counter-manager.js';
import { applyDefense, applySet, toggleDefense, getCardState } from './card-state.js';
import { openCardContextMenu, closeContextMenu } from '../ui/context-menu.js';
import { isMobilePortrait } from '../utils/viewport.js';

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

    // リプレイログ記録（img.tier-item または div.tier-item（プロキシ）どちらでも対応）
    if (typeof window.replayLog === 'function') {
      const cardId = draggedElement.querySelector('.tier-item')?.id;
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
 * 指位置から最近傍の有効ドロップゾーンを探す。
 * タッチはスロット間ギャップや空白グリッドセルに当たりやすいため、
 * 直接ヒットしなかった場合に周囲 ~20px を8方向探索してフォールバックする。
 * @param {number} x - clientX
 * @param {number} y - clientY
 * @returns {Element|null}
 */
function findNearestDropZone(x, y) {
  // 1. 指の正確な位置で試す
  const direct = document.elementFromPoint(x, y);
  if (direct && getDropZoneInfo(direct)) return direct;

  // 2. 周囲8方向 (~20px) を探索してギャップを補完
  const R = 20;
  const offsets = [
    [0, -R], [0, R], [-R, 0], [R, 0],       // 上下左右
    [-R * 0.7, -R * 0.7], [R * 0.7, -R * 0.7],
    [-R * 0.7,  R * 0.7], [R * 0.7,  R * 0.7], // 斜め45°
  ];
  for (const [dx, dy] of offsets) {
    const el = document.elementFromPoint(x + dx, y + dy);
    if (el && getDropZoneInfo(el)) return el;
  }
  return direct; // 見つからなければ元の要素を返す（ドロップは失敗扱い）
}

/**
 * タッチドラッグ&ドロップの初期化（長押し=コンテキストメニュー対応）
 * @param {TouchEvent} ev - touchstartイベント
 */
export function enableTouchDrag(ev) {
  const draggedInfo = getDraggedElementInfo(ev.target);
  if (!draggedInfo) return;

  // スマホ縦向き かつ カードの場合のみ §8.3 の新しい状態機械を使う。
  // カウンター、PC/タブレット、横向きスマホは従来どおり以下の処理を使う。
  if (isMobilePortrait() && draggedInfo.type === 'card') {
    // documentへバブリングさせない（非カード要素向けの選択後タップ判定リスナーを誤発火させないため）
    ev.stopPropagation();
    _handleMobileCardTouchStart(ev, draggedInfo.element);
    return;
  }

  // デッキ/EXデッキ/手札エリアでは touchstart の preventDefault をスキップし横スクロールを許可。
  // ドラッグ開始後（8px 移動検知時）の handleTouchMove で preventDefault を呼ぶ。
  const inScrollZone = ev.currentTarget?.closest?.('#imagePool, #imagePool2, .sol-hand-area');
  if (!inScrollZone) ev.preventDefault();

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
        // img.tier-item または div.tier-item（プロキシ）どちらでも .tier-item で取れる
        const cardEl = draggingElem.querySelector('.tier-item');
        if (cardEl) openCardContextMenu(draggingElem, cardEl, startX, startY);
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
      // ドラッグ中はページ/プールのスクロールを防止
      // (touchstart で preventDefault をスキップしたプールカードでも、
      //  ドラッグ開始後はここで止める)
      ev.preventDefault();
      draggingElem.classList.add('touch-dragging');
      draggingElem.style.left = `${touch.clientX - draggingElem.offsetWidth / 2}px`;
      // 指がカード下端付近に来るよう少し浮かせる。
      // 判定はカードの視覚的下端(= clientY)を使うため、カードをスロットに当てる直感が正しい。
      draggingElem.style.top  = `${touch.clientY - draggingElem.offsetHeight}px`;
    }
  };

  // タッチ終了時の処理
  const handleTouchEnd = (ev) => {
    clearTimeout(longPressTimer);
    longPressTimer = null;

    if (isDragging) {
      const touch = ev.changedTouches[0];
      // 判定位置: カードの視覚的下端(= clientY) でスロットを探す。
      // top = clientY - offsetHeight なのでカード下端 = clientY。
      // 「カードをスロットに当てる」操作がそのまま正確なドロップ位置になる。
      const dropTarget = findNearestDropZone(touch.clientX, touch.clientY);

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

/* ════════════════════════════════════════════════════════════════
 * スマホ縦向き専用タップ状態機械（設計書 §8.3、reviewer監査 B系・H/M/L系修正込み）
 * idle → pressed(touching) → dragging（従来のドラッグ）
 *                          → selected（移動なしtouchend）→ idle
 * executeDrop / getDropZoneInfo は既存のものをそのまま使い、判定を二重に持たない。
 * ════════════════════════════════════════════════════════════════ */

// モジュールスコープの選択状態（touchstartごとの一時クロージャとは別に、
// 「selected」は次の操作まで持続するためモジュールレベルで管理する）
let _mState     = 'idle'; // 'idle' | 'pressed' | 'dragging' | 'selected'
let _mWrapper   = null;
let _touchLabelEl = null;

// H-2: 選択中に別ゾーンのカードへ touchstart した際の「保留中のドロップ」。
// touchstart 即 executeDrop はせず、移動なし touchend で確定・8px超で破棄する。
// getMobileSelection() から見えるようにモジュールスコープで持つ。
let _pendingDropWrapper = null;
let _pendingDropZone    = null;

// H-3: pressed/dragging セッションは同時に1本だけを許可する。
// 別カードへの新しい touchstart が来たら、前セッションのクリーンアップをここから強制実行する。
let _activeSessionCleanup = null;

/** 置ける先として §8.3 が指定する要素セレクタ（drop-hint の対象）。L-3: #poolRow は縦向きで非表示のため除外 */
const MOBILE_DROP_HINT_SELECTOR = '.custom-slot, .center-slot, .side-slot';

/** touchstart 時点で preventDefault をスキップし横スクロールを許可する領域（B-4: 墓地/除外も対象に含める） */
const MOBILE_SCROLL_ZONE_SELECTOR = '#imagePool, #imagePool2, .sol-hand-area, .side-slot';

/** 選択解除時に無視する（選択を保持したままにする）UI要素のセレクタ（M-5: カード上のカウンターも追加） */
const MOBILE_UI_IGNORE_SELECTOR =
  '#solMobileActionSheet, #solMobileCip, #solMobileBar, ' +
  '#solMobileMenuSheet, .sol-context-menu, #solSidebar, #solSidebarScrim, #opponentTray, ' +
  '.pool-label, #replayBarContainer, .sol-ops-details, .sol-mobile-help-scrim, .counter-container';

/**
 * カード名を取得する。
 * 既存の名前解決（dataset.cardName）を最優先にし、指示どおり img.tier-item の alt、
 * プロキシ要素の名前表示（実装上のクラス名は .proxy-card__name。設計書は .proxy-name と
 * 表記しているが実装に存在しないため、実在するクラス名で代替する）の順にフォールバックする。
 * @param {Element} wrapper - .tier-item-wrapper
 * @returns {string}
 */
function getMobileCardDisplayName(wrapper) {
  const cardEl = wrapper?.querySelector('.tier-item');
  if (!cardEl) return '';
  if (cardEl.dataset && cardEl.dataset.cardName) return cardEl.dataset.cardName;
  if (cardEl.tagName === 'IMG' && cardEl.alt) return cardEl.alt;
  const nameEl = cardEl.querySelector('.proxy-card__name');
  if (nameEl) return nameEl.textContent || '';
  return '';
}

function ensureTouchLabel() {
  if (_touchLabelEl && document.body.contains(_touchLabelEl)) return _touchLabelEl;
  const el = document.createElement('div');
  el.id = 'solTouchLabel';
  document.body.appendChild(el);
  _touchLabelEl = el;
  return el;
}

/** 名前ラベルを指の上40pxに表示する（XSS対策: textContent を使用） */
function showTouchLabel(wrapper, x, y) {
  const el = ensureTouchLabel();
  el.textContent = getMobileCardDisplayName(wrapper) || '名称不明';
  el.style.left = `${x}px`;
  el.style.top  = `${y - 40}px`;
  el.style.display = 'block';
}
function moveTouchLabel(x, y) {
  if (!_touchLabelEl) return;
  _touchLabelEl.style.left = `${x}px`;
  _touchLabelEl.style.top  = `${y - 40}px`;
}
function hideTouchLabel() {
  if (_touchLabelEl) _touchLabelEl.style.display = 'none';
}

/**
 * B-9: 「持ち上げ」表現を確実に見せる。
 * .touching クラスの transform:scale(1.15) はインライン style.transform（リプレイ再生後の
 * カードや重ね置きで style.zIndex が設定済みのケース）に優先度で負けて見えないことがあるため、
 * 既存のインライン transform/zIndex を退避したうえで合成し、解除時に復元する。
 * H-3: 同じカードに対して2回呼ばれても退避値を汚染しないよう冪等化する
 * （既に退避済み＝dataset.solOrigTransform が設定済みなら何もしない）。
 * @param {Element} wrapper - .tier-item-wrapper
 */
function liftTouching(wrapper) {
  if (wrapper.dataset.solOrigTransform !== undefined) return; // H-3: 冪等化
  wrapper.classList.add('touching');
  const origTransform = wrapper.style.transform || '';
  const origZIndex    = wrapper.style.zIndex || '';
  wrapper.dataset.solOrigTransform = origTransform;
  wrapper.dataset.solOrigZIndex    = origZIndex;
  wrapper.style.transform = `${origTransform} scale(1.15)`.trim();
  wrapper.style.zIndex    = '500';
}
/** liftTouching の復元（B-9） */
function unliftTouching(wrapper) {
  wrapper.classList.remove('touching');
  if (wrapper.dataset.solOrigTransform !== undefined) {
    wrapper.style.transform = wrapper.dataset.solOrigTransform;
    delete wrapper.dataset.solOrigTransform;
  }
  if (wrapper.dataset.solOrigZIndex !== undefined) {
    wrapper.style.zIndex = wrapper.dataset.solOrigZIndex;
    delete wrapper.dataset.solOrigZIndex;
  }
}

/**
 * B-10: 選択カード自身の現在ゾーンを drop-hint から除外する
 * （「手札のカードを選ぶと手札全体が点滅する」の解消。同じゾーンへの無意味な移動を光らせない）。
 * @param {Element|null} excludeZone - 選択カードの親要素（現在のゾーン）
 */
function showMobileDropHints(excludeZone) {
  document.querySelectorAll(MOBILE_DROP_HINT_SELECTOR).forEach(el => {
    if (el === excludeZone) return;
    el.classList.add('drop-hint');
  });
}
function hideMobileDropHints() {
  document.querySelectorAll('.drop-hint').forEach(el => el.classList.remove('drop-hint'));
}

/**
 * 残件4/6: 保留ドロップ（_pendingDropWrapper/_pendingDropZone）をクリアし、
 * 保留元カードの選択見た目（.selected）も外す。強制終了時（_activeSessionCleanup）と
 * H-1の resetMobileUI() の両方から呼ぶ共通関数。
 */
function clearPendingDrop() {
  if (_pendingDropWrapper) {
    _pendingDropWrapper.classList.remove('selected');
  }
  _pendingDropWrapper = null;
  _pendingDropZone = null;
}

/** pressed（移動なしtouchend）→ selected へ遷移する */
function selectMobileCard(wrapper) {
  // 残件4: 金枠（.selected）の二重残留を防ぐため、付ける前に全て外す
  document.querySelectorAll('.tier-item-wrapper.selected').forEach(el => el.classList.remove('selected'));
  _mState = 'selected';
  _mWrapper = wrapper;
  wrapper.classList.add('selected');
  showMobileDropHints(wrapper.parentElement);
  // 詳細パネル・アクションシートの表示は mobile-ui.js に委譲（疎結合にするため CustomEvent で通知）
  document.dispatchEvent(new CustomEvent('sol-mobile-card-selected', { detail: { wrapper } }));
}

/** selected を解除して idle へ戻す */
function clearMobileSelectionInternal() {
  const wasSelected = _mState === 'selected';
  if (_mWrapper) _mWrapper.classList.remove('selected');
  hideMobileDropHints();
  hideTouchLabel();
  _mState = 'idle';
  _mWrapper = null;
  if (wasSelected) document.dispatchEvent(new CustomEvent('sol-mobile-card-deselected'));
}

/**
 * カードへの touchstart（スマホ縦向き）を処理する。
 * idle → pressed。
 * selected(同じカード) → M-9: 即解除せず pressed フローに乗せる（移動なしで解除・8px超で通常ドラッグ・
 *   500ms静止で長押しメニュー、という分岐を「選択→長押しで削除」の導線のため成立させる）。
 * selected(同じゾーンの別カード) → 選択の付け替え(pressedへ)。
 * selected(別ゾーンのカード) → H-2: touchstart 時点では executeDrop しない。保留（pendingDrop）を
 *   記録して pressed フローに乗せ、移動なし touchend で確定・8px超の移動で破棄する。
 * @param {TouchEvent} ev
 * @param {Element} wrapper - .tier-item-wrapper
 */
function _handleMobileCardTouchStart(ev, wrapper) {
  if (ev.touches.length > 1) return; // H-3: マルチタッチの touchstart は無視

  // B-7: モバイル経路は stopPropagation するため、コンテキストメニューの
  // document closeOnOutside が発火しない。ここで明示的に閉じる。
  closeContextMenu();

  // H-3: 前の pressed/dragging セッションが残っていれば強制終了させる
  // （別カードを2本目の指で押して前のカードが持ち上がったまま残る事故の防止）。
  if (_activeSessionCleanup) {
    const cleanup = _activeSessionCleanup;
    _activeSessionCleanup = null;
    cleanup();
  }

  // pendingWrapper/pendingZone: このタッチセッション内だけで使うローカル変数。
  // 「touchstart 時点で選択中だったカード」を保持し、セッション終了時に
  // 保留ドロップの確定/破棄・選択状態の復元に使う（H-2）。
  let pendingWrapper = null;
  let pendingZone = null;

  if (_mState === 'selected') {
    if (_mWrapper === wrapper) {
      // M-9: 同じカードの再タップも pressed フローに乗せる（即解除しない）。
      // 見た目上いったん selected を解いて pressed に入り直す。
      wrapper.classList.remove('selected');
      hideMobileDropHints();
      _mState = 'idle';
      _mWrapper = null;
    } else {
      const currentZone = _mWrapper.parentElement;
      const targetZone   = wrapper.parentElement;
      if (targetZone !== currentZone) {
        // H-2: 別ゾーンのカードへの touchstart → 保留するだけ（即 executeDrop しない）。
        // 元の選択カード（pendingWrapper）の .selected 表示はここでは維持する。
        pendingWrapper = _mWrapper;
        pendingZone = targetZone;
        hideMobileDropHints();
        hideTouchLabel();
        _mState = 'idle';
        _mWrapper = null;
      } else {
        // 同じゾーン内の別カード → 選択を解除して pressed へ進む（付け替え）
        clearMobileSelectionInternal();
      }
    }
  }

  _pendingDropWrapper = pendingWrapper;
  _pendingDropZone = pendingZone;

  // B-4: 墓地/除外(.side-slot)を含むスクロール領域では touchstart 時点の preventDefault を
  // スキップし横スクロールを許可する。8px 超でドラッグ確定した後は handleMove 内で必ず止める。
  const inScrollZone = !!wrapper.closest(MOBILE_SCROLL_ZONE_SELECTOR);
  if (!inScrollZone) ev.preventDefault();

  _mState = 'pressed';
  _mWrapper = wrapper;

  const touch0 = ev.touches[0];
  const startX = touch0.clientX;
  const startY = touch0.clientY;
  let isDragging = false;
  let shouldResetTop = true;

  // .touching と名前ラベルは preventDefault の有無に関わらず必ず付ける（B-4）
  liftTouching(wrapper);
  showTouchLabel(wrapper, startX, startY);

  /**
   * H-2/M-10: 保留ドロップ(pendingWrapper)があれば選択状態(_mState/_mWrapper)へ復元し、
   * なければ共通のクリーンアップ関数を通して idle にする（直代入をやめて一貫性を保つ）。
   * pendingWrapper が既に DOM から外れていた場合（削除等）は idle にする。
   */
  function restorePendingSelection() {
    _pendingDropWrapper = null;
    _pendingDropZone = null;
    if (pendingWrapper && pendingWrapper.isConnected) {
      _mState = 'selected';
      _mWrapper = pendingWrapper;
      showMobileDropHints(pendingWrapper.parentElement); // 残件1: 保留破棄後も置ける先の点滅を戻す
    } else {
      clearMobileSelectionInternal();
    }
    pendingWrapper = null;
    pendingZone = null;
  }

  // 500ms 静止でコンテキストメニュー（現行どおり）
  let longPressTimer = setTimeout(() => {
    longPressTimer = null;
    if (!isDragging) {
      unliftTouching(wrapper);
      hideTouchLabel();
      document.removeEventListener('touchmove', handleMove);
      document.removeEventListener('touchend', handleEnd);
      document.removeEventListener('touchcancel', handleCancel);
      _activeSessionCleanup = null;
      // H-2: 長押しメニューを開く場合は保留ドロップは破棄しつつ、元の選択状態は復元する
      restorePendingSelection();
      const cardEl = wrapper.querySelector('.tier-item');
      // M-8/残件3: 長押しメニュー表示直後に発生しうる合成 touchend を1回だけ無効化し、
      // コンテキストメニューの closeOnOutside による即閉じを防ぐ。
      // 名前付き関数にして発火時に自分を明示的に remove し、1000ms 経っても未発火なら
      // フェイルセーフの setTimeout で remove する（{once:true} だけだと万一発火しない
      // 場合にリスナーが残留し、次の touchend を1回無意味に潰してしまう経路があった）。
      const suppressNextTouchEnd = (e) => {
        e.preventDefault();
        document.removeEventListener('touchend', suppressNextTouchEnd);
      };
      document.addEventListener('touchend', suppressNextTouchEnd, { once: true, passive: false });
      setTimeout(() => {
        document.removeEventListener('touchend', suppressNextTouchEnd);
      }, 1000);
      if (cardEl) openCardContextMenu(wrapper, cardEl, startX, startY);
    }
  }, 500);

  const handleMove = (mv) => {
    const touch = mv.touches[0];
    const dx = touch.clientX - startX;
    const dy = touch.clientY - startY;

    if (!isDragging && (Math.abs(dx) > 8 || Math.abs(dy) > 8)) {
      // 残件8: 横優勢の判定に非対称マージンを設ける（dx > dy*1.5 のときだけスクロールに譲る）。
      // 等号(dx>=dy)だと斜め45°の引き出しまでスクロール扱いになりドラッグできなかったため、
      // 「明確に横方向」のときだけスクロールに譲り、45°付近はドラッグ扱いにする。
      if (inScrollZone && Math.abs(dx) > Math.abs(dy) * 1.5) {
        // M-7: スクロール領域（墓地/除外・手札・EX）で横優勢の移動ならドラッグにせず、
        // pressed を解除してネイティブの横スクロールに任せる（preventDefault しない）。
        clearTimeout(longPressTimer);
        longPressTimer = null;
        unliftTouching(wrapper);
        hideTouchLabel();
        document.removeEventListener('touchmove', handleMove);
        document.removeEventListener('touchend', handleEnd);
        document.removeEventListener('touchcancel', handleCancel);
        _activeSessionCleanup = null;
        restorePendingSelection();
        return;
      }
      isDragging = true;
      _mState = 'dragging';
      clearTimeout(longPressTimer);
      longPressTimer = null;
      unliftTouching(wrapper);
      wrapper.classList.add('touch-dragging');
      // H-2: 8px超で動いたら保留ドロップは破棄する（元の選択の見た目は pendingWrapper に残したまま、
      // ドラッグ終了後 restorePendingSelection() で選択状態に戻す）。
      _pendingDropWrapper = null;
      _pendingDropZone = null;
    }

    if (isDragging) {
      // B-4/M-7: ドラッグ確定後は常に preventDefault（スクロール領域でもここで止める）
      mv.preventDefault();
      wrapper.style.left = `${touch.clientX - wrapper.offsetWidth / 2}px`;
      wrapper.style.top  = `${touch.clientY - wrapper.offsetHeight}px`;
      moveTouchLabel(touch.clientX, touch.clientY);
    }
  };

  const handleEnd = (ev2) => {
    clearTimeout(longPressTimer);
    longPressTimer = null;
    document.removeEventListener('touchmove', handleMove);
    document.removeEventListener('touchend', handleEnd);
    document.removeEventListener('touchcancel', handleCancel);
    _activeSessionCleanup = null;

    if (isDragging) {
      const touch = ev2.changedTouches[0];
      const dropTarget = findNearestDropZone(touch.clientX, touch.clientY);
      if (dropTarget) {
        const dropZoneInfo = getDropZoneInfo(dropTarget);
        if (dropZoneInfo) {
          shouldResetTop = dropZoneInfo.type !== 'CUSTOM_SLOT';
          executeDrop({ type: 'card', element: wrapper }, dropZoneInfo, dropTarget, {});
        }
      }
      wrapper.classList.remove('touch-dragging');
      resetCardStyle(wrapper, shouldResetTop);
      hideTouchLabel();
      restorePendingSelection();
    } else if (_mState === 'pressed') {
      unliftTouching(wrapper);
      hideTouchLabel();

      if (pendingWrapper && pendingZone) {
        // H-2: 移動なし touchend → 保留していたドロップを確定する
        ev2.preventDefault();
        const dropZoneInfo = getDropZoneInfo(pendingZone);
        const dropWrapper = pendingWrapper;
        const dropZoneEl = pendingZone;
        pendingWrapper = null;
        pendingZone = null;
        _pendingDropWrapper = null;
        _pendingDropZone = null;
        if (dropZoneInfo) {
          executeDrop({ type: 'card', element: dropWrapper }, dropZoneInfo, dropZoneEl, {});
        }
        dropWrapper.classList.remove('selected');
        hideMobileDropHints();
        _mState = 'idle';
        _mWrapper = null;
        document.dispatchEvent(new CustomEvent('sol-mobile-card-deselected'));
      } else {
        // 通常の selected への遷移（初回選択、または同じカード再タップ後の pressed）
        ev2.preventDefault();
        selectMobileCard(wrapper);
      }
    }
    // 長押しメニューが既に発火していた場合は上の restorePendingSelection() 済みなので何もしない
  };

  const handleCancel = () => {
    clearTimeout(longPressTimer);
    longPressTimer = null;
    document.removeEventListener('touchmove', handleMove);
    document.removeEventListener('touchend', handleEnd);
    document.removeEventListener('touchcancel', handleCancel);
    _activeSessionCleanup = null;
    // touchcancel でも持ち上げ状態を必ず外す（残留させない）
    unliftTouching(wrapper);
    wrapper.classList.remove('touch-dragging');
    resetCardStyle(wrapper, true);
    hideTouchLabel();
    restorePendingSelection(); // M-10: 直代入をやめ共通の復元関数を通す
  };

  document.addEventListener('touchmove', handleMove, { passive: false });
  document.addEventListener('touchend', handleEnd, { passive: false });
  document.addEventListener('touchcancel', handleCancel);

  // H-3: このセッションを強制終了するための関数をモジュールスコープへ登録
  // （次の touchstart が別カードに来たときに使う。持ち上げ・タイマー・リスナーを確実に片付ける）。
  _activeSessionCleanup = () => {
    clearTimeout(longPressTimer);
    document.removeEventListener('touchmove', handleMove);
    document.removeEventListener('touchend', handleEnd);
    document.removeEventListener('touchcancel', handleCancel);
    unliftTouching(wrapper);
    wrapper.classList.remove('touch-dragging');
    resetCardStyle(wrapper, true);
    hideTouchLabel();
    // 強制終了時は選択の復元をしない（呼び出し元の新しい touchstart が続けて状態を作るため）。
    // 残件4: ただし保留（pendingWrapper 由来の _pendingDropWrapper）は必ずクリアし、
    // 保留元カードの .selected も外す（残留した保留が次操作に持ち越されるのを防ぐ）。
    clearPendingDrop();
  };
}

/* ── B-3: selected 状態での「置ける先タップ」判定を常駐リスナー1本＋状態変数で行う ──
 * 以前は touchstart のたびに touchmove/touchend を動的 add/remove していたが、
 * touchcancel が未登録だったため（電話着信・通知バナー等での cancel）ハンドラがリークし、
 * 古いクロージャが次のタップで誤って executeDrop してしまうバグがあった（B-3）。
 * カード要素上の touchstart は enableTouchDrag 側で stopPropagation 済みのため
 * ここには届かない（同一 touchend で選択遷移と行き先判定が二重発火しないようにするため）。
 */
let _tapActive     = false;
let _tapStartX     = 0;
let _tapStartY     = 0;
let _tapMoved      = false;
let _tapTargetEl   = null;
let _tapIdentifier = null; // 残件2: touchstart した指の identifier（touchend の指一致確認用）

function _onDocTouchStart(ev) {
  if (_mState !== 'selected') return;
  const target = ev.target;
  // 残件2: マルチタッチと無視セレクタのどちらでも _tapActive=false に統一する
  // （無視セレクタ側だけ false に落とし、マルチタッチ側は何もしないという非対称があると、
  //   マルチタッチ中に古い _tapActive が残って次の判定を誤らせる経路があった）。
  if (ev.touches.length > 1 || target.closest?.(MOBILE_UI_IGNORE_SELECTOR)) {
    _tapActive = false;
    return;
  }
  if (_tapActive) return; // M-4: 既に tapActive 中の再 touchstart は無視（二重開始防止）
  const touch0 = ev.changedTouches[0]; // M-4: changedTouches[0] を使う
  _tapActive     = true;
  _tapStartX     = touch0.clientX;
  _tapStartY     = touch0.clientY;
  _tapMoved      = false;
  _tapTargetEl   = target;
  _tapIdentifier = touch0.identifier;
}
function _onDocTouchMove(ev) {
  if (!_tapActive) return;
  const t = ev.touches[0];
  if (Math.abs(t.clientX - _tapStartX) > 8 || Math.abs(t.clientY - _tapStartY) > 8) {
    _tapMoved = true;
  }
}
function _onDocTouchEnd(ev) {
  if (!_tapActive) return;
  // 残件2: touchstart した指の identifier が今回の changedTouches に含まれる場合のみ処理する。
  // 含まれなければ「別の指」の touchend なので無視する（状態は変えずそのまま待つ）。
  // これがないと、2本指でタップしたときに親指側の touchend で人差し指が触れていたスロットへ
  // 誤ってドロップしてしまう経路があった。
  const matchingTouch = Array.from(ev.changedTouches).find(t => t.identifier === _tapIdentifier);
  if (!matchingTouch) return;

  _tapActive = false;
  if (_tapMoved || _mState !== 'selected') return;

  // L-8: タップ対象要素が既に DOM から切り離されていればドロップしない
  if (!_tapTargetEl || !_tapTargetEl.isConnected) return;

  // B-5: 実際に置ける先（getDropZoneInfo が非null）で executeDrop した場合のみ preventDefault する。
  // 空き地タップは選択解除のみ行い、合成 click は通す（縦タブの全幅展開・操作方法の閉じる等を阻害しないため）。
  const dropZoneInfo = getDropZoneInfo(_tapTargetEl);
  if (dropZoneInfo) {
    ev.preventDefault();
    executeDrop({ type: 'card', element: _mWrapper }, dropZoneInfo, _tapTargetEl, {});
  }
  clearMobileSelectionInternal();
}
function _onDocTouchCancel() {
  _tapActive = false;
}

document.addEventListener('touchstart',  _onDocTouchStart,  { passive: true });
document.addEventListener('touchmove',   _onDocTouchMove,   { passive: true });
document.addEventListener('touchend',    _onDocTouchEnd,    { passive: false });
document.addEventListener('touchcancel', _onDocTouchCancel, { passive: true });

/**
 * 現在選択中のモバイルカード情報を返す（アクションシートから使う）。
 * 残件5: 保留ドロップ中（_pendingDropWrapper がある間）は選択なし扱いにする。
 * moveMobileSelectionTo と判定基準を揃え、保留中にアクションシートのボタンが
 * 誤って動いて二重 executeDrop になる経路と、誤った console.warn を防ぐ。
 * @returns {{type:'card', element:Element}|null}
 */
export function getMobileSelection() {
  return _mState === 'selected' && _mWrapper ? { type: 'card', element: _mWrapper } : null;
}

/**
 * 選択中のカードを指定のドロップゾーン要素へ移動する（アクションシートの「墓地へ」「除外へ」用）。
 * 既存の executeDrop / getDropZoneInfo をそのまま使い、判定を二重に持たない。
 * @param {Element} dropZoneElement - .side-slot 等の実要素
 * @returns {boolean} 成功したか
 */
export function moveMobileSelectionTo(dropZoneElement) {
  if (_mState !== 'selected' || !_mWrapper || !dropZoneElement) return false;
  const dropZoneInfo = getDropZoneInfo(dropZoneElement);
  if (!dropZoneInfo) return false;
  executeDrop({ type: 'card', element: _mWrapper }, dropZoneInfo, dropZoneElement, {});
  clearMobileSelectionInternal();
  return true;
}

/** 選択を解除する（アクションシートの各操作の実行後や「×」等から呼ぶ） */
export function clearMobileSelection() {
  clearMobileSelectionInternal();
}

/**
 * 残件6: 保留ドロップ（_pendingDropWrapper/_pendingDropZone）と保留元カードの選択見た目を消す。
 * mobile-ui.js の resetMobileUI()（H-1: portrait を外れた瞬間の全リセット）から呼ぶ。
 */
export function clearPendingMobileDrop() {
  clearPendingDrop();
}
