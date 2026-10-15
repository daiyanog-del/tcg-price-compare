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
  isPlaying,
  setPlaySpeed,
  getPlaySpeed,
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
  registerCardName,
  buildReplayPayload,
  loadReplayFromCompressedData,
} from '../services/replay-service.js';
import { buildBoardFromLogs } from '../services/replay-board.js';
import { setPcPlaybackMode } from '../components/drag-drop.js';
import { initPcRecording, _getPcRecordingRange, updatePcShareRecordingSection, _resetPcRecording, _syncPcRecordingEndIdxAfterComment } from './pc-recording.js';
import { clearEverything } from './deck-input-panel.js';
import { showToast } from '../utils/toast.js';
import { addSharedReplay } from '../services/shared-replays.js';
import { savedReplaysGet, saveReplayEntry } from '../services/saved-replays.js';

const REPLAY_TITLE_MAX_LEN = 100;

/**
 * リプレイのタイトルを決定する。
 * 優先順位: (1) スマホ共有シートのタイトル入力欄（#solMobileShareTitleInput、値があれば）
 *          → (2) PC側の .title 見出し要素（存在する場合）
 *          → (3) 既定値 '共有リプレイ'
 * #solMobileShareTitleInput は PC には存在しないため、PC では従来どおり (2)→(3) の挙動になる。
 * @returns {string}
 */
function _getReplayTitle() {
  const mobileInput = document.getElementById('solMobileShareTitleInput');
  const mobileTitle = mobileInput?.value?.trim();
  if (mobileTitle) return mobileTitle.slice(0, REPLAY_TITLE_MAX_LEN);

  const pageTitle = document.querySelector('.title')?.textContent;
  if (pageTitle) return pageTitle;

  return '共有リプレイ';
}

/**
 * リプレイ一覧への移動前の確認。今の記録（setup込み）があるときだけ confirm を出し、
 * キャンセルなら移動を止める。Ctrl/Cmd/Shift+クリック等（新しいタブ・ウィンドウ）は今のページが残るので確認しない。
 * @param {MouseEvent} ev
 */
function _confirmLeaveForReplayList(ev) {
  if (ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.altKey || ev.button !== 0) return;
  if (getLogLength() + getSetupLogs().length === 0) return;
  if (!confirm('今の記録は、移動すると消えます（端末に保存・共有したものは一覧から開けます）。リプレイ一覧へ移動しますか？')) {
    ev.preventDefault();
  }
}

/**
 * 現在の再生速度倍率に応じて速度ボタンの選択中スタイル（active クラス）を更新する
 */
function _updateSpeedButtons() {
  const current = getPlaySpeed();
  document.querySelectorAll('#replaySpeedGroup .replay-speed-btn')
    .forEach((btn) => {
      const speed = parseFloat(btn.dataset.speed);
      btn.classList.toggle('active', speed === current);
    });
}

/**
 * リプレイバーを初期化する
 * DOM要素 #replayBarContainer が存在することが前提
 */
export function initReplayUI() {
  initReplay();

  // 「リプレイ一覧」へのリンク（PCリプレイバー・スマホ⋯メニュー・スマホ共有シート）は、
  // 記録が残っているときだけ移動前に確認する（<a> のまま。新しいタブで開く操作は確認なし）
  document.querySelectorAll('[data-replay-list-link]').forEach((a) => {
    a.addEventListener('click', _confirmLeaveForReplayList);
  });

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

  // クリア: 右パネル「盤面操作」のクリアボタン。モバイル版⋯メニューの
  // 「クリア」（deck-input-panel.js clearEverything）と同じ処理を使い、盤面も含めて
  // 完全に初期状態へ戻す。誤操作防止のため確認ダイアログを必須にする。
  document.getElementById('clearButton')
    ?.addEventListener('click', async () => {
      if (!confirm('読み込んだデッキ・盤面・記録をすべて消去します。元に戻せません。よろしいですか？')) return;
      // clearEverything() はデッキ読込中だと何もせず false を返す。その場合に
      // _resetPcRecording() だけ実行すると「盤面は残るが録画範囲だけ消える」
      // 中途半端な状態になるため、成功時のみ後続処理を行う。
      if (await clearEverything()) {
        // clearEverything() は PC版の録画状態（pc-recording.js）を知らないため、ここで明示的にリセットする
        _resetPcRecording();
      } else {
        showToast('デッキ読込中のためクリアできませんでした');
      }
    });

  // 再生速度切替（0.5x / 1x / 2x）
  document.querySelectorAll('#replaySpeedGroup .replay-speed-btn')
    .forEach((btn) => {
      btn.addEventListener('click', () => {
        const speed = parseFloat(btn.dataset.speed);
        setPlaySpeed(speed);
        _updateSpeedButtons();
      });
    });
  _updateSpeedButtons();

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
      if (!text) return; // 空入力なら盤面をシークする副作用も起こさない

      // 再生中に停止しただけではカーソルが再生途中の位置に残ったままで、直後の logEvent() が
      // 「カーソルより先の未来ログを切り捨てる」処理（replay-service.js）を発火させてしまい、
      // 記録の後半が消える。再生中は先に停止し、カーソルが末尾でなければ末尾までシークしてから
      // コメントを追加する（＝コメントは常に記録の末尾に追記される）。
      if (isPlaying()) togglePlay();
      if (getCursor() < getLogLength() - 1) seekTo(getLogLength() - 1);
      logEvent({ actionType: 'comment', text });
      input.value = '';
      _syncPcRecordingEndIdxAfterComment(); // 中2対応: 停止済み録画があれば endIdx を追従させる
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
        _resetPcRecording(); // 重大4対応: JSON読込で古い録画範囲(_pcRecording)が残留するのを防ぐ
        alert('リプレイを読み込みました');
      } catch (err) {
        alert('読み込み失敗: ' + err.message);
      }
      e.target.value = ''; // リセット
    });

  // リプレイ共有（URL or Supabase）
  document.getElementById('replayShare')
    ?.addEventListener('click', handleShare);

  // 端末に保存（PC共有シート。スマホの共有シートと同じ saveReplayToDevice を使う）
  document.getElementById('pcShareSaveLocalBtn')
    ?.addEventListener('click', handleSaveToDevice);

  // X に投稿
  document.getElementById('replayShareX')
    ?.addEventListener('click', handleShareToX);

  // URLハッシュからリプレイを読み込む
  _tryLoadFromURL();

  // PC版共有シート（#pcShareSheet）の開閉。中身のボタン(#replayShare等)は
  // 上で既にイベント配線済みなので、ここでは表示/非表示のトグルのみ行う。
  const pcShareSheet = _initPcShareSheet();

  // PC版 録画→再生モードUI: 状態機械は pc-recording.js に分離。録画停止時に
  // 共有シートを自動で開くコールバックだけをここから渡す。
  initPcRecording({
    openShareSheet: () => pcShareSheet?.open(),
    closeShareSheet: () => pcShareSheet?.close(),
  });

  // 重大4対応: デッキ再読込で古い録画範囲(_pcRecording)が残留すると、別セッションの
  // 手数がたまたま範囲を満たしていた場合に無言で誤った共有リンクを生成してしまう。
  // deck-input-panel.js が dispatch する sol-deck-load-start（読込開始時点）で破棄する。
  document.addEventListener('sol-deck-load-start', () => _resetPcRecording());

  // 「リセット」(event-handlers.js handleReset) は記録も初期化するため、録画状態も破棄する。
  // 共有シートが開いたままでも録画セクション表示が追従するよう updatePcShareRecordingSection も呼ぶ。
  // （依存方向: event-handlers → イベント通知 → replay-ui → pc-recording。import の循環は作らない）
  document.addEventListener('sol-replay-reset', () => {
    _resetPcRecording();
    updatePcShareRecordingSection();
  });

  // PC版 再生中の操作無効化: #replayPlay の再生状態（play-active クラス。
  // replay-service.js の _updateUI が togglePlay/stepForward/stepBack/seekTo/undo・
  // 自動再生タイマーの各経路から更新する）を監視し、drag-drop.js の
  // setPcPlaybackMode／盤面の pointer-events を連動させる。
  _initPcPlaybackModeWatcher();
}

/**
 * PC版共有シートの開閉トグルを初期化する。
 * モバイル版の共有シート(#solMobileShareSheet, mobile-ui.js)とは別要素・別IDのため
 * 干渉しない。
 * @returns {{open: () => void, close: () => void}|null}
 */
function _initPcShareSheet() {
  const sheet = document.getElementById('pcShareSheet');
  if (!sheet) return null;

  const open = () => {
    updatePcShareRecordingSection(); // ①録画セクションの表示を開く直前に最新化する
    sheet.hidden = false;
  };
  const close = () => { sheet.hidden = true; };

  document.getElementById('pcShareSheetOpen')?.addEventListener('click', open);
  document.getElementById('pcShareSheetClose')?.addEventListener('click', close);
  document.getElementById('pcShareSheetCloseBtn')?.addEventListener('click', close);

  // Escapeキーで閉じる（feedback-modal.js等、既存の他モーダルと同じ作法に合わせる）
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !sheet.hidden) close();
  });

  return { open, close };
}

/**
 * PC専用: 再生中に pointer-events:none を当てる対象コンテナ。
 * #imagePool（デッキ欄。EXデッキの #imagePool2 とは別）はモバイル版の
 * body.sol-playback には含まれないが、PC版は常時表示されるため独自に追加が必要
 * （#imagePool 内カードの dblclick=効果発動 が再生中に window.replayLog() を呼ぶと
 * 　logEvent() の「巻き戻し後は未来ログを切り捨てる」処理でリプレイ記録が壊れるため）。
 * #solRightPanel（1ドロー/リセット/盤面ロード/カウンター/トークン等）も同じ理由で
 * 必須。右パネルはモバイル版に存在しないため、モバイル版の body.sol-playback を参考にした
 * 棚卸しでは構造上見落とされていた。
 */
const PC_PLAYBACK_LOCK_SELECTOR = '.custom-layout, .sol-hand-area, .side-slots-container, #imagePool, #imagePool2, #solRightPanel';

/**
 * #replayPlay の class="play-active" の有無を監視し、PC版の再生中フラグを連動させる。
 * ドラッグ無効化そのものは drag-drop.js の setPcPlaybackMode（dragstart の preventDefault）が
 * 担うが、クリック等での誤操作も防ぐため盤面に pointer-events:none も重ねて掛ける
 * （.sol-playback-pc は PC専用クラス。mobile.css の body.sol-playback とは別物）。
 */
function _initPcPlaybackModeWatcher() {
  const btnPlay = document.getElementById('replayPlay');
  if (!btnPlay) return;

  const sync = () => {
    const playing = btnPlay.classList.contains('play-active');
    setPcPlaybackMode(playing);
    document.querySelectorAll(PC_PLAYBACK_LOCK_SELECTOR).forEach((el) => {
      el.classList.toggle('sol-playback-pc', playing);
    });
  };

  new MutationObserver(sync).observe(btnPlay, { attributes: true, attributeFilter: ['class'] });
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
  const logs = logsOverride ?? getLogs();
  // 保険: 名前が辞書に無いカードは DOM の dataset.cardName で補う（復元・ロード等の経路で漏れても共有に名前を載せる）
  document.querySelectorAll('.tier-item[id]').forEach(el => {
    if (!getNames()[el.id] && el.dataset.cardName) registerCardName(el.id, el.dataset.cardName);
  });
  const images = getImages();
  const names = getNames();
  const exCardIds = getExCardIds();
  // 共有する logs の最後の時点の盤面（リンク画像用）。求められなくても共有自体は続ける。
  // PC・スマホの共有・X投稿はすべてこの関数を通る。
  let board = null;
  try {
    board = buildBoardFromLogs(logs, { images, names, exCardIds });
  } catch (e) {
    console.warn('盤面の算出に失敗（board なしで共有）:', e);
  }
  const res = await fetch('/api/solitaire/replay', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title, images, names, exCardIds, logs, board }),
  });
  if (!res.ok) {
    // サーバーが返すJSONのerrorメッセージ（レート制限・型不正等の理由）を優先して使う。
    // JSONとして読めない場合のみ、フォールバックでHTTPステータスを出す。
    const errData = await res.json().catch(() => ({}));
    throw new Error(errData.error || `HTTP ${res.status}`);
  }
  const { id } = await res.json();
  // 共有履歴（リプレイ一覧ページ用）に控える。記録に失敗しても共有自体は成功させる。
  try {
    const steps = logs.filter(ev => !ev.setup).length; // setup（初期配置）を除いた手数
    addSharedReplay({ id, title, steps });
  } catch (e) {
    console.warn('共有履歴の記録に失敗（共有は成功）:', e);
  }
  return `${location.origin}/solitaire?replay=${id}`;
}

/**
 * High-1: 既存の setup（初期配置。共有URLを開いて再共有した場合など）を先頭に必ず残す。
 * _buildRangeLogs は setup を含まない通常ログ（getLogs()）のみを対象に加工するため、
 * setup 自体はここで別途結合する（再共有で setup が消える不具合の修正）。
 * タスクA-4: 端末保存（mobile-ui.js の buildReplayPayload 呼び出し）でも同じ範囲加工を
 * 使うため export する（_buildRangeLogs 自体は変更せず、ロジックを複製しない）。
 * @param {{start:number, end:number}} [range]  D-2: 共有する範囲（1-indexed 手数）。省略時は全範囲
 * @returns {Array}
 */
export function buildRangeLogsForPayload(range) {
  return [...getSetupLogs(), ..._buildRangeLogs(getLogs(), range)];
}

// ══════════════════ 端末保存（スマホ・PC共通） ══════════════════

/** タイトル欄が空のときの既定タイトル（YYYY-MM-DD HH:MM） */
function _formatDefaultReplayTitle() {
  const d = new Date();
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/**
 * 現在の記録（setup込みの手数・メイン/EX枚数）を返す。
 * 共有シート冒頭の「現在の記録」表示（スマホ）と、端末保存の両方で使う。
 * @returns {{total:number, main:number, ex:number}}
 */
export function getReplayCounts() {
  const total = getLogLength() + getSetupLogs().length; // Low-13: setupのみの盤面も1手以上として扱う
  const exCount = new Set(getExCardIds()).size;
  const mainCount = Math.max(0, Object.keys(getImages()).length - exCount);
  return { total, main: mainCount, ex: exCount };
}

/**
 * 現在のリプレイを「このブラウザ」の保存一覧（saved-replays.js）へ追加する。
 * スマホの共有シート（mobile-ui.js）とPCの共有シートの両方から使う共通処理。
 * 範囲が渡されればその範囲のログだけを、無ければ全体を exportReplay と同じ payload で保存する。
 * 通知（トースト／alert）は呼び出し側で行う。
 * @param {{title?:string, range?:{start:number,end:number}|null}} [opts]
 * @returns {{ok:boolean, removedCount:number, reason?:'empty'|'no_lzstring'|'quota'|'too_large'}}
 */
export function saveReplayToDevice({ title, range } = {}) {
  const { total, main, ex } = getReplayCounts();
  if (total === 0) return { ok: false, removedCount: 0, reason: 'empty' };
  if (typeof LZString === 'undefined') return { ok: false, removedCount: 0, reason: 'no_lzstring' };

  const finalTitle = (title || '').trim().slice(0, REPLAY_TITLE_MAX_LEN) || _formatDefaultReplayTitle();
  const logsOverride = range ? buildRangeLogsForPayload(range) : undefined;
  const steps = range ? (range.end - range.start + 1) : getLogLength(); // 共有履歴と同じく setup を除いた手数

  const payload = buildReplayPayload(finalTitle, logsOverride);
  const compressed = LZString.compress(JSON.stringify(payload));
  return saveReplayEntry({ title: finalTitle, main, ex, steps, data: compressed });
}

/**
 * saveReplayToDevice の結果を、利用者向けの通知文にする（スマホ・PC共通）。
 * @param {{ok:boolean, removedCount:number, reason?:string}} result
 * @returns {string}
 */
export function describeSaveResult(result) {
  if (!result.ok) {
    if (result.reason === 'empty') return '記録がありません';
    if (result.reason === 'too_large') return 'このリプレイは大きすぎて保存できません';
    if (result.reason === 'quota') return '保存できませんでした（端末の空き容量）';
    return '保存に失敗しました';
  }
  return result.removedCount > 0
    ? `端末に保存しました（上限のため古い${result.removedCount}件を削除）。「リプレイ一覧」から開けます`
    : '端末に保存しました。「リプレイ一覧」から開けます';
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
  const logs = buildRangeLogsForPayload(range);

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
    // PC版: 停止済みの録画があればその範囲だけを共有する（無ければ range=null で全範囲）
    const url = await generateShareURL(_getPcRecordingRange());
    await _copyToClipboard(url);
    alert('共有リンクをコピーしました');
  } catch (e) {
    alert('共有リンクの作成に失敗しました: ' + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/**
 * PC共有シートの「端末に保存」。停止済みの録画があればその範囲だけを、無ければ全体を保存する。
 * タイトルは入力欄が無いため日時の既定タイトルになる（一覧で見分けられるようにするため）。
 */
function handleSaveToDevice() {
  const result = saveReplayToDevice({ range: _getPcRecordingRange() });
  showToast(describeSaveResult(result));
}

/**
 * X投稿用の短いURL生成（常にSupabase ID形式）
 * ハッシュ形式だと投稿準備画面に数千文字のURLが露出するため
 * 中1対応: 「共有リンクをコピー」と同じく、PC版の停止済み録画があればその範囲だけを使う
 * （buildRangeLogsForPayload が setup を含めて組み立てる。range 省略時は全範囲）。
 * @param {{start:number, end:number}} [range]
 */
async function _generateShortURL(range) {
  const title = _getReplayTitle();
  const logs = buildRangeLogsForPayload(range);
  return await _saveAndGetShortURL(title, logs);
}

/**
 * H-1: X投稿用のtweet intent URLを組み立てる（文面の組み立てを共通化。mobile-ui.js の
 * 共有シートが本物の <a href> を組み立てる際にもこれを使う）。
 * @param {string} shareUrl  共有URL（generateShareURL() の戻り値等）
 * @returns {string} tweet intent URL
 */
export function buildShareTweetUrl(shareUrl) {
  const text = 'リプレイを共有しました #TCGYM';
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
    // 中1対応: 「共有リンクをコピー」と同じくPC版の停止済み録画範囲を反映する
    const url = await _generateShortURL(_getPcRecordingRange());
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
      _resetPcRecording(); // 重大4対応: JSON読込で古い録画範囲(_pcRecording)が残留するのを防ぐ
      // タスクB-1: 共有リンクから開いたときは mobile-ui.js が再生モードを自動で開く
      document.dispatchEvent(new CustomEvent('sol-replay-loaded-from-url'));
    }
    return;
  }

  const params = new URLSearchParams(location.search);

  // 端末保存（?saved=<端末保存のid>）: リプレイ一覧ページから開く
  const savedId = params.get('saved');
  if (savedId) {
    await _loadSavedFromURL(savedId);
    return;
  }

  // Supabase ID 方式
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
    _resetPcRecording(); // 重大4対応: JSON読込で古い録画範囲(_pcRecording)が残留するのを防ぐ
    // タスクB-1: 共有リンクから開いたときは mobile-ui.js が再生モードを自動で開く
    document.dispatchEvent(new CustomEvent('sol-replay-loaded-from-url'));
  } catch (e) {
    console.warn('リプレイの読み込みに失敗:', e.message);
  }
}

/**
 * ?saved=<id> で端末保存のリプレイを開く（リプレイ一覧ページからの遷移）。
 * 読込後の扱いは ?replay= と同じ（録画状態のリセット＋ sol-replay-loaded-from-url の発行。
 * スマホでは mobile-ui.js がこれを受けて再生モードを開く）。
 * 再読込で盤面が上書きされるのを防ぐため、読込の成否に関わらず URL から saved を消す。
 * @param {string} savedId
 */
async function _loadSavedFromURL(savedId) {
  const item = savedReplaysGet().find(r => r.id === savedId);
  // URL から saved を外す（再読込で同じリプレイが盤面を上書きし続けるのを防ぐ）
  const url = new URL(location.href);
  url.searchParams.delete('saved');
  history.replaceState(null, '', url.pathname + url.search + url.hash);

  if (!item) {
    showToast('保存済みリプレイが見つかりません（別のブラウザで保存したものは開けません）');
    return;
  }
  try {
    await loadReplayFromCompressedData(item.data);
    _resetPcRecording(); // JSON読込と同様、古い録画範囲の残留を防ぐ
    document.dispatchEvent(new CustomEvent('sol-replay-loaded-from-url'));
  } catch (e) {
    console.warn('保存済みリプレイの読み込みに失敗:', e);
    showToast('保存済みリプレイを開けませんでした');
  }
}
