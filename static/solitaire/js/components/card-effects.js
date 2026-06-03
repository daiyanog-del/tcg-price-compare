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
 * 効果発動アニメーションを再生（fixedカードクローン方式）
 *
 * wrapper を cloneNode(true) して position:fixed で body 直下に生成し
 * scale + drop-shadow アニメを当てる。本体 DOM はそのまま。
 * fixed 要素は祖先の overflow:hidden に影響しないため scrollbar シフトが出ない。
 * 連打対応: 既存クローンは即座に除去してから新しいものを生成する。
 *
 * @param {Element} wrapper - .tier-item-wrapper
 */
export function playActivateEffect(wrapper) {
  if (!wrapper) return;
  const rect = wrapper.getBoundingClientRect();

  // 連打: 既存クローンを先に除去
  const prev = wrapper._activateGlow;
  if (prev && prev.parentNode) prev.parentNode.removeChild(prev);

  // カードの見た目ごとコピーした fixed クローン（守備回転・is-set裏面も再現）
  const clone = wrapper.cloneNode(true);
  clone.classList.add('sol-activate-glow');
  // inline style を上書きして fixed 配置に固定
  clone.style.cssText = [
    `left: ${rect.left}px`,
    `top: ${rect.top}px`,
    `width: ${rect.width}px`,
    `height: ${rect.height}px`,
    'margin: 0',
  ].join('; ');

  document.body.appendChild(clone);
  wrapper._activateGlow = clone;

  const cleanup = () => {
    if (clone.parentNode) clone.parentNode.removeChild(clone);
    if (wrapper._activateGlow === clone) delete wrapper._activateGlow;
  };
  clone.addEventListener('animationend', cleanup, { once: true });
  setTimeout(cleanup, 600); // フェイルセーフ
}

/**
 * セット/表向き切り替えアニメーション
 *
 * wrapper を scaleX=0 まで折り畳み（is-flipping-out）、
 * 不可視の瞬間に toggleFn() で状態変更してから展開する（is-flipping-in）。
 * アニメ中は pointer-events:none で誤操作を防止する。
 *
 * @param {Element}  wrapper    - .tier-item-wrapper
 * @param {Function} toggleFn  - scaleX=0 のタイミングで呼ぶ状態変更関数
 */
export function playSetFlip(wrapper, toggleFn) {
  if (!wrapper) return;
  const HALF_MS = 150; // solFlipOut の duration と一致させる

  wrapper.classList.add('is-flipping-out');
  setTimeout(() => {
    wrapper.classList.remove('is-flipping-out');
    toggleFn(); // カード不可視の瞬間に状態変更
    wrapper.classList.add('is-flipping-in');
    wrapper.addEventListener('animationend', () => {
      wrapper.classList.remove('is-flipping-in');
    }, { once: true });
  }, HALF_MS);
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
 * @param {Element}       wrapper    - .tier-item-wrapper（既に移動先に配置済み）
 * @param {DOMRect}       firstRect  - 移動前の getBoundingClientRect() 値
 * @param {Function|null} onComplete - アニメ完了（またはスキップ）後に呼ぶコールバック
 */
export function flipMoveClone(wrapper, firstRect, onComplete = null) {
  // 移動なし・引数不正の場合はコールバックだけ呼んで終了
  if (!wrapper || !firstRect) {
    if (onComplete) onComplete();
    return;
  }

  const lastRect = wrapper.getBoundingClientRect();

  // 実質的な移動なし（2px 以内）: アニメスキップ、コールバックは即時呼ぶ
  const dx = firstRect.left - lastRect.left;
  const dy = firstRect.top  - lastRect.top;
  if (Math.abs(dx) < 2 && Math.abs(dy) < 2) {
    if (onComplete) onComplete();
    return;
  }

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

  // アニメ中は本体を非表示（クローンと二重に見えないよう）
  wrapper.style.visibility = 'hidden';

  document.body.appendChild(clone);

  // 次フレームで transition を付与し最終位置へ移動
  requestAnimationFrame(() => {
    void clone.offsetWidth; // reflow 強制（initial 位置を確定させる）
    clone.style.transition = 'left 0.45s cubic-bezier(0.4,0,0.2,1), top 0.45s cubic-bezier(0.4,0,0.2,1)';
    clone.style.left = `${lastRect.left}px`;
    clone.style.top  = `${lastRect.top}px`;
  });

  // クリーンアップ: クローン除去・本体再表示・コールバック呼び出し
  // タブ非アクティブ時など transitionend が発火しない場合でも確実に処理する
  let _done = false;
  const cleanup = () => {
    if (clone.parentNode) clone.parentNode.removeChild(clone);
    wrapper.style.visibility = '';
    if (!_done && onComplete) { _done = true; onComplete(); }
  };
  clone.addEventListener('transitionend', cleanup, { once: true });
  setTimeout(cleanup, 600); // 自動再生インターバル (600ms) に合わせたフェイルセーフ
}
