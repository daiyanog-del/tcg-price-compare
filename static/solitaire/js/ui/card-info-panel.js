/**
 * card-info-panel.js
 * クリックしたカードの詳細情報を左パネルに表示する
 *
 * カード名の取得経路:
 *  - テキスト/環境デッキ読込: img.dataset.cardName に名前を格納
 *  - ニューロン画像読込: 名前なし（未対応）
 */

const API_CARD_INFO = '/api/card-info';

/** カード名を img 要素に付与（deck-input-panel から呼ぶ） */
export function setCardName(imgEl, name) {
  if (imgEl) imgEl.dataset.cardName = name;
}

/** カード詳細パネルを初期化（クリックリスナー設定） */
export function initCardInfoPanel() {
  // プール・フィールド上のカードへのクリックを委譲で受け取る
  document.addEventListener('click', (e) => {
    const img = e.target.closest('img.tier-item');
    if (!img) return;
    const name = img.dataset.cardName;
    if (name) {
      _fetchAndShow(name, img.src);
    } else {
      // 名前不明（ニューロン画像）: サムネだけ表示
      _showUnknown(img.src);
    }
  });

  // 初期状態
  _showEmpty();
}

// ── 内部処理 ──────────────────────────────────────

async function _fetchAndShow(name, imgSrc) {
  _showLoading(name, imgSrc);
  try {
    const res = await fetch(`${API_CARD_INFO}?name=${encodeURIComponent(name)}`);
    const data = await res.json();
    if (data.found) {
      _renderCard(data, imgSrc);
    } else {
      _showUnknown(imgSrc, name);
    }
  } catch {
    _showUnknown(imgSrc, name);
  }
}

function _renderCard(data, imgSrc) {
  const panel = document.getElementById('cardInfoPanel');
  if (!panel) return;

  const level = data.level ? `Lv.${data.level}` : data.rank ? `Rank ${data.rank}` : '';
  const atk = data.atk != null ? data.atk : '?';
  const def = data.def != null ? data.def : '?';

  panel.innerHTML = `
    <div class="cip-header">
      <img class="cip-img" src="${_esc(imgSrc)}" alt="${_esc(data.name || '')}">
      <div class="cip-meta">
        <p class="cip-name">${_esc(data.name || '')}</p>
        <p class="cip-type">${_esc([data.card_type, data.race].filter(Boolean).join(' / '))}</p>
        <p class="cip-attr">${_esc([data.attribute, level].filter(Boolean).join(' &nbsp;·&nbsp; '))}</p>
        <p class="cip-stats">ATK <strong>${atk}</strong> &nbsp;/&nbsp; DEF <strong>${def}</strong></p>
      </div>
    </div>
    <div class="cip-effect">${_esc(data.effect_text || '').replace(/\n/g, '<br>')}</div>
  `;
}

function _showLoading(name, imgSrc) {
  const panel = document.getElementById('cardInfoPanel');
  if (!panel) return;
  panel.innerHTML = `
    <div class="cip-header">
      <img class="cip-img" src="${_esc(imgSrc)}" alt="">
      <div class="cip-meta">
        <p class="cip-name">${_esc(name)}</p>
        <p class="cip-type" style="color:#4a6490">読み込み中...</p>
      </div>
    </div>
  `;
}

function _showUnknown(imgSrc, name) {
  const panel = document.getElementById('cardInfoPanel');
  if (!panel) return;
  panel.innerHTML = `
    <div class="cip-header">
      <img class="cip-img" src="${_esc(imgSrc)}" alt="">
      <div class="cip-meta">
        <p class="cip-name">${_esc(name || '不明なカード')}</p>
        <p class="cip-type" style="color:#4a6490">詳細情報なし</p>
      </div>
    </div>
  `;
}

function _showEmpty() {
  const panel = document.getElementById('cardInfoPanel');
  if (!panel) return;
  panel.innerHTML = `<p class="cip-empty">カードをクリックすると詳細を表示</p>`;
}

function _esc(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
