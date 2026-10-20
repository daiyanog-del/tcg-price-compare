/**
 * cip-layout.js — カード詳細パネル(.sol-cip-col)の幅と上端を盤面に合わせる
 *
 * 2026-10-16: 相手の想定妨害トレイ（opponent-tray.js）を廃止し、カットインに一本化したため、
 * トレイのファイルに同居していたこの処理だけを移した（中身は変えていない）。
 */

/**
 * 盤面の左の隙間がこれより狭い画面では、詳細パネルを「重ねて広げる」表示にする（2026-10-20 ユーザー承認の案A）。
 * 1920×1080（隙間 249px）は今のまま読めるので重ねない。1536×864（160px）以下を対象にする。
 * 隙間が狭いと文字が1行数文字しか入らず読めないため（1440×900 で 112px・1920×1080 で 249px を実測）。
 * 広げたときの幅（300px）は CSS の .sol-cip-col.is-expanded。
 */
const CIP_OVERLAY_BELOW = 240;

/**
 * カード詳細パネルの幅を制御する CSS 変数 --cip-width を更新する。
 *
 * .custom-layout（ゲームグリッド）の左端を実測し、
 * 盤面に被らない最大幅を常に px でセットする。
 *
 * ※ レイアウト確定後（requestAnimationFrame 内）に呼ぶこと。
 */
function _updateCipWidth() {
  const fieldArea    = document.querySelector('.sol-field-area');
  const customLayout = document.querySelector('.custom-layout');
  if (!fieldArea || !customLayout) return;

  const faLeft = fieldArea.getBoundingClientRect().left;
  const clLeft = customLayout.getBoundingClientRect().left;

  // 盤面左端 − フィールド左端 − パネル開始位置(10px) − cipToggle幅+余白(28px)
  const maxW = Math.floor(clLeft - faLeft - 38);
  if (maxW > 0) {
    document.documentElement.style.setProperty('--cip-width', `${maxW}px`);
  }
  // 狭い画面: 普段は隙間に収まる小さい表示、カードを選んだときだけ盤面の上へ広げる（card-info-panel.js）
  const col = document.querySelector('.sol-cip-col');
  if (col) {
    const overlay = maxW < CIP_OVERLAY_BELOW;
    col.toggleAttribute('data-overlay', overlay);
    if (!overlay) setCipExpanded(false);
  }
}

/**
 * 狭い画面で詳細パネルを盤面の上へ広げる／隙間に戻す。広い画面（data-overlay なし）では何もしない。
 * @param {boolean} on
 */
export function setCipExpanded(on) {
  const col = document.querySelector('.sol-cip-col');
  const area = document.querySelector('.sol-field-area');
  if (!col) return;
  const expand = !!on && col.hasAttribute('data-overlay') && !col.classList.contains('hidden');
  col.classList.toggle('is-expanded', expand);
  area?.classList.toggle('cip-expanded', expand);
}

/** main.js から呼べるよう export */
export function updateCipWidth() {
  _updateCipWidth();
  // 盤面の上の要素の高さが変わると #mainContainer が上下するので、詳細パネルの上端も合わせ直す
  syncCipPosition();
}

/**
 * カード詳細パネル(.sol-cip-col)と cipToggle の top を
 * #mainContainer の上端に動的に合わせる。
 */
export function syncCipPosition() {
  requestAnimationFrame(() => {
    const fieldArea = document.querySelector('.sol-field-area');
    const mainContainer = document.getElementById('mainContainer');
    const cipCol = document.querySelector('.sol-cip-col');
    const cipToggle = document.getElementById('cipToggle');
    if (!fieldArea || !mainContainer || !cipCol || !cipToggle) return;

    const faRect = fieldArea.getBoundingClientRect();
    const mcRect = mainContainer.getBoundingClientRect();
    // #mainContainer 上端からさらに 4px 下（既存の padding-top 相当）
    const topPx = Math.round(mcRect.top - faRect.top) + 4;

    cipCol.style.top = `${topPx}px`;
    cipToggle.style.top = `${topPx}px`;
  });
}
