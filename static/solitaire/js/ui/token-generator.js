/**
 * token-generator.js
 * 盤面の空いたマスを右クリック → メニューで名前を入れて「出す」と、そのマスにトークンを出す
 *
 * 2026-10-20 右パネルの「トークン」欄（プレビュー画像をドラッグして盤面へ）から置き換え。
 * 2026-10-23 色つきの札（色4択）から、実物の羊トークンの画像を土台にした札へ。土台は
 *  YGOPRODeck のレプリカ画像（id 73915052）から名前・属性・レベル・テキスト欄・権利表記を消したもの
 *  （images/token-base.webp）。名前は帯の上に描く（2026-10-29 から描く処理は utils/token-image.js）。
 *
 * 仕組み:
 *  「出す」を押した時点で tokenDataURL から新しい .tier-item-wrapper を poolRow に作り、
 *  executeDrop で右クリックしたマスへ移す（ドラッグで置いたときと同じ配置・リプレイ記録の経路）。
 *  対象は .custom-slot の地（カードの上の右クリックはカードのメニューになる）。
 *  スマホ縦向きは右クリックの代わりに ⋯メニュー「トークンを出す」→ 名前のシート → 出すマスをタップ、
 *  または空いたマスの長押し（2026-10-23。シート・マス選び・長押しは mobile-ui.js。トークンを作るのはここの placeToken）。
 */

import { addCardToPool } from '../components/card-manager.js';
import { applyCardState } from '../components/card-state.js';
import { getDropZoneInfo, executeDrop } from '../components/drag-drop.js';
import { registerCardImage, registerCardName } from '../services/replay-service.js';
import { openContextMenuAt, closeContextMenu } from './context-menu.js';
import { isMobilePortrait } from '../utils/viewport.js';
import { dummyMenuItems } from './dummy-card-ui.js';
import { generateTokenDataURL, loadTokenBase, tokenNameOf, TOKEN_NAME_MAX, DEFAULT_TOKEN_NAME } from '../utils/token-image.js';

// 名前の整え方・上限・既定の名前は token-image.js に置き、ここからも出す（mobile-ui.js が import する）
export { tokenNameOf, TOKEN_NAME_MAX, DEFAULT_TOKEN_NAME };

// 前回の名前（同じトークンを続けて出すときに打ち直さなくてよいように。ページ内だけ覚える）
let _lastName = DEFAULT_TOKEN_NAME;
let _lastDefense = false; // 前回の表示形式（true＝守備表示）

/**
 * トークンを作ってマスに出す（ドラッグで置いたときと同じく executeDrop で配置・記録する）。
 * 名前・表示形式は次に出すときの既定として覚える。
 * @param {Element} slot - .custom-slot
 * @param {string} name - tokenNameOf で整えた名前
 * @param {{defense?: boolean}} [opt] defense: 守備表示で出す（2026-10-23。既定は攻撃表示）
 */
export async function placeToken(slot, name, { defense = false } = {}) {
  _lastName = name;
  _lastDefense = defense;
  const src = await generateTokenDataURL(name);
  if (!slot.isConnected) return; // 画像を作っている間に盤面が作り直された

  // poolRow に新カードを作り、その最後のカードを取る（従来のドラッグと同じ作り方）
  addCardToPool(src, false, false);
  const pool = document.getElementById('poolRow');
  const wrapper = pool?.querySelector('.tier-item-wrapper:last-child');
  const cardEl = wrapper?.querySelector('.tier-item');
  if (!cardEl?.id) return;
  registerCardImage(cardEl.id, src);
  // 名前（カード詳細・右クリックの見出し・共有リプレイの names に出る。保存・リプレイの復元でも戻る）
  cardEl.dataset.cardName = name;
  cardEl.alt = name;
  registerCardName(cardEl.id, name);

  const dropZoneInfo = getDropZoneInfo(slot);
  if (!dropZoneInfo) {
    console.warn('[token-generator] マスの種別を判定できませんでした');
    return;
  }
  // 形を先に当ててから置く（PC の「場に出す」と同じ。記録は形まで含めた1手になる）
  if (defense) applyCardState(wrapper, { orientation: 'defense', face: '' });
  executeDrop({ type: 'card', element: wrapper }, dropZoneInfo, slot, {});
}

/** 前回出したトークンの名前（名前欄の初期値） */
export function getLastTokenName() {
  return _lastName;
}

/** 前回は守備表示で出したか（表示形式の初期値） */
export function getLastTokenDefense() {
  return _lastDefense;
}

/** 土台の画像を先に読んでおく（名前を打っている間に。スマホのシートを開いたときにも呼ぶ） */
export function preloadTokenBase() {
  loadTokenBase();
}

/**
 * 空いたマスのメニューを開く: 見出し・名前欄・「攻撃表示で出す」「守備表示で出す」。
 * 名前欄で Enter を押すと前回の表示形式で出す。
 */
function openTokenMenu(slot, x, y) {
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'sol-context-menu-input';
  input.value = _lastName;
  input.maxLength = TOKEN_NAME_MAX;
  input.placeholder = 'トークン名';
  input.setAttribute('aria-label', 'トークン名');

  const place = (defense) => placeToken(slot, tokenNameOf(input.value), { defense });

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      place(_lastDefense);
      closeContextMenu();
    } else if (e.key === 'Escape') {
      closeContextMenu();
    }
  });

  openContextMenuAt([
    { heading: 'トークンを出す' },
    { element: input },
    { label: '攻撃表示で出す', sub: _lastDefense ? '' : 'Enter', action: () => place(false) },
    { label: '守備表示で出す', sub: _lastDefense ? 'Enter' : '', action: () => place(true) },
    // ダミーカード（2026-11-01・dummy-card-ui.js）: 素材など中身を問わない1枚
    { separator: true },
    { heading: 'ダミーカードを出す' },
    ...dummyMenuItems(slot),
  ], x, y);

  // すぐ名前を打ち直せるように（色を押すだけなら関係ない）
  input.focus();
  input.select();
}

export function initTokenGenerator() {
  const layout = document.querySelector('.main-content'); // 盤面と相手の場（.sol-opp-row・2026-10-29）の両方
  if (!layout) return;

  layout.addEventListener('contextmenu', (e) => {
    if (e.defaultPrevented) return;                     // カードのメニューなど、先に処理済み
    if (e.target.closest('.tier-item-wrapper')) return; // カードの上はカードのメニュー
    const slot = e.target.closest('.custom-slot');
    if (!slot) return;                                  // マスの間の隙間はブラウザのメニューのまま
    if (slot.getAttribute('data-slot') === '21') return; // 相手の墓地にはトークンを出さない（2026-10-29）
    if (isMobilePortrait()) return;
    e.preventDefault();
    loadTokenBase(); // 名前を打っている間に土台を読んでおく
    openTokenMenu(slot, e.clientX, e.clientY);
  });
}
