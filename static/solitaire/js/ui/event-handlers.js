import {
  addCardToPool,
  addCardsToPool,
  returnAllCardsToDeck,
  drawRandomCards,
} from '../components/card-manager.js';
import { extractCardsFromNeuronImage, extractImagesFromClipboard } from '../services/image-service.js';
import { selectFolderAndFilterImages } from './folder-selector.js';
import { ImageBrowserModal } from './image-browser-modal.js';
import { SaveLoadModal } from './save-load-modal.js';
import { registerCardImage } from '../services/replay-service.js';
import { initDeckInputPanel } from './deck-input-panel.js';

/**
 * UIイベントハンドラ（カード相場向け改変版）
 * ベース: Solo Mode (Fugarta, MIT) — 絵文字除去・リプレイフック・デッキ入力追加
 */

/**
 * 画像アップロードイベントハンドラ
 * @param {Event} event - changeイベント
 */
export async function handleImageUpload(event) {
  const files = Array.from(event.target.files);
  if (files.length === 0) return;

  // 複数ファイル = 個別カード画像として扱う
  if (files.length > 1) {
    for (const file of files) {
      const reader = new FileReader();
      reader.onload = (e) => {
        const src = e.target.result;
        addCardToPool(src, false);
        // リプレイ登録は card-manager 内で行うため不要（後続のregisterCardImage確認済）
      };
      reader.readAsDataURL(file);
    }
    return;
  }

  // 単一ファイル = ニューロン画像として処理
  const file = files[0];
  try {
    const { mainDeck, exDeck } = await extractCardsFromNeuronImage(file);
    addCardsToPool(mainDeck, false);
    addCardsToPool(exDeck, true);
    // ニューロン画像はDataURLのためimages辞書に登録（後でcard-managerから呼ばれる）
  } catch (error) {
    alert(error.message);
    console.error('ニューロン画像の処理エラー:', error);
  }
}

/**
 * 画像ペーストイベントハンドラ
 * @param {ClipboardEvent} event - pasteイベント
 */
export async function handleImagePaste(event) {
  const images = await extractImagesFromClipboard(event);
  images.forEach(dataURL => addCardToPool(dataURL, true));
}

/**
 * 盤面保存（画像）ボタンのイベントハンドラ
 */
export function handleSaveBoard() {
  const elementsToHide = [
    document.querySelector('.randomButton-container'),
    document.querySelector('.left-rectangle-container'),
    document.querySelector('#free-space'),
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
 * フォルダ選択ボタンのイベントハンドラ
 */
export async function handleFolderSelect() {
  try {
    const filteredImages = await selectFolderAndFilterImages();
    if (filteredImages.length === 0) {
      alert('適切なアスペクト比の画像が見つかりませんでした。');
      return;
    }
    const modal = new ImageBrowserModal(filteredImages);
    modal.show();
  } catch (error) {
    alert('フォルダ選択エラー: ' + error.message);
    console.error(error);
  }
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
  document.getElementById('imageUpload')
    ?.addEventListener('change', handleImageUpload);

  document.addEventListener('paste', handleImagePaste);

  document.getElementById('saveButton')
    ?.addEventListener('click', handleSaveBoard);

  document.getElementById('shareXButton')
    ?.addEventListener('click', handleShareX);

  document.getElementById('resetButton')
    ?.addEventListener('click', handleResetAndDraw);

  document.getElementById('randomButton')
    ?.addEventListener('click', handleDrawOne);

  document.getElementById('folderSelectButton')
    ?.addEventListener('click', handleFolderSelect);

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
