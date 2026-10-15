/**
 * replays-page.js
 * リプレイ一覧ページ（/solitaire/replays）の描画。
 *
 * このブラウザ（localStorage）にあるリプレイだけを扱う。
 *   1. 端末に保存したリプレイ（services/saved-replays.js）
 *   2. 共有リンクを作ったリプレイの履歴（services/shared-replays.js）
 * シミュレーター本体（main.js 等）は読み込まない。
 * ユーザー由来の文字列（タイトル等）は必ず textContent で入れる（innerHTML には入れない）。
 */

import { savedReplaysGet, deleteReplayEntry } from './services/saved-replays.js';
import { sharedReplaysGet, deleteSharedReplay } from './services/shared-replays.js';

/** 日時を「10/15 13:05」形式にする（不明なら空文字） */
function formatDateTime(ms) {
  if (!ms) return '';
  return new Date(ms).toLocaleString('ja-JP', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  });
}

/** 要素を作る小ヘルパー（テキストは textContent） */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** 行のボタンを作る */
function actionButton(label, className, onClick) {
  const btn = el('button', 'rp-btn' + (className ? ' ' + className : ''), label);
  btn.type = 'button';
  btn.addEventListener('click', onClick);
  return btn;
}

/** クリップボードへコピーする（失敗時は execCommand にフォールバック）。成功なら true */
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { ok = false; }
    ta.remove();
    return ok;
  }
}

// ── 1. 端末に保存したリプレイ ──

function renderSaved() {
  const listEl = document.getElementById('rpSavedList');
  const emptyEl = document.getElementById('rpSavedEmpty');
  if (!listEl) return;
  listEl.textContent = '';

  // 新しい順
  const items = savedReplaysGet().slice().sort((a, b) => (b.savedAt || 0) - (a.savedAt || 0));
  if (emptyEl) emptyEl.hidden = items.length > 0;

  for (const item of items) {
    const li = el('li', 'rp-row');

    // 行を押すとシミュレーターで開く（本物のリンクにして、新しいタブで開く等も使えるようにする）
    const link = el('a', 'rp-row-link');
    link.href = '/solitaire?saved=' + encodeURIComponent(item.id);
    const info = el('span', 'rp-row-info');
    info.appendChild(el('span', 'rp-row-title', item.title || '無題'));
    const date = formatDateTime(item.savedAt);
    info.appendChild(el('span', 'rp-row-meta',
      `${date ? date + '・' : ''}${item.steps ?? 0}手・メイン${item.main ?? 0}枚・EX${item.ex ?? 0}枚`));
    link.appendChild(info);
    li.appendChild(link);

    const actions = el('div', 'rp-row-actions');
    actions.appendChild(actionButton('削除', 'rp-btn-danger', () => {
      if (!confirm(`「${item.title || '無題'}」を削除します。元に戻せません。よろしいですか？`)) return;
      deleteReplayEntry(item.id);
      renderSaved();
    }));
    li.appendChild(actions);
    listEl.appendChild(li);
  }
}

// ── 2. 共有リンクを作ったリプレイ ──

function renderShared() {
  const listEl = document.getElementById('rpSharedList');
  const emptyEl = document.getElementById('rpSharedEmpty');
  if (!listEl) return;
  listEl.textContent = '';

  const items = sharedReplaysGet(); // 新しい順に保存されている
  if (emptyEl) emptyEl.hidden = items.length > 0;

  for (const item of items) {
    const li = el('li', 'rp-row');
    const idParam = encodeURIComponent(item.id);

    const link = el('a', 'rp-row-link');
    link.href = '/solitaire?replay=' + idParam;

    const thumb = document.createElement('img');
    thumb.className = 'rp-thumb';
    thumb.src = '/solitaire/og/' + idParam + '.png';
    thumb.alt = '';
    thumb.loading = 'lazy';
    thumb.width = 120;
    thumb.height = 63;
    // 画像が取れないときは、壊れた画像アイコンを出さずに隠す
    thumb.addEventListener('error', () => { thumb.hidden = true; });
    link.appendChild(thumb);

    const info = el('span', 'rp-row-info');
    info.appendChild(el('span', 'rp-row-title', item.title || '無題'));
    const date = formatDateTime(item.createdAt);
    info.appendChild(el('span', 'rp-row-meta', `${date ? date + '・' : ''}${item.steps ?? 0}手`));
    link.appendChild(info);
    li.appendChild(link);

    const actions = el('div', 'rp-row-actions');
    const copyBtn = actionButton('リンクをコピー', '', async () => {
      const ok = await copyText(location.origin + '/solitaire?replay=' + idParam);
      copyBtn.textContent = ok ? 'コピーしました' : 'コピーできませんでした';
      setTimeout(() => { copyBtn.textContent = 'リンクをコピー'; }, 2000);
    });
    actions.appendChild(copyBtn);
    actions.appendChild(actionButton('履歴から消す', 'rp-btn-danger', () => {
      if (!confirm(`「${item.title || '無題'}」をこの一覧から消します。共有リンクは作成から90日で自動的に消えます。よろしいですか？`)) return;
      deleteSharedReplay(item.id);
      renderShared();
    }));
    li.appendChild(actions);
    listEl.appendChild(li);
  }
}

renderSaved();
renderShared();
