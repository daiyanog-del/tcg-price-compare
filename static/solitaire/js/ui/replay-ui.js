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

  // URLハッシュからリプレイを読み込む
  _tryLoadFromURL();
}

/**
 * 共有ボタン処理
 * テキスト/メタデッキ盤面: LZString → URLハッシュ
 * ニューロン盤面 (dataURL): Supabase ID 発行
 */
async function handleShare() {
  if (getLogLength() === 0) { alert('記録がありません'); return; }
  const title = document.querySelector('.title')?.textContent || '一人回し';
  const shareBtn = document.getElementById('replayShare');
  if (shareBtn) shareBtn.disabled = true;

  try {
    // まずURLハッシュを試みる（軽量：テキスト/メタ盤面）
    const hash = exportAsURLHash();
    if (hash && hash.length < 8000) {
      const url = `${location.origin}/solitaire#replay=${hash}`;
      await _copyToClipboard(url);
      alert('共有リンクをコピーしました');
      return;
    }

    // 重い場合はSupabaseに保存してID発行
    const res = await fetch('/api/solitaire/replay', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    });

    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const { id } = await res.json();
    const url = `${location.origin}/solitaire?replay=${id}`;
    await _copyToClipboard(url);
    alert(`共有リンクをコピーしました\n${url}`);
  } catch (e) {
    alert('共有リンクの作成に失敗しました: ' + e.message);
  } finally {
    if (shareBtn) shareBtn.disabled = false;
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
    const { images, logs } = await res.json();

    const { importFromURLHash } = await import('../services/replay-service.js');
    // payload形式に変換してLZStringを介さず直接インポート
    const { initReplay: _init, registerCardImage } = await import('../services/replay-service.js');
    _init();
    Object.entries(images).forEach(([id, src]) => registerCardImage(id, src));
    // logsをセット（内部セッターを使う）
    const { _setReplayData } = await import('../services/replay-service.js');
    if (typeof _setReplayData === 'function') {
      _setReplayData(images, logs);
    }
    console.log(`リプレイID ${replayId} を読み込みました`);
  } catch (e) {
    console.warn('リプレイの読み込みに失敗:', e.message);
  }
}
