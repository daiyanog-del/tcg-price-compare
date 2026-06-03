/**
 * random-tools.js
 * コイントス・ダイスロール — フィールドオーバーレイアニメーション
 */

let _dismissTimer = null;

// ── ダイス定義 ──────────────────────────────────────────

// 各目を正面に向けるためのキューブ回転角（度）
// 面配置: front=1, back=6, right=3, left=4, top=2, bottom=5
const DICE_ROTATIONS = [
  null,
  { x: 0,   y: 0   }, // 1: front
  { x: -90, y: 0   }, // 2: top
  { x: 0,   y: -90 }, // 3: right
  { x: 0,   y: 90  }, // 4: left
  { x: 90,  y: 0   }, // 5: bottom
  { x: 0,   y: 180 }, // 6: back
];

// 3×3グリッド（1〜9）上のドット位置
const DOT_PATTERNS = {
  1: [5],
  2: [3, 7],
  3: [3, 5, 7],
  4: [1, 3, 7, 9],
  5: [1, 3, 5, 7, 9],
  6: [1, 3, 4, 6, 7, 9],
};

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

  if (_dismissTimer) { clearTimeout(_dismissTimer); _dismissTimer = null; }

  const face  = document.getElementById('randomOverlayFace');
  const label = document.getElementById('randomOverlayLabel');

  face.className   = 'sol-random-overlay-face';
  face.textContent = ''; // 子要素もクリア
  label.className  = 'sol-random-overlay-label';
  label.textContent = '';

  overlay.style.display = 'flex';
  overlay.classList.remove('sol-overlay-exit', 'sol-overlay-active');
  void overlay.offsetWidth;
  overlay.classList.add('sol-overlay-active');

  if (type === 'coin') {
    _animateCoin(face, label, result);
  } else {
    _animateDice(face, label, result);
  }

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

  setTimeout(() => {
    face.classList.remove('sol-coin-spin');
    face.textContent = result;
    face.classList.add('sol-coin-reveal');
  }, 700);
}

// ── ダイスロール（3Dキューブ） ──────────────────────────────────────────

function _buildDieCube() {
  const scene = document.createElement('div');
  scene.className = 'sol-dice-scene';

  const cube = document.createElement('div');
  cube.className = 'sol-dice-cube';

  const FACE_DEFS = [
    { cls: 'front',  val: 1 },
    { cls: 'back',   val: 6 },
    { cls: 'right',  val: 3 },
    { cls: 'left',   val: 4 },
    { cls: 'top',    val: 2 },
    { cls: 'bottom', val: 5 },
  ];

  for (const { cls, val } of FACE_DEFS) {
    const faceEl = document.createElement('div');
    faceEl.className = `sol-die-face sol-die-face--${cls}`;
    faceEl.dataset.val = val;
    const dots = DOT_PATTERNS[val];
    for (let i = 1; i <= 9; i++) {
      const cell = document.createElement('span');
      if (dots.includes(i)) cell.className = 'sol-die-dot';
      faceEl.appendChild(cell);
    }
    cube.appendChild(faceEl);
  }

  scene.appendChild(cube);
  return { scene, cube };
}

// 現在の累積角度から、目標面に前方向（増加方向）の最小回転で到達する角度を返す
function _nextAngle(current, target) {
  const cur360 = ((current % 360) + 360) % 360;
  const tgt360 = ((target  % 360) + 360) % 360;
  let delta = tgt360 - cur360;
  if (delta <= 0) delta += 360;
  return current + delta;
}

function _animateDice(face, label, finalResult) {
  const { scene, cube } = _buildDieCube();
  face.appendChild(scene);

  let rotX = -15, rotY = 20;
  let step = 0;
  const totalSteps = 12;

  // 初期位置をトランジションなしでセット
  cube.style.transition = 'none';
  cube.style.transform  = `rotateX(${rotX}deg) rotateY(${rotY}deg)`;

  function tick() {
    rotX += 80 + Math.random() * 100;
    rotY += 80 + Math.random() * 100;
    step++;

    const dur   = step <= 6  ? 0.04
                : step <= 10 ? 0.09 + (step - 6) * 0.03
                :              0.28;
    const delay = step <= 6  ? 45
                : step <= 10 ? 90 + (step - 6) * 35
                :              280;

    cube.style.transition = `transform ${dur}s ease`;
    cube.style.transform  = `rotateX(${rotX}deg) rotateY(${rotY}deg)`;

    if (step < totalSteps) {
      setTimeout(tick, delay);
    } else {
      // 前方向の最小回転で正しい面を向ける
      const rot     = DICE_ROTATIONS[finalResult];
      const finalRotX = _nextAngle(rotX, rot.x);
      const finalRotY = _nextAngle(rotY, rot.y);
      cube.style.transition = 'transform 0.55s cubic-bezier(0.25, 0.46, 0.45, 0.94)';
      cube.style.transform  = `rotateX(${finalRotX}deg) rotateY(${finalRotY}deg)`;
      setTimeout(() => {
        label.textContent = `${finalResult}`;
        label.classList.add('sol-label-reveal');
      }, 400);
    }
  }

  setTimeout(tick, 50);
}
