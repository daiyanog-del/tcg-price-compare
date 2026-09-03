/**
 * utils/toast.js — 短時間表示のトースト通知
 *
 * main.js（エントリーポイント。?v付きURLで読み込まれる）から実装を分離した独立モジュール。
 * main.js を他モジュールから import すると、テンプレート側の `main.js?v=...` と
 * import 指定子の `main.js`（クエリ無し）が別URL＝別モジュールインスタンスとして扱われ、
 * main.js が二重評価されて initializeApp() が2回走るバグがあった（2026-09-03 実走で発見）。
 * トースト表示だけを独立モジュール化することで、main.js 側からも mobile-ui.js 側からも
 * 安全に同じ実装を共有できるようにする。
 */

export function showToast(msg) {
  const el = document.createElement('div');
  el.className = 'sol-toast';
  el.textContent = msg;
  document.body.appendChild(el);
  // 表示→フェードアウト
  requestAnimationFrame(() => {
    el.classList.add('sol-toast-visible');
    setTimeout(() => {
      el.classList.remove('sol-toast-visible');
      el.addEventListener('transitionend', () => el.remove(), { once: true });
    }, 2500);
  });
}
