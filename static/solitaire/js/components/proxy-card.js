/**
 * proxy-card.js
 * 未発売カードのプロキシ表示要素を生成するモジュール
 *
 * 使い方:
 *   import { createProxyCardElement } from './proxy-card.js';
 *   const el = createProxyCardElement(proxyData);
 *   // el は .tier-item クラスを持つ div。wrapper(.tier-item-wrapper)に appendChild する。
 *
 * proxyData は /api/card-image, /api/card-images, /api/card-info(s) の
 * proxy フィールドと同一構造:
 *   { name, reading, card_type, attribute, race, level, rank, link_val,
 *     atk, def, effect_text, product_name, release_date,
 *     broad_type, is_ex }
 */

/**
 * テキストを HTML エスケープする
 * @param {string} str
 * @returns {string}
 */
function _esc(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/**
 * 効果テキスト長からフォントサイズクラスを返す
 * pc-text-m（〜80字）/ pc-text-s（〜160字）/ pc-text-xs（それ以上）
 * @param {string} text
 * @returns {string}
 */
function _effectTextClass(text) {
  const len = (text || '').length;
  if (len <= 80)  return 'pc-text-m';
  if (len <= 160) return 'pc-text-s';
  return 'pc-text-xs';
}

/**
 * 種別ラベル文字列を返す
 * @param {string} broadType  "monster"|"spell"|"trap" 等
 * @param {string} cardType   元の card_type 文字列
 * @returns {string}
 */
function _typeLabel(broadType, cardType) {
  if (broadType === 'spell') return '魔法';
  if (broadType === 'trap')  return '罠';
  // モンスター系は card_type の先頭語をそのまま使う（効果/融合/シンクロ等）
  if (cardType) {
    // 「効果モンスター」→「効果」のように最初の区切りまで取る
    const m = cardType.match(/^([^\s・\/]+)/);
    if (m) return m[1];
  }
  return 'モンスター';
}

/**
 * メタ行（属性/レベル/ランク/リンク/種族）の HTML 断片を返す
 * @param {Object} p  proxyData
 * @returns {string}
 */
function _metaHtml(p) {
  const parts = [];

  if (p.attribute) {
    parts.push(`<span class="proxy-card__meta-item">${_esc(p.attribute)}</span>`);
  }

  if (p.level != null) {
    parts.push(`<span class="proxy-card__meta-item">Lv${_esc(p.level)}</span>`);
  } else if (p.rank != null) {
    parts.push(`<span class="proxy-card__meta-item">Rk${_esc(p.rank)}</span>`);
  } else if (p.link_val != null) {
    parts.push(`<span class="proxy-card__meta-item">Link${_esc(p.link_val)}</span>`);
  }

  if (p.race) {
    parts.push(`<span class="proxy-card__meta-item">${_esc(p.race)}</span>`);
  }

  if (p.pendulum_scale != null) {
    parts.push(`<span class="proxy-card__meta-item">スケール${_esc(p.pendulum_scale)}</span>`);
  }

  return parts.join('');
}

/**
 * ATK/DEF 表示用の文字列を返す
 * atk が null の場合は「?」、def が空文字 or null の場合も「?」
 * @param {number|null} atk
 * @param {string}      def
 * @returns {{atkStr: string, defStr: string}}
 */
function _atkDef(atk, def) {
  const atkStr = atk != null ? String(atk) : '?';
  const defStr = (def != null && def !== '') ? String(def) : '?';
  return { atkStr, defStr };
}

/**
 * プロキシカード要素を生成する
 *
 * @param {Object} proxyData  proxy フィールドの内容
 * @returns {HTMLElement}     .tier-item クラスを持つ div（draggable=true）
 */
export function createProxyCardElement(proxyData) {
  const p = proxyData || {};
  const broadType = p.broad_type || 'monster';
  const label     = _typeLabel(broadType, p.card_type || '');
  const effectLen = (p.effect_text || '').length;
  const textClass = _effectTextClass(p.effect_text || '');
  const { atkStr, defStr } = _atkDef(p.atk, p.def);
  const cardName  = p.name || '不明なカード';

  const el = document.createElement('div');
  el.className  = 'proxy-card tier-item';
  el.setAttribute('draggable', 'true');
  el.setAttribute('data-broad-type', broadType);
  // カード名を data 属性にも保持（card-info-panel と hover 表示用）
  el.dataset.cardName = cardName;

  // ヘッダ
  const header = document.createElement('div');
  header.className = 'proxy-card__header';
  header.innerHTML =
    `<span class="proxy-card__name">${_esc(cardName)}</span>` +
    `<span class="proxy-card__type-label">${_esc(label)}</span>`;

  // メタ行
  const meta = document.createElement('div');
  meta.className = 'proxy-card__meta';
  meta.innerHTML = _metaHtml(p);

  // 効果テキスト（ペンデュラムは「ペンデュラム効果」を先頭に併記）
  const effect = document.createElement('div');
  effect.className = `proxy-card__effect ${textClass}`;
  const pendBlock = p.pendulum_effect
    ? `<span class="proxy-card__pendulum-label">【ペンデュラム効果】</span>`
      + _esc(p.pendulum_effect).replace(/\n/g, '<br>') + '<br>'
    : '';
  // 改行を <br> に変換して表示
  effect.innerHTML = pendBlock + _esc(p.effect_text || '').replace(/\n/g, '<br>');

  // ATK/DEF フッタ（モンスターのみ。魔法/罠は CSS で display:none）
  const stats = document.createElement('div');
  stats.className = 'proxy-card__stats';
  stats.innerHTML =
    `<span class="proxy-card__atk">ATK ${_esc(atkStr)}</span>` +
    `<span class="proxy-card__def">DEF ${_esc(defStr)}</span>`;

  el.appendChild(header);
  if (_metaHtml(p)) el.appendChild(meta);
  el.appendChild(effect);
  el.appendChild(stats);

  return el;
}
