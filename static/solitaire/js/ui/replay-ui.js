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
  getImages,
  getNames,
  getExCardIds,
  getLogs,
  getSetupLogs,
} from '../services/replay-service.js';

const REPLAY_TITLE_MAX_LEN = 100;

/**
 * リプレイのタイトルを決定する。
 * 優先順位: (1) スマホ共有シートのタイトル入力欄（#solMobileShareTitleInput、値があれば）
 *          → (2) PC側の .title 見出し要素（存在する場合）
 *          → (3) 既定値 '一人回し'
 * #solMobileShareTitleInput は PC には存在しないため、PC では従来どおり (2)→(3) の挙動になる。
 * @returns {string}
 */
function _getReplayTitle() {
  const mobileInput = document.getElementById('solMobileShareTitleInput');
  const mobileTitle = mobileInput?.value?.trim();
  if (mobileTitle) return mobileTitle.slice(0, REPLAY_TITLE_MAX_LEN);

  const pageTitle = document.querySelector('.title')?.textContent;
  if (pageTitle) return pageTitle;

  return '一人回し';
}

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
      // Low-13: setup（初期配置）だけの盤面も保存できるよう、setup込みの手数で判定する
      if (getLogLength() + getSetupLogs().length === 0) { alert('記録がありません'); return; }
      const title = _getReplayTitle();
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
 * D-2: 現在の全ログ（getLogs()。setup を含まない通常の手数）から、指定範囲だけを
 * 「そのまま」残し、それより前を setup:true 付きの初期配置ログへ変換する。
 * 元の配列・要素は変更しない（_safeCloneEvent 相当のシャローコピー）。
 * range 省略時・{start,end}が全範囲を指す場合はそのままの配列を返す（従来どおり）。
 * @param {Array} allLogs
 * @param {{start:number, end:number}|null|undefined} range  1-indexed 手数（開始・終了）
 * @returns {Array}
 */
function _buildRangeLogs(allLogs, range) {
  if (!range) return allLogs;
  const total = allLogs.length;
  const start = Math.max(1, Math.min(range.start || 1, total));
  const end = Math.max(start, Math.min(range.end || total, total));
  if (start <= 1 && end >= total) return allLogs; // 全範囲なら加工不要
  const setupPart = allLogs.slice(0, start - 1).map(ev => ({ ...ev, setup: true }));
  const restPart = allLogs.slice(start - 1, end);
  return [...setupPart, ...restPart];
}

/**
 * Supabaseにリプレイを保存し、短縮URL（?replay=ID）を返す共通処理
 * @param {string} title
 * @param {Array}  [logsOverride]  D-2: 範囲共有用に加工済みの logs を渡せる（省略時は全ログ）
 * @returns {Promise<string>} 短縮URL
 */
async function _saveAndGetShortURL(title, logsOverride) {
  const res = await fetch('/api/solitaire/replay', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      title,
      images: getImages(),
      names: getNames(),
      exCardIds: getExCardIds(),
      logs: logsOverride ?? getLogs(),
    }),
  });
  if (!res.ok) {
    // サーバーが返すJSONのerrorメッセージ（レート制限・型不正等の理由）を優先して使う。
    // JSONとして読めない場合のみ、フォールバックでHTTPステータスを出す。
    const errData = await res.json().catch(() => ({}));
    throw new Error(errData.error || `HTTP ${res.status}`);
  }
  const { id } = await res.json();
  return `${location.origin}/solitaire?replay=${id}`;
}

/**
 * H-1: 共有URL生成（常にSupabase ID方式で短いURLを発行）。保存失敗時のみハッシュ方式に
 * フォールバック。mobile-ui.js の共有シートからも同じロジックを使うため export する
 * （ロジックは移動せず export を足すだけ）。
 * @param {{start:number, end:number}} [range]  D-2: 共有する範囲（1-indexed 手数）。省略時は全範囲
 * @returns {Promise<string>} 共有URL
 */
export async function generateShareURL(range) {
  const title = _getReplayTitle();
  // High-1: 既存の setup（初期配置。共有URLを開いて再共有した場合など）を先頭に必ず残す。
  // _buildRangeLogs は setup を含まない通常ログ（getLogs()）のみを対象に加工するため、
  // setup 自体はここで別途結合する（再共有で setup が消える不具合の修正）。
  const logs = [...getSetupLogs(), ..._buildRangeLogs(getLogs(), range)];

  // 常にSupabase ID方式（?replay=8文字ID）で短いURLを発行する
  try {
    return await _saveAndGetShortURL(title, logs);
  } catch (e) {
    // Supabase未接続など保存失敗時のみハッシュ方式にフォールバック
    console.warn('Supabase保存失敗。ハッシュ方式にフォールバック:', e.message);
    const hash = exportAsURLHash(logs);
    if (hash) {
      return `${location.origin}/solitaire#replay=${hash}`;
    }
    throw new Error('共有URLの生成に失敗しました');
  }
}

/**
 * 共有リンクをコピー
 */
async function handleShare() {
  // Low-13: setup（初期配置）だけの盤面も共有できるよう、setup込みの手数で判定する
  if (getLogLength() + getSetupLogs().length === 0) { alert('記録がありません'); return; }
  const btn = document.getElementById('replayShare');
  if (btn) btn.disabled = true;
  try {
    const url = await generateShareURL();
    await _copyToClipboard(url);
    alert('共有リンクをコピーしました');
  } catch (e) {
    alert('共有リンクの作成に失敗しました: ' + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/**
 * X投稿用の短いURL生成（常にSupabase ID形式）
 * ハッシュ形式だと投稿準備画面に数千文字のURLが露出するため
 */
async function _generateShortURL() {
  const title = _getReplayTitle();
  // High-1: setup を含めて送る（X投稿でも setup が落ちないようにする）
  const logs = [...getSetupLogs(), ...getLogs()];
  return await _saveAndGetShortURL(title, logs);
}

/**
 * H-1: X投稿用のtweet intent URLを組み立てる（文面の組み立てを共通化。mobile-ui.js の
 * 共有シートが本物の <a href> を組み立てる際にもこれを使う）。
 * @param {string} shareUrl  共有URL（generateShareURL() の戻り値等）
 * @returns {string} tweet intent URL
 */
export function buildShareTweetUrl(shareUrl) {
  const text = '一人回しのリプレイを共有しました #TCGYM';
  return `https://twitter.com/intent/tweet?text=${encodeURIComponent(text)}&url=${encodeURIComponent(shareUrl)}`;
}

/**
 * X（Twitter）への投稿
 */
async function handleShareToX() {
  if (getLogLength() === 0) { alert('記録がありません'); return; }
  const btn = document.getElementById('replayShareX');
  if (btn) btn.disabled = true;
  try {
    const url = await _generateShortURL();
    window.open(buildShareTweetUrl(url), '_blank', 'noopener,noreferrer');
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
    // card:// センチネル対応のため await が必要（非同期関数）
    if (await importFromURLHash(encoded)) {
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
    const { images, names, logs, exCardIds } = await res.json();

    const { _setReplayData } = await import('../services/replay-service.js');
    if (typeof _setReplayData === 'function') {
      // card:// センチネル対応のため await が必要（非同期関数）
      await _setReplayData(images, names || {}, logs, exCardIds || []);
    }
    console.log(`リプレイID ${replayId} を読み込みました`);
  } catch (e) {
    console.warn('リプレイの読み込みに失敗:', e.message);
  }
}
