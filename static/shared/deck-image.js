/**
 * deck-image.js — デッキ画像（Xなどに貼る一覧画像）をブラウザの中で作る（2026-10-20）
 *
 * 見た目はニューロンのデッキ画像にそろえる: メイン／EX を見出しと枚数で分け、同じカードは1枚ずつ並べる。
 * ニューロンに無いものとして、右上に「デッキ相場」の合計を入れられる（なし／合計だけ を切り替え）。
 * モック: ../mocks/deck-image-20261020.html（ユーザー確認済み）
 *
 * 以前はサーバー（/api/deck-image・Pillow）で合成していたが、30〜60秒かかり Render のメモリも圧迫するため
 * canvas に移した。カード画像の配信元（ygoresources・Supabase Storage）は Access-Control-Allow-Origin: * なので
 * crossOrigin で読めば書き出せる。それ以外の配信元（カーナベル）は CORS を許可しておらず canvas に描くと書き出せないため、
 * カード名の札で描く（自サイトの中継 /api/card-image-proxy はカーナベルに 403 で拒否され常に失敗したので 2026-10-21 に撤去した）。
 * 読めなかったカードも同じ札で代わりに描く（書き出しが止まらないように）。
 *
 * 使い方: openDeckImageDialog({ deckName, main:[{name,qty}], ex:[{name,qty}], total?, missingKinds?, sort? })
 *   total を渡さなければ、合計は /api/deck-estimate（DB相場）から取る。
 */

// ── 画像の大きさ（px）。モックと同じ値 ──
export const LAYOUT = Object.freeze({
  cardW: 120, cardH: 175, gap: 6, cols: 10, pad: 28,
  headH: 118, secH: 46, secGap: 22, footH: 70,
});

const COLORS = Object.freeze({
  // サイトの色（index.html :root の --bg / --bg-card / --border / --text / --text-m / --text-d / --gold）
  bg: '#0a1024', band: '#1a2547', line: '#2a3460', text: '#ffffff', sub: '#94a3b8', dim: '#64748b', gold: '#e8c848',
  placeholder: '#26324f',
});

const FONT = '"Noto Sans JP","Hiragino Kaku Gothic ProN","Hiragino Sans","Yu Gothic UI","Meiryo",sans-serif';
const COPYRIGHT = '©スタジオ・ダイス／集英社・テレビ東京・KONAMI';
const WORDMARK_SRC = '/static/brand/wordmark-lockup.png';
const MAX_CARDS = 200;          // これより多いデッキは作らない（貼り付けミスで巨大な画像を作らないように）
const MAX_KINDS = 80;           // サーバーの MAX_DECK_CARDS（/api/card-images・/api/deck-estimate が受け付ける種類数）と同じ
// 画像1枚の読み込みを打ち切る時間。超えたらカード名の札で描く（「作成中」のまま止まらないように）
const IMAGE_TIMEOUT_MS = 10000; // TODO: calibrate from data
// 同時に読む枚数（配信元への同時接続を抑える）
const IMAGE_CONCURRENCY = 8;    // TODO: calibrate from data
const JPEG_QUALITY = 0.92;      // PNG だと約3MB。JPEG にして X の上限（5MB）に余裕を持たせる
const TYPE_ORDER = { monster: 0, spell: 1, trap: 2 }; // トップの _DECK_TYPE_ORDER と同じ

// crossOrigin でそのまま読める配信元（2026-10-20 に Access-Control-Allow-Origin: * を確認）。
// Supabase Storage は公開バケットのパスで見る（プロジェクトが変わってもホスト名を書き換えずに済むように）
const CORS_OK_HOSTS = ['artworks-jp-n.ygoresources.com'];
const isSupabasePublic = u => u.host.endsWith('.supabase.co') && u.pathname.startsWith('/storage/v1/object/public/');

/** 利用者に見せてよい文言のエラー（それ以外のエラーは既定の文言にする） */
class DeckImageError extends Error {}

// ══════════════════ 計算だけの関数（Node のテストで確かめる） ══════════════════

/** [{name,qty}] を1枚ずつの名前の並びへ（ニューロンと同じ並べ方） */
export function expandCards(cards) {
  const out = [];
  for (const c of cards || []) {
    const n = Math.max(0, Math.floor(Number(c?.qty) || 0));
    for (let i = 0; i < n; i++) out.push(c.name);
  }
  return out;
}

/**
 * /api/card-images の値から、canvas に読み込むときの URL を決める。読めないもの（未発売の代わり表示・不明）は null。
 * @param {string|{kind?:string,url?:string}|null} value
 * @param {string} name
 */
export function imageSource(value, name) {
  const url = typeof value === 'string' ? value : (value && value.kind === 'image' ? value.url : null);
  if (typeof url !== 'string' || !/^https:\/\//.test(url)) return null;
  let u;
  try { u = new URL(url); } catch { return null; }
  if (CORS_OK_HOSTS.includes(u.host) || isSupabasePublic(u)) {
    // 画面の <img>（crossOrigin なし）で読んだキャッシュを Safari が使い回すと canvas が汚れて書き出せなくなるため、
    // canvas 用は URL を変えて別に読む
    u.searchParams.set('dki', '1');
    return u.toString();
  }
  // それ以外（カーナベル）は canvas に描けないので札にする
  return null;
}

/**
 * items を同時に limit 件までで処理する（結果は items と同じ順）。
 * @param {Array} items
 * @param {number} limit
 * @param {(item:any)=>Promise<any>} fn
 */
export async function mapLimited(items, limit, fn) {
  const out = new Array(items.length);
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const i = next++;
      out[i] = await fn(items[i]);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return out;
}

/**
 * 画像全体の大きさと、各区分の見出し・カード列の位置。
 * @param {Array<{title:string,count:number}>} sections 枚数0の区分は渡さない
 */
export function computeLayout(sections, L = LAYOUT) {
  const width = L.pad * 2 + L.cols * L.cardW + (L.cols - 1) * L.gap;
  const gridH = n => { const r = Math.ceil(n / L.cols); return r ? r * L.cardH + (r - 1) * L.gap : 0; };
  let y = L.headH;
  const placed = sections.map((s, i) => {
    const sec = { ...s, bandY: y, gridY: y + L.secH };
    y = sec.gridY + gridH(s.count) + (i < sections.length - 1 ? L.secGap : 0);
    return sec;
  });
  return { width, height: y + L.footH, sections: placed, footY: y };
}

/** k 枚目のカードの左上（区分の中での位置） */
export function cardPosition(k, gridY, L = LAYOUT) {
  return { x: L.pad + (k % L.cols) * (L.cardW + L.gap), y: gridY + Math.floor(k / L.cols) * (L.cardH + L.gap) };
}

/** 種別（モンスター→魔法→罠）で並べ替え。分からないカードは最後。元の順は保つ */
export function sortByType(cards, types) {
  return [...cards].sort((a, b) => (TYPE_ORDER[types[a.name]] ?? 3) - (TYPE_ORDER[types[b.name]] ?? 3));
}

/** /api/deck-estimate の結果から合計と、相場の無いカードの種類数を出す */
export function summarizeEstimate(results) {
  let total = 0, missingKinds = 0;
  for (const r of results || []) {
    if (r && r.best && Number.isFinite(r.best.price)) total += r.best.price * (Number(r.qty) || 1);
    else missingKinds++;
  }
  return { total, missingKinds };
}

/** 保存するファイル名（OS で使えない文字を除く） */
export function fileNameFor(deckName) {
  const base = String(deckName || 'デッキ').replace(/[\\/:*?"<>|\u0000-\u001f]/g, '').trim().slice(0, 60) || 'デッキ';
  return `${base}_デッキ画像.jpg`;
}

// ══════════════════ 描画 ══════════════════

function _loadImage(src) {
  return new Promise(resolve => {
    if (!src) { resolve(null); return; }
    const im = new Image();
    let done = false;
    const finish = v => { if (done) return; done = true; clearTimeout(timer); resolve(v); };
    const timer = setTimeout(() => { im.src = ''; finish(null); }, IMAGE_TIMEOUT_MS);
    if (/^https?:/.test(src)) im.crossOrigin = 'anonymous';
    im.decoding = 'async';
    im.onload = () => finish(im);
    im.onerror = () => finish(null);
    im.src = src;
  });
}

/** 名前ごとに画像を読む（同時に IMAGE_CONCURRENCY 件まで） */
async function _loadCardImages(names, imageValues) {
  const srcs = names.map(n => [n, imageSource(imageValues?.[n], n)]).filter(([, s]) => s);
  const imgs = new Map();
  await mapLimited(srcs, IMAGE_CONCURRENCY, async ([n, s]) => { imgs.set(n, await _loadImage(s)); });
  return imgs;
}

function _fitText(ctx, text, maxW) {
  if (ctx.measureText(text).width <= maxW) return text;
  let t = text;
  while (t.length > 1 && ctx.measureText(t + '…').width > maxW) t = t.slice(0, -1);
  return t + '…';
}

function _roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

/** 読めなかったカードの代わりの札（カード名を折り返して書く） */
function _drawPlaceholder(ctx, name, x, y, L) {
  ctx.fillStyle = COLORS.placeholder;
  ctx.fillRect(x, y, L.cardW, L.cardH);
  ctx.fillStyle = COLORS.sub;
  ctx.font = `400 13px ${FONT}`;
  ctx.textAlign = 'left';
  const lines = [];
  let buf = '';
  for (const ch of String(name)) {
    if (ctx.measureText(buf + ch).width > L.cardW - 12) { lines.push(buf); buf = ch; } else buf += ch;
  }
  if (buf) lines.push(buf);
  const shown = lines.slice(0, 6);
  let ty = y + (L.cardH - shown.length * 17) / 2 + 13;
  for (const line of shown) { ctx.fillText(line, x + 6, ty); ty += 17; }
}

/**
 * デッキ画像を canvas に描く。
 * @param {{deckName:string, main:Array, ex:Array, price:null|{total:number,missingKinds:number}}} deck
 * @param {Record<string,any>} imageValues /api/card-images の images
 * @returns {Promise<HTMLCanvasElement>}
 */
export async function renderDeckImage(deck, imageValues) {
  const L = LAYOUT;
  const sectionDefs = [
    { title: 'メインデッキ', names: expandCards(deck.main) },
    { title: 'エクストラデッキ', names: expandCards(deck.ex) },
  ].filter(s => s.names.length);
  const layout = computeLayout(sectionDefs.map(s => ({ title: s.title, count: s.names.length })));

  // 画像は名前ごとに1回だけ読む
  // フォントは画像に書く文字の分を先に読んでおく（初めて出る字だけ別の書体になるのを防ぐ）
  const uniq = [...new Set(sectionDefs.flatMap(s => s.names))];
  const priceText = deck.price ? 'デッキ相場¥0123456789,※種類は相場データなし' : '';
  const texts = `${deck.deckName || 'デッキ'}メインデッキエクストラ枚0123456789${priceText}${COPYRIGHT}`;
  const fontLoads = document.fonts?.load
    ? ['900 46px', '700 20px', '400 14px'].map(f => document.fonts.load(`${f} "Noto Sans JP"`, texts).catch(() => null))
    : [];
  const [imgs, logo] = await Promise.all([_loadCardImages(uniq, imageValues), _loadImage(WORDMARK_SRC), ...fontLoads]);

  const cv = document.createElement('canvas');
  cv.width = layout.width;
  cv.height = layout.height;
  const ctx = cv.getContext('2d');
  ctx.fillStyle = COLORS.bg;
  ctx.fillRect(0, 0, cv.width, cv.height);
  ctx.textBaseline = 'alphabetic';

  // ── 見出し: デッキ名と（合計だけのとき）デッキ相場 ──
  let titleMax = cv.width - L.pad * 2;
  if (deck.price) {
    const price = '¥' + deck.price.total.toLocaleString('ja-JP');
    ctx.textAlign = 'right';
    ctx.font = `900 44px ${FONT}`;
    const pw = ctx.measureText(price).width;
    ctx.fillStyle = COLORS.gold;
    ctx.fillText(price, cv.width - L.pad, 80);
    ctx.font = `700 17px ${FONT}`;
    ctx.fillStyle = COLORS.sub;
    ctx.fillText('デッキ相場', cv.width - L.pad, 34);
    let rightW = pw;
    if (deck.price.missingKinds > 0) {
      ctx.font = `400 13px ${FONT}`;
      ctx.fillStyle = COLORS.dim;
      const note = `※${deck.price.missingKinds}種類は相場データなし`;
      rightW = Math.max(rightW, ctx.measureText(note).width);
      ctx.fillText(note, cv.width - L.pad, 97);
    }
    titleMax -= rightW + 40;
  }
  ctx.textAlign = 'left';
  ctx.font = `900 46px ${FONT}`;
  ctx.fillStyle = COLORS.text;
  ctx.fillText(_fitText(ctx, deck.deckName || 'デッキ', titleMax), L.pad, 80);
  ctx.fillStyle = COLORS.line;
  ctx.fillRect(L.pad, L.headH - 14, cv.width - L.pad * 2, 2);

  // ── 区分ごとの見出しとカード ──
  layout.sections.forEach((sec, i) => {
    const names = sectionDefs[i].names;
    ctx.fillStyle = COLORS.band;
    _roundRect(ctx, L.pad, sec.bandY + 4, cv.width - L.pad * 2, L.secH - 12, 6);
    ctx.fill();
    ctx.font = `700 20px ${FONT}`;
    ctx.fillStyle = COLORS.text;
    ctx.fillText(sec.title, L.pad + 14, sec.bandY + L.secH - 17);
    const tw = ctx.measureText(sec.title).width;
    ctx.fillStyle = COLORS.sub;
    ctx.fillText(`${names.length}枚`, L.pad + 14 + tw + 14, sec.bandY + L.secH - 17);
    names.forEach((n, k) => {
      const { x, y } = cardPosition(k, sec.gridY);
      const im = imgs.get(n);
      if (im) ctx.drawImage(im, x, y, L.cardW, L.cardH);
      else _drawPlaceholder(ctx, n, x, y, L);
    });
  });

  // ── 下の端: ロゴと権利表記 ──
  const fy = layout.footY;
  ctx.fillStyle = COLORS.line;
  ctx.fillRect(L.pad, fy + 14, cv.width - L.pad * 2, 1);
  if (logo) {
    const lh = 32, lw = logo.width * lh / logo.height;
    ctx.globalAlpha = 0.9;
    ctx.drawImage(logo, L.pad, fy + 26, lw, lh);
    ctx.globalAlpha = 1;
  }
  ctx.textAlign = 'right';
  ctx.font = `400 14px ${FONT}`;
  ctx.fillStyle = COLORS.dim;
  ctx.fillText(COPYRIGHT, cv.width - L.pad, fy + 48);
  ctx.textAlign = 'left';
  return cv;
}

// ══════════════════ 通信 ══════════════════

async function _postJson(url, body) {
  const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function _fetchImageValues(names) {
  const data = await _postJson('/api/card-images', { names });
  return data?.images || {};
}

async function _fetchTypes(names) {
  try { return (await _postJson('/api/card-types', { names }))?.types || {}; } catch { return {}; }
}

async function _fetchEstimate(cards) {
  const param = cards.map(c => (c.qty > 1 ? `${c.qty} ${c.name}` : c.name)).join('|');
  const data = await _postJson('/api/deck-estimate', { cards: param });
  if (data?.error) throw new Error(data.error);
  return summarizeEstimate(data.results);
}

// ══════════════════ 画面: 作成ダイアログ ══════════════════

const STYLE_ID = 'deck-image-style';
const STYLE = `
.dki-overlay{position:fixed;inset:0;z-index:10000;background:rgba(4,8,20,.72);display:flex;align-items:center;justify-content:center;padding:16px}
.dki-panel{background:#1a2547;color:#fff;border:1px solid #2a3460;border-radius:12px;width:min(720px,100%);max-height:calc(100dvh - 32px);display:flex;flex-direction:column;font-family:${FONT};box-shadow:0 12px 40px rgba(0,0,0,.5)}
.dki-head{display:flex;align-items:center;justify-content:space-between;padding:12px 14px 8px}
.dki-head h2{margin:0;font-size:1rem;font-weight:700}
.dki-close{background:none;border:0;color:#94a3b8;font-size:1.5rem;line-height:1;cursor:pointer;padding:4px 8px}
.dki-opts{display:flex;align-items:center;gap:8px;padding:0 14px 10px;font-size:.85rem;color:#94a3b8}
/* シミュレーターは span 全般に文字色（#333）を当てているので、ダイアログの文字は要素ごとに色を書く */
.dki-opts>span{color:#94a3b8}
.dki-head h2{color:#fff}
.dki-seg{display:inline-flex;border:1px solid #2a3460;border-radius:8px;overflow:hidden}
.dki-seg button{background:transparent;color:#94a3b8;border:0;padding:7px 14px;font:inherit;cursor:pointer}
.dki-seg button[aria-pressed="true"]{background:#4a7cff;color:#fff}
.dki-preview{flex:1;min-height:120px;overflow:auto;padding:0 14px;display:flex;align-items:flex-start;justify-content:center}
.dki-preview img{width:100%;height:auto;border-radius:6px;display:block}
.dki-msg{color:#94a3b8;font-size:.9rem;padding:32px 0;text-align:center}
.dki-msg[role="alert"]{color:#f87171}
.dki-actions{display:flex;gap:8px;justify-content:flex-end;padding:12px 14px 14px;flex-wrap:wrap}
.dki-btn{display:inline-flex;align-items:center;justify-content:center;min-height:40px;padding:0 18px;border-radius:8px;border:1px solid #2a3460;background:transparent;color:#fff;font:inherit;font-size:.9rem;font-weight:700;cursor:pointer;text-decoration:none}
.dki-btn-primary{background:#4a7cff;border-color:#4a7cff}
.dki-btn[aria-disabled="true"],.dki-btn:disabled{opacity:.45;pointer-events:none}
.dki-btn[hidden]{display:none}
`;

function _ensureStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const s = document.createElement('style');
  s.id = STYLE_ID;
  s.textContent = STYLE;
  document.head.appendChild(s);
}

function _el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

let _open = null; // 開いているダイアログを閉じる関数（二重に開かない）

/**
 * デッキ画像の作成ダイアログを開く。
 * @param {{deckName?:string, main:Array<{name,qty}>, ex?:Array<{name,qty}>,
 *          total?:number|null, missingKinds?:number, sort?:boolean}} opts
 *   total … 計算済みの合計（トップのデッキ相場計算）。無ければ合計を選んだときに DB 相場から取る
 *   sort  … true なら種別順（モンスター→魔法→罠）に並べ替える（シミュレーターの盤面順は並びが崩れているため）
 */
export function openDeckImageDialog(opts) {
  if (_open) _open();
  _ensureStyle();
  const deckName = String(opts.deckName || '').trim() || 'デッキ';
  let main = (opts.main || []).filter(c => c && c.name && c.qty > 0);
  let ex = (opts.ex || []).filter(c => c && c.name && c.qty > 0);
  const count = [...main, ...ex].reduce((s, c) => s + c.qty, 0);

  const overlay = _el('div', 'dki-overlay');
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.setAttribute('aria-label', 'デッキ画像');
  const panel = _el('div', 'dki-panel');
  const head = _el('div', 'dki-head');
  const close = _el('button', 'dki-close', '×');
  close.type = 'button';
  close.setAttribute('aria-label', '閉じる');
  head.append(_el('h2', '', 'デッキ画像'), close);

  const optsRow = _el('div', 'dki-opts');
  const seg = _el('div', 'dki-seg');
  const modes = [['total', '合計だけ'], ['none', 'なし']];
  let mode = 'total';
  const segBtns = modes.map(([v, label]) => {
    const b = _el('button', '', label);
    b.type = 'button';
    b.dataset.v = v;
    b.setAttribute('aria-pressed', String(v === mode));
    seg.appendChild(b);
    return b;
  });
  optsRow.append(_el('span', '', '価格'), seg);

  const preview = _el('div', 'dki-preview');
  const actions = _el('div', 'dki-actions');
  const shareBtn = _el('button', 'dki-btn', '共有');
  shareBtn.type = 'button';
  shareBtn.hidden = true;
  const save = _el('a', 'dki-btn dki-btn-primary', '画像を保存');
  save.setAttribute('aria-disabled', 'true');
  actions.append(shareBtn, save);
  panel.append(head, optsRow, preview, actions);
  overlay.appendChild(panel);
  document.body.appendChild(overlay);

  let objectUrl = null;
  let currentFile = null;
  let runId = 0;
  let closed = false;
  const setMsg = (text, isError = false, busy = false) => {
    const m = _el('div', 'dki-msg', text);
    if (isError) m.setAttribute('role', 'alert');
    if (busy) m.classList.add('ld-spin'); // 読込中の輪（loading.css）
    preview.replaceChildren(m);
  };
  const finish = () => {
    if (closed) return;
    closed = true;
    runId++;
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    overlay.remove();
    document.removeEventListener('keydown', onKey);
    if (_open === finish) _open = null;
  };
  const onKey = e => { if (e.key === 'Escape') finish(); };
  _open = finish;
  close.addEventListener('click', finish);
  overlay.addEventListener('click', e => { if (e.target === overlay) finish(); });
  document.addEventListener('keydown', onKey);

  if (!count) { setMsg('デッキにカードがありません', true); return finish; }
  if (count > MAX_CARDS) { setMsg(`カードが多すぎるため作れません（${MAX_CARDS}枚まで）`, true); return finish; }
  if (new Set([...main, ...ex].map(c => c.name)).size > MAX_KINDS) {
    setMsg(`カードの種類が多すぎるため作れません（${MAX_KINDS}種類まで）`, true); return finish;
  }

  // 並べ替え・画像URL・合計は1回だけ取って使い回す（価格の切り替えで取り直さない）
  let prepared = null;
  let estimate = Number.isFinite(opts.total)
    ? Promise.resolve({ total: opts.total, missingKinds: Math.max(0, opts.missingKinds | 0) })
    : null;
  const prepare = async () => {
    if (prepared) return prepared;
    const names = [...new Set([...main, ...ex].map(c => c.name))];
    const [values, types] = await Promise.all([
      _fetchImageValues(names),
      opts.sort ? _fetchTypes(names) : Promise.resolve(null),
    ]);
    if (types) main = sortByType(main, types);
    prepared = values;
    return prepared;
  };

  const run = async () => {
    const my = ++runId;
    save.setAttribute('aria-disabled', 'true');
    save.removeAttribute('href');
    shareBtn.hidden = true;
    currentFile = null;
    setMsg('画像を作成中…', false, true);
    try {
      const values = await prepare();
      let price = null;
      if (mode === 'total') {
        if (!estimate) estimate = _fetchEstimate([...main, ...ex]);
        try { price = await estimate; } catch { estimate = null; throw new DeckImageError('相場を取得できませんでした。「なし」なら作成できます'); }
        // 1枚も相場が無いときに「¥0」と書いた画像を作らない
        const kinds = new Set([...main, ...ex].map(c => c.name)).size;
        if (price.missingKinds >= kinds) throw new DeckImageError('このデッキは相場データが無いため合計を入れられません。「なし」なら作成できます');
      }
      const cv = await renderDeckImage({ deckName, main, ex, price }, values);
      const blob = await new Promise((resolve, reject) => {
        try { cv.toBlob(b => (b ? resolve(b) : reject(new Error('画像を書き出せませんでした'))), 'image/jpeg', JPEG_QUALITY); }
        catch (e) { reject(e); }
      });
      if (my !== runId) return;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      objectUrl = URL.createObjectURL(blob);
      const fname = fileNameFor(deckName);
      currentFile = typeof File === 'function' ? new File([blob], fname, { type: 'image/jpeg' }) : null;
      const img = _el('img');
      img.src = objectUrl;
      img.alt = `${deckName}のデッキ画像`;
      preview.replaceChildren(img);
      save.href = objectUrl;
      save.download = fname;
      save.removeAttribute('aria-disabled');
      shareBtn.hidden = !(currentFile && navigator.canShare && navigator.canShare({ files: [currentFile] }));
    } catch (e) {
      if (my !== runId) return;
      if (!(e instanceof DeckImageError)) console.warn('[deck-image] 作成に失敗', e);
      setMsg(e instanceof DeckImageError ? e.message : '画像を作成できませんでした。時間をおいてお試しください', true);
    }
  };

  segBtns.forEach(b => b.addEventListener('click', () => {
    if (mode === b.dataset.v) return;
    mode = b.dataset.v;
    segBtns.forEach(x => x.setAttribute('aria-pressed', String(x === b)));
    run();
  }));
  shareBtn.addEventListener('click', async () => {
    if (!currentFile) return;
    try { await navigator.share({ files: [currentFile] }); } catch { /* 共有シートを閉じただけ */ }
  });

  close.focus();
  run();
  return finish;
}
