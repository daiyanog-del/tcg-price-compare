/**
 * replay-caption-model.js
 * リプレイの字幕（その手で何をどこへ動かしたか）を記録だけから組み立てる純粋ロジック。
 * DOM を見ないので、次へ・巻き戻し・スライダーで飛んだときのどれでも同じ結果になる（node の単体テスト: tests/js/replay_caption_model_check.mjs）。
 *
 * 記録（moveCard など）には移動先しか入っていないので、移動元は「その手の前までの記録」を頭からたどって求める。
 * 召喚の種類（通常・特殊など）は記録から分からないので、場所の名前だけを出す（言い当てを書かない）。
 * 表示は ui/replay-caption.js。
 */

import { isTokenSrc } from '../utils/token-src.js';

/** ゾーンID → 場所の名前（盤面のマス番号は templates/solitaire.html・mobile.css の並びに対応） */
export function zoneLabel(zoneId) {
  if (!zoneId) return '';
  if (zoneId === 'poolRow') return 'デッキ';
  if (zoneId === 'poolRow2') return 'EXデッキ';
  if (zoneId === 'center-slot') return '手札';
  if (zoneId === 'side-slot-0') return '墓地';
  if (zoneId === 'side-slot-1') return '除外';
  const m = /^custom-slot-(\d+)$/.exec(zoneId);
  if (m) {
    const n = Number(m[1]);
    if (n === 1 || n === 2) return 'EXモンスターゾーン';
    if (n === 3) return 'フィールドゾーン';
    if (n >= 4 && n <= 8) return 'モンスターゾーン';
    if (n >= 10 && n <= 14) return '魔法・罠ゾーン';
  }
  // 9番のマス（PC でフィールドゾーンの下の空きマス）・free-space は名前の無い場所
  return '盤面';
}

const isFieldZone = (z) => /^custom-slot-\d+$/.test(z || '');

/**
 * カードごとの「今いる場所・表示形式」をたどる。
 * @returns {Map<string, {zone:string, orientation:string, face:string}>}
 */
function _initialState(dict) {
  const state = new Map();
  const exIds = new Set(dict.exCardIds || []);
  for (const id of Object.keys(dict.images || {})) {
    // トークンは出すまでどこにもいない（記録上はデッキに足してから場へ動かす）
    const zone = isTokenSrc(dict.images[id]) ? '' : (exIds.has(id) ? 'poolRow2' : 'poolRow');
    state.set(id, { zone, orientation: '', face: '' });
  }
  return state;
}

function _get(state, id) {
  if (!state.has(id)) state.set(id, { zone: '', orientation: '', face: '' });
  return state.get(id);
}

function _apply(state, ev, exIds) {
  if (!ev || !ev.actionType) return;
  switch (ev.actionType) {
    case 'moveCard': {
      if (!ev.cardId) return;
      const s = _get(state, ev.cardId);
      s.zone = ev.zoneId || s.zone;
      s.orientation = ev.orientation || '';
      s.face = ev.face || '';
      return;
    }
    case 'draw': {
      if (!ev.cardId) return;
      Object.assign(_get(state, ev.cardId), { zone: 'center-slot', orientation: '', face: '' });
      return;
    }
    case 'returnToDeck': {
      if (!ev.cardId) return;
      Object.assign(_get(state, ev.cardId), { zone: ev.isEx ? 'poolRow2' : 'poolRow', orientation: '', face: '' });
      return;
    }
    case 'resetDeck': {
      for (const [id, s] of state) {
        if (!s.zone) continue; // 出ていないトークンはそのまま
        Object.assign(s, { zone: exIds.has(id) ? 'poolRow2' : 'poolRow', orientation: '', face: '' });
      }
      for (const id of ev.drawnIds || []) Object.assign(_get(state, id), { zone: 'center-slot', orientation: '', face: '' });
      return;
    }
    case 'removeCard': {
      if (!ev.cardId) return;
      Object.assign(_get(state, ev.cardId), { zone: '', orientation: '', face: '' });
    }
  }
}

/** 同じマスに、そのカード以外のカードがいるか（重ねる手の判定） */
function _othersIn(state, zone, cardId) {
  for (const [id, s] of state) if (id !== cardId && s.zone === zone) return true;
  return false;
}

/**
 * index 番目の手の字幕。字幕にしない手（コメント・カード無しのカウンター・全戻し）と範囲外は null。
 * @param {Array} setupLogs  初期配置（setup:true。手数に数えない）
 * @param {Array} logs       手の記録
 * @param {number} index     見ている手（0 始まり）
 * @param {{images?:Object, names?:Object, exCardIds?:Array<string>}} dict
 * @returns {null|{cardId:string, name:string, tag:string, from:string, to:string}}
 *   tag は「効果発動」「ドロー」などの札（無ければ ''）。from／to は場所の名前（無ければ ''）。
 *   from と to が両方あるときは「from → to」、to だけなら今いる場所として出す。
 */
export function describeStep(setupLogs, logs, index, dict = {}) {
  if (!Array.isArray(logs) || index < 0 || index >= logs.length) return null;
  const ev = logs[index];
  if (!ev || !ev.cardId) return null; // コメント・全戻し・カード無しのカウンター

  const exIds = new Set(dict.exCardIds || []);
  const state = _initialState(dict);
  for (const e of setupLogs || []) _apply(state, e, exIds);
  for (let i = 0; i < index; i++) _apply(state, logs[i], exIds);

  const before = { ..._get(state, ev.cardId) };
  const name = (dict.names && dict.names[ev.cardId]) || (isTokenSrc(dict.images?.[ev.cardId]) ? 'トークン' : 'カード');
  const out = { cardId: ev.cardId, name, tag: '', from: '', to: '' };

  switch (ev.actionType) {
    case 'moveCard': {
      const toZone = ev.zoneId || before.zone;
      const defense = ev.orientation === 'defense';
      const down = ev.face === 'down';
      if (!before.zone) {
        // 出ていなかったトークンを場に出した
        out.tag = 'トークン';
        out.to = zoneLabel(toZone);
        if (down) out.tag = 'トークン・セット';
        else if (defense) out.tag = 'トークン・守備表示';
        return out;
      }
      if (toZone === before.zone) {
        // 同じ場所で表示形式だけを変えた
        out.to = zoneLabel(toZone);
        if (down && before.face !== 'down') out.tag = 'セット';
        else if (!down && before.face === 'down') out.tag = '表にする';
        else if (defense && before.orientation !== 'defense') out.tag = '守備表示';
        else if (!defense && before.orientation === 'defense') out.tag = '攻撃表示';
        else out.tag = '置き直す';
        return out;
      }
      out.from = zoneLabel(before.zone);
      out.to = zoneLabel(toZone);
      if (isFieldZone(toZone) && _othersIn(state, toZone, ev.cardId)) out.tag = '重ねる';
      else if (down && isFieldZone(toZone)) out.tag = 'セット';
      else if (defense && isFieldZone(toZone)) out.tag = '守備表示';
      return out;
    }
    case 'draw':
      return { ...out, tag: 'ドロー', from: 'デッキ', to: '手札' };
    case 'returnToDeck':
      return { ...out, from: zoneLabel(before.zone), to: ev.isEx ? 'EXデッキ' : 'デッキ' };
    case 'activateEffect':
      return { ...out, tag: '効果発動', to: zoneLabel(before.zone) };
    case 'counterChange':
      return { ...out, tag: ev.op === 'remove' ? 'カウンターを外す' : 'カウンター', to: zoneLabel(before.zone) };
    case 'removeCard':
      return { ...out, tag: '取り除く', from: zoneLabel(before.zone) };
    default:
      return null;
  }
}
