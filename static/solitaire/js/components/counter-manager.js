// カウンターの初期化関数
//
// リプレイへの記録（actionType: 'counterChange'）:
//   カードの上のカウンターは cardId（.tier-item の id。画像の無い代用カードでも取れる）と
//   index（そのカード上の .counter-container の並び順）で特定する。
//   op: 'add'（置く）/ 'set'（値を変える・省略時）/ 'remove'（外す）。
//   右パネルの基準カウンター（#parent）は cardId なし・index なしの 'set' のみ。
//   （#parent は 2026-10-20 に撤去。カードの右クリックメニュー「カウンターを置く」に置き換えた。
//     古い記録の 'set'（cardId なし）は再生側で何もしない）

/** 重複しない id を作るための連番（同じミリ秒に複数作っても id がぶつからないように） */
let _counterSeq = 0;

/**
 * カウンターが載っているカード（.tier-item-wrapper）を返す。右パネルの基準カウンターなら null。
 * @param {Element} container - .counter-container
 * @returns {Element|null}
 */
function _wrapperOf(container) {
  return container.closest('.tier-item-wrapper');
}

/**
 * カード上のカウンターの並び順（同じカードに複数置けるため、cardId だけでは特定できない）。
 * @param {Element} wrapper - .tier-item-wrapper
 * @param {Element} container - .counter-container
 * @returns {number}
 */
export function counterIndexOnCard(wrapper, container) {
  return Array.from(wrapper.querySelectorAll(':scope > .counter-container')).indexOf(container);
}

/**
 * カウンターの操作をリプレイへ記録する。
 * @param {Element} container - 対象の .counter-container
 * @param {'add'|'set'|'remove'} op
 * @param {number} value - 操作時点の値
 * @param {number} [indexOverride] - 取り外しの記録など、DOM から外す前の index を使いたいとき
 */
function _logCounter(container, op, value, indexOverride) {
  if (typeof window.replayLog !== 'function') return;
  const wrapper = _wrapperOf(container);
  if (!wrapper) {
    // 右パネルの基準カウンター（値の変更のみ記録する。従来どおり）
    if (op === 'set') window.replayLog({ actionType: 'counterChange', cardId: null, counter: value });
    return;
  }
  // img でも div（画像の無い代用カード）でも .tier-item で取れる
  const cardId = wrapper.querySelector('.tier-item')?.id ?? null;
  if (!cardId) return;
  const index = indexOverride ?? counterIndexOnCard(wrapper, container);
  window.replayLog({ actionType: 'counterChange', cardId, op, index, counter: value });
}

/** カードを離れる前のカウンターを「取り外し」として記録する（×・0から−・どちらからも使う） */
export function logCounterRemove(container) {
  const wrapper = _wrapperOf(container);
  if (!wrapper) return;
  const textbox = container.querySelector('.counter-textbox');
  _logCounter(container, 'remove', parseInt(textbox?.value, 10) || 0);
}

/** カードにカウンターを置いた直後に記録する（container は card に追加済みであること） */
export function logCounterAdd(container) {
  const textbox = container.querySelector('.counter-textbox');
  _logCounter(container, 'add', parseInt(textbox?.value, 10) || 0);
}

export function initializeCounter(container) {
  const upButton = container.querySelector(".triangle-button.up");
  const downButton = container.querySelector(".triangle-button.down");
  const textbox = container.querySelector(".counter-textbox");

  function logCounter() {
    _logCounter(container, 'set', parseInt(textbox.value, 10));
  }

  // ▲ボタンのクリックイベント
  upButton.addEventListener("click", () => {
    let value = parseInt(textbox.value, 10);
    textbox.value = value + 1;
    logCounter();
  });

  // ▼ボタンのクリックイベント
  downButton.addEventListener("click", () => {
    let value = parseInt(textbox.value, 10);
    if (value > 0) {
      textbox.value = value - 1;
      logCounter();
    } else {
      logCounterRemove(container); // DOM から外す前に index を確定して記録する
      container.remove(); // カウンターが負になったら削除
    }
  });
}

/**
 * カード上に置くカウンター（[−][N][+][×] のフラット構造）を作る。
 * ドラッグでの設置とリプレイ再生の両方がこの1つを使う（DOM の作りをずらさないため）。
 * 作っただけでは記録しない（記録は設置操作の側が logCounterAdd で行う）。
 * @param {string|number} value - 初期値
 * @returns {Element} .counter-container（カードへの追加は呼び出し側が行う）
 */
export function createCardCounter(value) {
  const newCounter = document.createElement('div');
  newCounter.className = 'counter-container';
  newCounter.id = `clonedCounter-${Date.now()}-${_counterSeq++}`;

  const downBtn = document.createElement('button');
  downBtn.className = 'triangle-button down';
  downBtn.textContent = '−';

  const textbox = document.createElement('input');
  textbox.type = 'text';
  textbox.className = 'counter-textbox';
  textbox.value = value;
  textbox.readOnly = true;

  const upBtn = document.createElement('button');
  upBtn.className = 'triangle-button up';
  upBtn.textContent = '+';

  newCounter.appendChild(downBtn);
  newCounter.appendChild(textbox);
  newCounter.appendChild(upBtn);

  initializeCounter(newCounter);
  addCounterDeleteButton(newCounter); // カード上のカウンターのみ×で即削除できるようにする
  return newCounter;
}

/**
 * カード上に配置されたカウンターへ削除(×)ボタンを追加する
 * 0まで減らさなくても任意のタイミングで外せるようにするための手段（プール側の
 * 基準カウンターは外す必要がないため、呼び出し側で対象を限定すること）
 * @param {Element} container - counter-container 要素
 */
export function addCounterDeleteButton(container) {
  if (container.querySelector('.counter-delete-btn')) return; // 二重追加防止

  const deleteBtn = document.createElement('button');
  deleteBtn.className = 'triangle-button counter-delete-btn';
  deleteBtn.textContent = '×';
  deleteBtn.setAttribute('aria-label', 'カウンターを削除');
  deleteBtn.addEventListener('click', (e) => {
    e.stopPropagation(); // ドラッグ開始やカードクリックへの伝播を防ぐ
    logCounterRemove(container); // DOM から外す前に index を確定して記録する
    container.remove();
  });

  container.appendChild(deleteBtn);
}

/**
 * カードにカウンターを1つ置き、リプレイへ記録する（右クリックメニュー「カウンターを置く」）。
 * @param {Element} wrapper - .tier-item-wrapper
 * @param {string|number} [value=1] - 初期値
 * @returns {Element} 置いた .counter-container
 */
export function addCounterToCard(wrapper, value = 1) {
  const counter = createCardCounter(value);
  wrapper.appendChild(counter);
  logCounterAdd(counter); // 置いたことをリプレイへ記録（cardId・index・初期値）
  return counter;
}
