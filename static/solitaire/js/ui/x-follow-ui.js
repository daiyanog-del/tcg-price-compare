/* X（@GreeedyLab）のフォロー案内（2026-10-31）。
   ?newcards=1 で開いた（X の新カード投稿などから来た）ときだけ、PC は左サイドバー・スマホは自動で開くカード詳細の下に出す。
   フォローを押すか閉じたら、この端末では以後出さない。サイドバー下部の常設リンク（data-x-follow="sidebar"）は消さない。
   2026-10-23〜30 に X から約40回来ていたが、サイトのどこにもアカウントへの案内が無かった。 */
const DISMISS_KEY = 'sol_x_follow_dismissed';
const ON_CLASS = 'sol-x-follow-on';

function track(name, params) {
  try { if (typeof window.gtag === 'function') window.gtag('event', name, params || {}); } catch { /* 計測の失敗で止めない */ }
}

function isDismissed() {
  try { return localStorage.getItem(DISMISS_KEY) === '1'; } catch { return false; }
}

function dismiss() {
  try { localStorage.setItem(DISMISS_KEY, '1'); } catch { /* 保存できなくても今回は閉じる */ }
  document.body.classList.remove(ON_CLASS);
}

/** 新着カードを読み込めたときに呼ぶ。閉じたことがあれば何もしない。 */
export function showXFollowPrompt() {
  if (isDismissed()) return;
  document.body.classList.add(ON_CLASS);
  track('x_follow_shown');
}

export function initXFollow() {
  document.querySelectorAll('[data-x-follow]').forEach(a => {
    a.addEventListener('click', () => {
      const place = a.dataset.xFollow;
      track('x_follow_click', { place });
      if (place !== 'sidebar') dismiss();
    });
  });
  document.querySelectorAll('[data-x-follow-close]').forEach(btn => {
    btn.addEventListener('click', () => {
      track('x_follow_close');
      dismiss();
    });
  });
}
