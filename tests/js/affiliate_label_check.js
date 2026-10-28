// アフィリエイト広告表記（rel=sponsored・PRバッジ・注記）の実コード検証。
// index-main.js の実関数を vm で実行し、_affConfig がダミー有り／空の両方で
// 3箇所（出品一覧の店ごとの最安・買取カード・出品一覧のすべて）の出力を確認する。
// 2026-10-21 案C: 旧・店舗サマリー（#sumCards）は出品一覧の「店ごとの最安」（listView='shop'）に統合した。
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
let D=[],sort={k:'price',a:true},rarityFilter='',artFilter='',cardImgSmall='',tableShowCount=20,listView='shop',_currentCardName='灰流うらら';
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
  block('function _applyBuyMaxToD(', 'function _renderBuyInline('),
  block('// 買取の欄の見出し。', '// #buyInline を開いた経路'),
  block('function _shownRarity(', 'function renderTable('),
  block('function renderTable(', '// B-4: 「もっと見る」'),
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
// 注記は廃止（PRの印と rel だけ）。どの描画でも注記文言・要素が出ないこと
const noNote = () => assert(!get('tbody').innerHTML.includes('aff-note') && !get('tbody').innerHTML.includes('含みます') && !get('crListNote').textContent.includes('含みます'), '注記は出さない');
const shopCell = (name) => '<td class="cr-shop"><span class="cr-shop-name">' + name + '</span>';
const count = (h, s) => h.split(s).length - 1;
const PR = '<span class="aff-pr">PR</span>';
const FULL = `_affConfig={'カーナベル':{sell:'dummyA8',buy:'dummyA8'},'駿河屋':{user_id:'9999'}}`;

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

// 2) 出品一覧の店ごとの最安: ダミー設定あり
run(FULL);
run(`listView='shop'; renderSummary(); renderTable()`);
let h = get('tbody').innerHTML;
assert(h.includes('px.a8.net') && h.includes('affiliate.suruga-ya.jp'));
assert.equal(count(h, PR), 2, 'カーナベルと駿河屋にだけPR');
assert(h.includes(shopCell('カーナベル' + PR)));
assert(h.includes(shopCell('駿河屋' + PR)));
assert(h.includes(shopCell('トレコロ')), 'トレコロは直リンクでPRなし');
assert.equal(count(h, 'rel="sponsored noopener"'), 2, '変換2店×「店で見る」');
assert.equal(count(h, 'rel="noopener nofollow"'), 1, 'トレコロ×「店で見る」');
assert(!h.includes('<b>悪い店</b>') && h.includes('&lt;b&gt;悪い店&lt;/b&gt;'), '店名はesc済み');
noNote();

// 3) 設定が空: 直リンクのみ→PRなし・注記非表示。再描画で正しく切り替わる
run(`_affConfig={}`);
run(`renderSummary(); renderTable()`);
h = get('tbody').innerHTML;
assert.equal(count(h, 'aff-pr'), 0);
assert.equal(count(h, 'sponsored'), 0);
assert(h.includes('rel="noopener nofollow"'));
noNote();
run(`_affConfig={'カーナベル':{sell:'dummyA8'}}`);
run(`renderSummary(); renderTable()`);
noNote();
// レアリティ切替で変換店が消える場合（トレコロのみのレアリティ）
run(`D.push({shop:'トレコロ',name:'y',price:10,rarity:'ノーマル',code:'',condition:'-',url:'${TC}',sold_out:false,stock:1}); rarityFilter='ノーマル'`);
run(`renderSummary(); renderTable()`);
noNote();
// 在庫切れ表示でも隠す（直前が表示状態だった場合）
run(`rarityFilter=''`);
run(`renderSummary(); renderTable()`);
noNote();
run(`rarityFilter='存在しない'`);
run(`renderSummary(); renderTable()`);
noNote();
run(`D.pop(); rarityFilter=''`);

// 4) 買取カード
run(`_affConfig={'カーナベル':{sell:'dummyA8',buy:'dummyA8'}}`);
run(`_renderBuyInline()`);
h = get('buyInlineBody').innerHTML;
assert(h.includes('<div class="slabel">カーナベル' + PR + '</div>'));
// 見出し（閉じていても最高額が分かる）。「すべて」のときは最高額と言い切らない（H-1）
assert.equal(get('buyInlineHead').innerHTML, '<span class="cr-buy-t">買取価格</span><span class="cr-buy-max">レアリティを選ぶと最高額を表示します</span>');
run(`rarityFilter='ウルトラ'; _renderBuyInline()`);
assert.equal(get('buyInlineHead').innerHTML, '<span class="cr-buy-t">ウルトラの買取価格</span><span class="cr-buy-max">最高 <b>¥50</b> カーナベル</span>');
run(`rarityFilter='ノーマル'; _renderBuyInline()`);
assert.equal(get('buyInlineHead').innerHTML, '<span class="cr-buy-t">ノーマルの買取価格</span><span class="cr-buy-max">見つかりませんでした</span>');
run(`_setBuyInlineHead(null)`);
assert.equal(get('buyInlineHead').textContent, '買取価格を見る（いくらで売れるか）');
run(`rarityFilter=''; _renderBuyInline()`);
h = get('buyInlineBody').innerHTML;
assert(h.includes('<div class="slabel">トレコロ</div>'));
assert.equal(count(h, PR), 1);
assert(h.includes('rel="sponsored noopener"') && h.includes('rel="noopener nofollow"'));
assert(!h.includes('aff-note') && !h.includes('含みます'), '買取にも注記は出さない');
run(`_affConfig={}`);
run(`_renderBuyInline()`);
h = get('buyInlineBody').innerHTML;
assert(!h.includes('aff-note') && !h.includes('aff-pr') && !h.includes('sponsored'));

// 4b) 未開封・付属品つき（sealed）は店の最高額・見出しの最高額に入れず、高ければ添える。未開封しか無い店は札を付けて後ろ（2026-10-28）
run(`_affConfig={}; rarityFilter='ウルトラ'; _buyInlineD=[
  {shop:'カードラッシュ',name:'ブラックマジシャン',price:25000,rarity:'ウルトラ',code:'AB-JP001',condition:'-',url:'',sealed:false},
  {shop:'カードラッシュ',name:'(未開封)ブラックマジシャン',price:45000,rarity:'ウルトラ',code:'AB-JP001',condition:'-',url:'',sealed:true},
  {shop:'遊々亭',name:'ブラックマジシャン',price:24000,rarity:'ウルトラ',code:'AB-JP001',condition:'-',url:'',sealed:false},
  {shop:'カードラボ',name:'(未開封)ブラックマジシャン',price:50000,rarity:'ウルトラ',code:'AB-JP001',condition:'-',url:'',sealed:true}];
  _renderBuyInline()`);
h = get('buyInlineBody').innerHTML;
assert.equal(get('buyInlineHead').innerHTML, '<span class="cr-buy-t">ウルトラの買取価格</span><span class="cr-buy-max">最高 <b>¥25,000</b> カードラッシュ</span>', '最高額は通常品');
assert(h.indexOf('カードラッシュ') < h.indexOf('遊々亭') && h.indexOf('遊々亭') < h.indexOf('カードラボ'), '未開封しか無い店は後ろ');
assert(h.includes('・未開封・付属品つきは¥45,000'), '同じ店の未開封が高ければ添える');
assert.equal(count(h, 'sbest-badge'), 1);
assert(!/カードラボ[\s\S]*最高額/.test(h), '未開封しか無い店に最高額の印を付けない');
// 別の型番の未開封は添えない。「すべて」のときも添えない（別レアリティの未開封になりうる）
run(`_buyInlineD.push({shop:'遊々亭',name:'(未開封)ブラックマジシャン',price:99000,rarity:'ウルトラ',code:'ZZ-JP999',condition:'-',url:'',sealed:true}); _renderBuyInline()`);
assert(!get('buyInlineBody').innerHTML.includes('未開封・付属品つきは¥99,000'), '別の型番の未開封を通常品に添えない');
run(`rarityFilter=''; _renderBuyInline()`);
assert(!get('buyInlineBody').innerHTML.includes('未開封・付属品つきは'), '「すべて」では添えない');
run(`rarityFilter='ウルトラ'; _buyInlineD.pop()`);
// 一覧の「この型番の最高買取」も通常品
run(`D=[{shop:'遊々亭',name:'ブラックマジシャン',price:30000,rarity:'ウルトラ',code:'AB-JP001',condition:'-',url:'',sold_out:false,stock:1}]; _applyBuyMaxToD()`);
assert.equal(run('D[0].buyMax'), 25000);
assert.equal(run('D[0].buyMaxShop'), 'カードラッシュ');
setData(); run(`_affConfig={}; rarityFilter='ウルトラ'; _buyInlineD=[
  {shop:'カードラッシュ',name:'ブラックマジシャン',price:25000,rarity:'ウルトラ',code:'AB-JP001',condition:'-',url:'',sealed:false},
  {shop:'カードラッシュ',name:'(未開封)ブラックマジシャン',price:45000,rarity:'ウルトラ',code:'AB-JP001',condition:'-',url:'',sealed:true},
  {shop:'カードラボ',name:'(未開封)ブラックマジシャン',price:50000,rarity:'ウルトラ',code:'AB-JP001',condition:'-',url:'',sealed:true}]`);
// 未開封しか無いレアリティは「最高額」と言い切らない
run(`_buyInlineD=_buyInlineD.filter(r=>r.sealed); _renderBuyInline()`);
assert.equal(get('buyInlineHead').innerHTML, '<span class="cr-buy-t">ウルトラの買取価格</span><span class="cr-buy-max">通常品の買取は見つかりませんでした</span>');
assert.equal(count(get('buyInlineBody').innerHTML, 'sbest-badge'), 0);
run(`rarityFilter=''; _buyInlineD=null`);
setData();

// 5) 出品一覧のすべて
run(`_affConfig={'カーナベル':{sell:'dummyA8'},'駿河屋':{user_id:'9999'}}`);
run(`listView='all'; renderTable()`);
h = get('tbody').innerHTML;
assert(h.includes(shopCell('カーナベル' + PR)));
assert(h.includes(shopCell('駿河屋' + PR)));
assert(h.includes(shopCell('トレコロ')));
assert(h.includes(shopCell('&lt;b&gt;悪い店&lt;/b&gt;')), 'URL無し行はPRなし・esc済み');
assert.equal(count(h, 'class="row-link cr-go"'), 3);
assert.equal(count(h, 'rel="sponsored noopener"'), 2);
assert.equal(count(h, 'rel="noopener nofollow"'), 1);
assert.equal(count(h, 'data-href="https://px.a8.net') + count(h, 'data-href="https://affiliate.suruga-ya.jp'), 2);
run(`_affConfig={}`);
run(`renderTable()`);
h = get('tbody').innerHTML;
assert(!h.includes('aff-pr') && !h.includes('sponsored'));

// 6) 配線: index.html に印のCSSがあり、注記の要素・CSSは無い
const html = fs.readFileSync(path.join(root, 'templates/index.html'), 'utf8');
assert(html.includes('.aff-pr{'));
assert(!html.includes('aff-note') && !html.includes('sumAffNote') && !html.includes('含みます'));
// メルカリのリンクも同じ印の定数を使う（文字の「（PR）」表記・注記は持たない）
const mref = fs.readFileSync(path.join(root, 'static/js/mercari-ref.js'), 'utf8');
assert(mref.includes('AFF_PR_BADGE') && !mref.includes('AFF_NOTE_TEXT') && !mref.includes('aff-note') && !mref.includes('（PR）'));
console.log('affiliate_label_check: OK');
