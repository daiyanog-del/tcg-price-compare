/**
 * deck-card-info.js
 * マイデッキのカードをクリックしたとき、右からのドロワーに
 * カード詳細（効果・属性・ステータス・最安値・販売/買取リンク）を表示する。
 *
 * 一人回しの showCardByName() をそのまま流用（描画先は #cardInfoPanel）。
 * solitaire 固有の initCardInfoPanel() は使わない（.tier-item 委譲や cipToggle は不要）。
 */

import { showCardByName } from '/static/solitaire/js/ui/card-info-panel.js';

(function () {
  // マイデッキのグリッドだけを対象にする（環境デッキ #metaDeckCardList は従来通り）
  const list = document.getElementById('deckCardList');
  const drawer = document.getElementById('deckCardDrawer');
  if (!list || !drawer) return;

  function openDrawer() {
    drawer.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
  }

  function closeDrawer() {
    drawer.classList.remove('open');
    drawer.setAttribute('aria-hidden', 'true');
  }

  // ── デッキ内カードのクリック → ドロワー表示 ──
  // renderDeckGrid() は <a class="deck-grid-img-wrap" href="/card/..."> を生成する。
  // 左クリックのみ乗っ取り、修飾キー/中クリックは素通し（別タブ遷移を温存）。
  list.addEventListener('click', (e) => {
    const wrap = e.target.closest('.deck-grid-img-wrap');
    if (!wrap) return; // 編集モードの ±/× ボタンは wrap の外なので自然に除外される
    // 別タブで開きたいユーザー操作はそのまま通す
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;

    e.preventDefault();
    const cell = wrap.closest('[data-card]');
    const name = cell && cell.dataset.card;
    if (!name) return;
    const img = wrap.querySelector('img');
    const src = img ? img.src : '';
    showCardByName(name, src);
    openDrawer();
  });

  // ── 閉じる操作: ×ボタン・ESC・パネル外クリック ──
  // 背景dimはクリックを奪わない設計のため、閉じ判定は document で行う。
  drawer.addEventListener('click', (e) => {
    if (e.target.closest('[data-drawer-close]')) closeDrawer();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && drawer.classList.contains('open')) closeDrawer();
  });
  // パネル外クリックで閉じる。ただしデッキ内カード(.deck-grid-img-wrap)は
  // 「差し替え」操作なので閉じない（list のリスナーが内容を更新する）。
  document.addEventListener('click', (e) => {
    if (!drawer.classList.contains('open')) return;
    if (e.target.closest('.deck-card-drawer-panel')) return;
    if (e.target.closest('.deck-grid-img-wrap')) return;
    closeDrawer();
  });
})();
