/**
 * side-tips.js — 右パネル（リセット・クリア）の吹き出しをパネルの左に出す（2026-10-21・ユーザー承認の案A）
 *
 * 吹き出しは CSS（solitaire-brand.css の .sol-tip）で :hover / :focus-visible のときに出す。
 * 以前はリセットの吹き出しをボタンの上に出していたが、右パネルは overflow-y:auto で、
 * パネルの上端（画面の上端）から外へ出た部分が切れていた（1280x720 等で上 56px が常に切れていた）。
 * ここでは吹き出しを position:fixed（.sol-tip-side）にして、出すたびにボタンの左・縦の中央へ置く。
 * fixed なのでパネルのスクロール・切り取りに左右されない。下に出すとクリアのボタンに重なるため左にした。
 * 読み込めなかったときは CSS の既定（上・下に出す）のまま。
 */

const GAP = 10;     // ボタンと吹き出しの間
const MARGIN = 8;   // 画面の端との最小の隙間

function place(btn, tip) {
  const r = btn.getBoundingClientRect();
  const w = tip.offsetWidth;
  const h = tip.offsetHeight;
  tip.style.left = Math.max(MARGIN, r.left - w - GAP) + 'px';
  const top = r.top + r.height / 2 - h / 2;
  tip.style.top = Math.max(MARGIN, Math.min(window.innerHeight - h - MARGIN, top)) + 'px';
}

export function initSideTips() {
  for (const wrap of document.querySelectorAll('#solRightPanel .sol-tip-wrap')) {
    const btn = wrap.querySelector(':scope > button');
    const tip = wrap.querySelector(':scope > .sol-tip');
    if (!btn || !tip) continue;
    tip.classList.add('sol-tip-side');
    // 出す直前に置く（パネルのスクロール・開閉・画面の大きさで位置が変わるため、毎回測る）
    const show = () => place(btn, tip);
    btn.addEventListener('mouseenter', show);
    btn.addEventListener('focus', show);
  }
}
