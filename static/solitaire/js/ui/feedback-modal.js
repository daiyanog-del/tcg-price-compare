/**
 * feedback-modal.js — 不具合・要望送信モーダル
 * サイドバーの「不具合・要望を送る」ボタンで開くフォームを制御する。
 * 送信先: POST /api/feedback
 */

const API_FEEDBACK = '/api/feedback';
const MAX_BODY     = 2000;

export function initFeedbackModal() {
  const modal     = document.getElementById('feedbackModal');
  const openBtn   = document.getElementById('feedbackOpenBtn');
  if (!modal || !openBtn) return;

  const bodyEl    = document.getElementById('fbBody');
  const countEl   = document.getElementById('fbCount');
  const statusEl  = document.getElementById('fbStatus');
  const submitBtn = document.getElementById('fbSubmit');

  // ── 開閉 ──
  const open = () => {
    modal.style.display = 'flex';
    _resetStatus();
    bodyEl.focus();
  };
  const close = () => {
    modal.style.display = 'none';
  };

  openBtn.addEventListener('click', open);

  // data-fb-close を持つ要素（背景・× ・キャンセルボタン）で閉じる
  modal.querySelectorAll('[data-fb-close]').forEach(el => {
    el.addEventListener('click', close);
  });

  // Esc キーで閉じる
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && modal.style.display === 'flex') close();
  });

  // ── 文字数カウンタ ──
  bodyEl.addEventListener('input', () => {
    countEl.textContent = bodyEl.value.length;
  });

  // ── 送信 ──
  submitBtn.addEventListener('click', async () => {
    const kind    = document.getElementById('fbKind').value;
    const body    = bodyEl.value.trim();
    const contact = document.getElementById('fbContact').value.trim();

    if (!body) {
      _showStatus('本文を入力してください', false);
      return;
    }
    if (body.length > MAX_BODY) {
      _showStatus('本文が長すぎます（2000文字以内）', false);
      return;
    }

    submitBtn.disabled = true;
    _resetStatus();

    try {
      const res = await fetch(API_FEEDBACK, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ kind, body, contact, page: 'solitaire' }),
      });

      if (res.status === 429) {
        _showStatus('短時間に送りすぎです。少し待ってから再度お試しください', false);
        return;
      }

      const data = await res.json().catch(() => ({}));
      if (res.ok && data.ok) {
        _showStatus('送信しました。ありがとうございます！', true);
        // フォームをリセット
        bodyEl.value = '';
        countEl.textContent = '0';
        document.getElementById('fbContact').value = '';
        // 1.5秒後に閉じる
        setTimeout(close, 1500);
      } else {
        _showStatus(data.error || '送信に失敗しました', false);
      }
    } catch {
      _showStatus('通信エラーが発生しました', false);
    } finally {
      submitBtn.disabled = false;
    }
  });

  // ── 内部ヘルパー ──
  function _resetStatus() {
    statusEl.style.display  = 'none';
    statusEl.className      = 'sol-fb-status';
    statusEl.textContent    = '';
  }

  function _showStatus(msg, ok) {
    statusEl.textContent   = msg;
    statusEl.className     = 'sol-fb-status ' + (ok ? 'ok' : 'err');
    statusEl.style.display = 'block';
  }
}
