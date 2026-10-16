/**
 * cip-layout.js — カード詳細パネル(.sol-cip-col)の幅と上端を盤面に合わせる
 *
 * 2026-10-16: 相手の想定妨害トレイ（opponent-tray.js）を廃止し、カットインに一本化したため、
 * トレイのファイルに同居していたこの処理だけを移した（中身は変えていない）。
 */

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
