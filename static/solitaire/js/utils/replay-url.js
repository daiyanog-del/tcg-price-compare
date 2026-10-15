/**
 * utils/replay-url.js — 「URL にリプレイの指定が付いているか」の判定（唯一の定義）
 *
 * 共有リンク（?replay=<id> / #replay=<hash>）と、端末保存を開くリンク（?saved=<id>）のどれかが
 * 付いているときは、前回の盤面の復元（main.js）を止める。
 * ?at=end（最後の盤面から続ける）はリプレイの指定ではなく「読み込んだ後の位置」の修飾なので、
 * この判定には含めない（?saved= / ?replay= と組み合わせて付く）。読み取りは isReplayAtEnd。
 * 読み込み側（replay-ui.js の _tryLoadFromURL）が URL を書き換えても判定がぶれないよう、
 * main.js の initializeApp は initReplayUI() より前にこの関数を1回だけ評価して結果を使う。
 * @param {{search:string, hash:string}} [loc]
 * @returns {boolean}
 */
export function hasReplayInUrl(loc = location) {
  const params = new URLSearchParams(loc.search);
  return params.get('replay') !== null
    || params.get('saved') !== null
    || loc.hash.startsWith('#replay=');
}

/**
 * 「最後の盤面から続ける」（?at=end）の指定か。リプレイを読み込んだあと最後の手まで進めて、再生モードに入らず操作を続ける。
 * @param {{search:string}} [loc]
 * @returns {boolean}
 */
export function isReplayAtEnd(loc = location) {
  return new URLSearchParams(loc.search).get('at') === 'end';
}
