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
  TextDecoder, URLSearchParams, AbortController, Promise, setTimeout,
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
  const code = () => run('_pkPackCode(__st,_pkItemsFromPerShop(__ps))');
  sb.__st = { info: { code: 'DBGV' }, cards: [{ name: 'A' }, { name: 'B' }, { name: 'C' }] };
  sb.__ps = {
    A: { 店1: { ノーマル: item('DBGV-JP001', 20) } },
    B: { 店1: { ウルトラ: item('RC04-JP001', 100) }, 店2: { ウルトラ: item('DBGV-JP002', 300) } },
    C: { 店1: { ノーマル: item('', 30) } },
  };
  assert.equal(code(), 'DBGV');
  // 公式の略号と型番が違う商品（例: 略号 26LP・型番 WCS-）は、結果で多数のカードに付いている頭を使う
  sb.__st = { info: { code: '26LP' }, cards: [{ name: 'A' }, { name: 'B' }, { name: 'C' }] };
  sb.__ps = {
    A: { 店1: { ノーマル: item('WCS-JP001', 20) } },
    B: { 店1: { ノーマル: item('WCS-JP002', 20) } },
    C: { 店1: { ノーマル: item('RC04-JP003', 20) } },
  };
  assert.equal(code(), 'WCS');
  // 半分を超える頭が無ければ絞らない
  sb.__ps = {
    A: { 店1: { ノーマル: item('X1-JP001', 20) } },
    B: { 店1: { ノーマル: item('X2-JP002', 20) } },
    C: { 店1: { ノーマル: item('', 20) } },
  };
  assert.equal(code(), '');
});

check('まとめて検索の結果: 最安は「このパックの版」だけ。表は他の版を畳み、他の版しか無いカードは「このパックの版なし」', () => {
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
        店A: { 'ノーマル\tRC04': item('RC04-JP001', 10, 'ノーマル', '店A') },
        店B: { 'ノーマル\tDBGV': item('DBGV-JP012', 20, 'ノーマル', '店B'), 'シークレット\tDBGV': item('DBGV-JP012', 900, 'シークレット', '店B'),
               'ウルトラ\tTDPP': item('TDPP-JP001', 60, 'ウルトラ', '店B') },
      },
      '再録だけのカード': { 店A: { 'ノーマル\tRC04': item('RC04-JP002', 50, 'ノーマル', '店A') } },
      '型番不明': { 店B: { 'スーパー\t': item('', 70, 'スーパー', '店B') } },
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
  const g = name => r.matrix.groups.find(x => x.name === name);
  const tri = g('トライホーン・ドラゴン');
  // このパックのレアリティ（シークレット・ノーマル）が並び、ウルトラ（TDPP だけ）は畳む行
  assert.deepEqual(Array.from(tri.main.map(x => x.rarity)), ['シークレット', 'ノーマル']); // vm の中の配列なので Array.from で比べる
  assert.deepEqual(Array.from(tri.other.map(x => x.rarity)), ['ウルトラ']);
  // ノーマルの店A は他の版しか無い → 値段に版の札（最安から外す）
  const normal = tri.main.find(x => x.rarity === 'ノーマル');
  assert.equal(normal.cells['店A'].other, true);
  assert.equal(normal.cells['店B'].other, undefined);
  assert.equal(g('再録だけのカード').main.length, 0);
  assert.equal(g('再録だけのカード').other.length, 1);
  assert.equal(g('型番不明').main[0].rarity, 'スーパー');
  sb.__m = r.matrix;
  const html = run("_pkMatrixHtml(__m,'order',false)");
  // 最安の緑はこのパックの版（店B ¥20）。店A ¥10（他の版）には付けない
  assert.ok(/is-min">¥20/.test(html) || /is-min"><a[^>]*>¥20/.test(html), 'このパックの最安に緑が無い');
  assert.ok(!/is-min[^"]*"[^>]*>(?:<a[^>]*>)?¥10/.test(html), '他の版に緑を付けた');
  assert.ok(html.includes('他の商品の版 1行も見る'));
  assert.ok(html.includes('このパックの版は見つかりませんでした'));
});

check('毎朝の収集の表: このパックの行で他の版のマスには札。このパックの版があるカードの型番不明だけの行は「版不明」で畳む・型番の無いカードは隠さない', () => {
  sb.__st = { info: { code: 'DBGV' }, cards: [{ name: 'A' }, { name: 'B' }, { name: 'C' }] };
  sb.__db = { updated_at: '2026-10-22', shops: ['店1', '店2'], rows: [
    { name: 'A', rarity: 'ノーマル', cells: { 店1: { price: 20, code: 'DBGV-JP001' }, 店2: { price: 10, code: 'RC04-JP001' } } },
    { name: 'A', rarity: 'ウルトラ', cells: { 店1: { price: 80, code: 'TDPP-JP001' } } },
    { name: 'A', rarity: 'アルティメット', cells: { 店2: { price: 900, code: '' } } },
    { name: 'B', rarity: '', cells: {} },
    { name: 'C', rarity: 'スーパー', cells: { 店1: { price: 30, code: '' } } },
  ] };
  const items = run('_pkItemsFromDb(__db)');
  sb.__items = items;
  assert.equal(run('_pkPackCode(__st,__items)'), 'DBGV');
  sb.__mm = run("_pkMatrixModel(__st,__items,'DBGV',['店1','店2'],{})");
  const a = sb.__mm.groups[0];
  assert.deepEqual(Array.from(a.main.map(x => x.rarity)), ['ノーマル']);
  assert.deepEqual(Array.from(a.other.map(x => x.rarity)).sort(), ['アルティメット', 'ウルトラ']);
  assert.ok(Array.from(a.other.find(x => x.rarity === 'アルティメット').vers).includes('版不明'));
  assert.equal(sb.__mm.groups[2].main[0].rarity, 'スーパー', '型番の無いカードは隠さない');
  assert.equal(a.main.find(x => x.rarity === 'ノーマル').cells['店2'].other, true);
  assert.equal(sb.__mm.groups[1].main.length, 0);
  const html = run("_pkMatrixHtml(__mm,'order',true)");
  assert.ok(html.includes('このパックの版の記録がありません'));
  assert.ok(html.includes('pmx-ver">RC04'), '他の版のマスに札が無い');
});

check('店舗×レアの表: 名前・レアリティ・店名・版の札はエスケープし、http(s) 以外のリンクは張らない。各行の最安だけ印', () => {
  sb.__m = {
    shops: ['店<1>', '店2'],
    groups: [{ name: '<img src=x onerror=alert(1)>', failed: null,
      main: [{ rarity: '"><b>', cells: { '店<1>': { price: 100, url: 'javascript:alert(1)', code: '' }, 店2: { price: 80, url: 'https://ok.example/a', code: '' } } }],
      other: [{ rarity: '<i>', vers: ['<s>'], cells: { 店2: { price: 5, url: '', code: 'X-1' } } }] }],
  };
  const html = run("_pkMatrixHtml(__m,'order',false)");
  assert.ok(!html.includes('<img src=x'), 'カード名がエスケープされていない');
  assert.ok(!html.includes('"><b>'), 'レアリティがエスケープされていない');
  assert.ok(!html.includes('<i>') && !html.includes('<s>'), '畳む行がエスケープされていない');
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

check('型番の頭は完全一致（WPP と WPP5 は別の商品）', () => {
  assert.equal(run("_pkSameVersion('WPP','WPP5-JP001')"), false);
  assert.equal(run("_pkSameVersion('WPP','WPP-JP001')"), true);
});

check('表: 取得に失敗した店は列に残して「取得失敗」、レアリティ不明の品も行にする、版なしは理由を出す', () => {
  sb.__st = { info: { code: 'DBGV' }, cards: [{ name: 'A' }, { name: 'B' }] };
  sb.__res = {
    items: { A: { price: 50, status: 'partial' }, B: { price: 30, status: 'ok' } },
    perShop: {
      A: { 店1: { x: item('DBGV-JP001', 50, '', '店1') } },
      B: { 店1: { 'ノーマル\tRC04': item('RC04-JP002', 30, 'ノーマル', '店1') } },
    },
    shopOrder: ['店1'],
    failedByCard: { A: { shops: ['店2'], all: false } },
  };
  run('_pkBuildFromPerShop(__st,__res)');
  const m = sb.__res.matrix;
  assert.deepEqual(Array.from(m.shops), ['店1', '店2']);
  assert.ok(m.groups[0].main.some(r => r.rarity === 'レアリティ不明'));
  sb.__m = m;
  const html = run("_pkMatrixHtml(__m,'order',false)");
  assert.ok(html.includes('取得失敗'), '失敗した店の欄に「取得失敗」が無い');
  assert.ok(html.includes('このパックの版は見つかりませんでした'), '版なしの理由が出ていない');
});

// まとめて検索を、作り物の通信で最後まで動かす（画面の描き直しは空にする）
async function bulk(cards, respond) {
  const reqs = [];
  run('_pkRefreshPrices=()=>{};_pkUpdateProgress=()=>{};_pkHm=()=>"12:00";');
  sb._ga = () => {};
  sb.fetch = async (url, init) => {
    const body = new URLSearchParams(init.body);
    const names = body.get('cards').split('|');
    reqs.push({ url, names, perShop: body.get('include_per_shop') });
    const bytes = Buffer.from(respond(names, reqs.length), 'utf8');
    let sent = false;
    return { ok: true, status: 200, body: { getReader: () => ({ read: async () => (sent ? { done: true } : (sent = true, { done: false, value: bytes })) }) } };
  };
  sb.__st = { req: 1, name: 'P', info: { code: 'DBGV' }, unreleased: false, mode: 'sale', results: { sale: null, buy: null }, run: null,
    cards: cards.map(n => ({ name: n })) };
  run('_pkReq=1;_pk=__st;');
  await run('bulkSearchPackPrices()');
  return { st: sb.__st, reqs };
}
const sse = evs => evs.map(e => 'data: ' + JSON.stringify(e) + '\n\n').join('');

(async () => {
  // サーバーが名前を正式名に直して返しても、並びの番号で正しいカードに入る（reviewer 高2）
  {
    const { st, reqs } = await bulk(['A BF-五月雨のソハヤ', 'B'], () => sse([
      { type: 'card_done', index: 0, name: 'A BF－五月雨のソハヤ', best: { price: 100, shop: '店1', code: 'DBGV-JP001', rarity: 'ノーマル' }, status: 'ok',
        per_shop: { 店1: { 'ノーマル\tDBGV': { price: 100, shop: '店1', code: 'DBGV-JP001', rarity: 'ノーマル' } } } },
      { type: 'card_done', index: 1, name: 'B', best: null, status: 'ok', per_shop: {} },
      { type: 'done' },
    ]));
    assert.equal(reqs[0].perShop, 'version', '型番の頭ごとの価格を頼んでいない');
    assert.equal(st.results.sale.items['A BF-五月雨のソハヤ'].price, 100);
    assert.equal(st.results.sale.found, 1);
    assert.equal(st.run, null);
    passed++; console.log('ok - 並びの番号でカードに対応づける（名前が直されて返っても価格が入る）');
  }
  // 80種を超えると分けて送り、2回目の番号は続きのカードに入る（reviewer 低4）
  {
    const cards = Array.from({ length: 100 }, (_, i) => 'c' + i);
    const { st, reqs } = await bulk(cards, names => sse(names.map((n, i) => ({ type: 'card_done', index: i, name: n, best: { price: 10, shop: 's' }, status: 'ok', per_shop: {} }))
      .concat([{ type: 'done' }])));
    assert.deepEqual(reqs.map(r => r.names.length), [80, 20]);
    assert.equal(st.results.sale.done, 100);
    assert.ok(st.results.sale.items.c99, '2回目のカードが入っていない');
    passed++; console.log('ok - 80種を超える商品は分けて送る');
  }
  // 終わりの合図が来ないまま切れたら、失敗として出す（reviewer 中2）
  {
    const { st } = await bulk(['A', 'B'], () => sse([{ type: 'card_done', index: 0, name: 'A', best: { price: 10, shop: 's' }, status: 'ok' }]));
    assert.ok(/途中で切れました/.test(st.results.sale.error), '切れたことが出ていない');
    passed++; console.log('ok - 終わりの合図が無いまま切れたら失敗として出す');
  }
  console.log(`\n${passed} checks passed`);
})().catch(e => { console.error(e); process.exit(1); });
