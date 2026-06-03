/**
 * neuron-pdf-parser.js
 * ニューロン デッキシートPDFをパースしてカード一覧を返す
 * サーバー側(/api/parse-deck-pdf)にPDFを送信して解析結果を受け取る
 */

/**
 * ニューロン デッキシートPDFをパース
 * @param {File} file
 * @param {Object} [options]
 * @returns {Promise<{main:[{qty,name}], ex:[{qty,name}], side:[], warnings:string[], ok:boolean}>}
 */
export async function parseNeuronPdf(file, _options = {}) {
  const formData = new FormData();
  formData.append('file', file);

  let resp;
  try {
    resp = await fetch('/api/parse-deck-pdf', { method: 'POST', body: formData });
  } catch (e) {
    throw new Error(`サーバーへの接続に失敗しました。(${e.message})`);
  }

  let data;
  try {
    data = await resp.json();
  } catch {
    throw new Error(`サーバーから不正なレスポンスが返りました (HTTP ${resp.status})`);
  }

  if (!resp.ok) {
    throw new Error(data.error || `HTTP ${resp.status}`);
  }

  return {
    main:     data.main     || [],
    ex:       data.ex       || [],
    side:     [],
    warnings: data.warnings || [],
    ok:       data.ok       ?? false,
  };
}

/**
 * [{qty, name}] → "3 カード名\n" 形式のテキストに変換
 */
export function deckListToText(entries) {
  return entries.map(e => `${e.qty} ${e.name}`).join('\n');
}
