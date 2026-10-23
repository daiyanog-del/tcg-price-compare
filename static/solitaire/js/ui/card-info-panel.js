/**
 * card-info-panel.js
 * クリックしたカードの詳細情報を左パネルに表示する
 *
 * カード名の取得経路:
 *  - テキスト/環境デッキ読込: img.dataset.cardName に名前を格納
 *  - ニューロン画像読込: 名前なし（未対応）
 */

import { setCipExpanded } from './cip-layout.js';

const API_CARD_INFO = '/api/card-info';
const API_PRICE_HISTORY = '/api/price-history';

/** カード名を img 要素に付与（deck-input-panel から呼ぶ） */
export function setCardName(imgEl, name) {
  if (imgEl) imgEl.dataset.cardName = name;
}

/**
 * カード名と画像URLを直接指定してカード詳細パネルを表示する。
 * 委譲クリックを使わないカード（マイデッキの一覧など）から呼ぶ。
 */
export function showCardByName(name, imgSrc, source) {
  const isSample = source === 'official_sample';
  if (name) {
    _fetchAndShow(name, imgSrc || '', isSample);
  } else {
    _showUnknown(imgSrc || '', null, null, undefined, true, isSample);
  }
}

/** カード詳細パネルを初期化（クリックリスナー設定） */
export function initCardInfoPanel() {
  // 表示/非表示トグル
  document.getElementById('cipToggle')?.addEventListener('click', () => {
    const col  = document.querySelector('.sol-cip-col');
    const area = document.querySelector('.sol-field-area');
    const btn  = document.getElementById('cipToggle');
    // 盤面の上へ広げているときの ◀ は、隙間に戻すだけ（パネルは消さない）
    if (col.classList.contains('is-expanded')) { setCipExpanded(false); return; }
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
    const isSample = !!cardEl.closest('.sample-art');
    // プロキシカードは src がないため空文字または undefined になる
    const imgSrc = cardEl.src || '';
    if (cardEl.closest('.tier-item-wrapper')?.dataset.token === '1') {
      // トークン: 名前は利用者が付けたもの。実在のカード名と同じでも、そのカードの情報は引かない
      showTokenInfo(imgSrc, name);
    } else if (name) {
      _fetchAndShow(name, imgSrc, isSample);
    } else {
      // 名前不明（ニューロン画像等）: サムネだけ表示
      _showUnknown(imgSrc, undefined, undefined, undefined, true, isSample);
    }
    // 狭い画面では盤面の上へ広げて読めるようにする（広い画面では何もしない）
    setCipExpanded(true);
  });

  // ── 狭い画面で広げた詳細を閉じる（隙間の小さい表示に戻す）──
  // 誤ってカードを選んでも、次の操作の邪魔をしないように閉じる:
  //   ドラッグを始めた（隠れている盤面のマスへ置けるように）・盤面などカード以外を押した・Esc・ダブルクリック（効果発動）
  document.addEventListener('dragstart', () => setCipExpanded(false), true);
  document.addEventListener('dblclick', () => setCipExpanded(false));
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') setCipExpanded(false); });
  document.addEventListener('pointerdown', (e) => {
    const t = e.target instanceof Element ? e.target : null;
    if (!t || t.closest('.sol-cip-col, #cipToggle, .tier-item, .sol-context-menu')) return;
    setCipExpanded(false);
  }, true);
  // 隙間の小さい表示を押したら、表示中のカードの詳細を広げる
  document.querySelector('.sol-cip-col')?.addEventListener('click', () => {
    if (document.querySelector('#cardInfoPanel .cip-header')) setCipExpanded(true);
  });

  // 初期状態
  _showEmpty();
}

// ── 内部処理 ──────────────────────────────────────

/**
 * SAMPLEフラグは各描画関数の引数で受け取り、描画した img 自体に data-sample を付ける。
 * （パネル共通の状態にすると、fetch 完了が前後したとき別カードの画像がぼかされるため）
 */
function _sampleAttr(isSample) {
  return isSample ? ' data-sample="1"' : '';
}

async function _fetchAndShow(name, imgSrc, isSample = false) {
  _showLoading(name, imgSrc, undefined, isSample);
  try {
    const [infoRes, priceRes] = await Promise.all([
      fetch(`${API_CARD_INFO}?name=${encodeURIComponent(name)}`),
      fetch(`${API_PRICE_HISTORY}?card=${encodeURIComponent(name)}`).catch(() => null),
    ]);
    const data = await infoRes.json();
    const priceData = priceRes ? await priceRes.json().catch(() => null) : null;
    if (data.found) {
      // 発売済みカード: 情報は data 直下にある
      _renderCard(data, imgSrc, name, priceData, undefined, true, isSample);
    } else if (data.kind === 'proxy' && data.proxy) {
      // 未発売カード: 実データは proxy の中にある（フィールド名は _renderCard と一致）
      _renderCard(data.proxy, imgSrc, name, priceData, undefined, true, isSample, true);
    } else {
      _showUnknown(imgSrc, name, priceData, undefined, true, isSample);
    }
  } catch {
    _showUnknown(imgSrc, name, null, undefined, true, isSample);
  }
}

/**
 * カード詳細を描画する。
 * @param {Object}  data       カード情報（/api/card-info のレスポンス、または proxy オブジェクト）
 * @param {string}  imgSrc     画像URL
 * @param {string}  name       カード名
 * @param {Object|null} priceData  /api/price-history のレスポンス。スマホ詳細パネルからは null を渡す
 * @param {Element} [targetPanel]  描画先パネル要素。省略時は既存の #cardInfoPanel（PC用）
 *   スマホ詳細パネル（#solMobileCip）から再利用するための最小限の引数追加（mobile-ui.js が使用）。
 *   引数省略時の挙動は従来と完全に同じ（後方互換）。
 * @param {boolean} [showPrice=true]  価格ブロックを生成するか。スマホは false でブロック自体を作らない（B-12）。
 * @param {boolean} [isSample=false]  公式SAMPLE画像か（透かし・ぼかしの出し分け用）。
 * @param {boolean} [isUnreleased=false]  未発売カード（/api/card-info が kind:'proxy' を返したもの）か。
 *   true のときだけ「テキストの誤りを報告」ボタンを置く（発売済みカードには出さない）。
 *
 * TCG先行カード（data.tcg_preview。英語版で先に出て日本に無いカード）は、名前の下に「TCG先行・日本未発売」の札、
 * 効果文に「非公式訳（機械翻訳）」の注記、その下に英語の原文（折りたたみ。訳が無いカードは開いた状態）を出す。
 * 価格が無いので価格の欄・「テキストの誤りを報告」の代わりに「日本で未発売のため価格はありません」とだけ出す。
 */
export function _renderCard(data, imgSrc, name, priceData, targetPanel, showPrice = true, isSample = false, isUnreleased = false) {
  const panel = targetPanel || document.getElementById('cardInfoPanel');
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
  // E-1: effect_text 系フィールドが改行を文字列 "<br>" のまま持っていることがあるため、
  // _esc の前に実際の改行(\n)へ正規化してから \n→<br> 変換に通す（PC にも効く既存不具合の修正）。
  const pendHtml = data.pendulum_effect
    ? `<div class="cip-effect cip-pendulum"><span class="cip-pendulum-label">【ペンデュラム効果】</span>${_esc(_normalizeBreaks(data.pendulum_effect)).replace(/\n/g, '<br>')}</div>`
    : '';
  const isTcgPreview = !!data.tcg_preview;

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
      <img class="cip-img" src="${_esc(imgSrc)}" alt="${_esc(name || '')}"${_sampleAttr(isSample)}>
      <div class="cip-meta">
        <p class="cip-name">${_esc(name || '')}</p>
        ${isTcgPreview ? '<p class="cip-tcg"><span class="cip-tcg-badge">TCG先行・日本未発売</span></p>' : ''}
        <p class="cip-type">${typeLine}</p>
        ${attrLine ? `<p class="cip-attr">${attrLine}</p>` : ''}
        ${statsHtml}
      </div>
    </div>
    ${isTcgPreview ? _buildTcgPreviewBody(data, pendHtml) : `${pendHtml}
    <div class="cip-effect">${_esc(_normalizeBreaks(data.effect_text || '')).replace(/\n/g, '<br>')}</div>
    ${showPrice ? _buildPriceHtml(name, priceData) : ''}`}
  `;
  // 未発売カード専用: 読み取り・転記ミスを利用者が知らせる窓口。ボタンは DOM 生成（innerHTML に名前を入れない）
  // TCG先行カードの訳は機械訳で、報告の受け先（未発売カードの登録）とは別なので出さない
  if (isUnreleased && !isTcgPreview && window.TextReport) window.TextReport.attach(panel, data.name || name);
}

/** TCG先行カードの効果欄: 非公式訳＋英語の原文（折りたたみ）＋価格が無い旨 */
function _buildTcgPreviewBody(data, pendHtml) {
  const text = s => _esc(_normalizeBreaks(s || '')).replace(/\n/g, '<br>');
  const hasJa = !!data.unofficial_translation;
  const enParts = [];
  if (data.pendulum_effect_en) enParts.push(`<span class="cip-pendulum-label">[Pendulum Effect]</span>${text(data.pendulum_effect_en)}`);
  if (data.effect_text_en) enParts.push(text(data.effect_text_en));
  const enHtml = enParts.length
    // 訳が無い（通常モンスターの説明文・未訳）ときは原文が唯一の本文なので開いておく
    ? `<details class="cip-original"${hasJa ? '' : ' open'}><summary>英語の原文</summary><div class="cip-effect" lang="en">${enParts.join('<br>')}</div></details>`
    : '';
  const jaHtml = hasJa
    ? `${pendHtml}<div class="cip-effect"><p class="cip-unofficial">非公式訳（機械翻訳）</p>${text(data.effect_text)}</div>`
    : '';
  return `${jaHtml}${enHtml}<p class="cip-noprice">日本で未発売のため価格はありません</p>`;
}

/**
 * 読み込み中表示。targetPanel はスマホ詳細パネル再利用用（_renderCard 参照。A-2で export・引数追加）。
 * @param {string}  name
 * @param {string}  imgSrc
 * @param {Element} [targetPanel]  省略時は既存の #cardInfoPanel（PC用）
 */
export function _showLoading(name, imgSrc, targetPanel, isSample = false) {
  const panel = targetPanel || document.getElementById('cardInfoPanel');
  if (!panel) return;
  panel.innerHTML = `
    <div class="cip-header">
      <img class="cip-img" src="${_esc(imgSrc)}" alt=""${_sampleAttr(isSample)}>
      <div class="cip-meta">
        <p class="cip-name">${_esc(name)}</p>
        <p class="cip-type" style="color:#4a6490">読み込み中...</p>
      </div>
    </div>
  `;
}

/**
 * 詳細情報なし（未対応カード）の描画。targetPanel はスマホ詳細パネル再利用用（_renderCard 参照）。
 * @param {boolean} [showPrice=true]  価格ブロックを生成するか（B-12）。
 */
export function _showUnknown(imgSrc, name, priceData, targetPanel, showPrice = true, isSample = false) {
  const panel = targetPanel || document.getElementById('cardInfoPanel');
  if (!panel) return;
  panel.innerHTML = `
    <div class="cip-header">
      <img class="cip-img" src="${_esc(imgSrc)}" alt=""${_sampleAttr(isSample)}>
      <div class="cip-meta">
        <p class="cip-name">${_esc(name || '不明なカード')}</p>
        <p class="cip-type" style="color:#4a6490">詳細情報なし</p>
      </div>
    </div>
    ${(name && showPrice) ? _buildPriceHtml(name, priceData) : ''}
  `;
}

/**
 * トークンの描画（名前と「トークン」だけ。情報・相場は引かない）。targetPanel はスマホ詳細パネル用（_renderCard 参照）。
 * @param {string} imgSrc
 * @param {string} [name]  トークンに付けた名前（2026-10-23 より前に出したトークンは無い）
 * @param {Element} [targetPanel]
 */
export function showTokenInfo(imgSrc, name, targetPanel) {
  const panel = targetPanel || document.getElementById('cardInfoPanel');
  if (!panel) return;
  panel.innerHTML = `
    <div class="cip-header">
      <img class="cip-img" src="${_esc(imgSrc)}" alt="${_esc(name || 'トークン')}">
      <div class="cip-meta">
        <p class="cip-name">${_esc(name || 'トークン')}</p>
        <p class="cip-type">トークン</p>
      </div>
    </div>
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
  // 休止中の店（サーバーの SUSPENDED_SHOPS）は「いまの最安」に使わない。グラフ用に行は届く（2026-10-22 カーナベル休止）
  const suspended = new Set(data?.suspended_shops || []);
  const valid = items.filter(r => r.price > 0 && !suspended.has(r.shop));
  if (valid.length === 0) return null;
  // 最新収集日の行のみを対象にする（過去日の特売を拾わない）
  const latestDate = valid.reduce((a, b) => a.date > b.date ? a : b).date;
  const latest = valid.filter(r => r.date === latestDate);
  return latest.reduce((a, b) => a.price < b.price ? a : b);
}

/**
 * E-1: /api/card-info(s) の effect_text 等が改行を実際の \n ではなく文字列 "<br>" のまま
 * 持っているケースがあり、_esc 後に \n→<br> 変換しても "<br>" という文字列がそのまま
 * 画面に表示されてしまっていた（PC 側の詳細パネルにも同じ既存不具合）。
 * _esc に通す前に "<br>" 系タグ文字列を実際の改行へ正規化する。
 * @param {string} text
 * @returns {string}
 */
function _normalizeBreaks(text) {
  return String(text ?? '').replace(/<br\s*\/?>/gi, '\n');
}

function _esc(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
