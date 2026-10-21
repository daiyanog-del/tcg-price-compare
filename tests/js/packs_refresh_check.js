'use strict';
// 新商品タブ（static/packs.js。2026-10-21 モック packs-refresh-20261021 の A〜E）の判定部分の回帰検証。
// - まとめて検索の結果を「このパックの版」（型番の頭）に絞る（同じ名前の他の商品の再録版を最安・表に混ぜない）
// - 店舗×レアの表の組み立てと、文字のエスケープ
// - 価格の欄の出し分け（未発売・値動きの色・取得失敗・このパックの版なし）
// packs.js は読み込み時に画面へ触らない（宣言だけ）ので、ファイル全体を vm で読み込んで関数を呼ぶ。
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert/strict');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'packs.js'), 'utf8');
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const sb = {
  esc, escAttr: esc,
  safeUrl: u => (/^https?:\/\//i.test(String(u || '')) ? String(u) : ''),
  Date, Math, JSON, Object, Array, Set, Number, String, console,
};
sb.globalThis = sb;
vm.createContext(sb);
vm.runInContext(src, sb);
const run = code => vm.runInContext(code, sb);

let passed = 0;
const check = (name, fn) => { fn(); passed++; console.log('ok -', name); };

const item = (code, price, rarity, shop) => ({ code, price, rarity, shop, url: 'https://shop.example/' + code });

check('型番の頭で「このパックの版」を見分ける（型番が分からないものは残す・頭が分からなければ絞らない）', () => {
  assert.equal(run("_pkSameVersion('DBGV','DBGV-JP002')"), true);
  assert.equal(run("_pkSameVersion('DBGV','dbgv-jp002')"), true);
  assert.equal(run("_pkSameVersion('DBGV','RC04-JP010')"), false);
  assert.equal(run("_pkSameVersion('DBGV','')"), true);
  assert.equal(run("_pkSameVersion('','RC04-JP010')"), true);
});

check('パックの型番の頭: 一覧の code が結果に出ていればそれ、出ていなければ半分を超えるカードの頭、どちらも無ければ絞らない', () => {
  sb.__st = { info: { code: 'DBGV' }, cards: [{ name: 'A' }, { name: 'B' }, { name: 'C' }] };
  sb.__ps = {
    A: { 店1: { ノーマル: item('DBGV-JP001', 20) } },
    B: { 店1: { ウルトラ: item('RC04-JP001', 100) }, 店2: { ウルトラ: item('DBGV-JP002', 300) } },
    C: { 店1: { ノーマル: item('', 30) } },
  };
  assert.equal(run('_pkPackCode(__st,__ps)'), 'DBGV');
  // 公式の略号と型番が違う商品（例: 略号 26LP・型番 WCS-）は、結果で多数のカードに付いている頭を使う
  sb.__st = { info: { code: '26LP' }, cards: [{ name: 'A' }, { name: 'B' }, { name: 'C' }] };
  sb.__ps = {
    A: { 店1: { ノーマル: item('WCS-JP001', 20) } },
    B: { 店1: { ノーマル: item('WCS-JP002', 20) } },
    C: { 店1: { ノーマル: item('RC04-JP003', 20) } },
  };
  assert.equal(run('_pkPackCode(__st,__ps)'), 'WCS');
  // 半分を超える頭が無ければ絞らない
  sb.__ps = {
    A: { 店1: { ノーマル: item('X1-JP001', 20) } },
    B: { 店1: { ノーマル: item('X2-JP002', 20) } },
    C: { 店1: { ノーマル: item('', 20) } },
  };
  assert.equal(run('_pkPackCode(__st,__ps)'), '');
});

check('最安と表は「このパックの版」だけ。他の商品の版しか無いカードは「このパックの版なし」', () => {
  sb.__st = {
    info: { code: 'DBGV' },
    cards: [{ name: 'トライホーン・ドラゴン' }, { name: '再録だけのカード' }, { name: '型番不明' }],
  };
  sb.__res = {
    items: {
      'トライホーン・ドラゴン': { price: 10, shop: '店A', rarity: 'ノーマル' }, // サーバーの最安は他の商品の版
      '再録だけのカード': { price: 50, shop: '店A', rarity: 'ノーマル' },
      '型番不明': { price: 70, shop: '店B', rarity: 'スーパー' },
    },
    perShop: {
      'トライホーン・ドラゴン': {
        店A: { ノーマル: item('RC04-JP001', 10, 'ノーマル', '店A') },
        店B: { ノーマル: item('DBGV-JP012', 20, 'ノーマル', '店B'), シークレット: item('DBGV-JP012', 900, 'シークレット', '店B') },
      },
      '再録だけのカード': { 店A: { ノーマル: item('RC04-JP002', 50, 'ノーマル', '店A') } },
      '型番不明': { 店B: { スーパー: item('', 70, 'スーパー', '店B') } },
    },
    shopOrder: ['店A', '店B'],
  };
  run('_pkBuildFromPerShop(__st,__res)');
  const r = sb.__res;
  assert.equal(r.code, 'DBGV');
  assert.equal(r.items['トライホーン・ドラゴン'].price, 20);
  assert.equal(r.items['トライホーン・ドラゴン'].shop, '店B');
  assert.equal(r.items['再録だけのカード'].price, null);
  assert.equal(r.items['再録だけのカード'].other, true);
  assert.equal(r.items['型番不明'].price, 70);
  assert.equal(r.found, 2);
  // 表: 他の商品の版の店（店A）は列に出ない。同じカードのレアリティは高い順
  assert.deepEqual(Array.from(r.matrix.shops), ['店B']); // vm の中の配列なので Array.from で比べる
  const tri = r.matrix.rows.filter(x => x.name === 'トライホーン・ドラゴン').map(x => x.rarity);
  assert.deepEqual(Array.from(tri), ['シークレット', 'ノーマル']);
  assert.ok(r.matrix.rows.some(x => x.name === '再録だけのカード' && x.rarity === ''), '価格の無いカードも行に残す');
});

check('店舗×レアの表: 名前・レアリティ・店名はエスケープし、http(s) 以外のリンクは張らない。各行の最安だけ印', () => {
  sb.__m = {
    shops: ['店<1>', '店2'],
    rows: [
      { name: '<img src=x onerror=alert(1)>', rarity: '"><b>', cells: { '店<1>': { price: 100, url: 'javascript:alert(1)' }, 店2: { price: 80, url: 'https://ok.example/a' } } },
    ],
  };
  const html = run("_pkMatrixHtml(__m,'order')");
  assert.ok(!html.includes('<img src=x'), 'カード名がエスケープされていない');
  assert.ok(!html.includes('"><b>'), 'レアリティがエスケープされていない');
  assert.ok(html.includes('店&lt;1&gt;'));
  assert.ok(!html.includes('javascript:'), 'javascript: のリンクを張った');
  assert.ok(html.includes('href="https://ok.example/a"'));
  assert.equal((html.match(/is-min/g) || []).length, 1);
  assert.ok(/is-min"><a href="https:\/\/ok\.example\/a"/.test(html), '最安（¥80）に印が無い');
});

check('価格の欄: 未発売・販売の値動き（上がり=赤・下がり=緑）・買取の未検索・取得失敗・このパックの版なし', () => {
  const st = (o) => Object.assign({ unreleased: false, mode: 'sale', results: { sale: null, buy: null }, run: null }, o);
  sb.__c = { name: 'X', db: { price: 480, pct: 12 } };
  sb.__s = st({ unreleased: true });
  assert.ok(run('_pkPriceHtml(__s,__c)').includes('未発売'));
  sb.__s = st({});
  assert.ok(run('_pkPriceHtml(__s,__c)').includes('is-up'));
  sb.__c = { name: 'X', db: { price: 480, pct: -4 } };
  assert.ok(run('_pkPriceHtml(__s,__c)').includes('is-down'));
  sb.__s = st({ mode: 'buy' });
  assert.ok(run('_pkPriceHtml(__s,__c)').includes('—'), '買取を調べる前に販売の値を出した');
  sb.__s = st({ results: { sale: { items: { X: { price: null, status: 'failed' } } }, buy: null } });
  assert.ok(run('_pkPriceHtml(__s,__c)').includes('取得失敗'));
  sb.__s = st({ results: { sale: { items: { X: { price: null, status: 'ok', other: true } } }, buy: null } });
  assert.ok(run('_pkPriceHtml(__s,__c)').includes('このパックの版なし'));
  sb.__s = st({ results: { sale: { items: { X: { price: 440, shop: 'トレコロCB', status: 'ok', partial: true } } }, buy: null } });
  const h = run('_pkPriceHtml(__s,__c)');
  assert.ok(h.includes('¥440') && h.includes('トレコロCB') && h.includes('一部取得失敗'));
});

check('並び順: 高い順は表示中の価格で、価格の無いカードは後ろ', () => {
  sb.__s = { unreleased: false, mode: 'sale', sort: 'price', results: { sale: null, buy: null },
    cards: [{ name: 'a', db: { price: 20 } }, { name: 'b', db: null }, { name: 'c', db: { price: 980 } }] };
  assert.deepEqual(Array.from(run('_pkOrder(__s)')), [2, 0, 1]);
  sb.__s.sort = 'order';
  assert.deepEqual(Array.from(run('_pkOrder(__s)')), [0, 1, 2]);
});

console.log(`\n${passed} checks passed`);
