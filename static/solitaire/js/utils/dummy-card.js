/**
 * dummy-card.js
 * ダミーカード（「どのカードでもよい1枚」をイラストなしの枠で表す札）の共通部品（副作用なし・他を import しない）。
 *
 * 2026-11-01 追加。展開プランを記録するときの手札コスト・素材など、カードの中身は問わないが
 * 1枚として必要なリソースを表すために使う。モンスター・魔法・罠の3色。
 *
 * 記録・保存・共有では card://ダミー（モンスター） のような予約した名前のセンチネルで持つ。
 * card:// はリプレイの画像の方針（replay-image-policy.js）とサーバーの検査をそのまま通り、
 * この部品を知らない表示（サーバーが描く盤面画像など）でも名前だけの札で出るため。
 * 画面に出すときは /api/card-images に問い合わせず、ここで枠の画像（images/dummy-*.svg）に置き換える。
 */

// 種別ごとの名前と枠の画像（?v= は差し替えたときに上げる）
export const DUMMY_KINDS = {
  monster: { name: 'ダミー（モンスター）', label: 'モンスター', url: new URL('../../images/dummy-monster.svg?v=1', import.meta.url).href },
  spell:   { name: 'ダミー（魔法）',       label: '魔法',       url: new URL('../../images/dummy-spell.svg?v=1', import.meta.url).href },
  trap:    { name: 'ダミー（罠）',         label: '罠',         url: new URL('../../images/dummy-trap.svg?v=1', import.meta.url).href },
};

const _KIND_BY_NAME = new Map(Object.entries(DUMMY_KINDS).map(([kind, d]) => [d.name, kind]));

/** カード名がダミーの予約名なら種別（'monster'|'spell'|'trap'）、違えば null */
export function dummyKindOfName(name) {
  return _KIND_BY_NAME.get(String(name ?? '')) || null;
}

/** 画像辞書の値（card://名前）がダミーなら種別、違えば null */
export function dummyKindOfSrc(src) {
  if (typeof src !== 'string' || !src.startsWith('card://')) return null;
  return dummyKindOfName(src.slice('card://'.length));
}

/** 記録・保存に使う src（card://予約名） */
export function dummySrcOf(kind) {
  return `card://${DUMMY_KINDS[kind].name}`;
}

/**
 * card:// の解決結果と同じ形の表示指定（card-manager.js createCardElement・リプレイ／盤面の復元が受け取る形）。
 * source:'dummy' は透かし・イラストぼかしの対象外の印。
 */
export function dummyDisplayOf(kind) {
  return { kind: 'image', url: DUMMY_KINDS[kind].url, source: 'dummy', dummyKind: kind };
}

/**
 * ダミーの印を wrapper に付ける（data-dummy="1"・種別）。カードを作る全経路から呼ぶ。
 * 種別を付けるのは、セット・場に出すメニューをモンスター／魔法・罠で出し分けるため（card-state.js isMonsterCard）。
 * @param {Element} wrapper - .tier-item-wrapper
 * @param {string|null} kind
 */
export function markDummy(wrapper, kind) {
  if (!kind) return;
  wrapper.dataset.dummy = '1';
  wrapper.dataset.cardType = kind;
}

/** wrapper がダミーか */
export function isDummyCard(wrapper) {
  return wrapper?.dataset?.dummy === '1';
}
