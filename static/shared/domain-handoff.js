/**
 * domain-handoff.js — 旧URL（onrender）から独自ドメインへ、ブラウザ保存領域を引き継ぐ
 *
 * 設計文書: docs/design-domain-migration-2026-10-21.md
 *
 * localStorage は origin ごとに分かれるので、ドメインを変えると保存デッキ・購入候補・
 * リプレイ・同期の状態が新しいドメインから見えなくなる。そこで:
 *   送る側（旧URLの引き継ぎページ・mode=send）:
 *     運ぶ対象のキーを毎回すべて URL の # 以降に入れて、新ドメインの /handoff へ移る
 *     （# 以降はサーバーにも GA にも送られない）。送る側は印を持たない
 *     （受け取りに失敗したあとでも、次に旧URLを開けばまた全部送れるように）
 *   受け取る側（新ドメインの /handoff・mode=receive）:
 *     旧URLから来たときだけ（document.referrer で確かめる）書き戻して、元のページへ進む。
 *     初回（gl_handoff_received_at が無い）: 新ドメインが空なら丸ごと写す。空でなければ
 *       保存デッキ・購入候補を gl_handoff_pending に置き、トップを開いたときに
 *       handoff-apply.js が合流させる（保存の関数を通して端末間同期にも載せるため）
 *     2回目以降: 前回受け取った時刻より後に保存されたデッキだけを合流させる
 *       （更新前の拡張機能が旧URLに取り込んだデッキ。全部合流させると、新ドメインで
 *       消したデッキが旧URLの古い写しから復活する）
 *
 * referrer を確かめる理由: 他人が細工した # 付きリンクを踏ませると、その人の同期の状態
 * （sync_id）が書き込まれ、以後の保存がその人に読まれてしまう。旧URLの引き継ぎページを
 * 経由した場合だけ受け取れば、運ばれるのは本人のデータだけになる。
 */
(function () {
  'use strict';

  var RECEIVED_KEY = 'gl_handoff_received_at'; // 受け取る側: 前回受け取った時刻
  var PENDING_KEY = 'gl_handoff_pending';      // 受け取る側: トップで合流させる保存デッキ・購入候補
  var DECKS_KEY = 'cardprice_saved_decks';
  var WISH_KEY = 'cardprice_wishlist';
  var SYNC_KEYS = ['cardprice_sync_state', 'cardprice_sync_unsent'];
  // id で合流させる配列（端末間同期の対象外。新ドメイン側に直接書いてよい）
  var REPLAY_KEYS = ['sol-saved-replays', 'sol-shared-replays'];
  // 運ぶキー（列挙）。管理画面の鍵・プッシュ購読済みの印（購読は origin ごと）・一時キャッシュは運ばない。
  // 列挙にしておくと、確認ボタンの経路で細工された中身でも、これ以外のキーは書かれない
  var CARRY_EXACT = [DECKS_KEY, WISH_KEY, 'cardprice_deck_draft', 'rp_public_first_count', 'tcgym_sol_sample_blur']
    .concat(SYNC_KEYS);
  var CARRY_PREFIXES = ['sol-', 'card-sim-save-slot-'];
  // # 以降の長さの上限。超えたら自動では運ばず案内を出す（主要ブラウザの URL 上限より十分小さく）
  var MAX_FRAGMENT = 1000000;

  function isCarried(key) {
    if (CARRY_EXACT.indexOf(key) !== -1) return true;
    return CARRY_PREFIXES.some(function (p) { return key.indexOf(p) === 0; });
  }

  var cfgEl = document.getElementById('handoffConfig');
  if (!cfgEl) return;
  var cfg = cfgEl.dataset;

  function setStatus(text) {
    var el = document.getElementById('handoffStatus');
    if (el) el.textContent = text;
  }
  function showFallback(text, linkText, href) {
    var el = document.getElementById('handoffFallback');
    if (!el) return;
    el.textContent = text ? text + ' ' : '';
    var a = document.createElement('a');
    a.href = href;
    a.textContent = linkText;
    el.appendChild(a);
    el.classList.remove('hidden');
  }

  function lsGet(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
  function lsSet(key, value) { localStorage.setItem(key, value); }
  function parseList(raw) {
    try { var v = JSON.parse(raw); return Array.isArray(v) ? v : []; } catch (e) { return []; }
  }

  // ── 文字列 ⇔ # 以降（圧縮できる環境では deflate-raw。z. / j. の接頭辞で区別）──
  function toB64url(bytes) {
    var s = '';
    for (var i = 0; i < bytes.length; i += 0x8000) {
      s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }
  function fromB64url(text) {
    var b64 = text.replace(/-/g, '+').replace(/_/g, '/');
    while (b64.length % 4) b64 += '=';
    var bin = atob(b64);
    var bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  }
  function streamBytes(bytes, stream) {
    return new Response(new Blob([bytes]).stream().pipeThrough(stream)).arrayBuffer()
      .then(function (buf) { return new Uint8Array(buf); });
  }
  function encodePayload(obj) {
    var bytes = new TextEncoder().encode(JSON.stringify(obj));
    if (typeof CompressionStream === 'function') {
      return streamBytes(bytes, new CompressionStream('deflate-raw'))
        .then(function (z) { return 'z.' + toB64url(z); })
        .catch(function () { return 'j.' + toB64url(bytes); });
    }
    return Promise.resolve('j.' + toB64url(bytes));
  }
  function decodePayload(text) {
    var kind = text.slice(0, 2);
    var p;
    try {
      var bytes = fromB64url(text.slice(2));
      if (kind === 'z.') {
        if (typeof DecompressionStream !== 'function') return Promise.reject(new Error('no_decompress'));
        p = streamBytes(bytes, new DecompressionStream('deflate-raw'));
      } else if (kind === 'j.') {
        p = Promise.resolve(bytes);
      } else {
        return Promise.reject(new Error('bad_format'));
      }
    } catch (e) {
      return Promise.reject(e);
    }
    return p.then(function (b) { return JSON.parse(new TextDecoder().decode(b)); });
  }

  // ══════════ 送る側（旧URL）══════════

  // 更新前の拡張機能（v1.3）は ?tcgym_import=1 で旧URLを開き、取り込み画面を出して旧URLの
  // 保存領域にデッキを書く。その画面が閉じる（保存→再読み込み／キャンセル）まで待ってから運ぶ。
  // 拡張機能が無い・データが無いときは、引数が残ったままなので数秒で打ち切る
  function waitForExtensionImport() {
    if (!new URLSearchParams(location.search).has('tcgym_import')) return Promise.resolve();
    var started = Date.now();
    return new Promise(function (resolve) {
      (function poll() {
        var hasParam = new URLSearchParams(location.search).has('tcgym_import');
        var modalOpen = !!document.querySelector('.tdi-overlay');
        if (modalOpen) setStatus('デッキの取り込みが終わると、新しいアドレスへ移動します。');
        if (!modalOpen && (!hasParam || Date.now() - started > 5000)) { resolve(); return; }
        setTimeout(poll, 200);
      })();
    });
  }

  function buildSendPayload() {
    var items = {};
    var count = 0;
    try {
      for (var i = 0; i < localStorage.length; i++) {
        var key = localStorage.key(i);
        if (key === null || !isCarried(key)) continue;
        items[key] = localStorage.getItem(key);
        count++;
      }
    } catch (e) { return null; }
    return count ? { v: 2, items: items } : null;
  }

  function runSend() {
    var base = cfg.canonicalBase;
    var target = cfg.targetPath || '/';
    waitForExtensionImport().then(function () {
      var payload = buildSendPayload();
      if (!payload) { location.replace(base + target); return; }
      return encodePayload(payload).then(function (encoded) {
        if (encoded.length > MAX_FRAGMENT) {
          setStatus('保存されたデータが大きいため、自動で引き継げませんでした。');
          showFallback('データは今のアドレスに残っています。', 'データを引き継がずに新しいアドレスへ移動する', base + target);
          return;
        }
        location.replace(base + '/handoff?next=' + encodeURIComponent(target) + '#' + encoded);
      });
    }).catch(function () {
      location.replace(base + target);
    });
  }

  // ══════════ 受け取る側（新ドメイン）══════════

  // 移った先は同じ origin のパスだけ。URL として解析してから origin を比べる
  // （'/\t/evil.com' のような、文字列の見た目では弾けない入力があるため）。# は捨て、/handoff は避ける
  function safeNext(path) {
    if (!path) return '/';
    var u;
    try { u = new URL(path, location.origin); } catch (e) { return '/'; }
    if (u.origin !== location.origin || u.pathname === '/handoff') return '/';
    return u.pathname + u.search;
  }

  function hasLocalData() {
    return parseList(lsGet(DECKS_KEY)).length > 0
      || parseList(lsGet(WISH_KEY)).length > 0
      || parseList(lsGet('sol-saved-replays')).length > 0
      || !!lsGet('cardprice_sync_state');
  }

  function addPending(decks, wishlist) {
    var cur = {};
    try { cur = JSON.parse(lsGet(PENDING_KEY)) || {}; } catch (e) { cur = {}; }
    var next = {
      decks: (Array.isArray(cur.decks) ? cur.decks : []).concat(decks || []),
      wishlist: (Array.isArray(cur.wishlist) ? cur.wishlist : []).concat(wishlist || []),
    };
    lsSet(PENDING_KEY, JSON.stringify(next));
  }

  function mergeById(key, incoming) {
    var cur = parseList(lsGet(key));
    var ids = {};
    cur.forEach(function (r) { if (r && r.id != null) ids[r.id] = true; });
    var added = incoming.filter(function (r) { return r && r.id != null && !ids[r.id]; });
    if (added.length) lsSet(key, JSON.stringify(cur.concat(added)));
  }

  // trustSync=false のときは同期の状態（sync_id）を写さない（旧URLから来たと確かめられないとき）
  function applyPayload(payload, trustSync) {
    if (!payload || payload.v !== 2 || !payload.items || typeof payload.items !== 'object') throw new Error('bad_payload');
    var items = payload.items;
    var keys = Object.keys(items).filter(function (k) {
      return typeof items[k] === 'string' && isCarried(k) && (trustSync || SYNC_KEYS.indexOf(k) === -1);
    });
    var received = parseInt(lsGet(RECEIVED_KEY), 10) || 0;

    if (received) {
      // 2回目以降: 前回より後に旧URLで保存されたデッキだけ
      var newer = parseList(items[DECKS_KEY]).filter(function (d) {
        return d && (Number(d.updated) || 0) > received;
      });
      if (newer.length) addPending(newer, []);
    } else if (!hasLocalData()) {
      // 新ドメインはまだ空: 丸ごと写す（同期の状態も写るので、同じ同期アカウントを使い続ける）
      keys.forEach(function (k) { lsSet(k, items[k]); });
    } else {
      // 新ドメインを先に使っていた: 保存デッキ・購入候補はトップで合流、リプレイは id で足す、
      // それ以外は新に無いものだけ足す。同期の状態は新側を残す
      // （合流させた内容は保存の関数を通して新ドメイン側の同期に載る）
      addPending(keys.indexOf(DECKS_KEY) !== -1 ? parseList(items[DECKS_KEY]) : [],
                 keys.indexOf(WISH_KEY) !== -1 ? parseList(items[WISH_KEY]) : []);
      keys.forEach(function (k) {
        if (k === DECKS_KEY || k === WISH_KEY || SYNC_KEYS.indexOf(k) !== -1) return;
        if (REPLAY_KEYS.indexOf(k) !== -1) { mergeById(k, parseList(items[k])); return; }
        if (lsGet(k) === null) lsSet(k, items[k]);
      });
    }
    lsSet(RECEIVED_KEY, String(Date.now()));
  }

  function runReceive() {
    var encoded = location.hash.slice(1);
    var next = safeNext(new URLSearchParams(location.search).get('next'));
    // 運んできた中身を画面のURLに残さない
    try { history.replaceState(null, '', location.pathname + location.search); } catch (e) {}
    if (!encoded) { location.replace(next); return; }

    var legacy = (cfg.legacyOrigins || '').split(' ').filter(Boolean);
    var refOrigin = '';
    try { refOrigin = new URL(document.referrer).origin; } catch (e) {}
    var fromLegacy = refOrigin !== location.origin && legacy.indexOf(refOrigin) !== -1;

    function apply(trustSync) {
      return decodePayload(encoded).then(function (payload) {
        applyPayload(payload, trustSync);
        location.replace(next);
      }).catch(function () {
        setStatus('データの引き継ぎに失敗しました。データは以前のアドレスに残っています。');
        // 送る側は毎回全部送るので、旧URLを開き直せばもう一度引き継げる
        showFallback('', 'もう一度引き継ぐ', (fromLegacy ? refOrigin : (legacy[0] || '')) + next);
      });
    }

    if (fromLegacy) { apply(true); return; }
    if (refOrigin) {
      // 旧URL以外（自分自身を含む）から来た: 細工されたリンクの恐れがあるので受け取らない
      setStatus('このリンクからはデータを引き継げません。');
      showFallback('以前のアドレスから開き直すと、保存したデッキなどを引き継げます。', 'このまま強欲なラボを開く', next);
      return;
    }
    // 送り元が分からない（ブラウザの設定で referrer が送られない等）: 本人に確かめてから受け取る。
    // このときは同期の状態は写さない（他人の同期アカウントに合流させられるのを防ぐ）
    setStatus('以前のアドレス（tcg-price-compare.onrender.com）で保存したデッキなどを、この端末に引き継ぎますか？');
    var btn = document.getElementById('handoffConfirm');
    var note = document.getElementById('handoffNote');
    if (note) note.classList.remove('hidden');
    if (btn) {
      btn.classList.remove('hidden');
      btn.addEventListener('click', function () { btn.disabled = true; apply(false); });
    }
    showFallback('', '引き継がずに開く', next);
  }

  if (cfg.mode === 'send') runSend();
  else if (cfg.mode === 'receive') runReceive();
})();
