/**
 * dummy-card-ui.js
 * ダミーカード（イラストなしの枠だけの札。モンスター・魔法・罠の3色）を出す入口（2026-11-01）。
 * 展開プランを記録するときの手札コスト・素材など、中身は問わない1枚を表すために使う。
 *
 * 入口:
 *  PC   … 手札・墓地・除外の空いたところの右クリック → 「ダミーカードを出す」の3色
 *         空いたマスの右クリック（トークンのメニュー・token-generator.js）の下にも同じ3色
 *  スマホ縦向き … ⋯メニュー「ダミーカードを出す」→ 3色 → 手札へ
 *         空いたマスの長押し（mobile-ui.js）の「ダミーカードを出す…」→ 3色 → そのマスへ
 *
 * 仕組みはトークン（token-generator.js placeToken）と同じ: デッキ列に新しいカードを作り、
 * executeDrop で行き先へ移す（ドラッグで置いたときと同じ配置・リプレイ記録の経路）。
 * 記録の画像は card://予約名（utils/dummy-card.js）。盤面から消せるのはトークンと同じ（context-menu.js）。
 */

import { addCardToPool } from '../components/card-manager.js';
import { getDropZoneInfo, executeDrop } from '../components/drag-drop.js';
import { registerCardImage, registerCardName, registerCardType } from '../services/replay-service.js';
import { openContextMenuAt } from './context-menu.js';
import { isMobilePortrait } from '../utils/viewport.js';
import { DUMMY_KINDS, dummyDisplayOf, dummySrcOf } from '../utils/dummy-card.js';

/**
 * ダミーカードを作って行き先へ出す。
 * @param {Element} target - 行き先（.custom-slot・.center-slot（手札）・.side-slot（墓地・除外））
 * @param {'monster'|'spell'|'trap'} kind
 */
export function placeDummy(target, kind) {
  if (!DUMMY_KINDS[kind] || !target?.isConnected) return;
  const { name } = DUMMY_KINDS[kind];

  addCardToPool(dummyDisplayOf(kind), false, false);
  const pool = document.getElementById('poolRow');
  const wrapper = pool?.querySelector('.tier-item-wrapper:last-child');
  const cardEl = wrapper?.querySelector('.tier-item');
  if (!cardEl?.id) return;
  cardEl.dataset.cardName = name;
  cardEl.alt = name;
  registerCardImage(cardEl.id, dummySrcOf(kind));
  registerCardName(cardEl.id, name);
  registerCardType(cardEl.id, kind); // 再生・復元で種別（セットの形）を付け直すため

  const dropZoneInfo = getDropZoneInfo(target);
  if (!dropZoneInfo) {
    console.warn('[dummy-card-ui] 行き先の種別を判定できませんでした');
    return;
  }
  executeDrop({ type: 'card', element: wrapper }, dropZoneInfo, target, {});
}

/** 3色の項目（メニューに並べる形） */
export function dummyMenuItems(target) {
  return Object.entries(DUMMY_KINDS).map(([kind, d]) => ({
    label: d.label, action: () => placeDummy(target, kind),
  }));
}

/** 「ダミーカードを出す」のメニューを開く（見出し＋3色） */
export function openDummyMenu(target, x, y, heading = 'ダミーカードを出す') {
  openContextMenuAt([{ heading }, ...dummyMenuItems(target)], x, y);
}

/** 手札（縦向きスマホの ⋯メニューから・手札へ出す） */
export function handTarget() {
  return document.querySelector('.sol-hand-area .center-slot');
}

// 右クリックした場所 → 出す先と見出し（手札・墓地・除外。マスは token-generator.js のメニューに足す）
function _zoneTarget(el) {
  const hand = el.closest('.sol-hand-area');
  if (hand) return { target: hand.querySelector('.center-slot'), heading: 'ダミーカードを手札に加える' };
  const side = el.closest('.side-slot');
  if (side) return { target: side, heading: 'ダミーカードを置く' };
  return null;
}

export function initDummyCardUI() {
  document.addEventListener('contextmenu', (e) => {
    if (e.defaultPrevented) return;                     // カードのメニューなど、先に処理済み
    if (!(e.target instanceof Element)) return;
    if (e.target.closest('.tier-item-wrapper')) return; // カードの上はカードのメニュー
    if (isMobilePortrait()) return;
    const z = _zoneTarget(e.target);
    if (!z?.target) return;
    e.preventDefault();
    openDummyMenu(z.target, e.clientX, e.clientY, z.heading);
  });
}
