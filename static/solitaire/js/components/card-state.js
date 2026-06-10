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
 *   種別    : data-card-type = "monster"|"spell"|"trap"（deck-input-panel が付与）
 *
 * セット時の挙動:
 *   モンスター（data-card-type="monster" または EXデッキカード）
 *     → 裏側守備表示 = .is-set + .is-defense（回転＋裏面）
 *   魔法・罠（data-card-type="spell"|"trap"）、または種別不明
 *     → 伏せ = .is-set のみ（縦のまま裏面）
 *
 * セット解除時:
 *   モンスターは .is-defense も同時に解除（フリップ後は攻撃表示へ）
 *
 * 裏面は CSS ::after で描画するため img.src は一切変更しない。
 */

/**
 * カードがモンスターかどうかを判定
 * @param {Element} wrapper - .tier-item-wrapper
 * @returns {boolean}
 */
export function isMonsterCard(wrapper) {
  // EXデッキカード（wrapper.id に "ex" を含む）は常にモンスター
  if (wrapper.id.includes(' ex')) return true;
  // data-card-type による判定（deck-input-panel が付与）
  const t = wrapper.dataset.cardType || '';
  return t === 'monster';
}

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
 * モンスターは裏側守備表示（.is-defense も同時に制御）、
 * 魔法・罠は伏せ（縦のまま）。
 * img.src は変更しない。.is-set クラスと CSS ::after で裏面を描画。
 * @param {Element} wrapper - .tier-item-wrapper
 * @param {boolean} on      - true=裏面, false=表面
 */
export function applySet(wrapper, on) {
  // img.tier-item または div.tier-item（プロキシ）どちらでも .tier-item で取れる
  const cardEl = wrapper.querySelector('.tier-item');
  if (on) {
    wrapper.classList.add('is-set');
    wrapper.dataset.face = 'down';
    // カーソルを合わせたときにカード名を表示（裏側でも名称確認できるように）
    if (cardEl) cardEl.title = cardEl.dataset.cardName || '？';
    // モンスターは守備表示も追加（裏側守備表示）
    if (isMonsterCard(wrapper)) {
      applyDefense(wrapper, true);
    }
  } else {
    wrapper.classList.remove('is-set');
    delete wrapper.dataset.face;
    if (cardEl) cardEl.title = '';
    // モンスターのセット解除は守備表示も同時に解除（攻撃表示へ）
    if (isMonsterCard(wrapper)) {
      applyDefense(wrapper, false);
    }
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
 * applySet を先に呼び、次に applyDefense を呼ぶことで
 * 表側守備表示のモンスターを正しく復元できる。
 *
 * applySet(false) はモンスターに対して applyDefense(false) の副作用を持つため、
 * applyDefense を先に呼ぶと表側守備表示が打ち消されてしまう。
 * applySet を先に実行して副作用を発生させた上で、
 * 最後に applyDefense で orientation の値どおりに最終確定させる。
 * @param {Element} wrapper - .tier-item-wrapper
 * @param {{ orientation?: string, face?: string }} [state]
 */
export function applyCardState(wrapper, state = {}) {
  const { orientation, face } = state;
  applySet(wrapper, face === 'down');               // 先にセット状態を確定（副作用で守備が変わってもよい）
  applyDefense(wrapper, orientation === 'defense'); // 最後に守備/攻撃を orientation の値で確定させる
}
