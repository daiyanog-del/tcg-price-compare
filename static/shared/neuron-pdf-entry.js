/**
 * neuron-pdf-entry.js
 * index.html（非module）向けのPDFインポート橋渡しモジュール
 * <script type="module" src="/static/shared/neuron-pdf-entry.js"> で読み込む
 *
 * 露出するAPI: window.NeuronPDF.openPicker()
 */

import { parseNeuronPdf } from './neuron-pdf-parser.js';
import { NeuronPreviewModal } from './neuron-preview-modal.js';

// 隠しfileInputを生成してdocumentに追加
function _createInput() {
  const existing = document.getElementById('neuronPdfUploadIndex');
  if (existing) return existing;
  const input = document.createElement('input');
  input.type = 'file';
  input.id = 'neuronPdfUploadIndex';
  input.accept = 'application/pdf';
  input.style.cssText = 'position:absolute;width:0;height:0;opacity:0;pointer-events:none';
  document.body.appendChild(input);
  input.addEventListener('change', async () => {
    const file = input.files[0];
    input.value = ''; // 同じファイルを再選択できるようリセット
    if (!file) return;
    await window.NeuronPDF.handleFile(file);
  });
  return input;
}

async function _handleFile(file) {
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
      // index.htmlのグローバル関数を呼ぶ
      const combined = [mainText, exText].filter(Boolean).join('\n');
      const savedDecksGet = window.savedDecksGet;
      const savedDecksSet = window.savedDecksSet;
      const renderSavedDecks = window.renderSavedDecks;

      if (!savedDecksGet || !savedDecksSet) {
        alert('保存機能が利用できません。ページを再読み込みしてお試しください。');
        return;
      }

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
      renderSavedDecks?.();

      // textareaにも反映
      const ta = document.getElementById('deckTextarea');
      if (ta) ta.value = combined;

      alert(`「${name}」をマイデッキに保存しました`);
    },
  }).show();
}

// DOMContentLoaded後に初期化
function _init() {
  _createInput();
  window.NeuronPDF = {
    openPicker() {
      document.getElementById('neuronPdfUploadIndex')?.click();
    },
    handleFile: _handleFile,
  };
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _init);
} else {
  _init();
}
