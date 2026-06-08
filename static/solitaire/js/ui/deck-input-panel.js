/**
 * deck-input-panel.js
 * テキストデッキ / 環境メタデッキ読込UI
 * バッチAPI /api/card-images, /api/card-infos でデッキ全体を一括取得し高速化
 */

import { addCardToPool, sortPoolCards } from '../components/card-manager.js';
import { registerCardImage, registerCardName } from '../services/replay-service.js';
import { setCardName } from './card-info-panel.js';

const API_CARD_IMAGE  = '/api/card-image';   // 単数版（フォールバック用）
const API_CARD_IMAGES = '/api/card-images';  // バッチ版（POST）
const API_CARD_INFO   = '/api/card-info';    // 単数版（フォールバック用）
const API_CARD_INFOS  = '/api/card-infos';   // バッチ版（POST）
const API_META = '/api/meta';
const API_META_DECK = '/api/meta/deck';

/**
 * デッキリストテキストをパース
 * "3 灰流うらら" 形式 → [{qty, name}]
 */
function parseDeckList(text) {
  return text.split('\n')
    .map(l => l.trim())
    .filter(l => l && l !== '[EX]')  // [EX]区切り行を除外
    .map(line => {
      const m = line.match(/^(\d+)\s+(.+)$/);
      return m ? { qty: parseInt(m[1], 10), name: m[2].trim() } : { qty: 1, name: line };
    });
}

/**
 * 盤面・手札のカードをデッキに戻してから、プールの全カードを削除する
 * 新しいデッキを読み込む前に呼ぶ
 */
async function clearAllCards() {
  // フィールド・手札・墓地等のカードをすべてプールに戻す
  const { returnAllCardsToDeck } = await import('../components/card-manager.js');
  returnAllCardsToDeck();

  // プールの全カードを削除（ダミーも前デッキのカードも）
  ['poolRow', 'poolRow2'].forEach(poolId => {
    const pool = document.getElementById(poolId);
    if (pool) pool.innerHTML = '';
  });
}

/**
 * カード名からAPIで画像URLを取得してプールに追加（単枚版・フォールバック用）
 * @param {string}       name   カード名
 * @param {number}       qty    枚数
 * @param {boolean|null} isEx   EXデッキか（null=APIレスポンスから自動判定）
 */
async function addCardByName(name, qty, isEx) {
  // 画像URLとカード種別を並列取得
  const [imageRes, infoRes] = await Promise.all([
    fetch(`${API_CARD_IMAGE}?name=${encodeURIComponent(name)}`).catch(() => null),
    fetch(`${API_CARD_INFO}?name=${encodeURIComponent(name)}`).catch(() => null),
  ]);

  let src = null;
  try {
    if (imageRes?.ok) {
      const data = await imageRes.json();
      src = data.url || null;
    }
  } catch { /* 無視 */ }

  // broad_type: "monster" | "spell" | "trap"
  let cardType = null;
  let finalIsEx = isEx ?? false;  // null の場合は false をデフォルトにした上で上書き
  try {
    if (infoRes?.ok) {
      const data = await infoRes.json();
      if (data.found && data.broad_type) cardType = data.broad_type;
      // isEx が null（自動判定モード）の場合のみ API レスポンスの is_ex を使用
      if (isEx == null && data.found) finalIsEx = !!data.is_ex;
    }
  } catch { /* 無視 */ }

  if (!src) {
    console.warn(`画像取得失敗: ${name}`);
    return;
  }

  const poolId = finalIsEx ? 'poolRow2' : 'poolRow';
  for (let i = 0; i < qty; i++) {
    addCardToPool(src, false, finalIsEx);
    // 追加直後に最後の wrapper を取ってリプレイ辞書・カード名・種別を登録
    const pool = document.getElementById(poolId);
    if (pool) {
      const lastWrapper = pool.querySelector('.tier-item-wrapper:last-child');
      const lastImg     = lastWrapper?.querySelector('img');
      if (lastImg?.id) {
        registerCardImage(lastImg.id, src);
        registerCardName(lastImg.id, name);  // リプレイ共有用に名前を記録
        setCardName(lastImg, name);  // カード詳細パネル用に名前を付与
        // セット時のモンスター/魔法罠判定に使う（モンスター=裏側守備、魔法罠=伏せ）
        if (cardType && lastWrapper) lastWrapper.dataset.cardType = cardType;
      }
    }
  }
}

/**
 * デッキ全カードをバッチAPIで一括取得してプールに追加
 * 往復回数を「種類数×2本」→「2往復」に削減し高速化する。
 * バッチAPIが失敗した場合は addCardByName による逐次フォールバックで保険。
 *
 * @param {Array<{qty, name, is_ex?}>} cards       カードリスト
 * @param {boolean|null}               defaultIsEx  EXデッキ強制指定（null=card.is_ex/API自動判定）
 */
async function addCardsBatch(cards, defaultIsEx) {
  if (cards.length === 0) return;
  const names = cards.map(c => c.name);

  // 画像バッチとカード情報バッチを並列取得
  let images = {};
  let infos  = {};
  let imgBatchOk = false;

  const [imgRes, infoRes] = await Promise.all([
    fetch(API_CARD_IMAGES, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ names }),
    }).catch(() => null),
    fetch(API_CARD_INFOS, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ names }),
    }).catch(() => null),
  ]);

  if (imgRes?.ok) {
    try { images = (await imgRes.json()).images || {}; imgBatchOk = true; } catch { /* 無視 */ }
  }
  if (infoRes?.ok) {
    try { infos = (await infoRes.json()).infos || {}; } catch { /* 無視 */ }
  }

  // 画像バッチが使えない場合は逐次フォールバック（カードが全滅するより安全）
  if (!imgBatchOk) {
    console.warn('[addCardsBatch] 画像バッチAPI失敗、逐次フォールバック');
    for (const card of cards) {
      const fbIsEx = card.is_ex !== undefined ? !!card.is_ex : defaultIsEx;
      await addCardByName(card.name, card.qty, fbIsEx);
    }
    return;
  }

  // 取得結果を元の cards 配列順で DOM に反映（順序を保証するため配列ループ駆動）
  for (const card of cards) {
    const { qty, name } = card;
    const src = images[name] || null;
    if (!src) {
      console.warn(`画像取得失敗: ${name}`);
      continue;
    }

    const info = infos[name] || {};
    // broad_type: "monster" | "spell" | "trap"
    const cardType = (info.found && info.broad_type) ? info.broad_type : null;

    // EX判定の優先順: card.is_ex（metadeck付与済み）> defaultIsEx（強制値）> API応答
    let finalIsEx;
    if (card.is_ex !== undefined) {
      finalIsEx = !!card.is_ex;
    } else if (defaultIsEx !== null) {
      finalIsEx = defaultIsEx;
    } else {
      // defaultIsEx === null → API応答で自動判定（loadDeckFromText で使用）
      finalIsEx = info.found ? !!info.is_ex : false;
    }

    const poolId = finalIsEx ? 'poolRow2' : 'poolRow';
    for (let i = 0; i < qty; i++) {
      addCardToPool(src, false, finalIsEx);
      // 追加直後に最後の wrapper を取ってリプレイ辞書・カード名・種別を登録
      const pool = document.getElementById(poolId);
      if (pool) {
        const lastWrapper = pool.querySelector('.tier-item-wrapper:last-child');
        const lastImg     = lastWrapper?.querySelector('img');
        if (lastImg?.id) {
          registerCardImage(lastImg.id, src);
          registerCardName(lastImg.id, name);  // リプレイ共有用に名前を記録
          setCardName(lastImg, name);           // カード詳細パネル用に名前を付与
          // セット時のモンスター/魔法罠判定に使う（モンスター=裏側守備、魔法罠=伏せ）
          if (cardType && lastWrapper) lastWrapper.dataset.cardType = cardType;
        }
      }
    }
  }
}

/**
 * テキストデッキを読み込んでプールに追加（事前にダミーをクリア）
 */
export async function loadDeckFromText(text) {
  const cards = parseDeckList(text);
  if (cards.length === 0) { alert('デッキリストが空です'); return; }

  await clearAllCards();

  const msg = document.getElementById('deckLoadingMsg');
  if (msg) { msg.textContent = 'カード画像を読み込み中...'; msg.style.display = 'block'; }
  // null=APIレスポンスのis_exでEX/メインを自動判定
  await addCardsBatch(cards, null);
  sortPoolCards('poolRow');  // 種別ソート（モンスター→魔法→罠）
  if (msg) { msg.textContent = ''; msg.style.display = 'none'; }
}

/**
 * EXデッキテキストをEXプールに追加（clearAllCardsは行わない）
 */
export async function loadExDeckFromText(text) {
  const cards = parseDeckList(text);
  if (cards.length === 0) return;

  const msg = document.getElementById('deckLoadingMsg');
  if (msg) { msg.textContent = 'EXデッキを読み込み中...'; msg.style.display = 'block'; }
  // isEx=true 固定でEXプールへ（card-info のis_ex判定不要）
  await addCardsBatch(cards, true);
  if (msg) { msg.textContent = ''; msg.style.display = 'none'; }
}

/**
 * 環境デッキAPIから全デッキ情報を取得して読み込む（事前にダミーをクリア）
 * @param {string} theme  テーマ名
 */
export async function loadMetaDeck(theme) {
  const msg = document.getElementById('deckLoadingMsg');
  if (msg) { msg.textContent = `${theme} のデッキを読み込み中...`; msg.style.display = 'block'; }

  try {
    const res = await fetch(`${API_META_DECK}?theme=${encodeURIComponent(theme)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const fullDeck = data.full_deck;  // [{name, qty, is_ex}]
    if (!fullDeck || fullDeck.length === 0) throw new Error('デッキリストが取得できませんでした');

    await clearAllCards();

    // is_ex が各カードに付与済みなので defaultIsEx=null にして card.is_ex を優先させる
    // addCardsBatch が card.is_ex !== undefined の場合それを使う
    await addCardsBatch(fullDeck, null);
    sortPoolCards('poolRow');  // 種別ソート（モンスター→魔法→罠）
  } catch (e) {
    alert(`環境デッキの読み込みに失敗しました: ${e.message}`);
  }

  if (msg) { msg.textContent = ''; msg.style.display = 'none'; }
}

/**
 * デッキ入力パネルを初期化
 */
export function initDeckInputPanel() {
  const container = document.getElementById('deckInputContainer');
  if (!container) return;

  // タブ切り替え
  container.querySelectorAll('.deck-input-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      container.querySelectorAll('.deck-input-tab').forEach(t => t.classList.remove('active'));
      container.querySelectorAll('.deck-input-pane').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      const paneId = tab.dataset.pane;
      const pane = document.getElementById(paneId);
      if (pane) pane.classList.add('active');

      // 環境デッキタブに切り替えた時はティアリストを取得
      if (paneId === 'deckPanePreset') loadMetaTierList();
      // マイデッキタブに切り替えた時は一覧を描画
      if (paneId === 'deckPaneMyDeck') renderMyDeckList();
    });
  });

  // テキスト読み込みボタン
  document.getElementById('deckLoadTextBtn')?.addEventListener('click', () => {
    const text = document.getElementById('deckTextarea')?.value || '';
    loadDeckFromText(text);
  });
}

/**
 * 環境デッキのティアリストを取得してUIに表示
 */
async function loadMetaTierList() {
  const listEl = document.getElementById('metaTierList');
  if (!listEl) return;
  if (listEl.dataset.loaded === 'true') return;

  listEl.innerHTML = '<span class="deck-loading-msg">読み込み中...</span>';
  try {
    const res = await fetch(API_META);
    const data = await res.json();
    const tiers = Array.isArray(data) ? data : (data.tiers || data.themes || []);
    if (tiers.length === 0) throw new Error('データなし');

    listEl.innerHTML = '';
    tiers.forEach(item => {
      const theme = item.theme || item.name || item;
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'deck-card-row';
      row.innerHTML =
        `<span class="deck-card-info"><span class="deck-card-name">${_esc(String(theme))}</span></span>`
        + `<span class="deck-card-hint">読込 ›</span>`;
      row.addEventListener('click', () => loadMetaDeck(theme));
      listEl.appendChild(row);
    });
    listEl.dataset.loaded = 'true';
  } catch (e) {
    listEl.innerHTML = `<span class="deck-loading-msg">取得失敗: ${e.message}</span>`;
  }
}

/**
 * マイデッキ一覧を描画（タブを開くたびに最新を反映）
 */
function renderMyDeckList() {
  const listEl = document.getElementById('myDeckList');
  if (!listEl) return;

  let decks = [];
  try {
    decks = JSON.parse(localStorage.getItem('cardprice_saved_decks') || '[]');
  } catch {
    decks = [];
  }

  if (decks.length === 0) {
    listEl.innerHTML =
      '<div class="mydeck-empty">'
      + '<p class="sol-upload-note" style="margin:0 0 8px">保存済みデッキがありません。<br>'
      + 'ニューロンの拡張機能またはPDFからデッキを取り込むと、ここに表示されます。</p>'
      + '<button type="button" class="mydeck-goto-neuron">ニューロンタブへ</button>'
      + '</div>';
    const btn = listEl.querySelector('.mydeck-goto-neuron');
    if (btn) {
      btn.addEventListener('click', () => {
        document.querySelector('[data-pane="deckPaneNeuron"]')?.click();
      });
    }
    return;
  }

  listEl.innerHTML = '';
  decks
    .slice()
    .sort((a, b) => (b.updated || 0) - (a.updated || 0))
    .forEach(deck => {
      const date = deck.updated
        ? new Date(deck.updated).toLocaleDateString('ja-JP', { month: '2-digit', day: '2-digit' })
        : '';

      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'deck-card-row';
      row.innerHTML =
        `<span class="deck-card-info">`
        + `<span class="deck-card-name">${_esc(deck.name || '無題')}</span>`
        + (date ? `<span class="deck-card-date">${date}</span>` : '')
        + `</span>`
        + `<span class="deck-card-hint">読込 ›</span>`;
      row.addEventListener('click', () => loadDeckFromText(deck.text || ''));
      listEl.appendChild(row);
    });
}

function _esc(str) {
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
