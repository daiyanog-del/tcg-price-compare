/**
 * comment-model.js
 * コメント（カードの吹き出し・盤面のメモ・カットイン）の純粋ロジック。DOM・他モジュールに依存しない
 * （node の単体テストから直接読み込める。tests/js/comment_model_check.mjs）。
 *
 * 記録の形（すべて actionType:'comment'。古いリプレイと互換）:
 *   吹き出し: { actionType:'comment', kind:'card',  cardId, text }
 *   盤面メモ: { actionType:'comment', kind:'board', text }
 *   カットイン: { actionType:'comment', kind:'cutin', text }（本文は20字まで。その手に「進んだとき」だけ一瞬出る）
 *   相手の妨害のカットイン: { actionType:'comment', kind:'cutin', card, text }
 *     card はカード名（60字まで）。帯にカード画像と「相手の妨害／カード名 発動」を出す。
 *     text には card を20字で切ったものを入れておく（card を知らない古い画面でも文字のカットインとして出るように）。
 *     画像のURLは記録に残さず、出すときに名前から引き直す（comment-view.js）。
 *   kind が無い古いコメント・未知の kind は盤面メモとして扱う。
 */

/** コメント本文の文字数の上限（入力欄の制限・記録時・表示時のすべてで同じ値を使う） */
export const COMMENT_MAX_LEN = 60;

/**
 * 本文を整える（前後の空白を除き、上限で切る）。サロゲートペア（絵文字など）を途中で割らない。
 * @param {unknown} text
 * @returns {string}
 */
export function clampCommentText(text, max = COMMENT_MAX_LEN) {
  const s = typeof text === 'string' ? text.trim() : '';
  const chars = Array.from(s);
  return chars.length > max ? chars.slice(0, max).join('') : s;
}

/** カットインの本文の文字数の上限（入力欄・記録時・表示時のすべてで同じ値を使う） */
export const CUTIN_MAX_LEN = 20;

/** カットインを画面に出している時間（ms）。css/comments.css の cutIn アニメーションの長さと同じ値にする */
export const CUTIN_DURATION_MS = 1900;

/** カットインの言葉の札（押すとその場で入る） */
export const CUTIN_PRESETS = ['ターン終了', '相手ターン', '自分ターン', 'バトルフェイズ', 'メインフェイズ2', 'エンドフェイズ'];

/** カットインの本文を整える（前後の空白を除き、20字で切る） */
export function clampCutinText(text) {
  return clampCommentText(text, CUTIN_MAX_LEN);
}

/** 相手の妨害のカットインの帯に出す言葉（2026-10-16 ユーザー決定） */
export const CUTIN_DISRUPTION_LABEL = '相手の妨害';
export const CUTIN_DISRUPTION_VERB = '発動';

/**
 * 相手の妨害のカットインならカード名（60字で切ったもの）、そうでなければ null。
 * @param {Object} ev
 * @returns {string|null}
 */
export function cutinCard(ev) {
  if (!ev || ev.kind !== 'cutin') return null;
  return clampCommentText(ev.card) || null;
}

/**
 * 記録に入れるカットインの手を作る（本文が空なら null）。
 * card を渡すと相手の妨害のカットイン（text には card を20字で切ったものを入れる）。
 * @param {{text?:string, card?:string}} opts
 * @returns {Object|null}
 */
export function buildCutinEvent({ text, card } = {}) {
  const name = clampCommentText(card);
  if (name) return { actionType: 'comment', kind: 'cutin', card: name, text: clampCutinText(name) };
  const t = clampCutinText(text);
  return t ? { actionType: 'comment', kind: 'cutin', text: t } : null;
}

/**
 * コメントの種類。kind が 'card' で cardId があるときだけ吹き出し、kind が 'cutin' ならカットイン。
 * それ以外（kind 無しの古い記録・未知の kind を含む）は盤面メモ。
 * @param {Object} ev
 * @returns {'card'|'board'|'cutin'}
 */
export function commentKind(ev) {
  if (ev && ev.kind === 'cutin') return 'cutin';
  return ev && ev.kind === 'card' && typeof ev.cardId === 'string' && ev.cardId ? 'card' : 'board';
}

/** コメントの手の本文を、種類ごとの上限で整えて返す（カットインは20字、ほかは60字。相手の妨害のカットインはカード名） */
export function commentText(ev) {
  if (commentKind(ev) !== 'cutin') return clampCommentText(ev?.text);
  return cutinCard(ev) || clampCutinText(ev?.text);
}

/**
 * 「その手に進んだとき」に出すカットインを返す（出さないときは null）。
 * 出すのは、前進で（手を打った直後・付けた直後・次へ・自動再生）その手に来たときだけ。
 * 巻き戻し・スライダーで飛んだとき・取消・セッション復元・読み込み直後は arrivedForward を false にして呼ぶ。
 * @param {Object|undefined} ev  カーソルが来た手
 * @param {boolean} arrivedForward  前進でその手に来たか
 * @returns {{text:string, card:string|null}|null}  card があれば相手の妨害のカットイン（text はカード名）
 */
export function cutinToShow(ev, arrivedForward) {
  if (!arrivedForward || !ev || ev.actionType !== 'comment' || commentKind(ev) !== 'cutin') return null;
  const text = commentText(ev);
  return text ? { text, card: cutinCard(ev) } : null;
}

/**
 * 記録の start 番目以降にある、相手の妨害のカットインのカード名（重複なし・記録順で limit 件まで。画像の先読み用）。
 * @param {Array} logs
 * @param {number} [start]
 * @param {number} [limit]
 * @returns {string[]}
 */
export function cutinCardsIn(logs, start = 0, limit = Infinity) {
  const names = new Set();
  const list = logs || [];
  for (let i = Math.max(0, start); i < list.length && names.size < limit; i++) {
    const ev = list[i];
    if (ev?.actionType !== 'comment') continue;
    const name = cutinCard(ev);
    if (name) names.add(name);
  }
  return [...names];
}

/**
 * 自動再生で、いまの手のあとに待つ時間（ms）。
 * カットインの手は、動きが終わるまで（CUTIN_DURATION_MS）待つ。それ以外のコメント（吹き出し・メモ）は
 * 読めるよう commentFactor 倍、ふつうの手は baseMs。
 * @param {Object|undefined} ev
 * @param {number} baseMs  ふつうの手の待ち時間（再生速度を反映済み）
 * @param {number} commentFactor  吹き出し・メモの倍率
 * @returns {number}
 */
export function playPauseMs(ev, baseMs, commentFactor) {
  if (ev?.actionType !== 'comment') return baseMs;
  if (commentKind(ev) === 'cutin') return Math.max(baseMs, CUTIN_DURATION_MS);
  return baseMs * commentFactor;
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
        const text = commentText(ev);
        if (!text) break;
        if (commentKind(ev) === 'cutin') break; // カットインは盤面に残らない（盤面メモを置き換えない）
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
 * disruption は相手の妨害のカットインか（一覧で「（相手の妨害）」と添える）。
 * @returns {Array<{index:number, kind:'card'|'board'|'cutin', cardId:string|null, text:string, disruption:boolean}>}
 */
export function listComments(logs) {
  const out = [];
  (logs || []).forEach((ev, index) => {
    if (ev?.actionType !== 'comment') return;
    const kind = commentKind(ev);
    out.push({ index, kind, cardId: kind === 'card' ? ev.cardId : null, text: commentText(ev), disruption: !!cutinCard(ev) });
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

/**
 * ログの insertedIdx 番目にコメントを差し込んだとき、範囲（recording 状態）の開始・終了がどうずれるか
 * （shiftRangeAfterRemove の逆）。範囲は logs[startIdx..endIdx-1]。insertedIdx は差し込んだ後の位置。
 *   - 範囲より前（insertedIdx < startIdx）: 開始も終わりも1つ後ろへ
 *   - 範囲の中・範囲の終わり直後（startIdx <= insertedIdx <= endIdx）: 終わりだけ1つ後ろへ（コメントは範囲に入る）
 *   - それより後ろ: そのまま
 * 範囲を選んでいる最中（endIdx が null）は、開始より前に差し込んだときだけ開始を1つ後ろへ
 * （開始と同じ位置への差し込みは、これから決まる範囲の最初の手になるので開始はそのまま）。
 * 元のオブジェクトは変更しない。
 * @param {{recording:boolean, startIdx:number|null, endIdx:number|null}} state
 * @param {number} insertedIdx
 * @returns {{recording:boolean, startIdx:number|null, endIdx:number|null}}
 */
export function shiftRangeAfterInsert(state, insertedIdx) {
  const next = { ...state };
  if (next.startIdx === null || next.startIdx === undefined) return next;
  const hasEnd = next.endIdx !== null && next.endIdx !== undefined;
  if (insertedIdx < next.startIdx) {
    next.startIdx += 1;
    if (hasEnd) next.endIdx += 1;
  } else if (hasEnd && insertedIdx <= next.endIdx) {
    next.endIdx += 1;
  }
  return next;
}

/**
 * 一覧の行が指している内容と、記録の手が同じコメントかどうか（削除の取り違え防止）。
 * @param {Object|undefined} ev  記録の手
 * disruption（相手の妨害のカットインか）は、渡されたときだけ照合する（文字の「灰流うらら」と妨害の「灰流うらら」を区別する）。
 * @param {{kind:'card'|'board'|'cutin', cardId:string|null, text:string, disruption?:boolean}|undefined} expected 一覧の行の内容
 */
export function isSameComment(ev, expected) {
  if (!ev || ev.actionType !== 'comment') return false;
  if (!expected) return true;
  return commentKind(ev) === expected.kind
    && (commentKind(ev) === 'card' ? ev.cardId : null) === (expected.cardId ?? null)
    && commentText(ev) === expected.text
    && (expected.disruption === undefined || !!cutinCard(ev) === expected.disruption);
}
