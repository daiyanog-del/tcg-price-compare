/**
 * landing-mark.js
 * 置いた先の印: 再生で1手だけ進んだとき（次へ・自動再生・1つ先への移動）、動いたカードが着地したところで
 * カードの四隅に白いかぎ括弧を一瞬出す（2026-10-29 ユーザー選択の案A。モック _mocks/landing-mark-20261029）。
 * 録画した動画で「どこに置いたか」を目で追えるようにする（X の他ツールの展開動画との比較から）。
 *
 * - 合図は replay-service の navigated の2つ目の引数（1手だけ進んだか）。巻き戻し・スライダーで飛んだときは出さない。
 * - 出す手: 盤面・手札・墓地・除外へ移す moveCard と draw。デッキへ戻す手（山が光る演出がある）・
 *   同じマスで表示形式だけを変える手（inPlace）・効果発動などカードが動かない手には出さない。
 * - 着地は、飛んでいる複製（card-effects.js の _flipClone）が本体から外れた時点。外れるまで rAF で待つ。
 * - 色は白・形は角だけ。金の実線（選択中）・アクセント色の実線（触れている間）・破線（置き場所の案内）と重ねない
 *   （CLAUDE.md 画面デザイン）。見た目は css/landing-mark.css。
 */

import { subscribeReplay, getLogs } from '../services/replay-service.js';

const SHOW_MS = 700;   // css/landing-mark.css の solLandingMark と同じ長さ
const WAIT_MS = 1500;  // 着地を待つ上限（演出は長くても約 0.6 秒）
const PAD = 5;         // カードの外側へ出す幅

let _el = null;
let _gen = 0;          // 新しい手が来たら、待っている古い手の印を出さない
let _timer = 0;

function _hide() {
  clearTimeout(_timer);
  _el?.remove();
  _el = null;
}

function _show(card) {
  const r = card.getBoundingClientRect();
  if (!r.width || !r.height) return; // 隠れている場所（縦向きスマホのデッキ列など）
  _el = document.createElement('div');
  _el.className = 'sol-landing-mark';
  _el.setAttribute('aria-hidden', 'true');
  Object.assign(_el.style, {
    left: `${r.left - PAD}px`, top: `${r.top - PAD}px`,
    width: `${r.width + PAD * 2}px`, height: `${r.height + PAD * 2}px`,
  });
  document.body.append(_el);
  _timer = setTimeout(_hide, SHOW_MS);
}

/** 印を出す手なら動いたカードの本体（.tier-item-wrapper）を返す */
function _targetCard(ev) {
  if (!ev || !ev.cardId) return null;
  if (ev.actionType === 'moveCard') {
    if (ev.inPlace === true || ev.zoneId === 'poolRow' || ev.zoneId === 'poolRow2') return null;
  } else if (ev.actionType !== 'draw') {
    return null;
  }
  return document.getElementById(ev.cardId)?.closest('.tier-item-wrapper') ?? null;
}

function _onNavigated(cursor, stepped) {
  _hide();
  const gen = ++_gen;
  if (!stepped) return;
  const card = _targetCard(getLogs()[cursor]);
  if (!card) return;
  const start = performance.now();
  const wait = () => {
    if (gen !== _gen || !card.isConnected) return;
    if (card._flipClone && performance.now() - start < WAIT_MS) { requestAnimationFrame(wait); return; }
    _show(card);
  };
  requestAnimationFrame(wait);
}

export function initLandingMark() {
  subscribeReplay('navigated', _onNavigated);
  subscribeReplay('edited', () => { _gen++; _hide(); });
}
