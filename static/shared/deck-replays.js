/**
 * deck-replays.js — デッキ（材料）とリプレイ（レシピ）のひも付け（2026-10-21・モック _mocks/deck-replay-link-20261021 をユーザー承認）
 *
 * 端末に保存したリプレイ（localStorage 'sol-saved-replays'・saved-replays.js）の1件に
 *   deckId   … 記録したときにシミュレーターがひも付いていたマイデッキのID（無ければ「デッキなし」）
 *   deckSnap … 記録したときのデッキの中身 {カード名: 枚数}（メインと EX を合わせて名前で数える）
 * を足す。今のマイデッキと deckSnap を比べて「記録の後にデッキが変わった」かと、その差を出す。
 *
 * シミュレーター（ES module から window.DeckReplays）・トップのマイデッキ・リプレイ一覧ページで共通に使う。
 * DOM には触らない（tests/js/deck_replays_check.js から直接読む）。読むのは localStorage だけ。
 * リプレイは端末の中だけ（同期しない）なので、別の端末で撮ったリプレイはその端末にしか出ない。
 */
(function (global) {
  'use strict';
  const KEY = 'sol-saved-replays'; // saved-replays.js の SAVED_REPLAYS_KEY と同じ
  const has = (o, k) => Object.prototype.hasOwnProperty.call(o, k);

  /** 保存したリプレイの一覧（壊れていれば空）。data（中身）は使わない。
   *  1件が数百KBになるので、保存の文字列が前回と同じなら解析し直さず前回の結果を返す（マイデッキの +/− のたびに呼ばれる） */
  let _cacheRaw = null, _cacheList = [];
  function list(storage) {
    try {
      const s = storage || global.localStorage;
      const raw = s.getItem(KEY);
      if (raw === _cacheRaw) return _cacheList.slice();
      const l = JSON.parse(raw);
      const out = Array.isArray(l) ? l.filter(r => r && typeof r === 'object' && typeof r.id === 'string')
        .map(r => ({ id: r.id, title: r.title, savedAt: r.savedAt, steps: r.steps, deckId: r.deckId, deckSnap: r.deckSnap })) : [];
      _cacheRaw = raw;
      _cacheList = out;
      return out.slice();
    } catch (e) {
      return [];
    }
  }

  /** デッキの中身 {main:[{name,qty}], ex:[...]} を {カード名: 枚数} にする（プロトタイプの無いオブジェクト） */
  function snapshot(deck) {
    const out = Object.create(null);
    for (const c of [...((deck && deck.main) || []), ...((deck && deck.ex) || [])]) {
      if (!c || typeof c.name !== 'string' || !c.name) continue;
      const q = Number.isInteger(c.qty) && c.qty > 0 ? c.qty : 1;
      out[c.name] = (out[c.name] || 0) + q;
    }
    return out;
  }

  /** そのデッキのリプレイ（新しい順） */
  function forDeck(items, deckId) {
    if (!deckId) return [];
    return items.filter(r => r.deckId === deckId).sort((a, b) => (b.savedAt || 0) - (a.savedAt || 0));
  }

  /** デッキごとのリプレイの数 {デッキID: 件数} */
  function countByDeck(items) {
    const out = Object.create(null);
    for (const r of items) if (r.deckId) out[r.deckId] = (out[r.deckId] || 0) + 1;
    return out;
  }

  /**
   * 記録したときの中身（snap）と今の中身（deck）の差。added＝今のほうが多い・removed＝今のほうが少ない。
   * snap が無い（古いリプレイ）ときは比べられないので null。
   */
  function diff(snap, deck) {
    if (!snap || typeof snap !== 'object') return null;
    const now = snapshot(deck);
    const added = [], removed = [];
    const names = new Set([...Object.keys(snap), ...Object.keys(now)]);
    for (const name of names) {
      const before = has(snap, name) && Number.isInteger(snap[name]) ? snap[name] : 0;
      const after = now[name] || 0;
      if (after > before) added.push({ name, n: after - before });
      else if (after < before) removed.push({ name, n: before - after });
    }
    return { added, removed, changed: added.length + removed.length > 0 };
  }

  /** 差を短い文にする（「＋無限泡影 1枚　−墓穴の指名者 1枚」。多いときは max 件まで＋「ほか N種類」） */
  function describe(d, max) {
    if (!d || !d.changed) return '';
    const parts = d.added.map(x => `＋${x.name} ${x.n}枚`).concat(d.removed.map(x => `−${x.name} ${x.n}枚`));
    const m = max || 3;
    return parts.slice(0, m).join('　') + (parts.length > m ? `　ほか${parts.length - m}種類` : '');
  }

  /** 行の補足（「18手・10/20」） */
  function meta(r) {
    const d = r.savedAt ? new Date(r.savedAt) : null;
    const when = d && !isNaN(d.getTime()) ? `${d.getMonth() + 1}/${d.getDate()}` : '';
    return `${Number.isInteger(r.steps) ? r.steps : 0}手${when ? '・' + when : ''}`;
  }

  // 行の印（記録したときのデッキと今のデッキが違う）。保存していない増減のまま記録した場合も含むので「記録の後に」とは言わない
  const DRIFT_SHORT = '今のデッキと違う';

  const api = { KEY, DRIFT_SHORT, list, snapshot, forDeck, countByDeck, diff, describe, meta };
  global.DeckReplays = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
