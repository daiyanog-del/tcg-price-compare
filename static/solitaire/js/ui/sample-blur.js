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
  if (on) scheduleScan();
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

// ───────── ペンデュラム枠判定（イラスト枠の範囲が通常枠と異なるため） ─────────
// 未発売カードのカード情報APIから pendulum_scale / card_type を引き、
// .sample-art 要素に data-art-frame="pendulum" を付ける（範囲は sample-blur.css）。

const API_CARD_INFO = '/api/card-info';
const _pendulumCache = new Map(); // 名前 -> Promise<boolean>
let _warned = false;

function _warnOnce(msg, err) {
  if (_warned) return;
  _warned = true;
  console.warn(msg, err || '');
}

/** 名前ごとにキャッシュしたペンデュラム判定。失敗時は false（通常枠のまま） */
function isPendulumCard(name) {
  if (!_pendulumCache.has(name)) {
    const p = fetch(`${API_CARD_INFO}?name=${encodeURIComponent(name)}`)
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`HTTP ${res.status}`))))
      .then((data) => {
        const info = data && data.kind === 'proxy' ? data.proxy : data;
        if (!info) return false;
        return info.pendulum_scale != null || String(info.card_type || '').includes('ペンデュラム');
      })
      .catch((err) => {
        _warnOnce('[sample-blur] ペンデュラム判定に失敗（通常枠のまま表示）', err);
        _pendulumCache.delete(name); // 一時的な失敗は次回再試行できるようにする
        return false;
      });
    _pendulumCache.set(name, p);
  }
  return _pendulumCache.get(name);
}

/** .sample-art 要素からカード名を取る（盤面・トレイ・スマホセル・情報パネルの包み） */
function nameOfSampleEl(el) {
  const own = el.dataset.cardName || el.querySelector('[data-card-name]')?.dataset.cardName;
  if (own) return own;
  if (el.classList.contains('cip-img-wrap')) {
    const t = el.closest('.cip-header')?.querySelector('.cip-name')?.textContent?.trim();
    if (t && t !== '不明なカード') return t;
  }
  return '';
}

function updateArtFrame(el) {
  const name = nameOfSampleEl(el);
  if (!name) return;
  if (el.dataset.artFrameName === name) return; // 判定済み or 判定中
  el.dataset.artFrameName = name;
  el.removeAttribute('data-art-frame');
  isPendulumCard(name).then((pend) => {
    if (el.dataset.artFrameName !== name) return; // その間に別カードへ差し替わった
    if (pend) el.setAttribute('data-art-frame', 'pendulum');
  });
}

let _scanQueued = false;
function scheduleScan() {
  if (_scanQueued) return;
  _scanQueued = true;
  queueMicrotask(() => {
    _scanQueued = false;
    // ぼかしオフ時はAPIを叩かない（オンにした時点で判定開始）
    if (!document.body.hasAttribute('data-sample-blur')) return;
    document.querySelectorAll('.sample-art').forEach(updateArtFrame);
  });
}

function initArtFrameObserver() {
  // ドロー・ロード・リプレイ・トレイ追加・パネル描画で後から増える要素にも付ける
  new MutationObserver(scheduleScan).observe(document.body, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ['class', 'data-card-name'],
  });
}

export function initSampleBlur() {
  initArtFrameObserver();
  applySetting(loadSetting());
  document.querySelectorAll('input[data-sample-blur-toggle]').forEach((cb) => {
    cb.addEventListener('change', () => {
      saveSetting(cb.checked);
      applySetting(cb.checked);
    });
  });
  initCipObserver();
}
