/**
 * sync-client.js — 購入候補・保存デッキの端末間同期クライアント
 *
 * 設計文書: docs/design-sync-2026-08-09.md（第2版）§4.2・§7.4・§8・§11
 *
 * 責務:
 *   - localStorage キー 'cardprice_sync_state' に {sync_id, wishlist_rev, decks_rev} を保持する
 *   - 遅延発行: 購入候補またはデッキが1件以上あるときだけ /api/sync/init を呼ぶ
 *     （全訪問者に行を作らない。どちらか一方の保存操作が単独できっかけになる）
 *   - 送信は種別（購入候補／デッキ）ごとに独立して2秒デバウンス。直前送信内容と同一ならスキップ
 *   - 受信（merge結果・pull結果）は必ず wishSave() / savedDecksSet() 経由で適用する。
 *     直接 localStorage には書かない
 *     （wishSave() がバッジ更新・プッシュ購読更新も行うため。§7.4）
 *   - 適用中は送信フックを止めて、受信→保存→再送信の無限ループを防ぐ
 *   - 失敗（ネットワーク断・429・500）は画面に何も出さず、指数バックオフで最大3回まで再送
 *   - reissued(=sync_id再発行) を受け取ったら控えめなバナーで通知する
 *   - pullIfNeeded() は購入候補・保存デッキの両方を1回のAPI呼び出しで扱う。ページによっては
 *     片方の適用フック（wishSave/renderWishlist または savedDecksSet/renderSavedDecks）が
 *     存在しないことがある（例: 一人回しページには購入候補のUIが無い）。その場合は
 *     該当種別のリビジョンをローカルで進めない（=次に両方揃ったページを開いたときに
 *     再取得・再適用される）。適用できていないのにrevだけ進めるとデータが永久に
 *     取りこぼされるため
 *
 * index.html / 一人回し 双方から <script> で読み込む想定のため非module・IIFEで実装し、
 * window.SyncClient にAPIを公開する（static/wish-split.js と同じ形式）。
 */
(function (global) {
  "use strict";

  const STATE_KEY = 'cardprice_sync_state';
  const DEBOUNCE_MS = 2000;
  const MAX_RETRY = 3;
  const BACKOFF_BASE_MS = 1000;

  // pull/merge結果の適用中は true。onWishSave/onDecksSave からの再送信を止め、無限ループを防ぐ
  let _applying = false;

  // 種別ごとのデバウンスタイマー・直前送信シグネチャ（購入候補とデッキが互いを妨げないように分離）
  const _kindState = {
    wishlist: { debounceTimer: null, lastSentJson: null },
    decks:    { debounceTimer: null, lastSentJson: null },
  };

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

  // 種別ごとの正規化（無変化判定・送信内容の比較用）
  function _canonicalize(kind, list) {
    if (kind === 'decks') {
      return JSON.stringify((list || []).map(function (d) {
        return { id: d.id, name: d.name, text: d.text || '', main: d.main || [], ex: d.ex || [],
                 updated: d.updated || 0 };
      }));
    }
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

  // pull/merge で受け取った購入候補を wishSave() 経由で適用する（直接 localStorage に書かない。§7.4）
  // 適用できた（＝wishSave が存在した）場合のみ true を返す。呼び出し側はこれを見て
  // ローカルの wishlist_rev を進めるかどうかを判断する
  function _applyReceivedWishlist(items) {
    if (typeof global.wishSave !== 'function') return false;
    _applying = true;
    try {
      global.wishSave(items || []);
    } finally {
      _applying = false;
    }
    // wishSave() はバッジ更新・push購読更新のみで画面上のリスト表示は更新しないため、
    // 購入候補タブが描画済みなら明示的に再描画する
    if (typeof global.renderWishlist === 'function') {
      global.renderWishlist();
    }
    return true;
  }

  // pull/merge で受け取った保存デッキを savedDecksSet() 経由で適用する（直接 localStorage に書かない）
  function _applyReceivedDecks(items) {
    if (typeof global.savedDecksSet !== 'function') return false;
    _applying = true;
    try {
      global.savedDecksSet(items || []);
    } finally {
      _applying = false;
    }
    if (typeof global.renderSavedDecks === 'function') {
      global.renderSavedDecks();
    }
    return true;
  }

  // sync_id が未発行の端末で、対象種別が1件以上あるときだけ発行する（遅延発行。§8）。
  // 遅延発行の条件は「購入候補またはデッキが1件以上」（設計文書 §8）。onWishSave/onDecksSave の
  // どちらが先に1件以上を持つかに関わらず、どちらの保存操作でも単独できっかけになる
  function _ensureAccount(kind, items) {
    if (!items.length) return Promise.resolve(null);
    const payload = kind === 'decks' ? { decks: items } : { wishlist: items };
    return _postJson('/api/sync/init', payload)
      .then(function (res) {
        if (!res.ok) return null;
        return res.json().catch(function () { return {}; });
      })
      .then(function (data) {
        if (!data || !data.ok) return null;
        // もう一方の種別は未送信のため rev=0（=サーバーと必ず不一致）で始める。
        // 次にそちら側がpushされた際、条件付き更新が0行で失敗→競合経路のマージで
        // 自然に解決される（P1から続く自己修復の仕組み。§4.3）
        const state = {
          sync_id: data.sync_id,
          wishlist_rev: kind === 'wishlist' ? (data.wishlist_rev || 1) : 0,
          decks_rev: kind === 'decks' ? (data.decks_rev || 1) : 0,
        };
        _setState(state);
        return state;
      })
      .catch(function () { return null; });
  }

  function _pushOnce(kind, items) {
    const state = _getState();
    if (!state || !state.sync_id) return Promise.resolve();
    const revKey = kind === 'decks' ? 'decks_rev' : 'wishlist_rev';
    return _postJson('/api/sync/push', {
      sync_id: state.sync_id,
      kind: kind,
      base_rev: state[revKey] || 0,
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
        // 再発行＝新しい行。今回pushした種別のrevだけ反映し、もう片方は0から
        // 自己修復させる（_ensureAccount と同じ考え方）
        const next = { sync_id: data.sync_id, wishlist_rev: 0, decks_rev: 0 };
        next[revKey] = data[revKey] || 1;
        _setState(next);
        _notifyReissued();
        return;
      }
      if (!data.ok) return; // db_unavailable 等。画面には何も出さない（§4.2）
      if (data.status === 'applied') {
        const next = Object.assign({}, state);
        next[revKey] = data.rev;
        _setState(next);
      } else if (data.status === 'merged') {
        const next = Object.assign({}, state);
        next[revKey] = data.rev;
        _setState(next);
        if (kind === 'decks') _applyReceivedDecks(data.items);
        else _applyReceivedWishlist(data.items);
      }
    });
  }

  function _pushWithRetry(kind, items, attempt) {
    attempt = attempt || 0;
    return _pushOnce(kind, items).catch(function (e) {
      attempt++;
      if (attempt >= MAX_RETRY) return; // 次回ページ読み込み時に解決させる（§4.2）
      return _sleep(BACKOFF_BASE_MS * Math.pow(2, attempt - 1)).then(function () {
        return _pushWithRetry(kind, items, attempt);
      });
    });
  }

  function _doPush(kind, list) {
    if (_applying) return; // 受信適用中は送信しない（無限ループ防止）
    const ks = _kindState[kind];
    const canon = _canonicalize(kind, list);
    if (canon === ks.lastSentJson) return; // 直前送信内容と同一ならスキップ
    ks.lastSentJson = canon;

    const state = _getState();
    if (!state || !state.sync_id) {
      // 遅延発行: init が初期値として list をそのまま保存するため、成功時は追送不要
      _ensureAccount(kind, list);
      return;
    }
    _pushWithRetry(kind, list);
  }

  // wishSave() から呼ばれるフック。2秒デバウンスして push する（§4.2）
  function onWishSave(list) {
    if (_applying) return;
    const ks = _kindState.wishlist;
    if (ks.debounceTimer) clearTimeout(ks.debounceTimer);
    const snapshot = (list || []).map(function (c) {
      return { name: c.name, rarity: c.rarity || '', qty: c.qty };
    });
    ks.debounceTimer = setTimeout(function () { _doPush('wishlist', snapshot); }, DEBOUNCE_MS);
  }

  // savedDecksSet() から呼ばれるフック（P2）。2秒デバウンスして push する（§4.2）
  function onDecksSave(list) {
    if (_applying) return;
    const ks = _kindState.decks;
    if (ks.debounceTimer) clearTimeout(ks.debounceTimer);
    const snapshot = (list || []).map(function (d) {
      return { id: d.id, name: d.name, text: d.text || '', main: d.main || [], ex: d.ex || [],
               updated: d.updated || 0 };
    });
    ks.debounceTimer = setTimeout(function () { _doPush('decks', snapshot); }, DEBOUNCE_MS);
  }

  // トップページ・購入候補タブ・マイデッキタブ・一人回しページを開いたときに呼ぶ（§8）。
  // カード個別ページの閲覧では走らせない（呼ぶかどうかの判定は各ページ側が行う）。
  // 購入候補・保存デッキの両方を1回のAPI呼び出しで扱う
  function pullIfNeeded() {
    const state = _getState();
    if (!state || !state.sync_id) return Promise.resolve();
    return _postJson('/api/sync/pull', {
      sync_id: state.sync_id,
      wishlist_rev: state.wishlist_rev || 0,
      decks_rev: state.decks_rev || 0,
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

      const next = Object.assign({}, state);
      let changed = false;

      const wl = data.wishlist || {};
      if (!wl.unchanged && typeof wl.rev === 'number') {
        // 適用（wishSave経由）できた場合のみrevを進める。適用フックが無いページ
        // （例: 一人回し）では据え置き、次に適用可能なページで再取得させる
        if (_applyReceivedWishlist(wl.items)) {
          next.wishlist_rev = wl.rev;
          changed = true;
        }
      }
      const dk = data.decks || {};
      if (!dk.unchanged && typeof dk.rev === 'number') {
        if (_applyReceivedDecks(dk.items)) {
          next.decks_rev = dk.rev;
          changed = true;
        }
      }
      if (changed) _setState(next);
    }).catch(function () {}); // ネットワーク断等。画面には何も出さない
  }

  global.SyncClient = { onWishSave: onWishSave, onDecksSave: onDecksSave, pullIfNeeded: pullIfNeeded };
})(window);
