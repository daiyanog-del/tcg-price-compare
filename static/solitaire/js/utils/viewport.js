/**
 * viewport.js — スマホ縦向き判定の共通ユーティリティ
 *
 * CSS 側の @media (max-width: 767px) and (orientation: portrait) と完全に一致させる。
 * 以前は JS 側だけ「短辺 < 500px」という別基準で判定しており、500〜767px の縦長
 * （iPad mini 縦・PC でウィンドウを狭めた時）で CSS はモバイルレイアウトを適用するのに
 * JS は非該当と判定し、デッキ行もサイドバーも消えて下部バーも出ず操作不能になっていた
 * （2026-09-02 実走検証・reviewer監査で発見。司令塔決定A-1）。
 */

const MOBILE_PORTRAIT_QUERY = '(max-width: 767px) and (orientation: portrait)';
// A-3/Low-8: 「縦向き推奨」帯専用のクエリ。responsive.css の .sol-landscape-hint 用
// メディアクエリ（@media (orientation:landscape) and (max-height:499px) and (max-width:1023px)）と
// 完全に一致させること（両者がズレるとPC横長ウィンドウで帯が出てしまう・出ない不整合が起きる）。
// max-width:1023px は「1400×400のようなPCの横長ウィンドウ」を除外するために追加した
// （reviewer指摘 Low-8。max-height:499px単独だとPCでも高さを詰めれば該当してしまうため）。
const MOBILE_LANDSCAPE_QUERY = '(orientation: landscape) and (max-height: 499px) and (max-width: 1023px)';

/**
 * スマホ縦向き（CSS の @media (max-width:767px) and (orientation:portrait) と同一基準）か判定する。
 * @returns {boolean}
 */
export function isMobilePortrait() {
  return window.matchMedia(MOBILE_PORTRAIT_QUERY).matches;
}

/**
 * スマホ縦向きの該当/非該当が切り替わった瞬間に callback(matches) を呼ぶ（H-1）。
 * resize/orientationchange の逐次発火ではなく matchMedia の change イベントに一本化することで、
 * 実際に閾値を跨いだ時だけ発火させる（デバウンス不要・PC でのresize連打でも余計な処理が走らない）。
 * @param {(matches: boolean) => void} callback
 * @returns {MediaQueryList} 呼び出し側で保持不要なら無視してよい
 */
export function watchMobilePortrait(callback) {
  const mq = window.matchMedia(MOBILE_PORTRAIT_QUERY);
  mq.addEventListener('change', (ev) => callback(ev.matches));
  return mq;
}

/**
 * A-3: 横向きスマホ（CSS の @media (orientation:landscape) and (max-height:499px) と同一基準）か判定する。
 * @returns {boolean}
 */
export function isMobileLandscape() {
  return window.matchMedia(MOBILE_LANDSCAPE_QUERY).matches;
}

/**
 * A-3: 横向きスマホの該当/非該当が切り替わった瞬間に callback(matches) を呼ぶ。
 * @param {(matches: boolean) => void} callback
 * @returns {MediaQueryList}
 */
export function watchMobileLandscape(callback) {
  const mq = window.matchMedia(MOBILE_LANDSCAPE_QUERY);
  mq.addEventListener('change', (ev) => callback(ev.matches));
  return mq;
}
