/* X（@GreeedyLab）のフォロー案内（2026-10-31）。
   ?newcards=1 で開いた（X の新カード投稿などから来た）ときだけ、スマホ縦のカード詳細のいちばん下に1行で出す。
   シミュレーターは情報が多いので、1つの端末で出すのは1回だけ（出した時点で記録し、次からは出さない）。
   PC はサイドバー下部の常設リンク（data-x-follow="sidebar"）だけ。
   2026-10-23〜30 に X から約40回来ていたが、サイトのどこにもアカウントへの案内が無かった。 */
const SHOWN_KEY = 'sol_x_follow_shown';
// 10-31 の最初の版（× で閉じる箱）で閉じた人にも出さない
const OLD_DISMISS_KEY = 'sol_x_follow_dismissed';
const ON_CLASS = 'sol-x-follow-on';

function track(name, params) {
  try { if (typeof window.gtag === 'function') window.gtag('event', name, params || {}); } catch { /* 計測の失敗で止めない */ }
}

/** 新着カードを読み込めたときに呼ぶ。この端末で一度出していれば何もしない。 */
export function showXFollowPrompt() {
  try {
    if (localStorage.getItem(SHOWN_KEY) === '1' || localStorage.getItem(OLD_DISMISS_KEY) === '1') return;
    localStorage.setItem(SHOWN_KEY, '1');
  } catch { /* 保存できない端末では毎回出る（1行なので許容） */ }
  document.body.classList.add(ON_CLASS);
  track('x_follow_shown');
}

export function initXFollow() {
  document.querySelectorAll('[data-x-follow]').forEach(a => {
    a.addEventListener('click', () => track('x_follow_click', { place: a.dataset.xFollow }));
  });
}
