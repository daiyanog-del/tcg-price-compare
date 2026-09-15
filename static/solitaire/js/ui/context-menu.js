/**
 * context-menu.js
 * カードの右クリック / 長押しコンテキストメニュー
 *
 * 表示項目（ゾーン別）:
 *   フィールド: 攻撃/守備の切替 / セット・表にする / 一番下に重ねる / 墓地送り / 除外 / 手札に戻す / デッキに戻す / 削除
 *   プール    : 削除（初期カードは不可）
 *   その他    : 墓地送り / 除外 / 手札に戻す / デッキに戻す / 削除
 *
 * 依存: card-state.js のほか、固定ゾーン移動（墓地送り/除外/手札に戻す）のために
 * drag-drop.js の getDropZoneInfo/executeDrop/getZoneId を使う（実際の配置・リプレイ記録ロジックの二重持ちを避ける）。
 * main.js は drag-drop.js を先に import し、drag-drop.js が本ファイルを import するため、
 * 本ファイル（context-menu.js）の方が drag-drop.js より先にモジュール評価が完了する。
 * そのため本ファイルのトップレベル（関数の外）で drag-drop.js からの import 値を参照すると
 * 未初期化になる危険がある。参照は必ず関数内（呼び出し時点）に限ること。
 */

import {
  applyDefense,
  applySet,
  applyCardState,
  getCardState,
  isMonsterCard,
} from '../components/card-state.js';
import { playSetFlip } from '../components/card-effects.js';
import { getDropZoneInfo, executeDrop, getZoneId } from '../components/drag-drop.js';

// ── 単一インスタンス ──────────────────────────────────────────────
let _menuEl          = null;
let _removeListeners = null;

/**
 * コンテキストメニューを開く
 * @param {Element} wrapper  - .tier-item-wrapper
 * @param {Element} cardEl   - img.tier-item または div.tier-item（proxy-card）
 * @param {number}  x        - 表示位置 clientX
 * @param {number}  y        - 表示位置 clientY
 */
export function openCardContextMenu(wrapper, cardEl, x, y) {
  // 後方互換: 内部で img という名前を使っていた箇所を cardEl として統一する
  const img = cardEl;  // 既存の _buildMenuItems / _returnToDeck の引数に渡す
  closeContextMenu();

  const parent   = wrapper.parentElement;
  const isInPool = isCardInPool(wrapper);
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

// ── スマホ アクションシート用（mobile-ui.js から呼ぶ）──────────────
// _buildMenuItems 内のアクションと同じ処理を export し、ロジックの複製を避ける。

/**
 * カードがプール内（#poolRow / #poolRow2 の直接の子）かどうかを判定する。
 * openCardContextMenu の isInPool 判定と同じ条件を共有する（A-3・司令塔決定。判定の二重持ち禁止）。
 * @param {Element} wrapper - .tier-item-wrapper
 * @returns {boolean}
 */
export function isCardInPool(wrapper) {
  const parent = wrapper.parentElement;
  const poolRow  = document.getElementById('poolRow');
  const poolRow2 = document.getElementById('poolRow2');
  return parent === poolRow || parent === poolRow2;
}

/**
 * 守備表示をトグルする（コンテキストメニュー「守備表示にする/攻撃表示にする」と同じ処理）
 * @param {Element} wrapper - .tier-item-wrapper
 */
export function toggleCardDefense(wrapper) {
  const isDefense = wrapper.dataset.orientation === 'defense';
  applyDefense(wrapper, !isDefense);
  _logState(wrapper);
}

/**
 * セット状態をトグルする（コンテキストメニュー「セット（裏向きにする）/表にする」と同じ処理）。
 * セット解除時、コンテキストメニューはモンスターに対して攻撃/守備を選ばせるが、
 * アクションシートは単純トグルのため攻撃表示に固定する。
 * @param {Element} wrapper - .tier-item-wrapper
 */
export function toggleCardSet(wrapper) {
  const isSet = wrapper.dataset.face === 'down';
  playSetFlip(wrapper, () => {
    applySet(wrapper, !isSet);
    _logState(wrapper);
  });
}

/**
 * カードをデッキに戻す（コンテキストメニュー「デッキに戻す」と同じ処理）
 * @param {Element} wrapper - .tier-item-wrapper
 */
export function returnCardToDeckMenu(wrapper) {
  const img = wrapper.querySelector('.tier-item');
  if (img) _returnToDeck(wrapper, img);
}

// L-2: removeCardMenu は A-3 でアクションシートから「削除」を外した際に未使用になったため削除。
// カードの削除は引き続き _buildMenuItems 内の長押しメニュー項目（PC 右クリックメニューと共通）が担う。

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
  if (isSet && isMonsterCard(wrapper)) {
    // モンスターのセット解除：攻撃表示か守備表示を選べる
    items.push({
      label: '表にする（攻撃表示）',
      action: () => {
        playSetFlip(wrapper, () => {
          applySet(wrapper, false);
          _logState(wrapper);
        });
      },
    });
    items.push({
      label: '表にする（守備表示）',
      action: () => {
        playSetFlip(wrapper, () => {
          applyCardState(wrapper, { orientation: 'defense', face: '' });
          _logState(wrapper);
        });
      },
    });
  } else {
    items.push({
      label: isSet ? '表にする' : 'セット（裏向きにする）',
      action: () => {
        playSetFlip(wrapper, () => {
          applySet(wrapper, !isSet);
          _logState(wrapper);
        });
      },
    });
  }

  // 下重ね（フィールドスロット、かつ他のカードがある場合）
  if (isCustom && parent.querySelectorAll('.tier-item-wrapper').length > 1) {
    items.push({
      label: '一番下に重ねる',
      action: () => { _placeUnder(wrapper, parent); },
    });
  }

  // 固定ゾーンへの移動（墓地送り / 除外 / 手札に戻す）
  // 既に対象ゾーンにいるカードにはその項目を出さない（無意味な1手がリプレイに記録されるのを防ぐ）
  const graveZoneEl  = _getGraveZoneElement();
  const banishZoneEl = _getBanishZoneElement();
  const handZoneEl   = _getHandZoneElement();
  const fixedZoneItems = [];
  if (parent !== graveZoneEl) {
    fixedZoneItems.push({
      label: '墓地送り',
      action: () => { _moveToFixedZone(wrapper, graveZoneEl); },
    });
  }
  if (parent !== banishZoneEl) {
    fixedZoneItems.push({
      label: '除外',
      action: () => { _moveToFixedZone(wrapper, banishZoneEl); },
    });
  }
  if (parent !== handZoneEl) {
    fixedZoneItems.push({
      label: '手札に戻す',
      action: () => { _moveToFixedZone(wrapper, handZoneEl); },
    });
  }

  if (fixedZoneItems.length > 0) {
    items.push({ separator: true });
    items.push(...fixedZoneItems);
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
 * 墓地ゾーンのDOM要素を取得する（mobile-ui.js の getSideSlot(true) と同一セレクタ）。
 * @returns {Element|null}
 */
function _getGraveZoneElement() {
  return document.querySelector('.sol-grave .side-slot');
}

/**
 * 除外ゾーンのDOM要素を取得する（mobile-ui.js の getSideSlot(false) と同一セレクタ）。
 * 墓地(.sol-grave)を除外することで、想定妨害トレイ等が将来増えても誤って墓地を掴まない。
 * @returns {Element|null}
 */
function _getBanishZoneElement() {
  return document.querySelector('.side-slots-container .sol-side-area:not(.sol-grave) .side-slot');
}

/**
 * 手札ゾーン（center-slot）のDOM要素を取得する。
 * @returns {Element|null}
 */
function _getHandZoneElement() {
  return document.querySelector('.sol-hand-area .center-slot');
}

/**
 * カードを固定ゾーン（墓地/除外/手札）へ移動する。
 * drag-drop.js の executeDrop をそのまま使い、配置・リプレイ記録ロジックを複製しない
 * （moveMobileSelectionTo と同じパターン）。
 * @param {Element} wrapper     - .tier-item-wrapper
 * @param {Element|null} zoneEl - 移動先のゾーンDOM要素
 */
function _moveToFixedZone(wrapper, zoneEl) {
  if (!zoneEl) {
    console.warn('[context-menu] 移動先のゾーン要素が見つかりません');
    return;
  }
  const dropZoneInfo = getDropZoneInfo(zoneEl);
  if (!dropZoneInfo) {
    console.warn('[context-menu] ゾーン種別を判定できませんでした');
    return;
  }
  executeDrop({ type: 'card', element: wrapper }, dropZoneInfo, zoneEl, {});
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
  // img.tier-item または div.tier-item（プロキシ）どちらでも .tier-item で取れる
  const cardEl = wrapper.querySelector('.tier-item');
  if (!cardEl) return;
  const state  = getCardState(wrapper);
  const zoneEl = wrapper.parentElement;
  // drag-drop.js の getZoneId は zoneElement が null だと className アクセスで例外になるため、
  // 呼び出し側（ここ）でガードする（_getZoneId 複製時にあったnullガードを踏襲）
  window.replayLog({
    actionType:  'moveCard',
    cardId:      cardEl.id,
    zoneId:      zoneEl ? getZoneId(zoneEl) : 'unknown',
    zIndex:      wrapper.style.zIndex || '1',
    transform:   wrapper.style.transform || '',
    orientation: state.orientation,
    face:        state.face,
  });
}
