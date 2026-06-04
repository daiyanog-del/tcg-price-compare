import {
  returnAllCardsToDeck,
  drawRandomCards,
} from '../components/card-manager.js';
import { SaveLoadModal } from './save-load-modal.js';
import { registerCardImage } from '../services/replay-service.js';
import { initDeckInputPanel, loadDeckFromText, loadExDeckFromText } from './deck-input-panel.js';
import { parseNeuronPdf } from '/static/shared/neuron-pdf-parser.js';
import { NeuronPreviewModal } from '/static/shared/neuron-preview-modal.js';

/**
 * UIイベントハンドラ（カード相場向け改変版）
 * ベース: Solo Mode (Fugarta, MIT) — 絵文字除去・リプレイフック・デッキ入力追加
 */

/**
 * 盤面保存（画像）ボタンのイベントハンドラ
 */
export function handleSaveBoard() {
  const elementsToHide = [
    document.querySelector('.randomButton-container'),
    document.querySelector('.left-rectangle-container'),
    document.getElementById('replayBarContainer'),
    document.getElementById('deckInputContainer'),
  ];

  const validElements = elementsToHide.filter(el => el !== null);
  const originalStyles = validElements.map(el => el.style.display);
  validElements.forEach(el => el.style.display = 'none');

  const titleElement = document.querySelector('.title');
  const titleText = titleElement ? titleElement.textContent : '一人回し';
  const formattedTitle = titleText.replace(/[\s　]/g, '_');

  html2canvas(document.getElementById('mainContainer')).then(canvas => {
    const link = document.createElement('a');
    link.download = `${formattedTitle}.png`;
    link.href = canvas.toDataURL();
    link.click();
    validElements.forEach((el, i) => el.style.display = originalStyles[i]);
  });
}

/**
 * X(Twitter)投稿ボタンのイベントハンドラ
 */
export function handleShareX() {
  const tweetText = encodeURIComponent('カード相場 一人回しシミュレータで展開を確認しました\n#カード相場\nhttps://kso.b01.dev/solitaire');
  const url = `https://twitter.com/intent/tweet?text=${tweetText}`;
  window.open(url, '_blank');
}

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
      const SAVED_DECKS_KEY = 'cardprice_saved_decks';
      try {
        const list = JSON.parse(localStorage.getItem(SAVED_DECKS_KEY) || '[]');
        const existing = list.find(d => d.name === name);
        if (existing) {
          if (!confirm(`「${name}」はすでに保存されています。上書きしますか？`)) return;
          existing.text = combined;
          existing.updated = Date.now();
        } else {
          list.push({ id: 'd_' + Date.now(), name, text: combined, updated: Date.now() });
        }
        localStorage.setItem(SAVED_DECKS_KEY, JSON.stringify(list));
      } catch (e) {
        console.error('マイデッキ保存エラー:', e);
      }
    },
    onLoad: async ({ mainText, exText }) => {
      if (mainText) await loadDeckFromText(mainText);
      if (exText)   await loadExDeckFromText(exText);
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
  returnAllCardsToDeck();
  const selectedCards = drawRandomCards('poolRow', 5);
  const centerSlot = document.querySelector('.center-slot');
  const drawnIds = [];
  selectedCards.forEach(card => {
    centerSlot.appendChild(card);
    const img = card.querySelector('img');
    if (img) drawnIds.push(img.id);
  });

  if (typeof window.replayLog === 'function') {
    window.replayLog({ actionType: 'resetDeck', drawnIds });
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
    const img = card.querySelector('img');
    if (img && typeof window.replayLog === 'function') {
      window.replayLog({ actionType: 'draw', cardId: img.id, zoneId: 'center-slot' });
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

  document.getElementById('saveButton')
    ?.addEventListener('click', handleSaveBoard);

  document.getElementById('shareXButton')
    ?.addEventListener('click', handleShareX);

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
