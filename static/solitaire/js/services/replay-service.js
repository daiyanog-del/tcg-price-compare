/**
 * replay-service.js
 * イベントログ方式のリプレイシステム
 *
 * 設計: mira (mirayugioh) のイベントログ方式を参考にした自前実装。
 * miraのコードはライセンス未設定のため流用禁止。本ファイルはゼロから実装。
 *
 * アーキテクチャ:
 *   images = { cardId: src }   … 画像辞書（重い部分。1回だけ保持）
 *   logs   = [ ...event ]      … 操作ログ（軽い部分。URL共有に向く）
 *
 * イベントスキーマ:
 *   { seq, actionType, cardId?, zoneId?, zIndex?, transform?, orientation?, face?, counter?, text?,
 *     inPlace?, beforeCardId?, op?, index? }
 *
 * actionType:
 *   moveCard       … カードをゾーンに移動（orientation/face も含む）
 *                    inPlace=true: 同じゾーンの中で表示形式だけを変えた記録。再生は位置（重なり・並び）を動かさない
 *                    beforeCardId: デッキ列の途中へ戻したとき、その直後に来るカードのID（無ければ末尾）
 *   draw           … デッキからドロー（cardId=ドローしたカードID）
 *   returnToDeck   … カードをデッキに戻す
 *   resetDeck      … 全カードをデッキへ戻す。現行の「リセット」ボタンは記録ごと初期化するためこのログを
 *                    残さない。旧仕様で作られた共有リプレイ（drawnIds に5ドローを含む）の再生用に残している
 *   counterChange  … カウンター（counter: 値）。cardId なしは右パネルの基準カウンター。
 *                    カード上は op（'add' 置く / 'set' 値変更・省略時 / 'remove' 外す）と
 *                    index（そのカード上のカウンターの並び順・省略時 0）で特定する
 *   removeCard     … トークンを盤面から消す（zoneId=消す前にいたゾーン）。再生は要素を消し、
 *                    巻き戻しでは画像・名前の辞書から作り直して元の位置へ戻す
 *   comment        … コメント挿入
 *   activateEffect … 効果発動（cardId のカードで発動演出を再生）
 *
 * ゾーンID（Fugartaの要素に対応）:
 *   poolRow, poolRow2,
 *   custom-slot-N (N=1〜14),
 *   side-slot-0 (墓地), side-slot-1 (除外),
 *   center-slot,
 *   free-space
 *
 * リプレイ payload の省略可項目: cardTypes = { cardId: 'monster'|'spell'|'trap' }
 * （再生・復元でカードを作るとき data-card-type を付け直す）
 */

import { returnAllCardsToDeck, attachCardImageListeners, sortPoolCards } from '../components/card-manager.js';
import { applyCardState, getCardState, isTokenSrc, isTokenCard, markTokenIfSrc } from '../components/card-state.js';
import { createCardCounter } from '../components/counter-manager.js';
import { restackCustomSlot } from '../components/slot-stack.js';
import { playActivateEffect, flipMoveClone, playSetFlip, flyBetweenRects, prefersReducedMotion } from '../components/card-effects.js';
import { createProxyCardElement } from '../components/proxy-card.js';
import { isMobilePortrait } from '../utils/viewport.js';

const _API_CARD_IMAGES = '/api/card-images';

// ── 状態 ──────────────────────────────────────────────
let _images = {};       // { cardId: src }
let _names  = {};       // { cardId: cardName }
let _exCardIds = new Set(); // EXデッキに属するカードIDの集合
let _cardTypes = {};    // { cardId: 'monster'|'spell'|'trap' }（data-card-type の付け直し用。省略可・旧リプレイには無い）
let _logs   = [];       // イベントログ配列（手数として扱う。setup ログは含まない）
// D-3: 範囲共有（設計書 §D）で先頭に付く「初期配置」ログ。setup:true が付いた要素のみを
// 保持し、_setReplayData / _applyReplayPayload / importFromURLHash 時に初期配置として即時適用する。
// カウンター・スライダー・前後/再生の対象は _logs のみで、_setupLogs は手数に数えない。
let _setupLogs = [];
let _cursor = -1;       // 現在の再生位置（-1 = ログ先頭の盤面外）
let _playing = false;
let _playTimer = null;
const DEFAULT_INTERVAL_MS = 900;
// 再生速度倍率（有効値: 0.5 / 1 / 2）。実際の再生間隔は DEFAULT_INTERVAL_MS / _speedMultiplier
let _speedMultiplier = 1;

// リプレイ前進時のみ true にしてアニメーション（FLIP / 発動演出）を有効化。
// _replayTo 経由の後退・全再構築では false のまま（瞬間適用）。
let _animateForward = false;

// 再生による演出のうち「完了コールバックで DOM を確定するもの」の進行中の数。
// ページ離脱時に盤面が中間状態かの判定（isReplayAnimating）に使う。
// 全作り直し（_replayTo・initReplay・restoreReplaySession）で 0 に戻し、世代を進めて
// 古い完了コールバックが後から数を狂わせないようにする。
let _pendingReplayAnims = 0;
let _animGen = 0;

function _resetAnimMarks() {
  _pendingReplayAnims = 0;
  _animGen++;
}

/**
 * 完了コールバックを包み、呼ばれるまで _pendingReplayAnims を1つ上げておく。
 * 返した関数は何度呼ばれても1回しか数えない。作り直し後（世代が変わった後）の呼び出しは数に影響しない。
 */
function _trackAnim(cb) {
  _pendingReplayAnims++;
  const gen = _animGen;
  let done = false;
  return (...args) => {
    if (done) return;
    done = true;
    try {
      if (cb) cb(...args);
    } finally {
      if (gen === _animGen) _pendingReplayAnims = Math.max(0, _pendingReplayAnims - 1);
    }
  };
}

/** 再生の演出が進行中か（完了コールバックで盤面を確定する演出が未完了か） */
export function isReplayAnimating() {
  return _pendingReplayAnims > 0;
}

// ── 外部API ──────────────────────────────────────────────

/**
 * リプレイシステムを初期化（ページロード時に呼ぶ）
 */
export function initReplay() {
  _images = {};
  _names  = {};
  _exCardIds = new Set();
  _cardTypes = {};
  _logs = [];
  _setupLogs = [];
  _cursor = -1;
  _playing = false;
  _resetAnimMarks();
  _updateUI();
}

/**
 * カード登録（Neuron切り出し/api/card-image 両方で呼ぶ）
 * @param {string} cardId  - カードID（item-NNN 形式）
 * @param {string} src     - 画像URL or dataURL
 */
export function registerCardImage(cardId, src) {
  _images[cardId] = src;
}

/**
 * カードをEXデッキとしてマーク（poolRow2に追加されたカード）
 * @param {string} cardId
 */
export function registerCardIsEx(cardId) {
  _exCardIds.add(cardId);
}

/**
 * カードの種類（monster / spell / trap）を登録（再生・復元でカードを作るとき data-card-type を付け直すため）
 * @param {string} cardId
 * @param {string} type
 */
export function registerCardType(cardId, type) {
  if (type) _cardTypes[cardId] = type;
}

/**
 * カード名を登録（カード詳細パネル用）
 * @param {string} cardId  - カードID（item-NNN 形式）
 * @param {string} name    - カード名
 */
export function registerCardName(cardId, name) {
  if (name) _names[cardId] = name;
}

/**
 * カードIDから画像ソースを取得
 * @param {string} cardId
 * @returns {string|null}
 */
export function getCardSrc(cardId) {
  return _images[cardId] ?? null;
}

/**
 * イベントをログに追記
 * @param {Object} event  - イベントオブジェクト（seq自動付与）
 */
export function logEvent(event) {
  // 巻き戻した位置で操作したら、それより先のログは確認なしで切り捨てる
  // （cursor=-1 の0手目・読み込み直後も同じ。-1 なら全ログが空になる）
  if (_cursor < _logs.length - 1) {
    _logs = _logs.slice(0, _cursor + 1);
  }
  const seq = _logs.length;
  _logs.push({ seq, ..._safeCloneEvent(event) });
  _cursor = _logs.length - 1;
  _updateUI();
}

/**
 * 現在位置（_cursor）より先のログを切り捨てる（cursor=-1 なら全ログを空にする）。
 * 「録画をやり直す」で録画開始時点へ巻き戻した直後に呼び、未来ログの切り捨てを確定させる。
 * logEvent も同じ切り捨てを行うが、録画やり直しでは操作の前に切り捨てを確定させたいので
 * 単独の関数として残している。
 * 盤面は _cursor の位置のまま変わらない（巻き戻し済み前提）。
 */
export function truncateAfterCursor() {
  if (_cursor >= _logs.length - 1) return;
  _logs = _logs.slice(0, _cursor + 1);
  _updateUI();
}

/**
 * 案B: 開始直後（記録0件の状態）のリセットのみを setup ログとして記録する。
 * logEvent と違い _logs ではなく _setupLogs に積む（手数カウンターに数えない・取消の対象外）。
 * 呼び出し元（event-handlers.js）が「記録0件かどうか」を判定してから呼ぶこと。
 * @param {Object} event  - イベントオブジェクト（setup:true を強制付与）
 */
export function logSetupEvent(event) {
  const cloned = { ..._safeCloneEvent(event), setup: true, seq: -1 };
  // 開始直後のリセットを連打すると呼び出し元の判定（記録0件）が
  // 毎回成立してしまうため、末尾が同じ resetDeck であれば追加せず置換する。
  // setupログ全体には範囲共有由来の counterChange 等が混在し得るため、
  // truncateはせず「末尾が同種actionTypeの場合のみ置換」に限定する。
  const last = _setupLogs[_setupLogs.length - 1];
  if (last && last.actionType === cloned.actionType && cloned.actionType === 'resetDeck') {
    _setupLogs[_setupLogs.length - 1] = cloned;
  } else {
    _setupLogs.push(cloned);
  }
  _updateUI();
}

/**
 * undo: いま見ている手（_cursor の手）とそれより先を消し、1手前の盤面に戻す。
 * 末尾にいる場合は最後の1手を取り消すのと同じ。0手目（_cursor=-1）では何もしない。
 * 自動再生中も何もしない（再生途中の手を基準に消すと、見ていない記録まで大量に消えるため）。
 */
export function undoLast() {
  if (_cursor <= -1 || _playing) return;
  _logs = _logs.slice(0, _cursor);
  _replayTo(_logs.length - 1);
}

/** 1手進む */
export function stepForward() {
  if (_cursor >= _logs.length - 1) return;
  const next = _cursor + 1;
  _animateForward = true;
  _applyEvent(_logs[next]);
  _animateForward = false;
  _cursor = next;
  _maybeShowCommentToast(_cursor);
  _updateUI();
}

/** 1手戻る */
export function stepBack() {
  if (_cursor <= -1) return;
  const target = _cursor - 1;
  _replayTo(target);
}

/** 手番Nにジャンプ */
export function seekTo(n) {
  const target = Math.max(-1, Math.min(n, _logs.length - 1));
  if (target === _cursor) return;
  if (target > _cursor) {
    // 前進：差分だけ適用（最終手のみアニメ）
    for (let i = _cursor + 1; i < target; i++) {
      _applyEvent(_logs[i]);
    }
    _animateForward = true;
    _applyEvent(_logs[target]);
    _animateForward = false;
    _cursor = target;
  } else {
    // 後退：最初から再適用
    _replayTo(target);
  }
  _maybeShowCommentToast(_cursor);
  _updateUI();
}

/**
 * 再生速度を変更する（有効値: 0.5 / 1 / 2）
 * togglePlay中でも次のタイマー設定（_playNext内）から新しい間隔が反映される。
 * @param {number} multiplier
 */
export function setPlaySpeed(multiplier) {
  _speedMultiplier = multiplier;
  _updateUI();
}

/** 現在の再生速度倍率を取得（UIのボタンハイライト用） */
export function getPlaySpeed() {
  return _speedMultiplier;
}

/** 再生/一時停止 */
export function togglePlay() {
  if (_playing) {
    _stopPlay();
  } else {
    _startPlay();
  }
}

/** 現在自動再生中かどうか（PC版「この範囲を再生」等、再生中の判定が必要な呼び出し元用） */
export function isPlaying() {
  return _playing;
}

/**
 * D-3/D-4: logs 配列の先頭から連続する setup:true の要素を取り出す。
 * setup:true が付いていない要素に到達した時点で走査を止める（間に非setupが挟まっている
 * 場合、それ以降の setup:true 風要素は誤爆防止のため通常ログとして扱う）。
 * @param {Array} logs
 * @returns {{setupLogs: Array, logs: Array}}
 */
function _splitSetupLogs(logs) {
  const setupLogs = [];
  let i = 0;
  while (i < logs.length && logs[i]?.setup === true) {
    setupLogs.push(logs[i]);
    i++;
  }
  // Low-11: 本体ログ（setup を除いた部分）の seq を 0 から振り直す。
  // logEvent() は seq = _logs.length（本体ログの長さ）で採番するため、範囲共有等で
  // setup が挟まった状態のログを再インポートしたときも同じ採番規則に揃える必要がある。
  // setup 側の seq は元の値のまま変更しない（本体の手数としては数えないため）。
  const bodyLogs = logs.slice(i).map((ev, idx) => ({ ...ev, seq: idx }));
  return { setupLogs, logs: bodyLogs };
}

/**
 * D-3: setup ログを初期配置として即時適用する（演出・待ち時間なし）。
 * 呼び出し前提: プールへのカード配置（_createReplayCardElements/_rebuildDeck）が完了していること。
 */
function _applySetupLogs() {
  for (const ev of _setupLogs) _applyEvent(ev);
}

/**
 * リプレイの payload オブジェクトを組み立てる（exportReplay・E-2端末保存の共通ロジック）。
 * D-4: setup ログもそのまま含める（加工しない）。
 * タスクA-4: logsOverride を渡すと（録画範囲の共有・端末保存など）、通常の全ログの代わりに
 * それを使う。加工（setup付与・範囲抽出）自体は呼び出し元（replay-ui.js の
 * buildRangeLogsForPayload 等）が行い、ここではロジックを複製しない。
 * @param {string} title
 * @param {Array} [logsOverride]
 */
export function buildReplayPayload(title = 'replay', logsOverride) {
  return {
    version: 1, title, images: _images, names: _names, exCardIds: [..._exCardIds],
    ..._cardTypesField(),
    logs: logsOverride ?? [..._setupLogs, ..._logs],
  };
}

/** payload に載せる cardTypes 項目（1件も無ければ項目ごと省略して旧形式と同じ形にする） */
function _cardTypesField() {
  return Object.keys(_cardTypes).length > 0 ? { cardTypes: _cardTypes } : {};
}

/** リプレイデータをエクスポート（ファイル保存用）。D-4: setup ログもそのまま含めて保存する */
export function exportReplay(title = 'replay') {
  const payload = buildReplayPayload(title);
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${title.replace(/\s/g, '_')}.json`;
  a.click();
  URL.revokeObjectURL(url);
}

/**
 * リプレイ payload（{images, names, logs, exCardIds}）を盤面に反映する共通処理。
 * E-3 保存済みリプレイの「開く」（loadReplayFromCompressedData）から呼ぶ。
 * （JSON ファイルはリプレイ一覧ページで端末保存に登録してから、この経路で開く）
 * D-4: setup 付きログもそのまま通す（加工しない）。
 * @param {{images:Object, names?:Object, exCardIds?:Array, logs:Array}} payload
 */
async function _applyReplayPayload(payload) {
  _images = payload.images;
  _names  = payload.names || {};
  _exCardIds = new Set(payload.exCardIds || []);
  _cardTypes = { ...(payload.cardTypes || {}) };
  const split = _splitSetupLogs(payload.logs);
  _setupLogs = split.setupLogs;
  _logs = split.logs;
  _cursor = -1;
  // card:// センチネルの解決を含むため await が必要
  await _createReplayCardElements();
  _applySetupLogs();
  _updateUI();
}

/**
 * E-3: 端末保存された圧縮リプレイデータ（LZString.compress の出力）を復元する。
 * 他の読込経路と同じ _applyReplayPayload を経由する（ロジックを複製しない）。
 * @param {string} compressed
 */
export async function loadReplayFromCompressedData(compressed) {
  if (typeof LZString === 'undefined') throw new Error('LZStringが利用できません');
  const json = LZString.decompress(compressed);
  if (!json) throw new Error('リプレイデータの展開に失敗しました');
  const payload = JSON.parse(json);
  if (!payload.logs || !payload.images) throw new Error('不正なリプレイデータです');
  await _applyReplayPayload(payload);
}

/**
 * テキスト/メタデッキ読込時のURL共有用 LZString圧縮エクスポート
 * @param {Array} [logsOverride]  D-2: 範囲共有用に加工済みの logs を渡せる（省略時は全ログ）
 */
export function exportAsURLHash(logsOverride) {
  if (typeof LZString === 'undefined') return null;
  try {
    const logs = logsOverride ?? [..._setupLogs, ..._logs];
    const payload = { version: 1, images: _images, names: _names, exCardIds: [..._exCardIds], ..._cardTypesField(), logs };
    return LZString.compressToEncodedURIComponent(JSON.stringify(payload));
  } catch {
    return null;
  }
}

/** URLハッシュから読み込み（card:// センチネル対応のため非同期化）。D-4: setup 付きログもそのまま通す */
export async function importFromURLHash(hash) {
  if (typeof LZString === 'undefined') return false;
  try {
    const json = LZString.decompressFromEncodedURIComponent(hash);
    const payload = JSON.parse(json);
    if (!payload.logs || !payload.images) return false;
    _images = payload.images;
    _names  = payload.names || {};
    _exCardIds = new Set(payload.exCardIds || []);
    _cardTypes = { ...(payload.cardTypes || {}) };
    const split = _splitSetupLogs(payload.logs);
    _setupLogs = split.setupLogs;
    _logs = split.logs;
    _cursor = -1;
    // card:// センチネルの解決を含むため await が必要
    await _createReplayCardElements();
    _applySetupLogs();
    _updateUI();
    return true;
  } catch {
    return false;
  }
}

/** 現在のログ数（ステップ数） */
export function getLogLength() { return _logs.length; }
/** 現在のカーソル位置 */
export function getCursor() { return _cursor; }
/** 画像辞書を取得（外部からの参照用） */
export function getImages() { return _images; }
/** カード名辞書を取得（外部からの参照用） */
export function getNames() { return _names; }
/** カード種類の辞書を取得（外部からの参照用。共有時にサーバーへ送る） */
export function getCardTypes() { return _cardTypes; }
/** EXカードIDの配列を取得（外部からの参照用） */
export function getExCardIds() { return [..._exCardIds]; }
/** ログを取得 */
export function getLogs() { return _logs; }
/** High-1: setup ログ（初期配置。範囲共有の再共有時に先頭へ結合するため）を取得 */
export function getSetupLogs() { return _setupLogs; }

/**
 * ページ遷移をまたぐ退避用に、記録の状態を写しとして返す（session-resume 用）。
 * 記録（ログ・setupログ）が両方とも空なら null を返す（退避不要）。
 * @returns {{logs:Array, setupLogs:Array, cursor:number, images:Object, names:Object, exCardIds:string[], cardTypes:Object}|null}
 */
export function captureReplaySession() {
  if (_logs.length === 0 && _setupLogs.length === 0) return null;
  return {
    logs: _logs,
    setupLogs: _setupLogs,
    cursor: _cursor,
    images: _images,
    names: _names,
    exCardIds: [..._exCardIds],
    cardTypes: _cardTypes,
  };
}

/**
 * captureReplaySession の写しを状態へ戻す。
 * 盤面は呼び出し側が別途（盤面の写しから）復元済みで、その盤面はカーソル位置の盤面そのものなので、
 * ここでは変数に代入して UI を更新するだけにする（盤面の再構築・_replayTo・seekTo は呼ばない。
 * 呼ぶと二重適用になりカーソルも動く）。再生は停止状態のまま。
 * @param {Object} snap captureReplaySession の戻り値
 */
export function restoreReplaySession(snap) {
  if (!snap) return;
  _stopPlay();
  _logs = Array.isArray(snap.logs) ? snap.logs : [];
  _setupLogs = Array.isArray(snap.setupLogs) ? snap.setupLogs : [];
  const maxCursor = _logs.length - 1;
  const cur = Number.isInteger(snap.cursor) ? snap.cursor : maxCursor;
  _cursor = Math.max(-1, Math.min(cur, maxCursor));
  _images = { ...(snap.images || {}) };
  _names = { ...(snap.names || {}) };
  _exCardIds = new Set(snap.exCardIds || []);
  _cardTypes = { ...(snap.cardTypes || {}) };
  _resetAnimMarks();
  _updateUI();
}

/**
 * 現在のカーソル位置の盤面を、演出なしで最初から作り直す（session-resume 用）。
 * 演出（飛行クローン等）の途中でページを離れた退避は、盤面の写しが中間状態の可能性があるため
 * 復元後にこれで正しい盤面へ直す。再生は停止し、カーソルは動かさない。
 */
export function rebuildAtCursor() {
  _stopPlay();
  _replayTo(_cursor, { toast: false }); // 復元直後にコメントトーストを出さない
}

/**
 * リプレイ記録を初期化する（「すべて消去」用・deck-input-panel.js clearEverything から呼ばれる）。
 * 再生中なら停止してから、ページロード時と同じ initReplay() で状態を空に戻す
 * （ロジック複製を避け、状態リセット自体は initReplay に委譲する）。
 * 盤面DOMの掃除は呼び出し元（clearAllCards）が担当するため、ここでは行わない。
 */
export function resetReplay() {
  _stopPlay();
  initReplay();
}

/**
 * 記録（ログ・setupログ・カーソル）だけを初期化する（「リセット」用・event-handlers.js handleReset から呼ばれる）。
 * resetReplay / initReplay と違い、画像辞書・カード名辞書・EX判定は保持する
 * （読み込んだデッキは残すため。ただし盤面に無いトークンの項目は消す）。盤面DOMは呼び出し元（returnAllCardsToDeck）が担当する。
 */
export function clearReplayLog() {
  _stopPlay(); // 再生中なら停止（内部で _updateUI も呼ばれる）
  _logs = [];
  _setupLogs = [];
  _cursor = -1;
  // 盤面に無いトークン（記録を始める前に削除したもの）を辞書から消す。残すと、リセット後の
  // 巻き戻しで _restoreRemovedTokens が削除済みのトークンをデッキへ作り直してしまう
  for (const [cardId, src] of Object.entries(_images)) {
    if (isTokenSrc(src) && !document.getElementById(cardId)) {
      delete _images[cardId];
      delete _names[cardId];
      delete _cardTypes[cardId];
      _exCardIds.delete(cardId);
    }
  }
  // 表示中のコメントトーストが残らないよう即座に隠す
  const toast = document.getElementById('replayCommentToast');
  if (toast) {
    clearTimeout(toast._hideTimer);
    toast.classList.remove('rct-show');
    toast.classList.add('rct-hide');
  }
  _updateUI();
}

/**
 * 外部から画像辞書・ログを直接セット（Supabase読み込み用）
 * card:// センチネル対応のため非同期化。
 * @param {Object} images  { cardId: src }
 * @param {Object} names   { cardId: cardName }（省略可）
 * @param {Array}  logs    イベントログ配列
 * @param {Array}  exCardIds  EXカードID（省略可）
 * @param {Object} cardTypes  { cardId: 'monster'|'spell'|'trap' }（省略可・旧リプレイには無い）
 */
export async function _setReplayData(images, names, logs, exCardIds = [], cardTypes = {}) {
  _images = images;
  _names  = names || {};
  _exCardIds = new Set(exCardIds);
  _cardTypes = { ...(cardTypes || {}) };
  const split = _splitSetupLogs(logs);
  _setupLogs = split.setupLogs;
  _logs = split.logs;
  _cursor = -1;
  // card:// センチネルの解決を含むため await が必要
  await _createReplayCardElements();
  // D-3: setup ログを初期配置として即時適用（演出・待ち時間なし）。以降のログだけを手数として扱う
  _applySetupLogs();
  _updateUI();
}

// ── 内部処理 ──────────────────────────────────────────────

function _safeCloneEvent(event) {
  // 将来の拡張に備えてシャローコピー
  return { ...event };
}

/**
 * 手番Nまでを最初から再適用（後退・seekTo で使う）
 * @param {number} target  -1 = 初期盤面
 */
function _replayTo(target, { toast = true } = {}) {
  _resetAnimMarks(); // 全作り直し: 古い演出の完了待ちは無効にする
  _rebuildDeck();
  // D-3: setup ログ（初期配置）を通常ログより先に、演出なしで再適用する
  _applySetupLogs();
  for (let i = 0; i <= target; i++) {
    _applyEvent(_logs[i]);
  }
  _cursor = target;
  if (toast) _maybeShowCommentToast(_cursor);
  _updateUI();
}

/**
 * デッキを再構築（全カードをプールに戻す）
 * - Fugartaの returnAllCardsToDeck を流用
 * - カードの上のカウンターは再適用で作り直すため、先に全部片付ける
 *   （returnAllCardsToDeck は片付けない。残すと巻き戻しのたびに増える・値が残る）
 * - 削除されたトークンは辞書（画像・名前）から作り直す。再適用の途中で
 *   removeCard が来た時点で改めて消える
 */
function _rebuildDeck() {
  document.querySelectorAll('.tier-item-wrapper > .counter-container').forEach(el => el.remove());
  _restoreRemovedTokens();
  returnAllCardsToDeck();
}

/**
 * 盤面に無いトークンを画像辞書から作り直してデッキ列（#poolRow）へ置く。
 * 削除したトークンまで巻き戻したとき、削除より前の手を正しく再現するための処理。
 * 普通のカードは削除できないため対象外（card:// の再解決は非同期で、ここは同期処理のため）。
 */
function _restoreRemovedTokens() {
  const poolRow = document.getElementById('poolRow');
  if (!poolRow) return;
  for (const [cardId, src] of Object.entries(_images)) {
    if (!isTokenSrc(src) || document.getElementById(cardId)) continue;
    poolRow.appendChild(_buildReplayCard(cardId, src, false, new Map()));
  }
}

/**
 * _images 内の card:// センチネルをバッチ解決して Map で返す
 * 解決不要のカード（実URL）は含まない。card:// が1件も無ければ空 Map を返す。
 *
 * @returns {Promise<Map<string, Object>>} カード名 -> { kind, url?, proxy? }
 */
async function _resolveReplaySentinels() {
  const sentinelNames = [];
  for (const src of Object.values(_images)) {
    if (src && src.startsWith('card://')) {
      const name = src.slice('card://'.length);
      if (name && !sentinelNames.includes(name)) sentinelNames.push(name);
    }
  }
  if (sentinelNames.length === 0) return new Map();

  try {
    const res = await fetch(_API_CARD_IMAGES, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ names: sentinelNames }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const images = data.images || {};

    const result = new Map();
    for (const name of sentinelNames) {
      const val = images[name] ?? null;
      if (!val) {
        result.set(name, { kind: 'none' });
      } else if (typeof val === 'string') {
        result.set(name, { kind: 'image', url: val });
      } else {
        result.set(name, val);
      }
    }
    return result;
  } catch (e) {
    console.warn('[replay] card:// 解決失敗（プロキシ風表示にフォールバック）:', e);
    const fallback = new Map();
    for (const name of sentinelNames) {
      fallback.set(name, { kind: 'none' });
    }
    return fallback;
  }
}

/**
 * _images の全カードをDOMに生成してプールに配置する
 * URL共有・ファイルインポート時、デッキが空の状態から再生できるようにする。
 * card:// センチネルは /api/card-images バッチで再解決してから描画する。
 */
async function _createReplayCardElements() {
  // 既存カードを全てプールに回収してからクリア
  returnAllCardsToDeck();
  const poolRow  = document.getElementById('poolRow');
  const poolRow2 = document.getElementById('poolRow2');
  if (!poolRow || !poolRow2) return;
  poolRow.querySelectorAll('.tier-item-wrapper').forEach(el => el.remove());
  poolRow2.querySelectorAll('.tier-item-wrapper').forEach(el => el.remove());

  // card:// センチネルを一括解決
  const resolved = await _resolveReplaySentinels();

  // EXカードIDを特定（_exCardIdsが空の場合はログから推定して旧形式に対応）
  const exCardIds = _exCardIds.size > 0
    ? _exCardIds
    : new Set(_logs.filter(e => e.actionType === 'returnToDeck' && e.isEx).map(e => e.cardId));

  for (const [cardId, src] of Object.entries(_images)) {
    const isEx = exCardIds.has(cardId);
    (isEx ? poolRow2 : poolRow).appendChild(_buildReplayCard(cardId, src, isEx, resolved));
  }
  // 読み込み直後のデッキ列の並びを、デッキ読込時と同じ（種類順→ID順）にそろえる
  // （data-card-type を付け直したので、巻き戻し時の returnAllCardsToDeck の並びとも一致する）
  sortPoolCards('poolRow');
  sortPoolCards('poolRow2');
}

/**
 * 画像辞書の1件から .tier-item-wrapper を作る（_createReplayCardElements と
 * 削除トークンの作り直しで共有）。data-card-type・data-token も付け直す。
 * @param {string} cardId
 * @param {string} src       辞書の画像（実URL / data: / card://名前）
 * @param {boolean} isEx
 * @param {Map<string,Object>} resolved  card:// の解決結果（カード名 -> {kind,...}）
 * @returns {Element}
 */
function _buildReplayCard(cardId, src, isEx, resolved) {
  const wrapper = document.createElement('div');
  wrapper.classList.add('tier-item-wrapper');
  wrapper.id = `${cardId} ${isEx ? 'ex' : 'normal'}`;
  if (_cardTypes[cardId]) wrapper.dataset.cardType = _cardTypes[cardId];
  markTokenIfSrc(wrapper, src);

  let cardEl;

  if (src && src.startsWith('card://')) {
    // card:// センチネル: 再解決結果に基づいてカード要素を生成
    const name = src.slice('card://'.length);
    const displayResult = resolved.get(name) || { kind: 'none' };

    if (displayResult.kind === 'image' && displayResult.url) {
      // 発売済み or linked 後の正規画像
      const img = document.createElement('img');
      img.src = displayResult.url;
      img.classList.add('tier-item');
      img.setAttribute('draggable', 'true');
      img.id = cardId;
      if (_names[cardId]) img.dataset.cardName = _names[cardId];
      cardEl = img;
      // 発売済みクリーン画像にのみ透かし（SAMPLE画像は除外）
      if (displayResult.source !== 'official_sample') wrapper.classList.add('wm-released');
      else wrapper.classList.add('sample-art'); // 未発売SAMPLE（イラストぼかし設定の対象）
    } else if (displayResult.kind === 'proxy' && displayResult.proxy) {
      // 未発売プロキシ
      const proxyEl = createProxyCardElement(displayResult.proxy);
      proxyEl.setAttribute('draggable', 'true');
      proxyEl.id = cardId;
      if (_names[cardId]) proxyEl.dataset.cardName = _names[cardId];
      cardEl = proxyEl;
    } else {
      // kind='none'（却下・削除済み等）: カード名のみのフォールバックプロキシ
      const fallbackProxy = { name: name || _names[cardId] || '不明なカード' };
      const proxyEl = createProxyCardElement(fallbackProxy);
      proxyEl.setAttribute('draggable', 'true');
      proxyEl.id = cardId;
      if (_names[cardId]) proxyEl.dataset.cardName = _names[cardId];
      cardEl = proxyEl;
    }
  } else {
    // 通常の実URL（後方互換: 既存リプレイはこのパスを通る）。実URLは発売済みクリーン画像
    const img = document.createElement('img');
    img.src = src;
    img.classList.add('tier-item');
    img.setAttribute('draggable', 'true');
    img.id = cardId;
    if (_names[cardId]) img.dataset.cardName = _names[cardId];
    cardEl = img;
    wrapper.classList.add('wm-released');
  }

  attachCardImageListeners(wrapper, cardEl);
  wrapper.appendChild(cardEl);
  return wrapper;
}

/**
 * イベントを1件適用してDOMを更新
 * @param {Object} event
 */
function _applyEvent(event) {
  switch (event.actionType) {
    case 'moveCard':
      _applyMoveCard(event);
      break;
    case 'draw':
      _applyDraw(event);
      break;
    case 'returnToDeck':
      _applyReturnToDeck(event);
      break;
    case 'resetDeck':
      _applyResetDeck(event);
      break;
    case 'counterChange':
      _applyCounterChange(event);
      break;
    case 'removeCard':
      _applyRemoveCard(event);
      break;
    case 'comment':
      // コメントはDOMに反映しない（ログのみ）
      break;
    case 'activateEffect':
      _applyActivateEffect(event);
      break;
  }
}

/** moveCard: cardId のカードを zoneId に移動（守備・セット状態も復元） */
function _applyMoveCard(event) {
  const { cardId, zoneId, zIndex, transform, orientation, face } = event;
  const card = _findCardWrapper(cardId);
  if (!card) return;

  const zone = _findZone(zoneId);
  if (!zone) return;

  // inPlace: 同じゾーンの中で表示形式だけを変えた記録。位置（重なりの順番・手札の並び）は動かさない。
  // （zIndex は「その時点の重なりの段」で、一番上へ移す意味ではないため、位置の決定に使わない）
  // 別のゾーンにいるなら従来どおり移動する（記録と盤面がずれていても動作を保つ）。
  const keepPosition = event.inPlace === true && card.parentElement === zone;
  const place = () => {
    if (!keepPosition) _placeCardInZone(zone, card, zIndex, event.beforeCardId);
  };

  card.style.transform = transform || '';

  // 後退・全再構築（_replayTo経由）: アニメなしで即時適用
  if (!_animateForward) {
    applyCardState(card, { orientation, face });
    place();
    return;
  }

  // ── 前進再生 ─────────────────────────────────────────────────────────
  const firstRect = card.getBoundingClientRect();
  const sameZone  = (card.parentElement === zone);
  const prevState = getCardState(card);
  const faceChanged   = prevState.face        !== (face        || '');
  const orientChanged = prevState.orientation !== (orientation || '');
  const stateChanged  = faceChanged || orientChanged;

  // タスクB: 縦向きスマホでは #poolRow（デッキ列）が display:none のため、移動元/移動先が
  // #poolRow だと矩形が0幅になり flipMoveClone の演出が成立しない。#solMobileDeckBtn の
  // 矩形を代替の出発点/到着点にして飛ばす（PC・横向き・#poolRow が絡まない移動は従来どおり）。
  const mobilePortrait = isMobilePortrait();
  const toIsDeckPool = zoneId === 'poolRow' && mobilePortrait;
  const fromIsEmpty  = mobilePortrait && firstRect.width === 0;

  if (toIsDeckPool || fromIsEmpty) {
    _applyMoveCardWithDeckFly({ card, place, orientation, face, faceChanged, stateChanged, firstRect, toIsDeckPool });
    return;
  }

  if (sameZone && stateChanged) {
    // ── 同一ゾーン内の状態変更（守備/セット切替など） ──────────────────
    // 移動なし（dx≈0）のため flipMoveClone がすぐ onComplete を呼ぶ。
    // 状態はコールバック内で変更し、変化アニメ（回転/フリップ）を見せる。
    place();
    flipMoveClone(card, firstRect, _trackAnim(() => {
      if (faceChanged) {
        playSetFlip(card, _trackAnim(() => applyCardState(card, { orientation, face })));
      } else {
        applyCardState(card, { orientation, face }); // CSS transition が回転を担う
      }
    }));

  } else if (!sameZone && stateChanged) {
    // ── 別ゾーンへ移動 ＋ 状態変化（初期配置で守備/セット など） ───────
    // 状態変更「前」に .tier-item の transition を止める。
    // こうすることでオリジナルも cloneNode で生成するクローンも
    // 挿入時に 0° から再起動せず、最初から最終状態（守備回転済み等）になる。
    const tierItem = card.querySelector('.tier-item');
    if (tierItem) tierItem.style.transition = 'none';
    applyCardState(card, { orientation, face }); // 即時・アニメなしで状態確定
    place();
    flipMoveClone(card, firstRect, () => {
      // FLIP 完了後にトランジションを復元（次の操作から回転アニメが効くよう）
      if (tierItem) tierItem.style.transition = '';
    });

  } else {
    // ── 状態変化なし ──────────────────────────────────────────────────
    applyCardState(card, { orientation, face });
    place();
    flipMoveClone(card, firstRect, null);
  }
}

/**
 * 第6次実機検収-2a: デッキ演出の錨（アンカー）要素を返す。
 * 通常モードでは #solMobileDeckBtn（下部バーのデッキボタン）が可視だが、再生モード中は
 * 下部バーが再生用の中身に切り替わり #solMobileDeckBtn が非表示（矩形0）になるため、
 * 演出の出発/到着点として機能しなくなる（司令塔の以前の確認は通常バーのままだったため見逃した
 * バグ）。#solMobileDeckBtn が可視（幅>0）ならそれを、そうでなければ再生モードの帯にある
 * #solMobilePlayDeckBadge を使う。パルス演出も同じ要素に当てる（_pulseDeckBtn）。
 * @returns {Element|null}
 */
function _getDeckAnchorEl() {
  const deckBtn = document.getElementById('solMobileDeckBtn');
  if (deckBtn && deckBtn.getBoundingClientRect().width > 0) return deckBtn;
  return document.getElementById('solMobilePlayDeckBadge');
}

/** @returns {DOMRect|null} */
function _getDeckAnchorRect() {
  const el = _getDeckAnchorEl();
  return el ? el.getBoundingClientRect() : null;
}

/**
 * タスクB: #poolRow（デッキ列。縦向きスマホでは非表示）が絡む moveCard の前進適用。
 * 出発点/到着点のうち #poolRow 側はデッキ演出の錨（_getDeckAnchorRect）の矩形で代替して飛ばす。
 * 到着点がデッキ側（toIsDeckPool）の場合、#poolRow の実際の矩形は取得できない（0幅）ため
 * クローンを飛ばし終えてから DOM を確定する。出発点だけの代替（toIsDeckPool=false）は
 * 従来の「別ゾーンへ移動」と同じ順序（状態確定 → DOM配置 → 演出）を保つ。
 */
function _applyMoveCardWithDeckFly({ card, place, orientation, face, faceChanged, stateChanged, firstRect, toIsDeckPool }) {
  const deckBtnRect = _getDeckAnchorRect(); // 第6次検収2a
  const fromRect = firstRect.width === 0 ? deckBtnRect : firstRect;

  _pulseDeckBtn();

  if (toIsDeckPool) {
    // 到着点はデッキ錨の矩形。飛ばし終えてから DOM を確定する（既存の onComplete 経路を維持）。
    if (stateChanged) applyCardState(card, { orientation, face });
    flyBetweenRects(card, fromRect, deckBtnRect, _trackAnim(_afterFly(card, place)));
    return;
  }

  // 出発点だけデッキ錨の矩形に差し替え、到着点は通常どおり最終位置（先にDOMを確定してから飛ばす）
  const tierItem = card.querySelector('.tier-item');
  if (stateChanged) {
    if (tierItem) tierItem.style.transition = 'none';
    applyCardState(card, { orientation, face });
  }
  place();
  flyBetweenRects(card, fromRect, null, () => {
    if (tierItem) tierItem.style.transition = '';
  });
}

/**
 * タスクB: 演出の間だけデッキ演出の錨（_getDeckAnchorEl）を軽く光らせる
 * （400ms・prefers-reduced-motion では何もしない）。
 */
function _pulseDeckBtn() {
  if (prefersReducedMotion()) return;
  const btn = _getDeckAnchorEl(); // 第6次検収2a
  if (!btn) return;
  btn.classList.remove('sol-pulse');
  void btn.offsetWidth; // 再アニメのための reflow 強制
  btn.classList.add('sol-pulse');
  setTimeout(() => btn.classList.remove('sol-pulse'), 400);
}

/**
 * 演出の完了後に配置を確定する処理を包む。演出の途中で作り直し（巻き戻し・次の手）が起きて世代が
 * 変わった、またはカードがもう画面に無い（削除されたトークンなど）なら、何もしない。
 * （そのまま配置すると、消えたカードが盤面に戻ってしまう）
 */
function _afterFly(card, fn) {
  const gen = _animGen;
  return () => {
    if (gen !== _animGen || !card.isConnected) return;
    fn();
  };
}

const DECK_FLY_STAGGER_MS = 80; // 第6次検収2c: resetDeck等で複数枚飛ばす際のずらし幅（60→80ms）

/**
 * タスクB追補: #poolRow が絡む draw/resetDeck/returnToDeck（moveCard以外のactionType）でも
 * 前進再生かつ縦向きスマホなら演出対象、という共通判定。
 * PC・横向き・後退（_animateForwardがfalse。_replayTo経由の全再構築やstepBack）では false。
 */
function _shouldMobileFly() {
  return _animateForward && isMobilePortrait();
}

/**
 * タスクB追補: デッキ演出の錨（_getDeckAnchorRect）の矩形から card の現在位置
 * （既に最終配置済み）へ飛ばす。呼び出し前提: _shouldMobileFly() が true であること
 * （呼び出し側で判定済み）。
 * @param {Element}  card
 * @param {DOMRect}  deckBtnRect
 * @param {number}   [delayMs=0]  resetDeckの複数枚ずらし用
 */
function _flyFromDeckBtn(card, deckBtnRect, delayMs = 0) {
  if (delayMs > 0) {
    setTimeout(() => flyBetweenRects(card, deckBtnRect, null, null), delayMs);
  } else {
    flyBetweenRects(card, deckBtnRect, null, null);
  }
}

/**
 * activateEffect: 効果発動演出（前進再生時のみ）
 * _replayTo による後退・全再構築では _animateForward が false のため演出はスキップ。
 */
function _applyActivateEffect(event) {
  if (!_animateForward) return;
  const { cardId } = event;
  const card = _findCardWrapper(cardId);
  if (!card) return;
  playActivateEffect(card);
}

/**
 * draw: cardId をデッキから手札(center-slot)へ（状態クリア）
 * タスクB追補: 前進再生かつ縦向きスマホではデッキ演出の錨（_getDeckAnchorRect）から飛ばす
 * （#poolRow は非表示のため、デッキから来たことが分かる演出をここで補う）。
 */
function _applyDraw(event) {
  const { cardId } = event;
  const card = _findCardWrapper(cardId);
  if (!card) return;
  const center = document.querySelector('.center-slot');
  if (!center) return;
  const sourceSlot = card.parentElement;
  card.style = '';
  applyCardState(card, {}); // 守備・セット状態をクリア
  center.appendChild(card);
  restackCustomSlot(sourceSlot);

  if (_shouldMobileFly()) {
    const deckBtnRect = _getDeckAnchorRect(); // 第6次検収2a
    if (deckBtnRect) {
      _pulseDeckBtn();
      _flyFromDeckBtn(card, deckBtnRect);
    }
  }
}

/**
 * returnToDeck: cardId をプールへ戻す（状態クリア）
 * タスクB追補: 非EX（#poolRow行き）かつ前進再生・縦向きスマホでは、現在位置から
 * デッキ演出の錨（_getDeckAnchorRect）へ飛ばし終えてから DOM を確定する（#poolRow は
 * 非表示のため実際の到着矩形が取れない。_applyMoveCardWithDeckFly の toIsDeckPool と同じ順序）。
 * EX（#poolRow2）は常時表示のため上記のデッキ演出分岐を通らないが、前進再生時は
 * moveCard（_applyMoveCard）と同じ方針で flipMoveClone による通常のFLIPアニメーションが
 * 入る（演出なしではない。以前は瞬間移動していたが、2026-09-15にPC/EX含めた全分岐で
 * アニメーションを追加した）。
 */
function _applyReturnToDeck(event) {
  const { cardId, isEx } = event;
  const card = _findCardWrapper(cardId);
  if (!card) return;
  const poolId = isEx ? 'poolRow2' : 'poolRow';
  const pool = document.getElementById(poolId);
  if (!pool) return;

  // 状態クリア＋DOM確定（PC/モバイル/EX/非EXの全分岐で共通）
  const commit = () => {
    const sourceSlot = card.parentElement;
    card.style = '';
    applyCardState(card, {}); // 守備・セット状態をクリア
    pool.appendChild(card);
    restackCustomSlot(sourceSlot);
  };

  if (!isEx && _shouldMobileFly()) {
    const deckBtnRect = _getDeckAnchorRect(); // 第6次検収2a
    if (deckBtnRect) {
      // 第6次検収2b: 出発の実カードを飛ばし始めた瞬間に隠す（flyBetweenRects が visibility:hidden
      // にするのはクローン生成の直前なので、fromRect 取得後・flyBetweenRects 呼び出し前に
      // 追加の隠蔽は不要。flyBetweenRects 内の cardEl.style.visibility='hidden' に委譲する）。
      const fromRect = card.getBoundingClientRect();
      _pulseDeckBtn();
      flyBetweenRects(card, fromRect, deckBtnRect, _trackAnim(_afterFly(card, commit)));
      return;
    }
  }

  // moveCard（_applyMoveCard）は前進再生時にPCでも flipMoveClone を呼ぶが、returnToDeck は
  // 上のモバイル縦向き分岐（flyBetweenRects）にしか演出がなく、PCでは瞬間移動していた。
  // _applyMoveCard と同じ方針で、前進再生時はPCでも FLIP アニメーションを挟む。
  // deckBtnRect が取れない縦向きスマホ（錨要素も非表示）でこの分岐に落ちてきた場合は、
  // flipMoveClone 側のゼロ矩形ガード（display:none 配下への移動を検知して即完了扱いにする）
  // に判定を委ね、ここでは個別に isMobilePortrait 等を判定しない。
  if (_animateForward) {
    const firstRect = card.getBoundingClientRect();
    commit();
    flipMoveClone(card, firstRect, null);
    return;
  }

  commit();
}

/**
 * resetDeck: 全戻し（旧仕様のログは drawnIds のカードを手札へ引く）
 * タスクB追補: 前進再生かつ縦向きスマホでは、引いた各カードをデッキ演出の錨
 * （_getDeckAnchorRect）から 80ms ずつずらして飛ばす（既存の draw/moveCard の演出と同じ考え方。
 * 第6次検収2c: 60→80ms）。
 */
function _applyResetDeck(event) {
  const { drawnIds } = event;
  returnAllCardsToDeck(); // 内部で applyCardState({}) が呼ばれる
  if (!drawnIds || !drawnIds.length) return;
  const center = document.querySelector('.center-slot');
  if (!center) return;

  const deckBtnRect = _shouldMobileFly() ? _getDeckAnchorRect() : null; // 第6次検収2a
  if (deckBtnRect) _pulseDeckBtn();

  drawnIds.forEach((cardId, i) => {
    const card = _findCardWrapper(cardId);
    if (!card) return;
    card.style = '';
    center.appendChild(card);
    if (deckBtnRect) _flyFromDeckBtn(card, deckBtnRect, i * DECK_FLY_STAGGER_MS);
  });
}

/**
 * counterChange: カウンターの作成・値の変更・取り外し。
 * cardId なしは右パネルの基準カウンター（値の変更のみ）。
 * カード上は index（そのカード上のカウンターの並び順。省略時 0）で特定し、
 * op は 'add'（置く）/ 'remove'（外す）/ それ以外（省略含む）は値の変更。
 */
function _applyCounterChange(event) {
  const { cardId, counter, op } = event;
  if (!cardId) {
    // 盤面カウンター（id=parent）
    const tb = document.querySelector('#parent .counter-textbox');
    if (tb) tb.value = counter;
    return;
  }
  // カードに付いたカウンター（id属性で探す）
  const wrapper = _findCardWrapper(cardId);
  if (!wrapper) return;
  const index = Number.isInteger(event.index) ? event.index : 0;
  const list = Array.from(wrapper.querySelectorAll(':scope > .counter-container'));
  if (op === 'add') {
    wrapper.appendChild(createCardCounter(counter));
  } else if (op === 'remove') {
    list[index]?.remove();
  } else {
    const tb = list[index]?.querySelector('.counter-textbox');
    if (tb) tb.value = counter;
  }
}

/** removeCard: トークンを盤面から消す（辞書にはカードが残るので、巻き戻しでは作り直される） */
function _applyRemoveCard(event) {
  const card = _findCardWrapper(event.cardId);
  if (!card || !isTokenCard(card)) return; // トークン以外は消さない（手で作った記録などで普通のカードが消えないように）
  const sourceSlot = card.parentElement;
  card.remove();
  restackCustomSlot(sourceSlot); // フィールドの重ねを詰め直す（.custom-slot 以外は何もしない）
}

// ── ゾーン/カード検索ヘルパ ──────────────────────────────

function _findCardWrapper(cardId) {
  // .tier-item-wrapper は id が "item-NNN normal" or "item-NNN ex"
  // img の id は "item-NNN"
  const img = document.getElementById(cardId);
  if (!img) return null;
  return img.closest('.tier-item-wrapper') ?? null;
}

function _findZone(zoneId) {
  if (!zoneId) return null;
  if (zoneId === 'poolRow' || zoneId === 'poolRow2') {
    return document.getElementById(zoneId);
  }
  if (zoneId === 'center-slot') {
    return document.querySelector('.center-slot');
  }
  if (zoneId === 'free-space') {
    // free-space内のside-slot
    const fs = document.getElementById('free-space');
    return fs?.querySelector('.side-slot') ?? null;
  }
  // custom-slot-N
  const csMatch = zoneId.match(/^custom-slot-(\d+)$/);
  if (csMatch) {
    return document.querySelector(`.custom-slot[data-slot="${csMatch[1]}"]`);
  }
  // side-slot-N
  const ssMatch = zoneId.match(/^side-slot-(\d+)$/);
  if (ssMatch) {
    const allSideSlots = document.querySelectorAll('.side-slot');
    return allSideSlots[parseInt(ssMatch[1], 10)] ?? null;
  }
  return null;
}

function _placeCardInZone(zone, card, zIndex, beforeCardId) {
  const zoneCls = zone.className || '';
  // 移動元スロット（フィールドから出ていく場合に残りのカードを詰め直すため）
  const sourceSlot = card.parentElement;

  if (zoneCls.includes('tier-row')) {
    // プール（並び替え）。beforeCardId があれば、そのカードの手前へ差し込む
    // （無い・見つからない・同じプールにいない場合は従来どおり末尾）
    card.style = '';
    const ref = beforeCardId ? _findCardWrapper(beforeCardId) : null;
    if (ref && ref !== card && ref.parentElement === zone) {
      zone.insertBefore(card, ref);
    } else {
      zone.appendChild(card);
    }
  } else if (zoneCls.includes('custom-slot')) {
    // フィールドスロット（重ね置き）
    // DOM 上の位置だけ決め、z-index / top は restackCustomSlot が DOM 順から詰め直す。
    // 下重ね = 自分以外の既存カードがあり、かつログの zIndex が 1 以下 → 先頭（最背面）へ。
    // それ以外 → 末尾（最前面）へ。
    // 空スロットでも card.style='' はせず drag-drop 側と同じ（absolute / top 0）にそろえる。
    // 表示形式（守備・裏）は data 属性／クラスと transform で保持され、呼び出し側が先に適用済み。
    const zNum = parseInt(zIndex ?? '1', 10);
    const others = Array.from(zone.querySelectorAll(':scope > .tier-item-wrapper')).filter(el => el !== card);
    if (others.length > 0 && zNum <= 1) {
      zone.insertBefore(card, zone.firstChild);
    } else {
      zone.appendChild(card);
    }
    restackCustomSlot(zone);
  } else {
    // center-slot / side-slot / free-space
    card.style = '';
    zone.appendChild(card);
  }

  if (sourceSlot && sourceSlot !== zone) {
    restackCustomSlot(sourceSlot);
  }
}

// ── 再生制御 ──────────────────────────────────────────────

function _startPlay() {
  if (_cursor >= _logs.length - 1) {
    // 末尾なら先頭から再生
    _replayTo(-1);
  }
  _playing = true;
  _updateUI();
  _playNext();
}

function _stopPlay() {
  _playing = false;
  if (_playTimer) { clearTimeout(_playTimer); _playTimer = null; }
  _updateUI();
}

function _playNext() {
  if (!_playing || _cursor >= _logs.length - 1) {
    _stopPlay();
    return;
  }
  stepForward();
  _playTimer = setTimeout(_playNext, DEFAULT_INTERVAL_MS / _speedMultiplier);
}

// ── UI同期 ──────────────────────────────────────────────

function _updateUI() {
  const total = _logs.length;
  const cur = _cursor;

  const slider = document.getElementById('replaySlider');
  const counter = document.getElementById('replayCounter');
  const btnBack = document.getElementById('replayBack');
  const btnFwd = document.getElementById('replayFwd');
  const btnPlay = document.getElementById('replayPlay');
  const btnUndo = document.getElementById('replayUndo');

  if (slider) {
    slider.max = Math.max(0, total - 1);
    slider.value = Math.max(0, cur);
    slider.disabled = total === 0;
  }
  if (counter) {
    counter.textContent = total === 0 ? '0/0' : `${cur + 1}/${total}`;
  }
  if (btnBack) btnBack.disabled = cur <= -1 || total === 0;
  if (btnFwd)  btnFwd.disabled  = cur >= total - 1 || total === 0;
  if (btnUndo) btnUndo.disabled = total === 0 || cur <= -1 || _playing;
  if (btnPlay) {
    btnPlay.textContent = _playing ? '停止' : '再生';
    btnPlay.classList.toggle('play-active', _playing);
    btnPlay.disabled = total === 0;
  }
}

/**
 * カーソル位置のイベントがコメントなら トーストを表示する
 * @param {number} cursor
 */
function _maybeShowCommentToast(cursor) {
  if (cursor < 0 || cursor >= _logs.length) return;
  const ev = _logs[cursor];
  if (ev?.actionType !== 'comment') return;
  _showCommentToast(ev.text);
}

function _showCommentToast(text) {
  let toast = document.getElementById('replayCommentToast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'replayCommentToast';
    document.body.appendChild(toast);
  }
  toast.textContent = text;
  toast.classList.remove('rct-hide');
  toast.classList.add('rct-show');
  clearTimeout(toast._hideTimer);
  toast._hideTimer = setTimeout(() => {
    toast.classList.remove('rct-show');
    toast.classList.add('rct-hide');
  }, 3500);
}
