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
