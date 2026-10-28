// カードの検索結果（2026-10-21 案C）の実コード検証。index-main.js の実関数を vm で実行する。
// レアリティ欄の並びと値段・出品一覧の「店ごとの最安／すべて」・見出しと件数・空の案内・最安の印（J-5）・
// 型番の出し分け（K-1/L-1）・イラスト違いの札・レアリティが1種類のカード・一覧の切り替え・イラストの切り替え（10-28）。
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert/strict');
const root = path.join(__dirname, '../..');
const src = fs.readFileSync(path.join(root, 'static/js/index-main.js'), 'utf8');
function block(start, end) {
  const a = src.indexOf(start), b = src.indexOf(end, a + start.length);
  assert(a >= 0 && b > a, '抽出失敗: ' + start);
  return src.slice(a, b);
}
class FakeTextDiv {
  constructor() { this._t = ''; }
  set textContent(v) { this._t = String(v == null ? '' : v); }
  get innerHTML() { return this._t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
}
function element(extra) {
  const classes = new Set();
  return Object.assign({
    innerHTML: '', textContent: '', checked: false, value: '', style: {}, dataset: {}, attrs: {},
    classList: {
      add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
      toggle(c, on) { if (on) classes.add(c); else classes.delete(c); },
    },
    setAttribute(k, v) { this.attrs[k] = v; },
  }, extra || {});
}
const els = new Map();
const get = id => { if (!els.has(id)) els.set(id, element()); return els.get(id); };
const segBtns = [element({ dataset: { v: 'shop' } }), element({ dataset: { v: 'all' } })];
const ga = [];
const sandbox = {
  console, URL, Number, Object, encodeURIComponent,
  document: {
    getElementById: get, createElement: () => new FakeTextDiv(),
    querySelectorAll: sel => (sel === '.cr-seg-btn' ? segBtns : []),
  },
  location: { origin: 'https://example.test' }, window: {}, wishBtnHtml: () => '<button class="wish-add">+ 候補</button>',
  _ga: (name, p) => ga.push([name, p]),
};
vm.createContext(sandbox);
vm.runInContext(`
let D=[],sort={k:'price',a:true},rarityFilter='',artFilter='',cardImgSmall='',tableShowCount=20,listView='shop',_currentCardName='灰流うらら';
let _buyInlineD=null,_buyInlineFetched=false,_buyInlineFailedShops=[];
let _affConfig={};
const TABLE_PAGE_SIZE=20;
`, sandbox);
vm.runInContext([
  block('function esc(', 'function rankingLinkClick('),
  block('function affLink(', 'function copyShareText('),
  block('function _summaryShopItems(', '// ── C-2'),
  block('function _looksLikeCardCode(', '// U-1: 販売テーブル'),
  block('function _updateHeroSummary(', 'function renderTable('),
  block('function renderTable(', '// B-4: 「もっと見る」'),
  block('// 売り切れ表示トグル', '// 検索結果テーブルの行クリック'),
].join('\n'), sandbox);
const run = c => vm.runInContext(c, sandbox);
const count = (h, s) => h.split(s).length - 1;
const row = (shop, price, rarity, extra) => Object.assign(
  { shop, name: '灰流うらら', price, rarity, code: 'QCAC-JP050', condition: '-', url: 'https://shop.example/' + encodeURIComponent(shop) + price, sold_out: false, stock: 1 }, extra || {});
get('q').value = '灰流うらら';

sandbox.D = [];
run(`D=${JSON.stringify([
  row('まんぞく屋', 180, 'ウルトラ'),
  row('カーナベル', 280, 'ウルトラ', { code: 'TTP1', condition: '状態:SA' }),
  row('カーナベル', 400, 'ウルトラ'),
  row('遊々亭', 320, 'ウルトラ', { name: '灰流うらら(既存イラスト)', code: 'RC03-JP010' }),
  row('遊々亭', 500, 'ウルトラ', { sold_out: true, stock: 0 }),
  row('トレコロCB', 220, 'ノーマル'),
  row('トレコロCB', 71840, '20thシークレット'),
  row('カードラッシュ', 9000, 'プリシク', { sold_out: true, stock: 0 }),
])}`);

// 1) レアリティ欄: 「すべて」が先頭、在庫ありの最安の安い順、在庫が無いレアリティは後ろ（売切N件）
let h = run(`_rarityTilesHtml(['20thシークレット','ウルトラ','ノーマル','プリシク'],'ウルトラ')`);
const order = [...h.matchAll(/<span class="cr-rn">([^<]+)<\/span>/g)].map(m => m[1]);
assert.deepEqual(order, ['すべて', 'ウルトラ', 'ノーマル', '20thシークレット', 'プリシク']);
assert(h.includes('<b>¥180〜</b><i>6件</i>'), '「すべて」は全体の在庫ありの最安〜と在庫ありの件数');
assert(h.includes('<span class="cr-rn">ウルトラ</span><span class="cr-rp"><b>¥180</b><i>4件</i>'));
assert(h.includes('<b>¥71,840</b>'));
assert(h.includes('<span class="cr-rn">プリシク</span><span class="cr-rp"><b>在庫なし</b><i>売切1件</i>'));
assert.equal(count(h, 'class="cr-rtile active"'), 1);
assert(h.includes('data-r="ウルトラ" aria-pressed="true"'));
assert(h.includes(`onclick="setRarityFilter('20thシークレット')"`));
// 名前の悪い値はエスケープ
h = run(`_rarityTilesHtml(['<img src=x>'],'')`);
assert(!h.includes('<img src=x>') && h.includes('&lt;img src=x&gt;'));

// 2) 店ごとの最安（既定）: 店ごとに在庫ありの一番安い出品・価格順・最安の印は先頭だけ
run(`rarityFilter='ウルトラ'; listView='shop'; renderSummary(); renderTable(); _updateHeroSummary()`);
h = get('tbody').innerHTML;
const shops = [...h.matchAll(/<span class="cr-shop-name">([^<]+)<\/span>/g)].map(m => m[1]);
assert.deepEqual(shops, ['まんぞく屋', 'カーナベル', '遊々亭'], '店ごとに1行・価格順・売り切れは出さない');
assert.equal(count(h, 'cr-best-tag'), 1);
assert(h.indexOf('cr-best-tag') < h.indexOf('カーナベル'), '最安の印は先頭の行');
assert.equal(get('crListTitle').textContent, 'ウルトラの出品');
assert.equal(get('crCountShop').textContent, 3);
assert.equal(get('crCountAll').textContent, 4, 'すべて＝在庫ありの件数');
assert.equal(get('soToggle').classList.contains('hidden'), true, '売り切れの切替は「すべて」のときだけ');
assert.equal(get('tblMore').innerHTML, '', '店ごとの最安には「もっと見る」を出さない');
// K-1/L-1: 型番らしくない値（TTP1）は出さない
assert(!h.includes('TTP1'));
assert(h.includes('<td class="cr-code">QCAC-JP050</td>'));
// イラスト違いだけ札。同じ名前は札にしない
assert(h.includes('<span class="cr-tag">既存イラスト</span>'));
assert(!h.includes('<span class="cr-tag">灰流うらら</span>'));
// 状態の「-」は空、それ以外は出す
assert(h.includes('<td class="cr-cond">状態:SA</td>') && h.includes('<td class="cr-cond"></td>'));
// レアリティを選んでいるときはレアリティの札を付けない
assert(!h.includes('<span class="cr-tag">ウルトラ</span>'));
// ヒーローの最安は一覧の先頭と同じ（F-14）
assert.equal(get('heroBest').innerHTML,
  '<span class="cr-hb-best">最安</span><span class="cr-best-price">¥180</span><span class="cr-hb-rar">ウルトラ</span><span class="cr-best-shop">まんぞく屋・QCAC-JP050</span>');

// 3) すべて: 同じレアリティの在庫ありの出品を全部。売り切れの切替が見える
run(`setListView('all')`);
assert.equal(JSON.stringify(ga[ga.length - 1]), JSON.stringify(['list_view_change', { view: 'all' }]));
assert.equal(segBtns[1].classList.contains('active'), true);
assert.equal(segBtns[1].attrs['aria-pressed'], 'true');
assert.equal(segBtns[0].attrs['aria-pressed'], 'false');
h = get('tbody').innerHTML;
assert.equal(count(h, '<tr '), 4);
assert.equal(get('soToggle').classList.contains('hidden'), false);
assert(get('tblMore').innerHTML.includes('4件中 4件を表示'));
run(`setListView('all')`);
assert.equal(ga.filter(g => g[0] === 'list_view_change').length, 1, '同じ表示を押しても計測しない');
// 売り切れも表示
get('soCheck').checked = true;
run(`_resetTableAndRender()`);
assert.equal(get('crCountAll').textContent, 5);
assert(get('tbody').innerHTML.includes('class="sold-out'));
get('soCheck').checked = false;

// 4) 「すべて」のレアリティ: 最安の印を付けず（J-5）、行にレアリティの札
run(`rarityFilter=''; listView='shop'; renderSummary(); renderTable(); _updateHeroSummary()`);
h = get('tbody').innerHTML;
assert.equal(count(h, 'cr-best-tag'), 0, '別レアリティ同士で最安と言い切らない');
assert(h.includes('<span class="cr-tag">ウルトラ</span>') && h.includes('<span class="cr-tag">ノーマル</span>'));
assert.equal(get('crListTitle').textContent, 'すべてのレアリティの出品');
assert(get('heroBest').innerHTML.includes('<span class="cr-hb-rar">ウルトラ</span>'), '「すべて」でも最安の出品のレアリティを札に出す');

// 5) 在庫切れのレアリティ: 案内を出す（F-10）。店ごとの最安からは「すべて」への道も書く
run(`rarityFilter='プリシク'; listView='shop'; renderSummary(); renderTable(); _updateHeroSummary()`);
assert.equal(get('tbody').innerHTML, '');
assert.equal(get('crListNote').textContent, 'このレアリティは在庫切れです（売切1件）。「すべて」の「売り切れも表示」で見られます');
assert.equal(get('crListNote').classList.contains('hidden'), false);
assert.equal(get('heroBest').innerHTML, '<span class="cr-best-none">在庫なし</span><span class="cr-hb-rar">プリシク</span>');
run(`listView='all'; renderSummary()`);
assert.equal(get('crListNote').textContent, 'このレアリティは在庫切れです（売切1件）。「売り切れも表示」で見られます');
get('soCheck').checked = true;
run(`renderSummary()`);
assert.equal(get('crListNote').classList.contains('hidden'), true, '売り切れを出しているときは案内を隠す');
get('soCheck').checked = false;
run(`rarityFilter='ウルトラ'; listView='shop'; renderSummary()`);
assert.equal(get('crListNote').textContent, '');
assert.equal(get('crListNote').classList.contains('hidden'), true);

// 6) レアリティが1種類のカード: そのレアリティとして見せ、最安の印も付ける（同じレアリティ同士）
run(`D=${JSON.stringify([row('まんぞく屋', 300, 'ノーマル'), row('遊々亭', 350, 'ノーマル')])}; rarityFilter=''; renderSummary(); renderTable(); _updateHeroSummary()`);
h = get('tbody').innerHTML;
assert.equal(get('crListTitle').textContent, 'ノーマルの出品');
assert(get('heroBest').innerHTML.includes('<span class="cr-hb-rar">ノーマル</span>'));
assert.equal(count(h, 'cr-best-tag'), 1);
assert(!h.includes('<span class="cr-tag">ノーマル</span>'), '同じレアリティの札をくり返さない');

// 7) イラスト違いの札の取り出し
assert.equal(run(`_variantLabel('灰流うらら','灰流うらら')`), '');
assert.equal(run(`_variantLabel('灰流うらら（立ち）','灰流うらら')`), '立ち');
assert.equal(run(`_variantLabel('灰流うらら(既存イラスト)','灰流うらら')`), '既存イラスト');
assert.equal(run(`_variantLabel('【新】灰流うらら(座り)','灰流うらら')`), '【新】灰流うらら(座り)');
assert.equal(run(`_variantLabel('x','')`), '');

// 8) 購入候補ボタンの文言はレアリティに追従
run(`rarityFilter='ウルトラ'; _updateRarityBanner()`);
assert.equal(get('heroWishAdd').textContent, 'ウルトラを購入候補へ');
run(`rarityFilter=''; _updateRarityBanner()`);
assert.equal(get('heroWishAdd').textContent, 'レアリティ未指定で購入候補へ');

// 10) イラストの切り替え（2026-10-28）: 絵が2種類以上あるレアリティだけ。既定は元のイラスト。最安・一覧・件数をその絵で比べる
run(`D=${JSON.stringify([
  row('トレコロCB', 420, 'シークレット', { art: '元のイラスト', name: '灰流うらら[両腕開き]' }),
  row('遊々亭', 500, 'シークレット', { art: '元のイラスト' }),
  row('遊々亭', 980, 'シークレット', { art: 'イラスト違い', name: '灰流うらら(イラスト違い版)' }),
  row('カードラッシュ', 1120, 'シークレット', { art: 'イラスト違い', name: '灰流うらら(座り)' }),
  row('まんぞく屋', 300, 'シークレット', { art: '' }),
  row('カードラッシュ', 280, 'シークレット', { art: 'アジア版', edition: 'アジア版', name: '☆アジア☆灰流うらら(立ち)' }),
  row('まんぞく屋', 180, 'ウルトラ', { art: '' }),
  row('遊々亭', 320, 'ウルトラ', { art: '元のイラスト' }),
])}`);
assert.deepEqual(JSON.parse(run(`JSON.stringify(_artsOf('シークレット'))`)), ['元のイラスト', 'イラスト違い'],
  '判別できない行・言語版は日本版の絵の種類に数えない');
assert.deepEqual(JSON.parse(run(`JSON.stringify(_editionsOf('シークレット'))`)), ['アジア版']);
// レアリティ欄の値段は、選んだときに最初に見せる絵（元のイラスト）で数える（上の最安とそろえる）
h = run(`_rarityTilesHtml(['シークレット','ウルトラ'],'シークレット')`);
assert(h.includes('<span class="cr-rn">シークレット</span><span class="cr-rp"><b>¥420</b><i>2件</i>'), 'アジア版・判別できない行・イラスト違いを枠の最安に入れない');
assert(h.includes('<span class="cr-rn">ウルトラ</span><span class="cr-rp"><b>¥180</b><i>2件</i>'), '絵の切り替えが出ないレアリティは今までどおり');
assert.equal(run(`_defaultArt('シークレット')`), '元のイラスト');
assert.equal(run(`_defaultArt('ウルトラ')`), '', '絵が1種類なら絞らない');
assert.equal(run(`_defaultArt('')`), '');
run(`rarityFilter='シークレット'; artFilter=_defaultArt('シークレット'); listView='shop'; _renderArtFilter(); renderSummary(); renderTable(); _updateHeroSummary()`);
h = get('artFilter').innerHTML;
assert.deepEqual([...h.matchAll(/<span class="cr-rn">([^<]+)<\/span>/g)].map(m => m[1]), ['すべての絵（日本版）', '元のイラスト', 'イラスト違い', 'アジア版']);
assert(h.includes('<span class="cr-rn">すべての絵（日本版）</span><span class="cr-rp"><b>¥300</b><i>5件</i>'), '「すべての絵」は判別できない行を含み、言語版は含まない');
assert(h.includes('<span class="cr-rn">アジア版</span><span class="cr-rp"><b>¥280</b><i>1件</i>'));
assert(h.includes('<span class="cr-rn">イラスト違い</span><span class="cr-rp"><b>¥980</b><i>2件</i>'));
assert(h.includes(`data-art="元のイラスト" aria-pressed="true"`) && h.includes(`onclick="setArtFilter('イラスト違い')"`));
assert.equal(get('artFilter').classList.contains('hidden'), false);
assert.deepEqual([...get('tbody').innerHTML.matchAll(/<span class="cr-shop-name">([^<]+)<\/span>/g)].map(m => m[1]), ['トレコロCB', '遊々亭'],
  '元のイラストだけ。判別できないまんぞく屋は出さない');
assert.equal(get('crListTitle').textContent, 'シークレット・元のイラストの出品');
assert.equal(get('crCountAll').textContent, 2);
assert(get('heroBest').innerHTML.includes('<span class="cr-best-price">¥420</span><span class="cr-hb-rar">シークレット・元のイラスト</span>'));
run(`setArtFilter('イラスト違い')`);
assert.equal(JSON.stringify(ga[ga.length - 1]), JSON.stringify(['art_filter_change', { art: 'イラスト違い', from_art: '元のイラスト', rarity: 'シークレット' }]));
assert(get('heroBest').innerHTML.includes('<span class="cr-best-price">¥980</span><span class="cr-hb-rar">シークレット・イラスト違い</span>'));
assert.equal(get('crCountShop').textContent, 2);
run(`setArtFilter('')`);
assert.equal(get('crListTitle').textContent, 'シークレットの出品');
assert(get('heroBest').innerHTML.includes('<span class="cr-best-price">¥300</span>'), 'すべての絵は判別できない行も比べ、言語版は比べない');
run(`setArtFilter('アジア版')`);
assert(get('heroBest').innerHTML.includes('<span class="cr-best-price">¥280</span><span class="cr-hb-rar">シークレット・アジア版</span>'), '言語版は選んだときだけ');
assert.deepEqual([...get('tbody').innerHTML.matchAll(/<span class="cr-shop-name">([^<]+)<\/span>/g)].map(m => m[1]), ['カードラッシュ']);
run(`artFilter=''`);
// 「すべて」の枠は日本版で数える
h = run(`_rarityTilesHtml(['シークレット','ウルトラ'],'')`);
assert(h.includes('<span class="cr-rn">すべて</span><span class="cr-rp"><b>¥180〜</b><i>7件</i>'));
// 選んだ絵が在庫切れなら、その絵の在庫切れと書く（レアリティごと在庫切れとは言わない）
run(`D.forEach(r=>{if(r.art==='元のイラスト'){r.sold_out=true;r.stock=0;}}); artFilter='元のイラスト'; listView='shop'; renderSummary()`);
assert(get('crListNote').textContent.startsWith('この絵（元のイラスト）は在庫切れです'));
// 元のイラストに在庫が無いときは、最初から絞らない
assert.equal(run(`_defaultArt('シークレット')`), '');
// 言語版を日本版の最安に混ぜない（10-28 レビュー指摘 A）。日本版の行が (a) 判別できない行だけ (b) 元のイラストが売り切れ
// (c) イラスト違いだけ、のどの場合も、既定の最安・レアリティ欄に言語版を入れず、切り替え欄で言語版を選べる
const asia = row('カードラッシュ', 100, 'シークレット', { art: 'アジア版', edition: 'アジア版', name: '☆アジア☆灰流うらら' });
for (const [label, jp] of [
  ['(a)', [row('まんぞく屋', 500, 'シークレット', { art: '' })]],
  ['(b)', [row('遊々亭', 500, 'シークレット', { art: '元のイラスト', sold_out: true, stock: 0 }), row('遊々亭', 900, 'シークレット', { art: 'イラスト違い' })]],
  ['(c)', [row('遊々亭', 900, 'シークレット', { art: 'イラスト違い' })]],
]) {
  run(`D=${JSON.stringify([...jp, asia])}; rarityFilter='シークレット'; artFilter=_defaultArt('シークレット'); listView='shop'; _renderArtFilter(); renderSummary(); renderTable(); _updateHeroSummary()`);
  assert(!get('heroBest').innerHTML.includes('¥100'), label + ' 既定の最安に言語版を出さない');
  assert(!run(`_rarityTilesHtml(['シークレット'],'シークレット')`).includes('¥100'), label + ' レアリティ欄の値段に言語版を入れない');
  assert(get('artFilter').innerHTML.includes(`onclick="setArtFilter('アジア版')"`), label + ' 言語版は切り替え欄で選べる');
}
// 絵を書き分ける店に判別できない行がある（絵が3種類以上のカード等）ときは、元のイラストに絞らない。まんぞく屋は数えない
run(`D=${JSON.stringify([
  row('遊々亭', 500, '25thシークレット', { art: '元のイラスト' }),
  row('遊々亭', 900, '25thシークレット', { art: 'イラスト違い' }),
  row('トレコロCB', 700, '25thシークレット', { art: '', name: 'ブラック・マジシャン[腕組右手杖]' }),
])}`);
assert.equal(run(`_defaultArt('25thシークレット')`), '');
run(`D=D.filter(r=>r.shop!=='トレコロCB').concat([${JSON.stringify(row('まんぞく屋', 300, '25thシークレット', { art: '' }))}])`);
assert.equal(run(`_defaultArt('25thシークレット')`), '元のイラスト');

// 日本版の出品が無く言語版だけのレアリティは、最初からその言語版を選んだ状態
run(`D=${JSON.stringify([asia])}; rarityFilter='シークレット'; artFilter=_defaultArt('シークレット'); _renderArtFilter(); _updateHeroSummary()`);
assert.equal(run('artFilter'), 'アジア版');
assert(get('heroBest').innerHTML.includes('<span class="cr-best-price">¥100</span><span class="cr-hb-rar">シークレット・アジア版</span>'));

// 絵が1種類のレアリティでは欄を隠し、前の絵の絞り込みを持ち越さない
run(`D=${JSON.stringify([row('まんぞく屋', 180, 'ウルトラ', { art: '' }), row('遊々亭', 320, 'ウルトラ', { art: '元のイラスト' })])}; artFilter='イラスト違い'; rarityFilter='ウルトラ'; _renderArtFilter()`);
assert.equal(get('artFilter').classList.contains('hidden'), true);
assert.equal(run('artFilter'), '');

// 9) renderAll は新しいカードで「店ごとの最安」・価格順・売り切れを隠した状態に戻す（配線の静的確認）
const ra = block('function renderAll(', 'function _summaryShopItems(');
assert(/listView='shop'/.test(ra) && /sort=\{k:'price',a:true\}/.test(ra) && /soCheck\.checked=false/.test(ra));
console.log('card_result_check: OK');
