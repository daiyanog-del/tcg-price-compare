/**
 * replay-caption-model.js
 * リプレイの字幕（その手で何をどこへ動かしたか）を記録だけから組み立てる純粋ロジック。
 * DOM を見ないので、次へ・巻き戻し・スライダーで飛んだときのどれでも同じ結果になる（node の単体テスト: tests/js/replay_caption_model_check.mjs）。
 *
 * 記録（moveCard など）には移動先しか入っていないので、移動元は「その手の前までの記録」を頭からたどって求める。
 * たどり方は services/replay-board.js（OGP の盤面）と同じ規則（replay-service.js の _applyEvent 系と同じ意味）。
 * replay-service.js の適用規則を変えたら、replay-board.js とここの両方を合わせること。
 * 召喚の種類（通常・特殊など）は記録から分からないので、場所の名前だけを出す（言い当てを書かない）。
 * 表示は ui/replay-caption.js。
 */

import { isTokenSrc } from '../utils/token-src.js';
import { dummyKindOfSrc } from '../utils/dummy-card.js';

// 盤面で出したカード（トークン・ダミーカード）か。どちらも記録上はデッキに足してから動かすが、
// デッキにあったカードではないので、出す前は「まだ出ていない」（out）として扱う
const _isConjured = (src) => isTokenSrc(src) || !!dummyKindOfSrc(src);

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
    // 相手の場（2026-10-29・ui/opp-field.js）
    if (n >= 15 && n <= 19) return '相手のモンスターゾーン';
    if (n === 20) return '相手のフィールドゾーン';
    if (n === 21) return '相手の墓地';
  }
  // 9番のマス（PC でフィールドゾーンの下の空きマス）は名前の無い場所
  return '盤面';
}

const FIELD_SLOT_RE = /^custom-slot-(\d+)$/;
const isFieldZone = (z) => {
  const m = FIELD_SLOT_RE.exec(z || '');
  return !!m && Number(m[1]) >= 1 && Number(m[1]) <= 21; // 実盤面は 1〜14 と相手の場 15〜21（replay-board.js と同じ）
};
// _findZone が見つけられる場所（これ以外への moveCard は再生で何も起きない。replay-board.js と同じ）
const KNOWN_ZONES = new Set(['poolRow', 'poolRow2', 'center-slot', 'side-slot-0', 'side-slot-1']);
const isKnownZone = (z) => KNOWN_ZONES.has(z) || isFieldZone(z);

/**
 * カードごとの「今いる場所・表示形式」と、トークンがまだ場に出ていないか（out）をたどる。
 * 再生と同じく、全カード（トークンも）は最初デッキ列（EX は EX デッキ列）にいる。
 */
function _track(setupLogs, logs, upto, dict) {
  const images = dict.images || {};
  const ids = Object.keys(images);
  // EX の判定は replay-board.js と同じ（辞書が空なら returnToDeck の isEx から推定）
  const exSet = (dict.exCardIds && dict.exCardIds.length > 0)
    ? new Set(dict.exCardIds)
    : new Set([...(setupLogs || []), ...(logs || [])].filter((e) => e && e.actionType === 'returnToDeck' && e.isEx).map((e) => e.cardId));
  const poolOf = (id) => (exSet.has(id) ? 'poolRow2' : 'poolRow');
  const state = new Map();
  for (const id of ids) state.set(id, { zone: poolOf(id), orientation: '', face: '', out: _isConjured(images[id]) });

  const apply = (ev) => {
    if (!ev || !ev.actionType) return;
    const s = state.get(ev.cardId);
    switch (ev.actionType) {
      case 'moveCard':
        if (!s || !isKnownZone(ev.zoneId)) return;
        Object.assign(s, { zone: ev.zoneId, orientation: ev.orientation || '', face: ev.face || '', out: false });
        return;
      case 'draw':
        if (s) Object.assign(s, { zone: 'center-slot', orientation: '', face: '', out: false });
        return;
      case 'returnToDeck':
        if (s) Object.assign(s, { zone: ev.isEx ? 'poolRow2' : 'poolRow', orientation: '', face: '' });
        return;
      case 'removeCard':
        // トークン・ダミーカードだけ盤面から外れる（それ以外のカードは再生でも何も起きない）
        if (s && _isConjured(images[ev.cardId])) Object.assign(s, { zone: '', orientation: '', face: '', out: true });
        return;
      case 'resetDeck':
        for (const [id, c] of state) {
          if (c.out && !c.zone) continue; // 取り除いたトークン・ダミーは戻らない
          Object.assign(c, { zone: poolOf(id), orientation: '', face: '', out: _isConjured(images[id]) });
        }
        for (const id of ev.drawnIds || []) {
          const c = state.get(id);
          if (c) Object.assign(c, { zone: 'center-slot', orientation: '', face: '', out: false });
        }
    }
  };
  for (const e of setupLogs || []) apply(e);
  for (let i = 0; i < upto; i++) apply(logs[i]);
  return state;
}

/** 同じマスに、そのカード以外のカードがいるか（重ねる手の判定） */
function _othersIn(state, zone, cardId) {
  for (const [id, s] of state) if (id !== cardId && s.zone === zone) return true;
  return false;
}

/**
 * index 番目の手の字幕。字幕にしない手（コメント・カード無しのカウンター・全戻し）、
 * 再生で何も起きない手（辞書に無いカード・行き先の分からない移動・トークンでないカードの取り除き）と範囲外は null。
 * @param {Array} setupLogs  初期配置（setup:true。手数に数えない）
 * @param {Array} logs       手の記録
 * @param {number} index     見ている手（0 始まり）
 * @param {{images?:Object, names?:Object, exCardIds?:Array<string>}} dict
 * @returns {null|{cardId:string, name:string, tag:string, from:string, to:string}}
 *   tag は「効果発動」「ドロー」などの札（無ければ ''）。from／to は場所の名前（無ければ ''）。
 *   from と to が両方あるときは「from → to」、片方だけなら今いる場所として出す。
 */
export function describeStep(setupLogs, logs, index, dict = {}) {
  if (!Array.isArray(logs) || index < 0 || index >= logs.length) return null;
  const ev = logs[index];
  if (!ev || !ev.cardId) return null; // コメント・全戻し・カード無しのカウンター

  const state = _track(setupLogs, logs, index, dict);
  const cur = state.get(ev.cardId);
  if (!cur) return null; // 辞書に無いカード（再生でも何も起きない）
  const before = { ...cur };
  const isToken = isTokenSrc((dict.images || {})[ev.cardId]);
  const isDummy = !!dummyKindOfSrc((dict.images || {})[ev.cardId]); // ダミーカード（2026-11-01）
  const name = (dict.names && dict.names[ev.cardId]) || (isToken ? 'トークン' : 'カード');
  const out = { cardId: ev.cardId, name, tag: '', from: '', to: '' };

  switch (ev.actionType) {
    case 'moveCard': {
      const toZone = ev.zoneId;
      if (!isKnownZone(toZone)) return null;
      const defense = ev.orientation === 'defense';
      const down = ev.face === 'down';
      if (isToken && before.out) {
        // まだ場に出ていないトークンを出した（記録上はデッキに足してから場へ動かす）
        out.to = zoneLabel(toZone);
        out.tag = down ? 'トークン・セット' : defense ? 'トークン・守備表示' : 'トークン';
        return out;
      }
      if (isDummy && before.out) {
        // まだ出していないダミーカードを出した（トークンと同じく、記録上はデッキに足してから動かす）
        out.to = zoneLabel(toZone);
        out.tag = 'ダミー';
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
      // 相手の墓地（21）は重ねて置く場所なので「重ねる」にしない
      if (isFieldZone(toZone) && toZone !== 'custom-slot-21' && _othersIn(state, toZone, ev.cardId)) out.tag = '重ねる';
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
      if (!isToken && !isDummy) return null;
      return { ...out, tag: '取り除く', from: zoneLabel(before.zone) };
    default:
      return null;
  }
}
