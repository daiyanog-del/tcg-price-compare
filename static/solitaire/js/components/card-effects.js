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

  // クローンの .tier-item: トランジションを抑制し最終状態に固定する。
  // ブラウザは DOM 挿入時にトランジションを 0° から再起動するため、
  // transition:none だけでは不十分なケースがある。transform も明示的に設定する。
  const cloneTierItem = clone.querySelector('.tier-item');
  if (cloneTierItem) {
    cloneTierItem.style.transition = 'none';
    if (clone.classList.contains('is-defense')) {
      cloneTierItem.style.transform = 'rotate(90deg)';
    } else {
      cloneTierItem.style.transform = '';
    }
  }

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

/**
 * タスクB: prefers-reduced-motion: reduce か（flyBetweenRects 専用で見る。
 * 既存の flipMoveClone / playActivateEffect / playSetFlip は対象外＝挙動を変えない）。
 */
export function prefersReducedMotion() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/**
 * タスクB: 明示的な出発点(fromRect)から到着点(toRect)へカードのクローンを飛ばす。
 *
 * flipMoveClone は到着点を常に wrapper の実際の位置（getBoundingClientRect）から取るが、
 * こちらは呼び出し側が渡した矩形をそのまま使う。縦向きスマホでは #poolRow（デッキ列）が
 * display:none のため実際の位置が0矩形になり演出が成立しない移動（replay-service.js の
 * moveCard 前進適用）で、デッキ演出の錨（#solMobileDeckBtn or #solMobilePlayDeckBadge）の
 * 矩形を代替の出発点/到着点として渡すために使う（現状の呼び出し元はこの用途のみ）。
 * クローン生成方法・sol-flip-clone クラスは flipMoveClone と同じ。
 * 第6次実機検収-2c: 所要時間を 300→450ms に延長し、出発時に少し拡大(scale 1.1)して
 * 到着で通常サイズ(1.0)へ収束させ、影を付けて出発点が目に留まるようにする
 * （sol-deck-fly-clone は .sol-flip-clone に追加する専用クラス。.sol-flip-clone 自体
 * ＝PC の flipMoveClone とも共有する共通クラスは変更しない）。
 * 実カード（cardEl）は飛行中 visibility:hidden にし、到着（cleanup）で元に戻す
 * （クローンと二重に見えないようにする。飛ばし始める直前に隠す）。
 * prefers-reduced-motion: reduce では演出せずコールバックだけ呼ぶ。
 *
 * @param {Element}       cardEl     - .tier-item-wrapper（DOM上の実体。移動先の最終配置は
 *                                     呼び出し側が onComplete 内で行う想定）
 * @param {DOMRect|null}  fromRect
 * @param {DOMRect|null}  toRect     - 省略時は cardEl の現在位置（flipMoveClone と同じ挙動）
 * @param {Function|null} onComplete
 */
export function flyBetweenRects(cardEl, fromRect, toRect, onComplete = null) {
  if (!cardEl || !fromRect || prefersReducedMotion()) {
    if (onComplete) onComplete();
    return;
  }

  const lastRect = toRect || cardEl.getBoundingClientRect();

  const dx = fromRect.left - lastRect.left;
  const dy = fromRect.top  - lastRect.top;
  if (Math.abs(dx) < 2 && Math.abs(dy) < 2) {
    if (onComplete) onComplete();
    return;
  }

  const clone = cardEl.cloneNode(true);
  clone.classList.add('sol-flip-clone', 'sol-deck-fly-clone');

  const cloneTierItem = clone.querySelector('.tier-item');
  if (cloneTierItem) {
    cloneTierItem.style.transition = 'none';
    if (clone.classList.contains('is-defense')) {
      cloneTierItem.style.transform = 'rotate(90deg)';
    } else {
      cloneTierItem.style.transform = '';
    }
  }

  clone.style.cssText = [
    'position: fixed',
    `left: ${fromRect.left}px`,
    `top: ${fromRect.top}px`,
    `width: ${fromRect.width}px`,
    `height: ${fromRect.height}px`,
    'margin: 0',
    'z-index: 9000',
    'pointer-events: none',
    'overflow: visible',
    'transition: none',
  ].join('; ');
  clone.style.transform = 'scale(1.1)'; // 出発時は少し拡大（第6次検収2c）

  // 実カードは飛ばし始める直前（クローンをbodyへ追加する直前）に隠す
  cardEl.style.visibility = 'hidden';
  document.body.appendChild(clone);

  requestAnimationFrame(() => {
    void clone.offsetWidth; // reflow 強制（initial 位置を確定させる）
    clone.style.transition = 'left 0.45s cubic-bezier(0.4,0,0.2,1), top 0.45s cubic-bezier(0.4,0,0.2,1), transform 0.45s cubic-bezier(0.4,0,0.2,1)';
    clone.style.left = `${lastRect.left}px`;
    clone.style.top  = `${lastRect.top}px`;
    clone.style.transform = 'scale(1.0)'; // 到着で通常サイズへ収束
  });

  let _done = false;
  const cleanup = () => {
    if (clone.parentNode) clone.parentNode.removeChild(clone);
    cardEl.style.visibility = '';
    if (!_done && onComplete) { _done = true; onComplete(); }
  };
  clone.addEventListener('transitionend', cleanup, { once: true });
  setTimeout(cleanup, 510); // 450ms + フェイルセーフ余裕
}
