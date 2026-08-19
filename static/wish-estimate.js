/**
 * wish-estimate.js — 購入候補の見積もり計算（calcWishEstimate）のうち、
 * サーバ正本(entries)とlocalStorageの生データ(list)を突合する純粋ロジック。
 *
 * 背景（2026-08-19、まとめ買い機能(wishCheapestStores)で直したのと同根のバグ）:
 *   calcWishEstimate() は `const e=list[i]`（localStorageの生データ）を、サーバが返す
 *   補正済みの it.name／正規化済みの it.rarity_pref とそのまま突合していた。保存値の
 *   カード名が _correct_cardname() で補正される表記だったり、rarity が「ウルトラレア」の
 *   ような旧表記のままだったりすると突合が外れ、DBに価格があるのに「データなし」扱いで
 *   総額から丸ごと落ちていた。
 *
 *   サーバ(/api/wish-shop-totals)は既に補正済み・正規化済み・重複統合済みの entries を
 *   返している（まとめ買い機能で先に対応済み）。本モジュールは、その entries を
 *   表示・計算の両方の正本として使うための整列ロジックを持つ。
 *
 * 行インデックスのズレについて:
 *   画面のグリッド（wish-row-<i>）は fetch より前に localStorage の list から
 *   描画されている。entries は重複統合で件数・内容が変わりうるため、list と entries が
 *   完全一致しない場合だけグリッドを entries ベースで描き直す設計にした（一致するときは
 *   描き直さない＝通常ケースで画像の再取得やちらつきを避ける）。
 *
 * pickPerShopItem について（2026-08-19追加）:
 *   /api/deck の per_shop（リアルタイム検索の店舗別集計）から1店舗ぶんの採用候補を
 *   選ぶ純関数。見積もりのリアルタイム補完（calcWishEstimate Phase2）と、まとめ買い
 *   最安店舗のリアルタイム補完（wishCheapestStores）の両方から使われる、共通の
 *   「per_shop の中から1件選ぶ」ロジックをここに集約する。
 *
 * 純粋ロジックのみ（DOM 非依存）。node で単体検証できるよう module.exports も持つ。
 */
(function (global) {
  "use strict";

  // (name, rarity) から一意のキーを作る。制御文字で区切ることで
  // 「name="A",rarity="BC"」と「name="AB",rarity="C"」のような衝突を避ける
  function entryKey(name, rarity) {
    return (name || "") + "" + (rarity || "");
  }

  /**
   * サーバの entries（補正・正規化・重複統合済み）を、表示・計算に使う workingList に
   * 変換する。list（localStorageの生データ）と内容が完全一致するかも判定して返す。
   * @param {Array<{name:string,qty:number,rarity?:string}>} list localStorageの生データ
   * @param {Array<{name:string,qty:number,rarity?:string}>} srvEntries サーバ正本
   * @returns {{workingList: Array<{name:string,qty:number,rarity:string}>, needsRedraw: boolean}}
   */
  function buildWorkingList(list, srvEntries) {
    const workingList = (srvEntries || []).map((e) => ({
      name: e.name, qty: e.qty, rarity: e.rarity || "",
    }));
    let needsRedraw = list.length !== workingList.length;
    if (!needsRedraw) {
      for (let i = 0; i < list.length; i++) {
        if (list[i].name !== workingList[i].name ||
            (list[i].rarity || "") !== workingList[i].rarity) {
          needsRedraw = true;
          break;
        }
      }
    }
    return { workingList, needsRedraw };
  }

  /**
   * 1エントリについて、全店舗(shops)の中から最安を探す。
   * 照合は希望レアリティ（rarity_pref）で行う。未指定エントリ（rarity=""）はDBの
   * 「最安レアリティ採用」行（rarity_pref=""）とだけ一致させる。
   * @param {{name:string, rarity?:string}} entry
   * @param {Array} shops resp.shops（/api/wish-shop-totals のレスポンス）
   * @returns {?{price:number, shop:string, rarity:string}}
   */
  function findBestPrice(entry, shops) {
    let best = null;
    for (const shop of shops || []) {
      const item = (shop.items || []).find((it) => {
        if (it.name !== entry.name) return false;
        const pref = (it.rarity_pref !== undefined && it.rarity_pref !== null)
          ? it.rarity_pref : (it.rarity || "");
        return pref === (entry.rarity || "");
      });
      if (!item) continue;
      if (!best || item.price < best.price) {
        best = { price: item.price, shop: shop.shop, rarity: item.rarity || "" };
      }
    }
    return best;
  }

  /**
   * /api/deck の per_shop（{店舗名: {レアリティ: {price,url,rarity,...}}}）から、
   * 1店舗ぶんの採用候補を選ぶ純関数。
   *
   * 背景（2026-08-19、店舗別集計がレアリティを潰すバグの修正）:
   *   per_shop は以前「店につき最安1件」に畳まれており、ある店がノーマルとウルトラの
   *   両方を在庫していてもノーマルしか返らなかった。ユーザーがウルトラを指定していると
   *   その店は「ウルトラを持っていない」と誤判定されていた（実際には持っている）。
   *   サーバ側は店舗→レアリティの入れ子に直したので、こちらは入れ子から選ぶだけでよい。
   *
   * @param {?Object<string, {price:number,url?:string,rarity?:string}>} shopRarities
   *   perShop[店舗名]。その店が1件もヒットしていなければ undefined/null。
   * @param {string} rarityPref 希望レアリティ（未指定は ""）
   * @returns {?{price:number,url:string,rarity:string}}
   *   rarityPref が指定されていればその一致のみ、未指定なら全レアリティ中の最安
   *   （＝旧実装の「レアリティ未指定なら店の最安を採用」という挙動を維持する）
   */
  function pickPerShopItem(shopRarities, rarityPref) {
    if (!shopRarities) return null;
    if (rarityPref) {
      return shopRarities[rarityPref] || null;
    }
    let best = null;
    for (const rarity of Object.keys(shopRarities)) {
      const item = shopRarities[rarity];
      if (!item) continue;
      if (!best || item.price < best.price) best = item;
    }
    return best;
  }

  /**
   * db_missing（サーバがどの店にも価格を見つけられなかったキー集合）に無いのに
   * findBestPrice が null を返したエントリを検出する。サーバは価格を持っているはずなのに
   * こちら側の突合で見つけられなかった＝照合キー不一致のバグの疑いがある異常系。
   * 「落ちたことが分からない」のが本来の問題なので、このケースは呼び出し側で必ず
   * console.warn すること。
   * @param {Array<{name:string,rarity:string}>} workingList
   * @param {Array<?Object>} bestResults workingListと同じ長さ。findBestPriceの結果
   * @param {Array<{name:string,rarity:string}>} dbMissing resp.db_missing
   * @returns {Array<{name:string,rarity:string}>} 疑わしいエントリ
   */
  function detectSuspiciousMisses(workingList, bestResults, dbMissing) {
    const dbMissingKeys = new Set((dbMissing || []).map((m) => entryKey(m.name, m.rarity)));
    const suspicious = [];
    for (let i = 0; i < workingList.length; i++) {
      if (bestResults[i]) continue;
      const key = entryKey(workingList[i].name, workingList[i].rarity);
      if (!dbMissingKeys.has(key)) suspicious.push(workingList[i]);
    }
    return suspicious;
  }

  const api = { entryKey, buildWorkingList, findBestPrice, pickPerShopItem, detectSuspiciousMisses };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  global.WishEstimate = api;
})(typeof window !== "undefined" ? window : globalThis);
