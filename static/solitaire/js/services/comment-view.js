/**
 * comment-view.js
 * コメント（吹き出し・盤面メモの付箋・カットイン）を盤面へ描く。計算は comment-model.js の computeCommentView（純粋関数）。
 * replay-service.js の _updateUI から、カーソルが変わるたびに呼ばれる（操作・取消・巻き戻し・再生・
 * セッション復元・共有リンクで開いたときのすべてで、記録の位置から表示を作り直す）。
 *
 * 本文はすべて textContent で入れる（共有リプレイの本文は他人が作れるため innerHTML には入れない）。
 */

import {
  computeCommentView, cutinCardsIn, CUTIN_DURATION_MS, CUTIN_DISRUPTION_LABEL, CUTIN_DISRUPTION_VERB,
} from './comment-model.js';

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

/**
 * 盤面メモの付箋（#solBoardMemo）を更新する。メモが無いときは隠す。
 * 付箋は盤面（.custom-layout）の空いた角に position:absolute で重ねるだけなので、出入りしても盤面の配置・大きさは
 * 変わらない（位置は css/comments.css。盤面の列の幅から calc で決める）。長い文は途中で省略し、押すと全文が開く
 * （開いた全文も同じ付箋が広がって重なるだけ）。
 */
function _renderMemo(memo) {
  const note = document.getElementById('solBoardMemo');
  if (!note) return;
  const textEl = document.getElementById('solBoardMemoText');
  const changed = (textEl ? textEl.textContent : '') !== (memo || '');
  if (memo) {
    if (textEl) textEl.textContent = memo;
    note.title = memo;
    note.hidden = false;
  } else {
    if (textEl) textEl.textContent = '';
    note.removeAttribute('title');
    note.hidden = true;
  }
  // メモが変わったら、開いていた全文は閉じる
  if (changed || !memo) {
    note.classList.remove('is-open');
    note.setAttribute('aria-expanded', 'false');
  }
}

// ── カットイン ─────────────────────────────────────────────

let _cutinEl = null;
let _cutinTimer = null;

/** 出ているカットインを消す（盤面には何も残らない） */
export function hideCutin() {
  clearTimeout(_cutinTimer);
  _cutinTimer = null;
  if (_cutinEl) { _cutinEl.remove(); _cutinEl = null; }
}

// 相手の妨害のカットインのカード画像。記録には URL を残さないので、カード名から /api/card-image で引き直す。
// 名前ごとに1回だけ問い合わせる。失敗（画像の無いカード・通信の失敗）も覚えておき、ページを開き直すまで文字だけで出す
// （手を動かすたびに先読みが走るので、失敗を覚えないと同じ問い合わせを繰り返すため）。
const _cardImageCache = new Map(); // カード名 -> Promise<{url:string, sample:boolean}|null>

/**
 * カード名から画像の URL を引く（取れなければ null。そのときは帯に文字だけを出す）。
 * @param {string} name
 * @returns {Promise<{url:string, sample:boolean}|null>}
 */
function _cardImage(name) {
  if (!_cardImageCache.has(name)) {
    const p = fetch(`/api/card-image?name=${encodeURIComponent(name)}`)
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`HTTP ${res.status}`))))
      .then((data) => {
        const url = data && data.kind === 'image' ? (data.url || data.image_url) : null;
        return url ? { url, sample: data.source === 'official_sample' } : null; // プロキシ（画像なし）は文字だけ
      })
      .catch((err) => {
        console.warn('[cutin] カード画像の取得に失敗（文字だけで出す）', name, err);
        return null;
      });
    _cardImageCache.set(name, p);
  }
  return _cardImageCache.get(name);
}

/**
 * 相手の妨害のカットインの帯の中身を作る。
 * 背景にカードのイラストをぼかして暗く敷き、左にカード、右に「相手の妨害／カード名 発動」（見本:
 * mocks/sim-disruption-cutin-20261016.html の案S）。画像は届いたら入れる（届く前は文字だけ）。
 */
function _fillDisruptionCutin(el, name) {
  el.classList.add('sol-cutin--card');
  const art = document.createElement('div');
  art.className = 'sol-cutin-art';
  const artImg = document.createElement('i');
  art.appendChild(artImg);
  // カードは img を包む枠に .sample-art を付ける（未発売 SAMPLE のぼかし設定を、盤面と同じ sample-blur.css で効かせる）
  const cardWrap = document.createElement('span');
  cardWrap.className = 'sol-cutin-card';
  cardWrap.dataset.cardName = name;
  const body = document.createElement('div');
  body.className = 'sol-cutin-body';
  const who = document.createElement('span');
  who.className = 'sol-cutin-who';
  who.textContent = CUTIN_DISRUPTION_LABEL;
  const line = document.createElement('span');
  line.className = 'sol-cutin-line';
  const nameEl = document.createElement('span');
  nameEl.className = 'sol-cutin-text';
  nameEl.textContent = name; // カード名も共有リプレイ由来なので textContent
  const verb = document.createElement('span');
  verb.className = 'sol-cutin-verb';
  verb.textContent = CUTIN_DISRUPTION_VERB;
  line.append(nameEl, verb);
  body.append(who, line);
  el.append(art, cardWrap, body);

  _cardImage(name).then((img) => {
    if (!img || _cutinEl !== el) return; // 取れない・もう消えた（次のカットインに替わった）
    artImg.style.backgroundImage = `url(${JSON.stringify(img.url)})`; // 引用符で囲んだ CSS の文字列として渡す
    const cardImg = document.createElement('img');
    cardImg.src = img.url;
    cardImg.alt = '';
    cardWrap.classList.toggle('sample-art', img.sample);
    cardWrap.appendChild(cardImg);
    el.classList.add('has-card-image');
  });
}

/**
 * カットインを出す。出ているものがあれば消して、新しいものを出す。約1.9秒の動き（css/comments.css の cutIn）で
 * 入って抜け、終わったら要素ごと消える。操作の邪魔をしない（pointer-events:none）。
 * 置き場所は、出す時点の盤面の実際の要素の位置から決める:
 *   - 横: 盤面の入れ物（#mainContainer。縦向きスマホは .sol-field-area）の幅いっぱい（モックどおり、カード詳細欄の下にも薄く伸びる）
 *   - 縦: 盤面（.custom-layout）の中央の高さ（PC は3段の真ん中、スマホ縦向きは真ん中の段の高さ）
 * 画面に固定（position:fixed）で body 直下に置くので、盤面の配置には影響しない。
 * @param {{text:string, card?:string|null}} cutin  card があれば相手の妨害のカットイン（text はカード名）
 */
export function showCutin({ text, card } = {}) {
  hideCutin();
  const board = document.querySelector('.custom-layout');
  if (!board || !text) return;
  // 横の基準: 盤面の入れ物（#mainContainer）。縦向きスマホでは display:contents で矩形を持たないので .sol-field-area
  let area = document.getElementById('mainContainer');
  if (!area || area.getBoundingClientRect().width === 0) area = document.querySelector('.sol-field-area') || board;
  const b = board.getBoundingClientRect();
  const a = area.getBoundingClientRect();
  const el = document.createElement('div');
  el.className = 'sol-cutin';
  el.setAttribute('aria-hidden', 'true'); // 記録は一覧に残る。スクリーンリーダーには読ませない
  el.style.animationDuration = `${CUTIN_DURATION_MS}ms`; // 長さの正本は JS 側（CSS の 1.9s は既定値）
  el.style.left = `${a.left}px`;
  el.style.width = `${a.width}px`;
  el.style.top = `${b.top + b.height / 2}px`; // 帯の高さの半分だけ上へずらすのは CSS（margin-top）
  _cutinEl = el; // 画像の到着時に「まだ出ているか」を見るので、中身を作る前に控える
  if (card) {
    _fillDisruptionCutin(el, card);
  } else {
    const span = document.createElement('span');
    span.className = 'sol-cutin-text';
    span.textContent = text; // 本文は他人が作れるので textContent
    el.appendChild(span);
  }
  // 子（カードの img など）の animationend も泡立って来るので、帯自身の動きの終わりだけを見る
  el.addEventListener('animationend', (e) => { if (e.target === el && _cutinEl === el) hideCutin(); });
  document.body.appendChild(el);
  // animationend が来ない環境（動きを止めている等）でも消えるように、保険のタイマーを置く
  _cutinTimer = setTimeout(() => { if (_cutinEl === el) hideCutin(); }, CUTIN_DURATION_MS + 300);
}

/**
 * カーソル位置の記録から、吹き出しと盤面メモを描く。
 * @param {Array} setupLogs
 * @param {Array} logs
 * @param {number} cursor
 */
export function renderCommentView(setupLogs, logs, cursor) {
  // 相手の妨害のカットインの画像を先に引いておく（共有リンクを開いた直後でも、帯の動きに画像が間に合うように）
  for (const name of cutinCardsIn(logs)) _cardImage(name);
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
