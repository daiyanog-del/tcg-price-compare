// メルカリ参考相場の実コード(static/js/mercari-ref.js)を実行し、表示内容（レアリティ選択・すべて・
// 店舗に無いレアリティの除外）・XSS対策・古い応答の破棄を検証する。esc/escAttr/cssClass/safeUrl は index-main.js の実物を使う。
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
  open: false,
  classList: { add: (n) => classes.add(n), remove: (n) => classes.delete(n), contains: (n) => classes.has(n) },
};
const body = { innerHTML: '' };
const pending = [];
const sandbox = {
  console, URL, Number, Promise, encodeURIComponent, Object,
  location: { origin: 'https://example.test' },
  window: { RARITY_CONFIG: { 'シークレット': { slug: 'secret', order: 40 }, 'ウルトラ': { slug: 'ultra', order: 110 }, 'ノーマル': { slug: 'normal', order: 160 } } },
  document: { getElementById: (id) => (id === 'mercariRef' ? box : id === 'mercariRefBody' ? body : null), createElement: () => new FakeTextDiv() },
  fetch(url) { return new Promise((resolve, reject) => pending.push({ url, resolve, reject })); },
};
vm.createContext(sandbox);
vm.runInContext(block('function esc(', 'function rankingLinkClick('), sandbox);
// 店舗リンクと共通の「PR」の印・注記（index-main.js の実定数）
vm.runInContext(block('const AFF_PR_BADGE=', '// 注記ブロック') + block('const AFF_NOTE_TEXT=', 'function affPixel('), sandbox);
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

(async () => {
  // 0) 取得中は枠を出さない・閉じた状態（店舗表の描画を遅らせない・前カードの表示を残さない）
  box.open = true;
  api.load('灰流うらら');
  assert.equal(pending.length, 1);
  assert.equal(pending[0].url, '/api/mercari?name=' + encodeURIComponent('灰流うらら'));
  assert(classes.has('hidden') && body_() === '' && box.open === false);
  api.setView({ rarity: 'ウルトラ', stores: [] });
  reply(pending.shift(), resp(BY));
  await tick(); await tick();
  assert(!classes.has('hidden'));
  assert.equal(box.open, false, '初期状態は閉じる');

  // 1) レアリティ選択: タイル2枚
  let h = body_();
  assert(h.includes('<p class="buy-inline-note">メルカリで出品中の価格です（関連度上位120件から集計）</p>'));
  assert(h.includes('<div class="summary">') && h.split('class="scard').length - 1 === 2);
  assert(h.includes('1枚で買う') && h.includes('3枚組で買う'));
  assert(h.includes('<div class="sprice">¥1,000</div>'), '単品最安');
  assert(h.includes('ウルトラ・出品5件の最安<br>中央値 ¥1,500'));
  assert(h.includes('<div class="sprice">¥833<span class="mref-unit">/枚</span></div>'), '1枚あたり＋/枚');
  assert(h.includes('ウルトラ・3枚組 ¥2,500<br>同じカードだけの組 4件'), '総額は出品価格そのもの');
  // 価格は金色にしない（HTMLに金色指定を持ち込まない・CSSは通常文字色に上書き）
  assert(!/gold|#e8c848|style="color/i.test(h));
  const tpl = fs.readFileSync(path.join(root, 'templates/index.html'), 'utf8');
  assert(tpl.includes('#mercariRef .scard .sprice{color:var(--text)}'), 'メルカリのタイル価格は通常文字色');
  // 他のレアリティ: 1枚あたりの安い順（セットのみのレアリティも含む）。リンクも同じ行
  assert(h.includes('他のレアリティ: ノーマル ¥300〜 / レリーフ ¥400〜 / シークレット ¥640〜'), h);
  assert(!h.includes('ほか'), '3つ以内なら「ほかN種」なし');
  assert(h.includes('rel="noopener"') && h.includes('target="_blank"') && h.includes('メルカリで見る'));
  assert(!h.includes('sponsored') && !h.includes('PR') && !h.includes('aff-note'), 'affiliateなしはPRの印・注記なし');
  assert(!h.includes('店舗に無い'));

  // 1b) 再取得せず選択を切り替える（レアリティ変更）
  api.setView({ rarity: 'ノーマル', stores: [] });
  assert.equal(pending.length, 0, '切替で再取得しない');
  h = body_();
  assert(h.includes('<div class="sprice">¥300<span class="mref-unit">最低出品価格</span></div>'), '300円は最低出品価格');
  assert(h.includes('中央値 —（3件未満）'));
  assert(h.includes('2枚組で買う') && h.includes('ノーマル・2枚組 ¥600<br>同じカードだけの組 2件'), '最安がN=2の組なら見出しも2枚組');
  api.setView({ rarity: 'レリーフ', stores: [] });
  h = body_();
  assert(h.includes('1枚で買う</div><div class="sprice mref-none">出品なし</div>'), '単品なしは出品なし');
  assert(h.includes('<div class="sprice">¥400<span class="mref-unit">/枚</span></div>'));
  api.setView({ rarity: 'シークレット', stores: [] });
  h = body_();
  assert(h.includes('3枚組で買う</div><div class="sprice mref-none">出品なし</div>'), 'セットなしは出品なし');
  assert(h.includes('<div class="sprice">¥640</div>'));
  // 開いた状態は描き直しで閉じない
  box.open = true;
  api.setView({ rarity: 'ウルトラ', stores: [] });
  assert.equal(box.open, true);

  // 2) 選択レアリティの出品が無い
  api.setView({ rarity: 'スーパー', stores: [] });
  h = body_();
  assert(h.includes('このレアリティの出品は見つかりませんでした') && !h.includes('class="scard'));
  assert(h.includes('他のレアリティ: ノーマル ¥300〜 / レリーフ ¥400〜 / シークレット ¥640〜 ほか1種'), h);
  assert(h.includes('メルカリで見る'));

  // 3) すべて表示: 1行ずつ（札・単品最安・セット1枚あたり）。中央値・件数列なし
  api.setView({ rarity: '', stores: [] });
  h = body_();
  assert(h.indexOf('シークレット') < h.indexOf('ウルトラ') && h.indexOf('ウルトラ') < h.indexOf('ノーマル'), 'レアリティ順');
  assert(h.includes('>単品最安<') && h.includes('>セット(1枚)<') && !h.includes('中央') && !h.includes('件数'));
  assert.equal(h.split('class="mref-h"').length - 1, 3);
  assert(h.includes('<span class="mref-min">¥640</span><span class="mref-set">—</span>'));
  assert(h.includes('<span class="mref-min">¥1,000</span><span class="mref-set">¥833<small>3枚組</small></span>'));
  assert(h.includes('<span class="mref-min">¥300*</span><span class="mref-set">¥300<small>2枚組</small></span>'), '300円は*');
  assert(h.includes('<span class="mref-min">—</span><span class="mref-set">¥400<small>3枚組</small></span>'), 'セットのみ');
  assert(h.includes('* 300円は最低出品価格'));
  assert(h.includes('rb-secret'));
  assert(h.indexOf('メルカリで出品中の価格です') < h.indexOf('mref-grid') && h.indexOf('mref-grid') < h.indexOf('メルカリで見る'), '注記・表・リンクの順');
  assert(!h.includes('他のレアリティ'));

  // 4) 店舗に無いレアリティは表示対象から外し、件数を注記する（選択表示・すべて表示の両方）
  api.setView({ rarity: 'ウルトラ', stores: ['ウルトラ', 'シークレット'] });
  h = body_();
  assert(h.includes('他のレアリティ: シークレット ¥640〜 ') === false && h.includes('他のレアリティ: シークレット ¥640〜') && !h.includes('ノーマル ¥') && !h.includes('レリーフ ¥'));
  assert(h.includes('店舗に無いレアリティ（ノーマル・レリーフ）は表示していません'), h);
  assert(!h.includes('件は表示') && !h.includes('レアリティ 2件'), '単位「件」は使わない');
  api.setView({ rarity: '', stores: ['ウルトラ', 'シークレット'] });
  h = body_();
  assert(!h.includes('rb-normal') && !h.includes('>ノーマル<') && h.includes('>ウルトラ<') && h.includes('>シークレット<'));
  assert(h.includes('店舗に無いレアリティ（ノーマル・レリーフ）は表示していません'));
  assert(!h.includes('¥300'), '除外したレアリティの価格が残っていない');
  // 店舗に無いレアリティを選んでいる場合（通常は起きない）も出品は出さない
  api.setView({ rarity: 'ノーマル', stores: ['ウルトラ'] });
  assert(body_().includes('このレアリティの出品は見つかりませんでした') && !body_().includes('¥600'));
  // 店舗結果が空のときは絞らない
  api.setView({ rarity: '', stores: [] });
  assert(body_().includes('>ノーマル<') && !body_().includes('店舗に無い'));
  // 全レアリティが店舗に無い
  api.setView({ rarity: '', stores: ['エクストラ'] });
  assert(body_().includes('条件に合う出品なし') && body_().includes('店舗に無いレアリティ（シークレット・ウルトラ・ノーマル・ほか1種）は表示していません'));

  // 4b) 表示対象が1種類だけなら「すべて」でもタイル表示（rarityFilterが空でも）
  api.setView({ rarity: '', stores: ['ウルトラ'] });
  h = body_();
  assert(h.includes('class="scard') && !h.includes('mref-grid') && h.includes('ウルトラ・出品5件の最安'), h);
  assert(h.includes('店舗に無いレアリティ（シークレット・ノーマル・レリーフ）は表示していません'));
  // 店舗側で「(不明)」を選んでいるときはタイルでなく一覧
  api.setView({ rarity: '(不明)', stores: [] });
  assert(body_().includes('mref-grid') && !body_().includes('class="scard'));
  // 単一レアリティの応答はそのままタイル
  api.setView({ rarity: '', stores: [] });
  assert(body_().includes('mref-grid'));

  // 5) PR表記・sponsored の分岐
  await loadWith(resp(BY, { affiliate: true, search_url: 'https://jp.mercari.com/search?keyword=x&afid=8033172568' }), { rarity: 'ウルトラ', stores: [] });
  h = body_();
  assert(h.includes('rel="sponsored noopener"') && h.includes('>メルカリで見る</a><span class="aff-pr">PR</span>'), h);
  assert(!h.includes('メルカリで見る（PR）'), '文字表記「（PR）」は廃止');
  assert(h.includes('<div class="aff-note">一部のリンクは広告（PR）を含みます</div>'));
  assert(h.indexOf('class="aff-note"') < h.indexOf('メルカリで見る') && h.indexOf('class="summary"') < h.indexOf('class="aff-note"'), 'タイルの直下に注記');
  assert(h.includes('afid=8033172568') && h.includes('target="_blank"'));
  await loadWith(resp(BY, { affiliate: 'true' }), { rarity: 'ウルトラ', stores: [] });
  assert(!body_().includes('sponsored') && !body_().includes('aff-pr') && !body_().includes('aff-note') && body_().includes('rel="noopener"'));

  // 5b) レアリティ判定できず件数: 最下行の注記にまとめる。全件が判定不能のとき「出品なし」と言わない
  await loadWith(resp(BY, { unclassified_count: 7 }), { rarity: 'ウルトラ', stores: [] });
  assert(body_().includes('レアリティ判定できず 7件'));
  await loadWith(resp({}, { unclassified_count: 5 }), { rarity: '', stores: [] });
  h = body_();
  assert(h.includes('レアリティ判定できず 5件') && h.includes('メルカリで見る') && !h.includes('出品なし') && !h.includes('条件に合う'), h);
  await loadWith(resp(BY), { rarity: 'ウルトラ', stores: [] });
  assert(!body_().includes('判定できず'));
  // 5c) 店舗に無いレアリティが3種のとき（ほかN種は4種超の全除外ケースで検証済み）
  await loadWith(resp(BY), { rarity: '', stores: ['エクストラ', 'ウルトラ'] });
  assert(body_().includes('店舗に無いレアリティ（シークレット・ノーマル・レリーフ）は表示していません'));

  // 6) 全件取得できているときは「関連度上位」の注記を出さない
  await loadWith(resp(BY, { fetched_count: 5, num_found: 5 }), { rarity: 'ウルトラ', stores: [] });
  assert(body_().includes('メルカリで出品中の価格です</p>') && !body_().includes('関連度上位'));

  // 7) XSS: レアリティ名・URLに脱出ペイロード（選択表示・すべて・他のレアリティ行）
  const evil = '"><img src=x onerror=alert(1)>';
  const byEvil = { [evil]: { count: 1, min: 500, median: null, min_is_floor: false, set: { unit_min: 100, unit_min_qty: 3, unit_min_total: 300, count: 1 } },
    'ウルトラ': { count: 1, min: 900, median: null, min_is_floor: false, set: null } };
  for (const rar of ['', evil, 'ウルトラ']) {
    await loadWith(resp(byEvil, { search_url: 'javascript:alert(1)' }), { rarity: rar, stores: [] });
    h = body_();
    assert(!h.includes('<img'), 'レアリティ名がエスケープされていない: ' + h);
    assert(!h.includes('javascript:'), '危険なURLが出力された');
    assert(!h.includes('メルカリで見る'), '安全でないURLのときはリンクを出さない');
  }
  await loadWith(resp({}, { search_url: 'https://jp.mercari.com/search?keyword="onmouseover="x' }), { rarity: '', stores: [] });
  assert(!/href="[^"]*"[^>]*onmouseover=/.test(body_()) && !body_().includes('"onmouseover="'), 'URLが属性から脱出');

  // 8) 出品なし
  await loadWith(resp({}, { fetched_count: 0, num_found: 0 }), { rarity: '', stores: [] });
  assert(body_().includes('出品なし') && !body_().includes('条件に合う') && !body_().includes('取得できませんでした'));
  await loadWith(resp({}), { rarity: '', stores: [] });
  assert(body_().includes('条件に合う出品なし'));

  // 9) 取得失敗(ok:false / HTTPエラー / 通信例外)は0件と区別した文言。パネルは出す
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
  // 失敗後のレアリティ切替で古いデータを描き直さない
  api.setView({ rarity: 'ウルトラ', stores: [] });
  assert(body_().includes('メルカリの情報を取得できませんでした') && !body_().includes('class="scard'));

  // 10) 未知のカード(404)は枠ごと出さない
  await loadWith({ ok: false, error: 'unknown_card' }, { rarity: '', stores: [] }, 404);
  assert(classes.has('hidden') && body_() === '');

  // 11) 古い応答は捨てる(カード切替・clear後に届いた応答)
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
  // clear: 閉じて初期化。以後の古い応答は描画しない
  box.open = true;
  api.load('C');
  const c = pending.shift();
  api.clear();
  assert(box.open === false);
  reply(c, one(222));
  await tick(); await tick();
  assert(classes.has('hidden') && body_() === '', 'clear後の応答が描画された');
  // clear後にsetViewしてもデータは無いので描かれない
  api.setView({ rarity: 'ウルトラ', stores: [] });
  assert(body_() === '');

  // 12) 店舗検索の途中（pending）: 確認できたレアリティだけ出し、残りは「確認中」と注記する（「店舗に無い」とは言わない）
  await loadWith(resp(BY), { rarity: '', stores: [], pending: true });
  h = body_();
  assert(!classes.has('hidden'), '店舗検索中でも枠は出る');
  assert.equal(box.open, false, '閉じた見出しのまま');
  assert(!h.includes('mref-grid') && !h.includes('class="scard'), 'D空: 一覧は出さない: ' + h);
  assert(h.includes('店舗の検索中のため確認中のレアリティ（シークレット・ウルトラ・ノーマル・ほか1種）があります'), h);
  assert(h.includes('メルカリで見る') && !h.includes('出品なし') && !h.includes('店舗に無い') && !h.includes('表示していません'));
  // 一部の店舗が返った: その店舗のレアリティだけ表示（1種ならタイル）。残りは確認中
  api.setView({ rarity: '', stores: ['ウルトラ'], pending: true });
  assert.equal(pending.length, 0, '描き直しで再取得しない');
  h = body_();
  assert(h.includes('ウルトラ・出品5件の最安') && !h.includes('¥640') && !h.includes('¥300'), h);
  assert(h.includes('店舗の検索中のため確認中のレアリティ（シークレット・ノーマル・レリーフ）があります'), h);
  assert(!h.includes('店舗に無い'));
  api.setView({ rarity: '', stores: ['ウルトラ', 'シークレット'], pending: true });
  h = body_();
  assert(h.includes('mref-grid') && h.includes('>ウルトラ<') && h.includes('>シークレット<') && !h.includes('>ノーマル<'), h);
  assert(h.includes('店舗の検索中のため確認中のレアリティ（ノーマル・レリーフ）があります'));
  // 開いたまま描き直してもパネルを閉じない
  box.open = true;
  api.setView({ rarity: '', stores: ['ウルトラ'], pending: true });
  assert.equal(box.open, true, '描き直しで閉じない');
  // 確定（pending 解除）: 従来どおり「店舗に無い」
  api.setView({ rarity: 'ウルトラ', stores: ['ウルトラ', 'シークレット'] });
  h = body_();
  assert(h.includes('店舗に無いレアリティ（ノーマル・レリーフ）は表示していません') && !h.includes('確認中'), h);
  assert.equal(box.open, true);
  // pending でも全レアリティが確認中でメルカリが出品なしなら「出品なし」と言う（確認中の注記は出さない）
  await loadWith(resp({}, { fetched_count: 0, num_found: 0 }), { rarity: '', stores: [], pending: true });
  assert(body_().includes('出品なし') && !body_().includes('確認中'));
  // XSS: pending の注記でもレアリティ名はエスケープ
  await loadWith(resp(byEvil), { rarity: '', stores: [], pending: true });
  assert(!body_().includes('<img') && body_().includes('確認中のレアリティ'), body_());
  // 同じ内容の描き直しでは DOM を書き換えない
  let writes = 0, inner = body.innerHTML;
  Object.defineProperty(body, 'innerHTML', { configurable: true, get: () => inner, set: (v) => { writes++; inner = v; } });
  api.setView({ rarity: '', stores: [], pending: true });
  api.setView({ rarity: '', stores: [], pending: true });
  assert(writes <= 1, '同一HTMLを再代入しない: ' + writes);
  delete body.innerHTML; body.innerHTML = inner;

  // 13) 取得中に pending のまま応答が届く（load → 応答）。pending 中の 404 は枠ごと消える
  api.setView({ rarity: '', stores: [], pending: true });
  api.load('灰流うらら');
  reply(pending.shift(), { ok: false, error: 'unknown_card' }, 404);
  await tick(); await tick();
  assert(classes.has('hidden') && body_() === '');

  console.log('mercari_ref_check: OK');
})().catch((e) => { console.error('FAIL', e); process.exit(1); });
