// メルカリ参考相場の実コード(static/js/mercari-ref.js)を実行し、表示内容（レアリティ選択・すべて・
// 店舗に無いレアリティの除外・タイル構成・開閉）・XSS対策・古い応答の破棄を検証する。esc/escAttr/cssClass/safeUrl は index-main.js の実物を使う。
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert/strict');
const root = path.join(__dirname, '../..');
const main = fs.readFileSync(path.join(root, 'static/js/index-main.js'), 'utf8');
function block(start, end) {
  const a = main.indexOf(start), b = main.indexOf(end, a + start.length);
  assert(a >= 0 && b > a, '抽出失敗: ' + start);
  return main.slice(a, b);
}
class FakeTextDiv {
  constructor() { this._t = ''; }
  set textContent(v) { this._t = String(v == null ? '' : v); }
  get innerHTML() { return this._t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
}
const classes = new Set(['hidden']);
const box = {
  open: false, dataset: {},
  classList: { add: (n) => classes.add(n), remove: (n) => classes.delete(n), contains: (n) => classes.has(n) },
};
const body = { innerHTML: '' };
const head = { innerHTML: 'メルカリ' };
const pending = [];
const sandbox = {
  console, URL, Number, Promise, encodeURIComponent, Object,
  location: { origin: 'https://example.test' },
  window: { RARITY_CONFIG: { 'シークレット': { slug: 'secret', order: 40 }, 'ウルトラ': { slug: 'ultra', order: 110 }, 'ノーマル': { slug: 'normal', order: 160 } } },
  document: { getElementById: (id) => (id === 'mercariRef' ? box : id === 'mercariRefBody' ? body : id === 'mercariRefHead' ? head : null), createElement: () => new FakeTextDiv() },
  fetch(url) { return new Promise((resolve, reject) => pending.push({ url, resolve, reject })); },
};
vm.createContext(sandbox);
vm.runInContext(block('function esc(', 'function rankingLinkClick('), sandbox);
// 店舗リンクと共通の「PR」の印（index-main.js の実定数。注記は廃止）
vm.runInContext(block('const AFF_PR_BADGE=', 'function affPixel('), sandbox);
vm.runInContext(fs.readFileSync(path.join(root, 'static/js/mercari-ref.js'), 'utf8'), sandbox);
const api = sandbox.window.MercariRef;
const reply = (req, body, status = 200) => req.resolve({ ok: status >= 200 && status < 300, status, json: async () => body });
const tick = () => new Promise((r) => setImmediate(r));

const U = 'https://jp.mercari.com/search?keyword=x';
const resp = (by, extra = {}) => Object.assign({ ok: true, unclassified_count: 0, fetched_count: 120, num_found: 1316, search_url: U, by_rarity: by }, extra);
const BY = {
  'シークレット': { count: 3, min: 640, median: 700, min_is_floor: false, set: null },
  'ウルトラ': { count: 5, min: 1000, median: 1500, min_is_floor: false, set: { unit_min: 833, unit_min_qty: 3, unit_min_total: 2500, count: 4 } },
  'ノーマル': { count: 1, min: 300, median: null, min_is_floor: true, set: { unit_min: 300, unit_min_qty: 2, unit_min_total: 600, count: 2 } },
  'レリーフ': { count: 0, min: null, median: null, min_is_floor: false, set: { unit_min: 400, unit_min_qty: 3, unit_min_total: 1200, count: 1 } },
};
const body_ = () => body.innerHTML;
async function loadWith(d, view, status = 200) {
  api.load('x');
  api.setView(view);
  reply(pending.shift(), d, status);
  await tick(); await tick();
}

const BANNED = ['メルカリで出品中の価格です', '関連度上位', 'aff-note', '一部のリンクは広告', '他のレアリティ', 'レアリティ判定できず',
  '店舗に無いレアリティ', '表示していません', '確認中のレアリティ', '店舗の検索中', '最低出品価格', '3件未満', 'mref-grid', '単品最安',
  'rb rb-', '個人出品'];
const noBanned = (h) => BANNED.forEach((w) => assert(!h.includes(w), '削除した文言が残っている: ' + w + '\n' + h));
const nTiles = (h) => h.split('class="scard fin"').length - 1;

(async () => {
  // 0) 取得中は枠を出さない・前カードの表示を残さない
  box.open = true;
  api.load('灰流うらら');
  assert.equal(pending.length, 1);
  assert.equal(pending[0].url, '/api/mercari?name=' + encodeURIComponent('灰流うらら'));
  assert(classes.has('hidden') && body_() === '' && box.open === false);
  api.setView({ rarity: 'ウルトラ', stores: [] });
  reply(pending.shift(), resp(BY));
  await tick(); await tick();
  assert(!classes.has('hidden'));
  assert.equal(box.open, true, '表示するときは開いた状態');

  // 1) レアリティ選択: そのレアリティの「1枚」「N枚組」タイル（.summary に .scard 2枚）
  let h = body_();
  noBanned(h);
  assert(h.startsWith('<div class="summary">') && nTiles(h) === 2, h);
  assert.equal(h.split('mref-group').length - 1, 1, 'グループは1つ');
  assert(!h.includes('mref-ghead') && !h.includes('ウルトラ'), '特定レアリティ選択時は見出しを出さない（絞り込みバナーに出ている）');
  assert(h.includes('<div class="slabel">1枚</div>') && h.includes('<div class="slabel">3枚組</div>'), 'タイルのラベルは「1枚」「3枚組」だけ');
  assert(h.indexOf('<div class="slabel">1枚') < h.indexOf('<div class="slabel">3枚組'), '1枚が先（左）');
  const A1 = '<a class="scard-link" href="' + U + '" target="_blank" rel="noopener">';
  assert(h.includes('<div class="sprice">' + A1 + '¥1,000</a></div>'), '単品最安の数字がリンク: ' + h);
  assert(h.includes('<div class="sdetail">中央 ¥1,500・5件</div>'), h);
  assert(h.includes('<div class="sprice">' + A1 + '¥833</a><span class="mref-unit">/枚</span></div>'), '1枚あたり最安＋/枚: ' + h);
  assert(h.includes('<div class="sdetail">3枚 ¥2,500・4件</div>'), h);
  assert(!/gold|#e8c848|style="color/i.test(h));
  assert.equal(head.innerHTML, 'メルカリ', 'affiliateなしは見出しに印なし');
  const tpl = fs.readFileSync(path.join(root, 'templates/index.html'), 'utf8');
  assert(tpl.includes('#mercariRef .scard .sprice{color:var(--text)}') && tpl.includes('#mercariRef .scard .sprice a{color:var(--text)}'), 'タイル価格・リンクは通常文字色');
  assert(tpl.includes('#mode-search #mercariRef .summary{grid-template-columns:repeat(auto-fill,minmax(min(360px,100%),1fr));margin-bottom:0}'), 'price-layout.css に負けない詳細度でグループの外側グリッド');
  assert(tpl.includes('.mref-ghead{') && tpl.includes('padding-left:18px}') && tpl.includes('#mercariRef .mref-ghead{padding-left:11px}'), '見出しの左端をタイル内文字に揃える（PC 18 / スマホ 11）');
  assert(tpl.includes('.mref-pair{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))'), 'グループ内は常に2列（スマホ幅も）');
  assert(!tpl.includes('.summary,#mercariRef .summary{grid-template-columns:1fr}'), 'スマホで1列に潰さない');
  assert(tpl.includes('<summary class="chart-title" id="mercariRefHead">メルカリ</summary>'), '見出し');
  assert(!tpl.includes('.mref-grid') && !tpl.includes('.mref-h{') && !tpl.includes('.mref-set') && !tpl.includes('mref-tail'), '旧一覧・リンク行のCSSは廃止');
  assert(!h.includes('メルカリで見る') && !h.includes('<p'), 'タイルがあるときリンク行は無い');

  // 1b) 再取得せず選択を切り替える。中央値なし・セットなし・セットのみ
  api.setView({ rarity: 'ノーマル', stores: [] });
  assert.equal(pending.length, 0, '切替で再取得しない');
  h = body_();
  assert(h.includes('>¥300</a></div>') && h.includes('<div class="sdetail">1件</div>'), '中央値なしは「1件」のみ: ' + h);
  assert(h.includes('<div class="slabel">2枚組</div>') && h.includes('<div class="sdetail">2枚 ¥600・2件</div>'), '2枚組: ' + h);
  assert(!h.includes('*') && !h.includes('最低出品価格'));
  api.setView({ rarity: 'シークレット', stores: [] });
  h = body_();
  assert(nTiles(h) === 1 && h.includes('<div class="slabel">1枚</div>') && h.includes('<div class="mref-pair">') && h.endsWith('<div class="mref-empty"></div></div></div></div>') && h.includes('<div class="sdetail">中央 ¥700・3件</div>') && !h.includes('枚組'), 'セット無しは1枚タイルだけ: ' + h);
  api.setView({ rarity: 'レリーフ', stores: [] });
  h = body_();
  assert(nTiles(h) === 1 && !h.includes('<div class="slabel">1枚</div>') && h.includes('<div class="mref-pair"><div class="mref-empty"></div><div class="scard fin"><div class="slabel">3枚組</div>'), '単品なしはN枚組タイルだけ: ' + h);
  assert(h.includes('¥400</a><span class="mref-unit">/枚</span>'), 'セットのみは1枚あたり＋/枚');
  assert(h.includes('<div class="sdetail">3枚 ¥1,200・1件</div>'), h);
  assert(!h.includes('出品なし'));
  noBanned(h);
  // 開いた状態は描き直しで閉じない／ユーザーが閉じたら開き直さない
  assert.equal(box.open, true);
  box.open = false; // ユーザーが閉じた
  api.setView({ rarity: 'ウルトラ', stores: [] });
  assert.equal(box.open, false, '同じカードの描き直しで勝手に開き直さない');
  api.setView({ rarity: '', stores: [] });
  assert.equal(box.open, false);
  box.open = true;
  api.setView({ rarity: 'ウルトラ', stores: [] });
  assert.equal(box.open, true);

  // 2) 選択レアリティの出品が無い: 1行のみ
  api.setView({ rarity: 'スーパー', stores: [] });
  h = body_();
  assert(h.includes('このレアリティの出品は見つかりませんでした') && nTiles(h) === 0);
  assert(h.includes('<a class="scard-link" href="'+U+'" target="_blank" rel="noopener">メルカリで見る</a>'), '出品なしの行に続けてリンク: '+h);
  noBanned(h);

  // 3) すべて: 全レアリティのタイルを既存の順序で並べる
  api.setView({ rarity: '', stores: [] });
  h = body_();
  assert.equal(nTiles(h), 6, h);
  const order = ['シークレット', 'ウルトラ', 'ノーマル', 'レリーフ'].map((x) => h.indexOf('<div class="mref-ghead">' + x + '</div>'));
  assert(order.every((v, i) => v >= 0 && (i === 0 || v > order[i - 1])), 'レアリティ順のグループ: ' + order);
  assert.equal(h.split('mref-group"').length - 1, 4, 'レアリティごとに1グループ');
  assert.equal(h.split('<div class="mref-pair">').length - 1, 4);
  assert.equal(h.split('class="mref-empty"').length - 1, 2, 'セット無し(シークレット)・単品無し(レリーフ)の空セルで位置を保つ: ' + h);
  // 各グループの中身は常に2セル（1枚＝左・N枚組＝右）
  h.split('<div class="mref-pair">').slice(1).forEach((g) => {
    const cells = g.split('</div></div></div>')[0];
    assert.equal(cells.split('class="scard fin"').length - 1 + cells.split('class="mref-empty"').length - 1, 2, 'グループ内は2セル: ' + g);
  });
  noBanned(h);
  // (不明) 選択も全タイル
  api.setView({ rarity: '(不明)', stores: [] });
  assert.equal(nTiles(body_()), 6);

  // 4) 店舗に無いレアリティは表示から外す（注記は出さない）
  api.setView({ rarity: 'ウルトラ', stores: ['ウルトラ', 'シークレット'] });
  h = body_();
  assert(nTiles(h) === 2 && !h.includes('mref-ghead') && h.includes('¥1,000') && !h.includes('¥640') && !h.includes('¥300'));
  noBanned(h);
  api.setView({ rarity: '', stores: ['ウルトラ', 'シークレット'] });
  h = body_();
  assert.equal(nTiles(h), 3);
  assert(!h.includes('ノーマル') && !h.includes('¥300') && !h.includes('¥400'));
  noBanned(h);
  api.setView({ rarity: 'ノーマル', stores: ['ウルトラ'] });
  assert(body_().includes('このレアリティの出品は見つかりませんでした') && !body_().includes('¥600'));
  api.setView({ rarity: '', stores: [] });
  assert.equal(nTiles(body_()), 6, '店舗結果が空（確定後）は絞らない');
  api.setView({ rarity: '', stores: ['エクストラ'] });
  h = body_();
  assert(h.includes('条件に合う出品なし') && nTiles(h) === 0);
  noBanned(h);

  // 4b) 表示対象が1種類だけでも「すべて」はそのレアリティの2枚
  api.setView({ rarity: '', stores: ['ウルトラ'] });
  assert.equal(nTiles(body_()), 2);

  // 5) PR表記・sponsored: 見出しの印（1つだけ）と価格リンクの rel。タイル内の注記は出さない
  await loadWith(resp(BY, { affiliate: true, search_url: 'https://jp.mercari.com/search?keyword=x&afid=8033172568' }), { rarity: 'ウルトラ', stores: [] });
  h = body_();
  assert.equal(head.innerHTML, 'メルカリ<span class="aff-pr">PR</span>', '見出しの直後にPRの印');
  assert(!h.includes('aff-pr'), 'タイル内に印は出さない');
  assert.equal(h.split('rel="sponsored noopener"').length - 1, 2, '価格リンク2つとも sponsored: ' + h);
  assert(!h.includes('メルカリで見る（PR）'), '文字表記「（PR）」は廃止');
  assert(!h.includes("aff-note") && !h.includes("一部のリンクは広告") && !h.includes("含みます"), "パネル内の注記は出さない（PRの印だけ）");
  assert(h.includes('afid=8033172568') && h.includes('target="_blank"'));
  await loadWith(resp(BY, { affiliate: 'true' }), { rarity: 'ウルトラ', stores: [] });
  assert(!body_().includes('sponsored') && body_().includes('rel="noopener"') && head.innerHTML === 'メルカリ', 'affiliate が true 以外なら印なし');
  // 取得失敗では印を残さない
  await loadWith(resp(BY, { affiliate: true }), { rarity: 'ウルトラ', stores: [] });
  assert(head.innerHTML.includes('aff-pr'));
  await loadWith({ ok: false, error: 'fetch_failed' }, { rarity: '', stores: [] });
  assert.equal(head.innerHTML, 'メルカリ', '失敗時は印なし');

  // 5b) レアリティ判定できず件数は出さない。全件が判定不能のとき「出品なし」と言わずリンクだけ
  await loadWith(resp(BY, { unclassified_count: 7 }), { rarity: 'ウルトラ', stores: [] });
  assert(!body_().includes('判定できず'));
  await loadWith(resp({}, { unclassified_count: 5 }), { rarity: '', stores: [] });
  h = body_();
  assert(!h.includes('出品なし') && !h.includes('条件に合う') && !h.includes('判定できず'), h);
  assert(h.includes('>メルカリで見る</a>') && nTiles(h) === 0, '表示対象0のときは「メルカリで見る」だけの1行: ' + h);
  await loadWith(resp({}, { unclassified_count: 5, affiliate: true }), { rarity: '', stores: [] });
  assert(body_().includes('rel="sponsored noopener">メルカリで見る</a>'), 'アフィリエイト時は sponsored');
  await loadWith({ ok: false, error: 'fetch_failed' }, { rarity: '', stores: [] });
  assert(!body_().includes('メルカリで見る'), '取得失敗にリンクは不要');

  // 6) pending（店舗検索の途中）: 確認できたレアリティだけタイル。注記は出さない
  await loadWith(resp(BY), { rarity: '', stores: [], pending: true });
  h = body_();
  assert(!classes.has('hidden'), '店舗検索中でも枠は出る');
  assert.equal(box.open, false, '店舗検索中は閉じた見出しだけ');
  assert(nTiles(h) === 0 && !h.includes('出品なし'), 'D空: タイルなし: ' + h);
  noBanned(h);
  api.setView({ rarity: '', stores: ['ウルトラ'], pending: true });
  assert.equal(pending.length, 0, '描き直しで再取得しない');
  h = body_();
  assert(nTiles(h) === 2 && h.includes('<div class="mref-ghead">ウルトラ</div>') && !h.includes('¥640') && !h.includes('¥300'), h);
  noBanned(h);
  api.setView({ rarity: '', stores: ['ウルトラ', 'シークレット'], pending: true });
  assert.equal(nTiles(body_()), 3);
  assert.equal(box.open, false, '描き直しでも pending 中は開かない');
  // pending 中にユーザーが開いたら、そのまま開いておく
  box.open = true;
  api.setView({ rarity: '', stores: ['ウルトラ'], pending: true });
  assert.equal(box.open, true);
  // 確定（pending 解除）
  api.setView({ rarity: 'ウルトラ', stores: ['ウルトラ', 'シークレット'] });
  assert.equal(box.open, true);
  h = body_();
  assert(nTiles(h) === 2);
  noBanned(h);
  // 出品が全く無いときは pending でも「出品なし」
  await loadWith(resp({}, { fetched_count: 0, num_found: 0 }), { rarity: '', stores: [], pending: true });
  assert(body_().includes('出品なし'));
  // 同じ内容の描き直しでは DOM を書き換えない
  await loadWith(resp(BY), { rarity: '', stores: [], pending: true });
  let writes = 0, inner = body.innerHTML;
  Object.defineProperty(body, 'innerHTML', { configurable: true, get: () => inner, set: (v) => { writes++; inner = v; } });
  api.setView({ rarity: '', stores: [], pending: true });
  api.setView({ rarity: '', stores: [], pending: true });
  assert.equal(writes, 0, '同一HTMLを再代入しない');
  delete body.innerHTML; body.innerHTML = inner;
  // pending 中の 404 は枠ごと消える
  api.setView({ rarity: '', stores: [], pending: true });
  api.load('灰流うらら');
  reply(pending.shift(), { ok: false, error: 'unknown_card' }, 404);
  await tick(); await tick();
  assert(classes.has('hidden') && body_() === '');

  // 6b) pending 中は閉じたまま → 確定で自動オープン。確定後にユーザーが閉じたら開き直さない
  await loadWith(resp(BY), { rarity: '', stores: [], pending: true });
  assert.equal(box.open, false);
  delete box.dataset.autoOpen;
  api.setView({ rarity: '', stores: ['ウルトラ'] });
  assert.equal(box.open, true, '確定した時点で開く');
  assert.equal(box.dataset.autoOpen, '1', '自動オープンはGAから除外する印が付く');
  box.open = false;
  api.setView({ rarity: 'ウルトラ', stores: ['ウルトラ'] });
  assert.equal(box.open, false, '確定後にユーザーが閉じたら開き直さない');
  // 応答が確定（pending解除）の後に届いた場合も、届いた時点で開く
  api.load('灰流うらら');
  api.setView({ rarity: '', stores: ['ウルトラ'] });
  reply(pending.shift(), resp(BY));
  await tick(); await tick();
  assert.equal(box.open, true);

  // 7) 自動で開く: 新しいロードで再び開いた状態から。自動で開いた印（GA除外用）が付く
  await loadWith(resp(BY), { rarity: 'ウルトラ', stores: [] });
  assert.equal(box.open, true);
  assert.equal(box.dataset.autoOpen, '1');
  box.open = false; // ユーザーが閉じる
  api.load('灰流うらら'); // 新しい検索
  api.setView({ rarity: 'ウルトラ', stores: [] });
  reply(pending.shift(), resp(BY));
  await tick(); await tick();
  assert.equal(box.open, true, '新しい検索では再び開いた状態から');

  // 8) XSS: レアリティ名・URLに脱出ペイロード（選択表示・すべて・pending）
  const evil = '"><img src=x onerror=alert(1)>';
  const byEvil = { [evil]: { count: 1, min: 500, median: null, min_is_floor: false, set: { unit_min: 100, unit_min_qty: 3, unit_min_total: 300, count: 1 } },
    'ウルトラ': { count: 1, min: 900, median: null, min_is_floor: false, set: null } };
  for (const rar of ['', evil, 'ウルトラ']) {
    await loadWith(resp(byEvil, { search_url: 'javascript:alert(1)' }), { rarity: rar, stores: [] });
    h = body_();
    assert(!h.includes('<img'), 'レアリティ名がエスケープされていない: ' + h);
    assert(!h.includes('javascript:'), '危険なURLが出力された');
    assert(!h.includes('<a '), '安全でないURLのときはリンクを出さない（数字だけ）');
  }
  await loadWith(resp(byEvil), { rarity: '', stores: [], pending: true });
  assert(!body_().includes('<img'));
  await loadWith(resp({}, { search_url: 'https://jp.mercari.com/search?keyword="onmouseover="x' }), { rarity: '', stores: [] });
  assert(!/href="[^"]*"[^>]*onmouseover=/.test(body_()) && !body_().includes('"onmouseover="'), 'URLが属性から脱出');

  // 9) 出品なし
  await loadWith(resp({}, { fetched_count: 0, num_found: 0 }), { rarity: '', stores: [] });
  assert(body_().includes('出品なし') && !body_().includes('条件に合う') && !body_().includes('取得できませんでした'));
  await loadWith(resp({}), { rarity: '', stores: [] });
  assert(body_().includes('条件に合う出品なし'));

  // 10) 取得失敗は0件と区別した文言。パネルは出す
  await loadWith({ ok: false, error: 'fetch_failed' }, { rarity: '', stores: [] });
  assert(!classes.has('hidden') && body_().includes('メルカリの情報を取得できませんでした') && !body_().includes('出品なし'));
  await loadWith({ ok: false, error: 'rate_limited' }, { rarity: '', stores: [] }, 429);
  assert(body_().includes('メルカリの情報を取得できませんでした'));
  await loadWith({ ok: false, error: 'busy' }, { rarity: '', stores: [] }, 503);
  assert(body_().includes('混雑中です。少し待って再表示してください') && !body_().includes('取得できませんでした'));
  api.load('x');
  pending.shift().reject(new Error('network'));
  await tick(); await tick();
  assert(body_().includes('メルカリの情報を取得できませんでした'));
  api.setView({ rarity: 'ウルトラ', stores: [] });
  assert(body_().includes('メルカリの情報を取得できませんでした') && nTiles(body_()) === 0);

  // 11) 未知のカード(404)は枠ごと出さない
  await loadWith({ ok: false, error: 'unknown_card' }, { rarity: '', stores: [] }, 404);
  assert(classes.has('hidden') && body_() === '');

  // 12) 古い応答は捨てる(カード切替・clear後に届いた応答)
  const one = (p) => resp({ 'ウルトラ': { count: 1, min: p, median: null, min_is_floor: false, set: null } });
  api.setView({ rarity: 'ウルトラ', stores: [] });
  api.load('A');
  const a = pending.shift();
  api.load('B');
  const b = pending.shift();
  reply(b, one(111));
  await tick(); await tick();
  reply(a, one(999));
  await tick(); await tick();
  assert(body_().includes('¥111') && !body_().includes('¥999'), '古いカードの応答で上書きされた');
  box.open = true;
  api.load('C');
  const c = pending.shift();
  api.clear();
  assert(box.open === false);
  reply(c, one(222));
  await tick(); await tick();
  assert(classes.has('hidden') && body_() === '', 'clear後の応答が描画された');
  api.setView({ rarity: 'ウルトラ', stores: [] });
  assert(body_() === '');

  console.log('mercari_ref_check: OK');
})().catch((e) => { console.error('FAIL', e); process.exit(1); });
