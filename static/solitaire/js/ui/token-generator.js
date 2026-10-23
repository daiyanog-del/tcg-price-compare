/**
 * token-generator.js
 * 盤面の空いたマスを右クリック → メニューで名前を入れて「出す」と、そのマスにトークンを出す
 *
 * 2026-10-20 右パネルの「トークン」欄（プレビュー画像をドラッグして盤面へ）から置き換え。
 * 2026-10-23 色つきの札（色4択）から、実物の羊トークンの画像を土台にした札へ。土台は
 *  YGOPRODeck のレプリカ画像（id 73915052）から名前・属性・レベル・テキスト欄・権利表記を消したもの
 *  （images/token-base.webp）。名前は帯の上にここで描く。
 *
 * 仕組み:
 *  「出す」を押した時点で tokenDataURL から新しい .tier-item-wrapper を poolRow に作り、
 *  executeDrop で右クリックしたマスへ移す（ドラッグで置いたときと同じ配置・リプレイ記録の経路）。
 *  対象は .custom-slot の地（カードの上の右クリックはカードのメニューになる）。
 *  スマホ縦向きは対象外（右パネルの欄もスマホでは出していなかった）。
 */

import { addCardToPool } from '../components/card-manager.js';
import { getDropZoneInfo, executeDrop } from '../components/drag-drop.js';
import { registerCardImage } from '../services/replay-service.js';
import { openContextMenuAt, closeContextMenu } from './context-menu.js';
import { isMobilePortrait } from '../utils/viewport.js';

// 出す画像の大きさ（盤面のカード 106×154 の1.5倍。トークンは1枚ずつ data: URL でリプレイに入るので、
// 見た目と容量の釣り合いで決めた。2倍・画質0.85 だと1枚 約19KB、これで約10KB。保存の上限は1件300KB）
const CARD_W = 159;
const CARD_H = 231;
const JPEG_QUALITY = 0.75;
const NAME_MAX = 12;
const DEFAULT_NAME = 'トークン';

// 土台の画像（?v= は差し替えたときに上げる）
const BASE_URL = new URL('../../images/token-base.webp?v=1', import.meta.url).href;
// 名前の帯の中（土台の幅を 1 としたときの位置。元画像 813×1185 の帯の中 x 50〜700・y 48〜128 から）
const NAME_X = 62 / 813;
const NAME_Y = 90 / 1185;
const NAME_MAX_W = 640 / 813;
const NAME_SIZE = 62 / 813;
const NAME_FONT = '"Noto Sans JP", "Hiragino Sans", "Yu Gothic", sans-serif';
// 土台が読めなかったときの地の色（土台の枠の灰色）
const FALLBACK_BG = '#a9adb5';

// 前回の名前（同じトークンを続けて出すときに打ち直さなくてよいように。ページ内だけ覚える）
let _lastName = DEFAULT_NAME;

let _basePromise = null;
/** 土台の画像を1回だけ読む。読めなければ null（地の色だけで描く） */
function _loadBase() {
  if (!_basePromise) {
    _basePromise = new Promise((resolve) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => {
        console.warn('[token-generator] トークンの土台画像を読めませんでした。地の色だけで描きます', BASE_URL);
        _basePromise = null; // 次に出すときに読み直す
        resolve(null);
      };
      img.src = BASE_URL;
    });
  }
  return _basePromise;
}

async function generateTokenDataURL(name) {
  const canvas = document.createElement('canvas');
  canvas.width  = CARD_W;
  canvas.height = CARD_H;
  const ctx = canvas.getContext('2d');

  const base = await _loadBase();
  if (base) {
    ctx.drawImage(base, 0, 0, CARD_W, CARD_H);
  } else {
    ctx.fillStyle = FALLBACK_BG;
    ctx.fillRect(0, 0, CARD_W, CARD_H);
  }

  // 名前（帯の左寄せ。長い名前は帯の幅に縮めて収める）
  const font = `700 ${Math.round(NAME_SIZE * CARD_W)}px ${NAME_FONT}`;
  try { await document.fonts.load(font, name); } catch { /* 読めなければ端末の字で描く */ }
  ctx.fillStyle = '#111';
  ctx.font = font;
  ctx.textAlign = 'left';
  ctx.textBaseline = 'middle';
  ctx.fillText(name, NAME_X * CARD_W, NAME_Y * CARD_H, NAME_MAX_W * CARD_W);

  // 写真の土台なので JPEG（PNG だと1枚が数倍の大きさになり、リプレイの保存に響く）
  return canvas.toDataURL('image/jpeg', JPEG_QUALITY);
}

/**
 * トークンを作ってマスに出す（ドラッグで置いたときと同じく executeDrop で配置・記録する）。
 * @param {Element} slot - .custom-slot
 * @param {string} name
 */
async function placeToken(slot, name) {
  const src = await generateTokenDataURL(name);
  if (!slot.isConnected) return; // 画像を作っている間に盤面が作り直された

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
 * 空いたマスのメニューを開く: 見出し・名前欄・「出す」。名前欄で Enter を押しても出す。
 */
function openTokenMenu(slot, x, y) {
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'sol-context-menu-input';
  input.value = _lastName;
  input.maxLength = NAME_MAX;
  input.placeholder = 'トークン名';
  input.setAttribute('aria-label', 'トークン名');

  const place = () => {
    _lastName = _nameOf(input);
    placeToken(slot, _lastName);
  };

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      place();
      closeContextMenu();
    } else if (e.key === 'Escape') {
      closeContextMenu();
    }
  });

  openContextMenuAt([
    { heading: 'トークンを出す' },
    { element: input },
    { label: '出す', sub: 'Enter', action: place },
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
    _loadBase(); // 名前を打っている間に土台を読んでおく
    openTokenMenu(slot, e.clientX, e.clientY);
  });
}
