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
  getDropZoneInfo,
  executeDrop,
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
import { showToast } from '../utils/toast.js';
import { generateShareURL, buildShareTweetUrl } from './replay-ui.js';

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

  if (show) {
    updateReplayBarMobile();
    updateEmptyCta();
  } else {
    setHidden('solMobileEmptyCta', true);
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
  closeDeckSheet();   // フェーズ2: デッキ一覧シート
  closeShareSheet();  // フェーズ3: 共有シート
  clearMobileSelection();
  clearPendingMobileDrop(); // 残件6: 保留ドロップと保留元カードの選択見た目も消す

  setHidden('solMobileBar', true);
  setHidden('solMobileActionSheet', true);
  setHidden('solMobileMenuSheet', true);
  setHidden('solMobileCip', true);
  setHidden('solSidebarScrim', true);
  setHidden('solMobileUndoBtn', true);
  setHidden('solMobileShareBtn', true);
  setHidden('solMobileDeckSheet', true);
  setHidden('solMobileShareSheet', true);
  setHidden('solMobileEmptyCta', true);
  setDeckBtnLoading(false); // 読込中表示のまま portrait を外れた場合の保険
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
    // §3.5 初見導線: 既存のデッキ枚数バッジ用オブザーバーを流用し、CTAの表示も更新する
    updateEmptyCta();
  };
  update();
  new MutationObserver(update).observe(poolRow, { childList: true });
}

function initBarButtons() {
  // デッキ N: フェーズ2でデッキ一覧シートを開く（1ドロー仮動作はシート内の副ボタンへ移動）
  document.getElementById('solMobileDeckBtn')
    ?.addEventListener('click', () => { openDeckSheet(); });

  // 取消: 既存の取消ボタンをそのまま呼ぶ
  document.getElementById('solMobileUndoBtn')
    ?.addEventListener('click', () => { document.getElementById('replayUndo')?.click(); });

  // 共有: フェーズ3の共有シートを開く
  document.getElementById('solMobileShareBtn')
    ?.addEventListener('click', () => { openShareSheet(); });

  // ⋯ メニュー
  document.getElementById('solMobileMenuBtn')
    ?.addEventListener('click', () => { openMenuSheet(); });
}

/**
 * A-4: 旧リプレイバーは縦向きスマホでは常時 display:none（mobile.css）。
 * 取消は下部バーの #solMobileUndoBtn だけが表示を担う。
 * リプレイの手数（#replayUndo の disabled 属性）を見て表示を切り替える。
 * フェーズ3: 共有ボタンも同じ「手数1以上」判定（undoBtn.disabled と同基準）で表示する。
 */
function updateReplayBarMobile() {
  if (!isMobilePortrait()) return;
  const undoBtn = document.getElementById('replayUndo');
  if (!undoBtn) return;
  setHidden('solMobileUndoBtn', undoBtn.disabled);
  setHidden('solMobileShareBtn', undoBtn.disabled);
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

// ══════════════════ デッキ読込の多重防止・進捗表示（バグ修正） ══════════════════
// deck-input-panel.js が dispatch する sol-deck-load-start / sol-deck-load-end を受けて、
// デッキ読込シートを閉じ、下部バーの「デッキ」ボタンを読込中表示に切り替える。

/** #solMobileDeckBtn の読込中表示を切り替える（見た目は mobile.css の .sol-loading が担う） */
function setDeckBtnLoading(loading) {
  const btn = document.getElementById('solMobileDeckBtn');
  if (!btn) return;
  btn.disabled = loading;
  btn.classList.toggle('sol-loading', loading);
}

function initDeckLoadProgress() {
  document.addEventListener('sol-deck-load-start', () => {
    if (!isMobilePortrait()) return;
    closeSidebarSheet();
    setDeckBtnLoading(true);
    // M-3: clearAllCards 中は一瞬盤面が空になり得るため、CTAが誤って一瞬出るのを防ぐ
    setHidden('solMobileEmptyCta', true);
  });
  document.addEventListener('sol-deck-load-end', (ev) => {
    if (!isMobilePortrait()) return;
    setDeckBtnLoading(false);
    const { ok, main, ex } = ev.detail || {};
    if (ok) {
      showToast(`デッキを読み込みました（メイン${main}・EX${ex}）`);
    } else {
      showToast('読み込みに失敗しました');
    }
    updateEmptyCta();
  });
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

// ══════════════════ デッキ一覧シート（フェーズ2・設計書 §3.3） ══════════════════

// 選択中のカードID（#poolRow内 .tier-item の id）の集合。シートを開くたびにクリアする。
const _selectedDeckCardIds = new Set();

/** 選択トグル用のセルを作る（img の src を複製した新規要素。#poolRow の元要素は動かさない） */
function _buildDeckCell(wrapper) {
  const cardEl = wrapper.querySelector('.tier-item');
  if (!cardEl?.id) return null;
  const { name, src } = getCardNameAndSrc(wrapper);

  const cell = document.createElement('button');
  cell.type = 'button';
  cell.className = 'sol-mobile-deck-cell';
  cell.dataset.cardId = cardEl.id;
  cell.dataset.cardName = name || '';
  // H-2: 元wrapperが自サイト透かし対象（発売済みクリーン画像）のときだけ透かしを付ける。
  // self-watermark.css の [data-self-wm] .wm-released::before は position:relative の
  // 祖先に依存するが .sol-mobile-deck-cell は既に position:relative なのでそのまま効く。
  if (wrapper.classList.contains('wm-released')) cell.classList.add('wm-released');

  if (src) {
    const thumb = document.createElement('img');
    thumb.className = 'sol-mobile-deck-thumb';
    thumb.src = src;
    thumb.alt = name || '';
    cell.appendChild(thumb);
  } else {
    // プロキシ等 img を持たないカード: 名前のみのフォールバック表示（XSS対策: textContent）
    const thumb = document.createElement('div');
    thumb.className = 'sol-mobile-deck-thumb sol-mobile-deck-thumb-proxy';
    thumb.textContent = name || '?';
    cell.appendChild(thumb);
  }

  const check = document.createElement('span');
  check.className = 'sol-mobile-deck-check';
  check.textContent = '✓';
  cell.appendChild(check);

  cell.addEventListener('click', () => _toggleDeckCardSelection(cell));
  return cell;
}

function _toggleDeckCardSelection(cell) {
  const id = cell.dataset.cardId;
  if (_selectedDeckCardIds.has(id)) {
    _selectedDeckCardIds.delete(id);
    cell.classList.remove('selected');
  } else {
    _selectedDeckCardIds.add(id);
    cell.classList.add('selected');
  }
  _updateDeckToHandBtn();
}

function _updateDeckToHandBtn() {
  const btn = document.getElementById('solMobileDeckToHandBtn');
  if (!btn) return;
  const n = _selectedDeckCardIds.size;
  btn.textContent = `手札へ (${n})`;
  btn.disabled = n === 0;
}

/** L-9: 大文字小文字・全角半角を正規化してから部分一致で比較する（NFKC正規化＋小文字化） */
function _normalizeForSearch(s) {
  return (s || '').normalize('NFKC').toLowerCase();
}

/** 検索欄の文字列（部分一致。NFKC正規化＋小文字化してから比較）でセルの表示を絞り込む */
function _applyDeckSearchFilter() {
  const q = _normalizeForSearch((document.getElementById('solMobileDeckSearch')?.value || '').trim());
  document.querySelectorAll('#solMobileDeckGrid .sol-mobile-deck-cell').forEach(cell => {
    const name = _normalizeForSearch(cell.dataset.cardName || '');
    cell.hidden = !(!q || name.includes(q));
  });
}

/** シートを開くたびに #poolRow から再構築する（デッキが変わる可能性があるため）。 */
function _buildDeckGrid() {
  const grid = document.getElementById('solMobileDeckGrid');
  const emptyEl = document.getElementById('solMobileDeckEmpty');
  if (!grid) return;

  grid.innerHTML = '';
  _selectedDeckCardIds.clear();

  const wrappers = Array.from(document.querySelectorAll('#poolRow .tier-item-wrapper'));
  if (wrappers.length === 0) {
    grid.hidden = true;
    if (emptyEl) emptyEl.hidden = false;
    _updateDeckToHandBtn();
    return;
  }

  if (emptyEl) emptyEl.hidden = true;
  grid.hidden = false;
  wrappers.forEach(wrapper => {
    const cell = _buildDeckCell(wrapper);
    if (cell) grid.appendChild(cell);
  });
  _applyDeckSearchFilter();
  _updateDeckToHandBtn();
}

/** 選択したカードを手札へ移動する。移動は必ず executeDrop 経由（リプレイ記録の唯一の経路）。 */
function _moveSelectedDeckCardsToHand() {
  const handSlot = document.querySelector('.sol-hand-area .center-slot');
  if (!handSlot) return;
  const dropZoneInfo = getDropZoneInfo(handSlot);
  if (!dropZoneInfo) return;

  Array.from(_selectedDeckCardIds).forEach(cardId => {
    const cardEl = document.getElementById(cardId);
    const wrapper = cardEl?.closest('.tier-item-wrapper');
    if (!wrapper) return;
    executeDrop({ type: 'card', element: wrapper }, dropZoneInfo, handSlot, {});
  });

  // M-4: 移動後は選択状態を明示的にクリアする（次にシートを開いた時の再構築任せにしない）
  _selectedDeckCardIds.clear();
  _updateDeckToHandBtn();
  closeDeckSheet();
}

function openDeckSheet() {
  const search = document.getElementById('solMobileDeckSearch');
  if (search) search.value = '';
  _buildDeckGrid();
  setHidden('solMobileDeckSheet', false);
}
function closeDeckSheet() {
  setHidden('solMobileDeckSheet', true);
}

function initDeckSheet() {
  const sheet = document.getElementById('solMobileDeckSheet');
  if (!sheet) return;

  sheet.querySelectorAll('[data-sol-deck-sheet-close]').forEach(el => {
    el.addEventListener('click', closeDeckSheet);
  });

  document.getElementById('solMobileDeckSearch')
    ?.addEventListener('input', _applyDeckSearchFilter);

  document.getElementById('solMobileDeckToHandBtn')
    ?.addEventListener('click', _moveSelectedDeckCardsToHand);

  // 副ボタン: 1ドロー・リセット&5ドロー（既存ボタンをclickするだけ。ロジックを複製しない）
  document.getElementById('solMobileDeckDrawBtn')?.addEventListener('click', () => {
    document.getElementById('randomButton')?.click();
    _buildDeckGrid();
  });
  document.getElementById('solMobileDeckResetBtn')?.addEventListener('click', () => {
    document.getElementById('resetButton')?.click();
    _buildDeckGrid();
  });

  // デッキ0枚時: 既存のデッキ読込シートを開く
  document.getElementById('solMobileDeckEmptyLoadBtn')?.addEventListener('click', () => {
    closeDeckSheet();
    openSidebarSheet();
  });
}

// ══════════════════ 共有シート（フェーズ3・設計書 §3.4／H-1で2段階構成に変更） ══════════════════
// (1) 「共有リンクを作成」→ replay-ui.js の generateShareURL() を直接呼ぶ（.click() 配線を
//     やめた。iOS Safari は await の後に user activation が失われ、.click() 経由の
//     clipboard/window.open が沈黙失敗するため）。結果は readonly input に表示し、
//     「共有…」（navigator.share）／「コピー」／「Xに投稿」（本物の<a>）をタップイベント
//     ハンドラの中で同期的に扱う。
// (2) 「JSONを保存」「JSONを読み込む」「今の手にコメント」は従来どおり既存ボタンを
//     .click() で発火させるだけ（ロジックを複製しない）。
// タイトル入力欄: replay-ui.js の _getReplayTitle() が #solMobileShareTitleInput を最優先で
// 読む（PC には無いため PC は従来どおり .title→既定値 の挙動）。

function openShareSheet() {
  // 前回の結果・エラー表示を残さない
  setHidden('solMobileShareResult', true);
  setHidden('solMobileShareError', true);
  const urlInput = document.getElementById('solMobileShareUrlInput');
  if (urlInput) urlInput.value = '';
  const xLink = document.getElementById('solMobileShareXLink');
  if (xLink) xLink.href = '#';
  setHidden('solMobileShareSheet', false);
}
function closeShareSheet() {
  setHidden('solMobileShareSheet', true);
}

function _showShareError(msg) {
  const el = document.getElementById('solMobileShareError');
  if (!el) return;
  el.textContent = msg;
  el.hidden = false;
}

/** H-1: 「共有リンクを作成」。await をまたぐため、この関数自体からは clipboard/share を呼ばない。 */
async function _generateShareLink() {
  const btn = document.getElementById('solMobileShareGenerateBtn');
  const resultEl = document.getElementById('solMobileShareResult');
  const urlInput = document.getElementById('solMobileShareUrlInput');
  const nativeBtn = document.getElementById('solMobileShareNativeBtn');
  const xLink = document.getElementById('solMobileShareXLink');
  if (!btn) return;

  setHidden('solMobileShareError', true);
  btn.disabled = true;
  try {
    const url = await generateShareURL();
    if (urlInput) urlInput.value = url;
    if (xLink) xLink.href = buildShareTweetUrl(url);
    if (nativeBtn) nativeBtn.hidden = typeof navigator.share !== 'function';
    if (resultEl) resultEl.hidden = false;
  } catch (e) {
    _showShareError('リンクを作成できませんでした');
  } finally {
    btn.disabled = false;
  }
}

/**
 * H-1: 「コピー」。タップイベントハンドラの中で同期的に navigator.clipboard.writeText を
 * 呼ぶ（await をまたがない）。失敗時は execCommand('copy') にフォールバックし、
 * それも失敗したら「長押しでコピーしてください」を表示する。
 */
function _copyShareUrl() {
  const input = document.getElementById('solMobileShareUrlInput');
  const url = input?.value || '';
  if (!url) return;
  setHidden('solMobileShareError', true);

  const fallbackCopy = () => {
    try {
      input.select();
      input.setSelectionRange(0, 99999);
      if (document.execCommand('copy')) {
        showToast('リンクをコピーしました');
      } else {
        _showShareError('長押しでコピーしてください');
      }
    } catch {
      _showShareError('長押しでコピーしてください');
    }
  };

  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(url).then(() => {
      showToast('リンクをコピーしました');
    }).catch(fallbackCopy);
  } else {
    fallbackCopy();
  }
}

/** H-1: 「共有…」。navigator.share も同様にタップイベントハンドラ内で直接呼ぶ。 */
function _nativeShareUrl() {
  const url = document.getElementById('solMobileShareUrlInput')?.value || '';
  if (!url || typeof navigator.share !== 'function') return;
  const title = document.getElementById('solMobileShareTitleInput')?.value?.trim() || '一人回し';
  navigator.share({ title, url }).catch(() => { /* ユーザーキャンセル等は無視 */ });
}

function initShareSheet() {
  const sheet = document.getElementById('solMobileShareSheet');
  if (!sheet) return;

  sheet.querySelectorAll('[data-sol-share-sheet-close]').forEach(el => {
    el.addEventListener('click', closeShareSheet);
  });

  // 今の手にコメントを付ける: 既存の #replayCommentInput に値を入れて #replayAddComment を叩く
  document.getElementById('solMobileShareCommentAddBtn')?.addEventListener('click', () => {
    const input = document.getElementById('solMobileShareCommentInput');
    const target = document.getElementById('replayCommentInput');
    if (!input || !target) return;
    target.value = input.value;
    document.getElementById('replayAddComment')?.click();
    input.value = '';
  });

  // (1) リンク作成〜共有・コピー・X投稿
  document.getElementById('solMobileShareGenerateBtn')?.addEventListener('click', () => { _generateShareLink(); });
  document.getElementById('solMobileShareCopyResultBtn')?.addEventListener('click', _copyShareUrl);
  document.getElementById('solMobileShareNativeBtn')?.addEventListener('click', _nativeShareUrl);
  // #solMobileShareXLink は本物の <a href> なのでリスナー登録は不要（href は _generateShareLink が設定）

  // (2) 従来どおり .click() 配線
  document.getElementById('solMobileShareExportBtn')?.addEventListener('click', () => {
    document.getElementById('replayExport')?.click();
  });
  document.getElementById('solMobileShareImportBtn')?.addEventListener('click', () => {
    document.getElementById('replayImportFile')?.click();
  });
}

// ══════════════════ 初見導線（設計書 §3.5） ══════════════════
// M-3: 盤面・手札・墓地・除外・デッキ・EX の .tier-item-wrapper 総数が0のときだけ
// 盤面中央にCTAを出す（カウンター・トークンプレビュー等の他要素は .tier-item-wrapper を
// 使わないため、document 全体をカウントしても他エリアを誤検出しない）。
// 監視は §3.3 で使う既存のデッキ枚数バッジ用オブザーバー（initDeckCountBadge）を流用する。

function updateEmptyCta() {
  if (!isMobilePortrait()) { setHidden('solMobileEmptyCta', true); return; }
  const totalCount = document.querySelectorAll('.tier-item-wrapper').length;
  setHidden('solMobileEmptyCta', totalCount !== 0);
}

function initEmptyCta() {
  document.getElementById('solMobileEmptyCta')?.addEventListener('click', () => { openSidebarSheet(); });
  // M-3: セッション復元（main.js loadSessionResume）完了後に判定を確定させる
  document.addEventListener('sol-session-restored', () => { updateEmptyCta(); });
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
  initDeckLoadProgress();
  initOppTraySheet();
  initSideAreaExpand();
  initCardDetailPanel();
  initActionSheet();
  initDeckSheet();
  initShareSheet();
  initEmptyCta();

  // H-1: resize/orientationchange の逐次発火はやめ、matchMedia の change に一本化する。
  watchMobilePortrait(handlePortraitChange);
}
