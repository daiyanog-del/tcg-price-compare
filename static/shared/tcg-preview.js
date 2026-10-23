/**
 * tcg-preview.js — TCG先行カード（英語版で先に出て日本に無いカード）の名前の一覧（2026-10-23 段2）
 *
 * TCG先行カードは英語名でデッキに入るが価格が無いので、購入候補に入れる操作を出さない。
 * 保存した盤面・リプレイの復元などカードの作られ方がいくつもあるため、カードに印を付けるのではなく
 * 名前の一覧（/api/tcg-preview-names）を1回だけ取り、購入候補の入口ごとに has(name) で確かめる。
 *   - トップ（index-main.js・マイデッキ）とシミュレーター（deck-edit-ui.js・deck-wish-ui.js）の両方から使う
 *   - 一覧が取れないうちは false（購入候補の操作は出る。入れても価格が出ないだけで害は小さい）
 *   - 候補（/api/suggest）の tcg_preview の印で知った名前は note() で足す（一覧の取得より先に選ばれても漏れない）
 */
(function (global) {
  'use strict';
  const names = new Set();
  const ready = fetch('/api/tcg-preview-names')
    .then(r => (r.ok ? r.json() : null))
    .then(d => { if (d && Array.isArray(d.names)) d.names.forEach(n => { if (typeof n === 'string') names.add(n); }); })
    .catch(() => {});

  global.TcgPreview = {
    /** TCG先行カードの名前か */
    has: name => names.has(name),
    /** TCG先行カードと分かった名前を足す */
    note: name => { if (typeof name === 'string' && name) names.add(name); },
    /** 一覧を取り終えたら解決する（失敗しても解決する） */
    ready,
  };
})(window);
