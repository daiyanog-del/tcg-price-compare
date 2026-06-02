/**
 * token-generator.js
 * トークンカード画像をCanvasで生成してデッキプールに追加する
 */

import { addCardToPool } from '../components/card-manager.js';
import { registerCardImage } from '../services/replay-service.js';

const CARD_W = 106;
const CARD_H = 154;

const TOKEN_COLORS = [
  { label: '緑',  value: '#2d6040' },
  { label: '金',  value: '#7a6020' },
  { label: '青',  value: '#1a3d6b' },
  { label: '赤',  value: '#6b1a2a' },
  { label: '紫',  value: '#4a1a6b' },
  { label: '灰',  value: '#383848' },
];

let _selectedColor = TOKEN_COLORS[0].value;

/**
 * CanvasでトークンカードのData URLを生成
 */
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

  // 薄文字「TOKEN」（透かし）
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
  // 長い名前は折り返し
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
 * トークン生成UIを初期化
 */
export function initTokenGenerator() {
  // カラーボタン
  document.querySelectorAll('.sol-token-color-btn').forEach(btn => {
    if (btn.dataset.color === _selectedColor) btn.classList.add('active');
    btn.addEventListener('click', () => {
      document.querySelectorAll('.sol-token-color-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      _selectedColor = btn.dataset.color;
    });
  });

  // 生成ボタン
  document.getElementById('generateTokenBtn')?.addEventListener('click', () => {
    const name = (document.getElementById('tokenName')?.value || 'トークン').trim() || 'トークン';
    const src = generateTokenDataURL(name, _selectedColor);
    addCardToPool(src, false, false);
    // リプレイ辞書に登録
    const pool = document.getElementById('poolRow');
    if (pool) {
      const last = pool.querySelector('.tier-item-wrapper:last-child img');
      if (last?.id) registerCardImage(last.id, src);
    }
  });
}
