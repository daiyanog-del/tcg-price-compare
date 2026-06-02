/**
 * random-tools.js
 * コイントス・ダイスロール — フィールドオーバーレイアニメーション
 */

const DICE_FACES = ['⚀', '⚁', '⚂', '⚃', '⚄', '⚅'];
let _dismissTimer = null;

// ── 公開API ──────────────────────────────────────────

export function initRandomTools() {
  document.getElementById('coinTossBtn')?.addEventListener('click', () => {
    const result = Math.random() < 0.5 ? '表' : '裏';
    _showOverlay('coin', result);
  });

  document.getElementById('diceRollBtn')?.addEventListener('click', () => {
    const result = Math.floor(Math.random() * 6) + 1;
    _showOverlay('dice', result);
  });

  document.getElementById('randomOverlay')?.addEventListener('click', _dismiss);
}

// ── オーバーレイ制御 ──────────────────────────────────────────

function _showOverlay(type, result) {
  const overlay = document.getElementById('randomOverlay');
  if (!overlay) return;

  // 前のタイマーをキャンセル
  if (_dismissTimer) { clearTimeout(_dismissTimer); _dismissTimer = null; }

  const face  = document.getElementById('randomOverlayFace');
  const label = document.getElementById('randomOverlayLabel');

  // クラスをリセット
  face.className  = 'sol-random-overlay-face';
  label.className = 'sol-random-overlay-label';
  label.textContent = '';

  overlay.style.display = 'flex';
  overlay.classList.remove('sol-overlay-exit');

  if (type === 'coin') {
    _animateCoin(face, label, result);
  } else {
    _animateDice(face, label, result);
  }

  // 3.5秒後に自動消去
  _dismissTimer = setTimeout(_dismiss, 3500);
}

function _dismiss() {
  const overlay = document.getElementById('randomOverlay');
  if (!overlay || overlay.style.display === 'none') return;
  overlay.classList.add('sol-overlay-exit');
  setTimeout(() => {
    overlay.style.display = 'none';
    overlay.classList.remove('sol-overlay-exit');
  }, 350);
}

// ── コイントス ──────────────────────────────────────────

function _animateCoin(face, label, result) {
  face.classList.add('sol-coin-face', 'sol-coin-spin');
  face.textContent = '';

  setTimeout(() => {
    face.classList.remove('sol-coin-spin');
    face.textContent = result;
    face.classList.add('sol-coin-reveal');
    setTimeout(() => {
      label.textContent = result === '表' ? '表（おもて）' : '裏（うら）';
      label.classList.add('sol-label-reveal');
    }, 80);
  }, 700);
}

// ── ダイスロール ──────────────────────────────────────────

function _animateDice(face, label, finalResult) {
  face.classList.add('sol-dice-face');

  let step = 0;
  const totalSteps = 20;

  function tick() {
    const random = Math.floor(Math.random() * 6);
    face.textContent = DICE_FACES[random];
    step++;

    // ステップが進むにつれてインターバルを延ばしスローダウン
    const delay = step < 8  ? 50
                : step < 14 ? 80 + (step - 8) * 25
                : step < 18 ? 200 + (step - 14) * 50
                : 350;

    if (step < totalSteps) {
      setTimeout(tick, delay);
    } else {
      // 確定
      face.textContent = DICE_FACES[finalResult - 1];
      face.classList.add('sol-dice-reveal');
      setTimeout(() => {
        label.textContent = `${finalResult}`;
        label.classList.add('sol-label-reveal');
      }, 100);
    }
  }

  tick();
}
