/**
 * context-menu.js
 * カードの右クリック / 長押しコンテキストメニュー
 *
 * 表示項目（ゾーン別）:
 *   フィールド: 攻撃/守備の切替 / セット・表にする / 一番下に重ねる / デッキに戻す / 削除
 *   プール    : 削除（初期カードは不可）
 *   その他    : デッキに戻す / 削除
 *
 * 依存: card-state.js のみ（drag-drop.js との循環を避けるため _getZoneId を内部実装）
 */

import {
  applyDefense,
  applySet,
  applyCardState,
  getCardState,
} from '../components/card-state.js';
import { playSetFlip } from '../components/card-effects.js';

// ── 単一インスタンス ──────────────────────────────────────────────
let _menuEl          = null;
let _removeListeners = null;

/**
 * コンテキストメニューを開く
 * @param {Element} wrapper - .tier-item-wrapper
 * @param {Element} img     - img.tier-item
 * @param {number}  x       - 表示位置 clientX
 * @param {number}  y       - 表示位置 clientY
 */
export function openCardContextMenu(wrapper, img, x, y) {
  closeContextMenu();

  const parent   = wrapper.parentElement;
  const poolRow  = document.getElementById('poolRow');
  const poolRow2 = document.getElementById('poolRow2');
  const isInPool = parent === poolRow || parent === poolRow2;
  const isInitial = wrapper.id === 'initial';

  const items = _buildMenuItems(wrapper, img, parent, isInPool, isInitial);
  if (items.length === 0) return;

  // ── メニュー要素を生成 ────────────────────────────────────────
  const menu = document.createElement('ul');
  menu.className = 'sol-context-menu';
  menu.setAttribute('role', 'menu');

  items.forEach(({ label, action, separator }) => {
    if (separator) {
      const hr = document.createElement('li');
      hr.className = 'sol-context-menu-sep';
      menu.appendChild(hr);
      return;
    }
    const li = document.createElement('li');
    li.className = 'sol-context-menu-item';
    li.textContent = label;
    li.setAttribute('role', 'menuitem');
    li.addEventListener('mousedown', (e) => {
      e.stopPropagation();
      action();
      closeContextMenu();
    });
    // タッチ対応（touchend でアクション）
    li.addEventListener('touchend', (e) => {
      e.preventDefault();
      e.stopPropagation();
      action();
      closeContextMenu();
    });
    menu.appendChild(li);
  });

  // ── 位置調整（画面端クランプ） ────────────────────────────────
  document.body.appendChild(menu);
  const mw = menu.offsetWidth  || 160;
  const mh = menu.offsetHeight || items.length * 36;
  menu.style.left = `${Math.min(x, window.innerWidth  - mw - 6)}px`;
  menu.style.top  = `${Math.min(y, window.innerHeight - mh - 6)}px`;

  _menuEl = menu;

  // ── 外クリック/スクロールで閉じる ────────────────────────────
  const closeOnOutside = (e) => {
    if (!menu.contains(e.target)) closeContextMenu();
  };
  // 同一イベントで即座に閉じないよう setTimeout で登録
  const tid = setTimeout(() => {
    document.addEventListener('mousedown', closeOnOutside);
    document.addEventListener('touchstart', closeOnOutside);
    document.addEventListener('scroll', closeContextMenu, { passive: true, capture: true });
  }, 0);

  _removeListeners = () => {
    clearTimeout(tid);
    document.removeEventListener('mousedown', closeOnOutside);
    document.removeEventListener('touchstart', closeOnOutside);
    document.removeEventListener('scroll', closeContextMenu, { capture: true });
  };
}

/**
 * 現在のコンテキストメニューを閉じる
 */
export function closeContextMenu() {
  if (_menuEl) {
    _menuEl.remove();
    _menuEl = null;
  }
  if (_removeListeners) {
    _removeListeners();
    _removeListeners = null;
  }
}

// ── 内部: メニュー項目の組み立て ──────────────────────────────────

function _buildMenuItems(wrapper, img, parent, isInPool, isInitial) {
  const items = [];

  if (isInPool) {
    if (!isInitial) {
      items.push({ label: '削除', action: () => { wrapper.remove(); } });
    }
    return items;
  }

  const isDefense = wrapper.dataset.orientation === 'defense';
  const isSet     = wrapper.dataset.face === 'down';
  const isCustom  = parent?.classList.contains('custom-slot');

  // 守備表示切替
  items.push({
    label: isDefense ? '攻撃表示にする' : '守備表示にする',
    action: () => {
      applyDefense(wrapper, !isDefense);
      _logState(wrapper);
    },
  });

  // セット切替（フリップアニメ付き: scaleX=0の瞬間に状態変更）
  items.push({
    label: isSet ? '表にする' : 'セット（裏向きにする）',
    action: () => {
      playSetFlip(wrapper, () => {
        applySet(wrapper, !isSet);
        _logState(wrapper);
      });
    },
  });

  // 下重ね（フィールドスロット、かつ他のカードがある場合）
  if (isCustom && parent.querySelectorAll('.tier-item-wrapper').length > 1) {
    items.push({
      label: '一番下に重ねる',
      action: () => { _placeUnder(wrapper, parent); },
    });
  }

  items.push({ separator: true });

  // デッキに戻す
  items.push({
    label: 'デッキに戻す',
    action: () => { _returnToDeck(wrapper, img); },
  });

  // 削除
  items.push({
    label: '削除',
    action: () => { wrapper.remove(); },
  });

  return items;
}

// ── 内部: カード操作ヘルパ ───────────────────────────────────────

/**
 * カードをデッキに戻す（リプレイログ付き、状態クリア）
 */
function _returnToDeck(wrapper, img) {
  const isEx   = wrapper.id.includes('ex');
  const cardId = img.id;

  if (typeof window.replayLog === 'function') {
    window.replayLog({ actionType: 'returnToDeck', cardId, isEx });
  }

  // スタイルと状態をクリア
  wrapper.style = '';
  applyCardState(wrapper, {});

  const pool = document.getElementById(isEx ? 'poolRow2' : 'poolRow');
  pool.appendChild(wrapper);
}

/**
 * カードを同スロットの一番下（z-index最背面）に移動
 */
function _placeUnder(wrapper, slot) {
  const items = Array.from(slot.querySelectorAll('.tier-item-wrapper'));
  if (items.length <= 1) return;

  const others = items.filter(el => el !== wrapper);

  // 既存カードを1段ずつ下へずらし、差し込むカードをスロット上端に置く。
  // 通常積みと同じ方向（上端に古いカード、下へ向かって新しいカード）になる。
  others.forEach((el, i) => {
    el.style.position = 'absolute';
    el.style.zIndex   = String(2 + i);
    el.style.top      = `calc(var(--slot-width) * 0.${i + 1})`;
  });

  wrapper.style.position = 'absolute';
  wrapper.style.zIndex   = '1';
  wrapper.style.top      = '0';

  // DOMの先頭に移動（視覚的な重なり順と一致させる）
  slot.insertBefore(wrapper, slot.firstChild);

  // リプレイ記録
  _logState(wrapper);
}

/**
 * 現在のゾーン・状態でリプレイログを記録
 */
function _logState(wrapper) {
  if (typeof window.replayLog !== 'function') return;
  const img = wrapper.querySelector('img.tier-item');
  if (!img) return;
  const state  = getCardState(wrapper);
  const zoneEl = wrapper.parentElement;
  window.replayLog({
    actionType:  'moveCard',
    cardId:      img.id,
    zoneId:      _getZoneId(zoneEl),
    zIndex:      wrapper.style.zIndex || '1',
    transform:   wrapper.style.transform || '',
    orientation: state.orientation,
    face:        state.face,
  });
}

/**
 * ゾーン要素から zoneId 文字列を取得
 * drag-drop.js の getZoneId と同ロジック（循環依存を避けるため複製）
 */
function _getZoneId(zoneElement) {
  if (!zoneElement) return 'unknown';
  const cls = zoneElement.className || '';
  if (zoneElement.id === 'poolRow')  return 'poolRow';
  if (zoneElement.id === 'poolRow2') return 'poolRow2';
  if (cls.includes('center-slot'))   return 'center-slot';
  if (cls.includes('custom-slot')) {
    const slot = zoneElement.getAttribute('data-slot');
    return slot ? `custom-slot-${slot}` : 'custom-slot-?';
  }
  if (cls.includes('side-slot')) {
    if (zoneElement.closest('#free-space')) return 'free-space';
    const all = Array.from(document.querySelectorAll(
      '.side-slot-group:not(#free-space .side-slot-group) .side-slot'
    ));
    const idx = all.indexOf(zoneElement);
    return idx >= 0 ? `side-slot-${idx}` : 'side-slot-?';
  }
  return 'unknown';
}
