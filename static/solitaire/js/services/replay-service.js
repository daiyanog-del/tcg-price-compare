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
 *   { seq, actionType, cardId?, zoneId?, zIndex?, transform?, orientation?, face?, counter?, text? }
 *
 * actionType:
 *   moveCard       … カードをゾーンに移動（orientation/face も含む）
 *   draw           … デッキからドロー（cardId=ドローしたカードID）
 *   returnToDeck   … カードをデッキに戻す
 *   resetDeck      … 全リセット&5ドロー
 *   counterChange  … カウンター値変更（counter: 新しい値）
 *   comment        … コメント挿入
 *   activateEffect … 効果発動（cardId のカードで発動演出を再生）
 *
 * ゾーンID（Fugartaの要素に対応）:
 *   poolRow, poolRow2,
 *   custom-slot-N (N=1〜14),
 *   side-slot-0 (墓地), side-slot-1 (除外),
 *   center-slot,
 *   free-space
 */

import { returnAllCardsToDeck, attachCardImageListeners } from '../components/card-manager.js';
import { applyCardState, getCardState } from '../components/card-state.js';
import { playActivateEffect, flipMoveClone, playSetFlip } from '../components/card-effects.js';
import { createProxyCardElement } from '../components/proxy-card.js';

const _API_CARD_IMAGES = '/api/card-images';

// ── 状態 ──────────────────────────────────────────────
let _images = {};       // { cardId: src }
let _names  = {};       // { cardId: cardName }
let _exCardIds = new Set(); // EXデッキに属するカードIDの集合
let _logs   = [];       // イベントログ配列（手数として扱う。setup ログは含まない）
// D-3: 範囲共有（設計書 §D）で先頭に付く「初期配置」ログ。setup:true が付いた要素のみを
// 保持し、_setReplayData / importReplay / importFromURLHash 時に初期配置として即時適用する。
// カウンター・スライダー・前後/再生の対象は _logs のみで、_setupLogs は手数に数えない。
let _setupLogs = [];
let _cursor = -1;       // 現在の再生位置（-1 = ログ先頭の盤面外）
let _playing = false;
let _playTimer = null;
const PLAY_INTERVAL_MS = 600;

// リプレイ前進時のみ true にしてアニメーション（FLIP / 発動演出）を有効化。
// _replayTo 経由の後退・全再構築では false のまま（瞬間適用）。
let _animateForward = false;

// ── 外部API ──────────────────────────────────────────────

/**
 * リプレイシステムを初期化（ページロード時に呼ぶ）
 */
export function initReplay() {
  _images = {};
  _names  = {};
  _exCardIds = new Set();
  _logs = [];
  _setupLogs = [];
  _cursor = -1;
  _playing = false;
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
  // 再生中の巻き戻し後は未来ログを切り捨て
  if (_cursor < _logs.length - 1 && _cursor >= 0) {
    _logs = _logs.slice(0, _cursor + 1);
  }
  const seq = _logs.length;
  _logs.push({ seq, ..._safeCloneEvent(event) });
  _cursor = _logs.length - 1;
  _updateUI();
}

/** undo: 最後のイベントを1件取り消し */
export function undoLast() {
  if (_logs.length === 0) return;
  _logs.pop();
  _cursor = _logs.length - 1;
  _replayTo(_cursor);
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

/** 再生/一時停止 */
export function togglePlay() {
  if (_playing) {
    _stopPlay();
  } else {
    _startPlay();
  }
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
 * @param {string} title
 */
export function buildReplayPayload(title = 'replay') {
  return { version: 1, title, images: _images, names: _names, exCardIds: [..._exCardIds], logs: [..._setupLogs, ..._logs] };
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
 * importReplay（ファイル読込）と E-3 保存済みリプレイの「開く」の両方から呼ぶ
 * （「開く」は importReplay と同じ経路で読み込む、という設計書の指示のための共通化）。
 * D-4: setup 付きログもそのまま通す（加工しない）。
 * @param {{images:Object, names?:Object, exCardIds?:Array, logs:Array}} payload
 */
async function _applyReplayPayload(payload) {
  _images = payload.images;
  _names  = payload.names || {};
  _exCardIds = new Set(payload.exCardIds || []);
  const split = _splitSetupLogs(payload.logs);
  _setupLogs = split.setupLogs;
  _logs = split.logs;
  _cursor = -1;
  // card:// センチネルの解決を含むため await が必要
  await _createReplayCardElements();
  _applySetupLogs();
  _updateUI();
}

/** リプレイJSONをインポートして盤面に反映。D-4: setup 付きログもそのまま通す（加工しない） */
export async function importReplay(file) {
  const text = await file.text();
  const payload = JSON.parse(text);
  if (!payload.logs || !payload.images) throw new Error('不正なリプレイファイルです');
  await _applyReplayPayload(payload);
}

/**
 * E-3: 端末保存された圧縮リプレイデータ（LZString.compress の出力）を復元する。
 * importReplay と同じ _applyReplayPayload を経由する（ロジックを複製しない）。
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
    const payload = { version: 1, images: _images, names: _names, exCardIds: [..._exCardIds], logs };
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
/** EXカードIDの配列を取得（外部からの参照用） */
export function getExCardIds() { return [..._exCardIds]; }
/** ログを取得 */
export function getLogs() { return _logs; }
/** High-1: setup ログ（初期配置。範囲共有の再共有時に先頭へ結合するため）を取得 */
export function getSetupLogs() { return _setupLogs; }

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
 * 外部から画像辞書・ログを直接セット（Supabase読み込み用）
 * card:// センチネル対応のため非同期化。
 * @param {Object} images  { cardId: src }
 * @param {Object} names   { cardId: cardName }（省略可）
 * @param {Array}  logs    イベントログ配列
 */
export async function _setReplayData(images, names, logs, exCardIds = []) {
  _images = images;
  _names  = names || {};
  _exCardIds = new Set(exCardIds);
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
function _replayTo(target) {
  _rebuildDeck();
  // D-3: setup ログ（初期配置）を通常ログより先に、演出なしで再適用する
  _applySetupLogs();
  for (let i = 0; i <= target; i++) {
    _applyEvent(_logs[i]);
  }
  _cursor = target;
  _maybeShowCommentToast(_cursor);
  _updateUI();
}

/**
 * デッキを再構築（全カードをプールに戻す）
 * - Fugartaの returnAllCardsToDeck を流用
 */
function _rebuildDeck() {
  returnAllCardsToDeck();
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
    const wrapper = document.createElement('div');
    wrapper.classList.add('tier-item-wrapper');
    wrapper.id = `${cardId} ${isEx ? 'ex' : 'normal'}`;

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
    (isEx ? poolRow2 : poolRow).appendChild(wrapper);
  }
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

  card.style.transform = transform || '';

  // 後退・全再構築（_replayTo経由）: アニメなしで即時適用
  if (!_animateForward) {
    applyCardState(card, { orientation, face });
    _placeCardInZone(zone, card, zIndex);
    return;
  }

  // ── 前進再生 ─────────────────────────────────────────────────────────
  const firstRect = card.getBoundingClientRect();
  const sameZone  = (card.parentElement === zone);
  const prevState = getCardState(card);
  const faceChanged   = prevState.face        !== (face        || '');
  const orientChanged = prevState.orientation !== (orientation || '');
  const stateChanged  = faceChanged || orientChanged;

  if (sameZone && stateChanged) {
    // ── 同一ゾーン内の状態変更（守備/セット切替など） ──────────────────
    // 移動なし（dx≈0）のため flipMoveClone がすぐ onComplete を呼ぶ。
    // 状態はコールバック内で変更し、変化アニメ（回転/フリップ）を見せる。
    _placeCardInZone(zone, card, zIndex);
    flipMoveClone(card, firstRect, () => {
      if (faceChanged) {
        playSetFlip(card, () => applyCardState(card, { orientation, face }));
      } else {
        applyCardState(card, { orientation, face }); // CSS transition が回転を担う
      }
    });

  } else if (!sameZone && stateChanged) {
    // ── 別ゾーンへ移動 ＋ 状態変化（初期配置で守備/セット など） ───────
    // 状態変更「前」に .tier-item の transition を止める。
    // こうすることでオリジナルも cloneNode で生成するクローンも
    // 挿入時に 0° から再起動せず、最初から最終状態（守備回転済み等）になる。
    const tierItem = card.querySelector('.tier-item');
    if (tierItem) tierItem.style.transition = 'none';
    applyCardState(card, { orientation, face }); // 即時・アニメなしで状態確定
    _placeCardInZone(zone, card, zIndex);
    flipMoveClone(card, firstRect, () => {
      // FLIP 完了後にトランジションを復元（次の操作から回転アニメが効くよう）
      if (tierItem) tierItem.style.transition = '';
    });

  } else {
    // ── 状態変化なし ──────────────────────────────────────────────────
    applyCardState(card, { orientation, face });
    _placeCardInZone(zone, card, zIndex);
    flipMoveClone(card, firstRect, null);
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

/** draw: cardId をデッキから手札(center-slot)へ（状態クリア） */
function _applyDraw(event) {
  const { cardId } = event;
  const card = _findCardWrapper(cardId);
  if (!card) return;
  const center = document.querySelector('.center-slot');
  if (!center) return;
  card.style = '';
  applyCardState(card, {}); // 守備・セット状態をクリア
  center.appendChild(card);
}

/** returnToDeck: cardId をプールへ戻す（状態クリア） */
function _applyReturnToDeck(event) {
  const { cardId, isEx } = event;
  const card = _findCardWrapper(cardId);
  if (!card) return;
  const poolId = isEx ? 'poolRow2' : 'poolRow';
  const pool = document.getElementById(poolId);
  if (!pool) return;
  card.style = '';
  applyCardState(card, {}); // 守備・セット状態をクリア
  pool.appendChild(card);
}

/** resetDeck: 全戻し&5ドロー */
function _applyResetDeck(event) {
  const { drawnIds } = event;
  returnAllCardsToDeck(); // 内部で applyCardState({}) が呼ばれる
  if (!drawnIds || !drawnIds.length) return;
  const center = document.querySelector('.center-slot');
  if (!center) return;
  drawnIds.forEach(cardId => {
    const card = _findCardWrapper(cardId);
    if (card) {
      card.style = '';
      center.appendChild(card);
    }
  });
}

/** counterChange: カウンター値を更新 */
function _applyCounterChange(event) {
  const { cardId, counter } = event;
  if (cardId) {
    // カードに付いたカウンター（id属性で探す）
    const wrapper = _findCardWrapper(cardId);
    if (!wrapper) return;
    const tb = wrapper.querySelector('.counter-textbox');
    if (tb) tb.value = counter;
  } else {
    // 盤面カウンター（id=parent）
    const tb = document.querySelector('#parent .counter-textbox');
    if (tb) tb.value = counter;
  }
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

function _placeCardInZone(zone, card, zIndex) {
  const zoneCls = zone.className || '';
  if (zoneCls.includes('tier-row')) {
    // プール（並び替え）
    card.style = '';
    zone.appendChild(card);
  } else if (zoneCls.includes('custom-slot')) {
    // フィールドスロット（重ね置き）
    const existingItems = Array.from(zone.querySelectorAll('.tier-item-wrapper'));
    const zNum = parseInt(zIndex ?? '1', 10);

    if (existingItems.length > 0 && existingItems[0] !== card) {
      const baseZ = 1;

      if (zNum <= 1 && existingItems.length > 0) {
        // 下重ね（zIndex=1 かつ既存カードがある場合）
        const others = existingItems.filter(el => el !== card);
        // 既存カードを1段ずつ下へずらし、差し込むカードをスロット上端に置く。
        // 通常積みと同じ方向（上端に古いカード、下へ向かって新しいカード）になる。
        others.forEach((el, idx) => {
          el.style.position = 'absolute';
          el.style.zIndex   = String(baseZ + 1 + idx);
          el.style.top      = `calc(var(--slot-width) * 0.${idx + 1})`;
        });
        card.style.position = 'absolute';
        card.style.zIndex   = String(baseZ);
        card.style.top      = '0';
        zone.insertBefore(card, zone.firstChild);
      } else {
        // 通常の上重ね
        existingItems.forEach((item, idx) => {
          item.style.position = 'absolute';
          item.style.zIndex   = `${baseZ + idx}`;
        });
        card.style.position = 'absolute';
        card.style.top      = `calc(var(--slot-width) * 0.${existingItems.length})`;
        card.style.zIndex   = zIndex ?? `${baseZ + existingItems.length}`;
        zone.appendChild(card);
      }
    } else {
      card.style = '';
      zone.appendChild(card);
    }
  } else {
    // center-slot / side-slot / free-space
    card.style = '';
    zone.appendChild(card);
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
  _playTimer = setTimeout(_playNext, PLAY_INTERVAL_MS);
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
  if (btnUndo) btnUndo.disabled = total === 0;
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
