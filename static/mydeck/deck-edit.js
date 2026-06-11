/*
 * deck-edit.js — マイデッキのデッキ編集UI
 *
 * 役割:
 *   - カード検索 → デッキへ追加（未発売カード対応）
 *   - 各カードの +/− 枚数変更・× 削除
 *   - PC（マウス）でのドラッグ&ドロップ並べ替え（同一セクション内）
 *
 * 設計:
 *   既存設計（#deckTextarea を唯一の正とし、parseDeckSections → _currentMydeckCards
 *   → renderDeckGrid の単方向）を壊さない。編集操作はすべて _deckMutate を通して
 *   「_currentMydeckCards を編集 → textarea を再構築 → 再描画」の単方向ループに統一する。
 *
 *   index.html のインライン <script> で定義済みのグローバル（DECK_CTX /
 *   _currentMydeckCards / _currentMydeckText / parseDeckSections / calcDeckEstimate /
 *   renderDeckGrid / esc / escJs）を実行時に参照する。本ファイルは index.html の
 *   インライン <script> の後に classic script として読み込まれる前提。
 */
(function(){
  'use strict';

  // タッチ端末ではドラッグを無効化し、ボタン操作のみにフォールバックする
  var _isTouch = (window.matchMedia && window.matchMedia('(pointer:coarse)').matches) ||
                 (navigator.maxTouchPoints > 0);

  // 編集モード状態は window 経由で公開（インライン側 calcDeckEstimate / renderDeckGrid から参照）
  window._deckEditMode = false;

  var _deckEstimateTimer = null;
  var _deckMainCount = 0; // 直近描画時のメインデッキ枚数（D&Dのセクション判定用）

  // ── デッキ直列化 ──────────────────────────────
  // main/ex 構造を textarea テキストへ。ex があれば [EX] 区切りを付与（parseDeckSections と往復可能）
  function _serializeDeck(deck){
    var line = function(c){ return c.qty > 1 ? (c.qty + ' ' + c.name) : c.name; };
    var mainLines = (deck.main || []).map(line);
    var exLines = (deck.ex || []).map(line);
    var out = mainLines.join('\n');
    if(exLines.length) out += (out ? '\n' : '') + '[EX]\n' + exLines.join('\n');
    return out;
  }

  // ── 編集の唯一の入口 ──────────────────────────
  // mutator で _currentMydeckCards(main/ex) を編集し、textarea を再構築して再描画する。
  // 構造・枚数バッジは即時再描画し、価格再取得（calcDeckEstimate）のみデバウンスする。
  function _deckMutate(mutator){
    var ta = document.getElementById('deckTextarea');
    if(!ta) return;
    // textarea が手編集されていれば先に取り込む（手入力との整合）
    if(ta.value !== _currentMydeckText){
      _currentMydeckCards = parseDeckSections(ta.value);
    }
    mutator(_currentMydeckCards);
    var text = _serializeDeck(_currentMydeckCards);
    ta.value = text;
    _currentMydeckText = text;

    _persistDeck(); // 下書き保存 + 保存済みデッキの自動更新
    _deckRenderImmediate();

    clearTimeout(_deckEstimateTimer);
    _deckEstimateTimer = setTimeout(function(){ calcDeckEstimate(); }, 350);
  }

  // 即時の構造再描画（画像・価格は後続の calcDeckEstimate が読み込む）
  function _deckRenderImmediate(){
    var ctx = DECK_CTX.mydeck;
    var main = _currentMydeckCards.main || [];
    var ex = _currentMydeckCards.ex || [];
    var resultsEl = document.getElementById(ctx.results);
    if(!main.length && !ex.length){
      if(resultsEl) resultsEl.classList.add('hidden');
      return;
    }
    if(resultsEl) resultsEl.classList.remove('hidden');
    renderDeckGrid([].concat(main, ex), ctx, main.length);
  }

  // ── 配列操作ヘルパー ──────────────────────────
  function _findCard(arr, name){
    for(var i = 0; i < arr.length; i++){ if(arr[i].name === name) return i; }
    return -1;
  }
  function _addToSection(deck, sec, name){
    if(!deck[sec]) deck[sec] = [];
    var arr = deck[sec];
    var i = _findCard(arr, name);
    if(i >= 0) arr[i].qty++;
    else arr.push({ qty: 1, name: name });
  }
  // main にあるカードを ex へ移す（全枚数を集約）
  function _moveCardToEx(deck, name){
    if(!deck.main) return;
    var mi = _findCard(deck.main, name);
    if(mi < 0) return;
    var qty = deck.main[mi].qty;
    deck.main.splice(mi, 1);
    if(!deck.ex) deck.ex = [];
    var ei = _findCard(deck.ex, name);
    if(ei >= 0) deck.ex[ei].qty += qty;
    else deck.ex.push({ qty: qty, name: name });
  }

  // ── 公開操作 ──────────────────────────────────
  // カードをデッキに追加。追加先トグル（自動/メイン/EX）に従う。
  // 「自動」のときは楽観的に main へ入れて即描画し、card-info の is_ex 判定後に EX へ補正する。
  function deckAddCard(name){
    if(!name) return;
    var destSel = document.getElementById('deckAddDest');
    var dest = destSel ? destSel.value : 'auto';
    var sec = (dest === 'ex') ? 'ex' : 'main';
    _deckMutate(function(d){ _addToSection(d, sec, name); });

    if(dest === 'auto'){
      fetch('/api/card-info?name=' + encodeURIComponent(name))
        .then(function(r){ return r.ok ? r.json() : null; })
        .then(function(info){
          if(info && info.is_ex){
            _deckMutate(function(d){ _moveCardToEx(d, name); });
          }
        })
        .catch(function(){});
    }
  }

  function deckInc(name, sec){
    _deckMutate(function(d){
      var a = d[sec] || []; var i = _findCard(a, name);
      if(i >= 0) a[i].qty++;
    });
  }
  function deckDec(name, sec){
    _deckMutate(function(d){
      var a = d[sec] || []; var i = _findCard(a, name);
      if(i >= 0){ a[i].qty--; if(a[i].qty <= 0) a.splice(i, 1); }
    });
  }
  function deckRemove(name, sec){
    _deckMutate(function(d){
      var a = d[sec] || []; var i = _findCard(a, name);
      if(i >= 0) a.splice(i, 1);
    });
  }
  // 同一セクション内でカードを並べ替える
  function deckReorder(sec, from, to){
    if(from === to) return;
    _deckMutate(function(d){
      var a = d[sec] || [];
      if(from < 0 || from >= a.length || to < 0 || to >= a.length) return;
      var moved = a.splice(from, 1)[0];
      a.splice(to, 0, moved);
    });
  }

  // ── 編集モードのトグル ────────────────────────
  function toggleDeckEdit(){
    window._deckEditMode = !window._deckEditMode;
    var btn = document.getElementById('deckEditToggle');
    if(btn){
      btn.classList.toggle('active', window._deckEditMode);
      btn.textContent = window._deckEditMode ? '編集を終了' : 'デッキを編集';
    }
    var box = document.getElementById('deckSearchBox');
    if(box) box.classList.toggle('hidden', !window._deckEditMode);
    // 並べ替えの有無（編集中はソートしない）が変わるため再計算・再描画する
    var ta = document.getElementById('deckTextarea');
    if(ta && ta.value.trim()) calcDeckEstimate();
    if(window._deckEditMode){
      var si = document.getElementById('deckSearchInput');
      if(si) si.focus();
    }
  }

  // ── renderDeckGrid 後フック（編集UIの注入/除去）────
  // インライン renderDeckGrid の末尾から呼ばれる。マイデッキ かつ 編集モードのときだけ
  // 各セルに操作バーと（非タッチ時）draggable を付与する。
  function _deckAfterRender(listEl, cards, mainCount, ctx){
    if(!listEl) return;
    var editing = (ctx === DECK_CTX.mydeck) && window._deckEditMode;
    listEl.classList.toggle('edit-mode', editing);
    _deckMainCount = mainCount;

    var cells = listEl.querySelectorAll('.deck-grid-cell');
    if(!editing){
      // 編集UIを除去
      cells.forEach(function(cell){
        cell.removeAttribute('draggable');
        var bar = cell.querySelector('.deck-edit-bar');
        if(bar) bar.remove();
        var wrap = cell.querySelector('.deck-grid-img-wrap');
        if(wrap) wrap.removeAttribute('draggable');
      });
      return;
    }

    cells.forEach(function(cell, i){
      var card = cards[i];
      if(!card) return;
      var sec = (i < mainCount) ? 'main' : 'ex';
      cell.dataset.sec = sec;
      cell.dataset.fi = String(i);

      // ドラッグ（PCのみ）。内側リンクが代わりにドラッグされるのを防ぐ
      if(!_isTouch){
        cell.setAttribute('draggable', 'true');
        var wrap = cell.querySelector('.deck-grid-img-wrap');
        if(wrap) wrap.setAttribute('draggable', 'false');
      }

      // 操作バー（既存があれば作り直し）
      var old = cell.querySelector('.deck-edit-bar');
      if(old) old.remove();
      var bar = document.createElement('div');
      bar.className = 'deck-edit-bar';
      bar.innerHTML =
        '<button type="button" class="deck-edit-btn deck-edit-dec" data-act="dec" title="1枚減らす">−</button>' +
        '<span class="deck-edit-q">' + card.qty + '</span>' +
        '<button type="button" class="deck-edit-btn deck-edit-inc" data-act="inc" title="1枚増やす">+</button>' +
        '<button type="button" class="deck-edit-btn deck-edit-del" data-act="del" title="削除">×</button>';
      cell.appendChild(bar);
    });
  }

  // ── イベント委譲（操作バー・ドラッグ）────────────
  function _cellSecIdx(cell){
    var fi = parseInt(cell.dataset.fi, 10);
    var sec = cell.dataset.sec || (fi < _deckMainCount ? 'main' : 'ex');
    var idx = (sec === 'main') ? fi : fi - _deckMainCount;
    return { sec: sec, idx: idx };
  }

  function _initDelegation(listEl){
    // 操作バーのクリック
    listEl.addEventListener('click', function(e){
      var btn = e.target.closest('.deck-edit-btn');
      if(!btn) return;
      e.preventDefault();
      e.stopPropagation();
      var cell = btn.closest('.deck-grid-cell');
      if(!cell) return;
      var name = cell.dataset.card;
      var sec = cell.dataset.sec || 'main';
      var act = btn.dataset.act;
      if(act === 'inc') deckInc(name, sec);
      else if(act === 'dec') deckDec(name, sec);
      else if(act === 'del') deckRemove(name, sec);
    });

    if(_isTouch) return; // ドラッグはPCのみ

    var dragFrom = null; // {sec, idx}
    listEl.addEventListener('dragstart', function(e){
      var cell = e.target.closest('.deck-grid-cell');
      if(!cell || !window._deckEditMode || !cell.getAttribute('draggable')) return;
      dragFrom = _cellSecIdx(cell);
      cell.classList.add('dragging');
      try{ e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', ''); }catch(_){}
    });
    listEl.addEventListener('dragover', function(e){
      var cell = e.target.closest('.deck-grid-cell');
      if(!cell || !dragFrom) return;
      var t = _cellSecIdx(cell);
      if(t.sec !== dragFrom.sec) return; // 同一セクション内のみ
      e.preventDefault();
      try{ e.dataTransfer.dropEffect = 'move'; }catch(_){}
      cell.classList.add('drag-over');
    });
    listEl.addEventListener('dragleave', function(e){
      var cell = e.target.closest('.deck-grid-cell');
      if(cell) cell.classList.remove('drag-over');
    });
    listEl.addEventListener('drop', function(e){
      var cell = e.target.closest('.deck-grid-cell');
      if(!cell || !dragFrom) return;
      e.preventDefault();
      cell.classList.remove('drag-over');
      var to = _cellSecIdx(cell);
      if(to.sec === dragFrom.sec) deckReorder(dragFrom.sec, dragFrom.idx, to.idx);
      dragFrom = null;
    });
    listEl.addEventListener('dragend', function(){
      listEl.querySelectorAll('.dragging,.drag-over').forEach(function(el){
        el.classList.remove('dragging'); el.classList.remove('drag-over');
      });
      dragFrom = null;
    });
  }

  // ── カード検索サジェスト（未発売カード対応・デッキ追加用）──
  function _setupDeckSuggest(){
    var input = document.getElementById('deckSearchInput');
    var drop = document.getElementById('deckSuggestDrop');
    if(!input || !drop) return;
    var timer = null, idx = -1, items = [];

    function close(){ drop.classList.remove('open'); drop.innerHTML = ''; idx = -1; items = []; }
    function highlight(){
      var els = drop.querySelectorAll('.deck-suggest-item');
      els.forEach(function(el, i){ el.classList.toggle('active', i === idx); });
      if(idx >= 0 && els[idx]) els[idx].scrollIntoView({ block: 'nearest' });
    }
    function select(i){
      var it = items[i];
      if(!it) return;
      deckAddCard(it.name);
      input.value = '';
      close();
      input.focus(); // 連続追加できるようフォーカス維持
    }
    function render(){
      drop.innerHTML = items.map(function(it, i){
        var badge = it.unreleased ? '<span class="deck-suggest-badge">未発売</span>' : '';
        return '<div class="deck-suggest-item" data-i="' + i + '">' +
               '<span class="deck-suggest-name">' + esc(it.name) + '</span>' + badge + '</div>';
      }).join('');
      // mousedown で選択（blur による先行クローズを防ぐ）
      drop.querySelectorAll('.deck-suggest-item').forEach(function(el){
        el.addEventListener('mousedown', function(ev){ ev.preventDefault(); select(parseInt(el.dataset.i, 10)); });
      });
      drop.classList.add('open');
    }
    function fetchSuggest(q){
      fetch('/api/suggest?include_unreleased=1&q=' + encodeURIComponent(q))
        .then(function(r){ return r.json(); })
        .then(function(arr){
          if(!Array.isArray(arr) || !arr.length){ close(); return; }
          // 後方互換: 文字列配列でも受け付ける
          items = arr.map(function(it){
            return (typeof it === 'string') ? { name: it, unreleased: false } : it;
          });
          idx = -1;
          render();
        })
        .catch(function(){ close(); });
    }

    input.addEventListener('input', function(){
      clearTimeout(timer);
      var v = input.value.trim();
      if(v.length < 2){ close(); return; }
      timer = setTimeout(function(){ fetchSuggest(v); }, 250);
    });
    input.addEventListener('keydown', function(e){
      if(drop.classList.contains('open') && items.length){
        if(e.key === 'ArrowDown'){ e.preventDefault(); idx = Math.min(idx + 1, items.length - 1); highlight(); return; }
        if(e.key === 'ArrowUp'){ e.preventDefault(); idx = Math.max(idx - 1, -1); highlight(); return; }
        if(e.key === 'Enter'){ e.preventDefault(); select(idx >= 0 ? idx : 0); return; }
        if(e.key === 'Escape'){ close(); return; }
      }
    });
    input.addEventListener('blur', function(){ setTimeout(close, 150); });
  }

  // ── 永続化（自動下書き + 保存済みデッキ自動更新）──
  // すべて localStorage（ブラウザ内）。サーバーDBは使わない。
  var DRAFT_KEY = 'cardprice_deck_draft';
  // 現在ひも付いている保存済みデッキのID（読込/保存時にセット）。編集はこのデッキへ自動反映する。
  window._currentSavedDeckId = window._currentSavedDeckId || null;

  function _clearDraft(){
    try{ localStorage.removeItem(DRAFT_KEY); }catch(_){}
  }

  // 現在の編集状態を下書き保存し、保存済みデッキにひも付いていればそれも更新する
  function _persistDeck(){
    var ta = document.getElementById('deckTextarea');
    if(!ta) return;
    if(ta.value !== _currentMydeckText){
      _currentMydeckCards = parseDeckSections(ta.value);
      _currentMydeckText = ta.value;
    }
    var main = _currentMydeckCards.main || [];
    var ex = _currentMydeckCards.ex || [];
    var text = ta.value;
    var id = window._currentSavedDeckId || null;

    // 下書き保存（次回開いたとき復元）
    try{
      localStorage.setItem(DRAFT_KEY, JSON.stringify({
        text: text, main: main, ex: ex, savedId: id,
        name: (typeof _currentDeckName !== 'undefined' ? _currentDeckName : '') || ''
      }));
    }catch(_){}

    // 保存済みデッキの自動更新（ひも付きがあるとき）
    if(id && typeof savedDecksGet === 'function'){
      try{
        var list = savedDecksGet();
        var d = list.find(function(x){ return x.id === id; });
        if(d){
          d.text = text; d.main = main; d.ex = ex; d.updated = Date.now();
          savedDecksSet(list);
        }else{
          window._currentSavedDeckId = null; // 削除済み → ひも付け解除
        }
      }catch(_){}
    }
  }

  // 起動時: 下書きがあれば復元する（textareaが空のときのみ。保存済み自動ロード等と競合しない）
  function _restoreDraft(){
    var ta = document.getElementById('deckTextarea');
    if(!ta || ta.value.trim()) return;
    var draft = null;
    try{ draft = JSON.parse(localStorage.getItem(DRAFT_KEY) || 'null'); }catch(_){}
    if(!draft || !draft.text || !draft.text.trim()) return;

    ta.value = draft.text;
    _currentMydeckText = draft.text;
    _currentMydeckCards = (draft.main) ? { main: draft.main, ex: draft.ex || [] }
                                       : parseDeckSections(draft.text);
    if(draft.savedId){
      window._currentSavedDeckId = draft.savedId;
      if(draft.name && typeof _currentDeckName !== 'undefined') _currentDeckName = draft.name;
      // 保存済みデッキ一覧で選択状態を反映（描画済みなら）
      try{
        var btn = document.querySelector('#sdcard-' + CSS.escape(draft.savedId) + ' .saved-deck-card-btn');
        if(btn) btn.classList.add('selected');
      }catch(_){}
    }
    // グリッドを復元表示（相場も取得）
    if(typeof calcDeckEstimate === 'function') calcDeckEstimate();
  }

  // inline 側のデッキ操作関数を拡張（元の挙動はそのまま、後処理でひも付け/下書きを更新）
  function _wrap(name, after){
    var orig = window[name];
    if(typeof orig !== 'function') return;
    window[name] = function(){
      var r = orig.apply(this, arguments);
      try{ after.apply(this, arguments); }catch(_){}
      return r;
    };
  }
  function _installWrappers(){
    // 保存済みデッキ読込 → 以後の編集はこのデッキへ自動反映。下書きも読込デッキに更新する
    _wrap('loadSavedDeck', function(id){ window._currentSavedDeckId = id; _persistDeck(); });
    // 入力クリア → ひも付け解除 + 下書き削除
    _wrap('clearDeck', function(){ window._currentSavedDeckId = null; _clearDraft(); });
    // PDF/拡張からの取込 → 新規の作業デッキ（保存済みとは切り離す）→ 下書き保存
    _wrap('onDeckImported', function(){ window._currentSavedDeckId = null; _persistDeck(); });
    // 環境デッキを送る → 同上
    _wrap('applyMetaDeckToTextarea', function(){ window._currentSavedDeckId = null; _persistDeck(); });
    // 手動「保存」 → 保存したデッキにひも付け（以後の編集を自動反映）
    _wrap('saveCurrentDeck', function(){
      try{
        if(typeof savedDecksGet !== 'function') return;
        var text = document.getElementById('deckTextarea').value.trim();
        var list = savedDecksGet().slice().reverse(); // 直近に保存したものを優先
        var d = list.find(function(x){ return (x.text || '').trim() === text; });
        if(d) window._currentSavedDeckId = d.id;
        _persistDeck();
      }catch(_){}
    });
  }

  // ── 初期化 ────────────────────────────────────
  function _init(){
    var listEl = document.getElementById('deckCardList');
    if(listEl) _initDelegation(listEl);
    _setupDeckSuggest();
    _installWrappers();

    // 手動でのtextarea編集も下書き保存（プログラム的な代入ではinputは発火しないので二重保存にならない）
    var ta = document.getElementById('deckTextarea');
    if(ta){
      var t = null;
      ta.addEventListener('input', function(){
        clearTimeout(t);
        t = setTimeout(function(){ _persistDeck(); }, 400);
      });
    }

    _restoreDraft();
  }

  // index.html のインライン <script> が参照するフック・関数を公開
  window._deckAfterRender = _deckAfterRender;
  window._deckMutate = _deckMutate;
  window.deckAddCard = deckAddCard;
  window.toggleDeckEdit = toggleDeckEdit;

  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', _init);
  }else{
    _init();
  }
})();
