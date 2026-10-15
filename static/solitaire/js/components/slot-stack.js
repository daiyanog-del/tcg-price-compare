/**
 * slot-stack.js
 * フィールドスロット（.custom-slot）の重ねカードの縦位置を詰め直す共通ヘルパ
 *
 * 他モジュールを import しない（drag-drop / replay-service / context-menu のどこからでも
 * 循環 import を起こさず使える）。
 */

/**
 * スロット直下の .tier-item-wrapper を DOM 順（先頭＝最背面）に並べ直す。
 * i 枚目の z-index = 1 + i、top = スロット幅 × (i / 10)（0 枚目は 0）。
 * position / z-index / top 以外（transform＝守備表示の回転など）には触れない。
 * @param {Element|null} slot - .custom-slot 要素
 */
export function restackCustomSlot(slot) {
  if (!slot || !slot.classList || !slot.classList.contains('custom-slot')) return;
  const items = slot.querySelectorAll(':scope > .tier-item-wrapper');
  items.forEach((item, i) => {
    item.style.position = 'absolute';
    item.style.zIndex   = String(1 + i);
    // 数値計算で作るので 10 枚以上でも単調に増える（i=10 で 1、i=11 で 1.1）
    item.style.top = i === 0 ? '0' : `calc(var(--slot-width) * ${i / 10})`;
  });
}
