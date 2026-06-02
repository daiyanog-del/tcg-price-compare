/**
 * random-tools.js
 * コイントス・ダイスロール
 */

const DICE_FACES = ['⚀', '⚁', '⚂', '⚃', '⚄', '⚅'];

function flash(el) {
  el.classList.remove('sol-random-flash');
  void el.offsetWidth; // reflow でアニメーションをリセット
  el.classList.add('sol-random-flash');
}

export function initRandomTools() {
  document.getElementById('coinTossBtn')?.addEventListener('click', () => {
    const result = Math.random() < 0.5 ? '表' : '裏';
    const el = document.getElementById('coinResult');
    if (el) { el.textContent = result; flash(el); }
  });

  document.getElementById('diceRollBtn')?.addEventListener('click', () => {
    const n = Math.floor(Math.random() * 6) + 1;
    const el = document.getElementById('diceResult');
    if (el) { el.textContent = DICE_FACES[n - 1]; flash(el); }
  });
}
