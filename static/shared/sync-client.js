/**
 * sync-client.js — 購入候補の端末間同期クライアント（P1）
 *
 * 設計文書: docs/design-sync-2026-08-09.md（第2版）§4.2・§7.4・§8
 *
 * 責務:
 *   - localStorage キー 'cardprice_sync_state' に {sync_id, wishlist_rev, decks_rev} を保持する
 *   - 遅延発行: 購入候補が1件以上あるときだけ /api/sync/init を呼ぶ（全訪問者に行を作らない）
 *   - 送信は2秒デバウンス。直前送信内容と同一ならスキップ
 *   - 受信（merge結果・pull結果）は必ず wishSave() 経由で適用する。直接 localStorage には書かない
 *     （wishSave() がバッジ更新・プッシュ購読更新も行うため）
 *   - 適用中は送信フックを止めて、受信→保存→再送信の無限ループを防ぐ
 *   - 失敗（ネットワーク断・429・500）は画面に何も出さず、指数バックオフで最大3回まで再送
 *   - reissued(=sync_id再発行) を受け取ったら控えめなバナーで通知する
 *
 * index.html / 一人回し 双方から <script> で読み込む想定のため非module・IIFEで実装し、
 * window.SyncClient にAPIを公開する（static/wish-split.js と同じ形式）。
 * P1では購入候補（wishlist）のみを扱う。保存デッキ（decks）はP2で追加する。
 */
(function (global) {
  "use strict";

  const STATE_KEY = 'cardprice_sync_state';
  const DEBOUNCE_MS = 2000;
  const MAX_RETRY = 3;
  const BACKOFF_BASE_MS = 1000;

  // pull/merge結果の適用中は true。onWishSave からの再送信を止め、無限ループを防ぐ
  let _applying = false;
  let _debounceTimer = null;
  // 直前に実際へ送信した内容のシグネチャ（無変化時の再送を防ぐ）
  let _lastSentJson = null;

  function _getState() {
    try {
      const raw = localStorage.getItem(STATE_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      return null;
    }
  }
  function _setState(state) {
    try { localStorage.setItem(STATE_KEY, JSON.stringify(state)); } catch (e) {}
  }
  function _clearState() {
    try { localStorage.removeItem(STATE_KEY); } catch (e) {}
  }

  // (name, rarity, qty) だけを比較対象にした正規化文字列（無変化判定・送信内容の比較用）
  function _canonicalize(list) {
    return JSON.stringify((list || []).map(function (c) {
      return { name: c.name, rarity: c.rarity || '', qty: c.qty };
    }));
  }

  function _sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  function _postJson(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }

  // 「同期が解除されました」を控えめなバナーで数秒表示する（設計文書 §6.8）
  function _notifyReissued() {
    try {
      const el = document.createElement('div');
      el.textContent = '同期が解除されました';
      el.style.cssText = 'position:fixed;bottom:16px;left:50%;transform:translateX(-50%);'
        + 'background:rgba(0,0,0,.75);color:#fff;padding:8px 16px;border-radius:6px;'
        + 'font-size:.8rem;z-index:9999;pointer-events:none;transition:opacity .4s;opacity:1;';
      document.body.appendChild(el);
      setTimeout(function () {
        el.style.opacity = '0';
        setTimeout(function () { el.remove(); }, 400);
      }, 4000);
    } catch (e) {}
  }

  // pull/merge で受け取ったリストを wishSave() 経由で適用する（直接 localStorage に書かない。§7.4）
  function _applyReceived(items) {
    if (typeof global.wishSave !== 'function') return;
    _applying = true;
    try {
      global.wishSave(items || []);
    } finally {
      _applying = false;
    }
    // wishSave() はバッジ更新・push購読更新のみで画面上のリスト表示は更新しないため、
    // 購入候補タブが描画済みなら明示的に再描画する（_applying 解除後＝再送信フックは既に有効な
    // 状態で呼ぶが、renderWishlist() 自体は wishSave() を呼ばないため無限ループにはならない）
    if (typeof global.renderWishlist === 'function') {
      global.renderWishlist();
    }
  }

  // sync_id が未発行の端末で、購入候補が1件以上あるときだけ発行する（遅延発行。§8）
  function _ensureAccount(items) {
    if (!items.length) return Promise.resolve(null);
    return _postJson('/api/sync/init', { wishlist: items })
      .then(function (res) {
        if (!res.ok) return null;
        return res.json().catch(function () { return {}; });
      })
      .then(function (data) {
        if (!data || !data.ok) return null;
        const state = { sync_id: data.sync_id, wishlist_rev: data.wishlist_rev || 1, decks_rev: 0 };
        _setState(state);
        return state;
      })
      .catch(function () { return null; });
  }

  function _pushOnce(items) {
    const state = _getState();
    if (!state || !state.sync_id) return Promise.resolve();
    return _postJson('/api/sync/push', {
      sync_id: state.sync_id,
      kind: 'wishlist',
      base_rev: state.wishlist_rev || 0,
      items: items,
    }).then(function (res) {
      if (res.status === 429 || res.status >= 500) {
        const err = new Error('sync push retryable failure: ' + res.status);
        err.retryable = true;
        throw err;
      }
      return res.json().catch(function () { return {}; });
    }).then(function (data) {
      if (!data) return;
      if (data.reissued) {
        _setState({ sync_id: data.sync_id, wishlist_rev: data.wishlist_rev || 1, decks_rev: state.decks_rev || 0 });
        _notifyReissued();
        return;
      }
      if (!data.ok) return; // db_unavailable 等。画面には何も出さない（§4.2）
      if (data.status === 'applied') {
        _setState({ sync_id: state.sync_id, wishlist_rev: data.rev, decks_rev: state.decks_rev || 0 });
      } else if (data.status === 'merged') {
        _setState({ sync_id: state.sync_id, wishlist_rev: data.rev, decks_rev: state.decks_rev || 0 });
        _applyReceived(data.items);
      }
    });
  }

  function _pushWithRetry(items, attempt) {
    attempt = attempt || 0;
    return _pushOnce(items).catch(function (e) {
      attempt++;
      if (attempt >= MAX_RETRY) return; // 次回ページ読み込み時に解決させる（§4.2）
      return _sleep(BACKOFF_BASE_MS * Math.pow(2, attempt - 1)).then(function () {
        return _pushWithRetry(items, attempt);
      });
    });
  }

  function _doPush(list) {
    if (_applying) return; // 受信適用中は送信しない（無限ループ防止）
    const canon = _canonicalize(list);
    if (canon === _lastSentJson) return; // 直前送信内容と同一ならスキップ
    _lastSentJson = canon;

    const state = _getState();
    if (!state || !state.sync_id) {
      // 遅延発行: init が初期値として list をそのまま保存するため、成功時は追送不要
      _ensureAccount(list);
      return;
    }
    _pushWithRetry(list);
  }

  // wishSave() から呼ばれるフック。2秒デバウンスして push する（§4.2）
  function onWishSave(list) {
    if (_applying) return;
    if (_debounceTimer) clearTimeout(_debounceTimer);
    const snapshot = (list || []).map(function (c) {
      return { name: c.name, rarity: c.rarity || '', qty: c.qty };
    });
    _debounceTimer = setTimeout(function () { _doPush(snapshot); }, DEBOUNCE_MS);
  }

  // トップページ・購入候補タブを開いたときに呼ぶ（§8）。カード個別ページからは呼ばない
  // （呼ぶかどうかの判定は index.html 側が page_mode / card_name を見て行う）。
  function pullIfNeeded() {
    const state = _getState();
    if (!state || !state.sync_id) return Promise.resolve();
    return _postJson('/api/sync/pull', {
      sync_id: state.sync_id,
      wishlist_rev: state.wishlist_rev || 0,
    }).then(function (res) {
      if (!res.ok) return null; // 429/5xx は再送しない。次回ページ読み込みで再試行される
      return res.json().catch(function () { return {}; });
    }).then(function (data) {
      if (!data) return;
      if (data.reissued) {
        _clearState();
        _notifyReissued();
        return;
      }
      if (!data.ok) return;
      const wl = data.wishlist || {};
      if (wl.unchanged) return;
      if (typeof wl.rev === 'number') {
        _setState({ sync_id: state.sync_id, wishlist_rev: wl.rev, decks_rev: state.decks_rev || 0 });
        _applyReceived(wl.items);
      }
    }).catch(function () {}); // ネットワーク断等。画面には何も出さない
  }

  global.SyncClient = { onWishSave: onWishSave, pullIfNeeded: pullIfNeeded };
})(window);
