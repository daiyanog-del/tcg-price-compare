/**
 * wish-deck.js — 購入候補の「どのデッキ用か」の内訳（2026-10-20・decisions 同日）
 *
 * 購入候補の1行 {name, rarity, qty} に decks: {デッキID: 枚数} を足す。
 *   - qty は今まで通りその行の合計枚数（店の比較・送料計算はこれを使う）
 *   - decks の合計は qty 以下。足りない分（qty − 内訳の合計）は「デッキ指定なし」
 *   - 同じカードが2つのデッキで足りないときは合計で数える（デッキA 2枚＋デッキB 3枚＝5枚。ユーザー決定）
 * 内訳を持たない古い行はすべて「デッキ指定なし」として読める（移行は不要）。
 *
 * トップ（index-wish.js・deck-ownership.js）とシミュレーター（deck-edit-ui.js）の両方から使う。
 * DOM と storage には触らない純粋な関数だけを置く（tests/js/wish_deck_check.js から直接読む）。
 */
(function (global) {
  'use strict';
  const QTY_MAX = 99;     // 購入候補の1行の上限（index-wish.js・sync.py と同じ）
  const ITEMS_MAX = 200;  // 購入候補の件数の上限（sync.py MAX_WISHLIST_ITEMS と同じ）
  const DECKS_PER_ITEM_MAX = 50; // 1行の内訳の件数の上限（sync.py MAX_WISH_DECKS_PER_ITEM・保存デッキの上限と同じ）
  const has = (o, k) => Object.prototype.hasOwnProperty.call(o, k);

  /** 内訳を検証して返す（不正な値は落とす）。合計が qty を超える分は後ろの内訳から削る。
   *  sync.py の _clean_wish_decks と同じ規則。ただし JS は数字だけのキーを先頭に並べ替えるので、
   *  そういう ID があると削られる内訳が Python と変わりうる（保存デッキの ID は 'd_' + 時刻なので起きない） */
  function cleanDecks(decks, qty) {
    const out = {};
    if (!decks || typeof decks !== 'object' || Array.isArray(decks)) return out;
    let left = qty;
    for (const id of Object.keys(decks).slice(0, DECKS_PER_ITEM_MAX)) {
      const n = decks[id];
      if (typeof id !== 'string' || !id || id.length > 40 || !Number.isInteger(n) || n < 1) continue;
      const take = Math.min(n, left);
      if (take < 1) break;
      out[id] = take;
      left -= take;
    }
    return out;
  }

  /** 1行を正規化した写しを返す（内訳が空なら decks を持たない） */
  function normalizeItem(c) {
    // 文字列の枚数（"3"）も読む（以前の wishGet は値をそのまま通していた）
    const n = Math.trunc(Number(c.qty));
    const qty = Math.max(1, Math.min(QTY_MAX, Number.isFinite(n) ? n : 1));
    const item = { name: c.name, qty, rarity: c.rarity || '' };
    const decks = cleanDecks(c.decks, qty);
    if (Object.keys(decks).length) item.decks = decks;
    return item;
  }

  function assigned(item) {
    return Object.values(item.decks || {}).reduce((a, n) => a + n, 0);
  }

  /** その行のうち、デッキ指定なしの枚数 */
  function unassigned(item) {
    return Math.max(0, item.qty - assigned(item));
  }

  /** その行のうち、deckId 用の枚数 */
  function forDeck(item, deckId) {
    return deckId && item.decks && has(item.decks, deckId) ? item.decks[deckId] : 0;
  }

  /** カード名ごとに、deckId 用の枚数・デッキ指定なしの枚数・全体の枚数を数える（レアリティはまとめる） */
  function countByName(list, deckId) {
    const m = new Map();
    for (const c of list) {
      const r = m.get(c.name) || { deck: 0, free: 0, total: 0 };
      r.deck += forDeck(c, deckId);
      r.free += unassigned(c);
      r.total += c.qty;
      m.set(c.name, r);
    }
    return m;
  }

  /**
   * name を n 枚足す。deckId があればそのデッキ用として数える（無ければデッキ指定なし）。
   * レアリティ指定なしの行に足す（無ければ作る）。上限を超えるときは Error。
   * @returns {Array} 新しい一覧（元の配列は変えない）
   */
  function add(list, name, n, deckId) {
    const next = list.map(c => ({ ...c, decks: c.decks ? { ...c.decks } : undefined }));
    let item = next.find(c => c.name === name && !(c.rarity || ''));
    if ((item ? item.qty : 0) + n > QTY_MAX) throw Error('購入候補の枚数上限（99枚）を超えます。');
    if (!item) {
      if (next.length >= ITEMS_MAX) throw Error('購入候補は最大200件です。先に候補を整理してください。');
      item = { name, qty: 0, rarity: '' };
      next.push(item);
    }
    item.qty += n;
    if (deckId) {
      item.decks = item.decks || {};
      item.decks[deckId] = (item.decks[deckId] || 0) + n;
    }
    return next.map(_strip);
  }

  /**
   * name を n 枚減らす。deckId 用の分から先に減らし、足りなければデッキ指定なしの分から減らす
   * （他のデッキ用の分は減らさない）。onlyDeck なら deckId 用の分だけ減らす（購入候補画面でデッキに絞り込んだ −）。
   * deckId が '' または null ならデッキ指定なしの分だけ減らす。0枚になった行は消す。
   * @returns {{list: Array, removed: number}}
   */
  function remove(list, name, n, deckId, opts) {
    const onlyDeck = !!(opts && opts.onlyDeck) && !!deckId;
    const next = list.map(c => ({ ...c, decks: c.decks ? { ...c.decks } : undefined }));
    let left = n;
    for (const item of next) {
      if (item.name !== name || left < 1) continue;
      const mine = forDeck(item, deckId);
      const fromDeck = Math.min(mine, left);
      if (fromDeck) {
        item.decks[deckId] -= fromDeck;
        if (!item.decks[deckId]) delete item.decks[deckId];
        item.qty -= fromDeck;
        left -= fromDeck;
      }
      const fromFree = onlyDeck ? 0 : Math.min(unassigned(item), left);
      item.qty -= fromFree;
      left -= fromFree;
    }
    return { list: next.filter(c => c.qty > 0).map(_strip), removed: n - left };
  }

  /**
   * 枚数を手で変えたとき（購入候補画面の −/＋）。減らすときはデッキ指定なしの分から削り、
   * それでも足りなければ内訳を後ろから削る（内訳の合計が qty を超えないように）。
   */
  function setQty(item, qty) {
    const next = { ...item, qty, decks: item.decks ? { ...item.decks } : undefined };
    next.decks = cleanDecks(next.decks, qty);
    return _strip(next);
  }

  /** 同じカードの2行を1行にまとめる（レアリティを変えて別の行とぶつかったとき）。内訳は足し合わせる */
  function mergeInto(target, source) {
    const decks = { ...(target.decks || {}) };
    for (const [id, n] of Object.entries(source.decks || {})) decks[id] = (decks[id] || 0) + n;
    const qty = Math.min(QTY_MAX, target.qty + source.qty);
    return _strip({ ...target, qty, decks: cleanDecks(decks, qty) });
  }

  /**
   * 絞り込み。deckId の分だけの一覧（枚数はそのデッキ用の枚数）を返す。
   * deckId が '' ならデッキ指定なしの分だけ。null なら全部（そのまま）。
   */
  function filter(list, deckId) {
    if (deckId === null || deckId === undefined) return list;
    return list
      .map(c => ({ ...c, qty: deckId === '' ? unassigned(c) : forDeck(c, deckId) }))
      .filter(c => c.qty > 0)
      .map(c => { const o = { name: c.name, qty: c.qty, rarity: c.rarity || '' }; return o; });
  }

  /**
   * 今は無いデッキ（削除したデッキ）用の内訳を外し、デッキ指定なしとして数える一覧を返す。knownIds に無い ID の分は unassigned に入る。
   * 表示・計算用。保存に使うときは onlyName でその行だけにかける
   * （「この端末に無いデッキ」には、別の端末で作ってまだ同期が届いていないデッキも含まれるため。全行にかけて保存すると
   *   それらの内訳まで消え、空の内訳として同期で全端末に広がる。2026-10-20 reviewer 指摘）
   */
  function forgetUnknown(list, knownIds, onlyName) {
    const known = new Set(knownIds || []);
    return list.map(c => {
      if (!c.decks || (onlyName !== undefined && c.name !== onlyName)) return c;
      const decks = {};
      for (const [id, n] of Object.entries(c.decks)) if (known.has(id)) decks[id] = n;
      return _strip({ ...c, decks });
    });
  }

  /** そのデッキ用の内訳を全行から外す（デッキを削除したとき。候補自体は残り、デッキ指定なしになる） */
  function dropDeck(list, deckId) {
    return list.map(c => {
      if (!c.decks || !has(c.decks, deckId)) return c;
      const decks = { ...c.decks };
      delete decks[deckId];
      return _strip({ ...c, decks });
    });
  }

  /** 一覧に出てくるデッキID（内訳の順） */
  function deckIds(list) {
    const ids = [];
    for (const c of list) for (const id of Object.keys(c.decks || {})) if (!ids.includes(id)) ids.push(id);
    return ids;
  }

  /**
   * デッキの不足分から、購入候補に足す枚数を出す（トップの所持枚数欄・シミュレーターの窓で共通）。
   * 「候補にある」は、そのデッキ用の分＋デッキ指定なしの分（他のデッキ用の分は数えない＝合計で数える）。
   * 持っている枚数が未入力（owned に無い）カードは足さない。
   * @param {Map<string,number>} totals カード名 → デッキに入っている枚数（メイン＋EX）
   * @param {Object} owned カード名 → 持っている枚数
   * @param {Array} list 購入候補
   * @param {string|null} deckId
   */
  function plan(totals, owned, list, deckId) {
    const counts = countByName(list, deckId);
    return [...totals].map(([name, required]) => {
      const checked = !!owned && has(owned, name);
      const c = counts.get(name) || { deck: 0, free: 0 };
      const candidate = c.deck + c.free;
      const shortage = checked ? Math.max(0, required - owned[name]) : null;
      return {
        name, required, owned: checked ? owned[name] : null, shortage, candidate,
        rarities: list.filter(x => x.name === name).map(x => ({ rarity: x.rarity || '', qty: x.qty })),
        add: checked ? Math.max(0, shortage - candidate) : 0,
      };
    });
  }

  /** plan の結果の add を購入候補に足す（deckId 用として数える） */
  function supplement(list, rows, deckId) {
    let next = list;
    for (const r of rows) if (r.add > 0) next = add(next, r.name, r.add, deckId);
    return next;
  }

  function _strip(c) {
    const o = { name: c.name, qty: c.qty, rarity: c.rarity || '' };
    if (c.decks && Object.keys(c.decks).length) o.decks = c.decks;
    return o;
  }

  const api = { normalizeItem, unassigned, forDeck, countByName, add, remove, setQty, mergeInto, filter, forgetUnknown, dropDeck, deckIds, plan, supplement, QTY_MAX };
  global.WishDeck = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
