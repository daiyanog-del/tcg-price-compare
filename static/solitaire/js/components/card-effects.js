/**
 * card-effects.js
 * カード演出モジュール
 *
 * 効果発動アニメーション（.is-activating）と
 * リプレイ用FLIP移動アニメ（fixedクローン方式）を提供する。
 *
 * card-manager.js と replay-service.js の双方から参照するため
 * 独立モジュールとして切り出し、循環依存を回避している。
 */

/**
 * 効果発動アニメーションを再生
 * wrapper に .is-activating を付与し、animationend で自動除去する。
 * 連打対応: 一旦 remove → reflow 強制 → 再付与。
 * @param {Element} wrapper - .tier-item-wrapper
 */
export function playActivateEffect(wrapper) {
  if (!wrapper) return;
  // 連打対応: アニメを最初からやり直す
  wrapper.classList.remove('is-activating');
  void wrapper.offsetWidth; // reflow 強制（クラス除去を確定させる）
  wrapper.classList.add('is-activating');
  wrapper.addEventListener('animationend', () => {
    wrapper.classList.remove('is-activating');
  }, { once: true });
}

/**
 * FLIPアニメーション（fixedクローン方式）
 *
 * 本体（wrapper）は呼び出し前に既に移動先へ配置済みであること。
 * firstRect の位置から lastRect（現在の wrapper 位置）へ滑らかに動く
 * 見た目専用クローンを body 直下に生成してアニメさせる。
 * クローンは pointer-events:none のため操作に干渉しない。
 * DOM（真実のソース）は常に最終位置を保持し続ける。
 *
 * @param {Element} wrapper   - .tier-item-wrapper（既に移動先に配置済み）
 * @param {DOMRect} firstRect - 移動前の getBoundingClientRect() 値
 */
export function flipMoveClone(wrapper, firstRect) {
  if (!wrapper || !firstRect) return;

  const lastRect = wrapper.getBoundingClientRect();

  // 実質的な移動なし（2px 以内）はスキップ
  const dx = firstRect.left - lastRect.left;
  const dy = firstRect.top  - lastRect.top;
  if (Math.abs(dx) < 2 && Math.abs(dy) < 2) return;

  // クローン生成（class/data属性ごとコピー → 守備回転・is-set裏面も再現）
  const clone = wrapper.cloneNode(true);
  clone.classList.add('sol-flip-clone');

  // inline style を上書き: position:fixed で全スロットの overflow:hidden を突破
  clone.style.cssText = [
    'position: fixed',
    `left: ${firstRect.left}px`,
    `top: ${firstRect.top}px`,
    `width: ${firstRect.width}px`,
    `height: ${firstRect.height}px`,
    'margin: 0',
    'z-index: 9000',
    'pointer-events: none',
    'overflow: visible',
    'transition: none',
  ].join('; ');

  document.body.appendChild(clone);

  // 次フレームで transition を付与し最終位置へ移動
  requestAnimationFrame(() => {
    void clone.offsetWidth; // reflow 強制（initial 位置を確定させる）
    clone.style.transition = 'left 0.45s cubic-bezier(0.4,0,0.2,1), top 0.45s cubic-bezier(0.4,0,0.2,1)';
    clone.style.left = `${lastRect.left}px`;
    clone.style.top  = `${lastRect.top}px`;
  });

  // クリーンアップ: transitionend + setTimeout フェイルセーフ
  // タブ非アクティブ時など transitionend が発火しない場合でも確実に除去する
  const cleanup = () => {
    if (clone.parentNode) clone.parentNode.removeChild(clone);
  };
  clone.addEventListener('transitionend', cleanup, { once: true });
  setTimeout(cleanup, 600); // 自動再生インターバル (600ms) に合わせたフェイルセーフ
}
