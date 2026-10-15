/**
 * token-src.js
 * 「画像の src がトークンのものか」の判定を1か所に置く小さな共通部品（副作用なし・他を import しない）。
 * card-state.js（DOM 側）と replay-board.js（盤面算出の純粋関数）の両方がこれを使う。
 *
 * トークンの画像は token-generator.js が canvas.toDataURL() で作る data: URL。
 * 実カードは https の URL、未発売カードは card:// センチネルかプロキシ（div）で、
 * data: URL の画像を作るのはトークン生成だけ。
 * @param {string|null|undefined} src
 * @returns {boolean}
 */
export function isTokenSrc(src) {
  return typeof src === 'string' && src.startsWith('data:');
}
