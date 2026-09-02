/**
 * mobile-ui.js — スマホ縦向き専用UI（フェーズ1・設計書 §8。2026-09-02 reviewer監査反映）
 *
 * 下部固定バー・⋯簡易メニュー・選択中カードのアクションシート・カード詳細パネル・
 * デッキ読込ボトムシート・相手の想定妨害シート・縦タブの全幅展開を配線する。
 *
 * 方針:
 *   - isMobilePortrait()（utils/viewport.js。CSS の @media 判定と統一・A-1）以外では
 *     何もしない。CSS側は @media (max-width:767px) and (orientation:portrait) に
 *     閉じているため、hidden 属性の付け外し以外は自動的に無効化される。
 *   - カード移動は必ず drag-drop.js の executeDrop 経由（getMobileSelection /
 *     moveMobileSelectionTo 経由）。判定ロジックを複製しない。
 *   - 守備表示・セット・デッキに戻す は context-menu.js の既存処理を呼ぶ（削除はアクション
 *     シートから外した・A-3）。
 *   - 既存ボタン（保存・読込・リセット等）は .click() で発火させ、ロジックを複製しない。
 */

import {
  getMobileSelection,
  moveMobileSelectionTo,
  clearMobileSelection,
  clearPendingMobileDrop,
} from '../components/drag-drop.js';
import {
  toggleCardDefense,
  toggleCardSet,
  returnCardToDeckMenu,
  isCardInPool,
} from './context-menu.js';
import { playActivateEffect } from '../components/card-effects.js';
import { _renderCard, _showUnknown, _showLoading } from './card-info-panel.js';
import { isMobilePortrait, watchMobilePortrait } from '../utils/viewport.js';

const API_CARD_INFO = '/api/card-info';

/** hidden 属性を要素へ適用するヘルパー（存在しない要素は無視） */
function setHidden(id, hidden) {
  const el = document.getElementById(id);
  if (!el) return;
  if (hidden) el.setAttribute('hidden', '');
  else el.removeAttribute('hidden');
}

/**
 * スマホ縦向きのときだけ新規UI部品を表示する。
 * PC・横向きスマホでは常に hidden のまま（表示・動作に一切影響しない＝制約1）。
 */
function updateMobileVisibility() {
  const show = isMobilePortrait();
  setHidden('solMobileBar', !show);
  // 共有ボタンはフェーズ3まで常に非表示（show=true でも外さない）
  const shareBtn = document.getElementById('solMobileShareBtn');
  if (shareBtn) shareBtn.setAttribute('hidden', '');

  if (show) {
    updateReplayBarMobile();
  }
}

/**
 * H-1: portrait を外れた瞬間に呼ぶ。開いている全シート・オーバーレイを閉じ、hidden を戻し、
 * #solSidebar.sol-sheet-open / #opponentTray のシートクラスと .collapsed、
 * .sol-ops-details の open、#solRightPanel.sol-mobile-help-active を全て元に戻す。
 * 選択状態（drag-drop.js 側）もクリアする。
 * 以前は resize が非該当時に早期 return するだけで hidden を戻さず、回転やウィンドウ幅変更で
 * portrait を外れると素の div として PC/横向きに露出するバグがあった。
 */
function resetMobileUI() {
  closeMenuSheet();
  closeSidebarSheet();
  closeOppTraySheet();
  closeHelpSheet();
  closeReplayOverlay();
  closeCardDetailOverlay();
  clearMobileSelection();
  clearPendingMobileDrop(); // 残件6: 保留ドロップと保留元カードの選択見た目も消す

  setHidden('solMobileBar', true);
  setHidden('solMobileActionSheet', true);
  setHidden('solMobileMenuSheet', true);
  setHidden('solMobileCip', true);
  setHidden('solSidebarScrim', true);
  setHidden('solMobileUndoBtn', true);
  const shareBtn = document.getElementById('solMobileShareBtn');
  if (shareBtn) shareBtn.setAttribute('hidden', '');
}

/**
 * H-1: portrait の該当/非該当が切り替わった瞬間だけ呼ばれる（matchMedia change に一本化。
 * resize/orientationchange の逐次発火はやめる＝B-13 のデバウンスも自然に不要になる）。
 */
function handlePortraitChange(matches) {
  if (matches) {
    updateMobileVisibility();
  } else {
    resetMobileUI();
  }
}

// ══════════════════ デッキ枚数バッジ・下部バーの基本ボタン ══════════════════

function initDeckCountBadge() {
  const poolRow = document.getElementById('poolRow');
  const countEl = document.getElementById('solMobileDeckCount');
  if (!poolRow || !countEl) return;
  const update = () => {
    countEl.textContent = String(poolRow.querySelectorAll('.tier-item-wrapper').length);
  };
  update();
  new MutationObserver(update).observe(poolRow, { childList: true });
}

function initBarButtons() {
  // デッキ N: フェーズ1では既存の1ドロー処理を仮動作として呼ぶ（一覧シートはフェーズ2）
  document.getElementById('solMobileDeckBtn')
    ?.addEventListener('click', () => { document.getElementById('randomButton')?.click(); });

  // 取消: 既存の取消ボタンをそのまま呼ぶ
  document.getElementById('solMobileUndoBtn')
    ?.addEventListener('click', () => { document.getElementById('replayUndo')?.click(); });

  // ⋯ メニュー
  document.getElementById('solMobileMenuBtn')
    ?.addEventListener('click', () => { openMenuSheet(); });
}

/**
 * A-4: 旧リプレイバーは縦向きスマホでは常時 display:none（mobile.css）。
 * 取消は下部バーの #solMobileUndoBtn だけが表示を担う。
 * リプレイの手数（#replayUndo の disabled 属性）を見て表示を切り替える。
 */
function updateReplayBarMobile() {
  if (!isMobilePortrait()) return;
  const undoBtn = document.getElementById('replayUndo');
  if (!undoBtn) return;
  setHidden('solMobileUndoBtn', undoBtn.disabled);
}

function initReplayBarMobile() {
  const undoBtn = document.getElementById('replayUndo');
  if (!undoBtn) return;
  updateReplayBarMobile();
  new MutationObserver(updateReplayBarMobile).observe(undoBtn, { attributes: true, attributeFilter: ['disabled'] });
}

// ══════════════════ ⋯メニュー「リプレイ操作」オーバーレイ（A-4） ══════════════════

function openReplayOverlay() {
  document.getElementById('replayBarContainer')?.classList.add('sol-mobile-fullbar-open');
}
function closeReplayOverlay() {
  document.getElementById('replayBarContainer')?.classList.remove('sol-mobile-fullbar-open');
}
function initReplayOverlay() {
  document.getElementById('solMobileReplayBarClose')?.addEventListener('click', closeReplayOverlay);
}

// ══════════════════ ⋯ メニューシート ══════════════════

function openMenuSheet() {
  updateOppMenuBadge();
  setHidden('solMobileMenuSheet', false);
}
function closeMenuSheet() {
  setHidden('solMobileMenuSheet', true);
}

const MENU_ACTIONS = {
  save:     () => { document.getElementById('saveButton2')?.click(); },
  load:     () => { document.getElementById('loadButton')?.click(); },
  reset:    () => { document.getElementById('resetButton')?.click(); },
  coin:     () => { document.getElementById('coinTossBtn')?.click(); },
  dice:     () => { document.getElementById('diceRollBtn')?.click(); },
  opptray:  () => { openOppTraySheet(); },
  deckinput:() => { openSidebarSheet(); },
  replay:   () => { openReplayOverlay(); },
  help:     () => { openHelpSheet(); },
  feedback: () => { document.getElementById('feedbackOpenBtn')?.click(); },
};

function initMenuSheet() {
  const sheet = document.getElementById('solMobileMenuSheet');
  if (!sheet) return;

  sheet.querySelectorAll('[data-sol-sheet-close]').forEach(el => {
    el.addEventListener('click', closeMenuSheet);
  });

  sheet.querySelectorAll('[data-menu-action]').forEach(btn => {
    btn.addEventListener('click', () => {
      const action = MENU_ACTIONS[btn.dataset.menuAction];
      if (action) action();
      // opptray/deckinput/help/replay はそれぞれのシートへ引き継ぐため、メニュー自体は閉じる
      closeMenuSheet();
    });
  });
}

// ══════════════════ デッキ読込ボトムシート（#solSidebar 再利用） ══════════════════

function openSidebarSheet() {
  document.getElementById('solSidebar')?.classList.add('sol-sheet-open');
  setHidden('solSidebarScrim', false);
}
function closeSidebarSheet() {
  document.getElementById('solSidebar')?.classList.remove('sol-sheet-open');
  setHidden('solSidebarScrim', true);
}

function initSidebarSheet() {
  document.getElementById('solSidebarSheetClose')
    ?.addEventListener('click', closeSidebarSheet);
  document.getElementById('solSidebarScrim')
    ?.addEventListener('click', closeSidebarSheet);
}

// ══════════════════ 相手の想定妨害シート（#opponentTray 再利用） ══════════════════

// 残件7: シートを開く直前に .collapsed だったかを控え、閉じるときに元へ戻す
// （横向きに回転したとき、開けっぱなしのトレイが盤面上に展開状態で現れる経路の根治）。
let _oppTrayWasCollapsed = false;

function openOppTraySheet() {
  const tray = document.getElementById('opponentTray');
  const toggle = document.getElementById('oppTrayToggle');
  if (!tray) return;
  _oppTrayWasCollapsed = tray.classList.contains('collapsed');
  tray.classList.add('sol-opp-sheet-open');
  if (_oppTrayWasCollapsed) {
    toggle?.click(); // opponent-tray.js 側の開閉ロジック（collapsed解除・opp-tray-resize発火）をそのまま使う
  }
}
function closeOppTraySheet() {
  const tray = document.getElementById('opponentTray');
  const toggle = document.getElementById('oppTrayToggle');
  tray?.classList.remove('sol-opp-sheet-open');
  // 残件7: シートを開く前に collapsed（畳んだ状態）だった場合だけ畳み直す
  if (_oppTrayWasCollapsed && tray && !tray.classList.contains('collapsed')) {
    toggle?.click();
  }
  _oppTrayWasCollapsed = false;
}

/**
 * L-1: ⋯メニュー内の「相手の想定妨害」項目のバッジ（#solMobileOppBadge）に加え、
 * ⋯ボタン自体（#solMobileMenuBadge）にも同じ枚数を反映する。
 */
function updateOppMenuBadge() {
  const count = document.querySelectorAll('#oppTraySlots .opp-slot.filled').length;
  [
    document.getElementById('solMobileOppBadge'),
    document.getElementById('solMobileMenuBadge'),
  ].forEach(badge => {
    if (!badge) return;
    if (count > 0) {
      badge.textContent = String(count);
      badge.removeAttribute('hidden');
    } else {
      badge.setAttribute('hidden', '');
    }
  });
}

function initOppTraySheet() {
  document.getElementById('oppTraySheetClose')
    ?.addEventListener('click', closeOppTraySheet);
  // トレイ内のカード増減にあわせてメニューバッジを更新
  const slots = document.getElementById('oppTraySlots');
  if (slots) {
    new MutationObserver(updateOppMenuBadge).observe(slots, { childList: true, subtree: true, attributes: true, attributeFilter: ['class'] });
  }
}

// ══════════════════ 操作方法シート（.sol-ops-details 再利用） ══════════════════

let _helpScrim = null;

// .sol-ops-details は display:none の #solRightPanel の子孫にあるため、
// details 自身を position:fixed にするだけでは祖先の display:none に隠れて描画されない。
// 祖先（#solRightPanel と直接の親 .sol-right-section）にも一時的に表示クラスを付ける。
function openHelpSheet() {
  const details = document.querySelector('.sol-ops-details');
  const section = details?.closest('.sol-right-section');
  const rightPanel = document.getElementById('solRightPanel');
  if (!details || !section) return;
  details.open = true;
  details.classList.add('sol-mobile-help-open');
  section.classList.add('sol-mobile-help-section-active');
  rightPanel?.classList.add('sol-mobile-help-active');
  if (!_helpScrim) {
    _helpScrim = document.createElement('div');
    _helpScrim.className = 'sol-mobile-help-scrim';
    _helpScrim.addEventListener('click', closeHelpSheet);
    document.body.appendChild(_helpScrim);
  } else {
    _helpScrim.style.display = '';
  }
}
function closeHelpSheet() {
  const details = document.querySelector('.sol-ops-details');
  const section = details?.closest('.sol-right-section');
  const rightPanel = document.getElementById('solRightPanel');
  if (details) {
    details.classList.remove('sol-mobile-help-open');
    details.open = false; // H-1: PC の <details> 挙動に戻す（勝手に開いたままにしない）
  }
  section?.classList.remove('sol-mobile-help-section-active');
  rightPanel?.classList.remove('sol-mobile-help-active');
  if (_helpScrim) _helpScrim.style.display = 'none';
}

// ══════════════════ 縦タブ: 墓地｜除外の全幅展開（設計書 a-3） ══════════════════

function initSideAreaExpand() {
  const areas = Array.from(document.querySelectorAll('.side-slots-container .sol-side-area'));
  if (areas.length === 0) return;
  areas.forEach(area => {
    const label = area.querySelector('.pool-label');
    if (!label) return;
    label.addEventListener('click', () => {
      if (!isMobilePortrait()) return;
      const wasExpanded = area.classList.contains('expanded');
      areas.forEach(a => a.classList.remove('expanded', 'sol-collapsed-chip'));
      if (!wasExpanded) {
        area.classList.add('expanded');
        areas.forEach(a => { if (a !== area) a.classList.add('sol-collapsed-chip'); });
      }
    });
  });
}

// ══════════════════ カード詳細パネル（スマホ・A-2で「既定は閉」に変更） ══════════════════

/** wrapper からカード名・画像URLを取り出す（card-info-panel.js の名前解決に合わせる） */
function getCardNameAndSrc(wrapper) {
  const cardEl = wrapper?.querySelector('.tier-item');
  if (!cardEl) return { name: '', src: '' };
  const name = (cardEl.dataset && cardEl.dataset.cardName)
    || (cardEl.tagName === 'IMG' ? (cardEl.alt || '') : '')
    || '';
  const src = cardEl.src || '';
  return { name, src };
}

/**
 * card-info-panel.js の _renderCard / _showUnknown / _showLoading を再利用してスマホパネルへ描画する。
 * 価格ブロックは生成しない（showPrice=false・B-12）。API呼び出し自体は card-info-panel.js の
 * _fetchAndShow が export されていないため、ここに最小限のロジックを複製している。
 */
async function showMobileCardInfo(name, imgSrc) {
  const body = document.getElementById('solMobileCipBody');
  if (!body) return;
  _showLoading(name || '名称不明', imgSrc || '', body);
  if (!name) {
    _showUnknown(imgSrc || '', null, null, body, false);
    return;
  }
  try {
    const res = await fetch(`${API_CARD_INFO}?name=${encodeURIComponent(name)}`);
    const data = await res.json();
    if (data.found) {
      _renderCard(data, imgSrc, name, null, body, false);
    } else if (data.kind === 'proxy' && data.proxy) {
      _renderCard(data.proxy, imgSrc, name, null, body, false);
    } else {
      _showUnknown(imgSrc, name, null, body, false);
    }
  } catch {
    _showUnknown(imgSrc, name, null, body, false);
  }
}

/**
 * A-2: 詳細パネルはアクションシートの「詳細」ボタンからのみ開くオーバーレイ。
 * 開いている間は選択状態を維持し、閉じても選択状態はそのまま（selected はここでは変更しない）。
 * 開閉状態は記憶しない（localStorage 廃止）。
 */
function openCardDetailOverlay() {
  const sel = getMobileSelection();
  if (!sel) return;
  const { name, src } = getCardNameAndSrc(sel.element);
  setHidden('solMobileCip', false);
  showMobileCardInfo(name, src);
}
function closeCardDetailOverlay() {
  setHidden('solMobileCip', true);
}

function initCardDetailPanel() {
  document.querySelectorAll('#solMobileCip [data-sol-cip-close]').forEach(el => {
    el.addEventListener('click', closeCardDetailOverlay);
  });
  document.getElementById('solMobileActionDetailBtn')
    ?.addEventListener('click', openCardDetailOverlay);
}

// ══════════════════ 選択中カードのアクションシート（A-3で再構成） ══════════════════

function getSideSlot(isGrave) {
  if (isGrave) return document.querySelector('.sol-grave .side-slot');
  return document.querySelector('.side-slots-container .sol-side-area:not(.sol-grave) .side-slot');
}

/** M-11: moveMobileSelectionTo が失敗（false）したときに原因を追える警告を出す */
function moveSelectionOrWarn(dropZoneElement, label) {
  const ok = moveMobileSelectionTo(dropZoneElement);
  if (!ok) {
    console.warn(`[mobile-ui] 移動先が見つからない: ${label}`);
  }
}

// A-3: 「削除」は外す（長押しメニューに残る）。6操作のみ。
const ACTION_HANDLERS = {
  defense: (wrapper) => { toggleCardDefense(wrapper); clearMobileSelection(); },
  set:     (wrapper) => { toggleCardSet(wrapper); clearMobileSelection(); },
  grave:   () => { moveSelectionOrWarn(getSideSlot(true), '墓地'); },
  banish:  () => { moveSelectionOrWarn(getSideSlot(false), '除外'); },
  return:  (wrapper) => { returnCardToDeckMenu(wrapper); clearMobileSelection(); },
  activate:(wrapper) => {
    playActivateEffect(wrapper);
    const cardEl = wrapper.querySelector('.tier-item');
    if (cardEl?.id && typeof window.replayLog === 'function') {
      window.replayLog({ actionType: 'activateEffect', cardId: cardEl.id });
    }
    clearMobileSelection();
  },
};

/**
 * A-3: ヘッダ行（カード名＋詳細ボタン）＋操作行。
 * 選択カードがプール内（#poolRow/#poolRow2 の子）の場合は操作ボタンを出さずヘッダ行だけにする
 * （context-menu.js の isCardInPool を共有。判定を二重に持たない）。
 */
function updateActionSheetForSelection(wrapper) {
  const { name } = getCardNameAndSrc(wrapper);
  const nameEl = document.getElementById('solMobileActionCardName');
  if (nameEl) nameEl.textContent = name || '名称不明';

  const opsEl = document.getElementById('solMobileActionOps');
  if (opsEl) opsEl.hidden = isCardInPool(wrapper);

  setHidden('solMobileActionSheet', false);
}

function initActionSheet() {
  const sheet = document.getElementById('solMobileActionSheet');
  if (!sheet) return;
  sheet.querySelectorAll('[data-action]').forEach(btn => {
    btn.addEventListener('click', () => {
      const sel = getMobileSelection();
      if (!sel) return;
      const handler = ACTION_HANDLERS[btn.dataset.action];
      if (handler) handler(sel.element);
    });
  });

  // drag-drop.js の状態機械からの通知を受けて表示/非表示を切り替える
  document.addEventListener('sol-mobile-card-selected', (ev) => {
    updateActionSheetForSelection(ev.detail.wrapper);
  });
  document.addEventListener('sol-mobile-card-deselected', () => {
    setHidden('solMobileActionSheet', true);
    closeCardDetailOverlay(); // A-2: 選択解除にあわせて詳細オーバーレイも閉じる
  });
}

// ══════════════════ エントリポイント ══════════════════

export function initMobileUI() {
  updateMobileVisibility();
  initDeckCountBadge();
  initBarButtons();
  initReplayBarMobile();
  initReplayOverlay();
  initMenuSheet();
  initSidebarSheet();
  initOppTraySheet();
  initSideAreaExpand();
  initCardDetailPanel();
  initActionSheet();

  // H-1: resize/orientationchange の逐次発火はやめ、matchMedia の change に一本化する。
  watchMobilePortrait(handlePortraitChange);
}
