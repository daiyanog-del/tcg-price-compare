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
 * デッキ/EXデッキエリアのロード中インジケータを表示・非表示する
 * @param {string[]} poolIds  対象プールの id（'imagePool' | 'imagePool2'）
 * @param {boolean}  on       true=表示 / false=非表示
 */
function setPoolLoading(poolIds, on) {
  poolIds.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle('is-loading', on);
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

  // API レスポンス: kind を見て srcOrDisplay を決定する
  let srcOrDisplay = null;
  try {
    if (imageRes?.ok) {
      const data = await imageRes.json();
      const kind = data.kind || (data.url ? 'image' : 'none');
      if (kind === 'image' && data.url) {
        if (data.source === 'official_sample') {
          // 未発売SAMPLE画像: object で渡し、透かしを付けない（createCardElement が判定）
          srcOrDisplay = { kind: 'image', url: data.url, source: 'official_sample' };
        } else {
          // 発売済みクリーン画像: 従来どおり URL 文字列として扱う（透かし対象）
          srcOrDisplay = data.url;
        }
      } else if (kind === 'proxy') {
        // 未発売プロキシ
        srcOrDisplay = { kind: 'proxy', proxy: data.proxy || {} };
      }
      // kind === 'none' は srcOrDisplay = null のままスキップ
    }
  } catch { /* 無視 */ }

  // broad_type: "monster" | "spell" | "trap"
  let cardType = null;
  let finalIsEx = isEx ?? false;  // null の場合は false をデフォルトにした上で上書き
  try {
    if (infoRes?.ok) {
      const data = await infoRes.json();
      // kind が 'proxy' の場合は proxy データから取得
      const proxyData = data.proxy;
      if (proxyData && data.kind === 'proxy') {
        cardType = proxyData.broad_type || null;
        if (isEx == null) finalIsEx = !!proxyData.is_ex;
      } else if (data.found && data.broad_type) {
        cardType = data.broad_type;
        // isEx が null（自動判定モード）の場合のみ API レスポンスの is_ex を使用
        if (isEx == null && data.found) finalIsEx = !!data.is_ex;
      }
    }
  } catch { /* 無視 */ }

  if (!srcOrDisplay) {
    // kind === 'none'（pending/rejected 等）はスキップ
    console.warn(`画像取得失敗またはスキップ: ${name}`);
    return;
  }

  const poolId = finalIsEx ? 'poolRow2' : 'poolRow';
  // addCardToPool は srcOrDisplay を createCardElement に渡す
  for (let i = 0; i < qty; i++) {
    addCardToPool(srcOrDisplay, false, finalIsEx);
    // 追加直後に最後の wrapper を取ってリプレイ辞書・カード名・種別を登録
    const pool = document.getElementById(poolId);
    if (pool) {
      const lastWrapper = pool.querySelector('.tier-item-wrapper:last-child');
      // プロキシは div.tier-item、画像は img.tier-item のどちらでも id を取れる
      const lastCardEl  = lastWrapper?.querySelector('.tier-item');
      if (lastCardEl?.id) {
        // リプレイ辞書への登録:
        //   発売済み（URL文字列）    -> 実URL（後方互換）
        //   未発売プロキシ or 取込画像 -> 'card://カード名'（再解決センチネル）
        const regSrc = typeof srcOrDisplay === 'string' ? srcOrDisplay : `card://${name}`;
        registerCardImage(lastCardEl.id, regSrc);
        registerCardName(lastCardEl.id, name);  // リプレイ共有用に名前を記録
        setCardName(lastCardEl, name);  // カード詳細パネル用に名前を付与
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

    // images[name] は以下のいずれか:
    //   - 文字列 URL（発売済みクリーン画像 / カーナベルフォールバック）   → 従来どおり
    //   - {url, kind:'image', source:'official_sample'}（未発売SAMPLE画像） → 多態対応
    //   - {url:null, kind:'proxy', proxy:{...}}（未発売プロキシ）          → 多態対応
    //   - null（pending/rejected/取得失敗）                              → スキップ
    const imgVal = images[name] ?? null;

    // kind === 'none' 相当（null または明示的な none オブジェクト）はスキップ
    if (!imgVal || (typeof imgVal === 'object' && imgVal.kind === 'none')) {
      console.warn(`画像取得失敗またはスキップ: ${name}`);
      continue;
    }

    // srcOrDisplay: createCardElement に渡す値を確定する
    let srcOrDisplay;
    if (typeof imgVal === 'string') {
      // 発売済みクリーン画像: URL 文字列（後方互換）
      srcOrDisplay = imgVal;
    } else if (imgVal.kind === 'image' || imgVal.kind === 'proxy') {
      // 未発売SAMPLE画像（kind:'image'）/ 未発売プロキシ: オブジェクトのまま渡す
      // （createCardElement が source を見て透かし・表示を出し分ける）
      srcOrDisplay = imgVal;
    } else {
      // 予期しない形式: スキップ
      console.warn(`不明な images 形式のためスキップ: ${name}`);
      continue;
    }

    const info = infos[name] || {};
    // broad_type: "monster" | "spell" | "trap"
    // 未発売カード（kind='proxy'）は proxy データから取得する
    let cardType = null;
    if (imgVal?.kind === 'proxy' && imgVal.proxy?.broad_type) {
      cardType = imgVal.proxy.broad_type;
    } else if (info.found && info.broad_type) {
      cardType = info.broad_type;
    } else if (info.kind === 'proxy' && info.proxy?.broad_type) {
      // card-infos バッチが proxy を返した場合
      cardType = info.proxy.broad_type;
    }

    // EX判定の優先順: card.is_ex（metadeck付与済み）> defaultIsEx（強制値）> API応答
    let finalIsEx;
    if (card.is_ex !== undefined) {
      finalIsEx = !!card.is_ex;
    } else if (defaultIsEx !== null) {
      finalIsEx = defaultIsEx;
    } else {
      // defaultIsEx === null → API応答で自動判定（loadDeckFromText で使用）
      if (imgVal?.kind === 'proxy' && imgVal.proxy) {
        finalIsEx = !!imgVal.proxy.is_ex;
      } else {
        finalIsEx = info.found ? !!info.is_ex : false;
      }
    }

    const poolId = finalIsEx ? 'poolRow2' : 'poolRow';
    for (let i = 0; i < qty; i++) {
      addCardToPool(srcOrDisplay, false, finalIsEx);
      // 追加直後に最後の wrapper を取ってリプレイ辞書・カード名・種別を登録
      const pool = document.getElementById(poolId);
      if (pool) {
        const lastWrapper = pool.querySelector('.tier-item-wrapper:last-child');
        // プロキシは div.tier-item、画像は img.tier-item のどちらでも .tier-item で取れる
        const lastCardEl  = lastWrapper?.querySelector('.tier-item');
        if (lastCardEl?.id) {
          // リプレイ辞書への登録:
          //   発売済み（URL文字列）    -> 実URL（後方互換）
          //   未発売プロキシ or 取込画像 -> 'card://カード名'（再解決センチネル）
          const regSrc = typeof srcOrDisplay === 'string' ? srcOrDisplay : `card://${name}`;
          registerCardImage(lastCardEl.id, regSrc);
          registerCardName(lastCardEl.id, name);  // リプレイ共有用に名前を記録
          setCardName(lastCardEl, name);           // カード詳細パネル用に名前を付与
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

  // デッキ・EXデッキ両エリアにロード中インジケータを表示（EXカードも振り分けられるため）
  setPoolLoading(['imagePool', 'imagePool2'], true);
  try {
    // null=APIレスポンスのis_exでEX/メインを自動判定
    await addCardsBatch(cards, null);
    sortPoolCards('poolRow');  // 種別ソート（モンスター→魔法→罠）
  } finally {
    setPoolLoading(['imagePool', 'imagePool2'], false);
  }
}

/**
 * EXデッキテキストをEXプールに追加（clearAllCardsは行わない）
 */
export async function loadExDeckFromText(text) {
  const cards = parseDeckList(text);
  if (cards.length === 0) return;

  // EXエリアのみロード中インジケータを表示
  setPoolLoading(['imagePool2'], true);
  try {
    // isEx=true 固定でEXプールへ（card-info のis_ex判定不要）
    await addCardsBatch(cards, true);
  } finally {
    setPoolLoading(['imagePool2'], false);
  }
}

/**
 * 環境デッキAPIから全デッキ情報を取得して読み込む（事前にダミーをクリア）
 * @param {string} theme  テーマ名
 */
export async function loadMetaDeck(theme) {
  // デッキ・EXデッキ両エリアにロード中インジケータを表示
  setPoolLoading(['imagePool', 'imagePool2'], true);

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
  } finally {
    setPoolLoading(['imagePool', 'imagePool2'], false);
  }
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
