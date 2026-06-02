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
  // exit クラスを除去してから active を付け直す（アニメーションを再トリガー）
  overlay.classList.remove('sol-overlay-exit', 'sol-overlay-active');
  void overlay.offsetWidth; // reflow でアニメーションをリセット
  overlay.classList.add('sol-overlay-active');

  if (type === 'coin') {
    _animateCoin(face, label, result);
  } else {
    _animateDice(face, label, result);
  }

  // 4秒後に自動消去（転がし~1.2s + 結果表示~2.8s）
  _dismissTimer = setTimeout(_dismiss, 4000);
}

function _dismiss() {
  const overlay = document.getElementById('randomOverlay');
  if (!overlay || overlay.style.display === 'none') return;
  overlay.classList.remove('sol-overlay-active');
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
  const totalSteps = 12; // 転がし時間: 約1.2s

  function tick() {
    const random = Math.floor(Math.random() * 6);
    face.textContent = DICE_FACES[random];
    step++;

    // 前半は高速、後半でスローダウン
    const delay = step < 6  ? 45                       // 0〜5: 高速 (270ms)
                : step < 10 ? 90 + (step - 6) * 35    // 6〜9: 減速 (90/125/160/195ms = 570ms)
                :             260;                     // 10〜11: 仕上げ (520ms)

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
