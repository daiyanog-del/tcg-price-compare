/**
 * replay-caption.js
 * リプレイの字幕: 利用者の再生の操作（次へ・前へ・自動再生・スライダー・ホイール）で手を見たとき、
 * 「カードの絵・カード名・元の場所 → 行き先（効果発動などの札）」を出す（2026-10-29 ユーザー選択。モック _mocks/replay-caption-20261029）。
 *
 * 出す場所（2026-10-30 ユーザー指摘「出たり消えたりがちらつく」「左寄せの文字がカードとかぶる」を受けて分けた。
 * モック _mocks/replay-caption-pc-20261030）:
 *   - PC のリプレイ画面: カード詳細パネル（.sol-cip-col）そのものに動いたカードを出し、上に「元の場所 → 行き先」の行を添える。
 *     パネルを隠している（◀ で畳んだ）ときだけ、下の帯で出す
 *   - PC の録画用の表示: 盤面の左の空き（ユーザー選択の R1）。空きが狭い画面は上の帯（ロゴ・題名・手数）の真ん中
 *   - スマホ（縦向き・横向き）: 下の操作の帯の真上に出す帯（縦向きスマホは再生の帯・範囲を選ぶ帯の上、録画用の表示は画面の下端）
 *
 * - 中身は services/replay-caption-model.js が記録だけから組み立てる（巻き戻し・スライダーでも同じ結果）。
 * - 出す合図は replay-service の navigated だけ（読み込み・続きから・再生モードの終了など、画面の都合で手を動かしたときは出ない）。
 * - 字幕にしない手（コメント・カットイン・全戻しなど）では消さずに、その前でカードを動かした一番近い手の字幕を出す
 *   （消して次の手でまた出すと、自動再生で出たり消えたりしてちらついた）。切り替えで点滅させる演出もしない。
 * - 手を打った・1手取り消した（edited）・先頭、再生モード・録画用の表示・範囲選択への出入り（body のクラスが変わる）では消す。
 *   範囲再生の前の手（setReplayCaptionFloor）も出さない。
 * - 帯は押しても反応しない（pointer-events:none。後ろのカードの操作をじゃましない）。見た目は css/replay-caption.css。
 */

import {
  subscribeReplay, getLogs, getSetupLogs, getImages, getNames, getExCardIds, getCursor,
} from '../services/replay-service.js';
import { describeStep } from '../services/replay-caption-model.js';
import { isMobilePortrait } from '../utils/viewport.js';
import { showCardForElement } from './card-info-panel.js';

let _el = null;       // 帯（スマホ・録画用の表示・パネルを隠した PC）
let _cipMove = null;  // カード詳細パネルの上の「元の場所 → 行き先」の行（PC のリプレイ画面）
let _cipCardId = '';  // パネルに出しているカード（同じカードの手が続くときは取り直さない）
let _floor = -1;      // これより前の手（カーソル）では出さない（範囲再生の開始の直前＝範囲外）

// 出入りで字幕を消す body のクラス（再生モード・録画用の表示・範囲選択のスマホ／PC）
const MODE_CLASSES = ['sol-playback', 'sol-recview', 'sol-range-pick', 'sol-pc-range-pick'];
// 字幕の下に来る操作の帯（見えているものの上端のうち一番上に合わせる）
const BOTTOM_CHROME_PORTRAIT = ['solMobileReplayStrip', 'solMobileBar', 'solMobileRangePanel'];
// 字幕にしない手のとき、さかのぼってカードを動かした手を探す数（長いコメントの連続でも重くならないように）
const LOOKBACK = 30;
// 録画用の表示（PC）で、盤面の左にこの幅（px）があれば左に出す。狭ければ上の帯の真ん中
const SIDE_MIN_W = 150;
const SIDE_MARGIN = 24;

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

/** 「効果発動 元の場所 → 行き先」の行 */
function _moveLine(info) {
  const move = document.createElement('div');
  move.className = 'sol-replay-caption-move';
  if (info.tag) move.append(_span('sol-replay-caption-tag', info.tag));
  if (info.from && info.to) {
    move.append(_span('sol-replay-caption-from', info.from), _span('sol-replay-caption-arrow', '→'), _span('sol-replay-caption-to', info.to));
  } else if (info.to || info.from) {
    move.append(_span('sol-replay-caption-to', info.to || info.from));
  }
  return move;
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
  body.append(_span('sol-replay-caption-name', info.name), _moveLine(info));
  _el.append(body);
}

const _visible = (e) => !!e && e.getBoundingClientRect().height > 0;

/** PC のリプレイ画面でカード詳細パネルが見えているか（畳んでいる・縦向き・録画用の表示では使わない） */
function _cipUsable() {
  if (isMobilePortrait() || document.body.classList.contains('sol-recview')) return false;
  const col = document.querySelector('.sol-cip-col');
  return !!col && !col.classList.contains('hidden') && col.getBoundingClientRect().width > 0;
}

/** 録画用の表示（PC の並び。横向きスマホは除く）の盤面の左の空き（px）。使えなければ 0 */
function _recSideWidth() {
  const b = document.body.classList;
  if (!b.contains('sol-recview') || b.contains('sol-recview-landscape') || isMobilePortrait()) return 0;
  const board = document.querySelector('.custom-layout')?.getBoundingClientRect(); // 盤面のマスの左端（相手の段も同じ列）
  if (!board) return 0;
  return Math.max(0, Math.floor(board.left - SIDE_MARGIN * 2));
}

/** 帯の置き場所（位置・左右の範囲）を決める */
function _place() {
  if (!_el || _el.hidden) return;
  _el.classList.remove('is-side', 'is-top');
  document.body.classList.remove('sol-rec-caption-top');
  _el.style.top = '';
  _el.style.removeProperty('--sol-caption-side-w');
  if (document.body.classList.contains('sol-recview')) {
    const side = _recSideWidth();
    if (side >= SIDE_MIN_W) {
      // 録画用の表示（PC）: 盤面の左の空きに、カードを大きく（盤面の上端にそろえる）
      const board = document.querySelector('.custom-layout')?.getBoundingClientRect();
      _el.classList.add('is-side');
      _el.style.setProperty('--sol-caption-side-w', `${side}px`);
      _el.style.left = `${SIDE_MARGIN}px`;
      _el.style.width = `${side}px`;
      _el.style.top = `${Math.round(board ? board.top : 0)}px`;
      _el.style.bottom = 'auto';
      return;
    }
    if (side > 0) {
      // 空きが狭い（PC の並び）: 上の帯の真ん中に1行（題名はロゴの横へ寄せる。rec-view.css）
      _el.classList.add('is-top');
      document.body.classList.add('sol-rec-caption-top');
      _el.style.left = '';
      _el.style.width = '';
      _el.style.bottom = 'auto';
      return;
    }
  }
  let bottomTop = window.innerHeight;
  let left = 0;
  let width = window.innerWidth;
  if (document.body.classList.contains('sol-recview')) {
    // 録画用の表示（スマホ）: 画面の下端（下端の進み具合の線の上に重ねる）
  } else if (isMobilePortrait()) {
    const tops = BOTTOM_CHROME_PORTRAIT.map((id) => document.getElementById(id)).filter(_visible)
      .map((e) => e.getBoundingClientRect().top);
    if (tops.length) bottomTop = Math.min(...tops);
  } else {
    const bar = document.getElementById('replayBarContainer');
    if (_visible(bar)) {
      const r = bar.getBoundingClientRect();
      bottomTop = r.top;
      left = r.left;
      width = r.width;
    }
    // PC の範囲を選ぶ窓（pc-recording.js）はリプレイバーの上に浮くので、その上に出す
    const pick = document.getElementById('solPcRangePanel');
    if (_visible(pick)) bottomTop = Math.min(bottomTop, pick.getBoundingClientRect().top);
  }
  _el.style.bottom = `${Math.max(0, window.innerHeight - bottomTop)}px`;
  _el.style.left = `${left}px`;
  _el.style.width = `${width}px`;
}

function _hideCipMove() {
  if (_cipMove) _cipMove.hidden = true;
}

function _hide() {
  if (_el) _el.hidden = true;
  document.body.classList.remove('sol-rec-caption-top');
  _hideCipMove();
}

/** カード詳細パネルに出す（PC のリプレイ画面） */
function _showInCip(info) {
  if (_el) _el.hidden = true;
  document.body.classList.remove('sol-rec-caption-top');
  if (!_cipMove) {
    _cipMove = document.createElement('div');
    _cipMove.className = 'sol-cip-replay-move';
    _cipMove.setAttribute('aria-live', 'polite');
    document.querySelector('.sol-cip-col')?.prepend(_cipMove);
  }
  _cipMove.replaceChildren(_moveLine(info));
  _cipMove.hidden = false;
  if (info.cardId !== _cipCardId) {
    const cardEl = document.getElementById(info.cardId);
    if (cardEl) {
      _cipCardId = info.cardId;
      showCardForElement(cardEl);
    }
  }
}

/** cursor の手の字幕。字幕にしない手なら、その前でカードを動かした一番近い手の字幕 */
function _infoAt(cursor) {
  const dict = { images: getImages(), names: getNames(), exCardIds: getExCardIds() };
  const setup = getSetupLogs();
  const logs = getLogs();
  const stop = Math.max(_floor, 0, cursor - LOOKBACK);
  for (let i = cursor; i >= stop; i--) {
    const info = describeStep(setup, logs, i, dict);
    if (info) return info;
  }
  return null;
}

function _show(cursor) {
  const info = cursor < _floor ? null : _infoAt(cursor);
  if (!info) { _hide(); return; }
  if (_cipUsable()) { _showInCip(info); return; }
  _hideCipMove();
  _fill(info);
  _el.hidden = false;
  _place();
}

/**
 * 範囲再生の間、範囲より前の手（カーソル）では字幕を出さない（受け手は範囲の最初の手から見るので、送る人の確認でも合わせる）。
 * @param {number|null} cursor  これより小さいカーソルでは出さない。null で解除
 */
export function setReplayCaptionFloor(cursor) {
  _floor = Number.isInteger(cursor) ? cursor : -1;
  if (getCursor() < _floor) _hide();
}

export function initReplayCaption() {
  _el = _build();
  subscribeReplay('navigated', (cursor) => _show(cursor));
  subscribeReplay('edited', _hide);
  // 記録が空になった（読み込み直し・すべて消去）・先頭より前に戻ったら消す
  subscribeReplay('changed', () => { if (getCursor() < 0 || getLogs().length === 0) _hide(); });
  // 利用者が別のカードを押して詳細を見たら、パネルの上の再生の行は下げる（押したカードの行ではないため）
  document.addEventListener('sol-cip-card-clicked', () => { _hideCipMove(); _cipCardId = ''; });

  // 再生モード・録画用の表示・範囲選択に出入りしたら消す（出入りの途中で手が動いても、古い字幕を残さない）
  let modes = MODE_CLASSES.filter((c) => document.body.classList.contains(c)).join(' ');
  new MutationObserver(() => {
    const now = MODE_CLASSES.filter((c) => document.body.classList.contains(c)).join(' ');
    if (now !== modes) { modes = now; _hide(); }
  }).observe(document.body, { attributes: true, attributeFilter: ['class'] });

  // 位置の測り直し: 画面の大きさ・左右のパネルの開閉（main.js sol-layout-resize）・下の帯の大きさの変化
  const replace = () => requestAnimationFrame(_place);
  window.addEventListener('resize', replace);
  window.addEventListener('sol-layout-resize', replace);
  window.addEventListener('scroll', replace, { passive: true }); // 横向きスマホのリプレイバーは sticky
  if (typeof ResizeObserver === 'function') {
    const ro = new ResizeObserver(replace);
    ['replayBarContainer', ...BOTTOM_CHROME_PORTRAIT].forEach((id) => {
      const e = document.getElementById(id);
      if (e) ro.observe(e);
    });
  }
}
