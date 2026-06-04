/**
 * sidebar-toggle.js — 左サイドバー・右パネルの開閉制御
 *
 * 機能:
 *   - 画面端固定トグルボタン (#sidebarToggleLeft / #sidebarToggleRight) で個別に開閉
 *   - matchMedia による画面幅での自動開閉
 *       >= 1280px: 左右とも自動展開
 *       1000–1279px: 右パネルのみ自動で畳む
 *       < 1000px: 左右とも自動で畳む
 *   - 手動操作 (override) を自動より優先
 *   - localStorage で override 状態を永続化 (キー: sol-panel-state)
 *
 * 開閉後は opp-tray-resize イベントを発火し、fitFieldToViewport（横幅含む）と
 * updateCipWidth を既存の仕組みで自動再計算させる。
 */

const STORAGE_KEY = 'sol-panel-state';

// ブレークポイント
const BP_BOTH  = 1000;  // < BP_BOTH  : 左右とも畳む
const BP_RIGHT = 1280;  // < BP_RIGHT : 右だけ畳む（左は開いたまま）

// override: null=自動 / true=ユーザーが開いた / false=ユーザーが閉じた
let state = { left: null, right: null };

/** localStorage から override 状態を読み込む */
function loadState() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
    if (saved.left  !== undefined) state.left  = saved.left;
    if (saved.right !== undefined) state.right = saved.right;
  } catch (_) { /* 無視 */ }
}

/** override 状態を localStorage に保存する */
function saveState() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch (_) { /* 無視 */ }
}

/**
 * 現在の画面幅から「自動値」を算出する。
 * @returns {{ left: boolean, right: boolean }} true=開く false=畳む
 */
function calcAutoState() {
  const w = window.innerWidth;
  if (w >= BP_RIGHT) return { left: false, right: false };
  if (w >= BP_BOTH)  return { left: false, right: true  }; // trueが「畳む」に対応するため反転: false=開く/true=畳む 注: 以下では collapsed=true を「畳む」として扱う
  return { left: true, right: true };
}

/**
 * パネルの実際の collapsed 状態を返す。
 * override があればそれを使い、なければ自動値を使う。
 * @param {'left'|'right'} side
 * @returns {boolean} true = 畳む
 */
function resolveCollapsed(side) {
  if (state[side] !== null) return state[side]; // override を優先
  return calcAutoState()[side];
}

/**
 * パネルの開閉状態を DOM に反映し、トグルボタンの位置・aria を更新する。
 * @param {'left'|'right'} side
 * @param {boolean} collapsed
 */
function applyCollapsed(side, collapsed) {
  const panelId = side === 'left' ? 'solSidebar' : 'solRightPanel';
  const btnId   = side === 'left' ? 'sidebarToggleLeft' : 'sidebarToggleRight';
  const panel   = document.getElementById(panelId);
  const btn     = document.getElementById(btnId);
  if (!panel || !btn) return;

  panel.classList.toggle('collapsed', collapsed);
  btn.setAttribute('aria-expanded', String(!collapsed));

  // トグルボタンの位置を更新（開時: パネル幅分ずらす / 閉時: 0px）
  const PANEL_W = 180;
  if (side === 'left') {
    btn.style.left = collapsed ? '0px' : `${PANEL_W}px`;
    btn.textContent = collapsed ? '▶' : '◀';
  } else {
    btn.style.right = collapsed ? '0px' : `${PANEL_W}px`;
    btn.textContent = collapsed ? '◀' : '▶';
  }
}

/** 開閉確定後に fitFieldToViewport + updateCipWidth を再実行させる */
function triggerRefit() {
  window.dispatchEvent(new Event('opp-tray-resize'));
}

/**
 * 手動トグル処理（ボタンクリック時）
 * @param {'left'|'right'} side
 */
function togglePanel(side) {
  const panelId = side === 'left' ? 'solSidebar' : 'solRightPanel';
  const panel   = document.getElementById(panelId);
  if (!panel) return;

  const nowCollapsed = panel.classList.contains('collapsed');
  const nextCollapsed = !nowCollapsed;

  // override を更新: 次の自動値と同じなら override をリセット(null)、違えば記録
  const autoCollapsed = calcAutoState()[side];
  state[side] = (nextCollapsed === autoCollapsed) ? null : nextCollapsed;
  saveState();

  applyCollapsed(side, nextCollapsed);
  triggerRefit();
}

/**
 * 画面幅変化に応じて自動開閉を適用する。
 * override が null のパネルだけ自動値を反映する。
 */
function applyAutoState() {
  const auto = calcAutoState();
  (['left', 'right']).forEach(side => {
    if (state[side] === null) {
      applyCollapsed(side, auto[side]);
    }
  });
  triggerRefit();
}

/**
 * サイドバー開閉を初期化する。main.js の initializeApp から呼ぶ。
 */
export function initSidebarToggle() {
  loadState();

  // トグルボタンのクリックハンドラ
  const btnL = document.getElementById('sidebarToggleLeft');
  const btnR = document.getElementById('sidebarToggleRight');
  if (btnL) btnL.addEventListener('click', () => togglePanel('left'));
  if (btnR) btnR.addEventListener('click', () => togglePanel('right'));

  // 初回表示時のみパルスアニメーションでボタンの存在を知らせる
  if (!localStorage.getItem('sol-toggle-seen')) {
    setTimeout(() => {
      [btnL, btnR].filter(Boolean).forEach(btn => {
        btn.classList.add('sol-panel-toggle-pulse');
        btn.addEventListener('animationend', () => {
          btn.classList.remove('sol-panel-toggle-pulse');
        }, { once: true });
      });
      localStorage.setItem('sol-toggle-seen', '1');
    }, 900);
  }

  // 初期状態を適用（override がなければ自動値を使う）
  (['left', 'right']).forEach(side => {
    applyCollapsed(side, resolveCollapsed(side));
  });

  // matchMedia で画面幅変化を監視し自動開閉
  const mqBoth  = window.matchMedia(`(max-width: ${BP_BOTH - 1}px)`);
  const mqRight = window.matchMedia(`(min-width: ${BP_BOTH}px) and (max-width: ${BP_RIGHT - 1}px)`);
  const mqWide  = window.matchMedia(`(min-width: ${BP_RIGHT}px)`);

  [mqBoth, mqRight, mqWide].forEach(mq => {
    mq.addEventListener('change', applyAutoState);
  });
}
