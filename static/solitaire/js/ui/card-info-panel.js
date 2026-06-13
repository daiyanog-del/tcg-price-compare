/**
 * card-info-panel.js
 * クリックしたカードの詳細情報を左パネルに表示する
 *
 * カード名の取得経路:
 *  - テキスト/環境デッキ読込: img.dataset.cardName に名前を格納
 *  - ニューロン画像読込: 名前なし（未対応）
 */

const API_CARD_INFO = '/api/card-info';
const API_PRICE_HISTORY = '/api/price-history';

/** カード名を img 要素に付与（deck-input-panel から呼ぶ） */
export function setCardName(imgEl, name) {
  if (imgEl) imgEl.dataset.cardName = name;
}

/**
 * カード名と画像URLを直接指定してカード詳細パネルを表示する。
 * 相手の想定妨害トレイなど、委譲クリックを使わないカードから呼ぶ。
 */
export function showCardByName(name, imgSrc) {
  if (name) {
    _fetchAndShow(name, imgSrc || '');
  } else {
    _showUnknown(imgSrc || '', null, null);
  }
}

/** カード詳細パネルを初期化（クリックリスナー設定） */
export function initCardInfoPanel() {
  // 表示/非表示トグル
  document.getElementById('cipToggle')?.addEventListener('click', () => {
    const col  = document.querySelector('.sol-cip-col');
    const area = document.querySelector('.sol-field-area');
    const btn  = document.getElementById('cipToggle');
    const hide = !col.classList.contains('hidden');
    col.classList.toggle('hidden', hide);
    area.classList.toggle('cip-hidden', hide);
    btn.textContent = hide ? '▶' : '◀';
    btn.title = hide ? 'カード情報パネルを表示' : 'カード情報パネルを非表示';
  });
  // プール・フィールド上のカードへのクリックを委譲で受け取る
  // img.tier-item（発売済み）または div.tier-item（プロキシ）どちらも .tier-item で対応
  document.addEventListener('click', (e) => {
    const cardEl = e.target.closest('.tier-item');
    if (!cardEl) return;
    const name = cardEl.dataset.cardName;
    // プロキシカードは src がないため空文字または undefined になる
    const imgSrc = cardEl.src || '';
    if (name) {
      _fetchAndShow(name, imgSrc);
    } else {
      // 名前不明（ニューロン画像等）: サムネだけ表示
      _showUnknown(imgSrc);
    }
  });

  // 初期状態
  _showEmpty();
}

// ── 内部処理 ──────────────────────────────────────

async function _fetchAndShow(name, imgSrc) {
  _showLoading(name, imgSrc);
  try {
    const [infoRes, priceRes] = await Promise.all([
      fetch(`${API_CARD_INFO}?name=${encodeURIComponent(name)}`),
      fetch(`${API_PRICE_HISTORY}?card=${encodeURIComponent(name)}`).catch(() => null),
    ]);
    const data = await infoRes.json();
    const priceData = priceRes ? await priceRes.json().catch(() => null) : null;
    if (data.found) {
      // 発売済みカード: 情報は data 直下にある
      _renderCard(data, imgSrc, name, priceData);
    } else if (data.kind === 'proxy' && data.proxy) {
      // 未発売カード: 実データは proxy の中にある（フィールド名は _renderCard と一致）
      _renderCard(data.proxy, imgSrc, name, priceData);
    } else {
      _showUnknown(imgSrc, name, priceData);
    }
  } catch {
    _showUnknown(imgSrc, name, null);
  }
}

function _renderCard(data, imgSrc, name, priceData) {
  const panel = document.getElementById('cardInfoPanel');
  if (!panel) return;

  const isMonster = data.broad_type === 'monster';
  const isSpellTrap = data.broad_type === 'spell' || data.broad_type === 'trap';

  // 種別行: モンスターは "シンクロ・効果モンスター"
  //         魔法/罠は "魔法 [速攻]" や "罠 [永続]"
  let typeLine = '';
  if (isMonster) {
    typeLine = _esc(data.card_type || '');
  } else {
    const prop = data.property ? ` [${_esc(data.property)}]` : '';
    typeLine = `${_esc(data.card_type)}${prop}`;
  }

  // 属性・種族・レベル行（モンスター）: 光 · 戦士族 · Lv.4
  const level = data.level   ? `Lv.${data.level}`
              : data.rank     ? `Rank ${data.rank}`
              : data.link_val ? `LINK-${data.link_val}`
              : '';
  const scale = data.pendulum_scale != null ? `スケール${data.pendulum_scale}` : '';
  const attrLine = isMonster
    ? [data.attribute, data.race, level, scale].filter(Boolean).map(_esc).join(' &nbsp;·&nbsp; ')
    : '';

  // ペンデュラム効果（本体効果とは別ブロック）
  const pendHtml = data.pendulum_effect
    ? `<div class="cip-effect cip-pendulum"><span class="cip-pendulum-label">【ペンデュラム効果】</span>${_esc(data.pendulum_effect).replace(/\n/g, '<br>')}</div>`
    : '';

  // ATK/DEF（モンスターのみ）
  const statsHtml = isMonster ? (() => {
    const atk = data.atk != null ? data.atk : '?';
    // def は発売済み=数値、未発売プロキシ=文字列（守備力なしは空文字）。
    // 空文字は「未設定」とみなし、モンスターなら '—' を表示する。
    const hasDef = data.def != null && data.def !== '';
    const def = hasDef ? data.def : (data.broad_type === 'monster' ? '—' : null);
    return `<p class="cip-stats">ATK <strong>${atk}</strong>${def !== null ? ` &nbsp;/&nbsp; DEF <strong>${def}</strong>` : ''}</p>`;
  })() : '';

  panel.innerHTML = `
    <div class="cip-header">
      <img class="cip-img" src="${_esc(imgSrc)}" alt="${_esc(name || '')}">
      <div class="cip-meta">
        <p class="cip-name">${_esc(name || '')}</p>
        <p class="cip-type">${typeLine}</p>
        ${attrLine ? `<p class="cip-attr">${attrLine}</p>` : ''}
        ${statsHtml}
      </div>
    </div>
    ${pendHtml}
    <div class="cip-effect">${_esc(data.effect_text || '').replace(/\n/g, '<br>')}</div>
    ${_buildPriceHtml(name, priceData)}
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

function _showUnknown(imgSrc, name, priceData) {
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
    ${name ? _buildPriceHtml(name, priceData) : ''}
  `;
}

function _showEmpty() {
  const panel = document.getElementById('cardInfoPanel');
  if (!panel) return;
  panel.innerHTML = `<p class="cip-empty">カードをクリックすると詳細を表示</p>`;
}

function _buildPriceHtml(name, priceData) {
  const searchUrl = '/card/' + encodeURIComponent(name);
  const buyUrl    = '/buy/'  + encodeURIComponent(name);

  const best = _bestFromHistory(priceData);
  const rarityHtml = best && best.rarity
    ? `<span class="cip-price-rarity">${_esc(best.rarity)}</span>` : '';
  const priceInfo = best
    ? `<p class="cip-price-val">¥${Number(best.price).toLocaleString('ja-JP')}${rarityHtml}<span class="cip-price-shop">${_esc(best.shop)}</span></p>`
    : '';

  return `
    <div class="cip-price">
      ${priceInfo}
      <div class="cip-price-links">
        <a href="${searchUrl}" target="_blank" rel="noopener" class="cip-price-link">販売価格を調べる</a>
        <a href="${buyUrl}"    target="_blank" rel="noopener" class="cip-price-link cip-buy-link">買取価格を調べる</a>
      </div>
    </div>
  `;
}

// /api/price-history レスポンスから最新収集日の最安値を返す
function _bestFromHistory(data) {
  const items = data?.data;
  if (!items || items.length === 0) return null;
  const valid = items.filter(r => r.price > 0);
  if (valid.length === 0) return null;
  // 最新収集日の行のみを対象にする（過去日の特売を拾わない）
  const latestDate = valid.reduce((a, b) => a.date > b.date ? a : b).date;
  const latest = valid.filter(r => r.date === latestDate);
  return latest.reduce((a, b) => a.price < b.price ? a : b);
}

function _esc(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
