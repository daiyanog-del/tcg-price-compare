/**
 * card-zoom.js — カード画像を画面いっぱいに拡大して見る（2026-10-31 ユーザー指定「全カード拡大」）
 *
 * カードページの上のカード画像（index-main.js の #heroImgWrap）と、海外先行カードのまとめページ
 * （tcg_announce.html のイラスト）の両方から使う共通部品。通常の script として読み込み、window.CardZoom を公開する。
 *
 * - 開く: CardZoom.open(src, {watermark, referrerPolicy, label})
 * - 閉じる: 画像・背景・✕のタップ／Esc／ブラウザの「戻る」
 * - 履歴の扱いは効果テキストのシート（index-main.js の _openFxSheet）と同じ: 開くときに履歴を1つ足し、
 *   閉じるときに戻す。開け閉めで出た popstate は consumePop(e) が true を返すので、ページ側の popstate で
 *   先に呼んで、そのページの処理（カードページは検索のやり直し）に渡さない。自前の popstate が無いページは listen() を呼ぶ
 * - 画面に出す値は属性・textContent で設定する（innerHTML は固定の骨組みだけ）
 */
(function () {
  'use strict';

  var el = null, imgBox = null, img = null, closeBtn = null;
  var hasHistory = false, pendingBack = false, returnFocus = null;

  function build() {
    if (el) return el;
    el = document.createElement('div');
    el.className = 'card-zoom';
    el.hidden = true;
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-modal', 'true');
    el.innerHTML = '<button type="button" class="card-zoom-close" aria-label="閉じる">✕</button>' +
      '<div class="card-zoom-img"><img alt=""></div>';
    imgBox = el.querySelector('.card-zoom-img');
    img = imgBox.querySelector('img');
    closeBtn = el.querySelector('.card-zoom-close');
    // 拡大した画面のどこを押しても閉じる（見るだけの画面で、他に押すものが無いため）
    el.addEventListener('click', function () { close(); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && el && !el.hidden) close();
    });
    document.body.appendChild(el);
    return el;
  }

  function open(src, opts) {
    if (!src) return;
    opts = opts || {};
    build();
    img.referrerPolicy = opts.referrerPolicy || '';
    img.src = src;
    el.setAttribute('aria-label', opts.label || 'カード画像の拡大');
    // 発売済みの画像は、小さい表示と同じく自サイトの透かしを重ねる（self-watermark.css）
    imgBox.classList.toggle('wm-released', !!opts.watermark);
    if (el.hidden && history.pushState) {
      try { history.pushState({ cardZoom: 1 }, ''); hasHistory = true; } catch (_) {}
    }
    returnFocus = document.activeElement;
    el.hidden = false;
    document.documentElement.classList.add('card-zoom-open'); // 後ろのページがスクロールしないように
    closeBtn.focus({ preventScroll: true });
  }

  // fromPop: ブラウザの「戻る」で閉じたとき（履歴はもう戻っているので戻さない）
  function close(fromPop) {
    if (!el || el.hidden) return;
    el.hidden = true;
    document.documentElement.classList.remove('card-zoom-open');
    if (fromPop) { hasHistory = false; }
    else if (hasHistory) {
      hasHistory = false; pendingBack = true;
      try { history.back(); } catch (_) { pendingBack = false; }
    }
    if (returnFocus && returnFocus.focus) { try { returnFocus.focus({ preventScroll: true }); } catch (_) {} }
    returnFocus = null;
  }

  // 拡大の開け閉めで出た popstate なら true（ページ側の処理に渡さない）
  function consumePop(e) {
    if (pendingBack) { pendingBack = false; return true; }        // ✕・背景・Esc で閉じて履歴を戻した分
    if (hasHistory) { close(true); return true; }                 // 開いたままブラウザの「戻る」
    return !!(e && e.state && e.state.cardZoom);                  // 閉じた後の「進む」（拡大の履歴に戻っただけ）
  }

  function listen() {
    window.addEventListener('popstate', consumePop);
  }

  window.CardZoom = { open: open, close: close, consumePop: consumePop, listen: listen };

  // サーバーで描くページ（まとめページ）は、画像に data-card-zoom を付けるだけで拡大できる。
  // 自前の popstate が無いページは、読み込む script に data-listen を付けて履歴の受けもここで持つ
  var me = document.currentScript;
  if (me && me.hasAttribute('data-listen')) listen();
  document.addEventListener('click', function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    var z = t.closest('img[data-card-zoom]');
    if (!z) return;
    open(z.currentSrc || z.src, { referrerPolicy: z.referrerPolicy, label: z.getAttribute('data-card-zoom') || '' });
  });
})();
