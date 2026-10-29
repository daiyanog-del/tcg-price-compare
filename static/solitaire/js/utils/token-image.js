/**
 * token-image.js
 * トークンの札の画像（data: URL）を名前から描く共通部品。
 * トークンを出すとき（ui/token-generator.js）と、リプレイを開くときに描き直すとき（services/replay-service.js）の両方が使う。
 *
 * 2026-10-29 token-generator.js から移した（中身は変えていない）。リプレイの画像辞書の data: は
 * 利用者が送った値なのでそのまま出さず、名前からここで描き直す（replay-image-policy.js）。
 * replay-service.js は token-generator.js を import できない（token-generator.js が replay-service.js を import する）ため、
 * 描く処理だけをどこも import しないここに置く。
 */

// 出す画像の大きさ（盤面のカード 106×154 の1.5倍。トークンは1枚ずつ data: URL でリプレイに入るので、
// 見た目と容量の釣り合いで決めた。2倍・画質0.85 だと1枚 約19KB、これで約10KB。保存の上限は1件300KB）
const CARD_W = 159;
const CARD_H = 231;
const JPEG_QUALITY = 0.75;
export const TOKEN_NAME_MAX = 12;
export const DEFAULT_TOKEN_NAME = 'トークン';

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

let _basePromise = null;
/** 土台の画像を1回だけ読む。読めなければ null（地の色だけで描く） */
export function loadTokenBase() {
  if (!_basePromise) {
    _basePromise = new Promise((resolve) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => {
        console.warn('[token-image] トークンの土台画像を読めませんでした。地の色だけで描きます', BASE_URL);
        _basePromise = null; // 次に出すときに読み直す
        resolve(null);
      };
      img.src = BASE_URL;
    });
  }
  return _basePromise;
}

/** 名前欄の値を整える（空なら既定の名前） */
export function tokenNameOf(raw) {
  return String(raw ?? '').trim().slice(0, TOKEN_NAME_MAX) || DEFAULT_TOKEN_NAME;
}

/**
 * トークンの札を描いて data: URL で返す。
 * @param {string} name - tokenNameOf で整えた名前
 * @returns {Promise<string>}
 */
export async function generateTokenDataURL(name) {
  const canvas = document.createElement('canvas');
  canvas.width  = CARD_W;
  canvas.height = CARD_H;
  const ctx = canvas.getContext('2d');

  const base = await loadTokenBase();
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
