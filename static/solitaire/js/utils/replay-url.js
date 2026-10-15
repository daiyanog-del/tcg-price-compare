/**
 * utils/replay-url.js — 「URL にリプレイの指定が付いているか」の判定（唯一の定義）
 *
 * 共有リンク（?replay=<id> / #replay=<hash>）と、端末保存を開くリンク（?saved=<id>）のどれかが
 * 付いているときは、前回の盤面の復元（main.js）を止める。
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
