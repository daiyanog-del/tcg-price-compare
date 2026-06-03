/**
 * token-generator.js
 * プレビュー画像をドラッグして盤面に直接トークンを配置する
 *
 * 仕組み:
 *  dragstart 時に tokenDataURL から新しい .tier-item-wrapper を poolRow に追加し、
 *  その img の ID を dataTransfer にセットする。
 *  既存の drop 処理がそのままカードをフィールドへ移動する。
 *  ドロップが無効な場所なら poolRow に残る（再利用可）。
 */

import { addCardToPool } from '../components/card-manager.js';
import { registerCardImage } from '../services/replay-service.js';

const CARD_W = 106;
const CARD_H = 154;

const TOKEN_COLORS = [
  { label: '緑', value: '#2d6040' },
  { label: '金', value: '#7a6020' },
  { label: '青', value: '#1a3d6b' },
  { label: '赤', value: '#6b1a2a' },
];

let _selectedColor = TOKEN_COLORS[0].value;

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

/** プレビュー画像を現在の設定で更新 */
function refreshPreview() {
  const img = document.getElementById('tokenPreviewImg');
  if (!img) return;
  const name = (document.getElementById('tokenName')?.value || 'トークン').trim() || 'トークン';
  img.src = generateTokenDataURL(name, _selectedColor);
}

export function initTokenGenerator() {
  // 初期プレビュー描画
  refreshPreview();

  // 名前入力で即時更新
  document.getElementById('tokenName')?.addEventListener('input', refreshPreview);

  // カラーボタン
  document.querySelectorAll('.sol-token-color-btn').forEach(btn => {
    if (btn.dataset.color === _selectedColor) btn.classList.add('active');
    btn.addEventListener('click', () => {
      document.querySelectorAll('.sol-token-color-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      _selectedColor = btn.dataset.color;
      refreshPreview();
    });
  });

  // プレビュー画像のドラッグ開始
  // → 新しいカード要素を poolRow に生成してその ID を dataTransfer に渡す
  const preview = document.getElementById('tokenPreviewImg');
  if (!preview) return;

  preview.addEventListener('dragstart', (ev) => {
    const name = (document.getElementById('tokenName')?.value || 'トークン').trim() || 'トークン';
    const src  = generateTokenDataURL(name, _selectedColor);

    // poolRow に新カードを追加
    addCardToPool(src, false, false);

    // 追加された最後のカードの ID を取得
    const pool = document.getElementById('poolRow');
    const lastImg = pool?.querySelector('.tier-item-wrapper:last-child img');
    if (!lastImg?.id) { ev.preventDefault(); return; }

    registerCardImage(lastImg.id, src);

    // 既存の drop 処理に渡す ID をセット
    ev.dataTransfer.setData('text/plain', lastImg.id);
  });
}
