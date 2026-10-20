/**
 * token-generator.js
 * 盤面の空いたマスを右クリック → メニューで名前と色を選ぶと、そのマスにトークンを出す
 *
 * 2026-10-20 右パネルの「トークン」欄（プレビュー画像をドラッグして盤面へ）から置き換え。
 *
 * 仕組み:
 *  色を選んだ時点で tokenDataURL から新しい .tier-item-wrapper を poolRow に作り、
 *  executeDrop で右クリックしたマスへ移す（ドラッグで置いたときと同じ配置・リプレイ記録の経路）。
 *  対象は .custom-slot の地（カードの上の右クリックはカードのメニューになる）。
 *  スマホ縦向きは対象外（右パネルの欄もスマホでは出していなかった）。
 */

import { addCardToPool } from '../components/card-manager.js';
import { getDropZoneInfo, executeDrop } from '../components/drag-drop.js';
import { registerCardImage } from '../services/replay-service.js';
import { openContextMenuAt, closeContextMenu } from './context-menu.js';
import { isMobilePortrait } from '../utils/viewport.js';

const CARD_W = 106;
const CARD_H = 154;
const NAME_MAX = 12;
const DEFAULT_NAME = 'トークン';

const TOKEN_COLORS = [
  { label: '緑', value: '#2d6040' },
  { label: '金', value: '#7a6020' },
  { label: '青', value: '#1a3d6b' },
  { label: '赤', value: '#6b1a2a' },
];

// 前回の名前・色（同じトークンを続けて出すときに打ち直さなくてよいように。ページ内だけ覚える）
let _lastName  = DEFAULT_NAME;
let _lastColor = TOKEN_COLORS[0].value;

function generateTokenDataURL(name, color) {
  const canvas = document.createElement('canvas');
  canvas.width  = CARD_W;
  canvas.height = CARD_H;
  const ctx = canvas.getContext('2d');

  // 背景
  ctx.fillStyle = color;
  _roundRect(ctx, 0, 0, CARD_W, CARD_H, 6, true, false);

  // 内枠
  ctx.strokeStyle = 'rgba(255,255,255,0.22)';
  ctx.lineWidth = 1.5;
  _roundRect(ctx, 3, 3, CARD_W - 6, CARD_H - 6, 5, false, true);

  // 薄文字「TOKEN」
  ctx.save();
  ctx.globalAlpha = 0.12;
  ctx.fillStyle = '#fff';
  ctx.font = 'bold 24px sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText('TOKEN', CARD_W / 2, CARD_H / 2 - 8);
  ctx.restore();

  // カード名
  ctx.fillStyle = '#fff';
  ctx.font = 'bold 9px "Noto Sans JP", sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  const displayName = name.length > 10 ? name.slice(0, 9) + '…' : name;
  ctx.fillText(displayName, CARD_W / 2, CARD_H / 2 + 14);

  // ATK / DEF
  ctx.fillStyle = 'rgba(255,255,255,0.6)';
  ctx.font = '7px monospace';
  ctx.fillText('ATK /    0', CARD_W / 2, CARD_H - 20);
  ctx.fillText('DEF /    0', CARD_W / 2, CARD_H - 10);

  return canvas.toDataURL();
}

function _roundRect(ctx, x, y, w, h, r, fill, stroke) {
  ctx.beginPath();
  if (ctx.roundRect) {
    ctx.roundRect(x, y, w, h, r);
  } else {
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.arcTo(x + w, y, x + w, y + r, r);
    ctx.lineTo(x + w, y + h - r);
    ctx.arcTo(x + w, y + h, x + w - r, y + h, r);
    ctx.lineTo(x + r, y + h);
    ctx.arcTo(x, y + h, x, y + h - r, r);
    ctx.lineTo(x, y + r);
    ctx.arcTo(x, y, x + r, y, r);
    ctx.closePath();
  }
  if (fill)   ctx.fill();
  if (stroke) ctx.stroke();
}

/**
 * トークンを作ってマスに出す（ドラッグで置いたときと同じく executeDrop で配置・記録する）。
 * @param {Element} slot - .custom-slot
 * @param {string} name
 * @param {string} color
 */
function placeToken(slot, name, color) {
  if (!slot.isConnected) return; // メニューを開いている間に盤面が作り直された
  const src = generateTokenDataURL(name, color);

  // poolRow に新カードを作り、その最後のカードを取る（従来のドラッグと同じ作り方）
  addCardToPool(src, false, false);
  const pool = document.getElementById('poolRow');
  const wrapper = pool?.querySelector('.tier-item-wrapper:last-child');
  const cardEl = wrapper?.querySelector('.tier-item');
  if (!cardEl?.id) return;
  registerCardImage(cardEl.id, src);

  const dropZoneInfo = getDropZoneInfo(slot);
  if (!dropZoneInfo) {
    console.warn('[token-generator] マスの種別を判定できませんでした');
    return;
  }
  executeDrop({ type: 'card', element: wrapper }, dropZoneInfo, slot, {});
}

/** メニューの名前欄の値（空なら既定の名前） */
function _nameOf(input) {
  return input.value.trim().slice(0, NAME_MAX) || DEFAULT_NAME;
}

/**
 * 空いたマスのメニューを開く: 見出し・名前欄・色4つ（押すとその色で出す）。
 * 名前欄で Enter を押すと前回の色で出す。
 */
function openTokenMenu(slot, x, y) {
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'sol-context-menu-input';
  input.value = _lastName;
  input.maxLength = NAME_MAX;
  input.placeholder = 'トークン名';
  input.setAttribute('aria-label', 'トークン名');

  const place = (color) => {
    _lastName  = _nameOf(input);
    _lastColor = color;
    placeToken(slot, _lastName, color);
  };

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      place(_lastColor);
      closeContextMenu();
    } else if (e.key === 'Escape') {
      closeContextMenu();
    }
  });

  openContextMenuAt([
    { heading: 'トークンを出す' },
    { element: input },
    ...TOKEN_COLORS.map(({ label, value }) => ({
      label,
      swatch: value,
      sub: value === _lastColor ? '前回' : '',
      action: () => place(value),
    })),
  ], x, y);

  // すぐ名前を打ち直せるように（色を押すだけなら関係ない）
  input.focus();
  input.select();
}

export function initTokenGenerator() {
  const layout = document.querySelector('.custom-layout');
  if (!layout) return;

  layout.addEventListener('contextmenu', (e) => {
    if (e.defaultPrevented) return;                     // カードのメニューなど、先に処理済み
    if (e.target.closest('.tier-item-wrapper')) return; // カードの上はカードのメニュー
    const slot = e.target.closest('.custom-slot');
    if (!slot) return;                                  // マスの間の隙間はブラウザのメニューのまま
    if (isMobilePortrait()) return;
    e.preventDefault();
    openTokenMenu(slot, e.clientX, e.clientY);
  });
}
