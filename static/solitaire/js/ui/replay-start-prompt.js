/**
 * replay-start-prompt.js
 * 共有リンク・保存したリプレイを開いた直後に、盤面の中央へ「▶ 再生する」の案内を出す
 * （2026-10-16 ユーザー要望: 共有リンクから来た人に再生ボタンが分かるようにしたい）。
 *
 * - replay-ui.js の _finishLoadFromURL が出す sol-replay-loaded-from-url を受けて出す。
 * - 「最後の盤面から続ける」（detail.atEnd）や、手が1つも無い記録では出さない。
 * - 「▶ 再生する」は、スマホの再生モード中なら再生バーの再生ボタンを、それ以外は
 *   リプレイバーの #replayPlay を押したのと同じにする（再生の仕組みは既存のものを使う）。
 * - 文言・タイトルは textContent で入れる（共有リンクのタイトルは他人が書いたもの）。
 */

import { getLogLength } from '../services/replay-service.js';
import { enterRecView } from './rec-view.js';

const PROMPT_ID = 'solReplayStartPrompt';

function _close() {
  document.getElementById(PROMPT_ID)?.remove();
  document.removeEventListener('keydown', _onKeydown, true);
}

function _onKeydown(ev) {
  if (ev.key !== 'Escape') return;
  ev.stopPropagation(); // 他のシートまで閉じないように
  _close();
}

/** 再生を始める。スマホの再生モード中は、そのバーの再生ボタンを押す */
function _startPlayback() {
  const mobileToggle = document.getElementById('solMobilePlayToggleBtn');
  const inMobilePlayback = document.body.classList.contains('sol-playback') && mobileToggle && mobileToggle.offsetParent !== null;
  if (inMobilePlayback) {
    mobileToggle.click();
    return;
  }
  const playBtn = document.getElementById('replayPlay');
  if (playBtn && !playBtn.disabled) playBtn.click();
}

/**
 * 案内を出す。
 * @param {{source?:'shared'|'saved', title?:string}} info
 */
export function showReplayStartPrompt(info = {}) {
  _close();
  const steps = getLogLength();
  if (steps <= 0) return;

  const root = document.createElement('div');
  root.id = PROMPT_ID;
  root.className = 'sol-replay-start';

  const backdrop = document.createElement('div');
  backdrop.className = 'sol-replay-start-backdrop';
  backdrop.addEventListener('click', _close);

  const card = document.createElement('div');
  card.className = 'sol-replay-start-card';
  card.setAttribute('role', 'dialog');
  card.setAttribute('aria-modal', 'true');
  card.setAttribute('aria-labelledby', 'solReplayStartTitle');

  const label = document.createElement('p');
  label.className = 'sol-replay-start-label';
  label.textContent = info.source === 'saved' ? '保存したリプレイ' : '共有されたリプレイ';

  const title = document.createElement('p');
  title.id = 'solReplayStartTitle';
  title.className = 'sol-replay-start-title';
  title.textContent = (info.title || '').trim() || 'リプレイ';

  const meta = document.createElement('p');
  meta.className = 'sol-replay-start-meta';
  meta.textContent = `${steps}手`;

  const play = document.createElement('button');
  play.type = 'button';
  play.className = 'sol-replay-start-play';
  play.textContent = '再生する';
  play.addEventListener('click', () => { _close(); _startPlayback(); });

  const later = document.createElement('button');
  later.type = 'button';
  later.className = 'sol-replay-start-later';
  later.textContent = '盤面だけ見る';
  later.addEventListener('click', _close);

  // 画面録画用の表示（rec-view.js）。スマホの再生バーには置き場所が無いので、共有リンクを開いた人の入口をここにも置く
  const rec = document.createElement('button');
  rec.type = 'button';
  rec.className = 'sol-replay-start-later';
  rec.textContent = '画面録画用に再生';
  rec.addEventListener('click', () => { _close(); enterRecView(); });

  card.append(label, title, meta, play, later, rec);
  root.append(backdrop, card);
  document.body.appendChild(root);
  document.addEventListener('keydown', _onKeydown, true);
  // キーボードで操作する人のためにフォーカスは移すが、マウス・タップで開いたときは枠を出さない
  play.focus({ focusVisible: false });
}

export function initReplayStartPrompt() {
  // 画面録画用の表示（rec-view.js）を開いたら案内を閉じる（Esc の受け口も外す）
  document.addEventListener('sol-recview-open', _close);
  document.addEventListener('sol-replay-loaded-from-url', (e) => {
    if (e.detail?.atEnd) return; // 続きから遊ぶときは出さない
    showReplayStartPrompt({ source: e.detail?.source, title: e.detail?.title });
  });
}
