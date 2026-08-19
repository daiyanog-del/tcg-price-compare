/**
 * tests/js/wish_estimate_check.js
 *
 * 「購入候補の見積もり計算」（calcWishEstimate）に関する2026-08-19の回帰テスト。
 * wishCheapestStores() で直したのと同根のバグ:
 *
 *   calcWishEstimate() は `const e=list[i]`（localStorageの生データ）を、サーバが返す
 *   補正済みの it.name／正規化済みの it.rarity_pref とそのまま突合していた。保存値の
 *   rarity が「ウルトラレア」のような旧表記のまま（正準名は「ウルトラ」）だったり、
 *   カード名が _correct_cardname() で補正される表記だったりすると突合が外れ、DBに価格が
 *   あるのに「データなし」扱いで総額から丸ごと落ちていた。
 *
 * このスクリプトは複製ロジックではなく、本物の static/wish-estimate.js を require して
 * 直接検証する。
 *
 * 実行: node tests/js/wish_estimate_check.js
 * 終了コード0=全項目OK、非0=失敗（stderrにFAIL行）。
 */
'use strict';
const path = require('path');

const WISH_ESTIMATE_PATH = path.join(__dirname, '..', '..', 'static', 'wish-estimate.js');
const WishEstimate = require(WISH_ESTIMATE_PATH);

let failures = 0;
function check(name, cond, detail) {
  if (cond) {
    console.log('OK: ' + name);
  } else {
    failures++;
    console.error('FAIL: ' + name + (detail ? ' — ' + detail : ''));
  }
}

// /api/wish-shop-totals の shops[].items[] を模したヘルパ
function shopWithItems(shopName, items) {
  return { shop: shopName, items };
}

// ── シナリオ1: rarity表記ゆれ（「ウルトラレア」保存 vs 正準名「ウルトラ」）で
//   バグ再現時の挙動（生の list 値で突合すると見つからない）と、修正後の挙動
//   （entries で突合すると見つかる）を対比する ──
function scenario1_rarity_normalization() {
  console.log('\n=== シナリオ1: rarity正規化のズレ（"ウルトラレア" 保存 vs 正準名 "ウルトラ"） ===');

  // localStorageの生データ（旧表記のまま保存されている）
  const list = [{ name: 'テストカードA', qty: 1, rarity: 'ウルトラレア' }];
  // サーバ(/api/wish-shop-totals)が返す正本。rarityは正準名へ正規化されている
  const srvEntries = [{ name: 'テストカードA', qty: 1, rarity: 'ウルトラ' }];
  const shops = [
    shopWithItems('カードラボ', [
      { name: 'テストカードA', qty: 1, price: 600, rarity: 'ウルトラ', rarity_pref: 'ウルトラ' },
    ]),
  ];

  // バグ再現: 修正前の calcWishEstimate は list の生値をそのまま entry として使っていた
  const buggyBest = WishEstimate.findBestPrice(list[0], shops);
  check('再現: list の生rarity（"ウルトラレア"）で突合すると価格が見つからない（バグそのもの）',
    buggyBest === null, 'buggyBest=' + JSON.stringify(buggyBest));

  // 修正後: entries を正本にした workingList で突合する
  const { workingList, needsRedraw } = WishEstimate.buildWorkingList(list, srvEntries);
  check('修正: workingList[0].rarity は正準名 "ウルトラ" に揃う',
    workingList[0].rarity === 'ウルトラ', JSON.stringify(workingList));
  const fixedBest = WishEstimate.findBestPrice(workingList[0], shops);
  check('修正: workingList で突合すると価格600円が見つかる（DBに価格があるのに落ちない）',
    fixedBest && fixedBest.price === 600, JSON.stringify(fixedBest));
  // list側のrarity表記("ウルトラレア")と正規化後("ウルトラ")が異なるため、この
  // ケースはneedsRedraw===trueが正しい（グリッドの表示も正規化後の値に揃える）
  check('rarityが正規化で変わるためグリッドの再描画が必要と判定される（needsRedraw===true）',
    needsRedraw === true, 'needsRedraw=' + needsRedraw);
}

// ── シナリオ2: カード名補正（_correct_cardname 相当）が入るケース ──
function scenario2_name_correction() {
  console.log('\n=== シナリオ2: カード名補正で保存名とDB名がズレるケース ===');

  const list = [{ name: 'ブラックマジシャン', qty: 2, rarity: '' }];
  // サーバ側で正式名称へ補正された
  const srvEntries = [{ name: 'ブラック・マジシャン', qty: 2, rarity: '' }];
  const shops = [
    shopWithItems('遊々亭', [
      { name: 'ブラック・マジシャン', qty: 2, price: 50, rarity: 'ノーマル', rarity_pref: '' },
    ]),
  ];

  const buggyBest = WishEstimate.findBestPrice(list[0], shops);
  check('再現: 補正前の名前で突合すると見つからない', buggyBest === null);

  const { workingList, needsRedraw } = WishEstimate.buildWorkingList(list, srvEntries);
  check('名前補正で list と内容が変わるため needsRedraw===true（グリッド再描画が必要）',
    needsRedraw === true);
  const fixedBest = WishEstimate.findBestPrice(workingList[0], shops);
  check('修正: 補正後の名前で突合すると価格50円が見つかる',
    fixedBest && fixedBest.price === 50, JSON.stringify(fixedBest));
}

// ── シナリオ3: 重複統合（2つのlistエントリが同一正規化キーに収束する）で
//   行インデックスがズレず、workingListが正しく再構成されること ──
function scenario3_dedup_realigns_rows() {
  console.log('\n=== シナリオ3: サーバ側の重複統合でentries件数がlistより減るケース ===');

  // 表記ゆれで別々に追加された2行が、サーバ側では同一キーに統合されてqty加算される
  const list = [
    { name: 'テストカードB', qty: 1, rarity: '' },
    { name: 'テストカードB', qty: 2, rarity: 'ノーマル' }, // rarity違いだが正規化後は同じキーになったと仮定
  ];
  const srvEntries = [{ name: 'テストカードB', qty: 3, rarity: '' }];

  const { workingList, needsRedraw } = WishEstimate.buildWorkingList(list, srvEntries);
  check('件数が変わるので needsRedraw===true', needsRedraw === true);
  check('workingListはentries由来の1件になり、qtyが合算されている（3）',
    workingList.length === 1 && workingList[0].qty === 3, JSON.stringify(workingList));
}

// ── シナリオ4: 通常ケース（表記ゆれなし）で結果が変わらないこと ──
function scenario4_normal_case_unchanged() {
  console.log('\n=== シナリオ4: 表記ゆれの無い通常ケースは従来どおり（needsRedraw===false） ===');

  const list = [
    { name: 'テストカードC', qty: 1, rarity: '' },
    { name: 'テストカードD', qty: 3, rarity: 'シークレット' },
  ];
  const srvEntries = [
    { name: 'テストカードC', qty: 1, rarity: '' },
    { name: 'テストカードD', qty: 3, rarity: 'シークレット' },
  ];
  const { workingList, needsRedraw } = WishEstimate.buildWorkingList(list, srvEntries);
  check('内容が完全一致するので needsRedraw===false', needsRedraw === false);
  check('workingListの中身はlistと同じ', JSON.stringify(workingList) === JSON.stringify(list));
}

// ── シナリオ5: 突合できないのにdb_missingにも無い＝照合キー不一致の疑いを検出する ──
function scenario5_suspicious_miss_detection() {
  console.log('\n=== シナリオ5: db_missingに無いのに見つからないエントリを異常検知する ===');

  const workingList = [
    { name: 'テストカードE', qty: 1, rarity: '' }, // dbMissingにある正当な欠落
    { name: 'テストカードF', qty: 1, rarity: '' }, // dbMissingに無いのに見つからない＝バグ疑い
  ];
  const bestResults = [null, null];
  const dbMissing = [{ name: 'テストカードE', rarity: '' }];

  const suspicious = WishEstimate.detectSuspiciousMisses(workingList, bestResults, dbMissing);
  check('正当な欠落（db_missingにある）は疑わしいリストに入らない',
    !suspicious.some((s) => s.name === 'テストカードE'));
  check('db_missingに無いのに見つからないエントリは疑わしいリストに入る（console.warnの発火対象）',
    suspicious.some((s) => s.name === 'テストカードF'), JSON.stringify(suspicious));
}

// ── シナリオ6: pickPerShopItem — 店舗別集計がレアリティを潰すバグ（2026-08-19）の回帰 ──
//   /api/deck の per_shop は旧実装だと「店につき最安1件」に畳まれていた。ある店が
//   ノーマル¥100とウルトラ¥3,000を両方在庫していても、返るのはノーマル¥100だけで、
//   ユーザーがウルトラを指定しているとその店は「持っていない」と誤判定されていた。
//   修正後の per_shop は {店舗名:{レアリティ:{price,url,rarity}}} の入れ子で、
//   pickPerShopItem がその中から1件選ぶ。
function scenario6_pick_per_shop_item() {
  console.log('\n=== シナリオ6: pickPerShopItem（店舗別・レアリティ別集計からの選択） ===');

  // 修正後の per_shop 形（1店舗が複数レアリティを在庫している）
  const shopRarities = {
    'ノーマル': { price: 100, url: 'u1', rarity: 'ノーマル' },
    'ウルトラ': { price: 3000, url: 'u2', rarity: 'ウルトラ' },
  };

  // 再現: レアリティ指定「ウルトラ」で、店が実際に持っているウルトラが選ばれること
  const ultra = WishEstimate.pickPerShopItem(shopRarities, 'ウルトラ');
  check('修正: レアリティ「ウルトラ」を指定するとウルトラ¥3,000が選ばれる（＝店にウルトラがあると正しく判定される）',
    ultra && ultra.price === 3000, JSON.stringify(ultra));

  // 在庫していないレアリティを指定した場合は選ばれない（対象外）
  const secret = WishEstimate.pickPerShopItem(shopRarities, 'シークレット');
  check('店が持っていないレアリティを指定すると null（その店は対象外）',
    secret === null, JSON.stringify(secret));

  // レアリティ未指定（""）は全レアリティ中の最安を採用する（従来挙動の維持）
  const cheapest = WishEstimate.pickPerShopItem(shopRarities, '');
  check('レアリティ未指定なら店の全レアリティ中の最安（ノーマル¥100）が選ばれる（回帰なし）',
    cheapest && cheapest.price === 100, JSON.stringify(cheapest));

  // レアリティ不明の商品は "" キーに残る（捨てられない）
  const unknownRarityShop = { '': { price: 200, url: 'u3', rarity: '' } };
  const picked = WishEstimate.pickPerShopItem(unknownRarityShop, '');
  check('レアリティ不明（""キー）の商品も未指定時の最安候補になる（捨てられない）',
    picked && picked.price === 200, JSON.stringify(picked));
  const pickedWithPref = WishEstimate.pickPerShopItem(unknownRarityShop, 'ウルトラ');
  check('レアリティ不明（""キー）の商品は、別レアリティを指定すると選ばれない',
    pickedWithPref === null, JSON.stringify(pickedWithPref));

  // その店が1件もヒットしていない（perShop[shop]が無い）場合は null
  const none = WishEstimate.pickPerShopItem(undefined, 'ウルトラ');
  check('その店が1件もヒットしていなければ null', none === null, JSON.stringify(none));
}

scenario1_rarity_normalization();
scenario2_name_correction();
scenario3_dedup_realigns_rows();
scenario4_normal_case_unchanged();
scenario5_suspicious_miss_detection();
scenario6_pick_per_shop_item();

console.log('\n' + (failures === 0 ? 'ALL OK' : failures + ' FAILURE(S)'));
process.exit(failures === 0 ? 0 : 1);
