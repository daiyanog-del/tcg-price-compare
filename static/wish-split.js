/**
 * wish-split.js — 複数店に分けて買った場合の最安プランを計算する（表示は1行のヒントのみ）
 *
 * 背景（2026-08-05 決定）:
 *   「まとめ買い最安店舗」は1店舗でまとめ買いした場合の比較。カードごとに最安店へ
 *   分けると安くなる余地があるが、送料が店舗数ぶん重複するため単純合計では比較できない。
 *   本モジュールは送料（無料しきい値含む）と最低注文金額を織り込んだ分割プランを計算する。
 *
 * アルゴリズム（案A・全列挙＋局所改善）:
 *   店舗は最大6店なので「どの店舗の組合せを使うか」は 2^6=64 通りしかない。
 *   各組合せについて (1) カードごとに組合せ内の最安店へ割り当て（貪欲）
 *   (2) 1エントリずつ別の店へ動かして総額が下がるかの入れ替え探索（局所改善）
 *   を行う。最低注文金額の違反はペナルティ付きスコアで同じループ内で修復する。
 *
 * 保証の向き:
 *   返すプランは実行可能（各店の最低注文金額を満たす）なので、表示する金額は
 *   「実際にこの通り買える」。理論上の厳密最適である保証は無いが、外れる方向は
 *   「節約額を控えめに出す」側にしかならない（過大表示にはならない）。
 *
 * 前提:
 *   - 同一エントリ（カード名×レアリティ）の複数枚は同じ店でまとめて買う（枚数を店間で
 *     割らない）。しきい値調整のために枚数を割ると得なケースは稀で、UIも複雑になるため。
 *   - 送料・最低注文金額の規則は /api/wish-shop-totals の shipping_rules（shipping.py が正本）。
 *
 * 純粋ロジックのみ（DOM 非依存）。node で単体検証できるよう module.exports も持つ。
 */
(function (global) {
  "use strict";

  // 送料。index.html 側 _wishShipping と同じ規則（subtotal<=0 は送料なし）
  function shipFee(rule, subtotal) {
    if (!rule || subtotal <= 0) return 0;
    if (rule.free_threshold !== null && rule.free_threshold !== undefined &&
        subtotal >= rule.free_threshold) return 0;
    return rule.base_fee || 0;
  }

  // 最低注文金額の不足額（0=満たしている／制限なし／買う物が無い）
  function minOrderDeficit(rule, subtotal) {
    if (!rule || subtotal <= 0) return 0;
    if (rule.min_order === null || rule.min_order === undefined) return 0;
    return Math.max(0, rule.min_order - subtotal);
  }

  // 割り当て（entryKey→shop）からプランの総額と妥当性を求める
  function evalPlan(assign, entries, priceTable, rules) {
    const sub = {};
    for (const e of entries) {
      const shop = assign.get(e.key);
      sub[shop] = (sub[shop] || 0) + priceTable[e.key][shop] * e.qty;
    }
    let itemsTotal = 0, shippingTotal = 0, deficit = 0;
    for (const shop of Object.keys(sub)) {
      itemsTotal += sub[shop];
      shippingTotal += shipFee(rules[shop], sub[shop]);
      deficit += minOrderDeficit(rules[shop], sub[shop]);
    }
    const total = itemsTotal + shippingTotal;
    // 最低注文金額の違反は巨額ペナルティで表現し、局所改善のループが自然に修復を試みる
    return { total, sub, deficit, score: total + deficit * 1e6 };
  }

  /**
   * 最安の分割プランを返す。
   * @param {Array<{key:string, qty:number}>} entries 購入エントリ
   * @param {Object<string, Object<string, number>>} priceTable priceTable[key][shop] = 単価
   * @param {string[]} shops 対象店舗名
   * @param {Object<string, {base_fee:number, free_threshold:?number, min_order:?number}>} rules
   * @returns {?{total:number, used:Array<{shop:string, subtotal:number, shipping:number, entryCount:number}>,
   *            coveredQty:number, assign:Map<string,string>}}
   */
  function computeBestSplit(entries, priceTable, shops, rules) {
    // どこかの店に価格があるエントリだけが対象
    const coverable = entries.filter(
      (e) => priceTable[e.key] && shops.some((s) => priceTable[e.key][s] !== undefined)
    );
    if (!coverable.length) return null;
    const useful = shops.filter(
      (s) => coverable.some((e) => priceTable[e.key][s] !== undefined)
    );

    let best = null;
    for (let mask = 1; mask < (1 << useful.length); mask++) {
      const S = useful.filter((_, i) => mask & (1 << i));
      // この組合せで全エントリを賄えるか
      if (!coverable.every((e) => S.some((s) => priceTable[e.key][s] !== undefined))) continue;

      // (1) 貪欲: 組合せ内の最安店へ
      const assign = new Map();
      for (const e of coverable) {
        let bs = null, bp = Infinity;
        for (const s of S) {
          const p = priceTable[e.key][s];
          if (p !== undefined && p < bp) { bp = p; bs = s; }
        }
        assign.set(e.key, bs);
      }
      let plan = evalPlan(assign, coverable, priceTable, rules);

      // (2) 局所改善: 1エントリの移動でスコアが下がる限り繰り返す（上限つき）
      for (let iter = 0; iter < 300; iter++) {
        let bestMove = null;
        for (const e of coverable) {
          const cur = assign.get(e.key);
          for (const s of S) {
            if (s === cur || priceTable[e.key][s] === undefined) continue;
            assign.set(e.key, s);
            const cand = evalPlan(assign, coverable, priceTable, rules);
            if (cand.score < plan.score - 1e-9 &&
                (!bestMove || cand.score < bestMove.plan.score)) {
              bestMove = { key: e.key, to: s, plan: cand };
            }
            assign.set(e.key, cur);
          }
        }
        if (!bestMove) break;
        assign.set(bestMove.key, bestMove.to);
        plan = bestMove.plan;
      }

      if (plan.deficit > 0) continue;  // 修復しきれない組合せは捨てる（他の組合せが拾う）
      if (!best || plan.total < best.plan.total) {
        best = { plan, assign: new Map(assign) };
      }
    }
    if (!best) return null;

    const used = Object.keys(best.plan.sub)
      .filter((shop) => best.plan.sub[shop] > 0)
      .map((shop) => ({
        shop,
        subtotal: best.plan.sub[shop],
        shipping: shipFee(rules[shop], best.plan.sub[shop]),
        entryCount: [...best.assign.values()].filter((s) => s === shop).length,
      }))
      .sort((a, b) => b.subtotal - a.subtotal);
    const coveredQty = coverable.reduce((s, e) => s + e.qty, 0);
    return { total: best.plan.total, used, coveredQty, assign: best.assign };
  }

  /**
   * 分割プランと同じカード集合を「1店舗だけで」揃えた場合の最安（送料込み）を求める。
   * index.html の baseline 選定ロジック（2026-08-19）をここに切り出したもの。テストが
   * 本番コードの複製ではなくこの関数自体を検証できるようにする（レビュー指摘対応）。
   * @param {string[]} splitKeys 分割プランが対象にしたエントリキー
   * @param {Map<string, number>} qtyByKey キー→枚数
   * @param {Object<string, Object<string, number>>} priceTable priceTable[key][shop] = 単価
   * @param {string[]} shops 対象店舗名
   * @param {Object} rules 送料ルール
   * @returns {{hasAll: boolean, baseline: ?number, shop: ?string}}
   *   hasAll   = splitKeys を全部持つ店が1つでも存在したか（注文可否は問わない）
   *   baseline = そのうち注文できる店の送料込み最安。該当なしは null
   *   shop     = baseline を出した店舗名。該当なしは null
   */
  function selectSingleShopBaseline(splitKeys, qtyByKey, priceTable, shops, rules) {
    let hasAll = false;
    let baseline = null;
    let baselineShop = null;
    for (const shop of shops) {
      // 「その店が splitKeys を全部持つか」は priceTable で判定する（店の全カード合計は使わない）
      const coversAll = splitKeys.every(
        (k) => priceTable[k] && priceTable[k][shop] !== undefined
      );
      if (!coversAll) continue;
      hasAll = true;
      // 小計は splitKeys ぶんだけの合計（店の全カード合計 s.total ではない）
      let subtotal = 0;
      for (const k of splitKeys) subtotal += priceTable[k][shop] * qtyByKey.get(k);
      if (minOrderDeficit(rules[shop], subtotal) !== 0) continue;  // 注文不可の店は候補から外す
      const total = subtotal + shipFee(rules[shop], subtotal);
      if (baseline === null || total < baseline) {
        baseline = total;
        baselineShop = shop;
      }
    }
    return { hasAll, baseline, shop: baselineShop };
  }

  const api = { computeBestSplit, shipFee, minOrderDeficit, evalPlan, selectSingleShopBaseline };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  global.WishSplit = api;
})(typeof window !== "undefined" ? window : globalThis);
