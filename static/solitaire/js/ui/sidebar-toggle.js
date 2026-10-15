/**
 * sidebar-toggle.js — 左サイドバー・右パネルの開閉制御
 *
 * 機能:
 *   - 開いている時: 各パネルの見出し行内のボタン (#sidebarToggleLeft=ロゴ行右端 /
 *     #sidebarToggleRight=「盤面操作」見出し行右端) で畳む
 *   - 畳んでいる時: 盤面レイアウトのフロー内に残る縦帯 (#solRailLeft / #solRailRight) 全体が開くボタン
 *     （盤面の上に fixed で浮かせない。カード詳細パネルとも重ならない）
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

import { isMobilePortrait, watchMobilePortrait } from '../utils/viewport.js';

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
 * パネルの開閉状態を DOM に反映し、見出し内ボタン・縦帯の表示と aria を更新する。
 * @param {'left'|'right'} side
 * @param {boolean} collapsed
 */
function applyCollapsed(side, collapsed) {
  const panelId = side === 'left' ? 'solSidebar' : 'solRightPanel';
  const btnId   = side === 'left' ? 'sidebarToggleLeft' : 'sidebarToggleRight';
  const railId  = side === 'left' ? 'solRailLeft' : 'solRailRight';
  const panel   = document.getElementById(panelId);
  const btn     = document.getElementById(btnId);
  const rail    = document.getElementById(railId);
  if (!panel || !btn) return;

  panel.classList.toggle('collapsed', collapsed);
  // 見出し内ボタンは開いている時だけ見える（畳むとパネルごと display:none になる）
  btn.setAttribute('aria-expanded', String(!collapsed));
  // 縦帯は畳んでいる時だけ出す（hidden 属性で出し分け。フロー内なので再計算は opp-tray-resize で行う）
  if (rail) {
    rail.hidden = !collapsed;
    rail.setAttribute('aria-expanded', String(!collapsed));
  }
}

/** 開閉確定後に fitFieldToViewport + updateCipWidth を再実行させる */
function triggerRefit() {
  window.dispatchEvent(new Event('opp-tray-resize'));
}

/**
 * スマホ縦向きでは開閉UI（見出し内ボタン・縦帯）を非表示、それ以外（PC・横向きスマホ）では既存表示に戻す。
 * 縦帯は hidden 属性で出し分けているため、ここでは inline の display:none だけを重ねる
 * （'' に戻せば hidden 属性による非表示がそのまま効く）。
 */
function applyMobileToggleVisibility() {
  const hide = isMobilePortrait();
  ['sidebarToggleLeft', 'sidebarToggleRight', 'solRailLeft', 'solRailRight'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = hide ? 'none' : '';
  });
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
  // 押したボタンは display:none になりフォーカスを失うため、入れ替わりで出た側へフォーカスを移す
  // （キーボード操作でも続けて開閉できるように）
  const nextFocusId = side === 'left'
    ? (nextCollapsed ? 'solRailLeft' : 'sidebarToggleLeft')
    : (nextCollapsed ? 'solRailRight' : 'sidebarToggleRight');
  document.getElementById(nextFocusId)?.focus();
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
  const railL = document.getElementById('solRailLeft');
  const railR = document.getElementById('solRailRight');
  if (btnL) btnL.addEventListener('click', () => togglePanel('left'));
  if (btnR) btnR.addEventListener('click', () => togglePanel('right'));
  if (railL) railL.addEventListener('click', () => togglePanel('left'));
  if (railR) railR.addEventListener('click', () => togglePanel('right'));

  // 初回表示時のみパルスアニメーションでボタンの存在を知らせる（見えているものだけが光る）
  if (!localStorage.getItem('sol-toggle-seen')) {
    setTimeout(() => {
      [btnL, btnR, railL, railR].filter(Boolean).forEach(btn => {
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

  // H-1: スマホ縦向き判定は matchMedia の change イベントに一本化（resize/orientationchange の
  // 逐次発火はやめる）。実際に portrait の該当/非該当が切り替わった瞬間だけ再評価する。
  applyMobileToggleVisibility();
  watchMobilePortrait(() => applyMobileToggleVisibility());
}
