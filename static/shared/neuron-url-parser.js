/**
 * neuron-url-parser.js
 * ニューロンのデッキ/カードURLをサーバー(/api/import-neuron)で解析して受け取る。
 *
 * スマホは拡張が使えないため、拡張のDOM抽出に代わりURLをサーバーに渡す。
 * neuron-pdf-parser.js と同型のインターフェース。
 */

/**
 * ニューロンURLを解析する。
 * @param {string} url ニューロンのデッキ or カードURL
 * @returns {Promise<{kind:'deck'|'card', name:string, main?:[{qty,name}], ex?:[{qty,name}], side?:[{qty,name}], warnings?:string[], ok:boolean}>}
 * @throws サーバー接続失敗・解析失敗時
 */
export async function parseNeuronUrl(url) {
  let resp;
  try {
    resp = await fetch('/api/import-neuron', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
  } catch (e) {
    throw new Error(`サーバーへの接続に失敗しました。(${e.message})`);
  }

  let data;
  try {
    data = await resp.json();
  } catch {
    throw new Error(`サーバーから不正なレスポンスが返りました (HTTP ${resp.status})`);
  }

  // ok=false はサーバーが理由付きで返す（URL不正・取得失敗・解析0件など）
  if (!resp.ok || !data.ok) {
    throw new Error(data.error || `HTTP ${resp.status}`);
  }

  if (data.kind === 'card') {
    return { kind: 'card', name: data.name || '', ok: true };
  }
  return {
    kind:     'deck',
    name:     data.name     || 'インポートデッキ',
    main:     data.main     || [],
    ex:       data.ex       || [],
    side:     data.side     || [],
    warnings: data.warnings || [],
    ok:       true,
  };
}
