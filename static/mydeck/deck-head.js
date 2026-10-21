/*
 * マイデッキの見出し（デッキ名・状態・操作ボタン）と、保存まわりの操作（2026-10-20 ユーザーがモックを見て承認。
 * モック _mocks/mydeck-refresh-20261020 の A＋B）。
 *
 *   - 保存済みのデッキを開いている間は自動で保存される（deck-edit.js _persistDeck）ので「保存」ボタンは出さない。
 *     未登録のデッキのときだけ「マイデッキに登録」を出し、名前はページ内の小窓で入れる（ブラウザの入力窓 prompt() をやめた）。
 *   - 「名前を変える」「別のデッキとして保存」「新しく作る」「端末同期」「このデッキを削除」は ⋯ メニュー。
 *   - 削除は確認窓を出さず、すぐ消して6秒間「取り消す」を出す（シミュレーターの上書き保存と同じ考え方）。
 *   - 枚数（メイン N / 60・EX N / 15）は各欄の見出しに出す（以前は見出しの上に別の行があり同じ枚数を2回出していた）。
 *
 * 見た目はシミュレーターのデッキ欄の見出し（components.css .sol-deck-bar-*）とメニュー（案Y）にそろえる。CSS は deck.css。
 */
(function(){
  'use strict';

  var NAME_MAX = 60;        // デッキ名の上限（シミュレーター deck-edit-ui.js と同じ）
  var UNDO_TOAST_MS = 6000; // 「取り消す」を押せる時間（シミュレーターと同じ）

  function $(id){ return document.getElementById(id); }
  function _decks(){ return (typeof savedDecksGet === 'function') ? savedDecksGet() : []; }
  function _curId(){ return window._currentSavedDeckId || null; }
  function _curDeck(){
    var id = _curId();
    if(!id) return null;
    return _decks().find(function(d){ return d.id === id; }) || null;
  }
  function _cards(){ return (typeof _currentMydeckCards !== 'undefined' && _currentMydeckCards) ? _currentMydeckCards : { main: [], ex: [] }; }
  function _sum(a){ return (a || []).reduce(function(s, c){ return s + (c.qty || 0); }, 0); }
  function _hasCards(){ var c = _cards(); return !!((c.main || []).length || (c.ex || []).length); }
  function _setDeckName(n){ try{ _currentDeckName = n; }catch(_){} }
  function _ga(name, params){ try{ if(typeof window._ga === 'function') window._ga(name, params); }catch(_){} }

  // 編集欄の内容を今の形で読み直す（saveCurrentDeck と同じ。テキストが変わっていれば読み直す）
  function _currentContents(){
    var ta = $('deckTextarea');
    var text = ta ? ta.value.replace(/\r/g, '') : '';
    if(typeof _currentMydeckText !== 'undefined' && text !== _currentMydeckText && typeof parseDeckSections === 'function'){
      _currentMydeckCards = parseDeckSections(text);
      _currentMydeckText = text;
    }
    var c = _cards();
    return { text: text, main: c.main || [], ex: c.ex || [] };
  }

  // 保存済み一覧を描き直し、開いているデッキに選択の印を付け直す
  function _rerenderList(){
    if(typeof renderSavedDecks === 'function') renderSavedDecks();
    var id = _curId();
    if(!id) return;
    try{
      var b = document.querySelector('#sdcard-' + CSS.escape(id) + ' .saved-deck-card-btn');
      if(b) b.classList.add('selected');
    }catch(_){}
  }
  function _persist(){ if(typeof window._deckPersist === 'function') window._deckPersist(); }
  function _chrome(){ if(typeof window._refreshDeckChrome === 'function') window._refreshDeckChrome(); else refresh(); }

  // ── 見出し ─────────────────────────────────────
  // buttonsOnly: テキスト欄の入力中。カードの一覧はまだ描き直されないので、欄の見出しの枚数は変えずにボタンの出し分けだけ直す
  function refresh(buttonsOnly){
    var nameEl = $('deckHeadName');
    var stateEl = $('deckSaveStatus');
    var regBtn = $('deckRegisterBtn');
    var imgBtn = $('deckImageBtn');
    var d = _curDeck();
    var has = _hasCards();
    if(nameEl){
      nameEl.textContent = d ? (d.name || 'マイデッキ') : 'マイデッキ未登録';
      nameEl.classList.toggle('is-unregistered', !d);
    }
    if(stateEl){
      stateEl.textContent = d ? '自動で保存しています' : '';
      stateEl.hidden = !d;
    }
    if(regBtn) regBtn.hidden = !!d || !has;
    // 「テキストで入力・編集」の下にも同じ登録ボタン（以前の「保存」の位置）
    var taReg = $('deckTextRegisterBtn');
    if(taReg) taReg.hidden = !!d || !has;
    if(imgBtn) imgBtn.hidden = !has;
    if(buttonsOnly !== true) _refreshSectionLabels();
  }

  // 各欄の見出しを「メインデッキ 40 / 60」の形に直す（renderDeckGrid は他の画面と共通なのでここで書き換える）。
  // 価格計算後の小計（.deck-section-subtotal・_updateSectionSubtotals が足す）は残す。
  function _refreshSectionLabels(){
    var list = $('deckCardList');
    if(!list) return;
    var c = _cards();
    list.querySelectorAll('.deck-grid-section-label').forEach(function(label){
      var sec = label.dataset.sec;
      if(!sec){
        sec = /^エクストラ/.test(label.textContent.trim()) ? 'ex' : 'main';
        label.dataset.sec = sec;
      }
      var n = _sum(sec === 'ex' ? c.ex : c.main);
      var max = sec === 'ex' ? 15 : 60;
      var state = sec === 'ex' ? (n > 15 ? 'over' : '') : (n > 60 ? 'over' : (n > 0 && n < 40 ? 'warn' : ''));
      var sub = label.querySelector('.deck-section-subtotal');
      label.textContent = '';
      label.classList.add('deck-sec-label');
      var nm = document.createElement('span');
      nm.className = 'deck-sec-name';
      nm.textContent = sec === 'ex' ? 'エクストラデッキ' : 'メインデッキ';
      var num = document.createElement('span');
      num.className = 'deck-sec-n' + (state ? ' ' + state : '');
      num.textContent = String(n);
      var mx = document.createElement('span');
      mx.className = 'deck-sec-max';
      mx.textContent = '/ ' + max;
      label.append(nm, num, mx);
      if(sub) label.appendChild(sub);
      // PC は増減を右クリックメニューに移したので、気づけるよう一言添える（マウスで指せる環境だけ。指で使う端末はタップで選ぶ）
      if(sec === 'main' && window.DeckCardOps && window.DeckCardOps.finePointer()){
        var hint = document.createElement('span');
        hint.className = 'deck-sec-hint';
        hint.textContent = '右クリックで枚数を変える・ドラッグで並べ替え';
        label.appendChild(hint);
      }
    });
  }

  // ── 浮かせる窓（登録・名前・メニュー共通） ──────────────────
  var _pop = null;      // { el, anchor, cleanup }
  function _closePop(restoreFocus){
    if(!_pop) return;
    var p = _pop;
    _pop = null;
    p.cleanup();
    if(p.el.parentNode) p.el.parentNode.removeChild(p.el);
    if(restoreFocus && p.anchor && document.body.contains(p.anchor)){
      try{ p.anchor.focus({ preventScroll: true }); }catch(_){}
    }
  }
  // 起点のボタンの下に置く。右端のボタンなら右をそろえる。下に入らなければ上へ
  function _place(el, anchor, alignRight){
    var m = 8;
    var vw = document.documentElement.clientWidth, vh = window.innerHeight;
    var r = anchor.getBoundingClientRect();
    var w = el.offsetWidth, h = el.offsetHeight;
    var left = alignRight ? r.right - w : r.left;
    left = Math.min(Math.max(m, left), Math.max(m, vw - w - m));
    var top = r.bottom + 6;
    if(top + h > vh - m){
      var above = r.top - 6 - h;
      top = above >= m ? above : Math.max(m, vh - h - m);
    }
    el.style.left = left + 'px';
    el.style.top = top + 'px';
  }
  function _openPop(el, anchor, alignRight){
    _closePop(false);
    document.body.appendChild(el);
    _place(el, anchor, alignRight);
    function onDown(e){
      if(el.contains(e.target) || anchor.contains(e.target)) return;
      _closePop(false);
    }
    function onKey(e){ if(e.key === 'Escape'){ e.preventDefault(); _closePop(true); } }
    function onMove(e){
      if(e && e.type === 'scroll' && el.contains(e.target)) return;
      if(!document.body.contains(anchor)){ _closePop(false); return; }
      _place(el, anchor, alignRight);
    }
    document.addEventListener('pointerdown', onDown, true);
    document.addEventListener('keydown', onKey);
    window.addEventListener('scroll', onMove, true);
    window.addEventListener('resize', onMove);
    _pop = { el: el, anchor: anchor, cleanup: function(){
      document.removeEventListener('pointerdown', onDown, true);
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('scroll', onMove, true);
      window.removeEventListener('resize', onMove);
    } };
  }
  function _isOpenFor(anchor){ return !!(_pop && _pop.anchor === anchor); }

  // 名前を入れる小窓。onOk(name) が文字列を返したらその文を出して閉じない（重複など）
  function _openNameDialog(anchor, opts){
    if(_isOpenFor(anchor)){ _closePop(true); return; }
    var el = document.createElement('div');
    el.className = 'deck-head-pop deck-name-pop';
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-label', opts.title);
    var form = document.createElement('form');
    var label = document.createElement('label');
    label.className = 'deck-name-pop-label';
    label.textContent = opts.title;
    var input = document.createElement('input');
    input.type = 'text';
    input.className = 'deck-name-pop-input';
    input.maxLength = NAME_MAX;
    input.autocomplete = 'off';
    input.placeholder = 'デッキ名';
    input.value = opts.initial || '';
    label.appendChild(input);
    var err = document.createElement('div');
    err.className = 'deck-name-pop-err';
    err.setAttribute('role', 'alert');
    err.hidden = true;
    var row = document.createElement('div');
    row.className = 'deck-name-pop-row';
    var cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'deck-pill';
    cancel.textContent = 'やめる';
    cancel.addEventListener('click', function(){ _closePop(true); });
    var ok = document.createElement('button');
    ok.type = 'submit';
    ok.className = 'deck-pill is-primary';
    ok.textContent = opts.okLabel;
    row.append(cancel, ok);
    form.append(label, err, row);
    el.appendChild(form);
    input.addEventListener('input', function(){ err.hidden = true; });
    form.addEventListener('submit', function(e){
      e.preventDefault();
      var n = input.value.trim().slice(0, NAME_MAX);
      if(!n){ err.textContent = '名前を入れてください'; err.hidden = false; input.focus(); return; }
      var msg = opts.onOk(n);
      if(typeof msg === 'string'){ err.textContent = msg; err.hidden = false; input.focus(); input.select(); return; }
      _closePop(true);
    });
    _openPop(el, anchor, !!opts.alignRight);
    input.focus();
    input.select();
  }

  function _dupMessage(n, exceptId){
    var dup = _decks().some(function(d){ return d.name === n && d.id !== exceptId; });
    return dup ? '「' + n + '」はすでにマイデッキにあります。別の名前にしてください。' : null;
  }

  // ── A: 登録・別のデッキとして保存・名前を変える ─────────────────
  // 今の編集欄の内容を新しいマイデッキとして保存し、以後の編集をそちらへ自動で保存する
  function _createFromEditor(name){
    var c = _currentContents();
    var deck = { id: 'd_' + Date.now(), name: name, text: c.text, main: c.main, ex: c.ex, updated: Date.now() };
    if(window.DeckOwnership) deck.owned = window.DeckOwnership.snapshot();
    var list = _decks();
    list.push(deck);
    savedDecksSet(list);
    window._currentSavedDeckId = deck.id;
    _setDeckName(name);
    if(typeof trackDeckCards === 'function') trackDeckCards([].concat(c.main, c.ex));
    _persist();
    _rerenderList();
    _chrome();
    return deck;
  }

  function openRegister(anchor){
    _currentContents(); // テキスト欄だけ変えた直後でも今の中身で判定する
    if(!_hasCards()){ _toast('デッキにカードがありません'); return; }
    var initial = '';
    try{ if(typeof _currentDeckName !== 'undefined' && _currentDeckName && !_dupMessage(_currentDeckName)) initial = _currentDeckName; }catch(_){}
    _openNameDialog(anchor, {
      title: 'デッキ名', okLabel: '登録する',
      initial: initial,
      onOk: function(n){
        var dup = _dupMessage(n);
        if(dup) return dup;
        _createFromEditor(n);
        _ga('deck_save', { where: 'top', kind: 'new' });
        _toast('「' + n + '」をマイデッキに登録しました');
      }
    });
  }

  function openSaveAs(anchor){
    var base = _curDeck();
    if(!base){ openRegister(anchor); return; }
    _openNameDialog(anchor, {
      title: '別のデッキとして保存', okLabel: '保存する', alignRight: true,
      initial: ((base.name || 'マイデッキ') + ' のコピー').slice(0, NAME_MAX),
      onOk: function(n){
        var dup = _dupMessage(n);
        if(dup) return dup;
        _createFromEditor(n);
        _ga('deck_save', { where: 'top', kind: 'copy' });
        _toast('「' + n + '」として保存しました。これからはこちらに保存します');
      }
    });
  }

  function openRename(anchor){
    var base = _curDeck();
    if(!base) return;
    _openNameDialog(anchor, {
      title: '名前を変える', okLabel: '変える', alignRight: true,
      initial: base.name || '',
      onOk: function(n){
        var list = _decks();
        var d = list.find(function(x){ return x.id === base.id; });
        if(!d) return '保存先のデッキが見つかりません（削除された可能性があります）';
        if(d.name === n) return;
        var dup = _dupMessage(n, d.id);
        if(dup) return dup;
        d.name = n;
        d.updated = Date.now();
        savedDecksSet(list);
        _setDeckName(n);
        _persist();
        _rerenderList();
        _chrome();
      }
    });
  }

  // ── A: 削除（確認窓なし・6秒間の取り消し） ─────────────────
  // 取り消しで名前がぶつかったら（取り消すまでの間に同じ名前で登録・名前の変更をした）「名前 (2)」にする（sync.py merge_decks と同じ付け方）
  function _freeName(n){
    var names = Object.create(null); // 「constructor」などの名前を空きと取り違えない
    _decks().forEach(function(d){ names[d.name] = true; });
    if(!names[n]) return n;
    for(var k = 2; ; k++){ var c = n + ' (' + k + ')'; if(!names[c]) return c; }
  }
  // そのデッキ用として入れた購入候補を「デッキ指定なし」に戻す（候補自体は消さない。以前の deleteSavedDeck と同じ）
  function _dropWishDeck(id){
    if(!window.WishDeck || typeof wishGet !== 'function' || typeof wishSave !== 'function') return;
    if(_decks().some(function(d){ return d.id === id; })) return; // 取り消しや同期で戻っていれば何もしない
    var list = wishGet();
    if(list.some(function(c){ return WishDeck.forDeck(c, id); })){
      wishSave(WishDeck.dropDeck(list, id));
      _renderWish();
    }
  }
  function _renderWish(){ try{ if(typeof renderWishlist === 'function') renderWishlist(); }catch(_){} }

  function deleteDeck(id){
    var list = _decks();
    var idx = list.findIndex(function(d){ return d.id === id; });
    if(idx < 0) return;
    var wasCurrent = (id === _curId());
    // 開いているデッキなら、自動保存（入力から400ms後）を待たずに今の中身を書き出してから消す（取り消しで最新の中身が戻るように）
    if(wasCurrent){
      _persist();
      list = _decks();
      idx = list.findIndex(function(d){ return d.id === id; });
      if(idx < 0) return;
    }
    var deck = list[idx];
    list.splice(idx, 1);
    savedDecksSet(list);
    if(wasCurrent){
      // 編集欄の中身は残し、未登録のデッキとして続けられるようにする
      window._currentSavedDeckId = null;
      _persist();
    }
    _rerenderList();
    _chrome();
    _renderWish(); // 購入候補の「どのデッキ用か」の札から消えたデッキを外す（内訳は取り消しの期限で戻す）
    var name = deck.name || 'マイデッキ';
    _undoToast('「' + name + '」を削除しました', function(){
      var cur = _decks();
      if(cur.some(function(d){ return d.id === deck.id; })){ _toast('すでに戻っています'); return; }
      var restoredName = _freeName(name);
      // 更新時刻は新しくする（古い時刻のままだと端末間同期で削除後の一覧が勝つことがある）
      var restored = Object.assign({}, deck, { name: restoredName, updated: Date.now() });
      cur.splice(Math.min(idx, cur.length), 0, restored);
      savedDecksSet(cur);
      // 削除したデッキを開いたまま、他のデッキに切り替えておらず中身も変えていなければ、ひも付けも戻す
      var ta = $('deckTextarea');
      if(wasCurrent && _curId() == null && ta && ta.value.replace(/\r/g, '') === String(deck.text || '').replace(/\r/g, '')){
        window._currentSavedDeckId = deck.id;
        _setDeckName(restoredName);
        _persist();
      }
      _rerenderList();
      _chrome();
      _renderWish();
      _toast(restoredName === name ? '「' + name + '」を元に戻しました'
        : '「' + restoredName + '」として元に戻しました（同じ名前のデッキがあるため）');
    }, function(){ _dropWishDeck(deck.id); });
  }

  // ── ⋯ メニュー ─────────────────────────────────
  function openMore(anchor){
    if(_isOpenFor(anchor)){ _closePop(true); return; }
    var d = _curDeck();
    var el = document.createElement('div');
    el.className = 'deck-head-pop deck-more-menu';
    el.setAttribute('role', 'menu');
    var items = [];
    function item(label, fn, cls){
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'deck-more-item' + (cls ? ' ' + cls : '');
      b.setAttribute('role', 'menuitem');
      b.textContent = label;
      b.addEventListener('click', function(){ fn(); });
      el.appendChild(b);
      items.push(b);
    }
    function sep(cls){ var s = document.createElement('div'); s.className = 'deck-more-sep' + (cls ? ' ' + cls : ''); el.appendChild(s); }
    // スマホでは「種別順に整列」を見出しから外してここに入れる（ボタンが2段に折れないように）
    item('種別順に整列', function(){ _closePop(true); if(typeof deckSortByType === 'function') deckSortByType(); }, 'deck-more-sort');
    sep('deck-more-sort');
    if(d){
      item('名前を変える', function(){ _closePop(false); openRename(anchor); });
      item('別のデッキとして保存', function(){ _closePop(false); openSaveAs(anchor); });
      sep();
    }
    item('＋ 新しく作る', function(){ _closePop(false); if(typeof deckCreateNew === 'function') deckCreateNew(); });
    item('端末同期', function(){ _closePop(false); if(typeof openSyncShareDialog === 'function') openSyncShareDialog(); });
    if(d){
      sep();
      item('このデッキを削除', function(){ _closePop(true); deleteDeck(d.id); });
    }
    _openPop(el, anchor, true);
    anchor.setAttribute('aria-expanded', 'true');
    var baseCleanup = _pop.cleanup;
    _pop.cleanup = function(){ baseCleanup(); anchor.setAttribute('aria-expanded', 'false'); };
    // 上下キーで項目を移る
    el.addEventListener('keydown', function(e){
      var vis = items.filter(function(b){ return b.offsetParent !== null; });
      var i = vis.indexOf(document.activeElement);
      if(e.key === 'ArrowDown'){ e.preventDefault(); (vis[i + 1] || vis[0]).focus(); }
      else if(e.key === 'ArrowUp'){ e.preventDefault(); (vis[i - 1] || vis[vis.length - 1]).focus(); }
    });
    var first = items.filter(function(b){ return b.offsetParent !== null; })[0];
    if(first) first.focus();
  }

  // ── B: デッキ画像 ───────────────────────────────
  // 価格計算・プレビューの描画で用意される値（index-main.js _deckShareData。合計を入れられる）を使う。
  // 名前は登録・名前の変更の後に描き直されないので、今の名前で上書きする。中身が描き直し前（入力から350ms以内）なら今のデッキから作る
  function _curName(){
    var d = _curDeck();
    if(d && d.name) return d.name;
    try{ if(typeof _currentDeckName !== 'undefined' && _currentDeckName) return _currentDeckName; }catch(_){}
    return 'デッキ';
  }
  function _sameCards(a, b){
    var key = function(list){ return (list || []).map(function(c){ return c.name + '×' + c.qty; }).join('\n'); };
    return key(a) === key(b);
  }
  function openImage(){
    var c = _currentContents();
    var nm = _curName();
    try{
      var sd = (typeof _deckShareData !== 'undefined') ? _deckShareData.deckShareArea : null;
      if(sd && typeof generateDeckImageUI === 'function' && _sameCards(sd.main, c.main) && _sameCards(sd.ex, c.ex)){
        sd.deckName = nm;
        generateDeckImageUI('deckShareArea');
        return;
      }
    }catch(_){}
    import('/static/shared/deck-image.js').then(function(m){
      m.openDeckImageDialog({ deckName: nm, main: c.main, ex: c.ex });
    }).catch(function(){
      alert('デッキ画像の機能を読み込めませんでした。ページを再読み込みしてお試しください。');
    });
  }

  // ── お知らせ ─────────────────────────────────
  // 取り消し付き（削除）と普通のお知らせは別の要素にし、取り消しを待っている間の普通のお知らせはその上に出す
  // （同じ要素だと「登録しました」などで「取り消す」が消えていた）。
  // 取り消し付きが続いたら、前のものはその時点で期限切れ（onExpire）にする。
  var _plain = null;   // { el, timer }
  var _undo = null;    // { el, timer, onExpire }
  function _mkToast(msg){
    var el = document.createElement('div');
    el.className = 'deck-head-toast';
    el.setAttribute('role', 'status');
    var t = document.createElement('span');
    t.textContent = msg;
    el.appendChild(t);
    return el;
  }
  // 取り消し付き → 普通 の順に下から積む。カード追加のお知らせ（deck-edit.js .deck-add-toast）が出ていればその上から
  // （高さは実際の大きさで測る。スマホで長いデッキ名が折り返しても重ならないように）
  function _placeToasts(){
    var y = 24;
    var add = document.querySelector('.deck-add-toast');
    if(add && add.offsetHeight) y = 20 + add.offsetHeight + 8;
    if(_undo){ _undo.el.style.bottom = y + 'px'; y += _undo.el.offsetHeight + 8; }
    if(_plain) _plain.el.style.bottom = y + 'px';
  }
  function _closePlain(){
    if(!_plain) return;
    clearTimeout(_plain.timer);
    if(_plain.el.parentNode) _plain.el.parentNode.removeChild(_plain.el);
    _plain = null;
  }
  function _closeUndo(expired){
    if(!_undo) return;
    var u = _undo;
    _undo = null;
    clearTimeout(u.timer);
    if(u.el.parentNode) u.el.parentNode.removeChild(u.el);
    _placeToasts();
    if(expired && u.onExpire){ try{ u.onExpire(); }catch(e){ console.warn('[deck-head] 削除の後始末に失敗しました', e); } }
  }
  function _toast(msg){
    _closePlain();
    var el = _mkToast(msg);
    document.body.appendChild(el);
    _plain = { el: el, timer: setTimeout(_closePlain, 4000) };
    _placeToasts();
  }
  function _undoToast(msg, onUndo, onExpire){
    _closeUndo(true);
    var el = _mkToast(msg);
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'deck-head-toast-btn';
    b.textContent = '取り消す';
    el.appendChild(b);
    var me = { el: el, onExpire: onExpire, timer: setTimeout(function(){ if(_undo === me) _closeUndo(true); }, UNDO_TOAST_MS) };
    b.addEventListener('click', function(){
      if(_undo !== me) return;
      _closeUndo(false);
      onUndo();
    });
    document.body.appendChild(el);
    _undo = me;
    _placeToasts();
  }

  // ── 配線 ─────────────────────────────────────
  function _init(){
    var reg = $('deckRegisterBtn');
    if(reg) reg.addEventListener('click', function(){ openRegister(reg); });
    var more = $('deckMoreBtn');
    if(more) more.addEventListener('click', function(){ openMore(more); });
    var img = $('deckImageBtn');
    if(img) img.addEventListener('click', openImage);
    var taReg = $('deckTextRegisterBtn');
    if(taReg) taReg.addEventListener('click', function(){ openRegister(taReg); });
    // テキスト欄に貼る・打つだけではカードの一覧が描き直されないので、ここで読み直して「マイデッキに登録」を出し分ける
    var ta = $('deckTextarea');
    if(ta) ta.addEventListener('input', function(){ _currentContents(); refresh(true); });
    refresh();
  }

  window.DeckHead = { refresh: refresh, deleteDeck: deleteDeck, openRegister: openRegister,
    toast: function(msg){ _toast(msg); }, // deck-card-ops.js（購入候補に入れた知らせ）
    _createFromEditor: _createFromEditor, _freeName: _freeName }; // _ で始まるものはテスト用（tests/js/deck_head_check.js）
  // 保存済み一覧の × も確認窓をやめて取り消しにする（index-wish.js の deleteSavedDeck を置き換える）
  window.deleteSavedDeck = deleteDeck;

  // deck-edit.js と同じく、index.html のインライン側の初期化より後に動かす（defer の実行時点は 'interactive'）
  if(document.readyState === 'complete') _init();
  else document.addEventListener('DOMContentLoaded', _init);
})();
