/*
 * マイデッキのカード操作（2026-10-21 ユーザーがモックを見て承認。モック _mocks/mydeck-refresh-20261020 の C）。
 *
 *   以前は全カードの上に「− N + ×」が並び、スマホでは 63px のボタン列が 55px のカードからはみ出して × が切れていた。
 *   シミュレーターのデッキ編集（deck-edit-ui.js）と同じく、カードの上は「×N」の札だけにする。
 *   - PC: カードの右クリックメニューで「デッキに1枚足す（今 N枚）・デッキから1枚抜く・デッキから外す」
 *         ＋「購入候補に入れる・カードの詳細・価格を比べる」。押したときの詳細ドロワーとドラッグの並べ替えは今のまま。
 *   - タッチ端末: カードをタップすると選択（金の枠＝選択中）、上の欄に［−］N［＋］・詳細・✕。
 *         以前はタップで詳細が開いたので、詳細は欄のボタンへ移す。もう一度タップ・✕・カード以外を押すと選択をやめる。
 *
 * 枚数の増減は deck-edit.js の deckInc/deckDec/deckRemove（window._deckOps）を使う。見た目は deck.css 末尾。
 */
(function(){
  'use strict';

  function $(id){ return document.getElementById(id); }
  function ops(){ return window._deckOps || null; }
  function isTouch(){ var o = ops(); return !!(o && o.isTouch); }
  function _cards(sec){
    var c = (typeof _currentMydeckCards !== 'undefined' && _currentMydeckCards) ? _currentMydeckCards : { main: [], ex: [] };
    return c[sec] || [];
  }
  function _qty(name, sec){
    var it = _cards(sec).find(function(c){ return c.name === name; });
    return it ? it.qty : 0;
  }
  function _cellImg(cell){
    var img = cell && cell.querySelector('.deck-grid-img-wrap img');
    return img ? img.src : '';
  }
  // 押した操作が終わってから開く（同じクリックが deck-card-info.js の「ドロワーの外を押したら閉じる」に届き、開いた直後に閉じるため）
  function _openInfo(name, src){
    setTimeout(function(){
      if(typeof window.openDeckCardInfo === 'function') window.openDeckCardInfo(name, src || '');
    }, 0);
  }
  function _toast(msg){
    if(window.DeckHead && typeof window.DeckHead.toast === 'function') window.DeckHead.toast(msg);
  }
  function _ga(name, params){ try{ if(typeof window._ga === 'function') window._ga(name, params); }catch(_){} }

  // ── 購入候補に入れる（シミュレーターの addOneToWish と同じ。開いているデッキ用として1枚） ──
  function _wishCount(name){
    if(typeof wishGet !== 'function') return 0;
    return wishGet().filter(function(c){ return c.name === name; }).reduce(function(a, c){ return a + (c.qty || 0); }, 0);
  }
  function _addToWish(name){
    if(!window.WishDeck || typeof wishGet !== 'function' || typeof wishSave !== 'function') return;
    var next;
    try{ next = window.WishDeck.add(wishGet(), name, 1, window._currentSavedDeckId || null); }
    catch(e){ _toast(e.message); return; }
    wishSave(next);
    // 価格の収集対象に登録（wishAdd と同じ。失敗しても画面には影響させない）
    try{
      fetch('/api/track', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ card: name }) }).catch(function(){});
    }catch(_){}
    try{ if(typeof renderWishlist === 'function') renderWishlist(); }catch(_){}
    _ga('wish_add', { source: 'mydeck_menu', count: 1 });
    _toast('「' + name + '」を購入候補に入れました（候補に' + _wishCount(name) + '枚）');
  }

  // ══════════════ PC: 右クリックメニュー ══════════════
  var _menu = null; // { el, cell, cleanup }
  var _refocus = null; // メニューで操作したカード。描き直しの後にフォーカスを戻す（キーボードで続けて操作できるように）
  function _closeMenu(){
    if(!_menu) return;
    var m = _menu;
    _menu = null;
    m.cleanup();
    if(m.el.parentNode) m.el.parentNode.removeChild(m.el);
    if(m.cell) m.cell.classList.remove('is-sel');
  }
  function _openMenu(cell, x, y){
    _closeMenu();
    var name = cell.dataset.card;
    var sec = cell.dataset.sec || 'main';
    var o = ops();
    if(!name || !o) return;
    var el = document.createElement('div');
    el.className = 'deck-head-pop deck-more-menu deck-card-menu';
    el.setAttribute('role', 'menu');
    var head = document.createElement('div');
    head.className = 'deck-card-menu-head';
    head.textContent = name;
    el.appendChild(head);
    var items = [];
    // mutates: 枚数を変える（一覧が描き直されるので、描き直しの後にフォーカスを戻す）。それ以外はすぐ戻す（詳細はドロワーへ）
    function item(label, sub, fn, mutates){
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'deck-more-item';
      b.setAttribute('role', 'menuitem');
      var l = document.createElement('span');
      l.textContent = label;
      b.appendChild(l);
      if(sub){
        var s = document.createElement('span');
        s.className = 'deck-more-sub';
        s.textContent = sub;
        b.appendChild(s);
      }
      b.addEventListener('click', function(){
        if(mutates) _refocus = { name: name, sec: sec, until: Date.now() + 1500 };
        _closeMenu();
        if(!mutates && fn !== info && document.body.contains(cell)){ try{ cell.focus({ preventScroll: true }); }catch(_){} }
        fn();
      });
      el.appendChild(b);
      items.push(b);
    }
    function sep(){ var s = document.createElement('div'); s.className = 'deck-more-sep'; el.appendChild(s); }
    var n = _qty(name, sec);
    var src = _cellImg(cell);
    function info(){ _openInfo(name, src); }
    item('デッキに1枚足す', '今 ' + n + '枚', function(){ o.inc(name, sec); }, true);
    item('デッキから1枚抜く', '', function(){ o.dec(name, sec); }, true);
    item('デッキから外す', '', function(){ o.remove(name, sec); }, true);
    sep();
    item('購入候補に入れる', '候補に ' + _wishCount(name) + '枚', function(){ _addToWish(name); });
    item('カードの詳細', '', info);
    item('価格を比べる', '', function(){ window.open('/card/' + encodeURIComponent(name), '_blank', 'noopener'); });

    document.body.appendChild(el);
    // マウスの位置に出す。画面からはみ出す向きなら反対側へ
    var m = 8, vw = document.documentElement.clientWidth, vh = window.innerHeight;
    var w = el.offsetWidth, h = el.offsetHeight;
    var left = x + w + m > vw ? Math.max(m, x - w) : x;
    var top = y + h + m > vh ? Math.max(m, y - h) : y;
    el.style.left = left + 'px';
    el.style.top = top + 'px';
    cell.classList.add('is-sel'); // メニューの対象（金の実線＝選択中）

    function onDown(e){ if(!el.contains(e.target)) _closeMenu(); }
    function onKey(e){
      if(e.key === 'Escape'){ e.preventDefault(); var c = cell; _closeMenu(); if(c && document.body.contains(c)) c.focus({ preventScroll: true }); return; }
      var i = items.indexOf(document.activeElement);
      if(e.key === 'ArrowDown'){ e.preventDefault(); (items[i + 1] || items[0]).focus(); }
      else if(e.key === 'ArrowUp'){ e.preventDefault(); (items[i - 1] || items[items.length - 1]).focus(); }
    }
    function onScroll(e){ if(!el.contains(e.target)) _closeMenu(); }
    document.addEventListener('pointerdown', onDown, true);
    document.addEventListener('keydown', onKey);
    window.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', _closeMenu);
    _menu = { el: el, cell: cell, cleanup: function(){
      document.removeEventListener('pointerdown', onDown, true);
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('resize', _closeMenu);
    } };
    items[0].focus({ preventScroll: true });
  }

  // ══════════════ タッチ: 選んだカードだけ上の欄で増減 ══════════════
  var _sel = null; // { name, sec }
  var _bar = null;
  function _ensureBar(){
    if(_bar) return _bar;
    var host = $('deckPaneMain');
    if(!host) return null;
    var bar = document.createElement('div');
    bar.className = 'deck-sel-bar';
    bar.id = 'deckSelBar';
    bar.hidden = true;
    bar.innerHTML =
      '<span class="deck-sel-thumb"></span>' +
      '<span class="deck-sel-name"></span>' +
      '<span class="deck-sel-step">' +
        '<button type="button" class="deck-sel-btn" data-act="dec" aria-label="1枚抜く">−</button>' +
        '<span class="deck-sel-qty" aria-live="polite"></span>' +
        '<button type="button" class="deck-sel-btn" data-act="inc" aria-label="1枚足す">＋</button>' +
      '</span>' +
      '<button type="button" class="deck-pill deck-sel-info" data-act="info">詳細</button>' +
      '<button type="button" class="deck-sel-close" data-act="close" aria-label="選択をやめる" title="選択をやめる">✕</button>';
    bar.addEventListener('click', function(e){
      var b = e.target.closest('[data-act]');
      if(!b || !_sel) return;
      var o = ops();
      var act = b.dataset.act;
      if(act === 'inc' && o) o.inc(_sel.name, _sel.sec);
      else if(act === 'dec' && o) o.dec(_sel.name, _sel.sec);
      else if(act === 'info') _openInfo(_sel.name, _sel.src);
      else if(act === 'close') select(null);
    });
    // 見出し（所持枚数の欄）の上、カードの一覧より前に置く
    var own = $('deckOwnership');
    host.insertBefore(bar, own || $('deckCardList'));
    _bar = bar;
    return bar;
  }
  function _renderBar(){
    var bar = _ensureBar();
    if(!bar) return;
    if(!_sel){ bar.hidden = true; return; }
    var thumb = bar.querySelector('.deck-sel-thumb');
    thumb.textContent = '';
    if(_sel.src){
      var img = document.createElement('img');
      img.src = _sel.src;
      img.alt = '';
      thumb.appendChild(img);
    }
    bar.querySelector('.deck-sel-name').textContent = _sel.name;
    bar.querySelector('.deck-sel-qty').textContent = String(_qty(_sel.name, _sel.sec));
    bar.hidden = false;
  }
  function _markSel(){
    var list = $('deckCardList');
    if(!list) return;
    list.querySelectorAll('.deck-grid-cell.is-sel').forEach(function(c){ c.classList.remove('is-sel'); });
    if(!_sel) return;
    var cell = null;
    list.querySelectorAll('.deck-grid-cell').forEach(function(c){
      if(!cell && c.dataset.card === _sel.name && (c.dataset.sec || 'main') === _sel.sec) cell = c;
    });
    if(cell){
      cell.classList.add('is-sel');
      var src = _cellImg(cell);
      if(src) _sel.src = src;
    }
  }
  function select(cell){
    if(!cell){ _sel = null; }
    else{
      var name = cell.dataset.card, sec = cell.dataset.sec || 'main';
      // もう一度タップしたら選択をやめる
      if(_sel && _sel.name === name && _sel.sec === sec) _sel = null;
      else _sel = { name: name, sec: sec, src: _cellImg(cell) };
    }
    _markSel();
    _renderBar();
  }

  // 描き直しのたびに（renderDeckGrid は一覧を作り直す）選択の印と欄を付け直す。抜いて0枚になったら選択をやめる
  function afterRender(){
    if(_sel && _qty(_sel.name, _sel.sec) <= 0) _sel = null;
    _markSel();
    _renderBar();
    _restoreFocus();
    // カードの上は「×N」の札だけ（シミュレーターと同じ書き方）
    var list = $('deckCardList');
    if(list){
      list.querySelectorAll('.deck-grid-cell').forEach(function(cell){
        var b = cell.querySelector('.deck-grid-badge');
        if(b && !/^×/.test(b.textContent)) b.textContent = '×' + b.textContent.trim();
        // PC は Tab でカードを選び、メニューキー（Shift+F10）で右クリックメニューを開けるようにする（以前の ± ボタンは Tab で押せた）
        if(!isTouch() && !cell.hasAttribute('tabindex')){
          cell.setAttribute('tabindex', '0');
          cell.setAttribute('aria-label', cell.dataset.card + '（メニューキーで枚数を変える）');
        }
      });
    }
    _restoreFocus();
  }

  function _restoreFocus(){
    if(!_refocus) return;
    // 一覧はすぐ1回と、少し後の価格プレビュー（deck-edit.js の 350ms 後）でもう1回描き直されるので、しばらく戻し続ける
    if(Date.now() > _refocus.until){ _refocus = null; return; }
    var want = _refocus;
    var list = $('deckCardList');
    if(!list) return;
    var cell = null;
    list.querySelectorAll('.deck-grid-cell').forEach(function(c){
      if(!cell && c.dataset.card === want.name && (c.dataset.sec || 'main') === want.sec) cell = c;
    });
    // 外したカード・詳細を開いたとき（ドロワーにフォーカスがある）は戻さない
    if(!cell || document.querySelector('.deck-card-drawer.open')){ _refocus = null; return; }
    if(!cell.hasAttribute('tabindex')) return; // 札と tabindex を付けた後（下の処理の後）にもう一度呼ばれる
    try{ cell.focus({ preventScroll: true }); }catch(_){}
  }

  // ── 配線 ──
  function _init(){
    var list = $('deckCardList');
    if(!list) return;
    list.addEventListener('contextmenu', function(e){
      if(isTouch()) return; // タッチ端末の長押しは端末の動きに任せる（選択はタップ）
      var cell = e.target.closest('.deck-grid-cell');
      if(!cell || !cell.dataset.card) return;
      e.preventDefault();
      // キーボード（メニューキー・Shift+F10）で開いたときは位置が無いので、カードの右下寄りに出す
      var x = e.clientX, y = e.clientY;
      if(!x && !y){ var r = cell.getBoundingClientRect(); x = r.left + r.width * 0.6; y = r.top + r.height * 0.4; }
      _openMenu(cell, x, y);
    });
    // タッチ端末: タップは選択（詳細ドロワーを開く deck-card-info.js より先に受け取り、そちらへは流さない）
    list.addEventListener('click', function(e){
      if(!isTouch()) return;
      var cell = e.target.closest('.deck-grid-cell');
      if(!cell || !cell.dataset.card) return;
      e.preventDefault();
      e.stopPropagation();
      select(cell);
    }, true);
    // カード・欄・詳細ドロワーの外を押したら選択をやめる
    document.addEventListener('click', function(e){
      if(!_sel) return;
      if(e.target.closest('.deck-grid-cell, #deckSelBar, .deck-card-drawer, .deck-head-pop')) return;
      select(null);
    });
    // デッキが入れ替わったら選択をやめる（読込・新規・取り込み）
    ['loadSavedDeck', 'clearDeck', 'onDeckImported'].forEach(function(fn){
      var orig = window[fn];
      if(typeof orig !== 'function') return;
      window[fn] = function(){
        _sel = null;
        _closeMenu();
        return orig.apply(this, arguments);
      };
    });
    _renderBar();
  }

  window.DeckCardOps = { afterRender: afterRender, select: select };

  // deck-edit.js・deck-head.js と同じく、index.html のインライン側の初期化より後に動かす
  if(document.readyState === 'complete') _init();
  else document.addEventListener('DOMContentLoaded', _init);
})();
