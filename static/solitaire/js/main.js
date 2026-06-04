/**
 * main.js — 一人回しシミュレータ エントリーポイント
 * ベース: Solo Mode (Fugarta, MIT) を改変
 */

import { initializeDesktopDragDrop, enableTouchDrag } from './components/drag-drop.js';
import { initializeCards } from './components/card-manager.js';
import { initializeCounter } from './components/counter-manager.js';
import { initializeEventListeners } from './ui/event-handlers.js';
import { initReplayUI } from './ui/replay-ui.js';
import { registerCardImage, registerCardIsEx } from './services/replay-service.js';
import { initTokenGenerator } from './ui/token-generator.js';
import { initRandomTools } from './ui/random-tools.js';
import { initCardInfoPanel } from './ui/card-info-panel.js';
import { saveSessionResume, loadSessionResume } from './services/save-load-service.js';
import { initOpponentTray, updateCipWidth } from './ui/opponent-tray.js';
import { initFeedbackModal } from './ui/feedback-modal.js';
import { initSidebarToggle } from './ui/sidebar-toggle.js';

/**
 * カード追加時にリプレイ画像辞書へ登録するフック
 * MutationObserver でプールの変化を監視し cardId→src を登録
 */
function initCardImageRegistration() {
  const observePool = (poolId) => {
    const pool = document.getElementById(poolId);
    if (!pool) return;
    const obs = new MutationObserver(mutations => {
      mutations.forEach(mut => {
        mut.addedNodes.forEach(node => {
          if (!(node instanceof Element)) return;
          const img = node.querySelector('img.tier-item')
            ?? (node.matches?.('img.tier-item') ? node : null);
          if (img?.id && img.src) {
            registerCardImage(img.id, img.src);
            if (poolId === 'poolRow2') registerCardIsEx(img.id);
          }
        });
      });
    });
    obs.observe(pool, { childList: true });
  };

  observePool('poolRow');
  observePool('poolRow2');
}

/**
 * ビューポート高さに合わせて --slot-width を動的に設定する。
 *
 * 全縦スペースの内訳:
 *   固定:    nav高さ + トレイヘッダ高さ + リプレイバー + 各種パディング/マージン
 *   比例:    slot_w × 7.54
 *            = フィールド3行(slot_w×1.45×3) + center-row(slot_w×1.45×1.1)
 *              + imagePool(slot_w×1.45×1.1)
 *
 * トレイ開閉・ウィンドウリサイズのたびに再計算し、
 * 常にスクロールなしで全体が収まるよう自動調整する。
 */
function fitFieldToViewport() {
  const nav    = document.querySelector('.sol-nav');
  const tray   = document.getElementById('opponentTray');
  const replay = document.getElementById('replayBarContainer');
  if (!nav || !tray) return;

  const navH    = nav.offsetHeight;
  const trayH   = tray.offsetHeight;   // 閉=28px 開=閉+ボディ高さ
  const replayH = replay ? replay.offsetHeight + 4 : 36; // margin-top(4px)込み

  // スマホ検出: 短辺 < 500px → スマホ（縦/横向き問わず検知）
  const isPhone     = Math.min(window.innerWidth, window.innerHeight) < 500;
  const isLandscape = window.innerWidth > window.innerHeight;

  // 高さ基準:
  //   横向きスマホ → 盤面3行のみをビューポートに収め、手札/デッキはスクロール
  //   縦向きスマホ・PC/タブレット → 全体フィット (比例係数 7.54)
  let slotW_h;
  if (isPhone && isLandscape) {
    // mainContainer padding-top(4) + row-gaps(20) + sol-field-area padding-bottom(6)
    const BOARD_FIXED = 30;
    const availForBoard = window.innerHeight - navH - trayH - replayH - BOARD_FIXED;
    slotW_h = Math.floor(availForBoard / 4.35);
  } else {
    // slot-width に依存しない固定オーバーヘッド:
    //   mainContainer padding-top  :  4px
    //   フィールド行間 gap×2       : 20px  (gap=10px固定 on ≥1000px)
    //   center-row margin-top      :  6px
    //   center-row内固定(label+pad): 29px  (imagePool2: padding8+border2+label19)
    //   imagePool margin-top       :  4px
    //   imagePool内固定(label+pad) : 29px  (imagePool: padding8+border2+label19)
    //   sol-field-area padding-bottom: 6px
    const FIXED_MISC = 4 + 20 + 6 + 29 + 4 + 29 + 6; // 98px
    const fixed      = navH + trayH + replayH + FIXED_MISC;
    const available  = window.innerHeight - fixed;
    slotW_h = Math.floor(available / 7.54);
  }

  // 横幅基準: .sol-main の実測幅を使う。サイドバー開閉で幅が変わるため実測値を使用。
  // ・縦向きスマホ: CSS で side-slots-container が縦積みになるため 7.2 列 / 88px
  //     (6列グリッド + 1.2列サイド) × slot + (グリッドgap50 + mainContentgap10 + 側面padding16 + 各種余白12)
  // ・横向きスマホ: side-slots-container が横並びのため 8.4 列 / 126px
  //     (6列グリッド + 2.4列サイド×2) × slot + (gap50 + gap10 + gap10 + 側面padding32 + 各種余白24)
  // ・PC/タブレット: 既存の 7.1 列 / 86px
  const mainEl  = document.querySelector('.sol-main');
  const availW  = mainEl ? mainEl.clientWidth : window.innerWidth;

  let slotW_w;
  if (isPhone && !isLandscape) {
    slotW_w = Math.floor((availW - 88) / 7.2);
  } else if (isPhone && isLandscape) {
    slotW_w = Math.floor((availW - 126) / 8.4);
  } else {
    slotW_w = Math.floor((availW - 86) / 7.1);
  }

  const minW = isPhone ? 26 : 60;
  const maxW = isPhone ? 200 : 110;
  const slotW = Math.max(minW, Math.min(maxW, Math.min(slotW_h, slotW_w)));
  document.documentElement.style.setProperty('--slot-width', `${slotW}px`);

  // リサイズ・初期化時にもパネル幅を再計算（開閉問わず）
  requestAnimationFrame(() => updateCipWidth());
}

let _fitTimer = null;

/**
 * アプリケーション初期化
 */
function initializeApp() {
  // デスクトップドラッグ&ドロップを初期化
  initializeDesktopDragDrop();

  // ダミー画像なし（デッキ読込後にカードが追加される）
  initializeCards([]);

  // カウンターを初期化
  document.querySelectorAll('.counter-container').forEach(container => {
    initializeCounter(container);
  });

  // タッチ対応
  const parentCounter = document.querySelector('#parent.counter-container');
  if (parentCounter) {
    parentCounter.addEventListener('touchstart', enableTouchDrag, { passive: false });
  }

  // イベントリスナーを設定
  initializeEventListeners();

  // リプレイUI初期化（window.replayLog バインド含む）
  initReplayUI();

  // トークン生成UI初期化
  initTokenGenerator();

  // コイントス・ダイスロール初期化
  initRandomTools();

  // カード詳細パネル初期化
  initCardInfoPanel();

  // 相手の想定妨害ミニ盤面を初期化
  initOpponentTray();

  // 左サイドバー・右パネルの開閉を初期化
  initSidebarToggle();

  // フィードバック（不具合・要望）モーダルを初期化
  initFeedbackModal();

  // ゾーン枚数カウント初期化
  initZoneCounts();

  // カード追加時の画像登録
  initCardImageRegistration();

  // ページ遷移後の盤面復元
  const restored = loadSessionResume();
  if (restored) {
    // 復元したカードをリプレイ画像辞書に登録
    document.querySelectorAll('img.tier-item').forEach(img => {
      if (img.id && img.src) {
        registerCardImage(img.id, img.src);
        const wrapper = img.closest('.tier-item-wrapper');
        if (wrapper?.id.includes(' ex')) registerCardIsEx(img.id);
      }
    });
    _showToast('前回の盤面を復元しました');
  }

  // ビューポートに合わせて --slot-width を初期設定
  // ※ fitFieldToViewport 内の RAF でパネル幅（--cip-width）も更新される
  fitFieldToViewport();

  // ウィンドウリサイズ時に再計算（デバウンス 150ms）
  window.addEventListener('resize', () => {
    clearTimeout(_fitTimer);
    _fitTimer = setTimeout(fitFieldToViewport, 150);
  });

  // デスクトップPWA（standalone）では window.resize が発火しないケースがある（Chromium既知挙動）。
  // visualViewport の resize を追加のリスナーとして登録しフォールバックとする。
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', () => {
      clearTimeout(_fitTimer);
      _fitTimer = setTimeout(fitFieldToViewport, 150);
    });
  }

  // 相手妨害トレイの開閉に連動して再計算
  window.addEventListener('opp-tray-resize', fitFieldToViewport);

  console.log('一人回しシミュレータ initialized');
}

/**
 * 墓地・除外スロットのカード重なり量を枚数に応じて動的調整する。
 * 親エリア(.sol-side-area)の確定高さを基準にし、
 * 枚数が増えるほど深く重ねて高さを抑制する。
 * 最小圧縮でも収まらない場合は overflow-y:auto によりゾーン内スクロールが発動する。
 */
function adjustSideStack(slotEl) {
  const area = slotEl.closest('.sol-side-area');
  if (!area) return;

  // CSS変数からフィールド高さを計算（DOMを測定しない: 測定時点でDOMが膨らんでいるため）
  // CSS の max-height: calc(3 * var(--slot-height) + 2 * var(--gap)) と同じ計算式
  const rootStyle = getComputedStyle(document.documentElement);
  const slotWidth = parseFloat(rootStyle.getPropertyValue('--slot-width')) || 120;
  const slotHeight = slotWidth * 1.45; // --slot-height = --slot-width * 1.45
  const gap = parseFloat(rootStyle.getPropertyValue('--gap')) || 12;
  const areaMaxH = 3 * slotHeight + 2 * gap; // CSSのmax-heightと同値

  // side-slotの利用可能高さ = エリア上限 - ラベル - 縦padding
  const label = area.querySelector('.pool-label');
  const labelH = label ? label.offsetHeight : 0;
  const areaStyle = getComputedStyle(area);
  const paddingV = parseFloat(areaStyle.paddingTop) + parseFloat(areaStyle.paddingBottom);
  const avail = areaMaxH - labelH - paddingV;

  const wrappers = slotEl.querySelectorAll('.tier-item-wrapper');
  const N = wrappers.length;

  // 1枚以下は重なり調整不要: 変数をリセットしてCSS既定（90%重ね）に戻す
  if (N <= 1) {
    slotEl.style.removeProperty('--stack-overlap');
    return;
  }

  // カード高さ: 先頭ラッパーの実測値、無ければCSS変数から算出
  const cardH = wrappers[0].offsetHeight || slotHeight;

  const MIN_VISIBLE = 6;            // カードが識別できる最小ステップ(px)
  const naturalStep = cardH * 0.1; // デフォルト（90%重ね）時の増分

  // 全枚数を avail に収めるための理想ステップを計算
  const idealStep = avail > cardH ? (avail - cardH) / (N - 1) : MIN_VISIBLE;

  // 下限: MIN_VISIBLE、上限: naturalStep（デフォルトより広げない）
  const step = Math.max(MIN_VISIBLE, Math.min(naturalStep, idealStep));

  // CSS変数に反映（margin-top = -(cardH - step)）
  slotEl.style.setProperty('--stack-overlap', -(cardH - step) + 'px');
}

/** 手札・EXデッキ・デッキ・墓地・除外の枚数をラベル横にリアルタイム表示 */
function initZoneCounts() {
  const zones = [
    { label: document.querySelector('#imagePool .pool-label'),     cards: document.getElementById('poolRow') },
    { label: document.querySelector('#imagePool2 .pool-label'),    cards: document.getElementById('poolRow2') },
    { label: document.querySelector('.sol-hand-area .pool-label'), cards: document.querySelector('.center-slot') },
    { label: document.querySelector('.sol-grave .pool-label'),     cards: document.querySelector('.sol-grave .side-slot'), isSideSlot: true },
    { label: document.querySelector('.side-slots-container .sol-side-area:not(.sol-grave) .pool-label'),
      cards:  document.querySelector('.side-slots-container .sol-side-area:not(.sol-grave) .side-slot'), isSideSlot: true },
  ];

  // リサイズ時に全墓地・除外スロットを再調整（デバウンス付き）
  let _resizeTimer = null;
  const sideSlotEls = zones.filter(z => z.isSideSlot).map(z => z.cards).filter(Boolean);
  window.addEventListener('resize', () => {
    clearTimeout(_resizeTimer);
    _resizeTimer = setTimeout(() => {
      sideSlotEls.forEach(adjustSideStack);
    }, 150);
  });

  for (const { label, cards, isSideSlot } of zones) {
    if (!label || !cards) continue;
    const badge = document.createElement('span');
    badge.className = 'zone-count';
    label.appendChild(badge);
    const update = () => {
      badge.textContent = cards.querySelectorAll('.tier-item-wrapper').length;
      if (isSideSlot) adjustSideStack(cards);
    };
    update();
    new MutationObserver(update).observe(cards, { childList: true, subtree: true });
  }
}

/** ページ遷移直前に盤面をsessionStorageへ退避 */
window.addEventListener('pagehide', () => {
  saveSessionResume();
});

/** 短時間表示のトースト通知 */
function _showToast(msg) {
  const el = document.createElement('div');
  el.className = 'sol-toast';
  el.textContent = msg;
  document.body.appendChild(el);
  // 表示→フェードアウト
  requestAnimationFrame(() => {
    el.classList.add('sol-toast-visible');
    setTimeout(() => {
      el.classList.remove('sol-toast-visible');
      el.addEventListener('transitionend', () => el.remove(), { once: true });
    }, 2500);
  });
}

window.addEventListener('DOMContentLoaded', initializeApp);
