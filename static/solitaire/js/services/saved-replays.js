/**
 * saved-replays.js
 * リプレイの端末保存（設計書 §E・localStorage）
 *
 * deck-input-panel.js の savedDecksGet/savedDecksSet と同じ作法（キー1本にJSON配列を保持）。
 * data は exportReplay が作るJSONと同じ構造（{version, title, images, names, exCardIds, logs}）を
 * LZString で圧縮した文字列として保持する（画像URL辞書を含むため生JSONのままだと肥大化するため）。
 *
 * 端末間同期（SyncClient）は行わない（将来課題。マイデッキ/購入候補と違い同期経路が未整備のため、
 * このモジュールは savedDecksSet のような window.SyncClient 連携を持たない）。
 * PC には配線しない（呼び出し元は mobile-ui.js のみ・設計書 E-4）。
 */

const SAVED_REPLAYS_KEY = 'sol-saved-replays';
// localStorage 5MB ÷ 通常1件≈20KB の余裕を見た暫定値。TODO: calibrate from data
const MAX_SAVED_REPLAYS = 20;
// 1件の圧縮後データの上限（reviewer指摘 High-3）。localStorage 全体の容量圧迫・
// 1件が異常に肥大化した場合の保存失敗を事前に防ぐための暫定値。TODO: calibrate from data
const MAX_ENTRY_BYTES = 300 * 1024; // 300KB
// savedReplaysSet が容量超過等で失敗した場合、最古の1件を落として再試行する最大回数
const MAX_EVICT_RETRY = 3;

/**
 * 保存済みリプレイの一覧を取得する。
 * @returns {Array<{id:string, title:string, savedAt:number, main:number, ex:number, steps:number, data:string}>}
 */
export function savedReplaysGet() {
  try {
    return JSON.parse(localStorage.getItem(SAVED_REPLAYS_KEY)) || [];
  } catch {
    return [];
  }
}

/**
 * 保存済みリプレイの一覧を保存する。
 * High-3: localStorage の容量超過（QuotaExceededError 等）を try/catch で拾い、成否を返す。
 * @param {Array} list
 * @returns {{ok: boolean}}
 */
export function savedReplaysSet(list) {
  try {
    localStorage.setItem(SAVED_REPLAYS_KEY, JSON.stringify(list));
    return { ok: true };
  } catch (e) {
    console.warn('[saved-replays] localStorage への保存に失敗しました:', e);
    return { ok: false };
  }
}

/** UTF-8バイト数を計算する（Blob経由。サロゲートペア等も正確に数えられる） */
function _byteLength(str) {
  return new Blob([str || '']).size;
}

/**
 * リプレイを1件、端末保存の一覧に追加する。
 * High-3:
 *   - 1件の圧縮サイズが MAX_ENTRY_BYTES を超える場合は保存前に reason:'too_large' で失敗を返す。
 *   - savedReplaysSet が失敗した場合（localStorage 容量超過等）は、最古の1件を落として
 *     再試行する（最大 MAX_EVICT_RETRY 回）。それでも失敗すれば reason:'quota' で失敗を返す。
 * @param {{title:string, main:number, ex:number, steps:number, data:string}} entry
 * @returns {{ok:boolean, removedCount:number, reason?:'quota'|'too_large'}}
 */
export function saveReplayEntry(entry) {
  if (_byteLength(entry.data) > MAX_ENTRY_BYTES) {
    return { ok: false, removedCount: 0, reason: 'too_large' };
  }

  const newItem = {
    id: 'r_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8),
    title: entry.title,
    savedAt: Date.now(),
    main: entry.main,
    ex: entry.ex,
    steps: entry.steps,
    data: entry.data,
  };

  let baseList = savedReplaysGet();

  // 通常の上限20件超過による整理（保存成功前提の間引き）はここで先に行う
  for (let attempt = 0; attempt <= MAX_EVICT_RETRY; attempt++) {
    const candidate = [...baseList, newItem];
    let removedByLimit = 0;
    if (candidate.length > MAX_SAVED_REPLAYS) {
      candidate.sort((a, b) => (a.savedAt || 0) - (b.savedAt || 0));
      removedByLimit = candidate.length - MAX_SAVED_REPLAYS;
      candidate.splice(0, removedByLimit);
    }

    const result = savedReplaysSet(candidate);
    if (result.ok) {
      return { ok: true, removedCount: removedByLimit };
    }

    // 保存失敗（容量超過等）: 最古の1件を落として再試行
    if (baseList.length === 0) break; // これ以上削れない
    baseList = baseList.slice().sort((a, b) => (a.savedAt || 0) - (b.savedAt || 0)).slice(1);
  }

  return { ok: false, removedCount: 0, reason: 'quota' };
}

/**
 * 指定IDのリプレイを一覧から削除する。
 * @param {string} id
 */
export function deleteReplayEntry(id) {
  const list = savedReplaysGet().filter(r => r.id !== id);
  savedReplaysSet(list);
  return list;
}
