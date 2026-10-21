/*
 * マイデッキの一覧を「マイデッキ ▾」ボタンから開く（2026-10-21 ユーザーがモックを見て承認。モック _mocks/mydeck-refresh-20261020 の D）。
 *
 *   以前は保存済みデッキの一覧がページの一番上に常に出ていて、スマホでは3件で最初の画面が埋まりデッキ本体が見えなかった。
 *   シミュレーターのサイドバー（「マイデッキ」を押すと窓）と同じく、見出しの左のボタンで開く。
 *   - PC: ボタンの下に窓。スマホ（≤900px・タブ表示のとき）: 下から出るシート。
 *   - 行: 先頭カードの画像・名前・「使用中」・メイン/EX の枚数・更新日・削除（deck-head.js の取り消し付き削除）。
 *   - 下に「＋ 新しく作る」「端末同期」。
 *
 * 一覧を描く renderSavedDecks（index-wish.js）は、同期・取り込み・保存などあちこちから呼ばれるので置き換え、
 * ボタンの件数と（開いていれば）窓の中身を描き直す。見た目は deck.css 末尾。
 */
(function(){
  'use strict';

  function $(id){ return document.getElementById(id); }
  function _decks(){ return (typeof savedDecksGet === 'function') ? savedDecksGet() : []; }
  function _norm(d){ return (typeof normalizeDeck === 'function') ? normalizeDeck(d) : { main: d.main || [], ex: d.ex || [] }; }
  function _sum(a){ return (a || []).reduce(function(s, c){ return s + (c.qty || 0); }, 0); }
  function _isSheet(){ return window.matchMedia ? window.matchMedia('(max-width: 900px)').matches : false; }
  function _date(t){ if(!t) return ''; var d = new Date(t); if(isNaN(d.getTime())) return ''; return (d.getMonth() + 1) + '/' + d.getDate() + ' 更新'; }

  // ── 先頭カードの画像（/api/card-images。同じ名前は一度だけ取りに行く） ──
  var _thumbs = Object.create(null); // 名前 → URL（'' は取れなかった）
  function _loadThumbs(names, done){
    var need = names.filter(function(n){ return n && !(n in _thumbs); });
    if(!need.length){ done(); return; }
    need.forEach(function(n){ _thumbs[n] = ''; });
    fetch('/api/card-images', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ names: need }) })
      .then(function(r){ return r.json(); })
      .then(function(data){
        var images = (data && data.images) || {};
        need.forEach(function(n){
          var url = (typeof _batchImgUrl === 'function') ? _batchImgUrl(images[n]) : '';
          if(url && typeof safeUrl === 'function') url = safeUrl(url) || '';
          _thumbs[n] = url || '';
        });
        done();
      })
      // 通信に失敗した名前は覚えない（次に開いたときに取り直す）
      .catch(function(){ need.forEach(function(n){ delete _thumbs[n]; }); done(); });
  }

  // ── ボタンの件数 ──
  function _updateCount(){
    var c = $('deckLibCount');
    if(c) c.textContent = String(_decks().length);
  }

  // ── 窓（シート） ──
  var _pop = null; // { el, body, dim, cleanup }
  function isOpen(){ return !!_pop; }

  function _row(d, curId){
    var row = document.createElement('div');
    row.className = 'deck-lib-row' + (d.id === curId ? ' is-cur' : '');
    var open = document.createElement('button');
    open.type = 'button';
    open.className = 'deck-lib-open';
    var th = document.createElement('span');
    th.className = 'deck-lib-thumb';
    var nd = _norm(d);
    var first = (nd.main[0] || nd.ex[0] || {}).name || '';
    th.dataset.firstCard = first;
    if(first && _thumbs[first]){
      var img = document.createElement('img');
      img.src = _thumbs[first];
      img.alt = '';
      img.loading = 'lazy';
      th.appendChild(img);
    }
    var meta = document.createElement('span');
    meta.className = 'deck-lib-meta';
    var nm = document.createElement('span');
    nm.className = 'deck-lib-name';
    var nmText = document.createElement('span');
    nmText.className = 'deck-lib-name-text';
    nmText.textContent = d.name || 'マイデッキ';
    nm.appendChild(nmText);
    if(d.id === curId){
      var tag = document.createElement('span');
      tag.className = 'deck-lib-tag';
      tag.textContent = '使用中';
      nm.appendChild(tag);
    }
    var sub = document.createElement('span');
    sub.className = 'deck-lib-sub';
    var when = _date(d.updated);
    sub.textContent = 'メイン ' + _sum(nd.main) + '・EX ' + _sum(nd.ex) + (when ? '・' + when : '');
    meta.append(nm, sub);
    open.append(th, meta);
    open.addEventListener('click', function(){ _openDeck(d.id); });
    var del = document.createElement('button');
    del.type = 'button';
    del.className = 'deck-lib-del';
    del.textContent = '削除';
    del.setAttribute('aria-label', '「' + (d.name || 'マイデッキ') + '」を削除');
    del.addEventListener('click', function(){
      if(window.DeckHead && typeof window.DeckHead.deleteDeck === 'function') window.DeckHead.deleteDeck(d.id);
      // 削除で一覧が描き直されるとボタンが消えるので、窓の中にフォーカスを残す
      var x = _pop && _pop.el.querySelector('.deck-lib-x');
      if(x) x.focus({ preventScroll: true });
    });
    row.append(open, del);
    return row;
  }

  function _renderRows(){
    if(!_pop) return;
    var body = _pop.body;
    // 同期などで描き直してもフォーカスを窓の中に残す（消えたボタンにあったら閉じるボタンへ）
    var hadFocus = _pop.el.contains(document.activeElement) && body.contains(document.activeElement);
    body.textContent = '';
    var list = _decks();
    var curId = window._currentSavedDeckId || null;
    if(!list.length){
      var empty = document.createElement('p');
      empty.className = 'deck-lib-empty';
      empty.textContent = '保存済みのデッキはありません。デッキを作って「マイデッキに登録」を押すと、ここに並びます。';
      body.appendChild(empty);
      return;
    }
    list.forEach(function(d){ body.appendChild(_row(d, curId)); });
    if(hadFocus){ var x0 = _pop.el.querySelector('.deck-lib-x'); if(x0) x0.focus({ preventScroll: true }); }
    var names = list.map(function(d){ var nd = _norm(d); return (nd.main[0] || nd.ex[0] || {}).name || ''; });
    _loadThumbs(names, function(){
      if(!_pop) return;
      _pop.body.querySelectorAll('.deck-lib-thumb').forEach(function(th){
        var url = _thumbs[th.dataset.firstCard];
        if(url && !th.firstChild){
          var img = document.createElement('img');
          img.src = url; img.alt = ''; img.loading = 'lazy';
          th.appendChild(img);
        }
      });
    });
  }

  // デッキを開く。保存していない編集中のデッキを捨てるときは確認する（deck-edit.js の「デッキを開く」と同じ）
  function _openDeck(id){
    if(id !== window._currentSavedDeckId){
      var ta = $('deckTextarea');
      var unsaved = ta && ta.value.trim() && (window._currentSavedDeckId == null);
      if(unsaved && !confirm('マイデッキに登録していない編集中のデッキがあります。破棄してこのデッキを開きますか？')) return;
      if(typeof loadSavedDeck === 'function') loadSavedDeck(id);
    }
    close(true);
    if(_isSheet() && typeof switchDeckPane === 'function') switchDeckPane('main');
  }

  function open(){
    if(_pop) return;
    var btn = $('deckLibBtn');
    if(!btn) return;
    var sheet = _isSheet();
    var el = document.createElement('div');
    el.className = 'deck-head-pop deck-lib-pop' + (sheet ? ' is-sheet' : '');
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-label', 'マイデッキ');
    var head = document.createElement('div');
    head.className = 'deck-lib-head';
    var title = document.createElement('span');
    title.textContent = 'マイデッキ';
    var x = document.createElement('button');
    x.type = 'button';
    x.className = 'deck-lib-x';
    x.setAttribute('aria-label', '閉じる');
    x.textContent = '×';
    x.addEventListener('click', function(){ close(true); });
    head.append(title, x);
    var body = document.createElement('div');
    body.className = 'deck-lib-body';
    var foot = document.createElement('div');
    foot.className = 'deck-lib-foot';
    var mk = function(label, fn){
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'deck-pill';
      b.textContent = label;
      b.addEventListener('click', fn);
      foot.appendChild(b);
    };
    mk('＋ 新しく作る', function(){ close(false); if(typeof deckCreateNew === 'function') deckCreateNew(); });
    mk('端末同期', function(){ close(false); if(typeof openSyncShareDialog === 'function') openSyncShareDialog(); });
    if(sheet){ var grab = document.createElement('div'); grab.className = 'deck-lib-grab'; el.appendChild(grab); }
    el.append(head, body, foot);
    var dim = null;
    if(sheet){
      dim = document.createElement('div');
      dim.className = 'deck-lib-dim';
      dim.addEventListener('click', function(){ close(true); });
      document.body.appendChild(dim);
    }
    document.body.appendChild(el);
    _pop = { el: el, body: body, dim: dim, cleanup: null };
    _renderRows();
    if(!sheet){
      var r = btn.getBoundingClientRect();
      var m = 8, vw = document.documentElement.clientWidth;
      el.style.left = Math.min(Math.max(m, r.left), Math.max(m, vw - el.offsetWidth - m)) + 'px';
      el.style.top = (r.bottom + 6) + 'px';
      el.style.maxHeight = Math.max(200, window.innerHeight - r.bottom - 6 - 16) + 'px';
    }
    btn.classList.add('is-active');
    btn.setAttribute('aria-expanded', 'true');

    function onDown(e){
      if(el.contains(e.target) || btn.contains(e.target)) return;
      if(e.target.closest && e.target.closest('.deck-head-toast, .deck-head-pop')) return; // 取り消しのお知らせ・名前の小窓
      close(false);
    }
    function onKey(e){
      if(e.key !== 'Escape') return;
      // 上に重なっている窓（名前の小窓・メニュー）があれば、そちらの Esc に任せる
      if(document.querySelector('.deck-name-pop, .deck-more-menu, .deck-card-menu, .npm-overlay')) return;
      e.preventDefault(); e.stopImmediatePropagation(); close(true);
    }
    // Tab で窓の外へ出たら閉じる（⋯ メニューと同じ）
    el.addEventListener('focusout', function(e){
      var to = e.relatedTarget;
      if(!to || el.contains(to) || btn.contains(to) || (to.closest && to.closest('.deck-head-pop, .deck-head-toast'))) return;
      close(false);
    });
    function onScroll(e){
      if(sheet || el.contains(e.target)) return;
      var r2 = btn.getBoundingClientRect();
      el.style.top = (r2.bottom + 6) + 'px';
    }
    document.addEventListener('pointerdown', onDown, true);
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('scroll', onScroll, true);
    _pop.cleanup = function(){
      document.removeEventListener('pointerdown', onDown, true);
      document.removeEventListener('keydown', onKey, true);
      window.removeEventListener('scroll', onScroll, true);
    };
    var first = body.querySelector('.deck-lib-row.is-cur .deck-lib-open') || body.querySelector('.deck-lib-open') || x;
    first.focus({ preventScroll: true });
  }

  function close(restoreFocus){
    if(!_pop) return;
    var p = _pop;
    _pop = null;
    p.cleanup && p.cleanup();
    if(p.el.parentNode) p.el.parentNode.removeChild(p.el);
    if(p.dim && p.dim.parentNode) p.dim.parentNode.removeChild(p.dim);
    var btn = $('deckLibBtn');
    if(btn){
      btn.classList.remove('is-active');
      btn.setAttribute('aria-expanded', 'false');
      if(restoreFocus) btn.focus({ preventScroll: true });
    }
  }

  // 同期・取り込み・保存などで一覧が変わったときに呼ばれる（index-wish.js の renderSavedDecks を置き換える）
  function render(){
    _updateCount();
    _renderRows();
  }

  function _init(){
    // 別の画面へ切り替えたら（戻る操作など）閉じる（窓・暗幕は body 直下なので、残ると別の画面の上に出る）
    var origSwitch = window.switchMode;
    if(typeof origSwitch === 'function'){
      window.switchMode = function(mode){
        if(mode !== 'mydeck') close(false);
        return origSwitch.apply(this, arguments);
      };
    }
    var btn = $('deckLibBtn');
    if(btn) btn.addEventListener('click', function(){ if(_pop) close(true); else open(); });
    // 画面の幅で窓とシートが入れ替わるので、幅をまたいだら閉じる
    if(window.matchMedia){
      var mq = window.matchMedia('(max-width: 900px)');
      var onChange = function(){ close(false); };
      if(mq.addEventListener) mq.addEventListener('change', onChange); else if(mq.addListener) mq.addListener(onChange);
    }
    _updateCount();
  }

  window.renderSavedDecks = render;
  window.DeckLibrary = { open: open, close: close, isOpen: isOpen, render: render,
    _openDeck: _openDeck }; // _ で始まるものはテスト用（tests/js/deck_library_check.js）

  if(document.readyState === 'complete') _init();
  else document.addEventListener('DOMContentLoaded', _init);
})();
