/**
 * sample-blur.js
 * 未発売SAMPLE画像のイラスト部分を薄くぼかす設定（一人回し）。
 *
 * - 設定は localStorage（tcgym_sol_sample_blur）に保存。既定オフ。
 * - オン時は body に data-sample-blur 属性を付ける（実際のぼかしは sample-blur.css）。
 * - 盤面カードの .sample-art クラスは各生成箇所（card-manager 等）が付ける。
 * - カード情報パネルの拡大画像（.cip-img）は img に擬似要素を付けられないため、
 *   表示中の画像が盤面の .sample-art カードと同じ URL のとき .cip-img-wrap.sample-art で包む。
 */

const STORAGE_KEY = 'tcgym_sol_sample_blur';

function loadSetting() {
  try {
    return localStorage.getItem(STORAGE_KEY) === '1';
  } catch {
    return false;
  }
}

function saveSetting(on) {
  try {
    localStorage.setItem(STORAGE_KEY, on ? '1' : '0');
  } catch {
    // プライベートモード等で保存できなくても、そのセッション内の切替は有効
  }
}

function applySetting(on) {
  document.body.toggleAttribute('data-sample-blur', on);
  document.querySelectorAll('input[data-sample-blur-toggle]').forEach((cb) => {
    cb.checked = on;
  });
}

/** 盤面にある .sample-art カードと同じ画像URLか（カード情報パネルの判定用） */
function isSampleSrc(src) {
  if (!src) return false;
  for (const img of document.querySelectorAll('.sample-art > img.tier-item, .sample-art > img.opp-card-img')) {
    if (img.src === src) return true;
  }
  return false;
}

/** パネル内の .cip-img がSAMPLE画像なら .cip-img-wrap.sample-art で包む */
function wrapSampleCipImages(root) {
  root.querySelectorAll('img.cip-img').forEach((img) => {
    if (img.parentElement?.classList.contains('cip-img-wrap')) return;
    // 明示フラグ（描画側が付ける img の data-sample）または盤面・トレイの .sample-art 画像とのURL一致
    if (img.dataset.sample !== '1' && !isSampleSrc(img.src)) return;
    const wrap = document.createElement('span');
    wrap.className = 'cip-img-wrap sample-art';
    img.replaceWith(wrap);
    wrap.appendChild(img);
  });
}

function initCipObserver() {
  ['cardInfoPanel', 'solMobileCipBody'].forEach((id) => {
    const panel = document.getElementById(id);
    if (!panel) return;
    // パネル描画は innerHTML 置換。置換直後（描画前のマイクロタスク）に包む
    new MutationObserver(() => wrapSampleCipImages(panel)).observe(panel, { childList: true });
  });
}

export function initSampleBlur() {
  applySetting(loadSetting());
  document.querySelectorAll('input[data-sample-blur-toggle]').forEach((cb) => {
    cb.addEventListener('change', () => {
      saveSetting(cb.checked);
      applySetting(cb.checked);
    });
  });
  initCipObserver();
}
