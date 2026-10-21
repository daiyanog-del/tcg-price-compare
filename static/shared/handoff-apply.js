/**
 * handoff-apply.js — 旧URLから運ばれた保存デッキ・購入候補を、トップを開いたときに合流させる
 *
 * 設計文書: docs/design-domain-migration-2026-10-21.md
 * /handoff（domain-handoff.js）は、新ドメイン側に既にデータがあるとき、運ばれた分を
 * gl_handoff_pending に置くだけにする。ここで index のグローバル関数
 * （savedDecksGet/Set・wishGet/wishSave）を通して足すことで、端末間同期にも載せる。
 *
 * 呼ぶのは index-sync.js（同期の pull が終わってから）。先に合流させると、購入候補には
 * 保存デッキのような「送信待ち」の保護が無いので、サーバー側が新しいときに pull が合流後の
 * 手元を上書きし、その後の送信がサーバーにだけあった候補を消す（2026-10-21 reviewer 指摘）。
 * pull の後なら、合流させた内容は最新の rev の上に送られる。
 */
(function () {
  'use strict';

  var PENDING_KEY = 'gl_handoff_pending';

  function applyPending() {
    var raw;
    try { raw = localStorage.getItem(PENDING_KEY); } catch (e) { return; }
    if (!raw) return;
    if (typeof savedDecksGet !== 'function' || typeof savedDecksSet !== 'function'
        || typeof wishGet !== 'function' || typeof wishSave !== 'function') return;

    var pending;
    try { pending = JSON.parse(raw) || {}; } catch (e) { pending = {}; }

    var decks = Array.isArray(pending.decks) ? pending.decks : [];
    if (decks.length) {
      var list = savedDecksGet();
      decks.forEach(function (d) {
        if (!d || typeof d !== 'object' || !d.id) return;
        var i = list.findIndex(function (x) { return x.id === d.id; });
        if (i === -1) list.push(d);
        else if ((Number(d.updated) || 0) > (Number(list[i].updated) || 0)) list[i] = d;
      });
      savedDecksSet(list);
      if (typeof renderSavedDecks === 'function') renderSavedDecks();
    }

    var wishes = Array.isArray(pending.wishlist) ? pending.wishlist : [];
    if (wishes.length) {
      var wl = wishGet();
      wishes.forEach(function (c) {
        if (!c || typeof c !== 'object' || !c.name) return;
        var exists = wl.some(function (x) { return x.name === c.name && (x.rarity || '') === (c.rarity || ''); });
        if (!exists) wl.push(c);
      });
      wishSave(wl);
      if (typeof renderWishlist === 'function') renderWishlist();
    }

    try { localStorage.removeItem(PENDING_KEY); } catch (e) {}
  }

  window.GLHandoff = { applyPending: applyPending };
})();
