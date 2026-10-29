/**
 * replay-caption.js
 * リプレイの字幕: 再生の操作（次へ・前へ・スライダー・自動再生）で手を見たとき、画面の下に
 * 「カードの絵・カード名・元の場所 → 行き先（効果発動などの札）」を出す（2026-10-29 ユーザー選択の案A
 * のグラデーション版。モック _mocks/replay-caption-20261029）。録画用の表示・普段のリプレイ画面の両方で出す。
 *
 * - 中身は services/replay-caption-model.js が記録だけから組み立てる（巻き戻し・スライダーでも同じ結果）。
 * - 手を打った・1手取り消した（edited）・先頭に戻った・字幕にしない手（コメントなど）では消す。
 * - 置き場所は下の操作の帯の真上: 縦向きスマホは再生の帯（無ければ下部バー）、PC はリプレイバー、
 *   録画用の表示は画面の下端。左右は PC のリプレイバーの幅に合わせる。
 * - 押しても反応しない（pointer-events:none。後ろのカードの操作をじゃましない）。見た目は css/replay-caption.css。
 */

import {
  subscribeReplay, getLogs, getSetupLogs, getImages, getNames, getExCardIds, getCursor,
} from '../services/replay-service.js';
import { describeStep } from '../services/replay-caption-model.js';
import { isMobilePortrait } from '../utils/viewport.js';

let _el = null;

function _build() {
  const el = document.createElement('div');
  el.id = 'solReplayCaption';
  el.className = 'sol-replay-caption';
  el.setAttribute('aria-live', 'polite');
  el.hidden = true;
  document.body.append(el);
  return el;
}

/** 画像として出せる src か（未発売カードの card:// やプロキシは出さない） */
function _thumbSrc(src) {
  return typeof src === 'string' && /^(https?:|data:)/.test(src) ? src : '';
}

function _span(cls, text) {
  const s = document.createElement('span');
  s.className = cls;
  s.textContent = text;
  return s;
}

function _fill(info) {
  _el.replaceChildren();
  const src = _thumbSrc(getImages()[info.cardId]);
  if (src) {
    const img = document.createElement('img');
    img.className = 'sol-replay-caption-thumb';
    img.src = src;
    img.alt = '';
    _el.append(img);
  }
  const body = document.createElement('div');
  body.className = 'sol-replay-caption-body';
  const move = document.createElement('div');
  move.className = 'sol-replay-caption-move';
  if (info.tag) move.append(_span('sol-replay-caption-tag', info.tag));
  if (info.from && info.to) {
    move.append(_span('sol-replay-caption-from', info.from), _span('sol-replay-caption-arrow', '→'), _span('sol-replay-caption-to', info.to));
  } else if (info.to || info.from) {
    move.append(_span('sol-replay-caption-to', info.to || info.from));
  }
  body.append(_span('sol-replay-caption-name', info.name), move);
  _el.append(body);
}

/** 下の操作の帯の上端（縦の位置）と、左右の範囲を決める */
function _place() {
  if (!_el || _el.hidden) return;
  let bottomTop = window.innerHeight;
  let left = 0;
  let width = window.innerWidth;
  const visible = (e) => e && e.getBoundingClientRect().height > 0;
  if (document.body.classList.contains('sol-recview')) {
    // 録画用の表示: 画面の下端（下端の進み具合の線の上に重ねる）
  } else if (isMobilePortrait()) {
    const strip = document.getElementById('solMobileReplayStrip');
    const bar = document.getElementById('solMobileBar');
    const top = [strip, bar].filter(visible).map((e) => e.getBoundingClientRect().top);
    if (top.length) bottomTop = Math.min(...top);
  } else {
    const bar = document.getElementById('replayBarContainer');
    if (visible(bar)) {
      const r = bar.getBoundingClientRect();
      bottomTop = r.top;
      left = r.left;
      width = r.width;
    }
  }
  _el.style.bottom = `${Math.max(0, window.innerHeight - bottomTop)}px`;
  _el.style.left = `${left}px`;
  _el.style.width = `${width}px`;
}

function _hide() {
  if (_el) _el.hidden = true;
}

function _show(cursor) {
  const info = describeStep(getSetupLogs(), getLogs(), cursor, {
    images: getImages(), names: getNames(), exCardIds: getExCardIds(),
  });
  if (!info) { _hide(); return; }
  _fill(info);
  _el.hidden = false;
  _place();
  // 手が変わるたびに少しだけ浮かび上がらせる（同じ場所で文字だけ変わると、変わったことに気づきにくい）
  _el.classList.remove('is-new');
  void _el.offsetWidth;
  _el.classList.add('is-new');
}

export function initReplayCaption() {
  _el = _build();
  subscribeReplay('navigated', (cursor) => _show(cursor));
  subscribeReplay('edited', _hide);
  // 記録が空になった（読み込み直し・すべて消去）・先頭より前に戻ったら消す
  subscribeReplay('changed', () => { if (getCursor() < 0 || getLogs().length === 0) _hide(); });
  window.addEventListener('resize', () => requestAnimationFrame(_place));
}
