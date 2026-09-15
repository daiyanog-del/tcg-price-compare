import {
  returnAllCardsToDeck,
  drawRandomCards,
} from '../components/card-manager.js';
import { SaveLoadModal } from './save-load-modal.js';
import { registerCardImage, logSetupEvent, getLogLength, getCursor } from '../services/replay-service.js';
import { initDeckInputPanel, loadDeckFromNeuron, savedDecksGet, savedDecksSet } from './deck-input-panel.js';
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
      const combined = [mainText, exText].filter(Boolean).join('\n');
      try {
        const list = savedDecksGet();
        const existing = list.find(d => d.name === name);
        if (existing) {
          if (!confirm(`「${name}」はすでに保存されています。上書きしますか？`)) return;
          existing.text = combined;
          existing.updated = Date.now();
        } else {
          list.push({ id: 'd_' + Date.now(), name, text: combined, updated: Date.now() });
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
 * リセット&5ドローボタンのイベントハンドラ
 */
export function handleResetAndDraw() {
  // 記録0件（デッキ読込直後）かどうかを先に判定しておく（returnAllCardsToDeck/drawRandomCardsは
  // DOM移動と抽選のみでログ記録を行わないため前後どちらで判定しても結果は変わらないが、
  // 意図を明確にするためログ記録直前ではなく処理冒頭で判定する）
  const isInitialState = getLogLength() === 0 && getCursor() === -1;

  returnAllCardsToDeck();
  const selectedCards = drawRandomCards('poolRow', 5);
  const centerSlot = document.querySelector('.center-slot');
  // リセット&5ドローは盤面の初期化操作として手数（取消・巻き戻しの対象）には記録しない仕様。
  // ただし開始直後（記録0件）のみは例外: 案B（2026-09-14承認）として setup ログに記録し、
  // 共有リンク・巻き戻し・取消の後でも初期手札5枚が復元されるようにする。
  // 元々（本変更前）は途中でのリセット&5ドローも通常ログ（window.replayLog経由）として記録していたが、
  // 2026-09-14にユーザーの判断で「途中リセットは記録しない」仕様に変更した。
  // 理由: 取消・共有リンクで手札が復元されない副作用は許容し、リセット操作自体を手数として
  // カウントしない体験を優先するため。
  const drawnIds = selectedCards
    .map(card => card.querySelector('.tier-item')?.id)
    .filter(Boolean);
  selectedCards.forEach(card => {
    centerSlot.appendChild(card);
  });
  if (isInitialState && drawnIds.length) {
    logSetupEvent({ actionType: 'resetDeck', drawnIds, setup: true });
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
    ?.addEventListener('click', handleResetAndDraw);

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
