/**
 * mobile-ui.js — スマホ縦向き専用UI（フェーズ1・設計書 §8。2026-09-02 reviewer監査反映）
 *
 * 下部固定バー・⋯簡易メニュー・選択中カードのアクションシート・カード詳細パネル・
 * デッキ読込ボトムシート・縦タブの全幅展開を配線する。
 *
 * 方針:
 *   - isMobilePortrait()（utils/viewport.js。CSS の @media 判定と統一・A-1）以外では
 *     何もしない。CSS側は @media (max-width:767px) and (orientation:portrait) に
 *     閉じているため、hidden 属性の付け外し以外は自動的に無効化される。
 *   - カード移動は必ず drag-drop.js の executeDrop 経由（getMobileSelection /
 *     moveMobileSelectionTo 経由）。判定ロジックを複製しない。
 *   - 守備表示・セット・デッキに戻す は context-menu.js の既存処理を呼ぶ（削除はアクション
 *     シートから外した・A-3）。
 *   - プール内カード（デッキ/EXデッキ）を選択した場合は、操作行のうち「墓地へ」「除外へ」のみ
 *     表示する（2026-09-15: 従来は操作行を丸ごと隠していたが、モバイルでも墓地送り/除外を
 *     使えるようユーザー決定で変更。詳細は docs/decisions.md 参照）。
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
  setMobilePlaybackMode,
  setMobilePlaceState,
} from '../components/drag-drop.js';
import { isMonsterCard } from '../components/card-state.js';
import {
  toggleCardDefense,
  toggleCardSet,
  returnCardToDeckMenu,
  isCardInPool,
  openContextMenuAt,
} from './context-menu.js';
import { playActivateEffect, flipMoveClone } from '../components/card-effects.js';
import { _renderCard, _showUnknown, _showLoading, showTokenInfo } from './card-info-panel.js';
import { placeToken, tokenNameOf, getLastTokenName, preloadTokenBase, TOKEN_NAME_MAX, DEFAULT_TOKEN_NAME } from './token-generator.js';
import { isMobilePortrait, watchMobilePortrait, isMobileLandscape, watchMobileLandscape } from '../utils/viewport.js';
import { enterRecView, exitRecView } from './rec-view.js';
import { showToast } from '../utils/toast.js';
import { guardUndoable, offerUndo, discardUndoPoint } from './undo-point.js';
import { resetMobileDeckMode, openDeckImage } from './deck-edit-ui.js';
import { generateShareURL, checkPublicTitle, resetShareUrlMemo, buildShareTweetUrl, getReplayCounts, saveReplayToDevice, describeSaveResult, exportReplayFile } from './replay-ui.js';
import { clearEverything } from './deck-input-panel.js';
import {
  getLogLength,
  getSetupLogs,
  getCursor,
  seekTo,
  subscribeReplay,
  addComment,
} from '../services/replay-service.js';
import { shiftRangeAfterInsert, shiftRangeAfterRemove, COMMENT_MAX_LEN, CUTIN_MAX_LEN, CUTIN_PRESETS } from '../services/comment-model.js';
import { setCardCommentOpener, setMemoFullOpener } from './comment-ui.js';
import { buildDisruptionPicker } from './cutin-disruption-picker.js';
import { getCurrentBubbleText } from '../services/comment-view.js';
import { savedReplaysGet } from '../services/saved-replays.js';
import { sharedReplaysGet } from '../services/shared-replays.js';

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
 * #solSidebar.sol-sheet-open のシートクラス、
 * .sol-ops-details の open、#solRightPanel.sol-mobile-help-active を全て元に戻す。
 * 選択状態（drag-drop.js 側）もクリアする。
 * 以前は resize が非該当時に早期 return するだけで hidden を戻さず、回転やウィンドウ幅変更で
 * portrait を外れると素の div として PC/横向きに露出するバグがあった。
 */
function resetMobileUI() {
  closeMenuSheet();
  closeSidebarSheet();
  closeHelpSheet();
  closeCardDetailOverlay();
  closeDeckSheet();   // フェーズ2: デッキ一覧シート
  closeShareSheet();  // フェーズ3: 共有シート
  closeCommentSheet(); // 整理④: コメント入力シート
  closeTokenSheet();   // トークンの名前のシート
  closeTokenPick();    // トークンを出すマスを選ぶ途中ならやめる
  // タスクB: portrait を外れる際は再生モードも強制終了。第6次検収-1: 回転時は
  // origin（'share'等）に関係なく共有シートへ戻らせない（この直後に setHidden で強制的に閉じる）。
  exitPlaybackMode({ returnToOrigin: false });
  _resetRecording();  // タスクA-1: 録画状態も破棄する
  clearMobileSelection();
  clearPendingMobileDrop(); // 残件6: 保留ドロップと保留元カードの選択見た目も消す

  setHidden('solMobileBar', true);
  setHidden('solMobileReplayStrip', true);
  setHidden('solMobileActionSheet', true);
  setHidden('solMobileMenuSheet', true);
  setHidden('solMobileCip', true);
  setHidden('solSidebarScrim', true);
  setHidden('solMobileUndoBtn', true);
  setHidden('solMobilePlayBtn', true);
  setHidden('solMobileDeckSheet', true);
  setHidden('solMobileShareSheet', true);
  setHidden('solMobileCommentSheet', true);
  setHidden('solMobileMemoSheet', true);
  setHidden('solMobileEmptyCta', true);
  setDeckBtnLoading(false); // 読込中表示のまま portrait を外れた場合の保険
}

/**
 * H-1: portrait の該当/非該当が切り替わった瞬間だけ呼ばれる（matchMedia change に一本化。
 * resize/orientationchange の逐次発火はやめる＝B-13 のデバウンスも自然に不要になる）。
 */
function handlePortraitChange(matches) {
  // 画面録画用の表示は向きごとに作りが違う（縦向きは再生モードの上に載る）ので、切り替わったら先に閉じる。
  // 再生モードの終了（resetMobileUI）より前に閉じ、下部の予約量を戻す順番にする
  exitRecView();
  closeRangePicker({ silent: true }); // 範囲を選ぶ画面も縦向きスマホ専用なので閉じる
  if (matches) {
    updateMobileVisibility();
  } else {
    resetMobileUI();
  }
}

// ══════════════════ デッキ枚数バッジ・下部バーの基本ボタン ══════════════════

/**
 * デッキ枚数バッジ（#solMobileDeckCount）を #poolRow の実枚数で更新する。
 * 第6次検収-2a: 再生モードの帯にある #solMobilePlayDeckCount も同じ枚数に追従させる
 * （デッキ演出の錨バッジが古い枚数のまま表示され続けないように）。
 */
function updateDeckCountBadge() {
  const poolRow = document.getElementById('poolRow');
  const countEl = document.getElementById('solMobileDeckCount');
  const playCountEl = document.getElementById('solMobilePlayDeckCount');
  if (!poolRow) return;
  const n = String(poolRow.querySelectorAll('.tier-item-wrapper').length);
  if (countEl) countEl.textContent = n;
  if (playCountEl) playCountEl.textContent = n;
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

  // 再生: ⋯メニュー「リプレイを再生」と同じ再生モードに入る（2026-10-16 案A）
  // 押したらすぐ再生も始める（再生モードに入るだけだと、もう一度押す必要があったため）
  document.getElementById('solMobilePlayBtn')
    ?.addEventListener('click', () => {
      enterPlaybackMode('menu');
      document.getElementById('solMobilePlayToggleBtn')?.click();
    });

  // タスクA-2: 録画開始/停止（トグル）
  document.getElementById('solMobileRecordBtn')
    ?.addEventListener('click', () => { toggleRecording(); });

  // ⋯ メニュー
  document.getElementById('solMobileMenuBtn')
    ?.addEventListener('click', () => { openMenuSheet(); });
}

/**
 * A-4: 旧リプレイバーは縦向きスマホでは常時 display:none（mobile.css）。
 * 取消は下部バーの #solMobileUndoBtn だけが表示を担う。
 * リプレイの手数（#replayUndo の disabled 属性）を見て表示を切り替える。
 * タスクA-5: 共有ボタンは下部バーから外した。2026-10-16 から再生ボタンもここで出し入れする
 * （#replayPlay の disabled＝記録0件を見る）。
 */
function updateReplayBarMobile() {
  if (!isMobilePortrait()) return;
  const undoBtn = document.getElementById('replayUndo');
  if (!undoBtn) return;
  setHidden('solMobileUndoBtn', undoBtn.disabled);
  // 再生は記録が1手以上あれば出す（#replayPlay の disabled は「記録0件」のときだけ立つ）
  const playBtn = document.getElementById('replayPlay');
  if (playBtn) setHidden('solMobilePlayBtn', playBtn.disabled);
}

function initReplayBarMobile() {
  const undoBtn = document.getElementById('replayUndo');
  if (!undoBtn) return;
  updateReplayBarMobile();
  new MutationObserver(updateReplayBarMobile).observe(undoBtn, { attributes: true, attributeFilter: ['disabled'] });
  const playBtn = document.getElementById('replayPlay');
  if (playBtn) new MutationObserver(updateReplayBarMobile).observe(playBtn, { attributes: true, attributeFilter: ['disabled'] });
}

// ══════════════════ ⋯ メニューシート ══════════════════

function openMenuSheet() {
  setHidden('solMobileMenuSheet', false);
}
function closeMenuSheet() {
  setHidden('solMobileMenuSheet', true);
}

/**
 * 「クリア」。以前は確認窓を出していたが、今は直前の写しを取ってすぐ消し、6秒間「元に戻す」を出す（undo-point.js。PC の clearButton と同じ）。
 * clearEverything はデッキ読込中だと何もせず false を返す。
 */
async function handleClearEverything() {
  // 写しを取れない（記録が大きすぎる等）ときだけ確認の小窓を出す
  const g = await guardUndoable(null, '記録が大きいため、消すと元に戻せません。デッキ・盤面・記録をすべて消しますか？', '消す');
  if (!g.ok) return;
  if (!(await clearEverything())) {
    discardUndoPoint(g.id);
    showToast('デッキ読込中のためクリアできませんでした');
    return;
  }
  if (g.id) offerUndo('デッキ・盤面・記録をすべて消しました', g.id);
  else showToast('クリアしました');
}

const MENU_ACTIONS = {
  reset:    () => { document.getElementById('resetButton')?.click(); },
  clearall: () => { handleClearEverything(); },
  coin:     () => { document.getElementById('coinTossBtn')?.click(); },
  dice:     () => { document.getElementById('diceRollBtn')?.click(); },
  deckinput:() => { openSidebarSheet(); },
  playback: () => { enterPlaybackMode('menu'); }, // タスクB-1: 旧「リプレイ操作」オーバーレイを廃止し再生モードへ
  recview:  () => { enterRecView(); }, // 画面録画用の表示（rec-view.js が再生モードへ入れてから下部バーを隠す）
  share:    () => { openShareSheet('share'); }, // ⋯メニューの「共有」
  deckimage:() => { openDeckImage(); },          // デッキの一覧を画像にする（deck-edit-ui.js → shared/deck-image.js）
  boardmemo:() => { openCommentSheet({ kind: 'board' }); }, // 整理④: 盤面にメモ
  cutin:    () => { openCommentSheet({ kind: 'cutin' }); },  // カットイン: 言葉の札＋自由入力のシート
  token:    () => { openTokenSheet(); },                     // トークン: 名前のシート → 出すマスをタップ
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
      // deckinput/help/replay などはそれぞれのシートへ引き継ぐため、メニュー自体は閉じる
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
    closeRangePicker({ silent: true });
    _resetRecording(); // タスクA-1: デッキ再読込で録画状態を破棄する
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
      _renderCard(data.proxy, imgSrc, name, null, body, false, false, true);
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
  // そのカードの今の吹き出しを全文で出す（盤面上の吹き出しは2行で省略されるため）
  const bubbleText = getCurrentBubbleText(sel.element.querySelector('.tier-item')?.id || '');
  const bubbleEl = document.getElementById('solMobileCipBubble');
  const bubbleTextEl = document.getElementById('solMobileCipBubbleText');
  if (bubbleTextEl) bubbleTextEl.textContent = bubbleText || ''; // 本文は textContent で入れる
  if (bubbleEl) bubbleEl.hidden = !bubbleText;
  setHidden('solMobileCip', false);
  // トークンは情報を引かない（名前は利用者が付けたもの。card-info-panel.js と同じ扱い）
  if (sel.element.dataset.token === '1') {
    showTokenInfo(src, name, document.getElementById('solMobileCipBody'));
    return;
  }
  showMobileCardInfo(name, src);
}
/** 選択状態なしで名前と画像から詳細を開く（?newcards=1&focus= の自動表示用。同じ #solMobileCip を使う）。 */
export function openCardDetailByName(name, src) {
  setHidden('solMobileCipBubble', true); // 選択したカードが無いので吹き出しは出さない
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

/**
 * M-11: moveMobileSelectionTo が失敗（false）したときに原因を追える警告を出す。
 * アクションシート経由の「墓地へ」「除外へ」は executeDrop の即時DOM移動だけで
 * FLIPアニメーションを一切呼んでいなかった（瞬間移動して見えるバグ）ため、
 * 移動前の座標を取得しておき、成功後に flipMoveClone で繋ぐ。
 * @param {Element} wrapper        - .tier-item-wrapper（移動対象。移動前座標の取得に使う）
 */
function moveSelectionOrWarn(wrapper, dropZoneElement, label) {
  const firstRect = wrapper ? wrapper.getBoundingClientRect() : null;
  const ok = moveMobileSelectionTo(dropZoneElement);
  if (!ok) {
    console.warn(`[mobile-ui] 移動先が見つからない: ${label}`);
    return;
  }
  if (wrapper && firstRect) flipMoveClone(wrapper, firstRect);
}

// A-3: 「削除」は外す（長押しメニューに残る）。6操作のみ。
// grave/banish は moveSelectionOrWarn 経由で moveMobileSelectionTo を呼ぶため、
// 選択中カードの現在ゾーン（フィールドでもプールでも）を問わず同じハンドラで動く。
const ACTION_HANDLERS = {
  defense: (wrapper) => { toggleCardDefense(wrapper); clearMobileSelection(); },
  set:     (wrapper) => { toggleCardSet(wrapper); clearMobileSelection(); },
  grave:   (wrapper) => { moveSelectionOrWarn(wrapper, getSideSlot(true), '墓地'); },
  banish:  (wrapper) => { moveSelectionOrWarn(wrapper, getSideSlot(false), '除外'); },
  return:  (wrapper) => { returnCardToDeckMenu(wrapper); clearMobileSelection(); },
  bubble:  (wrapper) => { openCardCommentSheet(wrapper); clearMobileSelection(); }, // 整理④: カードに吹き出し
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
 * 選択カードがプール内（#poolRow/#poolRow2 の子）の場合は、操作行のうち意味を持つ
 * 「墓地へ」「除外へ」だけを表示し、フィールド専用の「守備表示」「セット」「効果発動」と
 * 既にデッキ内のため無意味な「デッキに戻す」、デッキ内には付けない「吹き出し」は隠す（context-menu.js の isCardInPool を共有。
 * 判定を二重に持たない）。
 * 「手札に戻す」「場に出す」はここでは追加しない: プール内カードは drag-drop.js の
 * selectMobileCard()（タップ選択）→ _onDocTouchEnd（移動先スロットをタップして確定）という
 * 既存経路で墓地・除外・手札・盤面のどこへでも既に移動可能なため、ボタンとしての追加は
 * 見送った（ユーザー判断）。※デッキ一覧シート（#solMobileDeckSheet）の「手札へ」「盤面へ」は
 * #poolRow のみを列挙しており #poolRow2（EXデッキ）を含まないため、根拠にはならない
 * （2026-09-15: 従来は isCardInPool(wrapper) で操作行全体を hidden にしていた・A-3決定を変更）。
 */
function updateActionSheetForSelection(wrapper) {
  const { name } = getCardNameAndSrc(wrapper);
  const nameEl = document.getElementById('solMobileActionCardName');
  if (nameEl) nameEl.textContent = name || '名称不明';

  const opsEl = document.getElementById('solMobileActionOps');
  const inHand = !!wrapper.parentElement?.classList.contains('center-slot');
  if (opsEl) {
    opsEl.hidden = false;
    const inPool = isCardInPool(wrapper);
    ['defense', 'set', 'return', 'activate', 'bubble'].forEach(action => {
      const btn = opsEl.querySelector(`[data-action="${action}"]`);
      // 手札では守備表示・セットの代わりに「場に出すとき」で形を選ぶ（手札のまま形を変えても意味が無いため）
      if (btn) btn.hidden = inPool || (inHand && (action === 'defense' || action === 'set'));
    });
  }
  _renderPlaceRow(inHand ? wrapper : null);

  setHidden('solMobileActionSheet', false);
}

/**
 * 手札のカードを選んだときの「場に出すとき」（2026-10-20 ユーザー承認の案A。モック _mocks/sim-mobile-ux-20261020）。
 * 形は PC の右クリック「場に出す」と同じ: モンスター＝攻撃／守備／裏側守備、魔法・罠＝表側／セット。
 * 選んでからマスをタップすると、その形で1手として置く（drag-drop.js setMobilePlaceState）。
 * @param {Element|null} wrapper  手札のカード。null なら行を隠す
 */
function _renderPlaceRow(wrapper) {
  const row = document.getElementById('solMobilePlaceRow');
  const seg = document.getElementById('solMobilePlaceSeg');
  const sheet = document.getElementById('solMobileActionSheet');
  setMobilePlaceState(null);
  if (!row || !seg) return;
  row.hidden = !wrapper;
  sheet?.classList.toggle('has-place-row', !!wrapper);
  seg.replaceChildren();
  if (!wrapper) return;
  const choices = isMonsterCard(wrapper)
    ? [['攻撃', '', ''], ['守備', 'defense', ''], ['裏側守備', 'defense', 'down']]
    : [['表側', '', ''], ['セット', '', 'down']];
  choices.forEach(([label, orientation, face], i) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = label;
    btn.setAttribute('aria-pressed', String(i === 0));
    btn.addEventListener('click', () => {
      seg.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b === btn)));
      setMobilePlaceState({ orientation, face });
    });
    seg.appendChild(btn);
  });
}

function initActionSheet() {
  const sheet = document.getElementById('solMobileActionSheet');
  if (!sheet) return;
  // ✕: 選択をやめる（何もない所のタップと同じ。解除の通知でシートも閉じる）
  document.getElementById('solMobileActionCloseBtn')?.addEventListener('click', () => clearMobileSelection());
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
    closeTokenPick({ keepHints: true }); // トークンのマス選びの途中でカードを選んだら、カードの操作を優先する
    updateActionSheetForSelection(ev.detail.wrapper);
  });
  document.addEventListener('sol-mobile-card-deselected', () => {
    setHidden('solMobileActionSheet', true);
    closeCardDetailOverlay(); // A-2: 選択解除にあわせて詳細オーバーレイも閉じる
  });
}

// ══════════════════ トークンを出す（2026-10-23） ══════════════════
// PC は空いたマスの右クリック（token-generator.js）。縦向きスマホは ⋯メニュー「トークンを出す」→
// 名前のシート →「出すマスを選ぶ」→ 空いたマス（置き場所の案内 .drop-hint）をタップ。
// マスを選ぶ間は下部バーの代わりに帯（#solMobileTokenPanel）を出す。

let _tokenPick = null; // { name, slots: Element[] } マスを選んでいる間だけ
let _tokenSheetSlot = null; // 長押しの「名前を変えて出す…」から開いたとき、出すマス（マスを選び直さない）

/** @param {Element|null} [slot] 出すマスが決まっているとき（空いたマスの長押しから） */
function openTokenSheet(slot = null) {
  clearMobileSelection();
  _tokenSheetSlot = slot;
  const input = document.getElementById('solMobileTokenInput');
  if (input) input.value = getLastTokenName();
  const btn = document.getElementById('solMobileTokenPickBtn');
  if (btn) btn.textContent = slot ? '出す' : '出すマスを選ぶ';
  preloadTokenBase();
  setHidden('solMobileTokenSheet', false);
}
function closeTokenSheet() {
  _tokenSheetSlot = null;
  setHidden('solMobileTokenSheet', true);
}

/** 盤面の空いたマスに置き場所の案内を付け、帯を出す */
function startTokenPick(name) {
  closeTokenPick();
  clearMobileSelection(); // 選択中のカードがあると、マスのタップでそのカードが動いてしまう
  const slots = [...document.querySelectorAll('.custom-layout .custom-slot')]
    .filter((s) => !s.querySelector('.tier-item-wrapper'));
  if (!slots.length) { showToast('空いているマスがありません'); return; }
  slots.forEach((s) => s.classList.add('drop-hint'));
  _tokenPick = { name, slots };
  const label = document.getElementById('solMobileTokenPanelLabel');
  if (label) label.textContent = `「${name}」を出すマスをタップ`;
  document.body.classList.add('sol-token-pick');
  setHidden('solMobileTokenPanel', false);
}
/**
 * @param {{keepHints?: boolean}} [opt] keepHints: カードを選んだのでやめるとき。そのカードの置き場所の案内が
 *   同じ .drop-hint で先に付いているので外さない（外れるのは選択をやめたとき・drag-drop.js hideMobileDropHints）
 */
function closeTokenPick({ keepHints = false } = {}) {
  if (!_tokenPick) return;
  if (!keepHints) _tokenPick.slots.forEach((s) => s.classList.remove('drop-hint'));
  _tokenPick = null;
  document.body.classList.remove('sol-token-pick');
  setHidden('solMobileTokenPanel', true);
}

function initTokenSheet() {
  const input = document.getElementById('solMobileTokenInput');
  if (input) input.maxLength = TOKEN_NAME_MAX;
  document.querySelectorAll('[data-sol-token-sheet-close]').forEach((el) => {
    el.addEventListener('click', closeTokenSheet);
  });
  const submit = () => {
    const name = tokenNameOf(input?.value);
    const slot = _tokenSheetSlot;
    input?.blur(); // キーボードを閉じて盤面を見えるように
    closeTokenSheet();
    // 長押ししたマスがまだ空いていればそこへ。埋まっていたら（盤面が作り直された等）マスを選ぶ画面へ
    if (slot?.isConnected && !slot.querySelector('.tier-item-wrapper')) placeToken(slot, name);
    else startTokenPick(name);
  };
  document.getElementById('solMobileTokenPickBtn')?.addEventListener('click', submit);
  input?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) { // 日本語入力の変換確定の Enter では進まない
      e.preventDefault();
      submit();
    }
  });
  document.getElementById('solMobileTokenCancelBtn')?.addEventListener('click', closeTokenPick);
  // 案内の付いたマスのタップで出す（カードの選択とは別。選択中はマス選びをやめているので重ならない）
  document.querySelector('.custom-layout')?.addEventListener('click', (e) => {
    if (!_tokenPick) return;
    const slot = e.target.closest('.custom-slot');
    if (!slot || !_tokenPick.slots.includes(slot)) return;
    e.preventDefault();
    e.stopPropagation();
    const { name } = _tokenPick;
    closeTokenPick();
    placeToken(slot, name);
  }, true);
  initEmptySlotLongPress();
}

// ── 空いたマスの長押し（2026-10-23 案1。モック _mocks/token-longpress-20261023） ──
// カードの長押し（drag-drop.js）と同じく 500ms 動かさずにいたらメニュー。カードの上はカードの長押し、
// カードを選んでいる間（マスのタップ＝そのカードを置く）・マスを選んでいる間・再生モードでは何もしない。
const SLOT_LONG_PRESS_MS = 500;
const SLOT_LONG_PRESS_MOVE_PX = 8;

function initEmptySlotLongPress() {
  const layout = document.querySelector('.custom-layout');
  if (!layout) return;
  let timer = null;
  let start = null; // { x, y, id }
  const cancel = () => {
    clearTimeout(timer);
    timer = null;
    start = null;
  };
  layout.addEventListener('touchstart', (e) => {
    cancel();
    if (!isMobilePortrait() || _playbackMode || _tokenPick || getMobileSelection()) return;
    if (e.touches.length > 1 || e.target.closest('.tier-item-wrapper')) return;
    const slot = e.target.closest('.custom-slot');
    if (!slot || slot.querySelector('.tier-item-wrapper')) return;
    const t = e.changedTouches[0];
    start = { x: t.clientX, y: t.clientY, id: t.identifier };
    timer = setTimeout(() => {
      timer = null;
      const { x, y } = start;
      start = null;
      _openEmptySlotMenu(slot, x, y);
    }, SLOT_LONG_PRESS_MS);
  }, { passive: true });
  layout.addEventListener('touchmove', (e) => {
    if (!start) return;
    const t = [...e.touches].find((tt) => tt.identifier === start.id);
    if (!t || Math.abs(t.clientX - start.x) > SLOT_LONG_PRESS_MOVE_PX || Math.abs(t.clientY - start.y) > SLOT_LONG_PRESS_MOVE_PX) cancel();
  }, { passive: true });
  layout.addEventListener('touchend', cancel, { passive: true });
  layout.addEventListener('touchcancel', cancel, { passive: true });
}

function _openEmptySlotMenu(slot, x, y) {
  // 触れている間の印（アクセント色の実線）。次にどこかへ触れたら外す（メニューの項目を押したときも含む）
  slot.classList.add('sol-slot-pressed');
  setTimeout(() => {
    document.addEventListener('touchstart', () => slot.classList.remove('sol-slot-pressed'), { once: true, capture: true });
  }, 0);
  // メニューを開いた直後の指を離す操作で、メニューが閉じないように（drag-drop.js のカードの長押しと同じ）
  const suppressNextTouchEnd = (e) => {
    e.preventDefault();
    document.removeEventListener('touchend', suppressNextTouchEnd);
  };
  document.addEventListener('touchend', suppressNextTouchEnd, { once: true, passive: false });
  setTimeout(() => document.removeEventListener('touchend', suppressNextTouchEnd), 1000);

  openContextMenuAt([
    { heading: '空いたマス' },
    // 既定の名前で出す（前回の名前ではない。2026-10-23 ユーザー指示）
    { label: 'ここにトークンを出す', action: () => placeToken(slot, DEFAULT_TOKEN_NAME) },
    { label: '名前を変えて出す…', action: () => openTokenSheet(slot) },
    { label: '盤面にメモ', action: () => openCommentSheet({ kind: 'board' }) },
    { label: 'カットイン', action: () => openCommentSheet({ kind: 'cutin' }) },
  ], x, y);
}

// ══════════════════ C-2: 移動の演出（設計書 §C） ══════════════════
// デッキ一覧シート／1ドロー／B-1の移動時に、カードのサムネイル複製を
// 出発点（シートのセル。取れなければ下部バーの「デッキ」ボタン）から行き先ゾーンへ
// 300ms で飛ばす（position:fixed の clone。到着後に自身を消す）。
// 実際の移動は必ず executeDrop を先に完了させてから演出するため、演出の有無で
// リプレイ記録・DOM の最終状態は変わらない（C-1調査の結果、既存のドラッグ操作・
// 1ドロー・リセットには元々このような移動演出は無い＝ゼロから追加）。

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
 * 1ドローのように「移動対象のカードが呼び出し前には分からない」操作向け。
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
  // self-watermark.css の [data-self-wm] .wm-released::after は position:relative の
  // 祖先に依存するが .sol-mobile-deck-cell は既に position:relative なのでそのまま効く。
  if (wrapper.classList.contains('wm-released')) cell.classList.add('wm-released');
  // 未発売SAMPLE画像のイラストぼかし（sample-blur.css）。.sol-mobile-deck-cell は position:relative 済み
  if (wrapper.classList.contains('sample-art')) cell.classList.add('sample-art');
  // ?newcards=1&focus= の強調を引き継ぐ（シートは開くたびに作り直されるため）
  if (wrapper.classList.contains('sol-newcard-focus')) cell.classList.add('sol-newcard-focus');

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
  resetMobileDeckMode(); // 開くたびに「回す」から（編集は deck-edit-ui.js）
  _buildDeckGrid();
  setHidden('solMobileDeckSheet', false);
  // 強調カードが見える位置へ（表示後でないとスクロールできない）
  document.querySelector('#solMobileDeckGrid .sol-newcard-focus')?.scrollIntoView({ block: 'center' });
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

  // 副ボタン: 1ドロー・リセット（既存ボタンをclickするだけ。ロジックを複製しない）。
  // C-2: 1ドローは移動先（手札 .center-slot）の実行前 wrapper id 集合との差分で新規カードを検出して演出する。
  document.getElementById('solMobileDeckDrawBtn')?.addEventListener('click', () => {
    const handSlot = document.querySelector('.sol-hand-area .center-slot');
    const beforeIds = new Set(Array.from(handSlot?.querySelectorAll('.tier-item-wrapper') || []).map(w => w.id));
    document.getElementById('randomButton')?.click();
    _flyNewlyAddedCards(handSlot, beforeIds);
    _buildDeckGrid();
  });
  // リセットは全カードをデッキへ戻すだけ（手札へ移るカードが無いので移動演出は不要）。
  document.getElementById('solMobileDeckResetBtn')?.addEventListener('click', () => {
    document.getElementById('resetButton')?.click();
    _buildDeckGrid();
  });

  // 「編集」でカードを足した・抜いたら、「回す」の一覧も作り直す（戻ったときに古い一覧を見せない）
  document.addEventListener('sol-deck-edited', () => {
    if (!document.getElementById('solMobileDeckSheet')?.hidden) _buildDeckGrid();
  });

  // デッキ0枚時: 既存のデッキ読込シートを開く
  document.getElementById('solMobileDeckEmptyLoadBtn')?.addEventListener('click', () => {
    closeDeckSheet();
    openSidebarSheet();
  });
}

// ══════════════════ タスクA: 録画モード（設計書 §A） ══════════════════
// 「録画開始」でこの時点までの手数(startIdx)を、「停止」で終了時点(endIdx)を記録する。
// 共有・端末保存・X投稿は、録画（停止済み）があればその範囲だけを送る
// （既存の setup:true 方式・generateShareURL/buildRangeLogsForPayload の range 引数を使う）。

let _recording = { recording: false, startIdx: null, endIdx: null };

/** 録画が開始され、かつ停止済み（範囲が確定している）かどうか */
function _hasStoppedRecording() {
  return !_recording.recording && _recording.startIdx !== null && _recording.endIdx !== null;
}

/**
 * @returns {{start:number, end:number}|null} 1-indexed 手数の範囲。録画（停止済み）が無ければ null（＝全範囲扱い）。
 * M-2: 停止後の取消（undo）で手数が減っていても壊れないよう、表示・送信の直前で
 * endIdx を現在の手数以下へクランプする（_recording 自体は書き換えない。状態の掃除は _pruneRange）。
 * 範囲の手がすべて消えて空になった場合は null（PC版 _getPcRecordingRange と同じ）。
 */
function _getRecordingRange() {
  if (!_hasStoppedRecording()) return null;
  const total = getLogLength();
  const end = Math.min(_recording.endIdx, total);
  if (_recording.startIdx >= end) return null; // 空の範囲（取消で手が消えた）は全範囲扱い。「範囲 3〜2」の逆順表示を出さない
  return { start: _recording.startIdx + 1, end };
}

/**
 * 決まった範囲が、取消・巻き戻しからの新しい操作で古くなっていないかを確かめて直す
 * （PC版 pc-recording.js の _prunePcRange と同じ）。開始位置が手数以上なら状態ごと捨て、
 * 終了位置が手数を超えていれば手数まで下げる。手数が変わるたび（initRecording の監視）に呼ぶ。
 */
function _pruneRange() {
  if (!_hasStoppedRecording()) return;
  const total = getLogLength();
  if (_recording.startIdx >= total) {
    _resetRecording();
    _updateShareRecordingSection();
  } else if (_recording.endIdx > total) {
    _recording.endIdx = total;
    _updateRecordBtn();
    _updateShareRecordingSection();
  }
}

/** 録画状態の写しを返す（ページ遷移をまたぐ退避用） */
export function getMobileRecordingState() {
  return { ..._recording };
}

/** 退避した録画状態を戻し、ボタン表示を更新する */
export function setMobileRecordingState(state) {
  if (!state) return;
  // 2026-10-20 から範囲は「範囲を選ぶ画面」で一度に決めるので、選んでいる途中（recording）の状態は戻さない
  _recording = state.recording
    ? { recording: false, startIdx: null, endIdx: null }
    : { recording: false, startIdx: state.startIdx ?? null, endIdx: state.endIdx ?? null };
  _updateRecordBtn();
  _updateShareRecordingSection();
}

/**
 * 画面録画用の表示（rec-view.js）から呼ぶ: 縦向きスマホの再生モードに入る（既に入っていれば何もしない）。
 * 入口は「⋯メニュー」と同じ扱い（終了しても共有シートへは戻らない）。
 */
export function enterPlaybackModeForRecView() {
  enterPlaybackMode('menu');
}

/** 再生モード中かどうか（ページ遷移をまたぐ退避用） */
export function isMobilePlaybackMode() {
  return _playbackMode;
}

/**
 * 復元後、退避時にスマホの再生モード中だった（または縦向きスマホで途中の手を復元した）なら再生モードへ入り直す。
 * 縦向きスマホで、記録があり、カーソルが末尾より前（途中の手）のときだけ入る。
 * 範囲なしで入るので seekTo は呼ばれず、カーソルは動かない。
 * 仕様: 範囲再生（_playbackRange）と入口（_playbackOrigin。'share' 等）は退避しない。
 *   復元後は「範囲なし・入口なし」の通常の再生モードになる（終了しても共有シートへは戻らない）。
 * 縦向きスマホでないため入り直せない場合は、回転時の exitPlaybackMode と同じ H-4 規則で
 * 記録の末尾まで進める（途中の手のまま通常画面に残さない）。
 * @param {boolean} wasPlayback 退避時に再生モード中だったか
 * @returns {boolean} 入り直したか
 */
export function resumePlaybackModeIfNeeded(wasPlayback) {
  if (getLogLength() + getSetupLogs().length === 0) return false;
  if (getCursor() >= getLogLength() - 1) return false;
  // PC幅で途中の手を見たまま退避し、縦向きスマホで復元した場合（wasPlayback=false）も、
  // 縦向きの通常画面は常に末尾という前提（H-4）を守るため、カーソルを保ったまま再生モードに入る
  if (!wasPlayback && !isMobilePortrait()) return false;
  if (!isMobilePortrait()) {
    seekTo(getLogLength() - 1); // H-4: 入り直せないときは末尾へ
    showToast('最後の手まで進めました'); // 回転時（exitPlaybackMode）と同じ文言
    return false;
  }
  enterPlaybackMode(null, null);
  return _playbackMode;
}

/**
 * 録画状態を破棄する。「すべて消去」／デッキ再読込／保存済みリプレイを開く／
 * 共有リンクから開く の各経路（それぞれの呼び出し箇所）から呼ぶ。
 */
function _resetRecording() {
  _recording = { recording: false, startIdx: null, endIdx: null };
  _updateRecordBtn();
}


/**
 * 録画停止: 停止時点までを録画範囲として確定し、共有シートを開く（A-3）。
 * M-4: startIdx と同じ基準（getCursor()+1）で終端を確定する。
 * M-1: 1手も録画されていない（endIdx <= startIdx）場合は不成立として状態を破棄し、
 * トーストのみ表示して共有シートは開かない（M-3: 共有シートの「ここで停止して範囲を確定」
 * ボタン経由で呼ばれた場合は、シートが既に開いたままなので①の表示を全記録表示へ戻す）。
 */
function _stopRecording() {
  if (!_recording.recording) return;
  const endIdx = getCursor() + 1;
  if (endIdx <= _recording.startIdx) {
    _resetRecording();
    showToast('範囲に手が1つも入っていません');
    _updateShareRecordingSection();
    return;
  }
  _recording.endIdx = endIdx;
  _recording.recording = false;
  _updateRecordBtn();
  _updateShareRecordingSection(); // シートを開いたまま「ここまでで範囲を決める」を押した場合の表示更新
  // 整理③: 保存か共有かは利用者が選ぶので、シートは自動で開かず通知だけ出す
  const range = _getRecordingRange();
  if (range) showToast(`範囲を決めました（${range.start}〜${range.end}手目）。保存・共有で使えます`);
}

/**
 * 下部バーの「範囲」ボタン。範囲を選ぶ画面を開く（範囲が決まっていれば選び直し）。
 * 2026-10-20 ユーザー承認の案A: 以前は「押した後に動かした手」を記録する方式で、説明も無く、
 * 回し終えた手から選べなかった（スマホの通常画面は常に最後の手のため）。
 * 選んでいる途中（recording）の状態はもう作らない（setMobileRecordingState も戻さない）ので、
 * _stopRecording・共有シートの「ここまでで範囲を決める」は届かない古い経路として残っている。
 */
function toggleRecording() {
  if (_recording.recording) _stopRecording();
  else openRangePicker();
}

// ══════════════════ 範囲を選ぶ画面（2026-10-20 案A） ══════════════════
// 再生モードに入り、手を動かして「ここから」「ここまで」で選ぶ。決めたら _recording（startIdx＝範囲より前の手数、
// endIdx＝範囲の最後の手）へ入れる。保存・共有・書き出しは今までどおり _getRecordingRange を使う。
// 「ここから」「ここまで」は、今見ている手（getCursor()+1 手目）をその端に含める（PC の範囲選択と同じ数え方）。

/** @type {{s:number|null, e:number|null, fromSheet:boolean}|null} 選んでいる途中の範囲（1-indexed の手） */
let _rangePick = null;

function _rangeNow() {
  return getCursor() + 1; // 今見ている手（0 は何も進めていない最初の盤面）
}

/** 範囲を選ぶ画面を開く */
function openRangePicker({ fromSheet = false } = {}) {
  if (getLogLength() === 0) { showToast('記録がありません'); return; }
  const current = _getRecordingRange();
  enterPlaybackMode('menu');
  if (!_playbackMode) return;
  _stopAutoPlayIfNeeded();
  _rangePick = { s: current?.start ?? null, e: current?.end ?? null, fromSheet };
  seekTo((current?.start ?? 1) - 1); // 今の範囲の最初の手（無ければ1手目）を見せる
  document.body.classList.add('sol-range-pick');
  setHidden('solMobileRangePanel', false);
  _fitRangePanel();
  _renderRangePanel();
}

/** パネルの高さだけ盤面の下を空けて、盤面を収め直す（再生モードの帯・バーの代わり） */
let _rangePanelH = 0;
function _fitRangePanel() {
  const panel = document.getElementById('solMobileRangePanel');
  if (!panel) return;
  _rangePanelH = Math.ceil(panel.offsetHeight);
  document.documentElement.style.setProperty('--sol-mobile-bottom-reserve', `${_rangePanelH}px`);
  window.dispatchEvent(new Event('resize'));
}

/**
 * 範囲を選ぶ画面を閉じる。
 * @param {{confirm?: boolean, silent?: boolean}} opts  confirm: 範囲を決めて保存・共有シートを開く／
 *   silent: 画面の回転などで閉じるときはシートを開かない
 */
function closeRangePicker({ confirm = false, silent = false } = {}) {
  if (!_rangePick) return;
  const pick = _rangePick;
  _rangePick = null;
  if (confirm && pick.s && pick.e) {
    _recording = { recording: false, startIdx: pick.s - 1, endIdx: pick.e };
    _updateRecordBtn();
  }
  document.body.classList.remove('sol-range-pick');
  setHidden('solMobileRangePanel', true);
  if (_playbackMode) {
    // 通常の画面は常に最後の手（H-4）。先に最後へ進めて「最後の手まで進めました」の通知を出さない
    _stopAutoPlayIfNeeded();
    seekTo(getLogLength() - 1);
    exitPlaybackMode({ returnToOrigin: false });
  }
  if (silent) return;
  if (confirm || pick.fromSheet) openShareSheet();
}

function _renderRangePanel() {
  if (!_rangePick) return;
  const { s, e } = _rangePick;
  const total = getLogLength();
  const now = _rangeNow();
  const label = document.getElementById('solMobileRangeLabel');
  if (label) {
    label.textContent = s && e ? `範囲 ${s}〜${e}手目（${e - s + 1}手）`
      : s ? `${s}手目から。「ここまで」で終わりを決めます`
        : '手を動かして「ここから」を押します';
  }
  const nowEl = document.getElementById('solMobileRangeNow');
  if (nowEl) nowEl.textContent = `今 ${now} / ${total}手`;
  // 文言が折り返してパネルの高さが変わったら、盤面の下の空きも合わせ直す（reviewer 指摘）
  const panel = document.getElementById('solMobileRangePanel');
  if (panel && Math.ceil(panel.offsetHeight) !== _rangePanelH) _fitRangePanel();
  const slider = document.getElementById('solMobileRangeSlider');
  if (slider) {
    slider.min = '0';
    slider.max = String(total);
    slider.value = String(now);
  }
  // 金色の帯: 記録全体（0〜total）のうち、選んだ範囲（s-1〜e。s手目の手前から e手目まで）
  const fill = document.getElementById('solMobileRangeBandFill');
  if (fill) {
    const a = s ? s - 1 : null;
    const b = e ?? (s ? Math.max(now, s) : null);
    const pct = (v) => (total > 0 ? (v / total) * 100 : 0);
    fill.style.left = a === null ? '0' : `${pct(a)}%`;
    fill.style.width = a === null ? '0' : `${Math.max(0, pct(b) - pct(a))}%`;
  }
  const fromBtn = document.getElementById('solMobileRangeFromBtn');
  const toBtn = document.getElementById('solMobileRangeToBtn');
  const okBtn = document.getElementById('solMobileRangeOkBtn');
  if (fromBtn) fromBtn.disabled = now === 0;
  if (toBtn) toBtn.disabled = now === 0;
  if (okBtn) okBtn.disabled = !(s && e);
  const prev = document.getElementById('solMobileRangePrevBtn');
  const next = document.getElementById('solMobileRangeNextBtn');
  if (prev) prev.disabled = now === 0;
  if (next) next.disabled = now >= total;
}

function initRangePicker() {
  const on = (id, fn) => document.getElementById(id)?.addEventListener('click', fn);
  on('solMobileRangePrevBtn', () => { if (_rangePick) seekTo(getCursor() - 1); });
  on('solMobileRangeNextBtn', () => { if (_rangePick) document.getElementById('replayFwd')?.click(); });
  on('solMobileRangeFromBtn', () => {
    if (!_rangePick || _rangeNow() === 0) return;
    _rangePick.s = _rangeNow();
    if (_rangePick.e && _rangePick.e < _rangePick.s) _rangePick.e = null;
    _renderRangePanel();
  });
  on('solMobileRangeToBtn', () => {
    if (!_rangePick || _rangeNow() === 0) return;
    const now = _rangeNow();
    if (!_rangePick.s) _rangePick.s = 1; // 「ここから」を押していなければ1手目から
    if (now < _rangePick.s) { _rangePick.e = _rangePick.s; _rangePick.s = now; } // 前へ戻って押したら入れ替える
    else _rangePick.e = now;
    _renderRangePanel();
  });
  on('solMobileRangeCancelBtn', () => closeRangePicker());
  on('solMobileRangeOkBtn', () => closeRangePicker({ confirm: true }));
  document.getElementById('solMobileRangeSlider')?.addEventListener('input', (e) => {
    if (_rangePick) seekTo(parseInt(e.target.value, 10) - 1);
  });
  subscribeReplay('changed', _renderRangePanel);
}

/**
 * 録画ボタンの見た目・手数バッジを更新する。
 * A-1/M-4: 録画中に取消（undo）や巻き戻し（前ボタン・スライダー）で現在位置が startIdx より
 * 前に戻ったら startIdx をその値にクランプする。基準は全ログ長（getLogLength）ではなく
 * 現在の再生位置（getCursor()+1）を使う（巻き戻し中は logEvent の切り捨てが未発生でも
 * ログ長は変わらないため、ログ長基準だと巻き戻し直後の表示が実態とずれる）。
 * L-10: ラベルは「■ N手」に短縮（375px幅で折り返さないよう。録画中は赤いドット＋枠で分かる）。
 */
function _updateRecordBtn() {
  const btn = document.getElementById('solMobileRecordBtn');
  const label = document.getElementById('solMobileRecordLabel');
  if (!btn || !label) return;
  // 整理③: 呼び名を「範囲」に（旧「録画」）。選んでいる間「■ N手」、決まった後「N手」、待機「範囲」。
  // 範囲の線アイコン（2026-10-21・旧 ✂）は文字の外にあり、選んでいる間だけ隠す（.recording）。
  // 375px幅で折り返さない短さにしている（L-10）。
  const range = _recording.recording ? null : _getRecordingRange();
  if (_recording.recording) {
    const cur = getCursor() + 1; // M-4: 現在の再生位置基準
    if (cur < _recording.startIdx) _recording.startIdx = cur; // A-1/M-4: クランプ
    const n = Math.max(0, cur - _recording.startIdx);
    label.textContent = `■ ${n}手`;
  } else if (range) {
    // 手数だけを出す（「範囲 3〜12」は375px幅の下部バーではみ出すため。何手目から何手目かはシートの中で伝える）
    // 下部バーが6つ並ぶので短く（2026-10-16）。範囲であることは accent の面で分かる
    label.textContent = `${range.end - range.start + 1}手`;
  } else {
    label.textContent = '範囲';
  }
  btn.classList.toggle('recording', _recording.recording);
  btn.classList.toggle('has-range', !!range);
}

/**
 * A-2: #replayCounter のテキスト変化（stepForward/stepBack/seekTo/undo 等、既存の手数更新経路）を
 * 監視し、録画中バッジの N手・クランプを追従させる。
 */
function initRecording() {
  // コメントを差し込んだ（手数が1増える）ときは、範囲の開始・終了をずらす（PC版 pc-recording.js と同じ）
  subscribeReplay('commentAdded', (insertedIdx) => {
    _recording = shiftRangeAfterInsert(_recording, insertedIdx);
    _updateRecordBtn();
    _updateShareRecordingSection();
    _updateShareCurrentInfo();
  });
  // コメントを取り除いた（PC版で削除した）ときは、範囲の開始・終了をずらす。スマホには削除の操作は無いが、
  // 状態は PC 版と同じく記録に合わせて保つ
  subscribeReplay('removed', (removedIdx) => {
    _recording = shiftRangeAfterRemove(_recording, removedIdx);
    _updateRecordBtn();
    _updateShareRecordingSection();
    _updateShareCurrentInfo();
  });

  const counterEl = document.getElementById('replayCounter');
  if (counterEl) {
    new MutationObserver(() => {
      _pruneRange();
      if (_recording.recording || _hasStoppedRecording()) _updateRecordBtn();
    }).observe(counterEl, { childList: true, characterData: true, subtree: true });
  }
}

// ══════════════════ コメント（カードの吹き出し・盤面のメモ・カットイン。整理④） ══════════════════
// 入力は下から出る小さなシート（#solMobileCommentSheet）。記録への追加は replay-service の addComment
// （自動再生の停止・末尾への移動・本文の切り詰めを含む）に任せ、範囲の終わりの追従は
// 'commentAdded' の購読（initRecording）が行う。コメント一覧・削除はスマホには作らない
// （消したいときは「1手戻す」）。

/** @type {{kind:'card', cardId:string, name:string}|{kind:'board'}|{kind:'cutin'}|null} */
let _commentTarget = null;

/** カットインのシートの「相手の妨害」の部品（buildDisruptionPicker の戻り値） */
let _mobileDisruptionPicker = null;
/** カットインのシートの切り替え（言葉／相手の妨害）。シートを閉じても次に開いたときはこのまま */
let _mobileCutinMode = 'words';

/**
 * カットインのシートの「言葉／相手の妨害」を出し分ける。言葉＝言葉の札と自由入力、相手の妨害＝妨害の札と検索。
 * カットイン以外（吹き出し・盤面メモ）のシートでは、切り替えも妨害の部品も出さず、入力欄だけを出す。
 */
function _applyMobileCutinMode(isCutin) {
  const mode = isCutin ? _mobileCutinMode : 'words';
  document.querySelectorAll('#solMobileCutinSeg [data-cutin-mode]').forEach((btn) => {
    btn.setAttribute('aria-pressed', String(btn.dataset.cutinMode === mode));
  });
  setHidden('solMobileCutinSeg', !isCutin);
  setHidden('solMobileCutinChips', !isCutin || mode !== 'words'); // 言葉の札はカットインの「言葉」のときだけ
  setHidden('solMobileCutinDisruption', !isCutin || mode !== 'disruption');
  setHidden('solMobileCommentInputRow', isCutin && mode === 'disruption');
  if (isCutin && mode === 'disruption') _mobileDisruptionPicker?.prefetch(); // よく使う札の画像を先に読み込む
  const targetEl = document.getElementById('solMobileCommentTarget');
  if (targetEl && isCutin) {
    targetEl.textContent = mode === 'disruption'
      ? 'カードを選ぶと、その手に進んだとき盤面に一瞬出ます（残りません）'
      : 'その手に進んだとき、盤面に一瞬出ます（残りません）';
  }
}

function openCommentSheet(target) {
  _commentTarget = target;
  const isCard = target.kind === 'card';
  const isCutin = target.kind === 'cutin';
  const titleEl = document.getElementById('solMobileCommentSheetTitle');
  if (titleEl) titleEl.textContent = isCard ? '吹き出しを付ける' : (isCutin ? 'カットイン' : '盤面にメモ');
  const targetEl = document.getElementById('solMobileCommentTarget');
  if (targetEl) {
    targetEl.textContent = isCard
      ? `対象: ${target.name || '名称不明'}`
      : (isCutin ? 'その手に進んだとき、盤面に一瞬出ます（残りません）' : '盤面の空いた角に付箋で表示されます。次のメモが付くまで残ります');
  }
  _mobileDisruptionPicker?.reset();
  _applyMobileCutinMode(isCutin);
  const input = document.getElementById('solMobileCommentInput');
  if (input) {
    input.value = '';
    input.maxLength = isCutin ? CUTIN_MAX_LEN : COMMENT_MAX_LEN;
    input.placeholder = isCutin ? '自由に書く（20字まで）' : 'コメント';
  }
  const addBtn = document.getElementById('solMobileCommentAddBtn');
  if (addBtn) addBtn.textContent = isCutin ? '出す' : '付ける';
  setHidden('solMobileCommentSheet', false);
  // タップの中で同期的に呼ぶ（iOS はこの場でしかキーボードを出せない）。相手の妨害は札を押す想定なのでキーボードは出さない
  if (!(isCutin && _mobileCutinMode === 'disruption')) input?.focus();
}

function closeCommentSheet() {
  _commentTarget = null;
  _mobileDisruptionPicker?.reset();
  setHidden('solMobileCommentSheet', true);
}

/** カードを選んで「吹き出し」を押したとき。デッキ内のカードには付けない */
function openCardCommentSheet(wrapper) {
  if (!wrapper || isCardInPool(wrapper)) return;
  const cardEl = wrapper.querySelector('.tier-item');
  if (!cardEl?.id) return;
  openCommentSheet({ kind: 'card', cardId: cardEl.id, name: getCardNameAndSrc(wrapper).name });
}

function _submitMobileComment() {
  const input = document.getElementById('solMobileCommentInput');
  const target = _commentTarget;
  if (!input || !target) return;
  const ok = target.kind === 'card'
    ? addComment({ kind: 'card', cardId: target.cardId, text: input.value })
    : addComment({ kind: target.kind === 'cutin' ? 'cutin' : 'board', text: input.value });
  if (!ok) return; // 空欄は何もしない（シートは開いたまま）
  closeCommentSheet();
  // カットインは付けた直後にその場で出る（動きが見えるので通知は出さない）
  if (target.kind !== 'cutin') showToast(target.kind === 'card' ? '吹き出しを付けました' : 'メモを付けました');
}

/** カットインの言葉の札（押すとその場で入る） */
function _addCutinFromChip(label) {
  if (_commentTarget?.kind !== 'cutin') return;
  if (!addComment({ kind: 'cutin', text: label })) return;
  closeCommentSheet();
}

/** カットインの相手の妨害（札・検索で選ぶとその場で入る） */
function _addDisruptionCutin(name) {
  if (_commentTarget?.kind !== 'cutin') return;
  if (!addComment({ kind: 'cutin', card: name })) return;
  closeCommentSheet();
}

function initCommentSheet() {
  const input = document.getElementById('solMobileCommentInput');
  if (input) input.maxLength = COMMENT_MAX_LEN;
  document.querySelectorAll('[data-sol-comment-sheet-close]').forEach((el) => {
    el.addEventListener('click', closeCommentSheet);
  });
  document.getElementById('solMobileCommentAddBtn')?.addEventListener('click', _submitMobileComment);
  const chips = document.getElementById('solMobileCutinChips');
  if (chips) {
    for (const label of CUTIN_PRESETS) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'sol-cutin-chip';
      chip.textContent = label;
      chip.addEventListener('click', () => _addCutinFromChip(label));
      chips.appendChild(chip);
    }
  }
  document.querySelectorAll('#solMobileCutinSeg [data-cutin-mode]').forEach((btn) => {
    btn.addEventListener('click', () => {
      _mobileCutinMode = btn.dataset.cutinMode === 'disruption' ? 'disruption' : 'words';
      _applyMobileCutinMode(true);
      // 相手の妨害は札を押すのでキーボードを閉じる。言葉に戻したら自由入力へ（タップの中で同期的に。iOS の制約）
      const input = document.getElementById('solMobileCommentInput');
      if (_mobileCutinMode === 'disruption') input?.blur();
      else input?.focus();
    });
  });
  const disruption = document.getElementById('solMobileCutinDisruption');
  if (disruption) _mobileDisruptionPicker = buildDisruptionPicker(disruption, _addDisruptionCutin);
  input?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) { // 日本語入力の変換確定の Enter では付けない
      e.preventDefault();
      _submitMobileComment();
    }
  });
  // 盤面メモの付箋を押したら、全文を下から出るシートに出す（textContent。本文は他人が作れる共有リプレイ由来のため）
  document.querySelectorAll('[data-sol-memo-sheet-close]').forEach((el) => {
    el.addEventListener('click', () => setHidden('solMobileMemoSheet', true));
  });
  setMemoFullOpener((text) => {
    const p = document.getElementById('solMobileMemoFullText');
    if (p) p.textContent = text;
    setHidden('solMobileMemoSheet', false);
  });
  // 縦向きスマホでは、右クリック/長押しメニューの「吹き出しを付ける」もこのシートで入力させる
  setCardCommentOpener((wrapper) => {
    if (!isMobilePortrait()) return false;
    openCardCommentSheet(wrapper);
    return true;
  });
}

// ══════════════════ 共有シート（フェーズ3・設計書 §3.4／H-1で2段階構成に変更／
//                     2026-09-04 タスクA-3: ①を「共有する範囲」スライダーから録画状態表示へ） ══════════════════
// (1) 「共有リンクを作成」→ replay-ui.js の generateShareURL() を直接呼ぶ（.click() 配線を
//     やめた。iOS Safari は await の後に user activation が失われ、.click() 経由の
//     clipboard/window.open が沈黙失敗するため）。結果は readonly input に表示し、
//     「共有…」（navigator.share）／「コピー」／「Xに投稿」（本物の<a>）をタップイベント
//     ハンドラの中で同期的に扱う。
// (2) コメントは共有シートから外した（整理④）。カードの吹き出し・盤面メモは下の「コメント」節のシートで付ける。
//     「ファイルに書き出す」は範囲を渡すため replay-ui.js の exportReplayFile を直接呼ぶ。
// タイトル入力欄: [data-replay-title]（PC・スマホのシートに1つずつ。入力は互いに写される）。
//     replay-ui.js が、共有では「共有リプレイ」、保存・書き出しでは日時を既定値にする。

/** シートの用途（'save'＝保存 / 'share'＝共有）。再生して確認から戻るときは前回の用途で開き直す */
let _shareSheetMode = 'share';

/**
 * 保存・共有シートを開く（整理③で1つのシートを用途で切り替える形にした）。
 * @param {'save'|'share'} [mode] 省略時は前回の用途
 */
function openShareSheet(mode) {
  if (mode) _shareSheetMode = mode;
  const titleEl = document.getElementById('solMobileShareSheetTitle');
  if (titleEl) titleEl.textContent = _shareSheetMode === 'save' ? '保存' : '共有';
  document.querySelectorAll('#solMobileShareSheet [data-share-mode]').forEach((el) => {
    el.hidden = el.dataset.shareMode !== _shareSheetMode;
  });
  // タイトル欄の説明は実際の動きに合わせる（保存は日時が既定、共有は「共有リプレイ」。公開はタイトル必須）
  const titleNote = document.getElementById('solMobileShareTitleNote');
  if (titleNote) {
    titleNote.textContent = _shareSheetMode === 'save'
      ? '空欄なら日時が入ります'
      : '空欄なら「共有リプレイ」になります（公開するときはタイトルが必要です）';
  }
  // 開くたびに公開はオフに戻す（前回のチェックが残って意図せず公開しないように）。共有URLの使い回しも止める
  const publicCheck = document.getElementById('solMobileSharePublic');
  if (publicCheck) publicCheck.checked = false;
  resetShareUrlMemo();
  // 前回の結果・エラー表示を残さない
  setHidden('solMobileShareResult', true);
  setHidden('solMobileShareError', true);
  const urlInput = document.getElementById('solMobileShareUrlInput');
  if (urlInput) urlInput.value = '';
  const xLink = document.getElementById('solMobileShareXLink');
  if (xLink) xLink.href = '#';
  _updateShareRecordingSection(); // タスクA-3: ①録画した範囲 / 全記録の表示を更新
  _updateShareCurrentInfo(); // タスクA: 「現在の記録: N手（メインM枚・EX E枚）」
  _updateShareOpenSavedButton(); // 保存済みが1件以上あれば③に「リプレイ一覧を開く」を出す
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

/**
 * 範囲セクション（旧①録画）の表示を更新する。3状態:
 *   - 選んでいる途中: 「範囲を選んでいます（N手）」＋「ここまでで範囲を決める」
 *   - 範囲あり: 「s〜e手目（N手）が対象です」＋「範囲を選び直す」
 *   - 範囲なし: 「全部の手（N手）が対象です。…」
 */
function _updateShareRecordingSection() {
  const infoEl = document.getElementById('solMobileShareRecordingInfo');
  const stopBtn = document.getElementById('solMobileShareStopRecordingBtn');
  const redoBtn = document.getElementById('solMobileShareRedoRecordingBtn');
  if (!infoEl) return;

  if (_recording.recording) {
    // M-3: 範囲を選んでいる途中でシートを開いた場合（⋯メニュー「共有」経由等）
    const n = Math.max(0, getCursor() + 1 - _recording.startIdx);
    infoEl.textContent = `範囲を選んでいます（${n}手）`;
    if (stopBtn) stopBtn.hidden = false;
    if (redoBtn) redoBtn.hidden = true;
    return;
  }

  const range = _getRecordingRange();
  if (range) {
    const n = range.end - range.start + 1;
    infoEl.textContent = `${range.start}〜${range.end}手目（${n}手）が対象です`;
    if (stopBtn) stopBtn.hidden = true;
    if (redoBtn) { redoBtn.hidden = false; redoBtn.textContent = '範囲を選び直す'; }
  } else {
    const total = getLogLength();
    infoEl.textContent = `全部の手（${total}手）が対象です。`;
    if (stopBtn) stopBtn.hidden = true;
    if (redoBtn) { redoBtn.hidden = total === 0; redoBtn.textContent = '範囲を選ぶ'; }
  }
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

  // 「みんなのリプレイに公開する」: タイトル必須。空ならリンクを作らずその場でエラーを出す
  const isPublic = !!document.getElementById('solMobileSharePublic')?.checked;
  if (isPublic) {
    const titleErr = checkPublicTitle();
    if (titleErr) { _showShareError(titleErr); return; }
  }

  setHidden('solMobileShareError', true);
  btn.disabled = true;
  try {
    const url = await generateShareURL(_getRecordingRange(), { public: isPublic }); // タスクA-4: 録画があればその範囲を渡す
    if (urlInput) urlInput.value = url;
    if (xLink) xLink.href = buildShareTweetUrl(url);
    if (nativeBtn) nativeBtn.hidden = typeof navigator.share !== 'function';
    if (resultEl) resultEl.hidden = false;
    if (isPublic) showToast('みんなのリプレイに公開しました');
  } catch (e) {
    // 公開の失敗は理由（禁止語など）を出す。通常の共有は従来どおりの文言
    _showShareError(isPublic ? e.message : 'リンクを作成できませんでした');
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
  const title = document.getElementById('solMobileShareTitleInput')?.value?.trim() || '共有リプレイ';
  navigator.share({ title, url }).catch(() => { /* ユーザーキャンセル等は無視 */ });
}

function initShareSheet() {
  const sheet = document.getElementById('solMobileShareSheet');
  if (!sheet) return;

  sheet.querySelectorAll('[data-sol-share-sheet-close]').forEach(el => {
    el.addEventListener('click', closeShareSheet);
  });

  // M-3: 録画中に共有シートを開いた場合の「ここで停止して範囲を確定」（停止処理＝表示更新）
  document.getElementById('solMobileShareStopRecordingBtn')?.addEventListener('click', () => {
    _stopRecording();
  });

  // 範囲セクション（「範囲を選ぶ／選び直す」／「再生して確認」）
  // 2026-10-20 から範囲を選ぶ画面を開く（以前は開始位置まで巻き戻し、その後の手を消してから選び直していた）。
  // やめた・決めたあとは共有シートへ戻る。
  document.getElementById('solMobileShareRedoRecordingBtn')?.addEventListener('click', () => {
    closeShareSheet();
    openRangePicker({ fromSheet: true });
  });
  document.getElementById('solMobileSharePlaybackBtn')?.addEventListener('click', () => {
    closeShareSheet();
    // 第7次検収: 停止済み録画があれば「受け手が見るものと同じ」その範囲だけを再生する。
    // 録画が無ければ従来どおり全体を再生する（range=null）。
    const range = _getRecordingRange();
    enterPlaybackMode('share', range); // 第6次検収-1: origin を覚え、終了時に共有シートへ戻れるようにする
  });

  // (1) リンク作成〜共有・コピー・X投稿
  document.getElementById('solMobileShareGenerateBtn')?.addEventListener('click', () => { _generateShareLink(); });
  document.getElementById('solMobileShareCopyResultBtn')?.addEventListener('click', _copyShareUrl);
  document.getElementById('solMobileShareNativeBtn')?.addEventListener('click', _nativeShareUrl);
  // #solMobileShareXLink は本物の <a href> なのでリスナー登録は不要（href は _generateShareLink が設定）

  // (2) 従来どおり .click() 配線
  // ファイルへの書き出し: 範囲が決まっていればその範囲だけ（replay-ui.js の exportReplayFile）
  document.getElementById('solMobileShareExportBtn')?.addEventListener('click', () => {
    exportReplayFile(_getRecordingRange());
  });

  // E-2: このブラウザに保存
  document.getElementById('solMobileShareSaveLocalBtn')?.addEventListener('click', _saveReplayToDevice);
  // 整理③: 下部バーの「保存」は、同じシートを保存の用途で開く
  document.getElementById('solMobileSaveBtn')?.addEventListener('click', () => { openShareSheet('save'); });
}

// ══════════════════ E-1/E-2: リプレイの端末保存（設計書 §E） ══════════════════
// 保存処理本体（スマホ・PC共通）は replay-ui.js の saveReplayToDevice。
// 保存したリプレイは /solitaire/replays（リプレイ一覧ページ）から開く。

/** タスクA: 共有シート冒頭の「現在の記録: N手（メインM枚・EX E枚）」を更新する */
function _updateShareCurrentInfo() {
  const el = document.getElementById('solMobileShareCurrentInfo');
  if (!el) return;
  const { total, main, ex } = getReplayCounts();
  el.textContent = `現在の記録: ${total}手（メイン${main}枚・EX${ex}枚）`;
}

/** ③に「リプレイ一覧を開く（n件）」リンクを出す（件数は端末保存＋共有履歴の合計。0件なら隠す） */
function _updateShareOpenSavedButton() {
  const btn = document.getElementById('solMobileShareOpenSavedBtn');
  if (!btn) return;
  const n = savedReplaysGet().length + sharedReplaysGet().length;
  if (n === 0) {
    btn.setAttribute('hidden', '');
    return;
  }
  btn.textContent = `リプレイ一覧を開く（${n}件）`;
  btn.removeAttribute('hidden');
}

/**
 * E-2/タスクA-4: 共有シートの「端末に保存」。録画（停止済み）があればその範囲だけを、
 * 無ければリプレイ全体を保存する（実処理は replay-ui.js の saveReplayToDevice）。
 */
function _saveReplayToDevice() {
  const title = document.getElementById('solMobileShareTitleInput')?.value;
  const result = saveReplayToDevice({ title, range: _getRecordingRange() });

  showToast(describeSaveResult(result));
  _updateShareOpenSavedButton(); // 保存直後に件数を反映
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
    _resetRecording(); // タスクA-1: 「すべて消去」で録画状態を破棄する
  });

  // 「リセット」(event-handlers.js handleReset): 記録も初期化されるので録画状態を破棄し、
  // 再生バー（取消ボタン）・デッキ枚数バッジ・開いたままの共有シートの録画表示を追従させる。
  document.addEventListener('sol-replay-reset', () => {
    // 通常は再生モード中に⋯メニュー/デッキシートへ届かないが、念のため抜けて整合を取る
    closeRangePicker({ silent: true });
    if (_playbackMode) exitPlaybackMode({ returnToOrigin: false });
    _resetRecording();
    if (!isMobilePortrait()) return;
    updateDeckCountBadge();
    updateReplayBarMobile();
    _updateShareRecordingSection();
    _updateShareCurrentInfo();
  });
}

// ══════════════════ タスクB: 再生モード（設計書 §B） ══════════════════
// 下部バーの中身を [◀前][▶再生/❚❚停止][次▶][x/N][✕終了] に差し替え、その直上に
// スライダー帯（#solMobileReplayStrip）を出す。既存の #replayBack/#replayPlay/#replayFwd/
// #replaySlider/#replayCounter を .click()／textContent 追従で使い、ロジックを複製しない
// （B-2）。旧オーバーレイ（.sol-mobile-fullbar-open・⋯メニュー「リプレイ操作」）は廃止。

let _playbackMode = false;
// 第6次検収-1: 再生モードへ入った入口（'menu'|'url'|'saved'|'share'）。'share' の場合のみ
// 通常の「✕ 終了」で共有シートへ自動的に戻る（enterPlaybackMode の呼び出し元ごとに指定）。
let _playbackOrigin = null;
// 第7次検収: 「再生して確認」で停止済み録画があるときに渡す 1-based 範囲 {start, end}。
// 範囲再生でなければ null（従来どおり全体を再生）。
let _playbackRange = null;

/**
 * 第7次検収: 範囲再生中、絶対カーソル位置(getCursor()の値)から範囲内の相対位置(0..N)を求める。
 * 相対0 ＝ 範囲開始手の直前（seekTo(start-2) 相当）、相対N ＝ 範囲末尾（end手目）まで適用済み。
 * @returns {number}
 */
function _playbackRelCursor() {
  if (!_playbackRange) return 0;
  return getCursor() - (_playbackRange.start - 2);
}
/** @returns {number} 範囲の手数 N = end-start+1（範囲再生でなければ 0） */
function _playbackRangeTotal() {
  return _playbackRange ? (_playbackRange.end - _playbackRange.start + 1) : 0;
}

/** H-1: #replayPlay が自動再生中（.play-active）なら既存の togglePlay に委譲して止める。 */
function _stopAutoPlayIfNeeded() {
  const playBtn = document.getElementById('replayPlay');
  if (playBtn?.classList.contains('play-active')) {
    playBtn.click();
  }
}

/**
 * 再生モードのバー・帯を #replayBack/#replayPlay/#replayCounter/#replaySlider の
 * 現在値から同期する。#replayCounter・#replayPlay の MutationObserver から都度呼ぶ（B-2/B-3/M-7）。
 * M-6: 前/再生/次ボタンの disabled も元ボタンからコピーする。
 * 第7次検収: 範囲再生中（_playbackRange）は、カウンター・スライダー・前後/再生ボタンの
 * disabled を「範囲内の相対値」で上書きする（受け手が見るものと同じ範囲だけを操作させる）。
 * 自動再生が範囲末尾に達したら #replayPlay.click() で togglePlay に委譲して止める。
 */
function _syncPlaybackFromMain() {
  const counterEl = document.getElementById('replayCounter');
  const mobileCounter = document.getElementById('solMobilePlayCounter');

  const mainSlider = document.getElementById('replaySlider');
  const stripSlider = document.getElementById('solMobileReplayStripSlider');

  const playBtn = document.getElementById('replayPlay');
  const mobilePlayBtn = document.getElementById('solMobilePlayToggleBtn');

  const backBtn = document.getElementById('replayBack');
  const mobileBackBtn = document.getElementById('solMobilePlayBackBtn');

  const fwdBtn = document.getElementById('replayFwd');
  const mobileFwdBtn = document.getElementById('solMobilePlayFwdBtn');

  if (_playbackRange) {
    const total = _playbackRangeTotal();
    const rel = Math.max(0, Math.min(total, _playbackRelCursor()));

    if (mobileCounter) mobileCounter.textContent = `${rel}/${total}`;

    if (stripSlider) {
      stripSlider.min = '0';
      stripSlider.max = String(total);
      stripSlider.value = String(rel);
      stripSlider.disabled = false;
    }

    if (playBtn && mobilePlayBtn) {
      mobilePlayBtn.textContent = playBtn.classList.contains('play-active') ? '❚❚ 停止' : '▶ 再生';
      mobilePlayBtn.disabled = rel >= total;
    }
    if (mobileBackBtn) mobileBackBtn.disabled = rel <= 0;
    if (mobileFwdBtn) mobileFwdBtn.disabled = rel >= total;

    // 自動再生が範囲末尾に達したら togglePlay に委譲して止める（#replayCounter の変化を
    // 検知するたびにここへ来るため、範囲を1手も超えずに止まる）。
    if (playBtn?.classList.contains('play-active') && rel >= total) {
      playBtn.click();
    }
    return;
  }

  if (counterEl && mobileCounter) mobileCounter.textContent = counterEl.textContent;

  if (mainSlider && stripSlider) {
    stripSlider.min = mainSlider.min;
    stripSlider.max = mainSlider.max;
    stripSlider.value = mainSlider.value;
    stripSlider.disabled = mainSlider.disabled;
  }

  if (playBtn && mobilePlayBtn) {
    mobilePlayBtn.textContent = playBtn.classList.contains('play-active') ? '❚❚ 停止' : '▶ 再生';
    mobilePlayBtn.disabled = playBtn.disabled; // M-6
  }

  // M-6: 前/次ボタンの disabled を元ボタンからコピー（末尾で「次」を disabled にする等）
  if (backBtn && mobileBackBtn) mobileBackBtn.disabled = backBtn.disabled;
  if (fwdBtn && mobileFwdBtn) mobileFwdBtn.disabled = fwdBtn.disabled;
}

/**
 * 第7次検収: 帯の範囲ラベル（「録画範囲を再生中（s〜e手目）」）の表示を更新する。
 * @param {{start:number, end:number}|null} range
 */
function _updatePlaybackRangeBadge(range) {
  const el = document.getElementById('solMobilePlayRangeBadge');
  if (!el) return;
  if (range) {
    el.textContent = `範囲を再生中（${range.start}〜${range.end}手目）`;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

/**
 * 再生モードへ入る。入口（B-1）: ⋯メニュー「リプレイを再生」／共有リンクで開いたとき／
 * 保存済みリプレイを開いたとき／共有シート「再生して確認」。
 * M-6: 記録が0手（setup込み）なら入らずトーストのみ表示する。
 * 第6次検収-1: origin を覚えておく。'share'（共有シートの「再生して確認」）の場合のみ、
 * 通常の「✕ 終了」で共有シートへ自動的に戻れるようにする（exitPlaybackMode 参照）。
 * 第7次検収: range（1-based {start,end}）を渡すと、その範囲だけを再生する
 * （受け手が見るものと同じにする）。入場時に seekTo(start-2) で範囲開始手の直前へ移動し、
 * その位置を相対0として扱う（_syncPlaybackFromMain が絶対↔相対の変換を担う）。
 * @param {'menu'|'url'|'saved'|'share'|null} [origin]
 * @param {{start:number, end:number}|null} [range]
 */
function enterPlaybackMode(origin = null, range = null) {
  if (!isMobilePortrait() || _playbackMode) return;
  if (getLogLength() + getSetupLogs().length === 0) { // M-6
    showToast('記録がありません');
    return;
  }
  _playbackMode = true;
  _playbackOrigin = origin;
  _playbackRange = range; // 第7次検収
  clearMobileSelection(); // B-4: 選択中カードが残っていれば解除
  setMobilePlaybackMode(true); // drag-drop.js: タップ選択・ドラッグを無効化（B-4）
  document.body.classList.add('sol-playback'); // H-3: 盤面全体を pointer-events:none で二重防御
  // M-5: イベント経由に頼らず全シート・オーバーレイを明示的に閉じる
  closeMenuSheet();
  closeShareSheet();
  closeDeckSheet();
  setHidden('solMobileActionSheet', true);
  closeCardDetailOverlay();

  setHidden('solMobileBar', false); // 共有リンクの初回ロード等、まだ通常表示に至っていない場合の保険
  setHidden('solMobileBarNormal', true);
  setHidden('solMobilePlayBar', false);
  setHidden('solMobileReplayStrip', false);
  _updatePlaybackRangeBadge(range); // 第7次検収

  if (range) {
    // 第7次検収: 範囲開始手の直前（start=1 なら初期状態）へ移動してから同期する
    seekTo(range.start - 2);
  }
  _syncPlaybackFromMain();
  updateDeckCountBadge(); // 第6次検収-2a: 帯の「デッキ N」バッジを最新枚数にする

  // B-3: 盤面を覆わないよう下部予約量(52→88px)を増やし、既存の再フィット経路(resize)を発火する
  document.documentElement.style.setProperty('--sol-mobile-bottom-reserve', '88px');
  window.dispatchEvent(new Event('resize'));
}

/**
 * 再生モードを終了する。
 * H-1: 自動再生中なら先に止める。
 * H-4: 途中から分岐して続ける機能は作らない方針のため、カーソルが末尾（全記録の末尾。
 * 範囲再生中であっても全記録の末尾でよい＝司令塔裁定）でなければ末尾まで進めてから
 * 終了し、トーストで知らせる。
 * 第6次検収-1: 終了後に共有シートへ戻すかどうかを制御する。
 * @param {{ forceOpenShare?: boolean, returnToOrigin?: boolean }} [opts]
 *   forceOpenShare: true なら origin に関係なく終了後に共有シートを開く（再生バーの「共有」ボタン用）。
 *   returnToOrigin: false なら origin==='share' でも共有シートへ戻さない（回転による強制終了用）。既定 true。
 */
function exitPlaybackMode({ forceOpenShare = false, returnToOrigin = true } = {}) {
  if (!_playbackMode) return;
  _stopAutoPlayIfNeeded(); // H-1

  const total = getLogLength();
  if (getCursor() < total - 1) { // H-4: 全記録の末尾まで進める（範囲再生中でも同じ）
    seekTo(total - 1);
    showToast('最後の手まで進めました');
  }

  const origin = _playbackOrigin;
  _playbackOrigin = null;
  _playbackRange = null; // 第7次検収: 範囲状態を破棄
  _updatePlaybackRangeBadge(null);

  _playbackMode = false;
  setMobilePlaybackMode(false);
  document.body.classList.remove('sol-playback'); // H-3
  setHidden('solMobilePlayBar', true);
  setHidden('solMobileReplayStrip', true);
  setHidden('solMobileBarNormal', false);

  document.documentElement.style.setProperty('--sol-mobile-bottom-reserve', '52px');
  window.dispatchEvent(new Event('resize'));

  // 第6次検収-1: 「再生して確認」から入った場合は共有シートへ自動的に戻る
  if (forceOpenShare) {
    openShareSheet('share'); // 再生バーの「共有」
  } else if (returnToOrigin && origin === 'share') {
    openShareSheet(); // 「再生して確認」の前に開いていた用途（保存／共有）へ戻る
  }
}

function initPlaybackBar() {
  document.getElementById('solMobilePlayBackBtn')?.addEventListener('click', () => {
    document.getElementById('replayBack')?.click();
  });
  document.getElementById('solMobilePlayToggleBtn')?.addEventListener('click', () => {
    document.getElementById('replayPlay')?.click();
  });
  document.getElementById('solMobilePlayFwdBtn')?.addEventListener('click', () => {
    document.getElementById('replayFwd')?.click();
  });
  // 第6次検収-1: origin に関係なく終了→共有シートを開く（再生バーの小さな「共有」ボタン）
  document.getElementById('solMobilePlayShareBtn')?.addEventListener('click', () => {
    exitPlaybackMode({ forceOpenShare: true });
  });
  document.getElementById('solMobilePlayExitBtn')?.addEventListener('click', () => { exitPlaybackMode(); });

  // 帯のスライダー → 既存の #replaySlider の input イベントを発火させて seekTo させる（B-2）。
  // 第7次検収: 範囲再生中は stripSlider の値が「範囲内の相対値」なので、絶対位置に変換して
  // seekTo を直接呼ぶ（#replaySlider 経由にすると絶対値として誤解釈されるため）。
  const mainSlider = document.getElementById('replaySlider');
  const stripSlider = document.getElementById('solMobileReplayStripSlider');
  if (mainSlider && stripSlider) {
    stripSlider.addEventListener('input', () => {
      if (_playbackRange) {
        const rel = parseInt(stripSlider.value, 10) || 0;
        seekTo(_playbackRange.start - 2 + rel);
        return;
      }
      mainSlider.value = stripSlider.value;
      mainSlider.dispatchEvent(new Event('input', { bubbles: true }));
    });
  }

  // 再生モード中だけバー・帯を追従させる共通コールバック
  const syncIfPlayback = () => { if (_playbackMode) _syncPlaybackFromMain(); };

  // #replayCounter の変化（stepForward/stepBack/seekTo/togglePlay 等、既存の手数更新経路）を監視
  const counterEl = document.getElementById('replayCounter');
  if (counterEl) {
    new MutationObserver(syncIfPlayback).observe(counterEl, { childList: true, characterData: true, subtree: true });
  }
  // M-7: #replayPlay の class（play-active）／disabled 属性変化も監視する
  const playBtn = document.getElementById('replayPlay');
  if (playBtn) {
    new MutationObserver(syncIfPlayback).observe(playBtn, { attributes: true, attributeFilter: ['class', 'disabled'] });
  }
}

/**
 * 共有リンク・保存したリプレイ（?replay= / #replay= / ?saved=）をスマホ縦で開くときは、読み込みより前に
 * 下部の予約量を再生モードの 88px にしておく（main.js が最初の盤面合わせの前に呼ぶ）。
 * 以前は読み込みの後に再生モードへ入ってから 52→88px に変えて盤面を縮め直しており、盤面全体が動いて
 * CLS 0.016 が出ていた（2026-10-21 本番計測）。読み込みに失敗した・記録が空で再生モードに入らなかったときは
 * 読み込みの終わり（replay-ui.js の sol-replay-url-settled）で 52px に戻して合わせ直す。
 * ?at=end（最後の盤面から続ける）は再生モードに入らないので対象外（呼ぶ側で判定する）。
 */
export function reservePlaybackSpaceForUrlReplay() {
  if (!isMobilePortrait()) return;
  document.documentElement.style.setProperty('--sol-mobile-bottom-reserve', '88px');
  document.addEventListener('sol-replay-url-settled', () => {
    if (_playbackMode) return;
    document.documentElement.style.setProperty('--sol-mobile-bottom-reserve', '52px');
    window.dispatchEvent(new Event('resize'));
  }, { once: true });
}

/**
 * B-1: 共有リンク（#replay=/?replay=）で開いたとき、再生モードを自動で開く。
 * replay-ui.js の _tryLoadFromURL が読み込み完了後に発火するイベントを受ける
 * （このタイミングでは盤面・ログは既に反映済み）。
 */
function initPlaybackURLAutoOpen() {
  document.addEventListener('sol-replay-loaded-from-url', (e) => {
    if (!isMobilePortrait()) return;
    _resetRecording(); // タスクA-1: 共有リンクから開く で録画状態を破棄
    // 「最後の盤面から続ける」（?at=end）は再生モードに入らず、通常の操作画面のまま続ける
    if (e.detail?.atEnd) return;
    enterPlaybackMode('url');
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
  initRecording(); // タスクA
  initRangePicker(); // 範囲を選ぶ画面（2026-10-20）
  initPlaybackBar(); // タスクB
  initPlaybackURLAutoOpen(); // タスクB-1
  initMenuSheet();
  initSidebarSheet();
  initDeckLoadProgress();
  initCardDetailPanel();
  initActionSheet();
  initDeckSheet();
  initShareSheet();
  initCommentSheet();
  initTokenSheet();
  initEmptyCta();
  initClearEverything();
  initLandscapeHint(); // A-3: portrait 判定とは独立（横向きスマホでのみ表示するため）

  // H-1: resize/orientationchange の逐次発火はやめ、matchMedia の change に一本化する。
  watchMobilePortrait(handlePortraitChange);
}
