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

// ── 状態 ──────────────────────────────────────────────
let _images = {};   // { cardId: src }
let _names  = {};   // { cardId: cardName }
let _logs   = [];   // イベントログ配列
let _cursor = -1;   // 現在の再生位置（-1 = ログ先頭の盤面外）
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
  _logs = [];
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

/** リプレイデータをエクスポート（ファイル保存用） */
export function exportReplay(title = 'replay') {
  const payload = { version: 1, title, images: _images, names: _names, logs: _logs };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${title.replace(/\s/g, '_')}.json`;
  a.click();
  URL.revokeObjectURL(url);
}

/** リプレイJSONをインポートして盤面に反映 */
export async function importReplay(file) {
  const text = await file.text();
  const payload = JSON.parse(text);
  if (!payload.logs || !payload.images) throw new Error('不正なリプレイファイルです');
  _images = payload.images;
  _names  = payload.names || {};
  _logs = payload.logs;
  _cursor = -1;
  _createReplayCardElements();
  _updateUI();
}

/** テキスト/メタデッキ読込時のURL共有用 LZString圧縮エクスポート */
export function exportAsURLHash() {
  if (typeof LZString === 'undefined') return null;
  try {
    const payload = { version: 1, images: _images, names: _names, logs: _logs };
    return LZString.compressToEncodedURIComponent(JSON.stringify(payload));
  } catch {
    return null;
  }
}

/** URLハッシュから読み込み */
export function importFromURLHash(hash) {
  if (typeof LZString === 'undefined') return false;
  try {
    const json = LZString.decompressFromEncodedURIComponent(hash);
    const payload = JSON.parse(json);
    if (!payload.logs || !payload.images) return false;
    _images = payload.images;
    _names  = payload.names || {};
    _logs = payload.logs;
    _cursor = -1;
    _createReplayCardElements();
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
/** ログを取得 */
export function getLogs() { return _logs; }

/**
 * 外部から画像辞書・ログを直接セット（Supabase読み込み用）
 * @param {Object} images  { cardId: src }
 * @param {Object} names   { cardId: cardName }（省略可）
 * @param {Array}  logs    イベントログ配列
 */
export function _setReplayData(images, names, logs) {
  _images = images;
  _names  = names || {};
  _logs = logs;
  _cursor = -1;
  _createReplayCardElements();
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
  for (let i = 0; i <= target; i++) {
    _applyEvent(_logs[i]);
  }
  _cursor = target;
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
 * _images の全カードをDOMに生成してプールに配置する
 * URL共有・ファイルインポート時、デッキが空の状態から再生できるようにする
 */
function _createReplayCardElements() {
  // 既存カードを全てプールに回収してからクリア
  returnAllCardsToDeck();
  const poolRow  = document.getElementById('poolRow');
  const poolRow2 = document.getElementById('poolRow2');
  if (!poolRow || !poolRow2) return;
  poolRow.querySelectorAll('.tier-item-wrapper').forEach(el => el.remove());
  poolRow2.querySelectorAll('.tier-item-wrapper').forEach(el => el.remove());

  // ログからEXカードのIDを特定
  const exCardIds = new Set(
    _logs.filter(e => e.actionType === 'returnToDeck' && e.isEx).map(e => e.cardId)
  );

  for (const [cardId, src] of Object.entries(_images)) {
    const isEx = exCardIds.has(cardId);
    const wrapper = document.createElement('div');
    wrapper.classList.add('tier-item-wrapper');
    wrapper.id = `${cardId} ${isEx ? 'ex' : 'normal'}`;

    const img = document.createElement('img');
    img.src = src;
    img.classList.add('tier-item');
    img.setAttribute('draggable', 'true');
    img.id = cardId;
    if (_names[cardId]) img.dataset.cardName = _names[cardId];

    attachCardImageListeners(wrapper, img);
    wrapper.appendChild(img);
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
    const allSideSlots = document.querySelectorAll('.side-slot-group:not(#free-space .side-slot-group) .side-slot');
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
