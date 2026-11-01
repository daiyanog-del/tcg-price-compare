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
 * 吹き出しの要素を作る。外側が吹き出しの形（下向きの三角つき）、内側の span が数行までの省略表示。
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

// ── 吹き出しの置き場所（2026-11-01）──
// 吹き出しはカードの中ではなく、body 直下の層（#solBubbleLayer・position:fixed）に置いてカードの上に重ねる。
// カードの中に置くとカードの幅（PC で約60px）で数文字ごとに折り返して読みにくく、
// 墓地・除外・手札の枠は overflow で切るのでカードの外へ広げられないため。
// 位置はカードの実際の矩形からフレームごとに合わせる（移動・演出・盤面の伸縮・手札のスクロールのどれにも追いつく）。
// 置き場所が TRACK_IDLE_FRAMES 続けて変わらなければ止め、描き直し・画面の大きさ・スクロール・演出の終わり・
// 指やキーを離したときに再開する（吹き出しを出したまま盤面を眺めている間、ページを休ませるため）。吹き出しが無いときも止める。

/** 吹き出しの層。初めて使うときに作る */
let _layerEl = null;
/** 出している吹き出し: カード（wrapper）→ { el, cardId, text, clips, clipsParent, clipsW, size } */
const _placed = new Map();
let _trackRaf = 0;
/** 置き場所が変わらなかったフレームの数と、前のフレームの置き場所 */
let _idleFrames = 0;
let _lastSig = '';
/** これだけ続けて変わらなければ止める（約1.5秒。飛ぶ演出 450ms＋着地の確定より十分長く） */
const TRACK_IDLE_FRAMES = 90;
let _kickListening = false;

/** 位置合わせを（止まっていれば）再開する */
function _kickTracking() {
  _idleFrames = 0;
  if (!_trackRaf && _placed.size > 0) _trackRaf = requestAnimationFrame(_trackFrame);
}

function _listenKicks() {
  if (_kickListening) return;
  _kickListening = true;
  const opts = { capture: true, passive: true };
  window.addEventListener('resize', _kickTracking, opts);
  for (const type of ['scroll', 'transitionend', 'animationend', 'pointerup', 'touchend', 'keyup']) {
    document.addEventListener(type, _kickTracking, opts);
  }
  // 文字の形が読み込まれると吹き出しの大きさが変わるので測り直す
  document.fonts?.ready?.then(() => {
    for (const p of _placed.values()) p.size = null;
    _kickTracking();
  });
}

function _layer() {
  if (_layerEl?.isConnected) return _layerEl;
  _layerEl = document.createElement('div');
  _layerEl.id = 'solBubbleLayer';
  _layerEl.className = 'sol-bubble-layer';
  _layerEl.setAttribute('aria-hidden', 'true'); // 全文はカードの title・カード詳細で読める
  document.body.appendChild(_layerEl);
  return _layerEl;
}

/** 吹き出しを描き直す（変わらないものは触らない） */
function _renderBubbles(bubbles) {
  const plan = _planBubbles(bubbles);

  // 要らなくなった・本文や出す先が変わった吹き出しを外す。カードごと消えた吹き出しも、ここで拾う
  for (const [host, p] of _placed) {
    const want = plan.get(host);
    if (want && want.cardId === p.cardId && want.text === p.text) continue;
    p.el.remove();
    _placed.delete(host);
    if (host.isConnected) _clearTitles(host);
  }

  for (const [host, b] of plan) {
    if (!_placed.has(host)) {
      const el = _buildBubble(b.cardId, b.text);
      el.hidden = true; // 最初の位置合わせまで出さない
      _layer().appendChild(el);
      _placed.set(host, { el, cardId: b.cardId, text: b.text, clips: null, clipsParent: null, clipsW: 0, size: null });
    }
    _setTitles(host, b.text); // 裏向きの切り替えなどで title が書き換わっても、描くたびに合わせ直す
  }

  if (_placed.size > 0) {
    _listenKicks();
    _lastSig = _placeBubbles();
    _kickTracking();
  }
}

function _trackFrame() {
  _trackRaf = 0;
  if (_placed.size === 0) return;
  const sig = _placeBubbles();
  _idleFrames = sig === _lastSig ? _idleFrames + 1 : 0;
  _lastSig = sig;
  if (_idleFrames >= TRACK_IDLE_FRAMES) return;
  _trackRaf = requestAnimationFrame(_trackFrame);
}

/**
 * カードを切り取る祖先（overflow が visible でないもの）。カードが別のゾーンへ移ったときだけ調べ直す。
 * display:contents の要素は箱が無く切り取らない（縦向きスマホの #mainContainer。矩形が 0×0 になる）ので除く。
 */
function _clipAncestors(host) {
  const out = [];
  for (let e = host.parentElement; e && e !== document.body; e = e.parentElement) {
    const cs = getComputedStyle(e);
    if (cs.overflow !== 'visible' && cs.display !== 'contents') out.push(e);
  }
  return out;
}

/** カードが見えているか（演出で飛んでいる間は本体が visibility:hidden。外れたカード・畳まれたゾーンも隠す） */
function _isHostShown(host, r) {
  if (!host.isConnected || r.width < 2 || r.height < 2) return false;
  if (getComputedStyle(host).visibility === 'hidden') return false;
  const cardEl = host.querySelector('.tier-item');
  return !(cardEl && getComputedStyle(cardEl).visibility === 'hidden');
}

/** 幅のある（表示中の）要素の矩形。無ければ null */
function _shownRect(sel) {
  const r = document.querySelector(sel)?.getBoundingClientRect();
  return r && r.width > 0 && r.height > 0 ? r : null;
}

/**
 * 吹き出しを広げてよい範囲。盤面の入れ物（.sol-main）のうち、PC のカード詳細欄（.sol-cip-col・z10）の右から
 * 右パネル（.sol-right-panel）の左まで（どちらも層より下に描かれるので、かぶると文字を隠すため）。無ければ画面。
 */
function _bounds() {
  const main = _shownRect('.sol-main');
  let left = Math.max(0, main ? main.left : 0);
  let right = Math.min(window.innerWidth, main ? main.right : window.innerWidth);
  const cip = _shownRect('.sol-cip-col');
  if (cip && cip.right < right) left = Math.max(left, cip.right);
  const panel = _shownRect('.sol-right-panel');
  if (panel && panel.left > left) right = Math.min(right, panel.left);
  return { left, right, top: Math.max(0, main ? main.top : 0) };
}

function _setStyle(el, prop, value) {
  if (el.style.getPropertyValue(prop) !== value) el.style.setProperty(prop, value);
}

/** 三角の高さ（css/comments.css の .sol-bubble::after と同じ値） */
const BUBBLE_TIP_PX = 6;

/**
 * 吹き出しをカードの上に置く。カードの横の中心に合わせ、三角の先をカードの上端に少し重ねる。
 * 上に場所が無ければカードの上端に重ねる（三角なし）。盤面の左右からははみ出させない。
 * 先に置いた吹き出しと重なるときは、その上へずらす（隣り合うカードの吹き出しが互いに隠れないように）。
 * @returns {string} 置き場所の要約（前のフレームと同じなら、位置合わせを止めてよい）
 */
function _placeBubbles() {
  const bounds = _bounds();
  const items = [];
  for (const [host, p] of _placed) {
    const hostRect = host.getBoundingClientRect();
    let shown = _isHostShown(host, hostRect);
    // 置き場所は見えているカードの矩形で決める（守備表示はカード画像だけが横に回り、wrapper は縦長のまま）
    const r = shown ? (host.querySelector('.tier-item')?.getBoundingClientRect() ?? hostRect) : hostRect;
    if (shown) {
      if (p.clipsParent !== host.parentElement || p.clipsW !== window.innerWidth
        || p.clips.some((c) => !c.isConnected)) { // 別のゾーンへ移った・画面の幅や向きが変わった・枠が作り直された
        p.clips = _clipAncestors(host);
        p.clipsParent = host.parentElement;
        p.clipsW = window.innerWidth;
      }
      // スクロールの外・畳まれた枠の中にあるカードには出さない（カードの上端の中央が見えているかで判断）
      const cx = r.left + r.width / 2;
      const cy = r.top + Math.min(8, r.height / 2);
      shown = p.clips.every((c) => {
        const cr = c.getBoundingClientRect();
        return cx >= cr.left && cx <= cr.right && cy >= cr.top && cy <= cr.bottom;
      });
    }
    if (!shown) { p.el.hidden = true; continue; }
    items.push({ p, r });
  }
  // 上の段・左のカードから順に置く
  items.sort((a, b) => (a.r.top - b.r.top) || (a.r.left - b.r.left));

  const done = [];
  for (const { p, r } of items) {
    const el = p.el;
    // 幅の上限はカード幅に比例（小さい盤面では狭く）。文字の大きさは CSS（画面の幅で決まる）
    const maxW = Math.round(Math.min(240, Math.max(132, r.width * 2.4)));
    _setStyle(el, '--sol-bubble-max-w', `${maxW}px`);
    el.hidden = false;
    const key = `${maxW}|${window.innerWidth}`;
    if (!p.size || p.size.key !== key) p.size = { key, w: el.offsetWidth, h: el.offsetHeight };
    const { w, h } = p.size;

    const overlap = Math.min(10, r.height * 0.15); // 三角の先をカードに重ねる量
    const cardCx = r.left + r.width / 2;
    const left = Math.min(Math.max(bounds.left + 4, cardCx - w / 2), bounds.right - w - 4);
    let top = r.top + overlap - BUBBLE_TIP_PX - h;
    let inside = false;
    if (top < bounds.top + 2) { top = r.top + 4; inside = true; }
    for (let guard = 0; guard < 6; guard++) {
      const hit = done.find((d) => left < d.right && left + w > d.left && top < d.bottom && top + h > d.top);
      if (!hit) break;
      const up = hit.top - h - BUBBLE_TIP_PX - 2;
      if (up < bounds.top + 2) break; // 上に逃げられなければ重ねたままにする
      top = up;
    }
    done.push({ left, right: left + w, top, bottom: top + h + (inside ? 0 : BUBBLE_TIP_PX) });

    el.classList.toggle('is-inside', inside);
    const tailX = Math.min(Math.max(12, cardCx - left), w - 12);
    _setStyle(el, '--sol-bubble-tail-x', `${Math.round(tailX)}px`);
    _setStyle(el, 'transform', `translate(${Math.round(left)}px, ${Math.round(top)}px)`);
  }
  return [..._placed.values()].map((p) => (p.el.hidden ? '-' : p.el.style.transform)).join('|');
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
// 名前ごとに1回だけ問い合わせ、画像の本体も読み込み終えてから帯に入れる（帯の途中で絵が後から出ないように）。
// 「画像の無いカード」（プロキシ・HTTP エラー・画像が開けない）は覚えておき、ページを開き直すまで文字だけで出す
// （手を動かすたびに先読みが走るので、覚えないと同じ問い合わせを繰り返すため）。通信の失敗だけは次の機会に試し直す。
const _cardImageCache = new Map(); // カード名 -> Promise<{url:string, sample:boolean}|null>

/** 先読みする件数（いまの手より先の、相手の妨害のカットインのカード名の数）。共有リプレイを開いたとき全件を一度に引かない */
const CUTIN_PREFETCH_AHEAD = 4;

/**
 * 相手の妨害のカットインで、画像がそろうまで帯を出すのを待つ上限（ms）。
 * 帯は約1.9秒しか出ないので、文字だけで出してから絵が途中で現れる・間に合わない、を避けるため、絵がそろってから出す。
 * 本番で初回の取得は 0.2〜0.5 秒（2026-10-16 PC 実測）。回線が遅くても押してから反応が無い時間が長くならないよう 1 秒で打ち切り、
 * そのときは文字だけで出す（絵は届いた時点で入れる）。2回目以降はキャッシュ済みなので待たない。
 */
const CUTIN_IMAGE_WAIT_MS = 1000;

/**
 * 相手の妨害のカード画像を先に読み込んでおく（カットインの「相手の妨害」の欄を開いたときに、よく使う札の分を呼ぶ）。
 * @param {string} name
 */
export function prefetchCutinCard(name) {
  if (name) _cardImage(name);
}

/** 画像の本体を読み込み終えるまで待つ（開けなければ false） */
function _loadImage(url) {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => resolve(true);
    img.onerror = () => resolve(false);
    img.src = url;
  });
}

/**
 * カード名から画像を引く（取れなければ null。そのときは帯に文字だけを出す）。
 * @param {string} name
 * @returns {Promise<{url:string, sample:boolean}|null>}
 */
function _cardImage(name) {
  if (!_cardImageCache.has(name)) {
    const p = fetch(`/api/card-image?name=${encodeURIComponent(name)}`)
      .then(async (res) => {
        if (!res.ok) return null;
        const data = await res.json();
        const url = data && data.kind === 'image' ? (data.url || data.image_url) : null;
        if (!url) return null; // プロキシ（画像なし）は文字だけ
        return (await _loadImage(url)) ? { url, sample: data.source === 'official_sample' } : null;
      })
      .catch((err) => {
        console.warn('[cutin] カード画像の取得に失敗（文字だけで出す）', name, err);
        _cardImageCache.delete(name); // 通信の失敗は、次に出すとき・先読みのときに試し直す
        return null;
      });
    _cardImageCache.set(name, p);
  }
  return _cardImageCache.get(name);
}

/**
 * 相手の妨害のカットインの帯の中身を作る。
 * 背景にカードのイラストをぼかして暗く敷き、左にカード、右に「相手の妨害／カード名 発動」（見本:
 * mocks/sim-disruption-cutin-20261016.html の案S）。
 * 画像を入れる関数を返す（showCutin が、画像がそろってから帯を出すために使う）。
 * @returns {(img: {url:string, sample:boolean}) => void}
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

  return (img) => {
    // 未発売カードで「イラストをぼかす」設定のときは、帯いっぱいに拡大した背景の絵は出さない
    // （盤面のぼかしの強さはカード1枚の大きさで決めた値で、帯の幅では絵が見えてしまうため）。左のカードは .sample-art でぼかす
    const hideArt = img.sample && document.body.hasAttribute('data-sample-blur');
    if (!hideArt) artImg.style.backgroundImage = `url(${JSON.stringify(img.url)})`; // 引用符で囲んだ CSS の文字列として渡す
    el.classList.toggle('no-art', hideArt);
    const cardImg = document.createElement('img');
    cardImg.src = img.url;
    cardImg.alt = '';
    cardWrap.classList.toggle('sample-art', img.sample);
    cardWrap.appendChild(cardImg);
    el.classList.add('has-card-image');
  };
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
  el.style.top = `${b.top + b.height / 2}px`; // 帯の高さの半分だけ上へずらすのは CSS（translate）
  _cutinEl = el; // 画像の到着時に「まだ出ているか」を見るので、中身を作る前に控える
  // 子（カードの img など）の animationend も泡立って来るので、帯自身の動きの終わりだけを見る
  el.addEventListener('animationend', (e) => { if (e.target === el && _cutinEl === el) hideCutin(); });

  let started = false;
  const start = () => {
    if (started || _cutinEl !== el) return; // 待っている間に消された（次のカットイン・巻き戻し・画面サイズの変更）
    started = true;
    clearTimeout(_cutinTimer);
    document.body.appendChild(el); // ここで動き（css の solCutIn）が始まる
    // animationend が来ない環境（動きを止めている等）でも消えるように、保険のタイマーを置く
    _cutinTimer = setTimeout(() => { if (_cutinEl === el) hideCutin(); }, CUTIN_DURATION_MS + 300);
  };

  if (!card) {
    const span = document.createElement('span');
    span.className = 'sol-cutin-text';
    span.textContent = text; // 本文は他人が作れるので textContent
    el.appendChild(span);
    start();
    return;
  }

  // 相手の妨害: 画像がそろってから帯を出す（上限 CUTIN_IMAGE_WAIT_MS。過ぎたら文字だけで出し、絵は届いた時点で入れる）。
  // キャッシュ済みなら then はすぐ（同じ描画の前に）来るので、待ちは生じない
  const applyImage = _fillDisruptionCutin(el, card);
  _cutinTimer = setTimeout(start, CUTIN_IMAGE_WAIT_MS);
  _cardImage(card).then((img) => {
    if (_cutinEl !== el) return;
    if (img) applyImage(img);
    start();
  });
}

/**
 * カーソル位置の記録から、吹き出しと盤面メモを描く。
 * @param {Array} setupLogs
 * @param {Array} logs
 * @param {number} cursor
 */
export function renderCommentView(setupLogs, logs, cursor) {
  // 相手の妨害のカットインの画像を、いまの手より先の数件だけ先に引いておく（帯の動きに画像が間に合うように）
  for (const name of cutinCardsIn(logs, cursor + 1, CUTIN_PREFETCH_AHEAD)) _cardImage(name);
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
