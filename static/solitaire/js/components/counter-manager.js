// カウンターの初期化関数
export function initializeCounter(container) {
  const upButton = container.querySelector(".triangle-button.up");
  const downButton = container.querySelector(".triangle-button.down");
  const textbox = container.querySelector(".counter-textbox");

  function logCounter() {
    if (typeof window.replayLog !== 'function') return;
    const cardId = container.closest('.tier-item-wrapper')?.querySelector('img')?.id ?? null;
    window.replayLog({
      actionType: 'counterChange',
      cardId,
      counter: parseInt(textbox.value, 10),
    });
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
      container.remove(); // カウンターが負になったら削除
    }
  });
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
    container.remove();
  });

  container.appendChild(deleteBtn);
}
