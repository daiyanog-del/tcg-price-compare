/**
 * comment-view.js
 * コメント（吹き出し・盤面メモ）を盤面へ描く。計算は comment-model.js の computeCommentView（純粋関数）。
 * replay-service.js の _updateUI から、カーソルが変わるたびに呼ばれる（操作・取消・巻き戻し・再生・
 * セッション復元・共有リンクで開いたときのすべてで、記録の位置から表示を作り直す）。
 *
 * 本文はすべて textContent で入れる（共有リプレイの本文は他人が作れるため innerHTML には入れない）。
 */

import { computeCommentView } from './comment-model.js';

/** 重ねたカードの下側の吹き出しを、一番上のカードの位置に出すときの本文の頭に付ける印 */
export const UNDER_PREFIX = '下：';

/** 重ねて置かれるゾーン（エクシーズ素材など）。ここでは上のカードが下のカードの吹き出しを隠す */
const STACK_ZONE_SELECTOR = '.custom-slot, .side-slot';

let _lastView = { bubbles: [], memo: null };

/** カードの DOM（.tier-item-wrapper）を cardId から探す（replay-service の _findCardWrapper と同じ規則） */
function _findWrapper(cardId) {
  const el = document.getElementById(cardId);
  return el ? (el.closest('.tier-item-wrapper') ?? null) : null;
}

/**
 * 吹き出しの要素を作る。カードの中（右上）に置くので、カードと一緒に動き、ゾーンの overflow に切られない。
 * 外側が吹き出しの形（三角つき）、内側の span が最大2行の省略表示。
 * 全文はカード（.tier-item-wrapper とその画像）の title に入れる。吹き出し自体は pointer-events:none にして、
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

/**
 * どのカードの DOM に、どの本文で吹き出しを出すかを決める。
 * 重ねたゾーン（エクシーズ素材など）で、吹き出しの付いたカードが一番上でなければ、一番上のカードの位置に
 * 「下：」付きで出す。一番上のカード自身にも吹き出しがあれば、そちらを優先して下の分は出さない（一覧には残る）。
 * @returns {Map<Element, {cardId:string, text:string}>} 出す先のカード（wrapper）→ 吹き出し
 */
function _planBubbles(bubbles) {
  const direct = new Map();
  const under = new Map();
  for (const b of bubbles) {
    const wrapper = _findWrapper(b.cardId);
    if (!wrapper) continue;
    const zone = wrapper.parentElement;
    if (zone && zone.matches?.(STACK_ZONE_SELECTOR)) {
      const cards = zone.querySelectorAll(':scope > .tier-item-wrapper');
      const top = cards[cards.length - 1];
      if (top && top !== wrapper) {
        under.set(top, { cardId: b.cardId, text: UNDER_PREFIX + b.text }); // 後に付いたものが残る
        continue;
      }
    }
    direct.set(wrapper, { cardId: b.cardId, text: b.text });
  }
  const plan = new Map(direct);
  for (const [host, b] of under) if (!plan.has(host)) plan.set(host, b);
  return plan;
}

/** カードの title を、吹き出しの全文にする（裏向きのカードは画像側の title が優先されるので、画像にも入れる） */
function _setTitles(host, text) {
  host.title = text;
  const cardEl = host.querySelector('.tier-item');
  if (cardEl) cardEl.title = text;
}

/** 吹き出しが無くなったカードの title を元に戻す（裏向きなら card-state.js と同じくカード名） */
function _clearTitles(host) {
  host.removeAttribute('title');
  const cardEl = host.querySelector('.tier-item');
  if (cardEl) cardEl.title = host.dataset.face === 'down' ? (cardEl.dataset.cardName || '？') : '';
}

/** 吹き出しを描き直す（変わらないものは触らない） */
function _renderBubbles(bubbles) {
  const plan = _planBubbles(bubbles);

  // 要らなくなった・本文や出す先が変わった吹き出しを外す。カードごと消えた吹き出しも、ここで拾う
  document.querySelectorAll('.sol-bubble').forEach((el) => {
    const host = el.parentElement;
    const want = host ? plan.get(host) : null;
    const same = want && want.cardId === el.dataset.cardId && want.text === el.dataset.text;
    if (same) return;
    el.remove();
    if (host) {
      host.classList.remove('has-sol-bubble');
      _clearTitles(host);
    }
  });

  for (const [host, b] of plan) {
    if (!host.querySelector(':scope > .sol-bubble')) {
      host.appendChild(_buildBubble(b.cardId, b.text));
      host.classList.add('has-sol-bubble');
    }
    _setTitles(host, b.text); // 裏向きの切り替えなどで title が書き換わっても、描くたびに合わせ直す
  }
}

/** 盤面メモの帯（#solBoardMemo）を更新する。メモが無いときは帯ごと隠して場所を取らない */
function _renderMemo(memo) {
  const band = document.getElementById('solBoardMemo');
  if (!band) return;
  const textEl = document.getElementById('solBoardMemoText');
  const fullEl = document.getElementById('solBoardMemoFull');
  const wasHidden = band.hidden;
  const changed = (textEl ? textEl.textContent : '') !== (memo || '');
  if (memo) {
    if (textEl) textEl.textContent = memo;
    if (fullEl) fullEl.textContent = memo; // タップ・クリックで開く全文
    band.title = memo;
    band.hidden = false;
  } else {
    if (textEl) textEl.textContent = '';
    if (fullEl) fullEl.textContent = '';
    band.removeAttribute('title');
    band.hidden = true;
  }
  // メモが変わったら、開いていた全文は閉じる
  if (changed || !memo) {
    band.classList.remove('is-open');
    band.setAttribute('aria-expanded', 'false');
    if (fullEl) fullEl.hidden = true;
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
  _lastView = computeCommentView(setupLogs, logs, cursor);
  _renderBubbles(_lastView.bubbles);
  _renderMemo(_lastView.memo);
}

/**
 * いま（カーソル位置で）そのカードに付いている吹き出しの全文。無ければ null。
 * 重ねたカードの下側で、画面には出していない吹き出しも返す（スマホのカード詳細で全文を読むため）。
 * @param {string} cardId
 * @returns {string|null}
 */
export function getCurrentBubbleText(cardId) {
  const b = _lastView.bubbles.find((x) => x.cardId === cardId);
  return b ? b.text : null;
}
