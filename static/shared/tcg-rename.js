/**
 * tcg-rename.js — 日本に来たTCG先行カードの英語名を、保存済みのデッキで日本語の正式名に書き換える（2026-10-23 段3）
 *
 * TCG先行カードは英語名でデッキに入る（段2）。日本で発売されて日本語の正式名が分かると、サーバーの
 * 切り替え表（/api/tcg-preview-renames・英語名 → 日本語名）に載る。ページを開いたときにこの表を1回取り、
 * 手元に保存されている次のものの名前を書き換えて保存し直す。書き換えた後は今の価格の流れに乗る。
 *   - 保存デッキ（cardprice_saved_decks）: 本文・メイン・EX・所持枚数。savedDecksSet 経由で保存するので端末間同期で他の端末にも届く。
 *     書き換えたデッキだけ updated を進める（同期の結合で新しい方として残すため）
 *   - 編集中の下書き（cardprice_deck_draft）: 同期しないので直接書く
 *   - 購入候補（cardprice_wishlist）: wishSave がある画面（トップ）だけ。TCG先行カードは購入候補に入れる操作を出さないが、
 *     名前の一覧が取れる前に入ったものがありうる（tcg-preview.js の説明）
 * リプレイ・保存した盤面は書き換えない（サーバーが英語名を日本語名のカードとして表示し続ける）。
 *
 * 同期との順番: 端末間同期の受信（sync-client.js pullIfNeeded）は、手元に送信待ちの変更があるとき・受信の前と手元が
 * 変わったときは適用しないので、ここで先に書き換えても上書きされない。受信の後に書き換えた場合も普通の保存として送られる。
 * 書き換えは同じ表に対して何度行っても同じ結果になる（済んだものは変えない）。
 */
(function (global) {
  'use strict';
  // 名前の照合は tcg-preview.js と同じく大文字小文字・空白・記号のゆれを吸収する
  const key = name => String(name).normalize('NFKC').toLowerCase().replace(/[\s\p{P}\p{S}]/gu, '');

  /** 切り替え表から「名前 → 日本語名（無ければ undefined）」の関数を作る */
  function lookup(renames) {
    const map = new Map();
    Object.entries(renames || {}).forEach(([en, ja]) => {
      if (typeof en === 'string' && en && typeof ja === 'string' && ja) map.set(key(en), ja);
    });
    return name => (typeof name === 'string' && map.size ? map.get(key(name)) : undefined);
  }

  /** [{qty, name}] の名前だけを書き換える。行はまとめない（同じ名前の行が2つあっても、デッキの照合は名前ごとの合計で
   *  行うので食い違わない。まとめると、TCG先行カードを含まないデッキまで「変わった」扱いになる） */
  function renameRows(rows, look) {
    if (!Array.isArray(rows)) return { rows, changed: false };
    let changed = false;
    const out = rows.map(row => {
      const ja = row && typeof row === 'object' ? look(row.name) : undefined;
      if (!ja) return row;
      changed = true;
      return { ...row, name: ja };
    });
    return { rows: changed ? out : rows, changed };
  }

  /** デッキの本文（"3 名前" の行・[EX] 区切り）の名前を書き換える。行の並び・枚数・区切りはそのまま */
  function renameText(text, look) {
    if (typeof text !== 'string' || !text) return { text, changed: false };
    let changed = false;
    const lines = text.split('\n').map(line => {
      const t = line.trim();
      if (!t || t === '[EX]') return line;
      const m = line.match(/^(\s*\d+\s+)(.+?)(\s*)$/);
      const head = m ? m[1] : line.match(/^\s*/)[0];
      const name = m ? m[2] : t;
      const tail = m ? m[3] : line.slice(line.trimEnd().length);
      const ja = look(name);
      if (!ja) return line;
      changed = true;
      return head + ja + tail;
    });
    return { text: changed ? lines.join('\n') : text, changed };
  }

  /** 所持枚数（{名前: 枚数}）のキーを書き換える。日本語名の欄が既にあれば大きい方を残す（同期の結合と同じ規則。
   *  足すと、同期で両方の欄が戻ってきたときに書き換えのたびに増えてしまう） */
  function renameOwned(owned, look) {
    if (!owned || typeof owned !== 'object' || Array.isArray(owned)) return { owned, changed: false };
    let changed = false;
    const out = {};
    Object.keys(owned).forEach(name => {
      if (!look(name)) out[name] = owned[name];
    });
    Object.keys(owned).forEach(name => {
      const ja = look(name);
      if (!ja) return;
      changed = true;
      const has = Object.prototype.hasOwnProperty.call(out, ja);
      out[ja] = has ? Math.max(Number(out[ja]) || 0, Number(owned[name]) || 0) : owned[name];
    });
    return { owned: changed ? out : owned, changed };
  }

  /** デッキ（保存デッキ・下書きの形）1つを書き換える。変わらなければ同じオブジェクトを返す */
  function renameDeck(deck, look) {
    if (!deck || typeof deck !== 'object') return { deck, changed: false };
    const t = renameText(deck.text, look);
    const m = renameRows(deck.main, look);
    const e = renameRows(deck.ex, look);
    const o = renameOwned(deck.owned, look);
    if (!t.changed && !m.changed && !e.changed && !o.changed) return { deck, changed: false };
    const next = { ...deck };
    if (t.changed) next.text = t.text;
    if (m.changed) next.main = m.rows;
    if (e.changed) next.ex = e.rows;
    if (o.changed) next.owned = o.owned;
    return { deck: next, changed: true };
  }

  /** 購入候補の名前を書き換える。書き換えた行が同じ名前・レアリティの行とぶつかったときだけ1つにまとめ、
   *  枚数とデッキごとの内訳は大きい方を取る（サーバーの結合 merge_wishlist と同じ規則。足すと、同期で英語名の行が
   *  戻ってくるたびに倍になる）。書き換えの無い行同士はまとめない */
  function renameWishlist(list, look) {
    if (!Array.isArray(list)) return { list, changed: false };
    let changed = false;
    const out = [];
    const at = new Map();          // 名前・レアリティ → out の位置（最初に出た行）
    const renamedAt = new Set();   // 書き換えた行（または書き換えた行とまとめた行）の位置
    list.forEach(item => {
      if (!item || typeof item !== 'object') { out.push(item); return; }
      const ja = look(item.name);
      const next = ja ? { ...item, name: ja } : item;
      if (ja) changed = true;
      const k = next.name + '\u0000' + (next.rarity || '');
      const i = at.get(k);
      // ぶつかった相手も自分も書き換えていないなら、元からあった重複なので触らない
      if (i === undefined || (!ja && !renamedAt.has(i))) {
        if (i === undefined) at.set(k, out.length);
        if (ja) renamedAt.add(out.length);
        out.push(next);
        return;
      }
      renamedAt.add(i);
      const prev = out[i];
      const decks = { ...(prev.decks || {}) };
      Object.entries(next.decks || {}).forEach(([d, n]) => { decks[d] = Math.max(Number(decks[d]) || 0, Number(n) || 0); });
      out[i] = { ...prev, qty: Math.max(Number(prev.qty) || 0, Number(next.qty) || 0), decks };
    });
    return { list: changed ? out : list, changed };
  }

  function readJson(storage, k) {
    try { return JSON.parse(storage.getItem(k)); } catch (e) { return null; }
  }

  /**
   * 手元の保存デッキ・下書き・購入候補を書き換えて保存する。戻り値は書き換えた件数 {decks, draft, wishlist}。
   * env は試験用（既定は window・localStorage・Date.now）
   */
  function applyToStorage(renames, env) {
    const g = (env && env.global) || global;
    const storage = (env && env.storage) || g.localStorage;
    const look = lookup(renames);
    const result = { decks: 0, draft: 0, wishlist: 0 };
    if (!storage) return result;

    const decks = readJson(storage, 'cardprice_saved_decks');
    if (Array.isArray(decks)) {
      const next = decks.map(d => {
        const r = renameDeck(d, look);
        if (!r.changed) return d;
        result.decks += 1;
        // updated は「今」ではなく1つだけ進める。サーバーの結合（sync.py merge_decks）はデッキごとに updated の大きい方を
        // 残すので、手元が最新なら書き換えが残り、別の端末で後から編集されていればそちらが残る（その中身は次に開いたとき
        // にまた書き換える）。「今」にすると、受信が間に合わなかったときに別の端末の新しい編集を消してしまう
        return { ...r.deck, updated: (Number(d.updated) || 0) + 1 };
      });
      if (result.decks) {
        // 保存は savedDecksSet 経由（端末間同期へ送る・画面の件数も直る）。無い画面では同じキーに書いて同期にだけ知らせる
        if (typeof g.savedDecksSet === 'function') g.savedDecksSet(next);
        else {
          storage.setItem('cardprice_saved_decks', JSON.stringify(next));
          if (g.SyncClient && typeof g.SyncClient.onDecksSave === 'function') g.SyncClient.onDecksSave(next);
        }
        if (typeof g.renderSavedDecks === 'function') {
          try { g.renderSavedDecks(); } catch (e) { /* 画面の描き直しに失敗しても保存は済んでいる */ }
        }
      }
    }

    const draft = readJson(storage, 'cardprice_deck_draft');
    if (draft && typeof draft === 'object' && !Array.isArray(draft)) {
      const r = renameDeck(draft, look);
      if (r.changed) {
        storage.setItem('cardprice_deck_draft', JSON.stringify(r.deck));
        result.draft = 1;
      }
    }

    // 購入候補は wishSave（バッジ・同期・通知の購読まで更新する）がある画面だけで書き換える
    if (typeof g.wishSave === 'function') {
      const wish = readJson(storage, 'cardprice_wishlist');
      const r = renameWishlist(wish, look);
      if (r.changed) {
        g.wishSave(r.list);
        result.wishlist = r.list.length;
      }
    }
    return result;
  }

  // 切り替え表の控え。次にページを開いたとき、編集画面が下書きを読み込む前（このスクリプトの実行時）に書き換えるため
  const CACHE_KEY = 'cardprice_tcg_renames';

  function applyAndLog(renames, when) {
    if (!renames || !Object.keys(renames).length) return null;
    const res = applyToStorage(renames);
    if (res.decks || res.draft || res.wishlist) console.info('[tcg-rename] 日本に来たカードの名前を書き換えました（' + when + '）', res);
    return res;
  }

  /** 切り替え表を取り、控えと違えば控えを直して書き換える。取得失敗なら何もしない（次にページを開いたときに試す） */
  function run() {
    return fetch('/api/tcg-preview-renames')
      .then(r => (r.ok ? r.json() : null))
      .then(d => {
        if (!d || !d.renames || typeof d.renames !== 'object') return null;
        const json = JSON.stringify(d.renames);
        let cached = null;
        try { cached = global.localStorage.getItem(CACHE_KEY); } catch (e) { /* 読めなければ毎回書き換えを試すだけ */ }
        if (cached === json) return null;  // 控えと同じ表は、読み込み時に書き換え済み
        try { global.localStorage.setItem(CACHE_KEY, json); } catch (e) { /* 控えを残せなくても書き換えは行う */ }
        // 表が新しくなった最初の1回は、編集画面が古い名前を読み込んだ後になる。画面の中の状態は次に開いたときに揃う
        return applyAndLog(d.renames, '表の更新');
      })
      .catch(() => null);
  }

  global.TcgRename = { lookup, renameText, renameRows, renameOwned, renameDeck, renameWishlist, applyToStorage, run };

  if (typeof global.addEventListener === 'function' && typeof fetch === 'function') {
    // 1) 控えの表で、いますぐ書き換える。トップは defer で index-wish.js（savedDecksSet・wishSave）と sync-client.js の後、
    //    DOMContentLoaded（下書きの復元・同期の受信）の前に動く。シミュレーターは保存関数のモジュールより前なので、
    //    保存デッキは同じキーに書いて同期にだけ知らせる経路になる
    try { applyAndLog(JSON.parse(global.localStorage.getItem(CACHE_KEY) || 'null'), '控え'); } catch (e) { /* 控えが壊れていれば表の取得を待つ */ }
    // 2) 表を取り直して、新しく日本に来たカードがあれば書き換える
    if (global.document && global.document.readyState === 'complete') run();
    else global.addEventListener('load', () => { run(); }, { once: true });
  }
})(window);
