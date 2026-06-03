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
 * セット時の裏面は CSS ::after で描画するため img.src は一切変更しない。
 */

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
 * img.src は変更しない。.is-set クラスと data-face 属性のみで状態管理し、
 * CSS ::after で裏面を描画する。
 * @param {Element} wrapper - .tier-item-wrapper
 * @param {boolean} on      - true=裏面, false=表面
 */
export function applySet(wrapper, on) {
  if (on) {
    wrapper.classList.add('is-set');
    wrapper.dataset.face = 'down';
  } else {
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
 * @returns {{ orientation: string, face: string }}
 */
export function getCardState(wrapper) {
  return {
    orientation: wrapper.dataset.orientation || '',
    face:        wrapper.dataset.face        || '',
  };
}

/**
 * 保存した状態をカードに適用（復元用）
 * @param {Element} wrapper - .tier-item-wrapper
 * @param {{ orientation?: string, face?: string }} [state]
 */
export function applyCardState(wrapper, state = {}) {
  const { orientation, face } = state;
  applyDefense(wrapper, orientation === 'defense');
  applySet(wrapper, face === 'down');
}
