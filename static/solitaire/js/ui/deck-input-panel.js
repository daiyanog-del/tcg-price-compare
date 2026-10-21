/**
 * deck-input-panel.js
 * テキストデッキ / 環境メタデッキ読込UI
 * バッチAPI /api/card-images, /api/card-infos でデッキ全体を一括取得し高速化
 */

import { addCardToPool, sortPoolCards } from '../components/card-manager.js';
import { registerCardImage, registerCardName, registerCardType, resetReplay, getLogLength, getSetupLogs } from '../services/replay-service.js';
import { setCardName } from './card-info-panel.js';
import { clearSessionResume } from '../utils/session-resume-store.js';
import { setDeckLink, getDeckLink, onDeckLinkChange } from '../services/deck-link.js';
import { isMobilePortrait, watchMobilePortrait } from '../utils/viewport.js';
import { showToast } from '../utils/toast.js';
import { showActionToast } from './inline-ui.js';
import { saveUndoPoint, offerUndo, discardUndoPoint } from './undo-point.js';

const API_CARD_IMAGE  = '/api/card-image';   // 単数版（フォールバック用）
const API_CARD_IMAGES = '/api/card-images';  // バッチ版（POST）
const API_CARD_INFO   = '/api/card-info';    // 単数版（フォールバック用）
const API_CARD_INFOS  = '/api/card-infos';   // バッチ版（POST）
const API_META = '/api/meta?include_metadata=1';
const API_META_DECK = '/api/meta/deck';

// ── デッキ読込の多重防止（バグ修正・PCにも効く） ──
// 「読込 ›」連打や下部バーからの多重発火で、同じデッキが二重に読み込まれるのを防ぐ。
let _deckLoadInFlight = false;

/**
 * 一覧の読込ボタン全て（.deck-card-row）を disabled にし、
 * 押した行（triggerBtn）だけ文言を「読込中…」に切り替える。
 * @param {HTMLElement|null} triggerBtn  実際に押されたボタン（.deck-card-row想定。無ければ文言変更はスキップ）
 * @param {boolean}           loading
 */
function _setDeckRowsLoading(triggerBtn, loading) {
  document.querySelectorAll('.deck-card-row').forEach(btn => { btn.disabled = loading; });

  const hint = triggerBtn?.querySelector?.('.deck-card-hint');
  if (!hint) return;
  if (loading) {
    hint.dataset.origText = hint.textContent;
    hint.textContent = '読込中…';
  } else if (hint.dataset.origText !== undefined) {
    hint.textContent = hint.dataset.origText;
    delete hint.dataset.origText;
  }
}

/** 現在のプール枚数から { main, ex } を返す（sol-deck-load-end の detail 用） */
function _currentPoolCounts() {
  return {
    main: document.querySelectorAll('#poolRow .tier-item').length,
    ex: document.querySelectorAll('#poolRow2 .tier-item').length,
  };
}

// ── マイデッキ保存（localStorage）── index.html と同じキーを共有する。
// 端末間同期（P2・docs/design-sync-2026-08-09.md §11）: 保存デッキの読み書きは
// この2関数に集約し、他の場所から直接 localStorage を触らない
// （sync-client.js の pull/merge 適用も window.savedDecksSet 経由でここを呼ぶ）
const SAVED_DECKS_KEY = 'cardprice_saved_decks';

export function savedDecksGet() {
  let raw;
  try { raw = JSON.parse(localStorage.getItem(SAVED_DECKS_KEY)); } catch { return []; }
  if (raw == null) return [];
  // 読み取り時の正規化のみ（保存側へは書き戻さない）。壊れたデータで初期化を止めない
  if (!Array.isArray(raw)) {
    console.warn('[deck-input-panel] 保存デッキが配列ではないため無視しました');
    return [];
  }
  const list = raw
    .filter(d => d && typeof d === 'object' && !Array.isArray(d))
    .map(d => ({ ...d, name: String(d.name ?? '') }));
  if (list.length !== raw.length) {
    console.warn(`[deck-input-panel] 保存デッキの不正な要素を${raw.length - list.length}件無視しました`);
  }
  return list;
}

export function savedDecksSet(list) {
  localStorage.setItem(SAVED_DECKS_KEY, JSON.stringify(list));
  _updateMyDeckCount(list.length);
  // 端末間同期（P2）。sync-client.js 側でデバウンス・無限ループ防止を行う
  if (window.SyncClient) window.SyncClient.onDecksSave(list);
}

// sync-client.js（非モジュール・window.SyncClient経由）から呼べるよう window に公開する。
// pull/merge結果の適用時に SyncClient がこの2つを呼ぶ（設計文書 §7.4 と同じ経路）
window.savedDecksSet = savedDecksSet;
window.renderSavedDecks = renderMyDeckList;

/**
 * デッキリストテキストをパース
 * "3 灰流うらら" 形式 → [{qty, name}]
 */
export function parseDeckList(text) {
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

// 記録のあるデッキを入れ替えている途中（読み込みが終わったら「元に戻す」を出す）
let _replaceUndoPending = false;

/**
 * 新しいデッキに入れ替える前の準備。記録（setup込みで1手以上）があるときは、以前は確認窓を出していたが、
 * 今は直前の写しを取っておき、読み込みが終わったら6秒間「元に戻す」を出す（undo-point.js）。
 * 入れ替えると、前のデッキのカードを指す記録は再生しても何も動かなくなり、共有すると
 * 前のデッキのカードまで受け手のデッキに混ざるため、入れ替えと同時に記録を消す（_clearForNewDeck）。
 * 呼んだ後は必ず sol-deck-load-end を出すこと（3つの読み込みとも finally で出している）。
 * @returns {boolean} 続けてよければ true（今は常に true。呼び出し側の形を変えないため残す）
 */
function _confirmReplaceDeck() {
  if (getLogLength() + getSetupLogs().length === 0) return true;
  if (!saveUndoPoint()) return true;
  _replaceUndoPending = true;
  document.addEventListener('sol-deck-load-end', (e) => {
    _replaceUndoPending = false;
    const d = (e && e.detail) || {};
    if (d.ok) offerUndo('デッキを読み込みました（前の記録は消えました）');
    else if (d.error) offerUndo('デッキを読み込めませんでした: ' + d.error);
    else discardUndoPoint();
  }, { once: true });
  return true;
}

/**
 * 新しいデッキを読み込む直前の片付け。盤面・プールのカードに加えて、リプレイの記録と
 * カードの辞書（画像・名前・EX判定・種類）も初期化する。
 * 以前は clearAllCards だけで記録と辞書が残り、読み込み直すたびに前のデッキの分が辞書に
 * 積み重なっていた（2026-10-15 ユーザー報告: 記録を再生してもカードが動かない／共有リンクで
 * デッキとEXデッキが崩れる。原因の記録は同じデッキ4回分の辞書を持っていた）。
 */
async function _clearForNewDeck() {
  await clearAllCards();
  resetReplay();
}

/**
 * デッキ・盤面・リプレイ記録をすべて消去する（スマホ⋯メニュー「すべて消去」／PC版「すべて消去」用）。
 * loadDeckFromText 等のデッキ読込中に呼ばれた場合は、読込処理と競合しないよう何もしない。
 * @returns {Promise<boolean>} 実際に消去を実行したら true、読込中で無視したら false
 */
export async function clearEverything() {
  if (_deckLoadInFlight) {
    console.warn('[deck-input-panel] デッキ読込が進行中のため「すべて消去」を無視しました');
    return false;
  }
  await _clearForNewDeck();       // 盤面・プールのカードと、リプレイ記録・辞書を初期化
  clearSessionResume(sessionStorage); // 退避と「記録を落とした」フラグを両方消す。 次回アクセス時の自動復元を止める
  document.dispatchEvent(new CustomEvent('sol-board-cleared'));
  return true;
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
        if (cardType && lastWrapper) {
          lastWrapper.dataset.cardType = cardType;
          registerCardType(lastCardEl.id, cardType); // リプレイの再生・復元で付け直せるよう辞書にも登録
        }
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
        // 未発売の公式SAMPLE画像カードは画像が kind:'image' になるため、
        // card-infos 側の kind:'proxy'（found:false）の is_ex で判定する
        finalIsEx = (info.found || info.kind === 'proxy') ? !!info.is_ex : false;
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
          if (cardType && lastWrapper) {
            lastWrapper.dataset.cardType = cardType;
            registerCardType(lastCardEl.id, cardType); // リプレイの再生・復元で付け直せるよう辞書にも登録
          }
        }
      }
    }
  }
}

/**
 * L-10: テキストデッキを読み込んでプールに追加（事前にダミーをクリア）
 * 多重防止: 進行中に再度呼ばれた場合は何もしない（バグ修正・PCにも効く）。
 * 呼び出し元: マイデッキ一覧（renderMyDeckList）の行 → 実際に押された
 * .deck-card-row 要素を triggerBtn として渡す。
 * サンプルデッキ一覧（環境デッキ）は本関数ではなく loadMetaDeck を使う。
 * @param {string}            text
 * @param {HTMLElement|null} [triggerBtn]  マイデッキ一覧から呼ぶ場合のみ渡す押された行。
 *   渡すとその行の文言だけ「読込中…」に変わる。
 */
export async function loadDeckFromText(text, triggerBtn = null) {
  if (_deckLoadInFlight) {
    console.warn('[deck-input-panel] デッキ読込が進行中のため無視しました（loadDeckFromText）');
    return;
  }
  const cards = parseDeckList(text);
  if (cards.length === 0) { showToast('デッキリストが空です'); return; }
  if (!_confirmReplaceDeck()) return;

  _deckLoadInFlight = true;
  _setDeckRowsLoading(triggerBtn, true);
  // replace: デッキを入れ替える読込か（deck-edit-ui.js がマイデッキとのひも付けを外すのに使う）
  document.dispatchEvent(new CustomEvent('sol-deck-load-start', { detail: { replace: true } }));
  let ok = false;
  let error = null;
  try {
    await _clearForNewDeck();

    // デッキ・EXデッキ両エリアにロード中インジケータを表示（EXカードも振り分けられるため）
    setPoolLoading(['imagePool', 'imagePool2'], true);
    try {
      // null=APIレスポンスのis_exでEX/メインを自動判定
      await addCardsBatch(cards, null);
      sortPoolCards('poolRow');  // 種別ソート（モンスター→魔法→罠）
      ok = true;
    } finally {
      setPoolLoading(['imagePool', 'imagePool2'], false);
    }
  } catch (e) {
    error = e?.message || String(e);
    throw e;
  } finally {
    _deckLoadInFlight = false;
    _setDeckRowsLoading(triggerBtn, false);
    const { main, ex } = _currentPoolCounts();
    document.dispatchEvent(new CustomEvent('sol-deck-load-end', { detail: { ok, main, ex, error } }));
  }
  return ok;
}

/**
 * EXデッキテキストをEXプールに追加（clearAllCardsは行わない）
 * L-8: 単体で呼ばれた場合も _deckLoadInFlight ガードとイベント発行に参加する
 * （ニューロン経路のようにメインと連続で呼ぶ場合は loadDeckFromNeuron を使うこと。
 * こちらを直接メイン読込の直後に呼ぶと、メイン側が出す sol-deck-load-end との間で
 * 中間状態のイベントが挟まってしまう）。
 * @param {string}            text
 * @param {HTMLElement|null} [triggerBtn]
 */
export async function loadExDeckFromText(text, triggerBtn = null) {
  if (_deckLoadInFlight) {
    console.warn('[deck-input-panel] デッキ読込が進行中のため無視しました（loadExDeckFromText）');
    return;
  }
  const cards = parseDeckList(text);
  if (cards.length === 0) return;

  _deckLoadInFlight = true;
  _setDeckRowsLoading(triggerBtn, true);
  document.dispatchEvent(new CustomEvent('sol-deck-load-start', { detail: { replace: false } }));
  let ok = false;
  let error = null;
  try {
    // EXエリアのみロード中インジケータを表示
    setPoolLoading(['imagePool2'], true);
    try {
      // isEx=true 固定でEXプールへ（card-info のis_ex判定不要）
      await addCardsBatch(cards, true);
      ok = true;
    } finally {
      setPoolLoading(['imagePool2'], false);
    }
  } catch (e) {
    error = e?.message || String(e);
    throw e;
  } finally {
    _deckLoadInFlight = false;
    _setDeckRowsLoading(triggerBtn, false);
    const { main, ex } = _currentPoolCounts();
    document.dispatchEvent(new CustomEvent('sol-deck-load-end', { detail: { ok, main, ex, error } }));
  }
}

/**
 * L-8: ニューロン経由（PDF取込／Chrome拡張）のメイン→EX逐次読込を1組の
 * sol-deck-load-start/end イベントにまとめる。loadDeckFromText → loadExDeckFromText を
 * そのまま連続呼び出しすると、メイン完了時点で「EX 0」の中間トーストが出てしまうため、
 * event-handlers.js のニューロン取込み経路（onLoad）はこちらを呼ぶ。
 * @param {string} mainText
 * @param {string} exText
 */
export async function loadDeckFromNeuron(mainText, exText) {
  if (_deckLoadInFlight) {
    console.warn('[deck-input-panel] デッキ読込が進行中のため無視しました（loadDeckFromNeuron）');
    return;
  }
  const mainCards = mainText ? parseDeckList(mainText) : [];
  const exCards = exText ? parseDeckList(exText) : [];
  if (mainCards.length === 0 && exCards.length === 0) return;
  // メインがあるときだけデッキを入れ替える（EXだけのときは足すだけなので記録は消さない）
  if (mainCards.length > 0 && !_confirmReplaceDeck()) return;

  _deckLoadInFlight = true;
  _setDeckRowsLoading(null, true);
  document.dispatchEvent(new CustomEvent('sol-deck-load-start', { detail: { replace: mainCards.length > 0 } }));
  let ok = false;
  let error = null;
  try {
    if (mainCards.length > 0) {
      await _clearForNewDeck();
      setPoolLoading(['imagePool', 'imagePool2'], true);
      try {
        await addCardsBatch(mainCards, null);
        sortPoolCards('poolRow');
      } finally {
        setPoolLoading(['imagePool', 'imagePool2'], false);
      }
    }
    if (exCards.length > 0) {
      setPoolLoading(['imagePool2'], true);
      try {
        await addCardsBatch(exCards, true);
      } finally {
        setPoolLoading(['imagePool2'], false);
      }
    }
    ok = true;
  } catch (e) {
    error = e?.message || String(e);
    throw e;
  } finally {
    _deckLoadInFlight = false;
    _setDeckRowsLoading(null, false);
    const { main, ex } = _currentPoolCounts();
    document.dispatchEvent(new CustomEvent('sol-deck-load-end', { detail: { ok, main, ex, error } }));
  }
  return ok;
}

/**
 * 環境デッキAPIから全デッキ情報を取得して読み込む（事前にダミーをクリア）
 * 多重防止: 進行中に再度呼ばれた場合は何もしない（バグ修正・PCにも効く）。
 * @param {string}            theme  テーマ名
 * @param {HTMLElement|null} [triggerBtn]  押されたボタン（.deck-card-row）。渡すとその行の
 *   文言だけ「読込中…」に変わる。
 */
export async function loadMetaDeck(theme, triggerBtn = null) {
  if (_deckLoadInFlight) {
    console.warn('[deck-input-panel] デッキ読込が進行中のため無視しました（loadMetaDeck）');
    return;
  }
  if (!_confirmReplaceDeck()) return;
  _deckLoadInFlight = true;
  _setDeckRowsLoading(triggerBtn, true);
  document.dispatchEvent(new CustomEvent('sol-deck-load-start', { detail: { replace: true } }));

  // デッキ・EXデッキ両エリアにロード中インジケータを表示
  setPoolLoading(['imagePool', 'imagePool2'], true);

  let ok = false;
  let error = null;
  try {
    const res = await fetch(`${API_META_DECK}?theme=${encodeURIComponent(theme)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const fullDeck = data.full_deck;  // [{name, qty, is_ex}]
    if (!fullDeck || fullDeck.length === 0) throw new Error('デッキリストが取得できませんでした');

    await _clearForNewDeck();

    // is_ex が各カードに付与済みなので defaultIsEx=null にして card.is_ex を優先させる
    // addCardsBatch が card.is_ex !== undefined の場合それを使う
    await addCardsBatch(fullDeck, null);
    sortPoolCards('poolRow');  // 種別ソート（モンスター→魔法→罠）
    ok = true;
  } catch (e) {
    error = e?.message || String(e);
    // 記録を消して入れ替えている途中なら、読み込み終わりの「元に戻す」の知らせに失敗の理由を載せる（重ねて出さない）
    if (!_replaceUndoPending) showActionToast(`環境デッキの読み込みに失敗しました: ${e.message}`, { error: true });
  } finally {
    setPoolLoading(['imagePool', 'imagePool2'], false);
    _deckLoadInFlight = false;
    _setDeckRowsLoading(triggerBtn, false);
    const { main, ex } = _currentPoolCounts();
    document.dispatchEvent(new CustomEvent('sol-deck-load-end', { detail: { ok, main, ex, error } }));
  }
  if (ok) setDeckLink({ name: theme, source: 'sample' }); // 環境デッキはマイデッキに無いので未登録として始める
  return ok;
}

/** デッキを読み込んでいる途中か（途中で数えると EX などが欠けるため、デッキ画像の作成はこの間止める） */
export function isDeckLoading() {
  return _deckLoadInFlight;
}

/**
 * 今のデッキにカードを足す（デッキ編集の「デッキに入れる」「デッキに1枚足す」）。
 * デッキは入れ替えず、盤面・記録もそのまま。読込と同じ addCardsBatch で画像・種別・EX判定・辞書登録を行う。
 * 足したカードはデッキ欄（EX なら EX デッキ欄）に入り、種別順に並べ直す。
 * @param {Array<{qty:number, name:string}>} cards
 * @returns {Promise<Element[]|null>} 足したカードの .tier-item-wrapper（画像が取れず足せなければ空配列）。
 *   別の読込が進行中で何もしなかったときは null。
 */
export async function addCardsToDeck(cards) {
  if (_deckLoadInFlight) {
    console.warn('[deck-input-panel] デッキ読込が進行中のため無視しました（addCardsToDeck）');
    return null;
  }
  // 「新しく作られたカード」で見分ける（デッキ欄に戻ってきただけの既存カードを、足したカードと取り違えないため）
  const before = new Set(document.querySelectorAll('.tier-item-wrapper'));
  const names = new Set(cards.map(c => c.name));
  _deckLoadInFlight = true;
  try {
    await addCardsBatch(cards, null);
  } finally {
    _deckLoadInFlight = false;
  }
  const added = [...document.querySelectorAll('#poolRow .tier-item-wrapper, #poolRow2 .tier-item-wrapper')]
    .filter(w => !before.has(w) && names.has(w.querySelector('.tier-item')?.dataset.cardName));
  if (added.some(w => w.parentElement?.id === 'poolRow')) sortPoolCards('poolRow');
  return added;
}

/**
 * デッキ入力パネルを初期化。
 * 2026-10-20 サイドバー整理: 「マイデッキ」「サンプルデッキ」のボタンを押すと一覧の窓（.deck-pick-pop）を開く。
 * PC はサイドバーの右に重ねる窓（外を押す・Esc・×・読み込みで閉じる）。縦向きスマホのデッキ読込シートでは
 * 窓を浮かせず、ボタンの下にその場で開く（mobile.css。シートの中なので外を押しても閉じない）。
 */
export function initDeckInputPanel() {
  const container = document.getElementById('deckInputContainer');
  if (!container) return;

  container.querySelectorAll('[data-pane]').forEach(btn => {
    btn.addEventListener('click', () => {
      const paneId = btn.dataset.pane;
      if (_openPaneId === paneId) closeDeckPicker();
      else _openDeckPicker(paneId);
    });
  });
  container.querySelectorAll('.deck-pick-pop-close').forEach(b => b.addEventListener('click', closeDeckPicker));

  // 外を押したら閉じる（PC の浮いた窓のときだけ）。ボタン自身の押下はボタンのクリックで開閉する
  document.addEventListener('pointerdown', (e) => {
    if (!_openPaneId || isMobilePortrait()) return;
    const pane = document.getElementById(_openPaneId);
    if (pane?.contains(e.target) || e.target.closest?.(`[data-pane="${_openPaneId}"]`)) return;
    closeDeckPicker();
  }, true);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && _openPaneId && !isMobilePortrait()) closeDeckPicker();
  });
  // 窓の位置はボタンの位置から決めるので、画面の大きさが変わったら置き直す
  window.addEventListener('resize', () => { if (_openPaneId) _placeDeckPicker(_openPaneId); });
  // 縦向きスマホとの切り替えでは置き場所（body 直下／シートの中）が変わるので、一度閉じる
  watchMobilePortrait((portrait) => {
    closeDeckPicker();
    if (portrait) _openDeckPicker('deckPaneMyDeck');
  });
  document.getElementById('solSidebar')?.addEventListener('scroll', () => { if (_openPaneId) _placeDeckPicker(_openPaneId); });

  // 今のデッキが変わったら「使用中」を付け直す（縦向きスマホのシートは開いたまま残り、描き直されないため）
  onDeckLinkChange(() => { _markCurrentMyDeck(); _markCurrentMeta(); });

  renderMyDeckList();
  // 縦向きスマホのデッキ読込シートは、今までどおりマイデッキの一覧を開いた状態で始める
  if (isMobilePortrait()) _openDeckPicker('deckPaneMyDeck');
}

/** いま開いている窓の id（無ければ null） */
let _openPaneId = null;
/** body 直下へ移した窓の元の場所（閉じたら戻す） */
const _paneHome = new Map();

function _openDeckPicker(paneId) {
  closeDeckPicker();
  const pane = document.getElementById(paneId);
  if (!pane) return;
  _openPaneId = paneId;
  // PC: 左サイドバーは独立した重なり順（sticky）を持ち、中の窓は盤面の下に隠れる。開いている間だけ body 直下へ移す
  if (!isMobilePortrait()) {
    _paneHome.set(pane, { parent: pane.parentNode, next: pane.nextSibling });
    document.body.appendChild(pane);
  }
  pane.hidden = false;
  const btn = document.querySelector(`#deckInputContainer [data-pane="${paneId}"]`);
  btn?.classList.add('is-active');
  btn?.setAttribute('aria-expanded', 'true');
  if (paneId === 'deckPaneMyDeck') renderMyDeckList();
  if (paneId === 'deckPanePreset') { loadMetaTierList(); _markCurrentMeta(); }
  _placeDeckPicker(paneId);
  // PC は窓を body の末尾へ移すので、Tab でたどり着けるよう窓へフォーカスを移す（閉じたらボタンへ戻す）
  if (!isMobilePortrait()) pane.querySelector('.deck-pick-pop-close')?.focus({ preventScroll: true });
}

/** デッキの窓を閉じる（デッキを読み込んだ後にも呼ぶ） */
export function closeDeckPicker() {
  if (!_openPaneId) return;
  const pane = document.getElementById(_openPaneId);
  const btn = document.querySelector(`#deckInputContainer [data-pane="${_openPaneId}"]`);
  if (pane?.contains(document.activeElement)) btn?.focus({ preventScroll: true });
  if (pane) {
    pane.hidden = true;
    const home = _paneHome.get(pane);
    if (home) { home.parent.insertBefore(pane, home.next); _paneHome.delete(pane); }
  }
  btn?.classList.remove('is-active');
  btn?.setAttribute('aria-expanded', 'false');
  _openPaneId = null;
}

/** PC: サイドバーの右に、ボタンの高さに合わせて置く（画面の下にはみ出すなら上へずらす）。スマホのシートでは置かない */
function _placeDeckPicker(paneId) {
  const pane = document.getElementById(paneId);
  if (!pane) return;
  if (isMobilePortrait()) { pane.style.left = ''; pane.style.top = ''; return; }
  const btn = document.querySelector(`#deckInputContainer [data-pane="${paneId}"]`);
  const side = document.getElementById('solSidebar');
  if (!btn || !side) return;
  const margin = 16;
  const b = btn.getBoundingClientRect();
  pane.style.left = `${side.getBoundingClientRect().right + 8}px`;
  const h = pane.offsetHeight;
  pane.style.top = `${Math.max(margin, Math.min(b.top - 8, window.innerHeight - h - margin))}px`;
}

/** 読み込めたら、読み込みを始めた窓を閉じる（PC。読込中に別の窓へ切り替えていたらそちらは閉じない）。
 *  縦向きスマホのシートは閉じずに残す（シートの開け閉めは mobile-ui.js） */
function _closeAfterLoad(paneId) {
  if (!isMobilePortrait() && _openPaneId === paneId) closeDeckPicker();
}

/** マイデッキのボタンの丸い数字（保存件数。0件なら出さない） */
function _updateMyDeckCount(n) {
  const el = document.getElementById('myDeckCount');
  if (!el) return;
  el.textContent = String(n);
  el.hidden = !n;
}

/** 一覧の行に「使用中」の印を付ける（いま並んでいるデッキの出どころと同じ行） */
function _setRowCurrent(row, current) {
  row.classList.toggle('is-current', current);
  const hint = row.querySelector('.deck-card-hint');
  if (hint) hint.textContent = current ? '使用中' : '読込 ›';
}

/** マイデッキの一覧の「使用中」を付け直す（描き直さない。読込中の行の「読込中…」を消さないため） */
function _markCurrentMyDeck() {
  const link = getDeckLink();
  document.querySelectorAll('#myDeckList .deck-card-row').forEach(row => {
    if (row.disabled) return; // 読込中の行は読み込み処理が戻す
    _setRowCurrent(row, !!link?.savedId && link.savedId === row.dataset.savedId);
  });
}

/** サンプルデッキの一覧の「使用中」を付け直す（一覧は一度だけ取得するので、開くたびに見直す） */
function _markCurrentMeta() {
  const link = getDeckLink();
  document.querySelectorAll('#metaTierList .deck-card-row').forEach(row => {
    if (row.disabled) return; // 読込中（「読込中…」を出している間）は読み込み処理が戻す
    _setRowCurrent(row, !!link && !link.savedId && link.source === 'sample' && link.name === row.dataset.theme);
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
  let rendered = false;
  try {
    const res = await fetch(API_META);
    if (!res.ok) throw new Error('環境データ取得失敗');
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
      row.dataset.theme = String(theme);
      row.addEventListener('click', async () => {
        if (await loadMetaDeck(theme, row)) _closeAfterLoad('deckPanePreset');
      });
      listEl.appendChild(row);
    });
    rendered = true;
    // 更新失敗時の退避データはタブを再度開いたときに再取得する。
    const stale = data.metadata?.stale || data.metadata?.refresh_error;
    listEl.dataset.loaded = stale ? 'false' : 'true';
    if (stale) {
      const note = document.createElement('span');
      note.className = 'deck-loading-msg';
      note.textContent = '更新に失敗したため、以前取得した環境データを表示しています';
      listEl.appendChild(note);
    }
  } catch (e) {
    listEl.innerHTML = `<span class="deck-loading-msg">取得失敗: ${e.message}</span>`;
  }
  // 「使用中」の印と窓の位置は、取得の成否と分けて後から付ける（ここで起きた不具合を「取得失敗」と出さない）
  if (rendered) {
    _markCurrentMeta();
    if (_openPaneId === 'deckPanePreset') _placeDeckPicker('deckPanePreset'); // 中身が入って高さが変わったので置き直す
  }
}

/**
 * マイデッキ一覧を描画（窓を開くたびに最新を反映。端末間同期の適用後にも window.renderSavedDecks で呼ばれる）。
 * ボタンの件数も直す。ニューロン取り込みの入口は窓の下（#myDeckImportFoot）に置く。
 */
function renderMyDeckList() {
  const listEl = document.getElementById('myDeckList');
  if (!listEl) return;
  const foot = document.getElementById('myDeckImportFoot');

  const decks = savedDecksGet();
  _updateMyDeckCount(decks.length);

  if (decks.length === 0) {
    listEl.innerHTML =
      '<div class="mydeck-empty">'
      + '<p class="mydeck-empty-title">まだデッキがありません</p>'
      + _importEntryHtml(true)
      + '</div>';
    if (foot) { foot.replaceChildren(); foot.hidden = true; }
    return;
  }

  const link = getDeckLink();
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
      row.dataset.savedId = deck.id || '';
      _setRowCurrent(row, !!link?.savedId && link.savedId === deck.id);
      // 読み込めたら、このマイデッキにひも付ける（以後の「変更を保存」はこのデッキへ上書き）
      row.addEventListener('click', async () => {
        if (await loadDeckFromText(deck.text || '', row)) {
          setDeckLink({ savedId: deck.id || null, name: deck.name || '無題' });
          _closeAfterLoad('deckPaneMyDeck');
        }
      });
      listEl.appendChild(row);
    });

  // 窓の下に取り込みの入口
  if (foot) { foot.innerHTML = _importEntryHtml(false); foot.hidden = false; }
  if (_openPaneId === 'deckPaneMyDeck') _placeDeckPicker('deckPaneMyDeck');
}

/** ニューロン取り込みの入口（マイデッキページへのリンク）と補足 */
function _importEntryHtml(big) {
  return `<a class="mydeck-import-entry${big ? ' big' : ''}" href="/#mydeck-import">＋ ニューロンから<span class="mydeck-import-word">取り込む</span></a>`
    + '<small class="mydeck-import-note">取り込んで保存すると、ここに並びます</small>';
}

function _esc(str) {
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
