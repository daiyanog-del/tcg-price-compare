'use strict';
// 購入候補の「どこで買うと安いか」: 未発売カード（サーバーの unreleased）を店の検索に回さず、
// 「見つからなかったカード」と別の1行で出すことの回帰検証（2026-10-30）。
// index-wish.js から wishCheapestStores と、それが使う小さな関数だけを切り出して動かす。
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert/strict');

const root = path.join(__dirname, '..', '..');
const src = fs.readFileSync(path.join(root, 'static/js/index-wish.js'), 'utf8');
function slice(from, to) {
  const s = src.indexOf(from), e = src.indexOf(to, s);
  assert.ok(s >= 0 && e > s, `切り出す範囲が見つからない: ${from}`);
  return src.slice(s, e);
}
const code = [
  slice('// 購入候補の API が1回に受け取るカードの数', '// ページロード時に localStorage から即時復元'),
  slice('// まだ店に並ばないカードの言い方', '// 価格情報の表示 HTML を生成する'),
  slice('// 販売店舗ごとのカード検索URLを生成する', '// ── どのデッキ用か'),
  slice('// 「どこで買うと安いか調べる」ボタンのハンドラ', '// 追加ボタンのHTMLを生成するヘルパー'),
].join('\n');
const WishSplit = require(path.join(root, 'static/wish-split.js'));

const RULES = { 'カードラッシュ': { base_fee: 180, free_threshold: 5000, min_order: null } };

function run(list, dbData) {
  const el = { innerHTML: '', classList: { add() {}, remove() {} }, scrollIntoView() {}, querySelectorAll: () => [] };
  const fetched = [];
  const sb = {
    window: {}, WishSplit,
    wishGet: () => list,
    _ga: () => {},
    esc: s => String(s ?? ''), escAttr: s => String(s ?? ''), safeUrl: u => u,
    document: { getElementById: id => (id === 'wishShopRanking' ? el : null) },
    fetch: async (url) => { fetched.push(url); return { json: async () => dbData }; },
    ShopBusy: { fetch: async (url) => { fetched.push(url); throw new Error('店の検索はしない想定'); } },
    console, Map, Set, Object, JSON, Promise,
  };
  sb.window.WishSplit = WishSplit;
  vm.createContext(sb);
  vm.runInContext(code, sb);
  return vm.runInContext('wishCheapestStores()', sb).then(() => ({ html: el.innerHTML, fetched }));
}

const shopRow = (items, missing) => ({
  shop: 'カードラッシュ', total: items.reduce((s, it) => s + it.price * it.qty, 0),
  covered_qty: items.reduce((s, it) => s + it.qty, 0), total_qty: 2,
  items, missing_cards: missing,
});

(async () => {
  // 1. 発売済み1枚＋未発売1枚: 未発売は店の検索に回らず、「未発売のカード」の行に発売日つきで出る
  {
    const { html, fetched } = await run(
      [{ name: '発売済みカード', qty: 1 }, { name: '未来のカード', qty: 1 }],
      {
        shops: [shopRow([{ name: '発売済みカード', qty: 1, price: 500, rarity: 'ノーマル', rarity_pref: '' }],
          [{ name: '未来のカード', rarity: '' }])],
        db_missing: [],
        unreleased: [{ name: '未来のカード', rarity: '', release_date: '2026-11-01', tcg_preview: false }],
        entries: [{ name: '発売済みカード', qty: 1, rarity: '' }, { name: '未来のカード', qty: 1, rarity: '' }],
        shipping_rules: RULES,
      });
    assert.deepEqual(fetched, ['/api/wish-shop-totals'], '未発売カードを店のリアルタイム検索に回さない');
    assert.ok(html.includes('一番安くそろう買い方'), '発売済みの分で一番安い買い方を出す');
    assert.ok(html.includes('未発売のカード：未来のカード（11月1日発売）'), '未発売の行に発売日を出す');
    assert.ok(!html.includes('見つからなかったカード'), '未発売を「見つからなかった」と言わない');
    assert.ok(!html.includes('そろいません'), '未発売を店の欠けに数えない');
    assert.ok(!html.includes('この店に無いカード'), '店ごとの内訳でも「この店に無い」に入れない');
    assert.ok(html.includes('¥680'), '金額は発売済みの分＋送料だけ');
    assert.ok(html.includes('1店でまとめて買うと（送料込み）'), '店ごとの表の見出し（分けて買う内訳と読まれない言い方）');
  }

  // 2. 未発売（TCG先行・発売日未定）だけのリスト: 「どの店にも見つかりませんでした」ではなく未発売の行だけ
  {
    const { html, fetched } = await run(
      [{ name: 'Future Card', qty: 1 }, { name: '日付未定のカード', qty: 1 }],
      {
        shops: [shopRow([], [{ name: 'Future Card', rarity: '' }, { name: '日付未定のカード', rarity: '' }])],
        db_missing: [],
        unreleased: [
          { name: 'Future Card', rarity: '', release_date: '', tcg_preview: true },
          { name: '日付未定のカード', rarity: '', release_date: '', tcg_preview: false },
        ],
        entries: [{ name: 'Future Card', qty: 1, rarity: '' }, { name: '日付未定のカード', qty: 1, rarity: '' }],
        shipping_rules: RULES,
      });
    assert.deepEqual(fetched, ['/api/wish-shop-totals']);
    assert.ok(!html.includes('どの店にも見つかりませんでした'), '未発売だけなら「見つからない」と出さない');
    assert.ok(html.includes('Future Card（日本では未発売）'), 'TCG先行は「日本では未発売」');
    assert.ok(html.includes('日付未定のカード（発売日未定）'), '発売日が未定なら「発売日未定」');
  }

  // 2b. 3種以上: 名前を並べず、数と発売日の1行＋開いたら発売日ごとの名前（10-30 ユーザー指摘「ごちゃつく」）
  {
    const names = Array.from({ length: 27 }, (_, i) => `新カード${i + 1}`);
    const un = names.map((n, i) => ({ name: n, rarity: '', release_date: i < 25 ? '2026-10-31' : '', tcg_preview: i >= 25 }));
    const { html, fetched } = await run(
      [{ name: '発売済みカード', qty: 1 }, ...names.map(n => ({ name: n, qty: 1 }))],
      {
        shops: [shopRow([{ name: '発売済みカード', qty: 1, price: 500, rarity: 'ノーマル', rarity_pref: '' }],
          names.map(n => ({ name: n, rarity: '' })))],
        db_missing: [], unreleased: un,
        entries: [{ name: '発売済みカード', qty: 1, rarity: '' }, ...names.map(n => ({ name: n, qty: 1, rarity: '' }))],
        shipping_rules: RULES,
      });
    assert.deepEqual(fetched, ['/api/wish-shop-totals']);
    assert.ok(html.includes('未発売の27種（10月31日発売 25種・日本では未発売 2種）はこの金額に含みません'), '数と発売日の1行');
    assert.ok(html.includes('<summary>カード名を見る</summary>'), '名前は畳む');
    assert.ok(!html.includes('未発売のカード：'), '名前の羅列の1行は出さない');
    assert.ok(html.includes('新カード27'), '開けば名前がある');
  }

  // 3. 一覧の行の言い方
  {
    const sb = { console };
    vm.createContext(sb);
    vm.runInContext(code.split('// 販売店舗ごとのカード検索URLを生成する')[0], sb);
    const parts = m => JSON.parse(vm.runInContext(`JSON.stringify(_wishUnreleasedParts(${JSON.stringify(m)}))`, sb));
    assert.deepEqual(parts({ release_date: '2026-11-01' }), ['未発売', '11月1日発売']);
    assert.deepEqual(parts({ release_date: '' }), ['未発売', '発売日未定']);
    assert.deepEqual(parts({ tcg_preview: true }), ['日本では未発売']);
  }

  console.log('ok');
})().catch(e => { console.error(e); process.exit(1); });
