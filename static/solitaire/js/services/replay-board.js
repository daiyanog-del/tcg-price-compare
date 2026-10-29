/**
 * replay-board.js
 * 共有するログ配列から「最後の時点の盤面」を求める（OGP画像用の board。DOM・演出に触れない純粋関数）。
 *
 * 設計: replay-service.js の _applyEvent 系（moveCard/draw/returnToDeck/resetDeck/removeCard）と同じ意味で、
 * DOM を使わずカードの居場所だけを追う。保存APIへ送る logs と同じ配列を入力にするため、
 * 「共有範囲の最後の時点」と必ず一致し、seekTo による一時移動（演出・トースト・再生状態への干渉）が不要。
 * replay-service.js の適用規則を変えたらここも合わせること（tests/ にDOMとの突き合わせ手順あり）。
 * リプレイの字幕（replay-caption-model.js）も同じ規則でカードの居場所をたどっている。
 *
 * 出力 board の形（サーバー replay_board.py が検証する）:
 *   { v:1, moves, field:[{slot:'custom-slot-N', name, token?, pos:'atk'|'def', face:'up'|'down', n?}],
 *     hand:[{name, token?}], grave:{count, cards:[{name, token?, face?:'down'}]}, banish:{count, cards}, ex:{count}, deckCount }
 *   - grave/banish の cards は DOM の並び順の末尾（最前面）側から最大 PILE_CARDS 枚（先頭が奥・末尾が一番上）
 *   - field は 1マスにつき最上段の1枚のみ（n = そのマスの重なり枚数。2枚以上のときだけ付く）
 *   - 画像のURLは送らない（サーバーがカード名から解決する）。token は data: 画像のトークン（名前入りの枠で描かれる）
 */

import { isTokenSrc } from '../utils/token-src.js';

const FIELD_SLOT_RE = /^custom-slot-(\d+)$/;
const isFieldSlot = (z) => {
  const m = FIELD_SLOT_RE.exec(z || '');
  return !!m && Number(m[1]) >= 1 && Number(m[1]) <= 14; // 実盤面は 1〜14 のみ（それ以外のマスは存在しない）
};
const HAND_MAX = 60; // サーバー側の上限と揃える
const PILE_CARDS = 4; // 墓地・除外で送る枚数（サーバー replay_board.PILE_MAX_CARDS と揃える）

/**
 * @param {Array}  logs        保存APIへ送る logs（setup:true を含む）
 * @param {{images:Object, names:Object, exCardIds:Array<string>}} dict
 * @returns {Object|null}  盤面。logs が空・辞書が空なら null
 */
export function buildBoardFromLogs(logs, dict) {
  const images = dict?.images || {};
  const names = dict?.names || {};
  const cardIds = Object.keys(images);
  if (!Array.isArray(logs) || logs.length === 0 || cardIds.length === 0) return null;

  // EXカード判定は _createReplayCardElements と同じ（辞書が空なら returnToDeck の isEx から推定）
  const exSet = (dict.exCardIds && dict.exCardIds.length > 0)
    ? new Set(dict.exCardIds)
    : new Set(logs.filter(e => e.actionType === 'returnToDeck' && e.isEx).map(e => e.cardId));

  const zones = new Map();   // zoneId -> cardId[]（DOMの子の並び順。末尾が最前面）
  const state = new Map();   // cardId -> {orientation, face}
  const where = new Map();   // cardId -> zoneId
  const zoneList = (z) => { if (!zones.has(z)) zones.set(z, []); return zones.get(z); };

  const removed = new Set(); // removeCard で消えたトークン（resetDeck で戻さない）
  // opts.inPlace: 同じゾーンの中で表示形式だけを変えた記録。並びは動かさず状態だけ更新する
  // opts.beforeCardId: デッキ列の途中へ差し込む位置（_placeCardInZone と同じ）
  const place = (cardId, zoneId, { orientation = '', face = '' } = {}, zIndex, opts = {}) => {
    const prev = where.get(cardId);
    if (opts.inPlace === true && prev === zoneId) {
      state.set(cardId, { orientation, face });
      return;
    }
    if (prev) {
      const arr = zoneList(prev);
      const i = arr.indexOf(cardId);
      if (i >= 0) arr.splice(i, 1);
    }
    const arr = zoneList(zoneId);
    // _placeCardInZone と同じ: フィールドで他カードがあり zIndex<=1 なら最背面へ、それ以外は最前面
    if (isFieldSlot(zoneId) && arr.length > 0 && parseInt(zIndex ?? '1', 10) <= 1) {
      arr.unshift(cardId);
    } else if ((zoneId === 'poolRow' || zoneId === 'poolRow2') && opts.beforeCardId && arr.includes(opts.beforeCardId)) {
      arr.splice(arr.indexOf(opts.beforeCardId), 0, cardId);
    } else {
      arr.push(cardId);
    }
    where.set(cardId, zoneId);
    state.set(cardId, { orientation, face });
  };
  const poolOf = (cardId) => (exSet.has(cardId) ? 'poolRow2' : 'poolRow');
  const KNOWN_ZONES = new Set(['poolRow', 'poolRow2', 'center-slot', 'side-slot-0', 'side-slot-1']);

  for (const id of cardIds) place(id, poolOf(id));

  for (const ev of logs) {
    if (!ev) continue;
    const id = ev.cardId;
    switch (ev.actionType) {
      case 'moveCard': {
        if (!where.has(id)) break;
        const z = ev.zoneId;
        if (!KNOWN_ZONES.has(z) && !isFieldSlot(z)) break; // _findZone が null の場合は何もしない
        place(id, z, { orientation: ev.orientation, face: ev.face }, ev.zIndex,
          { inPlace: ev.inPlace, beforeCardId: ev.beforeCardId });
        break;
      }
      case 'draw':
        if (where.has(id)) place(id, 'center-slot');
        break;
      case 'returnToDeck':
        if (where.has(id)) place(id, ev.isEx ? 'poolRow2' : 'poolRow');
        break;
      case 'removeCard': {
        // トークンの削除。盤面から外す（辞書には残る）。居ない・トークンでないカードは何もしない（replay-service と同じ）
        if (!where.has(id) || !isTokenSrc(images[id])) break;
        const arr = zoneList(where.get(id));
        const i = arr.indexOf(id);
        if (i >= 0) arr.splice(i, 1);
        where.delete(id);
        state.delete(id);
        removed.add(id);
        break;
      }
      case 'resetDeck':
        for (const cid of cardIds) if (!removed.has(cid)) place(cid, poolOf(cid));
        for (const cid of (ev.drawnIds || [])) if (where.has(cid)) place(cid, 'center-slot');
        break;
      default:
        break; // counterChange / comment / activateEffect は盤面の配置に影響しない
    }
  }

  // カードは名前と種類だけを送る。トークンは辞書の画像が data: のもの（名前が無ければ「トークン」）
  const ref = (cid) => {
    const src = String(images[cid] ?? '');
    const isToken = isTokenSrc(src); // card-state.js の isTokenSrc と同じ基準（data: 画像）
    const name = String(names[cid] || (src.startsWith('card://') ? src.slice('card://'.length) : '') || (isToken ? 'トークン' : ''));
    return isToken ? { name, token: true } : { name };
  };
  const field = [];
  for (const [zoneId, arr] of zones) {
    if (!isFieldSlot(zoneId) || arr.length === 0) continue;
    const top = arr[arr.length - 1];
    const st = state.get(top) || {};
    const entry = {
      slot: zoneId,
      ...ref(top),
      pos: st.orientation === 'defense' ? 'def' : 'atk',
      face: st.face === 'down' ? 'down' : 'up',
    };
    if (arr.length > 1) entry.n = arr.length;
    field.push(entry);
  }
  const pile = (zoneId) => {
    const arr = zones.get(zoneId) || [];
    const cards = arr.slice(-PILE_CARDS).map((cid) => {
      const r = ref(cid);
      if (state.get(cid)?.face === 'down') r.face = 'down'; // 裏向きのまま置かれたカードは裏面で描く
      return r;
    });
    return { count: arr.length, cards };
  };

  return {
    v: 1,
    moves: logs.filter(e => e && e.setup !== true).length,
    field,
    hand: (zones.get('center-slot') || []).slice(0, HAND_MAX).map(ref),
    grave: pile('side-slot-0'),
    banish: pile('side-slot-1'),
    ex: { count: (zones.get('poolRow2') || []).length },
    deckCount: (zones.get('poolRow') || []).length,
  };
}
