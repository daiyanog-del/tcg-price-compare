/**
 * card-state.js
 * カードの状態（守備表示・セット）を管理するヘルパ
 *
 * 状態はdata-*属性＋CSSクラスで保持する。
 * style='' で全スタイルが消えても data-属性/class は残るため、
 * returnAllCardsToDeck などの影響を受けない。
 *
 * 状態スキーマ（wrapper = .tier-item-wrapper に付与）:
 *   守備表示: data-orientation="defense" + .is-defense
 *   セット  : data-face="down"           + .is-set
 *
 * セット時のsrc管理:
 *   裏にする → img.dataset.faceSrc = 元src; img.src = FACE_DOWN_SRC
 *   表にする → img.src = img.dataset.faceSrc ?? img.src; delete dataset.faceSrc
 *   ※ getCardState で faceSrc を保存し applyCardState で復元するため、
 *     セーブ/ロード後も正しく元画像に戻せる。
 */

const FACE_DOWN_SRC = '/static/solitaire/images/blanck.png';

/**
 * 守備表示をセット/解除
 * @param {Element} wrapper - .tier-item-wrapper
 * @param {boolean} on      - true=守備表示, false=攻撃表示
 */
export function applyDefense(wrapper, on) {
  if (on) {
    wrapper.classList.add('is-defense');
    wrapper.dataset.orientation = 'defense';
  } else {
    wrapper.classList.remove('is-defense');
    delete wrapper.dataset.orientation;
  }
}

/**
 * 守備/攻撃表示をトグル
 * @param {Element} wrapper - .tier-item-wrapper
 */
export function toggleDefense(wrapper) {
  applyDefense(wrapper, wrapper.dataset.orientation !== 'defense');
}

/**
 * セット（裏側表示）をセット/解除
 * @param {Element} wrapper - .tier-item-wrapper
 * @param {boolean} on      - true=裏面, false=表面
 */
export function applySet(wrapper, on) {
  const img = wrapper.querySelector('img.tier-item');
  if (!img) return;

  if (on) {
    // 裏にする: すでに裏なら二重処理しない
    if (wrapper.dataset.face !== 'down') {
      img.dataset.faceSrc = img.src;
      img.src = FACE_DOWN_SRC;
    }
    wrapper.classList.add('is-set');
    wrapper.dataset.face = 'down';
  } else {
    // 表にする: dataset.faceSrc から元srcを復元
    const origSrc = img.dataset.faceSrc ?? img.src;
    img.src = origSrc;
    delete img.dataset.faceSrc;
    wrapper.classList.remove('is-set');
    delete wrapper.dataset.face;
  }
}

/**
 * セット状態をトグル
 * @param {Element} wrapper - .tier-item-wrapper
 */
export function toggleSet(wrapper) {
  applySet(wrapper, wrapper.dataset.face !== 'down');
}

/**
 * カードの状態を取得（保存/リプレイ記録用）
 * @param {Element} wrapper - .tier-item-wrapper
 * @returns {{ orientation: string, face: string, faceSrc: string }}
 */
export function getCardState(wrapper) {
  const img = wrapper.querySelector('img.tier-item');
  return {
    orientation: wrapper.dataset.orientation || '',
    face:        wrapper.dataset.face        || '',
    faceSrc:     img?.dataset.faceSrc        || '',
  };
}

/**
 * 保存した状態をカードに適用（復元用）
 * face=down のカードは、呼び出し前に img.src が元画像になっていること。
 * @param {Element} wrapper - .tier-item-wrapper
 * @param {{ orientation?: string, face?: string, faceSrc?: string }} [state]
 */
export function applyCardState(wrapper, state = {}) {
  const { orientation, face, faceSrc } = state;
  const img = wrapper.querySelector('img.tier-item');

  // 守備表示
  applyDefense(wrapper, orientation === 'defense');

  // セット
  if (face === 'down') {
    // faceSrc があれば dataset.faceSrc に先に設定（applySet 内の上書き前に）
    if (faceSrc && img && !img.dataset.faceSrc) {
      img.dataset.faceSrc = faceSrc;
    }
    applySet(wrapper, true);
  } else {
    applySet(wrapper, false);
  }
}
