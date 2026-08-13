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
 *   - 【2026-08-13 データ消失バグの修正】init（_ensureAccount）と push再発行は必ず
 *     購入候補・デッキ「両方」のローカル内容を送る（localStorageから直接読む。§6.3）。
 *     さらに pullIfNeeded() はローカルrevが0（＝一度もサーバーと揃っていない。旧バグで
 *     生じた壊れた端末を含む）の種別についてはpull結果を適用せず、代わりにローカルの
 *     内容をpushしてサーバー側の和集合マージで合流させる（救済ルール）。詳細は各関数内コメント
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

  // 種別ごとの localStorage キー（index.html / 一人回し 双方で共通）
  const _LOCAL_KEYS = { wishlist: 'cardprice_wishlist', decks: 'cardprice_saved_decks' };

  // 種別のローカルデータを直接読む（読み取り専用。書き込みは必ず wishSave/savedDecksSet 経由。
  // wishGet()/savedDecksGet() 等のページ側ヘルパーは存在しないページがある（一人回しに
  // wishGet は無い等）ため、キーを直接読む。localStorage は同期的に書かれる前提のため、
  // onWishSave/onDecksSave が呼ばれた時点で既にここから最新値が読める
  function _readLocalList(kind) {
    try {
      const raw = localStorage.getItem(_LOCAL_KEYS[kind]);
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed : [];
    } catch (e) {
      return [];
    }
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

  // sync_id が未発行の端末で、購入候補またはデッキのどちらか（あるいは両方）が
  // 1件以上あるときに発行する（遅延発行。§8）。onWishSave/onDecksSave のどちらが
  // きっかけでも、その時点のローカルの購入候補・デッキ「両方」を送る。
  //
  // 【2026-08-13 データ消失バグ修正】以前はきっかけになった種別のみを送り、もう一方を
  // rev=0（プレースホルダ）で開始していた。設計文書 §6.3 の init は
  // 「端末の現在の中身を初期値にする」ため本来 {wishlist, decks} の両方を受け取る仕様であり、
  // 実装がそこから外れていた。rev=0のままpullが走ると、サーバー側の空リスト（未送信のため）
  // が「新しいデータ」として wishSave([])/savedDecksSet([]) で上書き適用され、
  // ローカルにしか無かったデータが消えるバグが本番で再現した。
  // 対策として、きっかけの種別に関わらず両方を localStorage から直接読んで送り、
  // 返ってきた両方のrevを保存する（rev=0のプレースホルダを作らない）。
  function _ensureAccount() {
    const wishlist = _readLocalList('wishlist');
    const decks = _readLocalList('decks');
    if (!wishlist.length && !decks.length) return Promise.resolve(null);
    return _postJson('/api/sync/init', { wishlist: wishlist, decks: decks })
      .then(function (res) {
        if (!res.ok) return null;
        return res.json().catch(function () { return {}; });
      })
      .then(function (data) {
        if (!data || !data.ok) return null;
        const state = {
          sync_id: data.sync_id,
          wishlist_rev: data.wishlist_rev || 1,
          decks_rev: data.decks_rev || 1,
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
        // 再発行＝サーバー側で新しい行が作られた（今回pushした種別のデータのみを持つ。
        // app.pyのpush再発行経路はkind単体しか受け取れない仕様のため）。
        // 【2026-08-13修正】もう一方の種別をrev=0のまま放置すると、その後のpullで
        // サーバーの空リストを誤って適用しデータを消してしまう（_ensureAccountと同じ
        // バグ構造）。rev=0はいったんセットするが、直後にもう一方のローカル内容を
        // 同じsync_idへpushして合流させる。§3の救済ルール（pullIfNeeded）と合わせて、
        // この間に別のpullが割り込んでも空リストで上書きされることはない
        const siblingKind = kind === 'decks' ? 'wishlist' : 'decks';
        const next = { sync_id: data.sync_id, wishlist_rev: 0, decks_rev: 0 };
        next[revKey] = data[revKey] || 1;
        _setState(next);
        _notifyReissued();
        const siblingItems = _readLocalList(siblingKind);
        if (siblingItems.length) {
          _doPush(siblingKind, siblingItems);
        }
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
      // 遅延発行: init が両種別のローカル内容を初期値としてそのまま保存するため、
      // 成功時は追送不要（_ensureAccount がローカルから両方を読んで送る）
      _ensureAccount();
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

    // 【2026-08-13 救済ルール】ローカルrevが0の種別は「一度もサーバーと揃っていない」状態
    // （旧クライアントのバグで rev=0 のまま保存された、既に壊れている端末を含む）。
    // pull結果をそのまま信頼して上書き適用すると、まだサーバーに送っていないローカル
    // データが消えてしまう（サーバー側はその種別が未送信＝空のため）。
    // そのため rev=0 の種別は pull結果を適用せず、代わりにローカルの内容をpushして
    // サーバー側の和集合マージ（§4.3）で合流させる。マージ後はrevが1以上になるため、
    // 次回以降のpullは通常どおり動く（自己修復）
    const staleWishlist = !state.wishlist_rev;
    const staleDecks = !state.decks_rev;
    if (staleWishlist) {
      const local = _readLocalList('wishlist');
      if (local.length) _doPush('wishlist', local);
    }
    if (staleDecks) {
      const local = _readLocalList('decks');
      if (local.length) _doPush('decks', local);
    }

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
      if (!staleWishlist && !wl.unchanged && typeof wl.rev === 'number') {
        // 適用（wishSave経由）できた場合のみrevを進める。適用フックが無いページ
        // （例: 一人回し）では据え置き、次に適用可能なページで再取得させる
        if (_applyReceivedWishlist(wl.items)) {
          next.wishlist_rev = wl.rev;
          changed = true;
        }
      }
      const dk = data.decks || {};
      if (!staleDecks && !dk.unchanged && typeof dk.rev === 'number') {
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
