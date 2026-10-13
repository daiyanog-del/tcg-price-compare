// アフィリエイト広告表記（rel=sponsored・PRバッジ・注記）の実コード検証。
// index-main.js の実関数を vm で実行し、_affConfig がダミー有り／空の両方で
// 3箇所（店舗サマリー・買取カード・全件表）の出力を確認する。
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
function element() {
  const classes = new Set();
  return {
    innerHTML: '', checked: false, style: {},
    classList: {
      add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
      toggle(c, on) { if (on) classes.add(c); else classes.delete(c); },
    },
  };
}
const els = new Map();
const get = id => { if (!els.has(id)) els.set(id, element()); return els.get(id); };
const sandbox = {
  console, URL, Number, Object, encodeURIComponent,
  document: { getElementById: get, createElement: () => new FakeTextDiv(), querySelectorAll: () => [] },
  location: { origin: 'https://example.test' }, window: {}, wishBtnHtml: () => '',
};
vm.createContext(sandbox);
vm.runInContext(`
let D=[],sort={k:'price',a:true},rarityFilter='',cardImgSmall='',tableShowCount=20;
let _buyInlineD=null,_buyInlineFetched=false,_buyInlineFailedShops=[];
let _affConfig={};
`, sandbox);
vm.runInContext([
  block('function esc(', 'function rankingLinkClick('),
  block('function affLink(', 'function copyShareText('),
  block('function _summaryShopItems(', '// ── C-2'),
  block('function _buyInlineFilteredData(', '// S-1:'),
  block('function _looksLikeCardCode(', '// U-1: 販売テーブル'),
  block('function _renderBuyInline(', '// F-5/F-14'),
  block('function renderTable(', 'function _showMoreRows('),
].join('\n'), sandbox);
const run = c => vm.runInContext(c, sandbox);

const NK = 'https://www.cardnavi.example/item/1?a=1&b=2';
const SR = 'https://www.suruga-ya.jp/product/detail/1';
const TC = 'https://www.torecolo.example/item/9';
function setData() {
  run(`D=[
    {shop:'カーナベル',name:'灰流うらら',price:100,rarity:'ウルトラ',code:'AB-JP001',condition:'-',url:'${NK}',sold_out:false,stock:1},
    {shop:'駿河屋',name:'灰流うらら',price:120,rarity:'ウルトラ',code:'AB-JP001',condition:'-',url:'${SR}',sold_out:false,stock:1},
    {shop:'トレコロ',name:'灰流うらら',price:130,rarity:'ウルトラ',code:'AB-JP001',condition:'-',url:'${TC}',sold_out:false,stock:1},
    {shop:'<b>悪い店</b>',name:'x',price:140,rarity:'ウルトラ',code:'',condition:'-',url:'',sold_out:false,stock:1}];
  _buyInlineFetched=true;
  _buyInlineD=[
    {shop:'カーナベル',name:'灰流うらら',price:50,rarity:'ウルトラ',code:'AB-JP001',condition:'-',url:'${NK}'},
    {shop:'トレコロ',name:'灰流うらら',price:40,rarity:'ウルトラ',code:'AB-JP001',condition:'-',url:'${TC}'}];`);
}
const noteHidden = () => get('sumAffNote').classList.contains('hidden');
const count = (h, s) => h.split(s).length - 1;
const PR = '<span class="aff-pr">PR</span>';
const FULL = `_affConfig={'カーナベル':{sell:'dummyA8',buy:'dummyA8'},'駿河屋':{user_id:'9999'}}`;

get('sumAffNote').classList.add('hidden'); // index.html の初期状態
setData();

// 1) ヘルパー
run(`_affConfig={}`);
assert.equal(run(`_isAffUrl('${NK}', affLink('${NK}','カーナベル','sell'))`), false, '設定空は直リンク＝非アフィリエイト');
assert.equal(run(`_isAffUrl('', affLink('','カーナベル','sell'))`), false, 'URL空は非アフィリエイト');
assert.equal(run(`_isAffUrl('javascript:alert(1)', affLink('javascript:alert(1)','カーナベル','sell'))`), false);
run(FULL);
assert.equal(run(`_isAffUrl('${NK}', affLink('${NK}','カーナベル','sell'))`), true);
assert.equal(run(`_isAffUrl('${SR}', affLink('${SR}','駿河屋','sell'))`), true);
assert.equal(run(`_isAffUrl('${TC}', affLink('${TC}','トレコロ','sell'))`), false, '設定の無い店は非アフィリエイト');
run(`_affConfig={'カーナベル':{sell:'dummyA8'}}`);
assert.equal(run(`_isAffUrl('${NK}', affLink('${NK}','カーナベル','buy'))`), false, '買取用IDが無ければ変換されない');
assert.equal(run(`_affRel(true)`), 'sponsored noopener');
assert.equal(run(`_affRel(false)`), 'noopener nofollow');

// 2) 店舗サマリー: ダミー設定あり
run(FULL);
run(`renderSummary()`);
let h = get('sumCards').innerHTML;
assert(h.includes('px.a8.net') && h.includes('affiliate.suruga-ya.jp'));
assert.equal(count(h, PR), 2, 'カーナベルと駿河屋にだけPR');
assert(h.includes('<div class="slabel">カーナベル' + PR + '</div>'));
assert(h.includes('<div class="slabel">駿河屋' + PR + '</div>'));
assert(h.includes('<div class="slabel">トレコロ</div>'), 'トレコロは直リンクでPRなし');
assert.equal(count(h, 'rel="sponsored noopener"'), 4, '変換2店×(名前+価格)');
assert.equal(count(h, 'rel="noopener nofollow"'), 2, 'トレコロ×(名前+価格)');
assert(!h.includes('<b>悪い店</b>') && h.includes('&lt;b&gt;悪い店&lt;/b&gt;'), '店名はesc済み');
assert.equal(noteHidden(), false, '注記表示');

// 3) 設定が空: 直リンクのみ→PRなし・注記非表示。再描画で正しく切り替わる
run(`_affConfig={}`);
run(`renderSummary()`);
h = get('sumCards').innerHTML;
assert.equal(count(h, 'aff-pr'), 0);
assert.equal(count(h, 'sponsored'), 0);
assert(h.includes('rel="noopener nofollow"'));
assert.equal(noteHidden(), true, '直リンクのみなら注記は隠す');
run(`_affConfig={'カーナベル':{sell:'dummyA8'}}`);
run(`renderSummary()`);
assert.equal(noteHidden(), false, '再描画で再び表示');
// レアリティ切替で変換店が消える場合（トレコロのみのレアリティ）
run(`D.push({shop:'トレコロ',name:'y',price:10,rarity:'ノーマル',code:'',condition:'-',url:'${TC}',sold_out:false,stock:1}); rarityFilter='ノーマル'`);
run(`renderSummary()`);
assert.equal(noteHidden(), true, '切替先に変換リンクが無ければ隠す');
// 在庫切れ表示でも隠す（直前が表示状態だった場合）
run(`rarityFilter=''`);
run(`renderSummary()`);
assert.equal(noteHidden(), false);
run(`rarityFilter='存在しない'`);
run(`renderSummary()`);
assert.equal(noteHidden(), true, '在庫切れ表示では隠す');
run(`D.pop(); rarityFilter=''`);

// 4) 買取カード
run(`_affConfig={'カーナベル':{sell:'dummyA8',buy:'dummyA8'}}`);
run(`_renderBuyInline()`);
h = get('buyInlineBody').innerHTML;
assert(h.includes('<div class="slabel">カーナベル' + PR + '</div>'));
assert(h.includes('<div class="slabel">トレコロ</div>'));
assert.equal(count(h, PR), 1);
assert(h.includes('rel="sponsored noopener"') && h.includes('rel="noopener nofollow"'));
assert(h.includes('<div class="aff-note">一部のリンクは広告（PR）を含みます</div>'));
assert(h.indexOf('aff-note') > h.indexOf('class="summary"'), '注記はカード一覧の後ろ');
run(`_affConfig={}`);
run(`_renderBuyInline()`);
h = get('buyInlineBody').innerHTML;
assert(!h.includes('aff-note') && !h.includes('aff-pr') && !h.includes('sponsored'));

// 5) 全件表
run(`_affConfig={'カーナベル':{sell:'dummyA8'},'駿河屋':{user_id:'9999'}}`);
run(`renderTable()`);
h = get('tbody').innerHTML;
assert(h.includes('<td data-label="店舗">カーナベル' + PR + '</td>'));
assert(h.includes('<td data-label="店舗">駿河屋' + PR + '</td>'));
assert(h.includes('<td data-label="店舗">トレコロ</td>'));
assert(h.includes('<td data-label="店舗">&lt;b&gt;悪い店&lt;/b&gt;</td>'), 'URL無し行はPRなし・esc済み');
assert.equal(count(h, 'class="row-link"'), 3);
assert.equal(count(h, 'rel="sponsored noopener"'), 2);
assert.equal(count(h, 'rel="noopener nofollow"'), 1);
assert.equal(count(h, 'data-href="https://px.a8.net') + count(h, 'data-href="https://affiliate.suruga-ya.jp'), 2);
run(`_affConfig={}`);
run(`renderTable()`);
h = get('tbody').innerHTML;
assert(!h.includes('aff-pr') && !h.includes('sponsored'));

// 6) 配線: index.html に注記要素とCSSがある（兄弟要素・サマリー直後）
const html = fs.readFileSync(path.join(root, 'templates/index.html'), 'utf8');
assert(html.includes('<div class="aff-note hidden" id="sumAffNote">一部のリンクは広告（PR）を含みます</div>'));
assert(html.indexOf('id="sumCards"') < html.indexOf('id="sumAffNote"') && html.indexOf('id="sumAffNote"') < html.indexOf('id="buyInline"'));
assert(html.includes('.aff-pr{') && html.includes('.aff-note{'));
console.log('affiliate_label_check: OK');
