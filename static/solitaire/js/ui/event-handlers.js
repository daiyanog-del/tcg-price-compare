import {
  returnAllCardsToDeck,
  drawRandomCards,
} from '../components/card-manager.js';
import { SaveLoadModal } from './save-load-modal.js';
import { registerCardImage, logSetupEvent, getLogLength, getCursor } from '../services/replay-service.js';
import { initDeckInputPanel, loadDeckFromNeuron, savedDecksGet, savedDecksSet, parseDeckList } from './deck-input-panel.js';
import { parseNeuronPdf } from '/static/shared/neuron-pdf-parser.js';
import { NeuronPreviewModal } from '/static/shared/neuron-preview-modal.js';

/**
 * UIイベントハンドラ（カード相場向け改変版）
 * ベース: Solo Mode (Fugarta, MIT) — 絵文字除去・リプレイフック・デッキ入力追加
 */

/**
 * ニューロンPDFファイル選択ハンドラ
 */
export async function handleNeuronPdfSelect(event) {
  const file = event.target.files[0];
  event.target.value = ''; // 同じファイルを再選択できるようリセット
  if (!file) return;

  let parsed;
  const defaultName = file.name.replace(/\.pdf$/i, '').replace(/[_\-]/g, ' ').trim();

  try {
    parsed = await parseNeuronPdf(file, { includeSide: false });
  } catch (e) {
    parsed = { main: [], ex: [], side: [], warnings: [e.message], ok: false };
  }

  new NeuronPreviewModal({
    parsed,
    defaultName,
    onSave: ({ name, mainText, exText }) => {
      // [EX]区切りを挿入（saveImportedDeck と同じ形式）
      const combined = exText ? mainText + '\n[EX]\n' + exText : mainText;
      // main/ex 配列も保存する（マイデッキは配列を優先して使うため text と同期させる）
      const main = parseDeckList(mainText || '');
      const ex = parseDeckList(exText || '');
      try {
        const list = savedDecksGet();
        const existing = list.find(d => d.name === name);
        if (existing) {
          if (!confirm(`「${name}」はすでに保存されています。上書きしますか？`)) return;
          existing.text = combined;
          existing.main = main;
          existing.ex = ex;
          existing.updated = Date.now();
        } else {
          list.push({ id: 'd_' + Date.now(), name, text: combined, main, ex, updated: Date.now() });
        }
        savedDecksSet(list);
      } catch (e) {
        console.error('マイデッキ保存エラー:', e);
      }
    },
    onLoad: async ({ mainText, exText }) => {
      // L-8: メイン→EXの逐次読込を1組の sol-deck-load-start/end にまとめる
      // （個別に呼ぶとメイン完了時点で中間の「EX 0」トーストが出てしまうため）。
      await loadDeckFromNeuron(mainText, exText);
    },
  }).show();
}

/**
 * セーブボタンのイベントハンドラ
 */
export function handleSaveGame() {
  const modal = new SaveLoadModal('save');
  modal.show();
}

/**
 * ロードボタンのイベントハンドラ
 */
export function handleLoadGame() {
  const modal = new SaveLoadModal('load');
  modal.show();
}

/**
 * リセットボタンのイベントハンドラ
 * 場・手札・墓地・除外のカードをすべてデッキ（EXはEXデッキ）に戻すだけ。5枚は引かない。
 */
export function handleReset() {
  // 記録0件（デッキ読込直後）かどうかを先に判定しておく
  const isInitialState = getLogLength() === 0 && getCursor() === -1;

  returnAllCardsToDeck();
  // リセットは盤面の初期化操作として手数（取消・巻き戻しの対象）には記録しない仕様
  // （2026-09-14 ユーザー判断）。
  // ただし開始直後（記録0件）のみは例外: setup ログ（引いたカード無し）を残す。
  // logSetupEvent は末尾の resetDeck を置換するため、過去の setup ログがあっても上書きされ、
  // 共有リンク・巻き戻し・取消の後でも「全カードがデッキにある」状態から再生される。
  if (isInitialState) {
    logSetupEvent({ actionType: 'resetDeck', drawnIds: [], setup: true });
  }
}

/**
 * 1ドローボタンのイベントハンドラ
 */
export function handleDrawOne() {
  const selectedCards = drawRandomCards('poolRow', 1);
  const centerSlot = document.querySelector('.center-slot');
  selectedCards.forEach(card => {
    centerSlot.appendChild(card);
    // img.tier-item（発売済み）または div.tier-item（プロキシ）どちらも .tier-item で取れる
    const cardEl = card.querySelector('.tier-item');
    if (cardEl && typeof window.replayLog === 'function') {
      window.replayLog({ actionType: 'draw', cardId: cardEl.id, zoneId: 'center-slot' });
    }
  });
}

/**
 * ダブルクリックで表示/非表示を切り替え
 */
export function setupToggleVisibility(elements) {
  elements.forEach(element => {
    element.addEventListener('dblclick', () => {
      element.style.display = element.style.display === 'none' ? '' : 'none';
    });
  });
}

/**
 * すべてのイベントリスナーを設定
 */
export function initializeEventListeners() {
  document.getElementById('neuronPdfUpload')
    ?.addEventListener('change', handleNeuronPdfSelect);

  document.getElementById('resetButton')
    ?.addEventListener('click', handleReset);

  document.getElementById('randomButton')
    ?.addEventListener('click', handleDrawOne);

  document.getElementById('saveButton2')
    ?.addEventListener('click', handleSaveGame);
  document.getElementById('loadButton')
    ?.addEventListener('click', handleLoadGame);

  // フリースペースと除外ゾーンのダブルクリック切り替え
  const sideSlotGroups = document.querySelectorAll('.side-slot-group');
  if (sideSlotGroups.length >= 3) {
    setupToggleVisibility([sideSlotGroups[0], sideSlotGroups[2]]);
  }

  // デッキ入力パネル初期化
  initDeckInputPanel();
}
