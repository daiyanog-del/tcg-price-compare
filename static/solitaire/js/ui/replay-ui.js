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
  setPlaySpeed,
  getPlaySpeed,
  exportReplay,
  exportAsURLHash,
  getLogLength,
  getImages,
  getNames,
  getExCardIds,
  getCardTypes,
  getLogs,
  getSetupLogs,
  registerCardName,
  buildReplayPayload,
  loadReplayFromCompressedData,
} from '../services/replay-service.js';
import { buildBoardFromLogs } from '../services/replay-board.js';
import { setPcPlaybackMode } from '../components/drag-drop.js';
import { initPcRecording, _getPcRecordingRange, updatePcShareRecordingSection, _resetPcRecording } from './pc-recording.js';
import { initCommentUI } from './comment-ui.js';
import { clearEverything } from './deck-input-panel.js';
import { initReplayStartPrompt } from './replay-start-prompt.js';
import { showToast } from '../utils/toast.js';
import { addSharedReplay } from '../services/shared-replays.js';
import { savedReplaysGet, saveReplayEntry } from '../services/saved-replays.js';
import { isReplayAtEnd } from '../utils/replay-url.js';
import { enterRecView, isRecViewActive } from './rec-view.js';

const REPLAY_TITLE_MAX_LEN = 100;
/** 公開（みんなのリプレイ）に使えるタイトルの長さ。サーバー（solitaire_routes.PUBLIC_TITLE_MAX_LEN）と同じ */
const PUBLIC_TITLE_MAX_LEN = 40;

/**
 * 利用者が入力したタイトル（保存・共有シートのタイトル欄 [data-replay-title]）を返す。
 * 欄は PC の保存・共有シートとスマホのシートにあり、入力は _initReplayTitleMirror で
 * 互いに写しているので、最初に見つかった値を使う。空なら ''。
 * @returns {string}
 */
function _getTypedReplayTitle() {
  for (const el of document.querySelectorAll('[data-replay-title]')) {
    const v = el.value?.trim();
    if (v) return v.slice(0, REPLAY_TITLE_MAX_LEN);
  }
  return '';
}

/**
 * 共有（リンク作成・X投稿）に使うタイトル。入力が無ければ従来どおり「共有リプレイ」。
 * 日時を既定にするのは「このブラウザに保存」とファイルへの書き出しだけ（一覧・ファイル名で見分けるため）。
 * @returns {string}
 */
function _getReplayTitle() {
  return _getTypedReplayTitle() || '共有リプレイ';
}

/**
 * 「みんなのリプレイに公開する」を選んだときのタイトルの検査。公開はタイトル必須（誰でも見られる一覧に載るため）。
 * 問題なければ ''、あればその場に出すエラー文を返す。禁止語の判定はサーバー側（保存時の 400）。
 * @returns {string}
 */
export function checkPublicTitle() {
  const t = _getTypedReplayTitle();
  if (!t) return '公開するにはタイトルを入れてください';
  if ([...t].length > PUBLIC_TITLE_MAX_LEN) return `公開するタイトルは${PUBLIC_TITLE_MAX_LEN}文字以内にしてください`;
  return '';
}

/** タイトル欄（[data-replay-title]）どうしで入力を写し合う（保存と共有で同じタイトルを使うため） */
function _initReplayTitleMirror() {
  const inputs = [...document.querySelectorAll('[data-replay-title]')];
  inputs.forEach((el) => {
    el.addEventListener('input', () => {
      inputs.forEach((other) => { if (other !== el) other.value = el.value; });
    });
  });
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
 * リプレイをファイルに書き出す（PC の #replayExport とスマホの書き出しボタン共通）。
 * 範囲が渡されればその範囲のログだけを書き出す（保存・共有と同じ buildRangeLogsForPayload）。
 * タイトルは入力があればそれ、無ければ日時。
 * @param {{start:number, end:number}|null} [range]
 */
export function exportReplayFile(range) {
  // Low-13: setup（初期配置）だけの盤面も保存できるよう、setup込みの手数で判定する
  if (getLogLength() + getSetupLogs().length === 0) { alert('記録がありません'); return; }
  const title = _getTypedReplayTitle() || _formatDefaultReplayTitle();
  exportReplay(title, range ? buildRangeLogsForPayload(range) : undefined);
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

  // コメント（吹き出し・盤面メモ）: 右パネル「コメント」区画の入力と一覧（comment-ui.js）
  initCommentUI();

  // リプレイエクスポート（ファイル保存）。範囲が決まっていればその範囲だけを書き出す
  document.getElementById('replayExport')
    ?.addEventListener('click', () => exportReplayFile(_getPcRecordingRange()));

  // リプレイ共有（URL or Supabase）
  document.getElementById('replayShare')
    ?.addEventListener('click', handleShare);

  // 端末に保存（PC共有シート。スマホの共有シートと同じ saveReplayToDevice を使う）
  document.getElementById('pcShareSaveLocalBtn')
    ?.addEventListener('click', handleSaveToDevice);

  // X に投稿
  document.getElementById('replayShareX')
    ?.addEventListener('click', handleShareToX);

  // 画面録画用の表示（rec-view.js）。押せるかどうかは再生ボタン（記録0件のとき disabled）に合わせる
  const recViewBtn = document.getElementById('replayRecView');
  const playBtn = document.getElementById('replayPlay');
  if (recViewBtn) {
    recViewBtn.addEventListener('click', () => enterRecView());
    if (playBtn) {
      const syncRecView = () => { recViewBtn.disabled = playBtn.disabled; };
      syncRecView();
      new MutationObserver(syncRecView).observe(playBtn, { attributes: true, attributeFilter: ['disabled'] });
    }
  }

  // 共有リンク・保存したリプレイを開いた直後の「▶ 再生する」の案内（読み込みより先に受け口を用意する）
  initReplayStartPrompt();

  // URLハッシュからリプレイを読み込む
  _tryLoadFromURL();

  _initReplayTitleMirror();

  // PC版の保存シート（#pcSaveSheet）・共有シート（#pcShareSheet）の開閉。中身のボタン
  // （#replayShare・#pcShareSaveLocalBtn 等）は上で配線済みなので、ここでは表示/非表示のみ。
  const pcShareSheet = _initPcSheet('pcShareSheet', 'pcShareSheetOpen', ['pcShareSheetClose', 'pcShareSheetCloseBtn']);
  const pcSaveSheet = _initPcSheet('pcSaveSheet', 'pcSaveSheetOpen', ['pcSaveSheetClose', 'pcSaveSheetCloseBtn']);

  // PC版の範囲選択: 状態機械は pc-recording.js。範囲が決まったら通知だけ出す
  // （保存か共有かは利用者が選ぶので、シートは自動で開かない）。
  initPcRecording({
    // range が null のときは、手が1つも入っていない範囲（0手のまま決めた等）で不成立になった通知
    onRangeDecided: (range) => showToast(range
      ? `範囲を決めました（${range.start}〜${range.end}手目）。保存・共有で使えます`
      : '範囲に手が1つも入っていません'),
    closeShareSheet: () => { pcShareSheet?.close(); pcSaveSheet?.close(); },
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
 * PC版の保存・共有シートの開閉トグルを初期化する。
 * モバイル版の共有シート(#solMobileShareSheet, mobile-ui.js)とは別要素・別IDのため
 * 干渉しない。
 * @param {string} sheetId
 * @param {string} openBtnId
 * @param {string[]} closeIds  背景・×ボタンなど、押すと閉じる要素の id
 * @returns {{open: () => void, close: () => void}|null}
 */
function _initPcSheet(sheetId, openBtnId, closeIds) {
  const sheet = document.getElementById(sheetId);
  if (!sheet) return null;

  const open = () => {
    updatePcShareRecordingSection(); // 範囲の表示を開く直前に最新化する
    // 開くたびに公開はオフに戻す（前回のチェックが残って、意図せず公開しないように）。共有URLの使い回しも止める
    if (sheetId === 'pcShareSheet') {
      const pub = document.getElementById('pcSharePublic');
      if (pub) pub.checked = false;
      _showPcPublicError('');
    }
    resetShareUrlMemo();
    sheet.hidden = false;
  };
  const close = () => { sheet.hidden = true; };

  document.getElementById(openBtnId)?.addEventListener('click', open);
  closeIds.forEach((id) => document.getElementById(id)?.addEventListener('click', close));

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
 * #solRightPanel（1ドロー/リセット/盤面ロード等）も同じ理由で
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

  // 画面録画用の表示（rec-view.js）の間も、再生中と同じく盤面を触れなくする（カウントダウン中・再生の後に
  // 盤面を動かすと、途中の手から先の記録が消えたり末尾に手が足されたりするため）
  const sync = () => {
    const playing = btnPlay.classList.contains('play-active') || isRecViewActive();
    setPcPlaybackMode(playing);
    document.querySelectorAll(PC_PLAYBACK_LOCK_SELECTOR).forEach((el) => {
      el.classList.toggle('sol-playback-pc', playing);
    });
  };

  new MutationObserver(sync).observe(btnPlay, { attributes: true, attributeFilter: ['class'] });
  new MutationObserver(sync).observe(document.body, { attributes: true, attributeFilter: ['class'] });
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
 * @param {boolean} [isPublic]  true なら「みんなのリプレイ」として公開する（サーバーが削除用の鍵を返す）
 * @returns {Promise<string>} 短縮URL
 */
async function _saveAndGetShortURL(title, logsOverride, isPublic = false) {
  const logs = logsOverride ?? getLogs();
  // 保険: 名前が辞書に無いカードは DOM の dataset.cardName で補う（復元・ロード等の経路で漏れても共有に名前を載せる）
  document.querySelectorAll('.tier-item[id]').forEach(el => {
    if (!getNames()[el.id] && el.dataset.cardName) registerCardName(el.id, el.dataset.cardName);
  });
  const images = getImages();
  const names = getNames();
  const exCardIds = getExCardIds();
  const cardTypes = getCardTypes();
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
    body: JSON.stringify({ title, images, names, exCardIds, cardTypes, logs, board, ...(isPublic ? { public: true } : {}) }),
  });
  if (!res.ok) {
    // サーバーが返すJSONのerrorメッセージ（レート制限・型不正等の理由）を優先して使う。
    // JSONとして読めない場合のみ、フォールバックでHTTPステータスを出す。
    const errData = await res.json().catch(() => ({}));
    throw new Error(errData.error || `HTTP ${res.status}`);
  }
  const { id, deleteToken } = await res.json();
  // 公開で保存したのに鍵が返らなければ、公開できたと言えない（通知を出さずエラーにする）
  if (isPublic && !deleteToken) throw new Error('公開を確認できませんでした');
  // 共有履歴（リプレイ一覧ページ用）に控える。記録に失敗しても共有自体は成功させる。
  // 公開したものは取り下げの鍵（deleteToken）も控える（このブラウザからだけ取り下げられる）
  try {
    const steps = logs.filter(ev => !ev.setup).length; // setup（初期配置）を除いた手数
    addSharedReplay({ id, title, steps, public: isPublic && !!deleteToken, deleteToken });
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
    ? `このブラウザに保存しました（上限のため古い${result.removedCount}件を削除）。「リプレイ一覧」から開けます`
    : 'このブラウザに保存しました。「リプレイ一覧」から開けます';
}

/**
 * H-1: 共有URL生成（常にSupabase ID方式で短いURLを発行）。保存失敗時のみハッシュ方式に
 * フォールバック。mobile-ui.js の共有シートからも同じロジックを使うため export する
 * （ロジックは移動せず export を足すだけ）。
 * @param {{start:number, end:number}} [range]  D-2: 共有する範囲（1-indexed 手数）。省略時は全範囲
 * @param {{public?:boolean}} [opts]  public:true なら「みんなのリプレイ」に公開する。タイトル必須で、
 *   保存に失敗してもハッシュ方式にはフォールバックしない（公開されないリンクを公開したつもりで渡さないため）
 * @returns {Promise<string>} 共有URL
 */
export async function generateShareURL(range, { public: isPublic = false } = {}) {
  const logs = buildRangeLogsForPayload(range);
  if (isPublic) {
    const err = checkPublicTitle();
    if (err) throw new Error(err);
    const title = _getTypedReplayTitle();
    return await _memoShareUrl(_shareMemoKey('pub', range, title, logs), () => _saveAndGetShortURL(title, logs, true));
  }
  const title = _getReplayTitle();
  return await _memoShareUrl(_shareMemoKey('link', range, title, logs), async () => {
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
  });
}

// ── 共有URLの使い回し ──
// 「リンクを作ってコピー」と「Xに投稿」（PC）、「共有リンクを作成」の連打（スマホ）は、共有シートを開いている間、
// 範囲・タイトル・公開の有無が同じなら最後に作ったURLを使い回す（保存のPOSTを1回にする。公開が重複して
// 2件できないように）。シートを開くたびに resetShareUrlMemo() で破棄する。
// 作成中の呼び出しも同じ Promise を返すので、先に押したほうの完了を待つ。失敗したものは覚えない。
let _shareMemo = null;   // {key, promise}

/** 共有シートを開くときに呼ぶ（前回の共有URLの使い回しを止める） */
export function resetShareUrlMemo() {
  _shareMemo = null;
}

function _shareMemoKey(kind, range, title, logs) {
  return JSON.stringify([kind, range ? [range.start, range.end] : null, title, logs.length]);
}

function _memoShareUrl(key, make) {
  if (_shareMemo && _shareMemo.key === key) return _shareMemo.promise;
  const promise = make();
  const entry = { key, promise };
  _shareMemo = entry;
  promise.catch(() => { if (_shareMemo === entry) _shareMemo = null; });
  return promise;
}

/**
 * 共有リンクをコピー
 */
async function handleShare() {
  // Low-13: setup（初期配置）だけの盤面も共有できるよう、setup込みの手数で判定する
  if (getLogLength() + getSetupLogs().length === 0) { alert('記録がありません'); return; }
  const isPublic = _isPcPublicChecked();
  if (!_checkPcPublicTitle(isPublic)) return;
  const btn = document.getElementById('replayShare');
  if (btn) btn.disabled = true;
  try {
    // PC版: 停止済みの録画があればその範囲だけを共有する（無ければ range=null で全範囲）
    const url = await generateShareURL(_getPcRecordingRange(), { public: isPublic });
    await _copyToClipboard(url);
    alert(isPublic ? '共有リンクをコピーしました。みんなのリプレイに公開しました' : '共有リンクをコピーしました');
  } catch (e) {
    // 公開の失敗（禁止語・タイトル不備など）は、チェックの下にその場で出す
    if (isPublic) _showPcPublicError(e.message);
    else alert('共有リンクの作成に失敗しました: ' + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/** PC共有シートの「みんなのリプレイに公開する」がオンか */
function _isPcPublicChecked() {
  return !!document.getElementById('pcSharePublic')?.checked;
}

/** チェックの下のエラー表示。空文字なら消す */
function _showPcPublicError(msg) {
  const el = document.getElementById('pcSharePublicError');
  if (!el) return;
  el.textContent = msg;
  el.hidden = !msg;
}

/**
 * 公開オンのときのタイトル検査（リンク作成・X投稿の前）。問題があればその場にエラーを出して false を返す。
 * オフのときは前回のエラーを消して true。
 */
function _checkPcPublicTitle(isPublic) {
  const err = isPublic ? checkPublicTitle() : '';
  _showPcPublicError(err);
  return !err;
}

/**
 * PC保存シートの「このブラウザに保存」。範囲が決まっていればその範囲だけを、無ければ全体を保存する。
 * タイトルは保存シートの入力欄。空なら日時が既定（一覧で見分けられるようにするため）。
 */
function handleSaveToDevice() {
  const result = saveReplayToDevice({ title: _getTypedReplayTitle(), range: _getPcRecordingRange() });
  showToast(describeSaveResult(result));
  if (result.ok) document.getElementById('pcSaveSheet')?.setAttribute('hidden', '');
}

/**
 * X投稿用の短いURL生成（常にSupabase ID形式）
 * ハッシュ形式だと投稿準備画面に数千文字のURLが露出するため
 * 中1対応: 「共有リンクをコピー」と同じく、PC版の停止済み録画があればその範囲だけを使う
 * （buildRangeLogsForPayload が setup を含めて組み立てる。range 省略時は全範囲）。
 * @param {{start:number, end:number}} [range]
 */
async function _generateShortURL(range, isPublic = false) {
  const logs = buildRangeLogsForPayload(range);
  if (isPublic) {
    // 「リンクを作ってコピー」と同じ公開の共有URLを使い回す（POSTは1回）
    const title = _getTypedReplayTitle();
    return await _memoShareUrl(_shareMemoKey('pub', range, title, logs), () => _saveAndGetShortURL(title, logs, true));
  }
  const title = _getReplayTitle();
  return await _memoShareUrl(_shareMemoKey('x', range, title, logs), () => _saveAndGetShortURL(title, logs));
}

/**
 * H-1: X投稿用のtweet intent URLを組み立てる（文面の組み立てを共通化。mobile-ui.js の
 * 共有シートが本物の <a href> を組み立てる際にもこれを使う）。
 * @param {string} shareUrl  共有URL（generateShareURL() の戻り値等）
 * @returns {string} tweet intent URL
 */
export function buildShareTweetUrl(shareUrl) {
  const text = 'リプレイを共有しました #強欲なラボ';
  return `https://twitter.com/intent/tweet?text=${encodeURIComponent(text)}&url=${encodeURIComponent(shareUrl)}`;
}

/**
 * X（Twitter）への投稿
 */
async function handleShareToX() {
  if (getLogLength() === 0) { alert('記録がありません'); return; }
  const isPublic = _isPcPublicChecked();
  if (!_checkPcPublicTitle(isPublic)) return;
  const btn = document.getElementById('replayShareX');
  if (btn) btn.disabled = true;
  try {
    // 中1対応: 「共有リンクをコピー」と同じくPC版の停止済み録画範囲を反映する
    const url = await _generateShortURL(_getPcRecordingRange(), isPublic);
    window.open(buildShareTweetUrl(url), '_blank', 'noopener,noreferrer');
    if (isPublic) showToast('みんなのリプレイに公開しました');
  } catch (e) {
    if (isPublic) _showPcPublicError(e.message);
    else alert('X投稿の準備に失敗しました: ' + e.message);
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
      _finishLoadFromURL(false, { source: 'shared' });
    }
    return;
  }

  const params = new URLSearchParams(location.search);
  // ?at=end（最後の盤面から続ける）。URL を書き換える前に読み取る
  const atEnd = isReplayAtEnd();

  // 端末保存（?saved=<端末保存のid>）: リプレイ一覧ページから開く
  const savedId = params.get('saved');
  if (savedId) {
    await _loadSavedFromURL(savedId, atEnd);
    return;
  }

  // Supabase ID 方式
  const replayId = params.get('replay');
  if (!replayId) return;
  if (atEnd) _removeParamsFromURL(['at']); // 読み込みに成功したら replay も外す（下）。失敗したときは replay を残す

  try {
    const res = await fetch(`/api/solitaire/replay/${replayId}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const { images, names, logs, exCardIds, cardTypes, title } = await res.json();

    const { _setReplayData } = await import('../services/replay-service.js');
    if (typeof _setReplayData === 'function') {
      // card:// センチネル対応のため await が必要（非同期関数）
      await _setReplayData(images, names || {}, logs, exCardIds || [], cardTypes || {});
    }
    console.log(`リプレイID ${replayId} を読み込みました`);
    _finishLoadFromURL(atEnd, { source: 'shared', title });
    // 続きから遊ぶ（at=end）場合は、再読込でサーバーのリプレイを最初から読み直さないよう replay も外す
    // （?saved= と同じ扱い。通常の共有リンクの ?replay= は従来どおり残す）
    if (atEnd) _removeParamsFromURL(['replay']);
  } catch (e) {
    console.warn('リプレイの読み込みに失敗:', e.message);
  }
}

/**
 * URL からリプレイを読み込んだ後の共通の締め。
 * 録画状態を捨て、atEnd なら最後の手まで進めてから、読み込み完了を知らせる。
 * 通常は mobile-ui.js が受けて再生モードを自動で開く（タスクB-1）。atEnd のときは detail.atEnd で
 * 「再生モードに入らず、そのまま操作を続ける」ことを伝える。
 * 最後の手にカーソルがあるので、続けて1手打っても記録は切り捨てられない（logEvent は末尾でのみ追記）。
 * detail の source（'shared'＝共有リンク／'saved'＝このブラウザに保存したもの）と title は、
 * 読み込み直後に出す「▶ 再生する」の案内（replay-start-prompt.js）が使う。
 * @param {boolean} atEnd
 * @param {{source?:'shared'|'saved', title?:string}} [info]
 */
function _finishLoadFromURL(atEnd, info = {}) {
  _resetPcRecording(); // 重大4対応: JSON読込で古い録画範囲(_pcRecording)が残留するのを防ぐ
  if (atEnd) seekTo(getLogLength() - 1);
  document.dispatchEvent(new CustomEvent('sol-replay-loaded-from-url', {
    detail: { atEnd, source: info.source || 'shared', title: typeof info.title === 'string' ? info.title : '' },
  }));
}

/** URL から指定のクエリを外す（他のクエリ・ハッシュは残す） */
function _removeParamsFromURL(names) {
  const url = new URL(location.href);
  names.forEach((n) => url.searchParams.delete(n));
  history.replaceState(null, '', url.pathname + url.search + url.hash);
}

/**
 * ?saved=<id> で端末保存のリプレイを開く（リプレイ一覧ページからの遷移）。
 * 読込後の扱いは ?replay= と同じ（_finishLoadFromURL。スマホでは mobile-ui.js がイベントを受けて再生モードを開く。
 * atEnd のときは最後の手まで進めて再生モードには入らない）。
 * 再読込で盤面が上書きされるのを防ぐため、読込の成否に関わらず URL から saved と at を消す。
 * @param {string} savedId
 * @param {boolean} [atEnd]
 */
async function _loadSavedFromURL(savedId, atEnd = false) {
  const item = savedReplaysGet().find(r => r.id === savedId);
  // URL から saved・at を外す（再読込で同じリプレイが盤面を上書きし続けるのを防ぐ）
  const url = new URL(location.href);
  url.searchParams.delete('saved');
  url.searchParams.delete('at');
  history.replaceState(null, '', url.pathname + url.search + url.hash);

  if (!item) {
    showToast('保存済みリプレイが見つかりません（別のブラウザで保存したものは開けません）');
    return;
  }
  try {
    await loadReplayFromCompressedData(item.data);
    _finishLoadFromURL(atEnd, { source: 'saved', title: item.title });
  } catch (e) {
    console.warn('保存済みリプレイの読み込みに失敗:', e);
    showToast('保存済みリプレイを開けませんでした');
  }
}
