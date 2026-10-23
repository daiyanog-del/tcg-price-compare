/**
 * replay-rename-ui.js — シミュレーターで、開いているリプレイの名前を変える（2026-10-22 ユーザー承認: 案A＋B）
 *
 *   - 案A（PC）: 再生バーの「一覧」の右に、開いているリプレイの名前＋鉛筆（#pcReplayRenameBtn）。押すと名前の小窓
 *   - 案B（PC・スマホ）: 開いた直後の案内（replay-start-prompt.js）の題名の下の「名前を変える」。openReplayRename を呼ぶ
 *
 * 出すのは、このブラウザが持ち主と分かるもの（端末に保存したもの・自分が公開中のもの。services/replay-rename.js）を
 * URL から開いたときだけ（sol-replay-loaded-from-url）。リセット・すべて消去・別のデッキを読み込むと隠す
 * （盤面がもうそのリプレイではないため）。開いたあとに手を足しても、変えるのは開いたリプレイの名前。
 * 名前を変えたら sol-replay-renamed（detail.title）を出す（画面録画用の表示の題名などが受ける）。
 */

import { findRenamableReplay, renameReplay, renameMaxLength } from '../services/replay-rename.js';
import { openNameDialog, showActionToast } from './inline-ui.js';

// 開いているリプレイ（名前を変えられるもの）。無ければ null
let _current = null;

// 再生バーは PC 幅で1段に収める作り（solitaire-brand.css「1段に収める」）。名前のボタンを足して2段に折り返すときは、
// 1段に収まる幅まで名前を縮める（…で省略。全文はボタンの title）。LABEL_MIN_PX（約5文字）も入らなければ鉛筆だけにする。
// 名前が無くても2段になる狭い幅では、名前で段が増えない範囲で出す。サイドバーの開閉でもバーの幅が変わるので、幅を測って決める
const LABEL_MAX_PX = 160;   // CSS の .pc-replay-rename-label の max-width と同じ
const LABEL_MIN_PX = 64;
const LABEL_STEP_PX = 16;

function _barRows(bar) {
  return new Set([...bar.querySelectorAll(':scope > .replay-group')].map(g => g.offsetTop)).size;
}

function _fitLabel() {
  const btn = document.getElementById('pcReplayRenameBtn');
  const label = document.getElementById('pcReplayRenameLabel');
  const bar = document.getElementById('replayBarContainer');
  if (!btn || !label || !bar || btn.hidden || !btn.offsetParent) return;
  label.style.maxWidth = '';
  label.hidden = true;
  const rowsWithout = _barRows(bar);   // 名前が無いときの段の数（狭い幅ではもとから2段以上）
  label.hidden = false;
  if (_barRows(bar) <= rowsWithout) return;
  for (let w = LABEL_MAX_PX - LABEL_STEP_PX; w >= LABEL_MIN_PX; w -= LABEL_STEP_PX) {
    label.style.maxWidth = w + 'px';
    if (_barRows(bar) <= rowsWithout) return;
  }
  label.hidden = true;
}

function _renderPcButton() {
  const btn = document.getElementById('pcReplayRenameBtn');
  const label = document.getElementById('pcReplayRenameLabel');
  if (!btn || !label) return;
  btn.hidden = !_current;
  if (!_current) return;
  const title = _current.title || '無題';
  label.textContent = title;
  btn.title = `名前を変える（${title}）`;
  btn.setAttribute('aria-label', `リプレイの名前を変える（今の名前: ${title}）`);
  _fitLabel();
}

function _forget() {
  if (!_current) return;
  _current = null;
  _renderPcButton();
}

/**
 * URL から開いたリプレイの知らせ（detail）から、名前を変えられる相手を返す（無ければ null）
 * @param {{savedReplayId?:string|null, replayId?:string|null}} detail
 */
export function renamableFromLoadDetail(detail) {
  return findRenamableReplay({ savedReplayId: detail?.savedReplayId, replayId: detail?.replayId });
}

/**
 * 名前の小窓を開いて名前を変える。
 * @param {Element|null} anchor
 * @param {{kind:'saved'|'public', id:string, title:string, token?:string}} target
 * @param {(title:string)=>void} [onDone] 変えた後（案内の題名を書き換えるなど）
 */
export function openReplayRename(anchor, target, onDone) {
  if (!target) return;
  openNameDialog(anchor, {
    title: target.kind === 'public' ? 'リプレイの名前（みんなのリプレイにも出ます）' : 'リプレイの名前',
    okLabel: '変える',
    initial: target.title || '',
    maxLength: renameMaxLength(target),
    onOk: async (name) => {
      const r = await renameReplay(target, name);
      if (!r.ok) {
        if (!r.gone) return r.message;
        // 公開中でなくなっていた: もう変えられないので、ボタンを隠して知らせる（小窓は閉じる）
        if (_current && _current.id === target.id) _forget();
        showActionToast(r.message, { error: true });
        return undefined;
      }
      target.title = r.title;
      if (_current && _current.id === target.id) {
        _current.title = r.title;
        _renderPcButton();
      }
      document.dispatchEvent(new CustomEvent('sol-replay-renamed', { detail: { title: r.title } }));
      if (onDone) onDone(r.title);
      showActionToast(`名前を「${r.title}」に変えました`);
      return undefined;
    },
  });
}

export function initReplayRenameUI() {
  document.addEventListener('sol-replay-loaded-from-url', (e) => {
    _current = renamableFromLoadDetail(e.detail);
    _renderPcButton();
  });
  document.addEventListener('sol-replay-reset', _forget);
  document.addEventListener('sol-board-cleared', _forget);
  document.addEventListener('sol-deck-load-start', (e) => { if (e.detail?.replace !== false) _forget(); });
  const btn = document.getElementById('pcReplayRenameBtn');
  if (btn) btn.addEventListener('click', () => { if (_current) openReplayRename(btn, _current); });
  // バーの幅が変わったら名前の幅を決め直す（高さの変化＝自分で段を変えたときは見ない）
  const bar = document.getElementById('replayBarContainer');
  if (bar && typeof ResizeObserver === 'function') {
    let lastW = 0;
    new ResizeObserver((entries) => {
      const w = Math.round(entries[0].contentRect.width);
      if (w === lastW) return;
      lastW = w;
      if (_current) _fitLabel();
    }).observe(bar);
  }
}
