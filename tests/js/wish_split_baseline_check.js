/**
 * tests/js/wish_split_baseline_check.js
 *
 * 「まとめ買い最安店舗」の分割プラン比較（baseline）に関する2026-08-19の回帰テスト。
 *
 * バグの構造（司令塔の調査結果より）:
 *   templates/index.html の wishCheapestStores() は、分割プランと比較する「1店舗
 *   まとめ買いの最安値」（baseline）を `covered_qty===split.coveredQty` という
 *   枚数一致だけで選んでいた。中身のカード集合が違っても枚数さえ一致すれば比較対象に
 *   選ばれてしまい、まったく別の商品と比較した誤った金額差を表示していた
 *   （実例: 分割プラン8枚 ¥3,690 を、別の8枚しか揃わないカードラボ ¥4,340 と比較）。
 *   さらに、比較対象の店の「注文可否」も店の全カード合計（s.ship.orderable）を
 *   流用しており、分割プランが対象にした部分集合だけでは最低注文金額に届かない店を
 *   見逃していた。
 *
 * 2026-08-19レビュー指摘での修正:
 *   baseline選定ロジックを index.html から static/wish-split.js の純関数
 *   `selectSingleShopBaseline()` に切り出した。このファイルは複製を一切持たず、
 *   本物の wish-split.js を require して直接検証する（以前は index.html のロジックを
 *   複製したテストコードで、本番コードを実際には一切実行していなかった）。
 *
 * 固定する仕様（このスクリプトが検証する項目）:
 *   (a) 分割プランと同じカード集合（キー）を全部持つ店だけが baseline 候補になる。
 *       枚数（covered_qty）が同じだけの別集合の店は選ばれない。
 *   (b) splitKeys を全部持つが、その部分集合の小計では最低注文金額に届かない店は
 *       baseline から除外され、かつ hasAll===true になる（=「1店舗ではそろいません」
 *       という虚偽文言を出させないための土台）。
 *   (c) splitKeys を全部持つ店が1つも無いときは hasAll===false かつ baseline===null。
 *   (d) 送料無料しきい値の判定は、店の全カード合計ではなく部分集合の小計で行われる。
 *
 * 実行: node tests/js/wish_split_baseline_check.js
 * 終了コード0=全項目OK、非0=失敗（stderrにFAIL行）。
 */
'use strict';
const path = require('path');

const WISH_SPLIT_PATH = path.join(__dirname, '..', '..', 'static', 'wish-split.js');
const WishSplit = require(WISH_SPLIT_PATH);

let failures = 0;
function check(name, cond, detail) {
  if (cond) {
    console.log('OK: ' + name);
  } else {
    failures++;
    console.error('FAIL: ' + name + (detail ? ' — ' + detail : ''));
  }
}

// priceTable[key][shop] = price の組み立てヘルパー
function buildPriceTable(perKeyPerShop) {
  const priceTable = {};
  for (const [key, shopPrices] of Object.entries(perKeyPerShop)) {
    priceTable[key] = { ...shopPrices };
  }
  return priceTable;
}

// ── シナリオ(a): カバー枚数が同じだけの別集合の店を baseline に選ばないこと ──
function scenarioA_key_set_must_match_not_just_qty() {
  console.log('\n=== シナリオ(a): 分割プランと同じカード集合を持つ店だけがbaselineに選ばれる ===');

  const splitKeys = ['A', 'B'];
  const qtyByKey = new Map([['A', 1], ['B', 1]]);
  const rules = {}; // 送料ルール無し＝常に注文可（今回の検証には無関係な要素）

  // shopFull: A,Bを両方とも持つ（分割プランと同じカード集合を1店で揃えられる店）
  // shopPartial: A,Bは一切持たないが、たまたま他のキー（このテストのsplitKeysには
  //   登場しないカード）を安く売っている（実運用でqtyが絡むと起こりうる偶然の一致。
  //   司令塔調査の「別の8枚しか揃わないカードラボ」に対応）
  const priceTable = buildPriceTable({
    A: { shopX: 90, shopFull: 120 },
    B: { shopY: 90, shopFull: 130 },
    C: { shopPartial: 20 }, // splitKeysに含まれないキー
  });
  const shops = ['shopX', 'shopY', 'shopFull', 'shopPartial'];

  const result = WishSplit.selectSingleShopBaseline(splitKeys, qtyByKey, priceTable, shops, rules);

  check('splitKeysを全部持つ店(shopFull)が存在するのでhasAll===true',
    result.hasAll === true, JSON.stringify(result));
  check('shopPartial(別集合)ではなくshopFull(同一集合)がbaselineに選ばれる(250=120+130)',
    result.baseline === 250, 'baseline=' + result.baseline);
  check('baselineを出した店舗名もshopFullである', result.shop === 'shopFull', 'shop=' + result.shop);
}

// ── シナリオ(b): 部分集合の小計では最低注文金額に届かない店をbaselineから除外すること ──
function scenarioB_min_order_reevaluated_on_subset() {
  console.log('\n=== シナリオ(b): 部分集合の小計で最低注文金額を満たさない店を除外する ===');

  const splitKeys = ['A', 'B'];
  const qtyByKey = new Map([['A', 1], ['B', 1]]);
  const rules = {
    shopC: { base_fee: 50, free_threshold: null, min_order: 1000 },
  };

  // shopC: A,Bともに単価は安い(10円ずつ=小計20円)が、最低注文金額1,000円に届かない。
  // 「店の全カード合計なら注文できる」という状況を模すため、splitKeysに含まれない
  // 高額カードDも持たせる（priceTableには入れるがsplitKeysには含めない＝関数は
  // splitKeysの小計だけを見て判定しなければならない）
  const priceTable = buildPriceTable({
    A: { shopX: 40, shopC: 10 },
    B: { shopY: 40, shopC: 10 },
    D: { shopC: 2000 }, // splitKeys外。店の全カード合計を使うと誤って注文可になってしまう
  });
  const shops = ['shopX', 'shopY', 'shopC'];

  const result = WishSplit.selectSingleShopBaseline(splitKeys, qtyByKey, priceTable, shops, rules);

  check('shopCはsplitKeys(A,B)を両方持つのでhasAll===true', result.hasAll === true,
    JSON.stringify(result));
  check('shopCは部分集合の小計(20円)では最低注文金額(1,000円)に届かず除外され、他に候補が無いのでbaseline===null',
    result.baseline === null, 'baseline=' + result.baseline);
}

// ── シナリオ(c): splitKeysを全部持つ店が1つも無いとき hasAll===false ──
function scenarioC_no_shop_has_full_set() {
  console.log('\n=== シナリオ(c): splitKeysを全部持つ店が無ければhasAll===false ===');

  const splitKeys = ['A', 'B'];
  const qtyByKey = new Map([['A', 1], ['B', 1]]);
  const rules = {};
  const priceTable = buildPriceTable({
    A: { shopX: 90 },
    B: { shopY: 90 },
  });
  const shops = ['shopX', 'shopY'];

  const result = WishSplit.selectSingleShopBaseline(splitKeys, qtyByKey, priceTable, shops, rules);

  check('どの店もA,Bを両方は持たないのでhasAll===false', result.hasAll === false,
    JSON.stringify(result));
  check('baselineもnull', result.baseline === null, 'baseline=' + result.baseline);
  check('shopもnull', result.shop === null, 'shop=' + result.shop);
}

// ── シナリオ(d): 送料無料しきい値の判定は部分集合の小計で行うこと ──
function scenarioD_free_shipping_threshold_uses_subset_subtotal() {
  console.log('\n=== シナリオ(d): 送料無料しきい値は店の全カード合計ではなく部分集合の小計で判定する ===');

  const splitKeys = ['A', 'B'];
  const qtyByKey = new Map([['A', 1], ['B', 1]]);
  // free_threshold=1000: 部分集合の小計(600)だけでは無料しきい値に届かず送料100円がかかる。
  // 店の全カード合計(splitKeys外のDを含めると1,600)を使うと誤って送料無料と判定してしまう
  const rules = {
    shopC: { base_fee: 100, free_threshold: 1000, min_order: null },
  };
  const priceTable = buildPriceTable({
    A: { shopC: 300 },
    B: { shopC: 300 },
    D: { shopC: 1000 }, // splitKeys外の高額カード。全カード合計を使うバグを検出する罠
  });
  const shops = ['shopC'];

  const result = WishSplit.selectSingleShopBaseline(splitKeys, qtyByKey, priceTable, shops, rules);

  check('shopCがbaselineに選ばれる', result.shop === 'shopC', JSON.stringify(result));
  check('部分集合の小計(600)では送料無料しきい値に届かず送料100円が乗り、baseline=700になる'
    + '（店の全カード合計1,600を使うと誤って送料無料の600になる）',
    result.baseline === 700, 'baseline=' + result.baseline);
}

scenarioA_key_set_must_match_not_just_qty();
scenarioB_min_order_reevaluated_on_subset();
scenarioC_no_shop_has_full_set();
scenarioD_free_shipping_threshold_uses_subset_subtotal();

console.log('\n' + (failures === 0 ? 'ALL OK' : failures + ' FAILURE(S)'));
process.exit(failures === 0 ? 0 : 1);
