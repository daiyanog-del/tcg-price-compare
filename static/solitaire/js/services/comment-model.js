/**
 * comment-model.js
 * コメント（カードの吹き出し・盤面のメモ）の純粋ロジック。DOM・他モジュールに依存しない
 * （node の単体テストから直接読み込める。tests/js/comment_model_check.mjs）。
 *
 * 記録の形（どちらも actionType:'comment'。古いリプレイと互換）:
 *   吹き出し: { actionType:'comment', kind:'card',  cardId, text }
 *   盤面メモ: { actionType:'comment', kind:'board', text }
 *   kind が無い古いコメントは盤面メモとして扱う。
 */

/** コメント本文の文字数の上限（入力欄の制限・記録時・表示時のすべてで同じ値を使う） */
export const COMMENT_MAX_LEN = 60;

/**
 * 本文を整える（前後の空白を除き、上限で切る）。サロゲートペア（絵文字など）を途中で割らない。
 * @param {unknown} text
 * @returns {string}
 */
export function clampCommentText(text) {
  const s = typeof text === 'string' ? text.trim() : '';
  const chars = Array.from(s);
  return chars.length > COMMENT_MAX_LEN ? chars.slice(0, COMMENT_MAX_LEN).join('') : s;
}

/**
 * コメントの種類。kind が 'card' で cardId があるときだけ吹き出し。それ以外（kind 無しの古い記録を含む）は盤面メモ。
 * @param {Object} ev
 * @returns {'card'|'board'}
 */
export function commentKind(ev) {
  return ev && ev.kind === 'card' && typeof ev.cardId === 'string' && ev.cardId ? 'card' : 'board';
}

/**
 * カーソル位置までの記録から、表示する吹き出しと盤面メモを求める。
 * 記録の並びは「setup（初期配置）→ 通常ログ（cursor まで）」。範囲共有で setup に入った古いコメントも、
 * 同じ規則で扱う（開始前に付いて、まだ動いていないカードの吹き出しは受け手にも見える）。
 *   - 吹き出し: カードごとに最後の1件。その後に同じカードの移動（moveCard。inPlace は除く）・
 *     removeCard・draw・returnToDeck があれば出さない。resetDeck は全カードがデッキへ戻るので全部消す。
 *   - 盤面メモ: 一番新しい1件。
 * @param {Array} setupLogs
 * @param {Array} logs
 * @param {number} cursor  logs の位置（-1 なら setup だけ）
 * @returns {{bubbles: Array<{cardId:string, text:string}>, memo: string|null}}
 */
export function computeCommentView(setupLogs, logs, cursor) {
  const bubbles = new Map();
  let memo = null;
  const upto = Math.min(cursor, (logs?.length ?? 0) - 1);
  const seq = [...(setupLogs || []), ...(logs || []).slice(0, Math.max(0, upto + 1))];
  for (const ev of seq) {
    if (!ev) continue;
    switch (ev.actionType) {
      case 'comment': {
        const text = clampCommentText(ev.text);
        if (!text) break;
        if (commentKind(ev) === 'card') {
          bubbles.delete(ev.cardId); // 同じカードの新しい吹き出しは末尾へ（並びを記録順にする）
          bubbles.set(ev.cardId, text);
        } else {
          memo = text;
        }
        break;
      }
      case 'moveCard':
        if (ev.inPlace !== true) bubbles.delete(ev.cardId);
        break;
      case 'removeCard':
      case 'draw':
      case 'returnToDeck':
        bubbles.delete(ev.cardId);
        break;
      case 'resetDeck':
        bubbles.clear();
        break;
      default:
        break;
    }
  }
  return { bubbles: [...bubbles].map(([cardId, text]) => ({ cardId, text })), memo };
}

/**
 * 通常ログの中のコメント一覧（右パネルの一覧用）。
 * @param {Array} logs
 * @returns {Array<{index:number, kind:'card'|'board', cardId:string|null, text:string}>}
 */
export function listComments(logs) {
  const out = [];
  (logs || []).forEach((ev, index) => {
    if (ev?.actionType !== 'comment') return;
    const kind = commentKind(ev);
    out.push({ index, kind, cardId: kind === 'card' ? ev.cardId : null, text: clampCommentText(ev.text) });
  });
  return out;
}

/**
 * ログから index 番目を取り除いたとき、カーソルがどこへずれるか。
 * 取り除く手がカーソル以前にあれば、カーソルは1つ前へ。
 * @param {number} cursor
 * @param {number} removedIdx
 * @returns {number}
 */
export function cursorAfterRemove(cursor, removedIdx) {
  return removedIdx <= cursor ? cursor - 1 : cursor;
}

/**
 * ログから removedIdx 番目を取り除いたとき、範囲（recording 状態）の開始・終了がどうずれるか。
 * startIdx / endIdx は「範囲より前の手数」「範囲の終わりまでの手数」（＝範囲は logs[startIdx..endIdx-1]）。
 *   - 範囲より前を消した（removedIdx < startIdx）: 開始も終わりも1つ前へ
 *   - 範囲の中を消した（startIdx <= removedIdx < endIdx）: 終わりだけ1つ前へ
 *   - 範囲より後を消した: そのまま
 * 範囲を選んでいる最中（endIdx が null）は開始だけを見る。元のオブジェクトは変更しない。
 * @param {{recording:boolean, startIdx:number|null, endIdx:number|null}} state
 * @param {number} removedIdx
 * @returns {{recording:boolean, startIdx:number|null, endIdx:number|null}}
 */
export function shiftRangeAfterRemove(state, removedIdx) {
  const next = { ...state };
  if (next.startIdx === null || next.startIdx === undefined) return next;
  if (removedIdx < next.startIdx) {
    next.startIdx -= 1;
    if (next.endIdx !== null && next.endIdx !== undefined) next.endIdx -= 1;
  } else if (next.endIdx !== null && next.endIdx !== undefined && removedIdx < next.endIdx) {
    next.endIdx -= 1;
  }
  return next;
}
