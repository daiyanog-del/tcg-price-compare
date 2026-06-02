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

  console.log('一人回しシミュレータ initialized');
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
