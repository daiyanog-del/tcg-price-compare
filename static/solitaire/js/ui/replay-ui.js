/**
 * replay-ui.js
 * リプレイバーのUI初期化・共有機能
 */

import {
  initReplay,
  logEvent,
  stepBack,
  stepForward,
  seekTo,
  undoLast,
  togglePlay,
  exportReplay,
  importReplay,
  exportAsURLHash,
  getLogLength,
  getCursor,
} from '../services/replay-service.js';

/**
 * リプレイバーを初期化する
 * DOM要素 #replayBarContainer が存在することが前提
 */
export function initReplayUI() {
  initReplay();

  // window.replayLog を replay-service の logEvent にバインド
  window.replayLog = (event) => logEvent(event);

  // リプレイバーのボタンイベント
  document.getElementById('replayBack')
    ?.addEventListener('click', () => stepBack());

  document.getElementById('replayFwd')
    ?.addEventListener('click', () => stepForward());

  document.getElementById('replayPlay')
    ?.addEventListener('click', () => togglePlay());

  document.getElementById('replayUndo')
    ?.addEventListener('click', () => undoLast());

  document.getElementById('replaySlider')
    ?.addEventListener('input', (e) => {
      const n = parseInt(e.target.value, 10);
      seekTo(n);
    });

  // コメント挿入
  document.getElementById('replayAddComment')
    ?.addEventListener('click', () => {
      const input = document.getElementById('replayCommentInput');
      const text = input?.value?.trim();
      if (text) {
        logEvent({ actionType: 'comment', text });
        if (input) input.value = '';
      }
    });

  // リプレイエクスポート（ファイル保存）
  document.getElementById('replayExport')
    ?.addEventListener('click', () => {
      if (getLogLength() === 0) { alert('記録がありません'); return; }
      const title = document.querySelector('.title')?.textContent || '一人回し';
      exportReplay(title);
    });

  // リプレイインポート（ファイル読込）
  document.getElementById('replayImportFile')
    ?.addEventListener('change', async (e) => {
      const file = e.target.files?.[0];
      if (!file) return;
      try {
        await importReplay(file);
        alert('リプレイを読み込みました');
      } catch (err) {
        alert('読み込み失敗: ' + err.message);
      }
      e.target.value = ''; // リセット
    });

  // リプレイ共有（URL or Supabase）
  document.getElementById('replayShare')
    ?.addEventListener('click', handleShare);

  // X に投稿
  document.getElementById('replayShareX')
    ?.addEventListener('click', handleShareToX);

  // URLハッシュからリプレイを読み込む
  _tryLoadFromURL();
}

/**
 * 共有URL生成（ハッシュ方式 or Supabase ID 方式）
 * @returns {Promise<string>} 共有URL
 */
async function _generateShareURL() {
  const title = document.querySelector('.title')?.textContent || '一人回し';

  const hash = exportAsURLHash();
  if (hash && hash.length < 8000) {
    return `${location.origin}/solitaire#replay=${hash}`;
  }

  const res = await fetch('/api/solitaire/replay', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const { id } = await res.json();
  return `${location.origin}/solitaire?replay=${id}`;
}

/**
 * 共有リンクをコピー
 */
async function handleShare() {
  if (getLogLength() === 0) { alert('記録がありません'); return; }
  const btn = document.getElementById('replayShare');
  if (btn) btn.disabled = true;
  try {
    const url = await _generateShareURL();
    await _copyToClipboard(url);
    alert('共有リンクをコピーしました');
  } catch (e) {
    alert('共有リンクの作成に失敗しました: ' + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/**
 * X（Twitter）への投稿
 */
async function handleShareToX() {
  if (getLogLength() === 0) { alert('記録がありません'); return; }
  const btn = document.getElementById('replayShareX');
  if (btn) btn.disabled = true;
  try {
    const url = await _generateShareURL();
    const text = '一人回しのリプレイを共有しました #カード相場';
    const xUrl = `https://twitter.com/intent/tweet?text=${encodeURIComponent(text)}&url=${encodeURIComponent(url)}`;
    window.open(xUrl, '_blank', 'noopener,noreferrer');
  } catch (e) {
    alert('X投稿の準備に失敗しました: ' + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function _copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // fallback
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
}

/**
 * ページロード時にURLからリプレイを読み込む
 */
async function _tryLoadFromURL() {
  // URLハッシュ方式
  const hash = location.hash;
  if (hash.startsWith('#replay=')) {
    const { importFromURLHash } = await import('../services/replay-service.js');
    const encoded = hash.slice('#replay='.length);
    if (importFromURLHash(encoded)) {
      console.log('URLハッシュからリプレイを読み込みました');
    }
    return;
  }

  // Supabase ID 方式
  const params = new URLSearchParams(location.search);
  const replayId = params.get('replay');
  if (!replayId) return;

  try {
    const res = await fetch(`/api/solitaire/replay/${replayId}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const { images, names, logs } = await res.json();

    const { _setReplayData } = await import('../services/replay-service.js');
    if (typeof _setReplayData === 'function') {
      _setReplayData(images, names || {}, logs);
    }
    console.log(`リプレイID ${replayId} を読み込みました`);
  } catch (e) {
    console.warn('リプレイの読み込みに失敗:', e.message);
  }
}
