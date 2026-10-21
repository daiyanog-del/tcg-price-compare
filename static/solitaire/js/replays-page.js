/**
 * replays-page.js
 * リプレイ一覧ページ（/solitaire/replays）の描画。
 *
 * このブラウザ（localStorage）にあるリプレイだけを扱う。
 *   1. 端末に保存したリプレイ（services/saved-replays.js）
 *   2. 共有リンクを作ったリプレイの履歴（services/shared-replays.js）。公開中のものは「公開をやめる」で取り下げられる
 * 加えて、一番上に「みんなのリプレイ」（公開されたリプレイ。GET /api/solitaire/public）を並べる。
 * シミュレーター本体（main.js 等）は読み込まない。
 * 起動時に、廃止した盤面セーブ（スロット）の移行（services/slot-migration.js）と、
 * 「ファイルから開く」（書き出した JSON を端末保存へ登録してシミュレーターで開く）も受け持つ。
 * ユーザー由来の文字列（タイトル等）は必ず textContent で入れる（innerHTML には入れない）。
 */

import { savedReplaysGet, savedReplaysSet, deleteReplayEntry, assertReplayPayload, saveReplayPayload } from './services/saved-replays.js';
import { migrateSlotsToReplays, getRemainingSlotKeys, exportRemainingSlotsJson } from './services/slot-migration.js';
import { sharedReplaysGet, sharedReplaysSet, deleteSharedReplay, unpublishSharedReplay } from './services/shared-replays.js';
import { shouldConfirmOpen, copyResumeToUndo, UNDO_OFFER_KEY, NOTICE_KEY } from './utils/session-resume-store.js';
import { showActionToast, openConfirm } from './ui/inline-ui.js';

/**
 * 行リンクを開くと、シミュレーターに退避中の記録が置き換わる場合の準備。
 * 以前は確認窓を出していたが、今は退避を「元に戻す」用に写しておき、開いた先（シミュレーター）で6秒間「元に戻す」を出す
 * （ui/undo-point.js consumeStartupOffers）。新しいタブで開く操作（修飾キー付き）は今のページが残るので何もしない。
 */
function confirmReplaceSessionRecord(ev) {
  askReplaceSessionRecord(ev);
}

/** 置き換わるなら写しを取って、開いた先で「元に戻す」を出す印を付ける。常に true（開いてよい） */
function askReplaceSessionRecord(ev) {
  let r;
  try { r = shouldConfirmOpen(ev, sessionStorage); } catch (e) { return true; }
  if (!r.ask) return true;
  try {
    if (copyResumeToUndo(sessionStorage)) {
      // 0手（setup ログだけ）のときは手数を出さない
      const what = r.steps > 0 ? `前の記録（${r.steps}手）` : '前の記録';
      sessionStorage.setItem(UNDO_OFFER_KEY, `リプレイを開きました（${what}に戻せます）`);
    }
  } catch (e) { /* 元に戻せないだけで、開くのは止めない */ }
  return true;
}

/**
 * 一覧から1件消して、6秒間「元に戻す」を出す（以前は確認窓）。元に戻すと同じ位置へ戻す。
 * @param {Function} getList  一覧を読む関数
 * @param {Function} setList  一覧を書く関数
 * @param {Function} remove   1件消す関数（id を受ける）
 * @param {Object} item
 * @param {Function} rerender 描き直す関数
 * @param {string} msg
 */
function removeWithUndo(getList, setList, remove, item, rerender, msg) {
  const before = getList();
  const idx = before.findIndex(x => x.id === item.id);
  // 別のタブで先に消えていた: 描き直すだけ（元に戻すものが無い）
  if (idx < 0) { rerender(); return; }
  const entry = before[idx];
  remove(item.id);
  rerender();
  showActionToast(msg, {
    label: '元に戻す',
    onClick: () => {
      const cur = getList();
      if (cur.some(x => x.id === item.id)) return;
      cur.splice(Math.min(idx, cur.length), 0, entry);
      const res = setList(cur);
      // 保存の失敗（容量超過など）は false または { ok:false } で返る
      if (res === false || (res && res.ok === false)) { showActionToast('元に戻せませんでした（ブラウザの保存容量を確認してください）', { error: true }); return; }
      rerender();
    },
  });
}

/** 日時を「10/15 13:05」形式にする（不明なら空文字） */
function formatDateTime(ms) {
  if (!ms) return '';
  return new Date(ms).toLocaleString('ja-JP', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  });
}

/** 要素を作る小ヘルパー（テキストは textContent） */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** 行のボタンを作る */
function actionButton(label, className, onClick) {
  const btn = el('button', 'rp-btn' + (className ? ' ' + className : ''), label);
  btn.type = 'button';
  btn.addEventListener('click', onClick);
  return btn;
}

/**
 * シミュレーターで開くリンクを、行のボタンとして作る（本物のリンクなので新しいタブでも開ける）。
 * 開くと今の記録が置き換わる場合は、行全体のリンクと同じ確認を出す。
 */
function openLinkButton(label, href) {
  const a = el('a', 'rp-btn rp-btn-open', label);
  a.href = href;
  a.addEventListener('click', confirmReplaceSessionRecord);
  return a;
}

/** クリップボードへコピーする（失敗時は execCommand にフォールバック）。成功なら true */
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { ok = false; }
    ta.remove();
    return ok;
  }
}

// ── 1. 端末に保存したリプレイ ──

// デッキで絞り込む（null＝すべて／''＝デッキなし／それ以外＝そのデッキID）。ページを開き直したら「すべて」
let _deckFilter = null;

/** マイデッキの名前 {デッキID: 名前}（このブラウザの保存済みデッキ。消したデッキのリプレイは「デッキなし」） */
function savedDeckNames() {
  try {
    const l = JSON.parse(localStorage.getItem('cardprice_saved_decks'));
    const out = new Map();
    if (Array.isArray(l)) for (const d of l) if (d && typeof d.id === 'string') out.set(d.id, d.name || 'マイデッキ');
    return out;
  } catch (e) {
    return new Map();
  }
}

/** 絞り込みのボタン（デッキ付きのリプレイが1件も無ければ出さない） */
function renderDeckFilter(all, names) {
  const box = document.getElementById('rpDeckFilter');
  if (!box) return;
  const ids = [...new Set(all.map(r => r.deckId).filter(id => id && names.has(id)))];
  box.textContent = '';
  box.hidden = ids.length === 0;
  if (!ids.length) { _deckFilter = null; return; }
  const opts = [[null, 'すべて'], ...ids.map(id => [id, names.get(id)])];
  if (all.some(r => !r.deckId || !names.has(r.deckId))) opts.push(['', 'デッキなし']);
  // 選んでいたもの（デッキなしを含む）が無くなったら「すべて」に戻す
  if (_deckFilter !== null && !opts.some(([id]) => id === _deckFilter)) _deckFilter = null;
  for (const [id, label] of opts) {
    const b = el('button', 'rp-btn rp-deck-filter-btn' + (id === _deckFilter ? ' is-active' : ''), label);
    b.type = 'button';
    b.setAttribute('aria-pressed', id === _deckFilter ? 'true' : 'false');
    b.addEventListener('click', () => { _deckFilter = id; renderSaved(); });
    box.appendChild(b);
  }
}

function renderSaved() {
  const listEl = document.getElementById('rpSavedList');
  const emptyEl = document.getElementById('rpSavedEmpty');
  if (!listEl) return;
  listEl.textContent = '';

  // 新しい順
  const all = savedReplaysGet().slice().sort((a, b) => (b.savedAt || 0) - (a.savedAt || 0));
  if (emptyEl) emptyEl.hidden = all.length > 0;
  const names = savedDeckNames();
  renderDeckFilter(all, names);
  const deckOf = (r) => (r.deckId && names.has(r.deckId) ? r.deckId : '');
  const items = _deckFilter === null ? all : all.filter(r => deckOf(r) === _deckFilter);

  for (const item of items) {
    const li = el('li', 'rp-row');

    // 行を押すとシミュレーターで開く（本物のリンクにして、新しいタブで開く等も使えるようにする）
    const openHref = '/solitaire?saved=' + encodeURIComponent(item.id);
    const link = el('a', 'rp-row-link');
    link.href = openHref;
    link.addEventListener('click', confirmReplaceSessionRecord);
    const info = el('span', 'rp-row-info');
    const titleLine = el('span', 'rp-row-titleline');
    titleLine.appendChild(el('span', 'rp-row-title', item.title || '無題'));
    // どのマイデッキで撮ったか（shared/deck-replays.js の deckId）。題名の外に置く（長い題名の省略で札ごと消えないように）
    if (deckOf(item)) titleLine.appendChild(el('span', 'rp-deck-tag', names.get(item.deckId)));
    info.appendChild(titleLine);
    const date = formatDateTime(item.savedAt);
    info.appendChild(el('span', 'rp-row-meta',
      `${date ? date + '・' : ''}${item.steps ?? 0}手・メイン${item.main ?? 0}枚・EX${item.ex ?? 0}枚`));
    link.appendChild(info);
    li.appendChild(link);

    const actions = el('div', 'rp-row-actions');
    actions.appendChild(openLinkButton('最初から再生', openHref));
    actions.appendChild(openLinkButton('最後の盤面から続ける', openHref + '&at=end'));
    actions.appendChild(actionButton('削除', 'rp-btn-danger', () => {
      removeWithUndo(savedReplaysGet, savedReplaysSet, deleteReplayEntry, item, renderSaved, `「${item.title || '無題'}」を削除しました`);
    }));
    li.appendChild(actions);
    listEl.appendChild(li);
  }
}

// ── 2. 共有リンクを作ったリプレイ ──

function renderShared() {
  const listEl = document.getElementById('rpSharedList');
  const emptyEl = document.getElementById('rpSharedEmpty');
  if (!listEl) return;
  listEl.textContent = '';

  const items = sharedReplaysGet(); // 新しい順に保存されている
  if (emptyEl) emptyEl.hidden = items.length > 0;

  for (const item of items) {
    const li = el('li', 'rp-row');
    const idParam = encodeURIComponent(item.id);

    const openHref = '/solitaire?replay=' + idParam;
    const link = el('a', 'rp-row-link');
    link.href = openHref;
    link.addEventListener('click', confirmReplaceSessionRecord);

    const thumb = document.createElement('img');
    thumb.className = 'rp-thumb';
    thumb.src = '/solitaire/og/' + idParam + '.png';
    thumb.alt = '';
    thumb.loading = 'lazy';
    thumb.width = 120;
    thumb.height = 63;
    // 画像が取れないときは、壊れた画像アイコンを出さずに隠す
    thumb.addEventListener('error', () => { thumb.hidden = true; });
    link.appendChild(thumb);

    const info = el('span', 'rp-row-info');
    const titleLine = el('span', 'rp-row-titleline');
    titleLine.appendChild(el('span', 'rp-row-title', item.title || '無題'));
    if (item.public && item.deleteToken) titleLine.appendChild(el('span', 'rp-badge-public', '公開中'));
    info.appendChild(titleLine);
    const date = formatDateTime(item.createdAt);
    info.appendChild(el('span', 'rp-row-meta', `${date ? date + '・' : ''}${item.steps ?? 0}手`));
    link.appendChild(info);
    li.appendChild(link);

    const actions = el('div', 'rp-row-actions');
    actions.appendChild(openLinkButton('最初から再生', openHref));
    actions.appendChild(openLinkButton('最後の盤面から続ける', openHref + '&at=end'));
    const copyBtn = actionButton('リンクをコピー', '', async () => {
      const ok = await copyText(location.origin + '/solitaire?replay=' + idParam);
      copyBtn.textContent = ok ? 'コピーしました' : 'コピーできませんでした';
      setTimeout(() => { copyBtn.textContent = 'リンクをコピー'; }, 2000);
    });
    actions.appendChild(copyBtn);
    if (item.public && item.deleteToken) {
      // 赤は文字色だけ（取り消せない操作ではないので枠・背景は付けない）
      const unpubBtn = actionButton('公開をやめる', 'rp-btn-unpublish', () => { unpublishItem(item, unpubBtn); });
      actions.appendChild(unpubBtn);
    }
    const forgetBtn = actionButton('履歴から消す', 'rp-btn-danger', () => {
      const isPublic = item.public && item.deleteToken;
      if (isPublic) {
        // 公開中は、消すとこのブラウザから公開をやめられなくなる（鍵を失う）。元に戻せても見落としやすいので、その場で確かめる
        openConfirm(forgetBtn, {
          message: `「${item.title || '無題'}」は公開中です。履歴から消すと、このブラウザから公開をやめられなくなります（先に「公開をやめる」を押してください）。`,
          okLabel: 'それでも消す',
          danger: true,
          onOk: () => removeWithUndo(sharedReplaysGet, sharedReplaysSet, deleteSharedReplay, item, renderShared, `「${item.title || '無題'}」を履歴から消しました`),
        });
        return;
      }
      removeWithUndo(sharedReplaysGet, sharedReplaysSet, deleteSharedReplay, item, renderShared,
        `「${item.title || '無題'}」を履歴から消しました（共有リンクは作成から90日で消えます）`);
    });
    actions.appendChild(forgetBtn);
    li.appendChild(actions);
    listEl.appendChild(li);
  }
}

/** 共有履歴の下のエラー表示（空文字で消す） */
function showSharedError(message) {
  const errEl = document.getElementById('rpSharedError');
  if (!errEl) return;
  errEl.textContent = message;
  errEl.hidden = !message;
}

/**
 * 公開をやめる。確認のあと、公開時にこのブラウザへ控えた鍵で取り下げ API を呼び、成功したら「公開中」の札を外す。
 * 403 は「鍵が合わない・すでに公開されていない（運営の削除を含む）」なので、控えを外して状況を伝える。
 */
function unpublishItem(item, anchor) {
  // サーバー側の操作で、元に戻す手段が無い（もう一度公開するには共有し直す）ので、ページ内の小窓で確かめる（以前は確認窓）
  openConfirm(anchor || null, {
    message: `「${item.title || '無題'}」の公開をやめます。みんなのリプレイの一覧から外れ、共有リンクは作成から90日で消えます。`,
    okLabel: '公開をやめる',
    danger: true,
    onOk: () => { doUnpublish(item); },
  });
}

async function doUnpublish(item) {
  showSharedError('');
  try {
    const res = await fetch('/api/solitaire/replay/' + encodeURIComponent(item.id) + '/unpublish', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: item.deleteToken }),
    });
    if (res.ok) {
      unpublishSharedReplay(item.id);
      renderShared();
      loadPublic(true); // 一覧から外れたことを上の区画にも反映する
      return;
    }
    const data = await res.json().catch(() => ({}));
    // 鍵を捨てるのは、サーバーが「取り下げできない（鍵違い・すでに非公開・運営の削除）」と目印を付けたときだけ。
    // プロキシ等の別の 403 では、鍵を残して後で再試行できるようにする
    if (res.status === 403 && data.code === 'not_unpublishable') {
      unpublishSharedReplay(item.id);
      renderShared();
      showSharedError('このリプレイはすでに公開されていないようです。表示を更新しました。');
      return;
    }
    showSharedError(res.status === 403
      ? '通信に失敗しました。時間をおいて試してください'
      : (data.error || '公開をやめられませんでした。しばらくしてからもう一度お試しください'));
  } catch {
    showSharedError('通信に失敗しました。しばらくしてからもう一度お試しください');
  }
}

// ── 0. みんなのリプレイ（公開されたリプレイ） ──

const PUBLIC_PAGE_SIZE = 24;
const PUBLIC_TAGS_SHOWN = 4;
let publicCard = '';        // 絞り込み中のカード名（空なら絞り込みなし）
let publicNext = null;      // 続きを読むときの before（無ければ null）
let publicReqSeq = 0;       // 古い応答が新しい表示を上書きしないための通し番号

/** 公開リプレイのカード1枚を作る。ユーザー由来の文字列（タイトル・カード名）は textContent だけで入れる */
function buildPublicCard(item) {
  const li = el('li', 'rp-pcard');
  const idParam = encodeURIComponent(item.id);
  const openHref = '/solitaire?replay=' + idParam;

  // カード全体を押せるように、主リンクの当たりを CSS（::after）でカード全体に広げる
  const link = el('a', 'rp-pcard-link');
  link.href = openHref;
  link.addEventListener('click', confirmReplaceSessionRecord);

  const thumb = document.createElement('img');
  thumb.className = 'rp-pcard-thumb';
  thumb.src = '/solitaire/og/' + idParam + '.png';
  thumb.alt = '';
  thumb.loading = 'lazy';
  thumb.width = 1200;
  thumb.height = 630;
  thumb.addEventListener('error', () => { thumb.style.visibility = 'hidden'; });
  link.appendChild(thumb);
  link.appendChild(el('span', 'rp-pcard-title', item.title || '無題'));
  li.appendChild(link);

  const names = Array.isArray(item.card_names) ? item.card_names : [];
  const total = Number.isFinite(item.card_count) ? item.card_count : names.length;
  if (names.length) {
    const tags = el('span', 'rp-pcard-tags');
    for (const name of names.slice(0, PUBLIC_TAGS_SHOWN)) tags.appendChild(el('span', 'rp-tag', name));
    if (total > PUBLIC_TAGS_SHOWN) tags.appendChild(el('span', 'rp-tag', `他${total - PUBLIC_TAGS_SHOWN}`));
    li.appendChild(tags);
  }

  const date = formatDateTime(Date.parse(item.published_at));
  li.appendChild(el('span', 'rp-pcard-meta', `${item.steps ?? 0}手${date ? '・' + date : ''}`));

  const cont = el('a', 'rp-pcard-continue', '最後の盤面から続ける');
  cont.href = openHref + '&at=end';
  cont.addEventListener('click', confirmReplaceSessionRecord);
  li.appendChild(cont);
  return li;
}

function showPublicError(message) {
  const errEl = document.getElementById('rpPublicError');
  if (!errEl) return;
  errEl.textContent = message;
  errEl.hidden = !message;
}

/**
 * 公開リプレイを読み込む。reset=true なら先頭から読み直し（絞り込み変更・取り下げ後）、
 * false なら続き（「もっと見る」）を末尾に足す。
 */
async function loadPublic(reset) {
  const listEl = document.getElementById('rpPublicList');
  const moreBtn = document.getElementById('rpPublicMore');
  const emptyEl = document.getElementById('rpPublicEmpty');
  if (!listEl) return;
  const seq = ++publicReqSeq;
  if (reset) { publicNext = null; }
  const params = new URLSearchParams({ limit: String(PUBLIC_PAGE_SIZE) });
  if (publicCard) params.set('card', publicCard);
  if (!reset && publicNext) params.set('before', publicNext);
  showPublicError('');
  if (moreBtn) moreBtn.disabled = true;
  try {
    const res = await fetch('/api/solitaire/public?' + params.toString());
    if (seq !== publicReqSeq) return; // 後から出した読み込みがあるので、この応答は使わない
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    const data = await res.json();
    if (reset) listEl.textContent = '';
    for (const item of data.items || []) listEl.appendChild(buildPublicCard(item));
    publicNext = data.next || null;
    if (moreBtn) moreBtn.hidden = !publicNext;
    const empty = listEl.children.length === 0;
    if (emptyEl) {
      emptyEl.hidden = !empty;
      emptyEl.textContent = publicCard
        ? `「${publicCard}」を使った公開リプレイはまだありません。`
        : 'まだ公開されたリプレイはありません。シミュレーターの共有で「みんなのリプレイに公開する」を選ぶと、ここに並びます';
    }
  } catch (err) {
    if (seq !== publicReqSeq) return;
    showPublicError('公開リプレイを読み込めませんでした。しばらくしてからもう一度お試しください');
    if (reset) { listEl.textContent = ''; if (emptyEl) emptyEl.hidden = true; if (moreBtn) moreBtn.hidden = true; }
  } finally {
    if (seq === publicReqSeq && moreBtn) moreBtn.disabled = false;
  }
}

document.getElementById('rpPublicMore')?.addEventListener('click', () => { loadPublic(false); });

// ── 0-b. カード名の絞り込み（入力補完は既存の /api/suggest。返り値は文字列の配列） ──

const SUGGEST_DEBOUNCE_MS = 200;
const SUGGEST_MAX = 8;
let suggestTimer = null;
let suggestAbort = null;
let suggestActive = -1; // 候補のうち、キーボードで選んでいる位置

function suggestEls() {
  return {
    input: document.getElementById('rpPublicCard'),
    list: document.getElementById('rpPublicSuggest'),
    clear: document.getElementById('rpPublicCardClear'),
  };
}

function closeSuggest() {
  const { input, list } = suggestEls();
  if (!list) return;
  list.hidden = true;
  list.textContent = '';
  suggestActive = -1;
  input?.setAttribute('aria-expanded', 'false');
}

/** 絞り込みを決める（name が空なら解除）。入力欄・✕ボタン・一覧を合わせる */
function applyCardFilter(name) {
  const { input, clear } = suggestEls();
  publicCard = name;
  if (input) input.value = name;
  if (clear) clear.hidden = !name;
  closeSuggest();
  loadPublic(true);
}

function renderSuggest(names) {
  const { input, list } = suggestEls();
  if (!list) return;
  list.textContent = '';
  suggestActive = -1;
  if (!names.length) { closeSuggest(); return; }
  for (const name of names) {
    const li = el('li', 'rp-suggest-item', name);
    li.setAttribute('role', 'option');
    // blur より先に処理する（mousedown）。タッチでも mousedown が合成される
    li.addEventListener('mousedown', (e) => { e.preventDefault(); applyCardFilter(name); });
    list.appendChild(li);
  }
  list.hidden = false;
  input?.setAttribute('aria-expanded', 'true');
}

async function fetchSuggest(q) {
  if (suggestAbort) suggestAbort.abort();
  suggestAbort = new AbortController();
  try {
    const res = await fetch('/api/suggest?q=' + encodeURIComponent(q), { signal: suggestAbort.signal });
    if (!res.ok) return;
    const data = await res.json();
    renderSuggest((Array.isArray(data) ? data : []).filter(n => typeof n === 'string').slice(0, SUGGEST_MAX));
  } catch {
    // 中断・通信失敗のときは候補を出さないだけ（絞り込み自体は入力した名前のまま Enter で決められる）
  }
}

function setSuggestActive(idx) {
  const { list } = suggestEls();
  const items = list ? [...list.children] : [];
  items.forEach((li, i) => li.classList.toggle('is-active', i === idx));
  suggestActive = idx;
}

function initCardFilter() {
  const { input, list, clear } = suggestEls();
  if (!input || !list) return;
  input.addEventListener('input', () => {
    clearTimeout(suggestTimer);
    const q = input.value.trim();
    if (!q) { closeSuggest(); return; }
    suggestTimer = setTimeout(() => fetchSuggest(q), SUGGEST_DEBOUNCE_MS);
  });
  input.addEventListener('keydown', (e) => {
    // 日本語入力の変換確定の Enter では決定しない
    if (e.isComposing || e.keyCode === 229) return;
    const n = list.children.length;
    if (e.key === 'ArrowDown' && n) { e.preventDefault(); setSuggestActive((suggestActive + 1) % n); }
    else if (e.key === 'ArrowUp' && n) { e.preventDefault(); setSuggestActive((suggestActive - 1 + n) % n); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      clearTimeout(suggestTimer);
      const picked = suggestActive >= 0 ? list.children[suggestActive]?.textContent : input.value.trim();
      if (picked) applyCardFilter(picked);
    } else if (e.key === 'Escape') {
      closeSuggest();
    }
  });
  // フォーカスが外れたら候補を閉じる（候補は mousedown で先に処理される）
  input.addEventListener('blur', () => { setTimeout(closeSuggest, 100); });
  clear?.addEventListener('click', () => { applyCardFilter(''); input.focus(); });
}

// ── 3. ファイルから開く ──

const SAVE_FAIL_MESSAGES = {
  too_large: 'このリプレイは大きすぎて保存できません',
  quota: '保存できませんでした（端末の空き容量）',
  no_lzstring: '圧縮ライブラリを読み込めませんでした。ページを再読み込みしてください',
};

function showOpenError(message) {
  const errEl = document.getElementById('rpOpenError');
  if (!errEl) return;
  errEl.textContent = message;
  errEl.hidden = !message;
}

/**
 * 書き出した JSON（シミュレーターの「JSON保存」）を選んで、端末保存のリプレイとして登録し、
 * シミュレーターで開く。読めないファイルはその場でエラーを表示して、何も登録しない。
 */
async function handleOpenFile(e) {
  const input = e.target;
  const file = input.files?.[0];
  if (!file) return;
  showOpenError('');
  try {
    let payload;
    try {
      payload = JSON.parse(await file.text());
    } catch {
      throw new Error('JSON として読めませんでした');
    }
    assertReplayPayload(payload);
    const title = (typeof payload.title === 'string' && payload.title.trim())
      ? payload.title : file.name.replace(/\.json$/i, '');
    const saved = saveReplayPayload(payload, { title });
    if (!saved.ok) throw new Error(SAVE_FAIL_MESSAGES[saved.reason] || '保存に失敗しました');
    // 保存できてから、今の記録が置き換わる場合の「元に戻す」の準備をする（失敗したら何も残さない）
    askReplaceSessionRecord({ button: 0 });
    // 上限20件の整理で古い保存が消えたときは、移った先（シミュレーター）で知らせる（以前は移る前に知らせの窓を出していた）
    if (saved.removedCount > 0) {
      try { sessionStorage.setItem(NOTICE_KEY, `保存の上限のため、古い${saved.removedCount}件を削除しました`); } catch (e) { /* 知らせられないだけ */ }
    }
    location.href = '/solitaire?saved=' + encodeURIComponent(saved.id);
  } catch (err) {
    showOpenError('開けませんでした: ' + err.message);
  } finally {
    input.value = ''; // 同じファイルをもう一度選べるようにする
  }
}

document.getElementById('rpOpenFile')?.addEventListener('change', handleOpenFile);

// ── 4. 移せなかった盤面セーブの案内 ──

/**
 * 移行に失敗して残っているスロットがあれば、一覧の上部に1行出す。
 * 「元のデータを書き出す」で、残っているスロットの中身を JSON ファイルにそのまま保存できる（データを捨てないための手段）。
 */
function showSlotMigrationNotice() {
  const box = document.getElementById('rpSlotNotice');
  const text = document.getElementById('rpSlotNoticeText');
  if (!box || !text) return;
  const n = getRemainingSlotKeys().length;
  box.hidden = n === 0;
  if (n === 0) return;
  text.textContent = `移せなかった盤面セーブが${n}件あります。元のデータは消していません。`;
}

document.getElementById('rpSlotExportBtn')?.addEventListener('click', () => {
  const json = exportRemainingSlotsJson();
  if (!json) { showSlotMigrationNotice(); return; }
  const url = URL.createObjectURL(new Blob([json], { type: 'application/json' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = 'card-sim-save-slots.json';
  a.click();
  URL.revokeObjectURL(url);
});

// 廃止した盤面セーブ（スロット1〜5）が残っていれば、0手のリプレイとして先に一覧へ移す
migrateSlotsToReplays();
showSlotMigrationNotice();
renderSaved();
renderShared();
initCardFilter();
loadPublic(true);
