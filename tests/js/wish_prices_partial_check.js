'use strict';
// 購入候補の価格: カードを足したときは足したカードだけ取りに行き、他の行の価格を出したままにすることの回帰検証
// （2026-10-31: 1枚足すたびに全部の価格を捨てて取り直し、一覧のすべての行が2〜3秒読み込み中になっていた）。
// index-wish.js から価格の取得まわり（_wishPrices の宣言〜_wishEnsurePrices）だけを切り出して動かす。
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert/strict');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static/js/index-wish.js'), 'utf8');
const start = src.indexOf('let _wishPrices = null;');
const end = src.indexOf('// 価格情報の表示 HTML を生成する');
assert.ok(start >= 0 && end > start, '切り出す範囲が見つからない');

(async () => {
  const store = new Map();
  const sent = []; // 取りに行った名前（1回ごと）
  let reply = null; // 次の応答を返す関数（ok=false なら失敗）
  const sb = {
    localStorage: { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) },
    wishGet: () => sb.__list,
    __list: [{ name: '灰流うらら', qty: 3 }, { name: '増殖するG', qty: 2 }],
    renders: 0,
    fetch: (_url, opt) => {
      const names = JSON.parse(opt.body).cards;
      sent.push(names);
      return new Promise(r => {
        reply = (ok = true) => r(ok
          ? { ok: true, json: async () => ({ cards: names.map(n => ({ name: n, status: 'ready', latest: 100 + sent.length })) }) }
          : { ok: false, status: 500 });
      });
    },
    setTimeout: () => 0, clearTimeout: () => {},
    Date, Promise, console,
  };
  sb.renderWishlist = () => { sb.renders++; vm.runInContext('_wishEnsurePrices()', sb); };
  sb.wishUpdateBadge = () => {};
  sb.globalThis = sb;
  vm.createContext(sb);
  vm.runInContext(src.slice(start, end) + '\n;globalThis.__get=()=>({p:_wishPrices,time:_wishPricesTime,failed:_wishPricesFailed,loading:_wishPricesLoading});', sb);
  const tick = () => new Promise(r => setTimeout(r, 0));
  const settle = async () => { await tick(); await tick(); await tick(); };

  // 1. 最初は全部取りに行く
  sb.renderWishlist();
  assert.deepEqual(sent.at(-1), ['灰流うらら', '増殖するG']);
  reply(); await settle();
  const firstTime = sb.__get().time;
  assert.equal(sb.__get().p['灰流うらら'].latest, 101);

  // 2. カードを足すと、足したカードだけ取りに行く。取得中も他の行の価格は残っている
  sb.__list = [...sb.__list, { name: '無限泡影', qty: 1 }];
  sb.renderWishlist();
  assert.equal(sent.length, 2);
  assert.deepEqual(sent.at(-1), ['無限泡影'], '足したカードだけを取りに行く');
  assert.equal(sb.__get().p['灰流うらら'].latest, 101, '取得中も他の行の価格を捨てない');
  assert.ok(sb.__get().loading, '足したカードは読み込み中');
  reply(); await settle();
  assert.equal(sb.__get().p['無限泡影'].latest, 102);
  assert.equal(sb.__get().p['増殖するG'].latest, 101);
  assert.equal(sb.__get().time, firstTime, '足した分に合わせて古い価格の期限を延ばさない');
  const saved = JSON.parse(store.get('cardprice_wish_prices_v1'));
  assert.deepEqual(saved.names.sort(), ['無限泡影', '増殖するG', '灰流うらら'].sort(), '端末の取っておきにも足したカードが入る');

  // 3. 取りに行った名前だけなら取り直さない
  sb.renderWishlist(); await settle();
  assert.equal(sent.length, 2);

  // 4. 期限が過ぎていれば、足したときも全部取り直す
  vm.runInContext('_wishPricesTime=Date.now()-11*60*1000', sb);
  sb.__list = [...sb.__list, { name: '墓穴の指名者', qty: 1 }];
  sb.renderWishlist();
  assert.equal(sent.at(-1).length, 4, '期限切れなら全部取り直す');
  reply(); await settle();

  // 5. 足したカードの取得が失敗したら「読み込めなかった」にし、次は全部取り直す
  sb.__list = [...sb.__list, { name: '抹殺の指名者', qty: 1 }];
  sb.renderWishlist();
  assert.deepEqual(sent.at(-1), ['抹殺の指名者']);
  reply(false); await settle();
  assert.equal(sb.__get().failed, true);
  assert.equal(sb.__get().p['灰流うらら'].latest, 103, '失敗しても他の行の価格は残す');
  vm.runInContext('_wishPricesTime=Date.now()-11*60*1000', sb);
  sb.renderWishlist();
  assert.equal(sent.at(-1).length, 5, '失敗のあとは全部取り直す');
  reply(); await settle();

  // 6. 足したカードの取得中にすべて削除された（価格が捨てられた）ら、届いた分を残さない
  sb.__list = [...sb.__list, { name: '原始生命態ニビル', qty: 1 }];
  sb.renderWishlist();
  assert.deepEqual(sent.at(-1), ['原始生命態ニビル']);
  vm.runInContext('_wishPrices=null', sb);
  reply(); await settle();
  assert.equal(sent.at(-1).length, 6, '捨てられていたら、届いた分は使わずに全部取り直す');
  assert.ok(!sb.__get().p['原始生命態ニビル'], '捨てられたあとに届いた分を残さない');
  reply(); await settle();
  assert.equal(sb.__get().p['原始生命態ニビル'].latest, sent.length + 100);
  console.log('購入候補の価格: 足したカードだけ取りに行くことを確認しました');
})().catch(e => { console.error(e); process.exit(1); });
