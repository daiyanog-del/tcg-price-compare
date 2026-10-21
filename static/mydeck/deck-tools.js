/*
 * マイデッキの右の欄（2026-10-21 ユーザーがモックを見て承認。モック _mocks/mydeck-refresh-20261020 の E）。
 *
 *   上から「カードを追加」「取り込み・編集」「デッキの値段」の3区画。
 *   - 「ニューロンから取り込む」「テキストで編集」は、以前の開閉欄（details）をやめて押すと窓にする
 *     （PC は右の欄の左へ重ねる・スマホ ≤900px は下から出るシート）。中身の要素（#deckTextarea など）は消さずに表示だけ切り替える。
 *   - 価格計算の欄（.deck-calc）は PC では右の欄へ（デッキの下にあるとカードが多いと見えない）。スマホはデッキの一番下のまま。
 *     要素は1つなので、画面の幅で置き場所を移す。
 *   - テキスト欄に打ったら、少し待ってカードの一覧にも反映する（以前は価格計算を押すまで一覧が変わらなかった）。
 *
 * 見た目は deck.css 末尾。ボタンの形はシミュレーターの .sol-side-btn にそろえる。
 */
(function(){
  'use strict';

  function $(id){ return document.getElementById(id); }
  function _isSheet(){ return window.matchMedia ? window.matchMedia('(max-width: 900px)').matches : false; }

  // ── 窓（取り込み・テキスト） ──
  var _open = null; // { pop, btn, dim, cleanup }
  var POPS = { import: { pop: 'deckImportPop', btn: 'deckImportBtn' }, text: { pop: 'deckTextPop', btn: 'deckTextBtn' } };

  function _place(pop, btn){
    if(_isSheet()){ pop.style.left = ''; pop.style.top = ''; pop.style.maxHeight = ''; return; }
    var m = 8, vh = window.innerHeight;
    var r = btn.getBoundingClientRect();
    var w = pop.offsetWidth, h = pop.offsetHeight;
    // 右の欄の左に重ねる（欄の中のボタンを隠さない）。入らなければボタンの下
    var left = r.left - w - 10;
    if(left < m) left = Math.max(m, Math.min(r.left, document.documentElement.clientWidth - w - m));
    var top = Math.min(Math.max(m, r.top - 60), Math.max(m, vh - h - m));
    pop.style.left = left + 'px';
    pop.style.top = top + 'px';
    pop.style.maxHeight = (vh - m * 2) + 'px';
  }

  function openPop(kind){
    var spec = POPS[kind];
    if(!spec) return;
    var pop = $(spec.pop), btn = $(spec.btn);
    if(!pop || !btn) return;
    if(_open && _open.pop === pop) return;
    closePop(false);
    var sheet = _isSheet();
    pop.classList.toggle('is-sheet', sheet);
    pop.hidden = false;
    var dim = null;
    if(sheet){
      dim = document.createElement('div');
      dim.className = 'deck-lib-dim';
      dim.addEventListener('click', function(){ closePop(true); });
      pop.parentNode.insertBefore(dim, pop);
    }
    _place(pop, btn);
    btn.classList.add('is-active');
    btn.setAttribute('aria-expanded', 'true');
    var textBefore = ($('deckTextarea') || {}).value;

    function onDown(e){
      if(pop.contains(e.target) || btn.contains(e.target)) return;
      // ニューロン取り込みの確認窓・ファイル選択・お知らせを押したときは閉じない
      if(e.target.closest && e.target.closest('.deck-head-toast, .neuron-preview-modal, .neuron-modal, [role="dialog"]')) return;
      closePop(false);
    }
    function onKey(e){ if(e.key === 'Escape'){ e.preventDefault(); e.stopImmediatePropagation(); closePop(true); } }
    function onMove(e){ if(e && e.type === 'scroll' && pop.contains(e.target)) return; _place(pop, btn); }
    document.addEventListener('pointerdown', onDown, true);
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('scroll', onMove, true);
    window.addEventListener('resize', onMove);
    _open = { pop: pop, btn: btn, dim: dim, kind: kind, cleanup: function(){
      document.removeEventListener('pointerdown', onDown, true);
      document.removeEventListener('keydown', onKey, true);
      window.removeEventListener('scroll', onMove, true);
      window.removeEventListener('resize', onMove);
      // テキストを変えて閉じたら、一覧にも反映しておく（入力中の反映を待たずに閉じた場合）
      if(kind === 'text' && ($('deckTextarea') || {}).value !== textBefore) _previewNow();
    } };
    var focusEl = kind === 'text' ? $('deckTextarea') : pop.querySelector('.deck-tool-pop-x');
    if(focusEl) focusEl.focus({ preventScroll: true });
  }

  function closePop(restoreFocus){
    if(!_open) return;
    var o = _open;
    _open = null;
    o.cleanup();
    o.pop.hidden = true;
    if(o.dim && o.dim.parentNode) o.dim.parentNode.removeChild(o.dim);
    o.btn.classList.remove('is-active');
    o.btn.setAttribute('aria-expanded', 'false');
    if(restoreFocus) o.btn.focus({ preventScroll: true });
  }

  // ── テキスト欄の入力を一覧へ反映 ──
  var _previewTimer = null;
  function _previewNow(){
    clearTimeout(_previewTimer);
    var ta = $('deckTextarea');
    if(!ta) return;
    if(ta.value.trim()){
      if(typeof calcDeckEstimate === 'function' && typeof DECK_CTX !== 'undefined') calcDeckEstimate(DECK_CTX.mydeck, { previewOnly: true });
    }else{
      try{ _currentMydeckCards = { main: [], ex: [] }; _currentMydeckText = ''; }catch(_){}
      if(typeof window._refreshDeckChrome === 'function') window._refreshDeckChrome();
    }
  }

  // ── 価格計算の欄の置き場所（PC は右の欄・スマホはデッキの一番下） ──
  function _placeCalc(){
    var calc = $('deckCalc');
    var slot = _isSheet() ? $('deckCalcSlotMain') : $('deckCalcSlotTools');
    if(calc && slot && calc.parentNode !== slot) slot.appendChild(calc);
    var h = $('deckCalcHeading');
    if(h) h.hidden = _isSheet();
  }

  function _init(){
    Object.keys(POPS).forEach(function(kind){
      var btn = $(POPS[kind].btn);
      if(btn) btn.addEventListener('click', function(){
        if(_open && _open.kind === kind) closePop(true); else openPop(kind);
      });
      var pop = $(POPS[kind].pop);
      var x = pop && pop.querySelector('.deck-tool-pop-x');
      if(x) x.addEventListener('click', function(){ closePop(true); });
    });
    var ta = $('deckTextarea');
    if(ta) ta.addEventListener('input', function(){
      clearTimeout(_previewTimer);
      _previewTimer = setTimeout(_previewNow, 600);
    });
    // 取り込めたら窓を閉じる（取り込んだデッキが一覧に出る）
    var orig = window.onDeckImported;
    if(typeof orig === 'function'){
      window.onDeckImported = function(){
        var r = orig.apply(this, arguments);
        if(_open && _open.kind === 'import') closePop(false);
        return r;
      };
    }
    _placeCalc();
    if(window.matchMedia){
      var mq = window.matchMedia('(max-width: 900px)');
      var onChange = function(){ closePop(false); _placeCalc(); };
      if(mq.addEventListener) mq.addEventListener('change', onChange); else if(mq.addListener) mq.addListener(onChange);
    }
  }

  window.DeckTools = { openImport: function(){ _placeCalc(); openPop('import'); }, openText: function(){ openPop('text'); }, close: closePop };

  if(document.readyState === 'complete') _init();
  else document.addEventListener('DOMContentLoaded', _init);
})();
