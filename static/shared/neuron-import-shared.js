/**
 * neuron-import-shared.js
 * ニューロン取り込み（PDF / URL / 共有ターゲット）共通の「マイデッキ保存」処理。
 *
 * NeuronPreviewModal の onSave から呼ばれる想定。保存先は index.html の
 * グローバル関数（window.savedDecksGet/Set, renderSavedDecks, onDeckImported）。
 */

/**
 * モーダルが返す mainText/exText をマイデッキ（localStorage）へ保存する。
 * @param {{name:string, mainText:string, exText:string}} payload
 * @returns {boolean} 保存したら true、中断（未保存）なら false
 */
export function saveImportedDeck({ name, mainText, exText }) {
  // [EX]区切りを挿入してメイン/エクストラデッキを区別できるようにする
  const combined = exText ? mainText + '\n[EX]\n' + exText : mainText;
  const savedDecksGet = window.savedDecksGet;
  const savedDecksSet = window.savedDecksSet;
  const renderSavedDecks = window.renderSavedDecks;

  if (!savedDecksGet || !savedDecksSet) {
    alert('保存機能が利用できません。ページを再読み込みしてお試しください。');
    return false;
  }

  // main/ex を行パースして配列化（モーダルはテキスト形式で返すため）
  const parseLine = text => (text || '').split('\n').map(l => l.trim()).filter(Boolean).map(l => {
    const m = l.match(/^(\d+)\s+(.+)$/);
    return m ? { qty: parseInt(m[1]), name: m[2].trim() } : { qty: 1, name: l };
  });
  const main = parseLine(mainText);
  const ex   = parseLine(exText);

  const list = savedDecksGet();
  const existing = list.find(d => d.name === name);
  let savedDeck;
  if (existing) {
    if (!confirm(`「${name}」はすでに保存されています。上書きしますか？`)) return false;
    existing.text = combined;
    existing.main = main;
    existing.ex   = ex;
    existing.updated = Date.now();
    savedDeck = existing;
  } else {
    savedDeck = { id: 'd_' + Date.now(), name, text: combined, main, ex, updated: Date.now() };
    list.push(savedDeck);
  }
  savedDecksSet(list);
  renderSavedDecks?.();

  // textareaにも反映
  const ta = document.getElementById('deckTextarea');
  if (ta) ta.value = combined;

  alert(`「${name}」をマイデッキに保存しました`);

  // グリッドプレビューを起動（保存直後にデッキ内容を画像グリッドで表示）
  window.onDeckImported?.(savedDeck);
  return true;
}
