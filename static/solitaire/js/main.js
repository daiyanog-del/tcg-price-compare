/**
 * main.js — 一人回しシミュレータ エントリーポイント
 * ベース: Solo Mode (Fugarta, MIT) を改変
 */

import { initializeDesktopDragDrop, enableTouchDrag } from './components/drag-drop.js';
import { initializeCards } from './components/card-manager.js';
import { initializeCounter } from './components/counter-manager.js';
import { initializeEventListeners } from './ui/event-handlers.js';
import { initializeOCRWorker } from './services/ocr-service.js';
import { initReplayUI } from './ui/replay-ui.js';
import { registerCardImage } from './services/replay-service.js';
import { initTokenGenerator } from './ui/token-generator.js';
import { initRandomTools } from './ui/random-tools.js';
import { initCardInfoPanel } from './ui/card-info-panel.js';
import { saveSessionResume, loadSessionResume } from './services/save-load-service.js';
import { initOpponentTray } from './ui/opponent-tray.js';

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

  // slot-width に依存しない固定オーバーヘッド:
  //   mainContainer padding-top  :  4px
  //   フィールド行間 gap×2       : 20px  (gap=10px固定 on ≥1000px)
  //   center-row margin-top      :  6px
  //   center-row内固定(label+pad): 29px  (imagePool2: padding8+border2+label19)
  //   imagePool margin-top       :  4px
  //   imagePool内固定(label+pad) : 29px  (imagePool: padding8+border2+label19)
  //   sol-field-area padding-bottom: 6px
  const FIXED_MISC = 4 + 20 + 6 + 29 + 4 + 29 + 6; // 98px

  const fixed     = navH + trayH + replayH + FIXED_MISC;
  const available = window.innerHeight - fixed;

  // slot_h = slot_w × 1.45 として比例係数 7.54 を計算済み
  const slotW = Math.max(60, Math.min(110, Math.floor(available / 7.54)));
  document.documentElement.style.setProperty('--slot-width', `${slotW}px`);
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

  // ゾーン枚数カウント初期化
  initZoneCounts();

  // カード追加時の画像登録
  initCardImageRegistration();

  // OCR Workerを事前初期化（バックグラウンド）
  initializeOCRWorker().catch(error => {
    console.warn('OCR Workerのプリロードに失敗（処理は続行）:', error);
  });

  // ページ遷移後の盤面復元
  const restored = loadSessionResume();
  if (restored) {
    // 復元したカードをリプレイ画像辞書に登録
    document.querySelectorAll('img.tier-item').forEach(img => {
      if (img.id && img.src) registerCardImage(img.id, img.src);
    });
    _showToast('前回の盤面を復元しました');
  }

  // ビューポートに合わせて --slot-width を初期設定
  fitFieldToViewport();

  // ウィンドウリサイズ時に再計算（デバウンス 150ms）
  window.addEventListener('resize', () => {
    clearTimeout(_fitTimer);
    _fitTimer = setTimeout(fitFieldToViewport, 150);
  });

  // 相手妨害トレイの開閉に連動して再計算
  window.addEventListener('opp-tray-resize', fitFieldToViewport);

  console.log('一人回しシミュレータ initialized');
}

/** 手札・EXデッキ・デッキ・墓地・除外の枚数をラベル横にリアルタイム表示 */
function initZoneCounts() {
  const zones = [
    { label: document.querySelector('#imagePool .pool-label'),     cards: document.getElementById('poolRow') },
    { label: document.querySelector('#imagePool2 .pool-label'),    cards: document.getElementById('poolRow2') },
    { label: document.querySelector('.sol-hand-area .pool-label'), cards: document.querySelector('.center-slot') },
    { label: document.querySelector('.sol-grave .pool-label'),     cards: document.querySelector('.sol-grave .side-slot') },
    { label: document.querySelector('.side-slots-container .sol-side-area:not(.sol-grave) .pool-label'),
      cards:  document.querySelector('.side-slots-container .sol-side-area:not(.sol-grave) .side-slot') },
  ];

  for (const { label, cards } of zones) {
    if (!label || !cards) continue;
    const badge = document.createElement('span');
    badge.className = 'zone-count';
    label.appendChild(badge);
    const update = () => {
      badge.textContent = cards.querySelectorAll('.tier-item-wrapper').length;
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
