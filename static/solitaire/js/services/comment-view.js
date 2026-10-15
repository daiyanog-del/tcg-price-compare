/**
 * comment-view.js
 * コメント（吹き出し・盤面メモ）を盤面へ描く。計算は comment-model.js の computeCommentView（純粋関数）。
 * replay-service.js の _updateUI から、カーソルが変わるたびに呼ばれる（操作・取消・巻き戻し・再生・
 * セッション復元・共有リンクで開いたときのすべてで、記録の位置から表示を作り直す）。
 *
 * 本文はすべて textContent で入れる（共有リプレイの本文は他人が作れるため innerHTML には入れない）。
 */

import { computeCommentView } from './comment-model.js';

/** カードの DOM（.tier-item-wrapper）を cardId から探す（replay-service の _findCardWrapper と同じ規則） */
function _findWrapper(cardId) {
  const el = document.getElementById(cardId);
  return el ? (el.closest('.tier-item-wrapper') ?? null) : null;
}

/**
 * 吹き出しの要素を作る。カードの中（右上）に置くので、カードと一緒に動き、ゾーンの overflow に切られない。
 * 外側が吹き出しの形（三角つき）、内側の span が最大2行の省略表示。
 * 全文はカード（.tier-item-wrapper）の title に入れる。吹き出し自体は pointer-events:none にして、
 * カードの移動・右クリック・ダブルクリックの邪魔をしない（そのためカードをポイントすると全文が出る）。
 */
function _buildBubble(cardId, text) {
  const el = document.createElement('div');
  el.className = 'sol-bubble';
  el.dataset.cardId = cardId;
  el.dataset.text = text;
  const inner = document.createElement('span');
  inner.className = 'sol-bubble-text';
  inner.textContent = text;
  el.appendChild(inner);
  return el;
}

/** 吹き出しを描き直す（変わらないものは触らない） */
function _renderBubbles(bubbles) {
  const want = new Map(bubbles.map((b) => [b.cardId, b.text]));

  // 要らなくなった・本文が変わった吹き出しを外す。カードごと消えた吹き出しも、ここで拾う
  document.querySelectorAll('.sol-bubble').forEach((el) => {
    const id = el.dataset.cardId;
    const wrapper = el.parentElement;
    const same = want.get(id) === el.dataset.text && wrapper && _findWrapper(id) === wrapper;
    if (same) return;
    el.remove();
    if (wrapper) {
      wrapper.classList.remove('has-sol-bubble');
      wrapper.removeAttribute('title');
    }
  });

  for (const [cardId, text] of want) {
    const wrapper = _findWrapper(cardId);
    if (!wrapper || wrapper.querySelector(':scope > .sol-bubble')) continue;
    wrapper.appendChild(_buildBubble(cardId, text));
    wrapper.classList.add('has-sol-bubble');
    wrapper.title = text;
  }
}

/** 盤面メモの帯（#solBoardMemo）を更新する。メモが無いときは帯ごと隠して場所を取らない */
function _renderMemo(memo) {
  const band = document.getElementById('solBoardMemo');
  if (!band) return;
  const textEl = document.getElementById('solBoardMemoText');
  const wasHidden = band.hidden;
  if (memo) {
    if (textEl) textEl.textContent = memo;
    band.title = memo;
    band.hidden = false;
  } else {
    if (textEl) textEl.textContent = '';
    band.removeAttribute('title');
    band.hidden = true;
  }
  // 帯が出入りすると盤面の使える高さが変わるので、カードの大きさを測り直させる（相手の想定妨害の開閉と同じ通知）
  if (band.hidden !== wasHidden) window.dispatchEvent(new Event('opp-tray-resize'));
}

/**
 * カーソル位置の記録から、吹き出しと盤面メモを描く。
 * @param {Array} setupLogs
 * @param {Array} logs
 * @param {number} cursor
 */
export function renderCommentView(setupLogs, logs, cursor) {
  const view = computeCommentView(setupLogs, logs, cursor);
  _renderBubbles(view.bubbles);
  _renderMemo(view.memo);
}
