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
 * アプリケーション初期化
 */
function initializeApp() {
  // デスクトップドラッグ&ドロップを初期化
  initializeDesktopDragDrop();

  // 初期カードを設定（裏面プレースホルダー）
  const initialImages = [
    '/static/solitaire/images/blanck.png',
    '/static/solitaire/images/blanck.png',
    '/static/solitaire/images/blanck.png',
  ];
  initializeCards(initialImages);

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

  // カード追加時の画像登録
  initCardImageRegistration();

  // OCR Workerを事前初期化（バックグラウンド）
  initializeOCRWorker().catch(error => {
    console.warn('OCR Workerのプリロードに失敗（処理は続行）:', error);
  });

  console.log('一人回しシミュレータ initialized');
}

window.addEventListener('DOMContentLoaded', initializeApp);
