/* deck-replays-top.js — マイデッキの「このデッキのリプレイ」（右の欄の4つ目の区画・2026-10-21 ユーザー承認のモック
   _mocks/deck-replay-link-20261021 の 6・8）。スマホは同じ区画が「カードを追加・取込」タブに並ぶ。
   一覧・差の計算は shared/deck-replays.js（window.DeckReplays）。行を押すとシミュレーターでそのリプレイを開く。
   deck-edit.js の見出しの描き直し（_updateDeckSaveBar）から refresh が呼ばれる。リプレイは端末の中だけ。 */
(function(global){
  'use strict';
  var EMPTY = 'まだありません。シミュレーターで回したあと「保存」→「端末に保存」すると、ここに入ります。';

  function $(id){ return document.getElementById(id); }
  function el(tag, cls, text){ var e = document.createElement(tag); if(cls) e.className = cls; if(text !== undefined) e.textContent = text; return e; }

  function _deck(){
    var id = global._currentSavedDeckId;
    if(!id || typeof global.savedDecksGet !== 'function') return null;
    return global.savedDecksGet().find(function(d){ return d.id === id; }) || null;
  }

  // シミュレーターの今の記録（同じタブで退避されたもの）が置き換わるときは確認する。
  // リプレイ一覧ページと同じ判定（static/solitaire/js/utils/session-resume-store.js shouldConfirmOpen・キー 'sol-session-resume-steps'）。
  // 新しいタブで開く（修飾キー・中クリック）ときは今のページが残るので確認しない
  function _confirmReplace(ev){
    if(!ev || ev.button !== 0 || ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.altKey) return true;
    var raw = null;
    try{ raw = sessionStorage.getItem('sol-session-resume-steps'); }catch(e){ return true; }
    if(raw === null || raw === '') return true;
    var steps = parseInt(raw, 10);
    if(!isFinite(steps) || steps < 0) return true;
    var what = steps > 0 ? '今の記録（' + steps + '手）' : '今の記録';
    return confirm('シミュレーターの' + what + 'は、このリプレイに置き換わります。開きますか？');
  }

  // 同じ中身なら描き直さない（入力中のフォーカスや押している途中を崩さない）
  var _sig = '';
  function refresh(){
    var DR = global.DeckReplays, h = $('deckReplaysH'), body = $('deckReplaysBody');
    if(!DR || !h || !body) return;
    var deck = _deck();
    var items = deck ? DR.forDeck(DR.list(), deck.id) : [];
    var contents = deck ? (typeof global.normalizeDeck === 'function' ? global.normalizeDeck(deck) : deck) : null;
    var rows = items.map(function(r){
      var d = DR.diff(r.deckSnap, contents);
      return { id: r.id, title: r.title || '無題', meta: DR.meta(r), drift: !!(d && d.changed) };
    });
    var sig = JSON.stringify([deck ? deck.id : null, rows]);
    if(sig === _sig) return;
    _sig = sig;
    h.hidden = body.hidden = !deck;
    body.replaceChildren();
    if(!deck) return;
    if(!rows.length){ body.appendChild(el('p', 'deck-drp-empty', EMPTY)); return; }
    rows.forEach(function(r){
      // 本物のリンクにする（新しいタブでも開ける）
      var b = el('a', 'deck-drp-row');
      b.href = '/solitaire?saved=' + encodeURIComponent(r.id);
      b.title = r.title + '（シミュレーターで開く）';
      b.appendChild(el('span', 'deck-drp-title', r.title));
      var m = el('span', 'deck-drp-meta', r.meta);
      if(r.drift){ m.appendChild(document.createTextNode('・')); m.appendChild(el('span', 'deck-drp-drift', DR.DRIFT_SHORT)); }
      b.appendChild(m);
      b.addEventListener('click', function(ev){
        if(!_confirmReplace(ev)){ ev.preventDefault(); return; }
        if(typeof _ga === 'function') _ga('deck_replay_open', { from: 'mydeck' });
      });
      body.appendChild(b);
    });
  }

  global.DeckReplaysTop = { refresh: refresh };
  // 別のタブ（シミュレーター）でリプレイを保存したとき
  global.addEventListener('storage', function(e){ if(global.DeckReplays && e.key === global.DeckReplays.KEY){ _sig = ''; refresh(); } });
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', refresh); else refresh();
})(window);
