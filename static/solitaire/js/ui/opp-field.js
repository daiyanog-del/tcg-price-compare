/**
 * opp-field.js
 * 相手の場（2026-10-29 ユーザー指示・モック _mocks/opp-field-20261029）の出し入れと、相手の墓地の枚数・中身の一覧。
 * マスは templates/solitaire.html .sol-opp-row（15〜19＝相手のモンスターゾーン・20＝フィールドゾーン・21＝墓地）、
 * 並び・見た目は css/opp-field.css。カードの移動・記録・保存・再生は自分の盤面のマス（.custom-slot）と同じ仕組みを使う。
 *
 * 出す条件（body に sol-opp-on）: 次のどれか
 *   - 利用者がスイッチ（PC の右パネル・スマホの⋯メニュー）で出した
 *   - 相手のマスにカードがある（しまうとカードの行き場が無くなるので、この間はしまえない）
 *   - 開いている記録が相手のマスを使っている（共有リンクで来た人にも最初から段が見えるように）。
 *     利用者がスイッチでしまったら、記録が空になるまでこの条件では出さない
 * 縦向きスマホは普段5列（15・19 を出さない）。15・19 にカードがある・記録が 15・19 を使っているときだけ 7列（sol-opp-wide）。
 */

import { subscribeReplay, getLogs, getSetupLogs } from '../services/replay-service.js';
import { showToast } from '../utils/toast.js';

const ON_CLASS = 'sol-opp-on';
const WIDE_CLASS = 'sol-opp-wide';
const OPP_ZONE_RE = /^custom-slot-(1[5-9]|2[01])$/;
const EDGE_ZONE_RE = /^custom-slot-(15|19)$/;
const GRAVE_SLOT = '21';
const GRAVE_STEP = 0.62; // 中身を広げたとき、カードを横にずらす幅（マス幅に対する割合）

let _row = null;
let _grave = null;
let _badge = null;
let _manual = false;  // 利用者がスイッチで出した
let _userOff = false; // 利用者がスイッチでしまった（記録が相手のマスを使っていても出さない）

const _cardsIn = (sel) => !!_row?.querySelector(`${sel} > .tier-item-wrapper`);
const _hasOppCards = () => _cardsIn('.sol-opp-slot');
const _hasEdgeCards = () => _cardsIn('.sol-opp-slot[data-slot="15"]') || _cardsIn('.sol-opp-slot[data-slot="19"]');

/** 記録（初期配置を含む）が、相手のマス（edge=true なら 15・19）へ動かす手を含むか */
function _logsUse(re) {
  return [...(getSetupLogs() || []), ...(getLogs() || [])]
    .some((e) => e && e.actionType === 'moveCard' && re.test(e.zoneId || ''));
}

function _syncSwitches(on) {
  document.querySelectorAll('input[data-opp-field-toggle]').forEach((cb) => { cb.checked = on; });
}

/** 出す・広げる条件を見直して body のクラスに反映する。変わったら盤面の大きさを測り直す */
function _update() {
  if (!_row) return;
  const logsOpp = !_userOff && _logsUse(OPP_ZONE_RE);
  const on = _manual || _hasOppCards() || logsOpp;
  const wide = on && (_hasEdgeCards() || (logsOpp && _logsUse(EDGE_ZONE_RE)));
  const body = document.body.classList;
  const changed = body.contains(ON_CLASS) !== on || body.contains(WIDE_CLASS) !== wide;
  body.toggle(ON_CLASS, on);
  body.toggle(WIDE_CLASS, wide);
  _syncSwitches(on);
  if (!on) _closeGrave();
  // main.js fitFieldToViewport が段の分を入れてすぐ測り直す（resize だと 150ms 待つ間に古い大きさで崩れ、
  // 出ているカットインも消える＝comment-ui.js。左右のパネルの開閉と同じ合図を使う）
  if (changed) window.dispatchEvent(new Event('sol-layout-resize'));
}

function _onSwitch(ev) {
  const cb = ev.currentTarget;
  if (!cb.checked && _hasOppCards()) {
    cb.checked = true;
    showToast('相手の場にカードがあるので、しまえません');
    return;
  }
  _manual = cb.checked;
  _userOff = !cb.checked;
  _update();
}

// ── 相手の墓地: 1マスに重ね、一番上のカードと「墓地の刻印＋枚数」の札。札を押すと中身を横に広げる ──

function _graveCards() {
  return _grave ? [..._grave.querySelectorAll(':scope > .tier-item-wrapper')] : [];
}

function _layoutGrave() {
  if (!_grave) return;
  const cards = _graveCards();
  _badge.hidden = cards.length === 0;
  const num = _badge.querySelector('span');
  if (num.textContent !== String(cards.length)) num.textContent = String(cards.length);
  _badge.setAttribute('aria-label', `相手の墓地 ${cards.length}枚${_grave.classList.contains('is-open') ? '（閉じる）' : '（中身を見る）'}`);
  if (cards.length === 0) _closeGrave();
  const open = _grave.classList.contains('is-open');
  const w = _grave.clientWidth || 0;
  // 枚数が多いと画面の外へはみ出して触れなくなるので、盤面の列（.sol-field-area）の右端までに収まるよう詰める
  let step = w * GRAVE_STEP;
  if (open && cards.length > 1) {
    const area = document.querySelector('.sol-field-area')?.getBoundingClientRect();
    const avail = area ? area.right - _grave.getBoundingClientRect().left - 8 : Infinity;
    step = Math.max(4, Math.min(step, (avail - w) / (cards.length - 1)));
  }
  cards.forEach((c, i) => {
    c.style.top = '0';
    c.style.left = open ? `${Math.round(i * step)}px` : '';
  });
  _grave.style.setProperty('--sol-opp-grave-span', `${Math.round((cards.length - 1) * step + w)}px`);
}

function _openGrave() {
  if (!_grave || _graveCards().length === 0) return;
  _grave.classList.add('is-open');
  _layoutGrave();
}

function _closeGrave() {
  if (!_grave || !_grave.classList.contains('is-open')) return;
  _grave.classList.remove('is-open');
  _layoutGrave();
}

function _initGrave() {
  _grave = _row.querySelector(`.sol-opp-slot[data-slot="${GRAVE_SLOT}"]`);
  if (!_grave) return;
  _badge = document.createElement('button');
  _badge.type = 'button';
  _badge.className = 'sol-opp-grave-badge';
  _badge.hidden = true;
  _badge.append(document.createElement('span'));
  _grave.append(_badge);
  // 札はマスの中にあるが、カードのドラッグ・マスへのドロップの的にはしない
  _badge.addEventListener('click', (e) => {
    e.stopPropagation();
    if (_grave.classList.contains('is-open')) _closeGrave(); else _openGrave();
  });
  ['mousedown', 'touchstart', 'dragstart'].forEach((t) => _badge.addEventListener(t, (e) => e.stopPropagation()));
  // 広げている間に、墓地の外を押したら閉じる（中のカードを押す・ドラッグするのは閉じない）
  document.addEventListener('pointerdown', (e) => {
    if (_grave.classList.contains('is-open') && !_grave.contains(e.target)) _closeGrave();
  }, true);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') _closeGrave(); });
}

export function initOppField() {
  _row = document.querySelector('.sol-opp-row');
  if (!_row) return;
  _initGrave();
  document.querySelectorAll('input[data-opp-field-toggle]').forEach((cb) => cb.addEventListener('change', _onSwitch));
  // カードの出入り（ドラッグ・メニュー・再生・保存した盤面の復元のどれでも）で、出す条件と墓地の枚数を見直す。
  // 見るのは各マスの直下（カードの出入り）だけ。段全体（subtree）を見ると、墓地の札の数字の書き換えも
  // 知らせになり、書き換え→知らせ→書き換えが止まらなくなる（2026-10-29 手元の確認で固まった）
  const mo = new MutationObserver(() => { _update(); _layoutGrave(); });
  _row.querySelectorAll('.sol-opp-slot').forEach((slot) => mo.observe(slot, { childList: true }));
  subscribeReplay('changed', () => {
    if ((getLogs() || []).length === 0 && (getSetupLogs() || []).length === 0) _userOff = false; // 記録が空になったら最初から
    _update();
  });
  window.addEventListener('resize', () => { if (_grave?.classList.contains('is-open')) _layoutGrave(); });
  _update();
  _layoutGrave();
}
