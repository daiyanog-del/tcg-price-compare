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
 * 【P3: ワンタイムリンク（§6.4〜§6.7・§7.1〜§7.2）】
 *   - startLinkShare(): 発行側（index.html）の「他の端末でも見る」ボタンから呼ぶ。
 *     sync_id 未発行の端末では、ローカルにデータが1件以上あれば先に init してから
 *     link/create を呼ぶ（§8 遅延発行の条件どおり）。データが無ければ呼び出し側に
 *     reason:"no_local_data" を返す（index.html 側で案内文を出す）
 *   - unlinkThisDevice(): 「同期を解除」から呼ぶ。/api/sync/unlink を呼び、返ってきた
 *     新しい sync_id・リビジョンで状態を張り替える
 *   - previewLink(token) / redeemLink(token): 引き換え側（/sync ページ）から呼ぶ。
 *     redeemLink() は成功時に applyRedeemResult() を内部で呼び、結果を適用する
 *   - applyRedeemResult(result): 「受け取った内容と rev を保存して合流を完了する」関数。
 *     /sync ページには index.html の完全な wishSave()/savedDecksSet()（バッジ更新・
 *     push購読同期込み）が無いため、window.wishSave/window.savedDecksSet が存在すれば
 *     それ経由で適用し（§7.4 と同じ経路。将来それらを持つページから呼ばれても正しく動く）、
 *     存在しなければ同じ localStorage キーへの書き込みだけをこの関数が代わりに行う
 *     （_writeLocalList。呼び出し側の /sync ページ自身は localStorage に一切触れない）
 *
 * index.html / 一人回し / sync.html 3者から <script> で読み込む想定のため非module・IIFEで
 * 実装し、window.SyncClient にAPIを公開する（static/wish-split.js と同じ形式）。
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

  // wishSave()/savedDecksSet() が存在しないページ（/sync）向けのフォールバック書き込み。
  // wishSave/savedDecksSet が書き込む先と同じキーに同じ形式で書くだけで、バッジ更新・
  // push購読同期は行わない（/syncページにはそのUI自体が無いため）。applyRedeemResult() 専用
  function _writeLocalList(kind, list) {
    try { localStorage.setItem(_LOCAL_KEYS[kind], JSON.stringify(list || [])); } catch (e) {}
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
  // 【2026-08-17 二重init不具合の修正】onWishSave/onDecksSave のデバウンスが2秒以内に
  // ほぼ同時発火すると、_doPush('wishlist') と _doPush('decks') の両方が「sync_id無し」と
  // 判断し、_ensureAccount() を独立に2回呼んでいた。/api/sync/init が2回叩かれ、
  // 内容は同じ（initは常に両方のリストを送るため両行とも中身は完全）だが孤児の行が
  // 1つ余分にできる（本番で0.2秒差の重複行として再現）。
  // 対策: 進行中のPromiseをモジュール内で共有し、後続の呼び出しは新しいPOSTを投げず
  // 同じ結果を待つ（冪等化）。成功・失敗を問わず完了時に解除し、次回は通常どおり発行できる。
  let _ensureAccountInFlight = null;

  function _ensureAccount() {
    if (_ensureAccountInFlight) return _ensureAccountInFlight;

    const wishlist = _readLocalList('wishlist');
    const decks = _readLocalList('decks');
    if (!wishlist.length && !decks.length) return Promise.resolve(null);

    _ensureAccountInFlight = _postJson('/api/sync/init', { wishlist: wishlist, decks: decks })
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
          // 発行直後は連携済みトークンが存在し得ない（新規行のため）。§7.3の表示は
          // is_linked（サーバー側の使用済みトークン判定）でのみ true にする
          linked: false,
        };
        _setState(state);
        return state;
      })
      .catch(function () { return null; })
      .finally(function () {
        _ensureAccountInFlight = null;
      });
    return _ensureAccountInFlight;
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
      // 通常（自分が先に呼んだ側=リーダー）は追送不要（_ensureAccount がローカルから
      // 両方を読んで送るため、自分のkindも既にpayloadに含まれている）。
      // 【2026-08-17】既に他の呼び出しが _ensureAccount を実行中だった場合（フォロワー）は、
      // その完了を待ってから自分のkind・itemsを明示的に push する。リーダーのinit呼び出しが
      // 読んだローカルデータに自分の分が含まれているはずだが、明示的に送ることで
      // 「デッキの内容がpushされないまま終わる」といった取りこぼしを起こさない
      const wasAlreadyInFlight = !!_ensureAccountInFlight;
      _ensureAccount().then(function (newState) {
        if (!wasAlreadyInFlight) return; // リーダー: 追送不要
        if (!newState || !newState.sync_id) return; // 発行失敗。次回のpushで再度試みられる
        _pushWithRetry(kind, list);
      });
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

      // 「他端末と連携済みか」（§7.3）はpullのたびにサーバーの判定結果で更新する。
      // sync_idの有無では判定しない（購入候補を1件持っただけの未連携端末にも
      // sync_idは自動発行されるため。2026-08-18 誤表示バグの修正）
      if (typeof data.linked === 'boolean' && next.linked !== data.linked) {
        next.linked = data.linked;
        changed = true;
      }

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

  // ── P3: ワンタイムリンク（QR＋リンク。設計文書 §6.4〜§6.7・§7.1〜§7.2） ──

  // この端末が同期中かどうか・現在の状態を返す（読み取り専用）。index.html が
  // 「他の端末と同期中」表示の出し分けに使う。localStorageキーを直接読ませないための窓口
  function getSyncState() {
    return _getState();
  }

  // 確認画面（/sync）向け: この端末のローカル件数（適用前に表示する）
  function getLocalCounts() {
    return { wishlist: _readLocalList('wishlist').length, decks: _readLocalList('decks').length };
  }

  function _createLinkFor(syncId) {
    return _postJson('/api/sync/link/create', { sync_id: syncId })
      .then(function (res) {
        if (res.status === 429) return { ok: false, reason: 'rate_limited' };
        return res.json().catch(function () { return { ok: false, reason: 'invalid_response' }; });
      })
      .catch(function () { return { ok: false, reason: 'network_error' }; });
  }

  // 発行側（index.html）「他の端末でも見る」ボタンから呼ぶ（設計文書 §7.1）。
  // sync_id 未発行の端末では、ローカルにデータが1件以上あるときだけ先に init する（§8）。
  // どちらも無ければ reason:"no_local_data" を返し、呼び出し側で案内文を出す
  function startLinkShare() {
    const state = _getState();
    if (state && state.sync_id) {
      return _createLinkFor(state.sync_id);
    }
    const wishlist = _readLocalList('wishlist');
    const decks = _readLocalList('decks');
    if (!wishlist.length && !decks.length) {
      return Promise.resolve({ ok: false, reason: 'no_local_data' });
    }
    return _ensureAccount().then(function (newState) {
      if (!newState || !newState.sync_id) return { ok: false, reason: 'init_failed' };
      return _createLinkFor(newState.sync_id);
    });
  }

  // 「同期を解除」から呼ぶ（設計文書 §7.3・§6.7）。この端末だけ切り離し、
  // 返ってきた新しい sync_id・リビジョンで状態を張り替える（元の行は他端末のため残る）
  function unlinkThisDevice() {
    const state = _getState();
    if (!state || !state.sync_id) return Promise.resolve({ ok: false, reason: 'not_linked' });
    return _postJson('/api/sync/unlink', { sync_id: state.sync_id })
      .then(function (res) {
        if (res.status === 429) return { ok: false, reason: 'rate_limited' };
        return res.json().catch(function () { return { ok: false, reason: 'invalid_response' }; });
      })
      .then(function (data) {
        if (data && data.ok) {
          _setState({
            sync_id: data.sync_id,
            wishlist_rev: data.wishlist_rev || 1,
            decks_rev: data.decks_rev || 1,
            // unlinkで移った新しいsync_idには使用済みトークンが存在しない（§7.3）。
            // 「同期を解除」直後に表示が消えるようここで明示的にfalseへ戻す
            linked: false,
          });
        }
        return data;
      })
      .catch(function () { return { ok: false, reason: 'network_error' }; });
  }

  // 引き換え側（/sync ページ）: 確認画面の下見。副作用なし（設計文書 §6.5）
  function previewLink(token) {
    return _postJson('/api/sync/link/preview', { token: token })
      .then(function (res) { return res.json().catch(function () { return { ok: false, reason: 'invalid_response' }; }); })
      .catch(function () { return { ok: false, reason: 'network_error' }; });
  }

  // 受け取った内容と rev を保存して合流を完了する（設計文書 §7.2 D-3）。
  // /sync ページには index.html の完全な wishSave()/savedDecksSet()（バッジ更新・push購読
  // 同期込み）が無いため、window にそれらが存在すればそちら経由で適用し（§7.4 と同じ経路）、
  // 存在しなければ同じ localStorage キーへの書き込みだけをこの関数が代わりに行う
  // （_writeLocalList）。呼び出し元（/sync ページ）は localStorage に一切触れない。
  // result: { sync_id, wishlist: {rev, items}, decks: {rev, items} }（redeemLink の成功応答）
  function applyRedeemResult(result) {
    if (!result || !result.sync_id || !result.wishlist || !result.decks) return false;
    _applying = true;
    try {
      if (typeof global.wishSave === 'function') global.wishSave(result.wishlist.items || []);
      else _writeLocalList('wishlist', result.wishlist.items || []);
      if (typeof global.savedDecksSet === 'function') global.savedDecksSet(result.decks.items || []);
      else _writeLocalList('decks', result.decks.items || []);
    } finally {
      _applying = false;
    }
    if (typeof global.renderWishlist === 'function') global.renderWishlist();
    if (typeof global.renderSavedDecks === 'function') global.renderSavedDecks();
    // 【2026-08-13データ消失バグと同じ構造の再発防止】新しいsync_idと「両方」のrevを必ず
    // 一緒に保存する。片方だけ進めるとrev=0のまま残った種別が次回pullで消失しうる
    // linked: 引き換えが成立した瞬間＝連携成立の瞬間なので true（§7.3）。
    // 発行側の端末は次回pullで気づく
    _setState({
      sync_id: result.sync_id,
      wishlist_rev: result.wishlist.rev,
      decks_rev: result.decks.rev,
      linked: true,
    });
    return true;
  }

  // 引き換え側（/sync ページ）: 確認画面のボタンを押したときに呼ぶ（設計文書 §6.6）。
  // この端末が既に同期中だった場合は old_sync_id として現在の sync_id を添える
  // （サーバー側はこれを一切使わない。旧行を削除しない保証はサーバー側で担保する）
  function redeemLink(token) {
    const state = _getState();
    const oldSyncId = (state && state.sync_id) ? state.sync_id : null;
    const wishlist = _readLocalList('wishlist');
    const decks = _readLocalList('decks');
    return _postJson('/api/sync/link/redeem', {
      token: token, wishlist: wishlist, decks: decks, old_sync_id: oldSyncId,
    }).then(function (res) {
      if (res.status === 429) return { ok: false, reason: 'rate_limited' };
      return res.json().catch(function () { return { ok: false, reason: 'invalid_response' }; });
    }).then(function (data) {
      if (data && data.ok) applyRedeemResult(data);
      return data;
    }).catch(function () { return { ok: false, reason: 'network_error' }; });
  }

  global.SyncClient = {
    onWishSave: onWishSave, onDecksSave: onDecksSave, pullIfNeeded: pullIfNeeded,
    getSyncState: getSyncState, getLocalCounts: getLocalCounts,
    startLinkShare: startLinkShare, unlinkThisDevice: unlinkThisDevice,
    previewLink: previewLink, redeemLink: redeemLink, applyRedeemResult: applyRedeemResult,
  };
})(window);
