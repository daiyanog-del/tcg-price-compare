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
  selectMobileCard,
} from '../components/drag-drop.js';
import {
  toggleCardDefense,
  toggleCardSet,
  returnCardToDeckMenu,
  isCardInPool,
} from './context-menu.js';
import { playActivateEffect } from '../components/card-effects.js';
import { _renderCard, _showUnknown, _showLoading } from './card-info-panel.js';
import { isMobilePortrait, watchMobilePortrait, isMobileLandscape, watchMobileLandscape } from '../utils/viewport.js';
import { showToast } from '../utils/toast.js';
import { generateShareURL, buildShareTweetUrl } from './replay-ui.js';
import { clearEverything } from './deck-input-panel.js';
import {
  getLogLength,
  getSetupLogs,
  buildReplayPayload,
  getImages,
  getExCardIds,
  loadReplayFromCompressedData,
} from '../services/replay-service.js';
import { savedReplaysGet, saveReplayEntry, deleteReplayEntry } from '../services/saved-replays.js';

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
  closeSavedReplaysSheet(); // E-3: 保存済みリプレイ一覧シート
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
  setHidden('solMobileSavedReplaysSheet', true);
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

/** デッキ枚数バッジ（#solMobileDeckCount）を #poolRow の実枚数で更新する */
function updateDeckCountBadge() {
  const poolRow = document.getElementById('poolRow');
  const countEl = document.getElementById('solMobileDeckCount');
  if (!poolRow || !countEl) return;
  countEl.textContent = String(poolRow.querySelectorAll('.tier-item-wrapper').length);
  // §3.5 初見導線: 既存のデッキ枚数バッジ用オブザーバーを流用し、CTAの表示も更新する
  updateEmptyCta();
}

function initDeckCountBadge() {
  const poolRow = document.getElementById('poolRow');
  if (!poolRow) return;
  updateDeckCountBadge();
  new MutationObserver(updateDeckCountBadge).observe(poolRow, { childList: true });
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

/** 「すべて消去」。確認ダイアログでOKされた場合のみ deck-input-panel.js の clearEverything を呼ぶ。 */
async function handleClearEverything() {
  if (!confirm('デッキ・盤面・記録をすべて消去します。よろしいですか？')) return;
  await clearEverything();
  showToast('すべて消去しました');
}

const MENU_ACTIONS = {
  save:     () => { document.getElementById('saveButton2')?.click(); },
  load:     () => { document.getElementById('loadButton')?.click(); },
  reset:    () => { document.getElementById('resetButton')?.click(); },
  clearall: () => { handleClearEverything(); },
  coin:     () => { document.getElementById('coinTossBtn')?.click(); },
  dice:     () => { document.getElementById('diceRollBtn')?.click(); },
  opptray:  () => { openOppTraySheet(); },
  deckinput:() => { openSidebarSheet(); },
  replay:   () => { openReplayOverlay(); },
  savedreplays: () => { openSavedReplaysSheet(); }, // E-3
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

// ══════════════════ C-2: 移動の演出（設計書 §C） ══════════════════
// デッキ一覧シート／1ドロー／リセット&5ドロー／B-1の移動時に、カードのサムネイル複製を
// 出発点（シートのセル。取れなければ下部バーの「デッキ」ボタン）から行き先ゾーンへ
// 300ms で飛ばす（position:fixed の clone。到着後に自身を消す）。
// 実際の移動は必ず executeDrop を先に完了させてから演出するため、演出の有無で
// リプレイ記録・DOM の最終状態は変わらない（C-1調査の結果、既存のドラッグ操作・
// 1ドロー・リセット&5ドローには元々このような移動演出は無い＝ゼロから追加）。

const FLY_DURATION_MS = 300;
const FLY_STAGGER_MS = 60;

function _prefersReducedMotion() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/**
 * 出発点の矩形を返す（cardId に対応するデッキシートのセルがあればそれ、
 * 無ければ＝シートが閉じている場合は下部バーの「デッキ」ボタン）。
 * @param {string|null} cardId
 * @returns {DOMRect|null}
 */
function _getFlyOriginRect(cardId) {
  const deckSheet = document.getElementById('solMobileDeckSheet');
  const sheetOpen = deckSheet && !deckSheet.hasAttribute('hidden');
  if (sheetOpen && cardId) {
    const cell = document.querySelector(`#solMobileDeckGrid .sol-mobile-deck-cell[data-card-id="${CSS.escape(cardId)}"]`);
    if (cell) return cell.getBoundingClientRect();
  }
  const deckBtn = document.getElementById('solMobileDeckBtn');
  return deckBtn ? deckBtn.getBoundingClientRect() : null;
}

/**
 * originRect から targetEl（移動後のカード実体。既に最終位置にある）へ、
 * カードのサムネイル複製を 300ms で飛ばす。prefers-reduced-motion: reduce では何もしない。
 * @param {DOMRect|null} originRect
 * @param {Element} targetEl  .tier-item-wrapper（移動先で最終位置に配置済み）
 * @param {number} [delayMs=0]
 */
function _flyCardClone(originRect, targetEl, delayMs = 0) {
  if (_prefersReducedMotion() || !originRect || !targetEl) return;
  setTimeout(() => {
    if (!targetEl.isConnected) return; // 遅延中に移動先が消えた場合は何もしない
    const targetRect = targetEl.getBoundingClientRect();
    const cardEl = targetEl.querySelector('.tier-item');
    const isImg = cardEl?.tagName === 'IMG';

    let clone;
    if (isImg) {
      clone = document.createElement('img');
      clone.src = cardEl.src;
    } else if (cardEl) {
      // Low-9: プロキシ（div.tier-item）は img を持たず名前等が子要素に描画されているため、
      // 空divではなく cardEl.cloneNode(true) を包んで見た目（名前等）を保つ。
      clone = document.createElement('div');
      clone.appendChild(cardEl.cloneNode(true));
    } else {
      clone = document.createElement('div');
    }
    clone.className = 'sol-fly-clone';
    clone.style.left = `${originRect.left}px`;
    clone.style.top = `${originRect.top}px`;
    clone.style.width = `${originRect.width}px`;
    clone.style.height = `${originRect.height}px`;
    document.body.appendChild(clone);

    requestAnimationFrame(() => {
      void clone.offsetWidth; // reflow 強制（初期位置を確定させる）
      clone.style.left = `${targetRect.left}px`;
      clone.style.top = `${targetRect.top}px`;
      clone.style.width = `${targetRect.width}px`;
      clone.style.height = `${targetRect.height}px`;
    });

    setTimeout(() => clone.remove(), FLY_DURATION_MS + 60);
  }, delayMs);
}

/**
 * 選択中カード群（wrapperEls）を演出付きで移動する。移動そのものは executeFn（executeDrop
 * 呼び出し）を各カードごとに先に実行し、その後 originRectForCardId が返す矩形から
 * wrapper の最終位置へ複製を飛ばす（60ms ずつずらす）。
 * @param {Array<{cardId:string, wrapper:Element}>} items
 * @param {(cardId:string, wrapper:Element)=>void} executeFn
 */
function _moveWithFlyEffect(items, executeFn) {
  items.forEach((item, i) => {
    const originRect = _getFlyOriginRect(item.cardId);
    executeFn(item.cardId, item.wrapper);
    _flyCardClone(originRect, item.wrapper, i * FLY_STAGGER_MS);
  });
}

/**
 * 1ドロー／リセット&5ドローのように「移動対象のカードが呼び出し前には分からない」操作向け。
 * targetContainer 内の実行前 wrapper id 集合との差分で新規カードを検出し、演出を飛ばす。
 * @param {Element|null} targetContainer  移動先の実コンテナ（例: .center-slot）
 * @param {Set<string>} beforeIds
 */
function _flyNewlyAddedCards(targetContainer, beforeIds) {
  if (!targetContainer) return;
  const originRect = _getFlyOriginRect(null); // 特定カードに紐付かないためデッキボタン基準
  const afterWrappers = Array.from(targetContainer.querySelectorAll('.tier-item-wrapper'));
  const newWrappers = afterWrappers.filter(w => !beforeIds.has(w.id));
  newWrappers.forEach((w, i) => _flyCardClone(originRect, w, i * FLY_STAGGER_MS));
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
  _updateDeckActionButtons();
}

/**
 * B-1: 「手札へ (n)」に加え、副行の「墓地へ」「除外へ」「EXへ」は選択1枚以上で有効。
 * 「盤面へ」は選択がちょうど1枚のときだけ有効（B-1指示どおり）。
 */
function _updateDeckActionButtons() {
  const n = _selectedDeckCardIds.size;

  const handBtn = document.getElementById('solMobileDeckToHandBtn');
  if (handBtn) {
    handBtn.textContent = `手札へ (${n})`;
    handBtn.disabled = n === 0;
  }

  ['solMobileDeckToGraveBtn', 'solMobileDeckToBanishBtn', 'solMobileDeckToExBtn'].forEach(id => {
    const btn = document.getElementById(id);
    if (btn) btn.disabled = n === 0;
  });

  const fieldBtn = document.getElementById('solMobileDeckToFieldBtn');
  if (fieldBtn) fieldBtn.disabled = n !== 1;
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
    _updateDeckActionButtons();
    return;
  }

  if (emptyEl) emptyEl.hidden = true;
  grid.hidden = false;
  wrappers.forEach(wrapper => {
    const cell = _buildDeckCell(wrapper);
    if (cell) grid.appendChild(cell);
  });
  _applyDeckSearchFilter();
  _updateDeckActionButtons();
}

/**
 * B-1: 選択したカードを指定のゾーン要素（.center-slot / .side-slot / #poolRow2）へ移動する。
 * 移動は必ず executeDrop 経由（リプレイ記録の唯一の経路）。C-2: 移動演出も併せて行う。
 * @param {Element|null} zoneSlotEl
 */
function _moveSelectedDeckCardsToZone(zoneSlotEl) {
  if (!zoneSlotEl) return;
  const dropZoneInfo = getDropZoneInfo(zoneSlotEl);
  if (!dropZoneInfo) return;

  const items = Array.from(_selectedDeckCardIds)
    .map(cardId => {
      const cardEl = document.getElementById(cardId);
      const wrapper = cardEl?.closest('.tier-item-wrapper');
      return wrapper ? { cardId, wrapper } : null;
    })
    .filter(Boolean);

  _moveWithFlyEffect(items, (cardId, wrapper) => {
    executeDrop({ type: 'card', element: wrapper }, dropZoneInfo, zoneSlotEl, {});
  });

  // M-4: 移動後は選択状態を明示的にクリアする（次にシートを開いた時の再構築任せにしない）
  _selectedDeckCardIds.clear();
  _updateDeckActionButtons();
  closeDeckSheet();
}

/** 選択したカードを手札へ移動する（B-1既存動作）。 */
function _moveSelectedDeckCardsToHand() {
  _moveSelectedDeckCardsToZone(document.querySelector('.sol-hand-area .center-slot'));
}

/** B-1: 選択したカードを墓地へ移動する。 */
function _moveSelectedDeckCardsToGrave() {
  _moveSelectedDeckCardsToZone(getSideSlot(true));
}

/** B-1: 選択したカードを除外へ移動する。 */
function _moveSelectedDeckCardsToBanish() {
  _moveSelectedDeckCardsToZone(getSideSlot(false));
}

/** B-1: 選択したカードをEXデッキへ移動する。#poolRow2 は getDropZoneInfo で POOL 判定になる。 */
function _moveSelectedDeckCardsToEx() {
  _moveSelectedDeckCardsToZone(document.getElementById('poolRow2'));
}

/**
 * B-2: 「盤面へ」。選択がちょうど1枚のときだけ有効。
 * シートを閉じて、そのカードを drag-drop.js のタップ状態機械の selected 状態にする
 * （既存の selectMobileCard を export して呼ぶ。executeDrop はここでは呼ばない。
 * ユーザーが盤面スロットをタップした時点で既存のタップ移動フローが executeDrop する）。
 */
function _moveSelectedDeckCardToField() {
  if (_selectedDeckCardIds.size !== 1) return;
  const cardId = Array.from(_selectedDeckCardIds)[0];
  const cardEl = document.getElementById(cardId);
  const wrapper = cardEl?.closest('.tier-item-wrapper');
  if (!wrapper) return;
  _selectedDeckCardIds.clear();
  _updateDeckActionButtons();
  closeDeckSheet();
  selectMobileCard(wrapper);
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
  // B-1: 副行（墓地へ／除外へ／EXへ／盤面へ）
  document.getElementById('solMobileDeckToGraveBtn')
    ?.addEventListener('click', _moveSelectedDeckCardsToGrave);
  document.getElementById('solMobileDeckToBanishBtn')
    ?.addEventListener('click', _moveSelectedDeckCardsToBanish);
  document.getElementById('solMobileDeckToExBtn')
    ?.addEventListener('click', _moveSelectedDeckCardsToEx);
  document.getElementById('solMobileDeckToFieldBtn')
    ?.addEventListener('click', _moveSelectedDeckCardToField);

  // 副ボタン: 1ドロー・リセット&5ドロー（既存ボタンをclickするだけ。ロジックを複製しない）。
  // C-2: 移動先（手札 .center-slot）の実行前 wrapper id 集合との差分で新規カードを検出して演出する。
  document.getElementById('solMobileDeckDrawBtn')?.addEventListener('click', () => {
    const handSlot = document.querySelector('.sol-hand-area .center-slot');
    const beforeIds = new Set(Array.from(handSlot?.querySelectorAll('.tier-item-wrapper') || []).map(w => w.id));
    document.getElementById('randomButton')?.click();
    _flyNewlyAddedCards(handSlot, beforeIds);
    _buildDeckGrid();
  });
  document.getElementById('solMobileDeckResetBtn')?.addEventListener('click', () => {
    const handSlot = document.querySelector('.sol-hand-area .center-slot');
    const beforeIds = new Set(Array.from(handSlot?.querySelectorAll('.tier-item-wrapper') || []).map(w => w.id));
    document.getElementById('resetButton')?.click();
    _flyNewlyAddedCards(handSlot, beforeIds);
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
  _initShareRangeSliders(); // D-1: 開くたびに現在の手数で範囲スライダーをリセット（既定=全範囲）
  _updateShareCurrentInfo(); // タスクA: 「現在の記録: N手（メインM枚・EX E枚）」
  _updateShareOpenSavedButton(); // タスクA: 保存済みが1件以上あれば③に「保存済みリプレイを開く」を出す
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

// ── D-1: 共有する範囲（開始・終了スライダー） ──

/** シートを開くたびに現在の手数(N)で min/max を張り直し、既定=全範囲(1〜N)に戻す */
function _initShareRangeSliders() {
  const total = Math.max(1, getLogLength());
  const startEl = document.getElementById('solMobileShareRangeStart');
  const endEl = document.getElementById('solMobileShareRangeEnd');
  if (!startEl || !endEl) return;
  startEl.min = '1'; startEl.max = String(total); startEl.value = '1';
  endEl.min = '1'; endEl.max = String(total); endEl.value = String(total);
  _updateShareRangeLabel();
}

function _updateShareRangeLabel() {
  const startEl = document.getElementById('solMobileShareRangeStart');
  const endEl = document.getElementById('solMobileShareRangeEnd');
  const labelEl = document.getElementById('solMobileShareRangeLabel');
  if (!startEl || !endEl || !labelEl) return;
  const total = getLogLength();
  const start = parseInt(startEl.value, 10);
  const end = parseInt(endEl.value, 10);
  // タスクA: 開始=1かつ終了=全手数なら「全範囲」とわかる表記にする
  const isFull = start === 1 && end === total;
  labelEl.textContent = isFull
    ? `${start}手目〜${end}手目（全範囲）`
    : `${start}手目〜${end}手目（全${total}手）`;
}

/**
 * タスクA: 「−」「＋」ボタンで開始/終了スライダーを1手ずつ動かす。
 * min/max は _initShareRangeSliders が張った値をそのまま使う（相互クランプは
 * 既存の _onShareRangeInput に委譲し、判定ロジックを複製しない）。
 * @param {'start'|'end'} which
 * @param {number} delta  -1 または 1
 */
function _stepShareRange(which, delta) {
  const el = document.getElementById(which === 'start' ? 'solMobileShareRangeStart' : 'solMobileShareRangeEnd');
  if (!el) return;
  const min = parseInt(el.min, 10);
  const max = parseInt(el.max, 10);
  const next = Math.max(min, Math.min(max, parseInt(el.value, 10) + delta));
  el.value = String(next);
  _onShareRangeInput(which);
}

/**
 * High-2: コメント追加でログが1件増えた直後に、範囲スライダーの max を新しい手数へ追従させる。
 * end が「変更前の max（＝全範囲の終端）」と同じ値だった場合のみ新しい max に追従させ、
 * ユーザーが明示的に途中の手数を指定していた場合はその値をそのまま保持する。
 * start は 1〜newTotal の範囲を外れない限りそのまま保持する（1は常に有効な最小値のため
 * 特別な追従処理は不要）。
 */
function _refreshShareRangeAfterCommentAdd() {
  const startEl = document.getElementById('solMobileShareRangeStart');
  const endEl = document.getElementById('solMobileShareRangeEnd');
  if (!startEl || !endEl) return;

  const oldEndMax = parseInt(endEl.max, 10) || 1;
  const endWasAtMax = parseInt(endEl.value, 10) === oldEndMax;

  const newTotal = Math.max(1, getLogLength());
  startEl.max = String(newTotal);
  endEl.max = String(newTotal);

  if (endWasAtMax) endEl.value = String(newTotal);

  // 値が新しい max を超えていれば安全のためクランプする（通常は増加方向なので発生しない）
  if (parseInt(startEl.value, 10) > newTotal) startEl.value = String(newTotal);
  if (parseInt(endEl.value, 10) > newTotal) endEl.value = String(newTotal);
  // start > end にならないよう最終調整
  if (parseInt(startEl.value, 10) > parseInt(endEl.value, 10)) startEl.value = endEl.value;

  _updateShareRangeLabel();
}

/** 開始 > 終了にならないよう相互クランプする */
function _onShareRangeInput(which) {
  const startEl = document.getElementById('solMobileShareRangeStart');
  const endEl = document.getElementById('solMobileShareRangeEnd');
  if (!startEl || !endEl) return;
  const s = parseInt(startEl.value, 10);
  const e = parseInt(endEl.value, 10);
  if (which === 'start' && s > e) endEl.value = startEl.value;
  else if (which === 'end' && e < s) startEl.value = endEl.value;
  _updateShareRangeLabel();
}

/** @returns {{start:number, end:number}} 1-indexed 手数の範囲（既定値=全範囲でも数値を返す） */
function _getShareRange() {
  const startEl = document.getElementById('solMobileShareRangeStart');
  const endEl = document.getElementById('solMobileShareRangeEnd');
  const total = Math.max(1, getLogLength());
  return {
    start: startEl ? parseInt(startEl.value, 10) : 1,
    end: endEl ? parseInt(endEl.value, 10) : total,
  };
}

/** H-1: 「共有リンクを作成」。await をまたぐため、この関数自体からは clipboard/share を呼ばない。 */
async function _generateShareLink() {
  const btn = document.getElementById('solMobileShareGenerateBtn');
  const resultEl = document.getElementById('solMobileShareResult');
  const urlInput = document.getElementById('solMobileShareUrlInput');
  const nativeBtn = document.getElementById('solMobileShareNativeBtn');
  const xLink = document.getElementById('solMobileShareXLink');
  if (!btn) return;

  // Medium-7: setup（初期配置）込みでも手数0なら記録なし扱いで止める
  if (getLogLength() + getSetupLogs().length === 0) {
    _showShareError('記録がありません');
    return;
  }

  setHidden('solMobileShareError', true);
  btn.disabled = true;
  try {
    const url = await generateShareURL(_getShareRange()); // D-2: 共有する範囲を渡す
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
    // High-2: コメント追加で手数が1増えるため、範囲スライダーの max を追従させる
    _refreshShareRangeAfterCommentAdd();
  });

  // D-1: 共有する範囲（開始・終了スライダー、相互クランプ）
  document.getElementById('solMobileShareRangeStart')?.addEventListener('input', () => _onShareRangeInput('start'));
  document.getElementById('solMobileShareRangeEnd')?.addEventListener('input', () => _onShareRangeInput('end'));
  // タスクA: 「−」「＋」ボタン（1手ずつ動かせる）
  sheet.querySelectorAll('[data-share-range-step]').forEach(btn => {
    btn.addEventListener('click', () => {
      _stepShareRange(btn.dataset.shareRangeStep, parseInt(btn.dataset.shareRangeDelta, 10));
    });
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

  // E-2: 端末に保存
  document.getElementById('solMobileShareSaveLocalBtn')?.addEventListener('click', _saveReplayToDevice);

  // タスクA: ③「保存済みリプレイを開く（n件）」→ 保存済み一覧シートへ切り替える
  document.getElementById('solMobileShareOpenSavedBtn')?.addEventListener('click', () => {
    closeShareSheet();
    openSavedReplaysSheet();
  });
}

// ══════════════════ E-1/E-2: リプレイの端末保存（設計書 §E） ══════════════════

/** タイトル欄が空のときの既定タイトル（YYYY-MM-DD HH:MM） */
function _formatDefaultReplayTitle() {
  const d = new Date();
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/**
 * タスクA: 現在の記録（setup込みの手数・メイン/EX枚数）をまとめて返す。
 * 共有シート冒頭の「現在の記録」表示と、端末保存（_saveReplayToDevice）の両方で使う
 * （手数・メイン/EX枚数の算出ロジックを二重に持たない）。
 */
function _getReplayCounts() {
  const total = getLogLength() + getSetupLogs().length; // Low-13: setupのみの盤面も1手以上として扱う
  const images = getImages();
  const exCount = new Set(getExCardIds()).size;
  const mainCount = Math.max(0, Object.keys(images).length - exCount);
  return { total, main: mainCount, ex: exCount };
}

/** タスクA: 共有シート冒頭の「現在の記録: N手（メインM枚・EX E枚）」を更新する */
function _updateShareCurrentInfo() {
  const el = document.getElementById('solMobileShareCurrentInfo');
  if (!el) return;
  const { total, main, ex } = _getReplayCounts();
  el.textContent = `現在の記録: ${total}手（メイン${main}枚・EX${ex}枚）`;
}

/** タスクA: ③に「保存済みリプレイを開く（n件）」を出す（0件なら隠す） */
function _updateShareOpenSavedButton() {
  const btn = document.getElementById('solMobileShareOpenSavedBtn');
  if (!btn) return;
  const n = savedReplaysGet().length;
  if (n === 0) {
    btn.setAttribute('hidden', '');
    return;
  }
  btn.textContent = `保存済みリプレイを開く（${n}件）`;
  btn.removeAttribute('hidden');
}

/**
 * E-2: 共有シートの「端末に保存」。現在のリプレイ全体（共有する範囲の指定は影響しない）を
 * exportReplay と同じ payload（buildReplayPayload）で組み立て、LZString 圧縮して
 * saved-replays.js の localStorage 一覧に追加する。
 */
function _saveReplayToDevice() {
  const { total, main: mainCount, ex: exCount } = _getReplayCounts();
  if (total === 0) { showToast('記録がありません'); return; }
  if (typeof LZString === 'undefined') { showToast('保存に失敗しました'); return; }

  const titleInput = document.getElementById('solMobileShareTitleInput');
  const title = titleInput?.value?.trim() || _formatDefaultReplayTitle();

  const payload = buildReplayPayload(title);
  const compressed = LZString.compress(JSON.stringify(payload));

  const result = saveReplayEntry({
    title,
    main: mainCount,
    ex: exCount,
    steps: total,
    data: compressed,
  });

  // High-3: 保存失敗時は理由に応じたトーストを出す
  if (!result.ok) {
    if (result.reason === 'too_large') {
      showToast('このリプレイは大きすぎて保存できません');
    } else {
      showToast('保存できませんでした（端末の空き容量）');
    }
    return;
  }
  // タスクA: 保存先とたどり方が分かる文言に変更
  showToast(result.removedCount > 0
    ? `端末に保存しました（上限のため古い${result.removedCount}件を削除）。⋯メニュー →「保存済みリプレイ」から開けます`
    : '端末に保存しました。⋯メニュー →「保存済みリプレイ」から開けます');
  _updateShareOpenSavedButton(); // 保存直後に件数を反映
}

// ══════════════════ E-3: 保存済みリプレイ一覧シート（設計書 §E） ══════════════════

function openSavedReplaysSheet() {
  _renderSavedReplaysList();
  setHidden('solMobileSavedReplaysSheet', false);
}
function closeSavedReplaysSheet() {
  setHidden('solMobileSavedReplaysSheet', true);
}

/**
 * E-3: 保存済みリプレイを開く。importReplay と同じ経路（loadReplayFromCompressedData）で読み込む。
 * Medium-4: 呼び出し元が成否で分岐できるよう Promise<boolean> を返す。
 * Medium-5: 現在の記録（setup込み）が残っている場合は上書き前に確認する。
 * @param {{data:string, title?:string}} item
 * @returns {Promise<boolean>}
 */
async function _openSavedReplay(item) {
  const hasCurrentRecord = (getLogLength() + getSetupLogs().length) > 0;
  if (hasCurrentRecord && !confirm('現在の記録を破棄して開きます。よろしいですか？')) {
    return false;
  }
  try {
    await loadReplayFromCompressedData(item.data);
    closeSavedReplaysSheet();
    showToast('リプレイを復元しました');
    return true;
  } catch (e) {
    console.warn('[mobile-ui] 保存済みリプレイの復元に失敗:', e);
    showToast('復元に失敗しました');
    return false;
  }
}

/** 一覧を再描画する（削除後・開くたびに呼ぶ）。カード名等ユーザー由来文字列は textContent で入れる（XSS対策）。 */
function _renderSavedReplaysList() {
  const listEl = document.getElementById('solMobileSavedReplaysList');
  const emptyEl = document.getElementById('solMobileSavedReplaysEmpty');
  if (!listEl) return;

  const items = savedReplaysGet().slice().sort((a, b) => (b.savedAt || 0) - (a.savedAt || 0));
  listEl.innerHTML = '';

  if (items.length === 0) {
    if (emptyEl) emptyEl.hidden = false;
    return;
  }
  if (emptyEl) emptyEl.hidden = true;

  items.forEach(item => {
    // タスクA: 行全体をタップで「開く」、右端「…」で共有/削除のミニメニュー。
    // 行の中に共有/削除の<button>を含めるため、行自体は<button>ではなくrole="button"のdivにする
    // （<button>の中に<button>を入れるとブラウザがDOM構造を勝手に組み替えてしまうため）。
    const row = document.createElement('div');
    row.className = 'sol-mobile-saved-replay-row';
    row.setAttribute('role', 'button');
    row.setAttribute('tabindex', '0');
    row.addEventListener('click', () => _openSavedReplay(item));
    row.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); _openSavedReplay(item); }
    });

    const info = document.createElement('div');
    info.className = 'sol-mobile-saved-replay-info';
    const titleEl = document.createElement('span');
    titleEl.className = 'sol-mobile-saved-replay-title';
    titleEl.textContent = item.title || '無題';
    const metaEl = document.createElement('span');
    metaEl.className = 'sol-mobile-saved-replay-meta';
    const dateStr = item.savedAt
      ? new Date(item.savedAt).toLocaleString('ja-JP', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
      : '';
    metaEl.textContent = `${dateStr} ・ ${item.steps ?? 0}手・メイン${item.main ?? 0}・EX${item.ex ?? 0}`;
    info.appendChild(titleEl);
    info.appendChild(metaEl);

    const actions = document.createElement('div');
    actions.className = 'sol-mobile-saved-replay-actions';

    const moreBtn = document.createElement('button');
    moreBtn.type = 'button';
    moreBtn.className = 'sol-mobile-saved-replay-more';
    moreBtn.textContent = '…';
    moreBtn.setAttribute('aria-label', 'その他の操作');

    const menu = document.createElement('div');
    menu.className = 'sol-mobile-saved-replay-menu';
    menu.hidden = true;

    const shareBtn = document.createElement('button');
    shareBtn.type = 'button';
    shareBtn.textContent = '共有';
    shareBtn.addEventListener('click', async (ev) => {
      ev.stopPropagation(); // 行タップ（開く）を誘発しない
      // Medium-4: 復元成功時（true）だけ共有シートを開く
      const ok = await _openSavedReplay(item);
      if (!ok) return;
      closeSavedReplaysSheet();
      openShareSheet();
    });

    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.textContent = '削除';
    delBtn.addEventListener('click', (ev) => {
      ev.stopPropagation(); // 行タップ（開く）を誘発しない
      if (!confirm(`「${item.title || '無題'}」を削除します。よろしいですか？`)) return;
      deleteReplayEntry(item.id);
      _renderSavedReplaysList();
    });

    moreBtn.addEventListener('click', (ev) => {
      ev.stopPropagation(); // 行タップ（開く）を誘発しない
      menu.hidden = !menu.hidden;
    });

    menu.appendChild(shareBtn);
    menu.appendChild(delBtn);
    actions.appendChild(moreBtn);
    actions.appendChild(menu);
    row.appendChild(info);
    row.appendChild(actions);
    listEl.appendChild(row);
  });
}

function initSavedReplaysSheet() {
  const sheet = document.getElementById('solMobileSavedReplaysSheet');
  if (!sheet) return;
  sheet.querySelectorAll('[data-sol-saved-replays-close]').forEach(el => {
    el.addEventListener('click', closeSavedReplaysSheet);
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

/**
 * 「すべて消去」（deck-input-panel.js clearEverything）完了後の通知を受けて、
 * デッキ枚数バッジ・CTA・下部バーの取消/共有ボタンの表示を確実に更新する。
 * poolRow の MutationObserver や #replayUndo の disabled 監視でも追従するはずだが、
 * 非同期処理をまたぐため念のためここでも明示的に更新する。
 */
function initClearEverything() {
  document.addEventListener('sol-board-cleared', () => {
    if (!isMobilePortrait()) return;
    updateDeckCountBadge();
    updateReplayBarMobile();
  });
}

// ══════════════════ A-3: 横向きスマホの「縦向き推奨」帯 ══════════════════
// PC幅ではメディアクエリ自体（(orientation:landscape) and (max-height:499px)）が
// 発火しないため CSS 側で常に非表示。ここでは横向きスマホの該当/非該当に応じて
// hidden を付け外しし、閉じた記憶を sessionStorage に残す。

const LANDSCAPE_HINT_CLOSED_KEY = 'sol-landscape-hint-closed';

function updateLandscapeHint() {
  const el = document.getElementById('solLandscapeHint');
  if (!el) return;
  if (sessionStorage.getItem(LANDSCAPE_HINT_CLOSED_KEY)) {
    el.setAttribute('hidden', '');
    return;
  }
  if (isMobileLandscape()) el.removeAttribute('hidden');
  else el.setAttribute('hidden', '');
}

function initLandscapeHint() {
  document.getElementById('solLandscapeHintClose')?.addEventListener('click', () => {
    sessionStorage.setItem(LANDSCAPE_HINT_CLOSED_KEY, '1');
    setHidden('solLandscapeHint', true);
  });
  updateLandscapeHint();
  watchMobileLandscape(updateLandscapeHint);
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
  initCardDetailPanel();
  initActionSheet();
  initDeckSheet();
  initShareSheet();
  initSavedReplaysSheet();
  initEmptyCta();
  initClearEverything();
  initLandscapeHint(); // A-3: portrait 判定とは独立（横向きスマホでのみ表示するため）

  // H-1: resize/orientationchange の逐次発火はやめ、matchMedia の change に一本化する。
  watchMobilePortrait(handlePortraitChange);
}
