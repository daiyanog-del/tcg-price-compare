/**
 * neuron-url-entry.js
 * index.html（非module）向け「URLから取り込む」橋渡しモジュール。
 * <script type="module" src="/static/shared/neuron-url-entry.js"> で読み込む。
 *
 * スマホは拡張が使えないため、ニューロンのURLを貼り付け／OS共有から受け取り、
 * サーバー解析(/api/import-neuron)→既存の確認モーダル→マイデッキ保存 を行う。
 *
 * 露出するAPI:
 *   window.NeuronURL.importFromInput()  … 入力欄の値で取り込み
 *   window.NeuronURL.import(url)        … 任意URLで取り込み
 */

import { parseNeuronUrl } from './neuron-url-parser.js';
import { NeuronPreviewModal } from './neuron-preview-modal.js';
import { saveImportedDeck } from './neuron-import-shared.js';

const INPUT_ID = 'neuronUrlInput';
const BTN_ID = 'neuronUrlBtn';

function _setBusy(busy) {
  const btn = document.getElementById(BTN_ID);
  if (btn) {
    btn.disabled = busy;
    btn.textContent = busy ? '取得中…' : '取り込む';
  }
}

async function _import(url) {
  url = (url || '').trim();
  if (!url) {
    alert('ニューロンのデッキ／カードのURLを貼り付けてください');
    return;
  }

  _setBusy(true);
  let parsed;
  try {
    parsed = await parseNeuronUrl(url);
  } catch (e) {
    alert(`取り込みに失敗しました。\n${e.message}`);
    return;
  } finally {
    _setBusy(false);
  }

  // カードURL: 相場ページへ遷移（拡張 content.js のモバイル再現）
  if (parsed.kind === 'card') {
    if (parsed.name) {
      location.href = '/card/' + encodeURIComponent(parsed.name);
    } else {
      alert('カード名を取得できませんでした');
    }
    return;
  }

  // デッキURL: 既存の確認モーダルで確認・編集 → マイデッキ保存（PDFと共通）
  new NeuronPreviewModal({
    parsed,
    defaultName: parsed.name || 'インポートデッキ',
    onSave: saveImportedDeck,
  }).show();

  // 入力欄をクリア
  const input = document.getElementById(INPUT_ID);
  if (input) input.value = '';
}

function _importFromInput() {
  _import(document.getElementById(INPUT_ID)?.value || '');
}

// 共有ターゲット等から ?import_url= 付きで開かれた場合、自動で取り込みを起動
function _handleAutoImport() {
  const params = new URLSearchParams(location.search);
  const url = params.get('import_url');
  if (!url) return;

  // URLからパラメータを除去（リロード／共有時の二重取り込み防止）
  params.delete('import_url');
  const qs = params.toString();
  history.replaceState(null, '', location.pathname + (qs ? '?' + qs : '') + location.hash);

  // マイデッキ画面へ切り替えてから取り込み（switchMode は index.html のグローバル）
  window.switchMode?.('mydeck');
  _import(url);
}

function _init() {
  window.NeuronURL = {
    import: _import,
    importFromInput: _importFromInput,
  };
  _handleAutoImport();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _init);
} else {
  _init();
}
