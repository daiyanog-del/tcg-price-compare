// 出品一覧の行の小さな商品画像（2026-11-01・_showRowThumbs / _rowThumbHtml）の実コード検証。
// index-main.js の実関数を vm で実行し、絵が分かれるレアリティを選んだときだけ画像が出ることを確かめる。
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
  return { innerHTML: '', checked: true, style: {}, classList: { add() {}, remove() {}, toggle() {}, contains: () => false } };
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
let D=[],sort={k:'price',a:true},rarityFilter='',artFilter='',cardImgSmall='',tableShowCount=20,listView='all',_currentCardName='ブラック・マジシャン';
let _buyInlineD=null,_buyInlineFetched=false,_buyInlineFailedShops=[];
let _affConfig={};
`, sandbox);
vm.runInContext([
  block('function esc(', 'function rankingLinkClick('),
  block('function affLink(', 'function copyShareText('),
  block('function _summaryShopItems(', '// ── C-2'),
  block('function _looksLikeCardCode(', '// U-1: 販売テーブル'),
  block('function _shownRarity(', 'function renderTable('),
  block('function renderTable(', '// B-4: 「もっと見る」'),
].join('\n'), sandbox);
const run = c => vm.runInContext(c, sandbox);
const body = () => get('tbody').innerHTML;
const count = (h, s) => h.split(s).length - 1;

run(`D=[
  {shop:'カーナベル',name:'ブラック・マジシャン(原作絵)',price:1480,rarity:'シークレット',code:'QCAC-JP018',condition:'-',url:'https://ka.example/1',image:'https://img.example/a.jpg?x=1&y="2"',sold_out:false,stock:1,art:'',edition:''},
  {shop:'カードラボ',name:'ブラック・マジシャン(通常イラスト/石板版)',price:80,rarity:'シークレット',code:'QCAC-JP018',condition:'-',url:'https://cl.example/1',image:'https://img.example/b.jpg',sold_out:false,stock:1,art:'元のイラスト',edition:''},
  {shop:'遊々亭',name:'ブラック・マジシャン(イラスト違い)',price:300,rarity:'シークレット',code:'PAC1-JP004',condition:'-',url:'https://yy.example/1',image:'',sold_out:false,stock:1,art:'イラスト違い',edition:''},
  {shop:'まんぞく屋',name:'ブラック・マジシャン',price:200,rarity:'ノーマル',code:'LB-01',condition:'-',url:'https://mz.example/1',image:'javascript:alert(1)',sold_out:false,stock:1,art:'',edition:''},
  {shop:'カーナベル',name:'ブラック・マジシャン',price:150,rarity:'ノーマル',code:'LB-01',condition:'-',url:'https://ka.example/2',image:'https://img.example/c.jpg',sold_out:false,stock:1,art:'元のイラスト',edition:''}];`);

// 1) レアリティを選んでいない一覧には出さない（いつもの一覧はそのまま）
run(`rarityFilter=''; renderTable()`);
assert.equal(count(body(), 'cr-thumb'), 0, 'レアリティ未選択では画像を出さない');

// 2) 絵が1種類だけのレアリティにも出さない
run(`rarityFilter='ノーマル'; renderTable()`);
assert.equal(count(body(), 'cr-thumb'), 0, '絵が分かれないレアリティでは画像を出さない');

// 3) 絵が分かれるレアリティでは全行に出る。画像の無い行は空きで頭をそろえる
run(`rarityFilter='シークレット'; renderTable()`);
const h = body();
assert.equal(count(h, '<td class="cr-shop"><button type="button" class="cr-thumb"'), 2, '画像のある2行はボタン');
assert.equal(count(h, 'cr-thumb cr-thumb-none'), 1, '画像の無い行は空き');
assert(h.includes('loading="lazy"') && h.includes('width="30" height="44"'), '遅延読み込み・大きさ固定');
assert(h.includes('data-shop="カーナベル"') && h.includes('aria-label="カーナベルの商品画像を拡大"'));
// 属性に入る URL の引用符は属性を閉じない（safeUrl が %22 にし、& は escAttr が &amp; にする）
assert(!h.includes('y="2"') && h.includes('a.jpg?x=1&amp;y=%222%22'), 'URL の引用符で属性が閉じない');

// 4) 絵を選んでも（イラスト違いに多くの絵が混ざるので）出し続ける
run(`artFilter='イラスト違い'; renderTable()`);
assert.equal(count(body(), 'cr-thumb cr-thumb-none'), 1, '絵を選んでいても出す');

// 5) 店ごとの最安の表示でも出る
run(`artFilter=''; listView='shop'; renderTable()`);
assert(count(body(), 'class="cr-thumb') >= 1, '店ごとの最安でも出す');

// 6) javascript: の画像は出さない
run(`listView='all'; D[3].rarity='シークレット'; renderTable()`);
assert(!body().includes('javascript:'), '危ない URL は画像にしない');

console.log('row_thumbs_check: ok');
