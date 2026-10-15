/**
 * slot-migration.js
 * 盤面セーブ（localStorage の card-sim-save-slot-1〜5）を、0手のリプレイとして
 * リプレイ一覧（端末保存 sol-saved-replays）へ移す一回きりの移行。
 *
 * 盤面セーブは廃止され、続きから遊ぶ手段はリプレイ一覧の「最後の盤面から続ける」に一本化された。
 * 既存の利用者のスロットを失わないよう、シミュレーター起動時とリプレイ一覧ページの起動時に
 * migrateSlotsToReplays() を呼ぶ（どちらが先でも、何度呼んでも結果は同じ）。
 *
 * 変換の考え方:
 *   スロットの盤面（各ゾーンのカード・表示形式・重なり・カウンター）を、
 *   「setup:true の moveCard / counterChange だけを並べたログ」へ書き下す。
 *   リプレイとして開くと setup ログが初期配置として即時適用され、手数は0のまま同じ盤面になる。
 *   payload の形は replay-service.js の buildReplayPayload と同じ。
 *
 * このモジュールはシミュレーター本体（DOM・他のコンポーネント）に依存しない
 * （一覧ページは本体を読み込まないため）。依存は saved-replays.js と、グローバルの LZString のみ。
 */

import { saveReplayPayload, hasReplayOrigin } from './saved-replays.js';

const SAVE_KEY_PREFIX = 'card-sim-save-slot-';
const MAX_SLOTS = 5;
const POOL_KEYS = ['poolRow', 'poolRow2'];
const TITLE_MAX_LEN = 100;

// card-manager.js の sortPoolCards と同じ並び（種類順→ID順）。
// リプレイを開いた直後のデッキ列はこの並びになるため、スロットの並びがこれと違うときだけ並び替えのログを足す。
// （card-manager.js は DOM に依存するため一覧ページから読めない。並びの規則を変えたらここも合わせること）
const TYPE_ORDER = { monster: 0, spell: 1, trap: 2 };

/** 盤面（ゾーン）として扱うキーか */
function isBoardZoneKey(key) {
  return /^custom-slot-\d+$/.test(key) || /^side-slot-\d+$/.test(key)
    || key === 'center-slot' || key === 'free-space';
}

/** 文字列の簡単なハッシュ（djb2。移行元の印で「同じ中身か」を見分けるためだけに使う。暗号用途ではない） */
function hashString(str) {
  let h = 5381;
  for (let i = 0; i < str.length; i++) h = ((h * 33) ^ str.charCodeAt(i)) >>> 0;
  return h.toString(36);
}

/** スロットの保存文字列を読む（圧縮形式。旧形式の生 JSON も読む）。読めなければ例外 */
function decodeSlot(raw) {
  let text = null;
  try { text = LZString.decompress(raw); } catch { /* 圧縮形式ではない */ }
  if (text) {
    try { return JSON.parse(text); } catch { /* 生 JSON として読み直す */ }
  }
  return JSON.parse(raw);
}

/**
 * スロットの中身（saveGameState が保存した形）を 0手リプレイの payload に変換する。
 * 純粋関数（DOM・localStorage に触らない）。
 *
 * 変換できない部分は捨てて warnings に積む（旧データの読み込み側 restoreCounters も同じものを復元できなかった）:
 *   - カードの付け先が見つからないカウンター（旧形式の class 名で持つもの等。同名カードの取り違えを避け推測しない）
 *   - 盤面のスロットに直接置かれたカウンター（今の画面では作れない旧形式。リプレイでは表せない）
 *   - id が無い・画像も名前も無いカード
 *
 * @param {Object} state - スロットの中身 { slotName, timestamp, slots, counters }
 * @param {number} slotNumber
 * @returns {{payload:Object|null, cardCount:number, warnings:string[]}}
 *   payload が null のとき: カードが1枚も無い（cardCount=0）か、変換できるカードが無かった（cardCount>0）
 */
export function convertSlotToPayload(state, slotNumber) {
  if (!state || typeof state !== 'object' || !state.slots || typeof state.slots !== 'object') {
    throw new Error('スロットの形式が読めません');
  }
  const warnings = [];
  const images = {};
  const names = {};
  const cardTypes = {};
  const exCardIds = [];
  const boardMoves = [];
  const poolOrder = { poolRow: [], poolRow2: [] };
  const idMap = new Map();   // ラッパーid（"item-007 normal"）/ カードid（"item-007"）→ カードid
  const seen = new Set();
  let cardCount = 0;

  // 画像辞書はデッキ→それ以外の順で作る（デッキ列の初期の並びがスロットに近くなる）
  const keys = [...POOL_KEYS, ...Object.keys(state.slots).filter(k => !POOL_KEYS.includes(k))];
  for (const key of keys) {
    const cards = state.slots[key];
    if (!Array.isArray(cards)) continue;
    const isPool = POOL_KEYS.includes(key);
    if (!isPool && !isBoardZoneKey(key)) {
      if (cards.length > 0) warnings.push(`未知のゾーン ${key} のカード ${cards.length} 枚を移せません`);
      continue;
    }
    cards.forEach((c, i) => {
      if (!c || typeof c.id !== 'string' || c.id === 'initial') return; // 'initial' は旧版の盤面に置かれていた見本カード
      cardCount++;
      const cid = c.id.split(' ')[0];
      if (!cid) return;
      if (seen.has(cid)) return; // 'free-space' と 'side-slot-N' は同じカードを二重に持つ。最初の1回だけ
      // 画像: 保存された src（実URL / card://名前 / data:）。無ければ名前から再解決用の card:// を作る
      const src = (typeof c.src === 'string' && c.src) ? c.src
        : (c.cardName ? `card://${c.cardName}` : '');
      if (!src) {
        warnings.push(`カード ${c.id} は画像も名前も無いため移せません`);
        return;
      }
      seen.add(cid);
      idMap.set(c.id, cid);
      idMap.set(cid, cid);
      images[cid] = src;
      if (c.cardName) names[cid] = c.cardName;
      if (c.cardType) cardTypes[cid] = c.cardType;
      if (key === 'poolRow2' || /\sex$/.test(c.id)) exCardIds.push(cid);

      if (isPool) {
        poolOrder[key].push({ cid, type: c.cardType });
        return; // デッキ・EXデッキに居るカードは配置のログを作らない
      }
      boardMoves.push({
        actionType: 'moveCard', cardId: cid, zoneId: key,
        // フィールドは DOM 順（先頭が最背面）に1枚ずつ末尾へ積む。zIndex が 1 を超えれば replay-service は末尾に置く
        zIndex: String(i + 1), transform: '',
        orientation: c.state?.orientation || '', face: c.state?.face || '',
        setup: true, seq: -1,
      });
    });
  }

  if (Object.keys(images).length === 0) {
    return { payload: null, cardCount, warnings };
  }

  const logs = [];

  // デッキ・EXデッキの並び: スロットの並びが開いた直後の並びと違うときだけ、末尾へ積み直すログを足す
  for (const poolKey of POOL_KEYS) {
    const list = poolOrder[poolKey];
    const natural = list.slice().sort((a, b) => {
      const ta = TYPE_ORDER[a.type] ?? 3;
      const tb = TYPE_ORDER[b.type] ?? 3;
      if (ta !== tb) return ta - tb;
      return a.cid.toLowerCase().localeCompare(b.cid.toLowerCase());
    });
    const same = list.every((x, idx) => x.cid === natural[idx].cid);
    if (same) continue;
    for (const x of list) {
      logs.push({ actionType: 'moveCard', cardId: x.cid, zoneId: poolKey,
        zIndex: '1', transform: '', orientation: '', face: '', setup: true, seq: -1 });
    }
  }

  logs.push(...boardMoves);

  // カード上のカウンター（置く操作として書き下す。今の記録の counterChange op:'add' と同じ）
  let droppedCounters = 0;
  for (const ct of (Array.isArray(state.counters) ? state.counters : [])) {
    const cid = ct && !ct.parentDataSlot ? idMap.get(ct.parentId) : null;
    if (!cid) { droppedCounters++; continue; }
    logs.push({ actionType: 'counterChange', cardId: cid, counter: parseInt(ct.value, 10) || 0,
      op: 'add', setup: true, seq: -1 });
  }
  if (droppedCounters > 0) warnings.push(`付け先のカードが分からないカウンター ${droppedCounters} 個を移せません`);

  const title = String(state.slotName || '').trim().slice(0, TITLE_MAX_LEN) || `スロット${slotNumber}`;
  const payload = {
    version: 1, title, images, names, exCardIds,
    ...(Object.keys(cardTypes).length > 0 ? { cardTypes } : {}),
    logs,
  };
  return { payload, cardCount, warnings };
}

/**
 * 盤面セーブのスロット1〜5を、0手のリプレイとして端末保存の一覧へ移す。
 *
 * - 登録に成功したスロットのキーだけを消す。変換エラー・容量不足で失敗したキーは残し、
 *   console.warn を出す（次に呼ばれたときにまた試す）。
 * - 既存の保存分は押し出さない（上限20件を超えて登録する。saveReplayEntry の keepExisting）。
 * - 移行元の印（origin）で登録済みかを確かめるので、キーを消す前に中断されても二重登録しない。
 *   すでに登録済みだったスロットはキーだけを消し、移した件数には数えない。
 * - カードが1枚も無い空のスロットは、失われる情報が無いのでキーだけを消す。
 * - LZString・localStorage が使えないときは何もしない。
 *
 * @returns {{migrated:number, failed:number}} migrated: 今回リプレイ一覧へ移した件数 / failed: 残した件数
 */
export function migrateSlotsToReplays() {
  const result = { migrated: 0, failed: 0 };
  if (typeof LZString === 'undefined') {
    console.warn('[slot-migration] LZString が無いため盤面セーブの移行を見送りました');
    return result;
  }

  for (let n = 1; n <= MAX_SLOTS; n++) {
    const key = SAVE_KEY_PREFIX + n;
    try {
      const raw = localStorage.getItem(key);
      if (!raw) continue;

      const state = decodeSlot(raw);
      const timestamp = Number.isFinite(state?.timestamp) && state.timestamp > 0 ? state.timestamp : null;
      const origin = `slot${n}:${timestamp ?? hashString(raw)}`; // 日時が無い旧データは中身のハッシュで見分ける

      if (hasReplayOrigin(origin)) { // 登録済み（前回、キーを消す前に中断された）
        localStorage.removeItem(key);
        continue;
      }

      const { payload, cardCount, warnings } = convertSlotToPayload(state, n);
      if (!payload) {
        if (cardCount === 0) { // 空の盤面。失われる情報が無い
          localStorage.removeItem(key);
        } else {
          console.warn(`[slot-migration] スロット${n}は変換できるカードが無いため残しました`, warnings);
          result.failed++;
        }
        continue;
      }
      if (warnings.length > 0) console.warn(`[slot-migration] スロット${n}の一部を移せませんでした:`, warnings);

      const saved = saveReplayPayload(payload, {
        title: payload.title, savedAt: timestamp ?? Date.now(), origin, keepExisting: true,
      });
      if (!saved.ok) {
        console.warn(`[slot-migration] スロット${n}を登録できず残しました（${saved.reason}）。次の読み込みで再試行します`);
        result.failed++;
        continue;
      }
      localStorage.removeItem(key);
      result.migrated++;
    } catch (e) {
      console.warn(`[slot-migration] スロット${n}の移行に失敗しました。キーは残します:`, e);
      result.failed++;
    }
  }
  return result;
}

/** 移行されずに残っているスロットのキー（card-sim-save-slot-1〜5 のうち保存文字列があるもの） */
export function getRemainingSlotKeys() {
  const keys = [];
  for (let n = 1; n <= MAX_SLOTS; n++) {
    const key = SAVE_KEY_PREFIX + n;
    try {
      if (localStorage.getItem(key)) keys.push(key);
    } catch { /* localStorage が使えない */ }
  }
  return keys;
}

/**
 * 残っているスロットの中身を、そのまま読める JSON 文字列にして返す（データを捨てないための書き出し用）。
 * キーごとに { キー: 中身 } とし、圧縮形式は展開した JSON を、展開できないものは保存文字列のまま入れる。
 * 残っているスロットが無ければ null。
 * @returns {string|null}
 */
export function exportRemainingSlotsJson() {
  const out = {};
  for (const key of getRemainingSlotKeys()) {
    const raw = localStorage.getItem(key);
    try {
      out[key] = decodeSlot(raw);
    } catch {
      out[key] = raw;
    }
  }
  return Object.keys(out).length > 0 ? JSON.stringify(out, null, 2) : null;
}
